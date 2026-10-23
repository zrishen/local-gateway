"""Select the fastest verified node from the configured region pool.

The top-level `sakura` selector normally points at one region group. When
`SakuraSelectionRegions` is configured, the selector measures every
non-multiplier node in those regions and chooses the fastest candidate that
passes real HTTPS and ChatGPT checks through the local gateway.
"""
import argparse
import concurrent.futures
import http.client
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

PROBE_URLS = (
    'https://www.gstatic.com/generate_204',
    'https://cp.cloudflare.com/generate_204',
    'https://www.google.com/generate_204',
)
QUALITY_HOST = 'www.gstatic.com'
QUALITY_PATH = '/webp/gallery/1.webp'
QUALITY_BYTES = 65536
CHATGPT_HOST = 'chatgpt.com'
CHATGPT_PATH = '/robots.txt'
HEALTH_HOST = 'speed.cloudflare.com'
HEALTH_PATH = '/__down?bytes=65536'
HEALTH_BYTES = 65536
HEALTH_FAILURE_LIMIT = 2
HEALTH_CANDIDATES = 3
HEALTH_IMPROVEMENT_RATIO = 1.25
HEALTH_IMPROVEMENT_KBPS = 8
HEALTH_REJECTION_SECONDS = 1800
CROSS_REGION_PROBES_PER_REGION = 2
CROSS_REGION_CANDIDATES = 4


def pick_lowest(results):
    valid = [item for item in results if item.get('delay') and item['delay'] > 0]
    return min(valid, key=lambda item: (item['delay'], item['tag'])) if valid else None


def classify_health(result, minimum_throughput_kbps):
    if not result.get('ok') or not result.get('complete'):
        return 'disconnected'
    if float(result.get('throughput_kbps', 0)) < minimum_throughput_kbps:
        return 'low'
    return 'healthy'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


class LatencySelector:
    def __init__(self, root, settings, interval=None, timeout=8, workers=9):
        self.root = Path(root)
        self.port = int(settings['ControllerPort'])
        self.mixed_port = int(settings['MixedPort'])
        self.secret = (self.root / 'state/controller-secret.txt').read_text().strip()
        if interval is None:
            minutes = float(settings.get('SakuraLatencyIntervalMinutes', 30))
            interval = max(60, int(minutes * 60))
        self.interval = int(interval)
        health_minutes = float(settings.get('SakuraHealthCheckMinutes', 5))
        self.health_interval = max(0, int(health_minutes * 60))
        self.timeout = timeout
        self.workers = workers
        self.minimum_throughput_kbps = max(0, int(settings.get('SakuraMinimumThroughputKBps', 50)))
        self.tun_validation = bool(settings.get('SakuraTunValidation', True))
        self.preferred_region = str(settings.get('SakuraRegion') or '美国')
        configured_regions = settings.get('SakuraSelectionRegions') or []
        if isinstance(configured_regions, str):
            configured_regions = [configured_regions]
        self.selection_regions = list(dict.fromkeys(
            str(value).strip() for value in configured_regions
            if isinstance(configured_regions, (list, tuple)) and str(value).strip()))
        self.cross_region_fallback = bool(settings.get('SakuraCrossRegionFallback', True))
        configured_fallbacks = settings.get('SakuraFallbackRegions') or []
        self.fallback_regions = ({str(value) for value in configured_fallbacks}
                                 if isinstance(configured_fallbacks, (list, tuple)) else set())
        configured_nodes = settings.get('SakuraPreferredFallbackNodes') or []
        self.preferred_fallback_nodes = list(dict.fromkeys(
            str(value) for value in configured_nodes
            if isinstance(configured_nodes, (list, tuple)) and str(value).strip()))
        self.stop = threading.Event()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.health_failures = 0
        self.pending_switch = None
        self.rejected_candidates = {}
        self.last_health = {}
        self._load_health_state()

    def request(self, path, method='GET', body=None):
        headers = {'Authorization': 'Bearer ' + self.secret}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data,
                                         headers=headers, method=method)
        with self.opener.open(request, timeout=self.timeout + 2) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def state(self):
        return json.loads((self.root / 'state/sakura-outbounds.json').read_text(encoding='utf-8'))

    def _load_health_state(self):
        path = self.root / 'state/sakura-health.json'
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(value, dict):
            return
        try:
            self.health_failures = max(0, int(value.get('consecutive_below_threshold', 0)))
        except (TypeError, ValueError):
            self.health_failures = 0
        pending = value.get('pending_switch')
        self.pending_switch = pending if isinstance(pending, dict) else None
        rejected = value.get('rejected_candidates')
        if isinstance(rejected, dict):
            now = time.time()
            self.rejected_candidates = {
                str(tag): float(until) for tag, until in rejected.items()
                if isinstance(until, (int, float)) and float(until) > now
            }
        last = value.get('last_health')
        self.last_health = last if isinstance(last, dict) else {}

    def _save_health_state(self, **extra):
        now = time.time()
        self.rejected_candidates = {
            tag: until for tag, until in self.rejected_candidates.items() if until > now
        }
        value = {
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'consecutive_below_threshold': self.health_failures,
            'pending_switch': self.pending_switch,
            'rejected_candidates': self.rejected_candidates,
            'last_health': self.last_health,
            **extra,
        }
        write_json(self.root / 'state/sakura-health.json', value)

    def _reset_health_state(self, current=None):
        self.health_failures = 0
        self.pending_switch = None
        self.last_health = {'tag': current} if current else {}
        self._save_health_state()

    def _small_quality(self):
        '''Measure a 64 KiB body through the local mixed proxy.

        Body time excludes CONNECT/TLS setup so a high-RTT but usable node is
        not classified as low throughput. A short/incomplete body is treated as
        a stream interruption and can trigger recovery immediately.
        '''
        connection = http.client.HTTPSConnection(
            '127.0.0.1', self.mixed_port, timeout=self.timeout,
            context=ssl.create_default_context())
        started = time.monotonic()
        try:
            connection.set_tunnel(HEALTH_HOST, 443)
            path = HEALTH_PATH + '&health=' + str(time.time_ns())
            connection.request('GET', path, headers={
                'User-Agent': 'UnifiedHealth/2', 'Accept-Encoding': 'identity',
                'Connection': 'close'})
            response = connection.getresponse()
            first_byte = time.monotonic()
            body = response.read(HEALTH_BYTES + 1)
            ended = time.monotonic()
            complete = response.status == 200 and len(body) == HEALTH_BYTES
            elapsed = max(.001, ended - first_byte)
            throughput = round(len(body) / elapsed / 1024, 1)
            result = {
                'ok': complete,
                'complete': complete,
                'status': response.status,
                'bytes': len(body),
                'connect_ms': int((first_byte - started) * 1000),
                'throughput_kbps': throughput,
            }
        except (OSError, http.client.HTTPException, ValueError, ssl.SSLError) as error:
            result = {
                'ok': False,
                'complete': False,
                'error': type(error).__name__,
                'throughput_kbps': 0,
                'elapsed_ms': int((time.monotonic() - started) * 1000),
            }
        finally:
            connection.close()
        result['health'] = classify_health(result, self.minimum_throughput_kbps)
        return result

    def _region_context(self, top):
        region, info, data = self.active_region(top)
        if not region or not info:
            return None
        group = self.request('/proxies/' + urllib.parse.quote(info['group']))
        current = str(group.get('now') or '')
        if not current:
            return None
        return region, info, data, current

    def active_region(self, top):
        data = self.state()
        groups = data.get('region_groups') or {}
        current = str(top.get('now') or '')
        for region, info in groups.items():
            if info.get('group') == current:
                return region, info, data
        for region, info in groups.items():
            if current in info.get('nodes', []):
                return region, info, data
        return None, None, data

    def measure(self, tag):
        """Try independent HTTPS endpoints and keep the best successful RTT.

        A single endpoint can be blocked or temporarily slow for a healthy node.
        Multiple fixed-size probes keep one failed route from hiding a usable node.
        """
        delays = []
        errors = []
        for url in PROBE_URLS:
            query = urllib.parse.urlencode({'url': url, 'timeout': int(self.timeout * 1000)})
            try:
                payload = self.request('/proxies/' + urllib.parse.quote(tag, safe='') + '/delay?' + query)
                delay = int(payload.get('delay', 0))
                if delay > 0:
                    delays.append(delay)
            except Exception as error:
                errors.append(f'{url}: {type(error).__name__}')
        if delays:
            return min(delays)
        raise ValueError('all probe endpoints failed: ' + '; '.join(errors))

    def _selection_members(self, data):
        names = data.get('names') or {}
        regions = data.get('region_groups') or {}
        members = []
        seen = set()
        available_regions = []
        for region in self.selection_regions:
            info = regions.get(region) or {}
            if not info.get('group'):
                continue
            available_regions.append(region)
            for tag in info.get('nodes', []):
                name = names.get(tag, tag)
                if tag in seen or '倍消耗' in name:
                    continue
                seen.add(tag)
                members.append({
                    'tag': tag,
                    'name': name,
                    'region': region,
                    'group': info['group'],
                })
        return available_regions, members

    def select_once(self):
        top = self.request('/proxies/sakura')
        if str(top.get('type', '')).lower() != 'selector':
            return None
        data = self.state()
        if self.selection_regions:
            regions, members = self._selection_members(data)
        else:
            previous_region, previous_info, _ = self.active_region(top)
            region = previous_region or ''
            info = previous_info or {}
            names = data.get('names') or {}
            regions = [region] if region else []
            members = [{'tag': tag, 'name': names.get(tag, tag), 'region': region,
                        'group': info['group']} for tag in info.get('nodes', [])]
        if not members:
            return None

        previous_region, previous_info, _ = self.active_region(top)
        previous_group = str((previous_info or {}).get('group') or '')
        previous = ''
        if previous_group:
            group_state = self.request('/proxies/' + urllib.parse.quote(previous_group))
            previous = str(group_state.get('now') or '')

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.workers, len(members))) as pool:
            futures = {pool.submit(self.measure, item['tag']): item for item in members}
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    delay = future.result()
                except Exception as error:
                    logging.debug('Sakura node %s unavailable: %s', item['tag'], error)
                    continue
                results.append({**item, 'delay': delay})

        if not results:
            if previous_group and previous:
                try:
                    self._set_top_region(previous_group)
                    self._set_group(previous_group, previous)
                except (OSError, http.client.HTTPException, ValueError):
                    pass
            write_json(self.root / 'state/sakura-selection.json', {
                'ok': False,
                'regions': regions,
                'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'error': 'no reachable nodes',
            })
            return None

        results.sort(key=lambda item: (item['delay'], item['tag']))
        verified = []
        for candidate in results:
            try:
                # Mixed proxy traffic follows the selected region. Test each
                # candidate through its own group so US and Singapore compete
                # on the same real transfer and ChatGPT checks.
                self._set_top_region(candidate['group'])
                self._set_group(candidate['group'], candidate['tag'])
                quality = self.verify_through_proxy()
                if quality.get('ok'):
                    verified.append({**candidate, **quality})
            except (OSError, http.client.HTTPException, ValueError, ssl.SSLError) as error:
                logging.debug('Sakura quality probe %s failed: %s', candidate['tag'], error)

        chatgpt_ready = [item for item in verified if item.get('chatgpt_ok')]
        pool = chatgpt_ready or verified
        # Practical network speed is the verified response throughput; RTT is
        # the tie breaker. This lets a faster Singapore node beat US directly.
        ranked = sorted(pool, key=lambda item: (
            item.get('throughput_kbps', 0), -item.get('delay', 0)), reverse=True)
        if not ranked:
            if previous_group and previous:
                try:
                    self._set_top_region(previous_group)
                    self._set_group(previous_group, previous)
                except (OSError, http.client.HTTPException, ValueError):
                    pass
            write_json(self.root / 'state/sakura-selection.json', {
                'ok': False,
                'regions': regions,
                'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'error': 'no node passed the end-to-end check',
                'reachable': len(results),
                'total': len(members),
            })
            return None

        members_by_tag = {item['tag']: item for item in members}
        finalists = []
        if previous in members_by_tag:
            current = next((item for item in results if item['tag'] == previous), None)
            finalists.append(current or {**members_by_tag[previous], 'delay': 0})
        for candidate in ranked:
            if candidate['tag'] not in {item['tag'] for item in finalists}:
                finalists.append(candidate)
            if len(finalists) >= 3:
                break

        chosen = ranked[0]
        if self.tun_validation and finalists:
            tun_results = []
            for candidate in finalists:
                actual = self.verify_via_tun(candidate['tag'], candidate['group'])
                if actual.get('tun_download_ok'):
                    tun_results.append({**candidate, **actual})
            if tun_results:
                prefer_chatgpt = [item for item in tun_results if item.get('tun_chatgpt_ok')]
                tun_pool = prefer_chatgpt or tun_results
                chosen = max(tun_pool, key=lambda item: (
                    item.get('tun_throughput_kbps', 0), -item.get('delay', 0)))

        self._set_top_region(chosen['group'])
        self._set_group(chosen['group'], chosen['tag'])
        snapshot = {
            'ok': True,
            'regions': regions,
            'region': chosen['region'],
            'group': chosen['group'],
            'tag': chosen['tag'],
            'name': chosen['name'],
            'delay': chosen['delay'],
            'throughput_kbps': chosen.get('throughput_kbps', 0),
            'first_byte_ms': chosen.get('first_byte_ms', 0),
            'chatgpt_ok': chosen.get('chatgpt_ok', False),
            'chatgpt_status': chosen.get('chatgpt_status'),
            'chatgpt_ms': chosen.get('chatgpt_ms', 0),
            'tun_verified': bool(chosen.get('tun_download_ok')),
            'tun_throughput_kbps': chosen.get('tun_throughput_kbps', 0),
            'tun_first_byte_ms': chosen.get('tun_first_byte_ms', 0),
            'tun_chatgpt_ok': chosen.get('tun_chatgpt_ok', False),
            'tun_chatgpt_ms': chosen.get('tun_chatgpt_ms', 0),
            'quality_verified': (bool(chosen.get('tun_download_ok') and chosen.get('tun_chatgpt_ok')
                                      and chosen.get('tun_throughput_kbps', 0) >= self.minimum_throughput_kbps)
                                 if self.tun_validation else
                                 bool(chosen.get('throughput_kbps', 0) >= self.minimum_throughput_kbps
                                      and chosen.get('chatgpt_ok'))),
            'minimum_throughput_kbps': self.minimum_throughput_kbps,
            'reachable': len(results),
            'total': len(members),
            'verified': True,
            'interval_seconds': self.interval,
            'health_check_seconds': self.health_interval,
            'health_probe_bytes': HEALTH_BYTES,
            'health_failure_limit': HEALTH_FAILURE_LIMIT,
            'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        write_json(self.root / 'state/sakura-selection.json', snapshot)
        self._reset_health_state(chosen['tag'])
        logging.info('Sakura selector: %s %s / %s (%s ms, %s KB/s)',
                     chosen['region'], chosen['tag'], chosen['name'],
                     chosen['delay'], chosen.get('throughput_kbps', 0))
        return snapshot

    def verify_through_proxy(self):
        """Require a real HTTPS transfer before keeping a latency winner.

        The selector first sorts by RTT, then accepts the first candidate that
        clears the optional throughput floor. If every candidate is slow, the
        fastest HTTPS-valid node is retained rather than leaving no route.
        """
        connection = http.client.HTTPSConnection('127.0.0.1', self.mixed_port,
                                                 timeout=self.timeout,
                                                 context=ssl.create_default_context())
        started = time.monotonic()
        try:
            connection.set_tunnel(QUALITY_HOST, 443)
            connection.request('GET', QUALITY_PATH, headers={'User-Agent': 'UnifiedSelector/2'})
            response = connection.getresponse()
            first_byte = time.monotonic()
            if response.status not in (200, 204):
                return {'ok': False, 'status': response.status}
            body = response.read(QUALITY_BYTES + 1)
            elapsed = max(.001, time.monotonic() - started)
            result = {
                'ok': len(body) > 0,
                'status': response.status,
                'bytes': len(body),
                'first_byte_ms': int((first_byte - started) * 1000),
                'throughput_kbps': int(len(body) / elapsed / 1024),
            }
            if result['ok']:
                chatgpt = self.verify_chatgpt()
                result.update(chatgpt)
                if not chatgpt.get('ok'):
                    result['ok'] = False
            return result
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            return {'ok': False, 'error': type(error).__name__}
        finally:
            connection.close()

    def verify_chatgpt(self):
        """Verify the route used by the bundled ChatGPT client.

        `/robots.txt` is a stable small response. The site root may legitimately
        return a challenge or 403 and is therefore unsuitable as a reachability
        test.
        """
        connection = http.client.HTTPSConnection('127.0.0.1', self.mixed_port,
                                                 timeout=self.timeout,
                                                 context=ssl.create_default_context())
        started = time.monotonic()
        try:
            connection.set_tunnel(CHATGPT_HOST, 443)
            connection.request('GET', CHATGPT_PATH,
                               headers={'User-Agent': 'UnifiedSelector/2', 'Connection': 'close'})
            response = connection.getresponse()
            response.read(8192)
            return {
                'chatgpt_ok': response.status == 200,
                'chatgpt_status': response.status,
                'chatgpt_ms': int((time.monotonic() - started) * 1000),
                'ok': response.status == 200,
            }
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            return {'chatgpt_ok': False, 'chatgpt_ms': int((time.monotonic() - started) * 1000),
                    'chatgpt_error': type(error).__name__, 'ok': False}
        finally:
            connection.close()

    def verify_via_tun(self, tag, group):
        """Measure a finalist through the actual TUN path.

        The full-region shortlist is built through the local proxy to avoid
        switching every live connection. The two finalists are then checked
        through the system TUN, which is the path users actually consume.
        """
        self._set_top_region(group)
        self._set_group(group, tag)
        if os.name == 'nt':
            try:
                subprocess.run(['ipconfig', '/flushdns'], capture_output=True,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                pass
        download = self._https_via_tun(
            'speed.cloudflare.com',
            f'/__down?bytes=131072&selector={time.time_ns()}',
            self.timeout, 131072)
        chatgpt_attempts = [
            self._https_via_tun('chatgpt.com', '/robots.txt', min(self.timeout, 8), 8192)
            for _ in range(2)
        ]
        successful = [item for item in chatgpt_attempts if item.get('ok')]
        chatgpt_ok = len(successful) == len(chatgpt_attempts)
        chatgpt_ms = max((item.get('total_ms', 0) for item in successful), default=0)
        return {
            'tun_download_ok': download.get('ok', False),
            'tun_download_status': download.get('status'),
            'tun_throughput_kbps': download.get('throughput_kbps', 0),
            'tun_first_byte_ms': download.get('first_byte_ms', 0),
            'tun_tls_ms': download.get('tls_ms', 0),
            'tun_download_error': download.get('error'),
            'tun_chatgpt_ok': chatgpt_ok,
            'tun_chatgpt_attempts': len(chatgpt_attempts),
            'tun_chatgpt_successes': len(successful),
            'tun_chatgpt_status': successful[-1].get('status') if successful else None,
            'tun_chatgpt_ms': chatgpt_ms,
            'tun_chatgpt_error': next((item.get('error') for item in chatgpt_attempts
                                        if item.get('error')), None),
        }

    def _https_via_tun(self, host, path, timeout, limit):
        connection = None
        started = time.monotonic()
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            resolved = sorted({item[4][0] for item in infos})
            tcp_started = time.monotonic()
            connection = socket.create_connection((host, 443), timeout=timeout)
            tcp_done = time.monotonic()
            secure = ssl.create_default_context().wrap_socket(connection, server_hostname=host)
            connection = secure
            tls_done = time.monotonic()
            request = (f'GET {path} HTTP/1.1\r\nHost: {host}\r\n'
                       'User-Agent: UnifiedSelectorTUN/1\r\nConnection: close\r\n\r\n')
            secure.sendall(request.encode('ascii'))
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = secure.recv(4096)
                if not chunk:
                    break
                data += chunk
            first = time.monotonic()
            status = int(data.split(b' ', 2)[1]) if data.startswith(b'HTTP/') else 0
            body = data.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in data else b''
            while len(body) < limit:
                chunk = secure.recv(65536)
                if not chunk:
                    break
                body += chunk
            end = time.monotonic()
            elapsed = max(.001, end - started)
            return {
                'ok': status in (200, 204),
                'status': status,
                'resolved': resolved[:4],
                'tls_ms': int((tls_done - tcp_done) * 1000),
                'first_byte_ms': int((first - started) * 1000),
                'total_ms': int(elapsed * 1000),
                'bytes': len(body),
                'throughput_kbps': int(len(body) / elapsed / 1024),
            }
        except (OSError, ValueError, ssl.SSLError) as error:
            return {'ok': False, 'error': type(error).__name__,
                    'elapsed_ms': int((time.monotonic() - started) * 1000)}
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    @staticmethod
    def _meaningfully_better(candidate, baseline):
        if candidate.get('health') != 'healthy':
            return False
        measured = float(candidate.get('throughput_kbps', 0))
        required = float(baseline.get('throughput_kbps', 0))
        if required < HEALTH_IMPROVEMENT_KBPS:
            return measured >= HEALTH_IMPROVEMENT_KBPS
        return (measured - required >= HEALTH_IMPROVEMENT_KBPS
                and measured >= required * HEALTH_IMPROVEMENT_RATIO)

    def _set_group(self, group, tag):
        self.request('/proxies/' + urllib.parse.quote(group), 'PUT', {'name': tag})

    def _set_top_region(self, group):
        self.request('/proxies/sakura', 'PUT', {'name': group})

    def _chatgpt_probe(self, tag):
        query = urllib.parse.urlencode({
            'url': 'https://chatgpt.com/robots.txt',
            'timeout': int(self.timeout * 1000),
        })
        payload = self.request(
            '/proxies/' + urllib.parse.quote(tag, safe='') + '/delay?' + query)
        delay = int(payload.get('delay', 0))
        if delay <= 0:
            raise ValueError('ChatGPT probe returned no delay')
        return delay

    def _fallback_region_names(self, data, current_region, only_region=None):
        if not self.cross_region_fallback:
            return []
        available = list((data.get('region_groups') or {}).keys())
        if only_region is not None:
            return [only_region] if only_region in available else []
        if self.fallback_regions:
            names = [name for name in available if name in self.fallback_regions]
        else:
            names = [name for name in available if name != current_region]
        # Prefer returning to the configured primary region first.
        if (current_region != self.preferred_region
                and self.preferred_region in available
                and self.preferred_region not in names):
            names.insert(0, self.preferred_region)
        elif self.preferred_region in names:
            names.remove(self.preferred_region)
            names.insert(0, self.preferred_region)
        return names

    def _find_cross_region_candidate(self, data, current_region, only_region=None):
        '''Find an alternate region only after local recovery is exhausted.'''
        names = data.get('names') or {}
        region_groups = data.get('region_groups') or {}
        allowed_regions = self._fallback_region_names(data, current_region, only_region)
        region_for_tag = {
            tag: region
            for region in allowed_regions
            for tag in (region_groups.get(region) or {}).get('nodes', [])
        }
        probes = []
        probe_keys = set()
        # Explicit nodes are tested first. This keeps a known, measured
        # fallback from being hidden by the per-region shortlist cap.
        for tag in self.preferred_fallback_nodes:
            region = region_for_tag.get(tag)
            name = names.get(tag, tag)
            if (region is None or '倍消耗' in name
                    or self.rejected_candidates.get(tag, 0) > time.time()):
                continue
            probes.append((region, tag))
            probe_keys.add((region, tag))
        for region in allowed_regions:
            info = region_groups.get(region) or {}
            eligible = [tag for tag in info.get('nodes', [])
                        if self.rejected_candidates.get(tag, 0) <= time.time()
                        and '倍消耗' not in names.get(tag, tag)]
            for tag in eligible[:CROSS_REGION_PROBES_PER_REGION]:
                if (region, tag) not in probe_keys:
                    probes.append((region, tag))
                    probe_keys.add((region, tag))
        if not probes:
            return None
        available = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(probes))) as pool:
            futures = {pool.submit(self._chatgpt_probe, tag): (region, tag)
                       for region, tag in probes}
            for future in concurrent.futures.as_completed(futures):
                region, tag = futures[future]
                try:
                    available.append({'region': region, 'tag': tag,
                                      'delay': future.result(),
                                      'preferred': tag in self.preferred_fallback_nodes})
                except Exception as error:
                    logging.debug('Sakura cross-region %s/%s unavailable: %s',
                                  region, tag, error)
                    self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
        available.sort(key=lambda item: (not item['preferred'], item['delay'], item['tag']))
        for candidate in available[:CROSS_REGION_CANDIDATES]:
            region, tag = candidate['region'], candidate['tag']
            info = region_groups[region]
            try:
                self._set_top_region(info['group'])
                self._set_group(info['group'], tag)
                quality = self._small_quality()
                if (quality.get('health') != 'healthy'
                        or float(quality.get('throughput_kbps', 0)) < self.minimum_throughput_kbps):
                    if quality.get('health') == 'disconnected':
                        self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
                    continue
                chatgpt = self.verify_chatgpt()
                if not chatgpt.get('ok'):
                    self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
                    continue
                logging.info('Sakura health: cross-region fallback %s/%s (%s ms, %s KB/s)',
                             region, tag, candidate['delay'], quality.get('throughput_kbps', 0))
                return {'region': region, 'group': info['group'],
                        'tag': tag, 'name': names.get(tag, tag), **candidate,
                        **quality, **chatgpt}
            except (OSError, http.client.HTTPException, ValueError, ssl.SSLError) as error:
                logging.debug('Sakura cross-region %s/%s probe failed: %s',
                              region, tag, error)
                self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
        return None

    def _find_recovery_candidate(self, info, data, current, baseline):
        names = data.get('names') or {}
        eligible = [tag for tag in info.get('nodes', [])
                    if tag != current and self.rejected_candidates.get(tag, 0) <= time.time()]
        if not eligible:
            return None
        delays = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.workers, len(eligible))) as pool:
            futures = {pool.submit(self.measure, tag): tag for tag in eligible}
            for future in concurrent.futures.as_completed(futures):
                tag = futures[future]
                try:
                    delays.append({'tag': tag, 'delay': future.result()})
                except Exception as error:
                    logging.debug('Sakura health candidate %s unavailable: %s', tag, error)
                    self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
        delays.sort(key=lambda item: (item['delay'], item['tag']))
        for candidate in delays[:HEALTH_CANDIDATES]:
            tag = candidate['tag']
            try:
                self._set_group(info['group'], tag)
                quality = self._small_quality()
                if not self._meaningfully_better(quality, baseline):
                    if quality.get('health') == 'disconnected':
                        self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
                    continue
                chatgpt = self.verify_chatgpt()
                if not chatgpt.get('ok'):
                    self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
                    continue
                return {'tag': tag, 'name': names.get(tag, tag), 'delay': candidate['delay'],
                        **quality, **chatgpt}
            except (OSError, http.client.HTTPException, ValueError, ssl.SSLError) as error:
                logging.debug('Sakura health candidate %s probe failed: %s', tag, error)
                self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
        return None

    def _begin_pending_switch(self, region, info, current, chosen, baseline):
        self._set_top_region(chosen['group'])
        self._set_group(chosen['group'], chosen['tag'])
        self.pending_switch = {
            'previous_region': region,
            'previous_group': info['group'],
            'previous_tag': current,
            'switched_region': chosen['region'],
            'switched_group': chosen['group'],
            'switched_tag': chosen['tag'],
            'cross_region': chosen['region'] != region,
            'baseline_kbps': float(baseline.get('throughput_kbps', 0)),
            'baseline_health': baseline.get('health', 'disconnected'),
            'samples': [],
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        self.health_failures = 0
        logging.info(
            'Sakura health: switch %s/%s -> %s/%s (%s KB/s); observing two rounds',
            region, current, chosen['region'], chosen['tag'],
            chosen.get('throughput_kbps', 0))
        return {'action': 'switch', **chosen, 'previous_region': region,
                'previous_tag': current}

    def _recover_current(self, region, info, data, current, baseline):
        if self.selection_regions:
            # A failed current node gets the same full US/Singapore comparison
            # as a periodic scan. There is no US-first recovery preference.
            selected = self.select_once()
            if selected:
                if selected.get('tag') == current:
                    return {'action': 'keep', **selected}
                # The pool scan identifies the fastest node. Keep the existing
                # two-round confirmation before accepting that health recovery.
                return self._begin_pending_switch(
                    region, info, current, selected, baseline)
            try:
                self._set_top_region(info['group'])
                self._set_group(info['group'], current)
            except (OSError, http.client.HTTPException, ValueError):
                pass
            return {'action': 'keep', 'region': region, 'tag': current}
        chosen = self._find_recovery_candidate(info, data, current, baseline)
        if chosen is None:
            # The local candidate probes may leave another node selected even
            # when none qualifies. Restore the live node before a global probe.
            try:
                self._set_group(info['group'], current)
            except (OSError, http.client.HTTPException, ValueError):
                pass
            chosen = self._find_cross_region_candidate(data, region)
            if chosen is not None:
                chosen.update(region=chosen['region'], group=chosen['group'])
            else:
                try:
                    self._set_top_region(info['group'])
                    self._set_group(info['group'], current)
                except (OSError, http.client.HTTPException, ValueError):
                    pass
                self.health_failures = 0
                logging.info('Sakura health: no usable local or fallback node; keeping %s/%s',
                             region, current)
                return {'action': 'keep', 'region': region, 'tag': current}
        if 'region' not in chosen:
            chosen['region'] = region
        if 'group' not in chosen:
            chosen['group'] = info['group']
        return self._begin_pending_switch(region, info, current, chosen, baseline)

    def _try_preferred_region(self):
        '''Periodically try returning from a fallback region to the primary.'''
        if not self.cross_region_fallback or self.pending_switch:
            return None
        top = self.request('/proxies/sakura')
        context = self._region_context(top)
        if context is None:
            return None
        region, info, data, current = context
        if region == self.preferred_region:
            return None
        primary = (data.get('region_groups') or {}).get(self.preferred_region)
        if not primary:
            return None
        chosen = self._find_cross_region_candidate(
            data, region, only_region=self.preferred_region)
        if chosen is None:
            try:
                self._set_top_region(info['group'])
                self._set_group(info['group'], current)
            except (OSError, http.client.HTTPException, ValueError):
                pass
            logging.info('Sakura health: primary region %s still unavailable; keeping %s/%s',
                         self.preferred_region, region, current)
            return None
        baseline = {
            'health': 'blocked',
            'throughput_kbps': self.last_health.get('throughput_kbps', 0),
        }
        return self._begin_pending_switch(region, info, current, chosen, baseline)

    def _finish_pending_switch(self, decision, result, reason):
        previous = decision['previous_tag']
        switched = decision['switched_tag']
        previous_group = (decision.get('previous_group') or decision.get('group')
                          or self.last_health.get('group'))
        previous_region = decision.get('previous_region') or self.last_health.get('region')
        switched_group = decision.get('switched_group') or self.last_health.get('group')
        switched_region = decision.get('switched_region') or self.last_health.get('region')
        if decision.get('restore', False):
            self._set_top_region(previous_group)
            self._set_group(previous_group, previous)
            self.rejected_candidates[switched] = time.time() + HEALTH_REJECTION_SECONDS
            logging.warning(
                'Sakura health: %s; restored %s/%s and rejected %s for %d minutes',
                reason, previous_region, previous, switched, HEALTH_REJECTION_SECONDS // 60)
        else:
            logging.info('Sakura health: kept %s/%s after two rounds (%s KB/s)',
                         switched_region, switched,
                         statistics.median(decision['samples']))
        self.pending_switch = None
        self.health_failures = 0
        restore = decision.get('restore', False)
        outcome = {
            'action': 'restore' if restore else 'accept',
            'region': previous_region if restore else switched_region,
            'tag': previous if restore else switched,
            'reason': reason,
            'samples': decision['samples'],
            'current_health': result,
        }
        self._save_health_state(last_transition=outcome)
        return outcome

    def _observe_pending_switch(self, current, result):
        decision = dict(self.pending_switch)
        current_group = self.last_health.get('group')
        expected_group = decision.get('switched_group') or decision.get('group')
        if (not decision or decision.get('switched_tag') != current
                or (expected_group and current_group != expected_group)):
            self.pending_switch = None
            return None
        decision['samples'] = list(decision.get('samples') or [])
        samples = decision['samples']
        samples.append(float(result.get('throughput_kbps', 0)))
        if result.get('health') == 'disconnected':
            decision.update(restore=True)
            return self._finish_pending_switch(decision, result, 'switched node disconnected')
        if len(samples) < HEALTH_FAILURE_LIMIT:
            self.pending_switch = decision
            self._save_health_state()
            return {'action': 'observe', 'tag': current, 'samples': samples}
        measured = float(statistics.median(samples))
        baseline = float(decision.get('baseline_kbps', 0))
        baseline_health = decision.get('baseline_health', 'disconnected')
        if baseline_health in ('blocked', 'disconnected'):
            throughput_improved = measured >= self.minimum_throughput_kbps
        else:
            throughput_improved = measured >= max(
                self.minimum_throughput_kbps,
                baseline * HEALTH_IMPROVEMENT_RATIO,
                baseline + HEALTH_IMPROVEMENT_KBPS)
        service_ok = result.get('health') == 'healthy' and result.get('chatgpt_ok', False)
        improved = throughput_improved and service_ok
        decision.update(restore=not improved)
        if not improved and not service_ok:
            reason = 'switched node did not restore ChatGPT service'
        else:
            reason = 'no meaningful improvement' if not improved else 'throughput improved'
        return self._finish_pending_switch(decision, result, reason)

    def health_check_once(self):
        top = self.request('/proxies/sakura')
        context = self._region_context(top)
        if context is None:
            return None
        region, info, data, current = context
        result = self._small_quality()
        result['tag'] = current
        if result.get('health') != 'disconnected':
            # A normal Cloudflare transfer does not prove that the ChatGPT
            # control plane is reachable through the same egress.
            chatgpt = self.verify_chatgpt()
            result.update(chatgpt)
            if not chatgpt.get('ok'):
                result['health'] = 'blocked'
        self.last_health = {
            'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'region': region,
            'group': info['group'],
            'tag': current,
            **result,
        }
        outcome = None
        if self.pending_switch:
            outcome = self._observe_pending_switch(current, result)
        elif result['health'] == 'disconnected':
            outcome = self._recover_current(region, info, data, current, result)
        elif result['health'] in ('low', 'blocked'):
            self.health_failures += 1
            logging.info('Sakura health: %s %s (%s KB/s, ChatGPT=%s), %d/%d',
                         current, result['health'], result['throughput_kbps'],
                         result.get('chatgpt_ok', False), self.health_failures,
                         HEALTH_FAILURE_LIMIT)
            if self.health_failures >= HEALTH_FAILURE_LIMIT:
                outcome = self._recover_current(region, info, data, current, result)
        else:
            self.health_failures = 0
        self._save_health_state(last_outcome=outcome)
        return {'current': self.last_health, 'failure_count': self.health_failures,
                'pending_switch': self.pending_switch, 'outcome': outcome}

    def current_tag(self):
        top = self.request('/proxies/sakura')
        current = str(top.get('now') or '')
        if current.startswith('sakura-region-'):
            inner = self.request('/proxies/' + urllib.parse.quote(current))
            return str(inner.get('now') or '')
        return current

    def check_current(self):
        tag = self.current_tag()
        if not tag:
            return None
        try:
            return self.measure(tag)
        except Exception:
            return None

    def run(self):
        last_full = None
        last_health = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                if last_full is None:
                    self.select_once()
                    last_full = time.monotonic()
                    last_health = last_full
                elif (not self.pending_switch and now - last_full >= self.interval):
                    transition = None
                    if not self.selection_regions:
                        transition = self._try_preferred_region()
                    if transition is None:
                        self.select_once()
                    last_full = time.monotonic()
                    last_health = last_full
                elif self.health_interval and now - last_health >= self.health_interval:
                    last_health = now
                    self.health_check_once()
            except Exception as error:
                logging.debug('Sakura latency selection unavailable: %s', error)
            self.stop.wait(30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval-minutes', type=float)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    from settings_config import load_settings
    settings = load_settings(root)
    interval = None
    if args.interval_minutes:
        interval = max(60, int(args.interval_minutes * 60))
    selector = LatencySelector(root, settings, interval=interval)
    if args.once:
        result = selector.select_once()
        if result:
            print(json.dumps(result, ensure_ascii=True))
            return 0
        print('selection failed', file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    selector.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())