"""Select the fastest verified node from the configured region pool.

The top-level `sakura` selector normally points at one region group. When
`SakuraSelectionRegions` is configured, the selector measures every
non-multiplier node in those regions and chooses the fastest candidate that
passes real HTTPS and ChatGPT checks through the local gateway.
"""
import argparse
import concurrent.futures
import http.client
from isolated_probes import IsolatedProbePool
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
import route_policy
from route_policy import classify_health

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


def measured_throughput_kbps(result):
    """Use end-to-end speed when available; fall back for legacy samples."""
    value = result.get('effective_throughput_kbps')
    if value is None:
        value = result.get('throughput_kbps', 0)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def measured_tun_throughput_kbps(result):
    value = result.get('tun_effective_throughput_kbps')
    if value is None:
        value = result.get('tun_throughput_kbps', 0)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


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
        self.policy = route_policy.policy_from_settings(settings)
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
        self.recovery_failures = 0
        self.next_recovery_at = 0.0
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
        try:
            self.recovery_failures = max(0, int(value.get('recovery_failures', 0)))
        except (TypeError, ValueError):
            self.recovery_failures = 0
        try:
            self.next_recovery_at = max(0.0, float(value.get('next_recovery_at', 0)))
        except (TypeError, ValueError):
            self.next_recovery_at = 0.0
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
            'recovery_failures': self.recovery_failures,
            'next_recovery_at': self.next_recovery_at,
            'pending_switch': self.pending_switch,
            'rejected_candidates': self.rejected_candidates,
            'last_health': self.last_health,
            **extra,
        }
        write_json(self.root / 'state/sakura-health.json', value)

    def _reset_health_state(self, current=None):
        self.health_failures = 0
        self.recovery_failures = 0
        self.next_recovery_at = 0.0
        self.pending_switch = None
        self.last_health = {'tag': current} if current else {}
        self._save_health_state()

    def _recovery_due(self):
        return time.time() >= self.next_recovery_at

    def _note_recovery_failure(self):
        self.recovery_failures += 1
        self.next_recovery_at = time.time() + route_policy.backoff_seconds(
            self.recovery_failures, self.policy)
        logging.info('Sakura health: recovery %d failed; next attempt in %d seconds',
                     self.recovery_failures,
                     max(0, int(self.next_recovery_at - time.time())))

    def _note_recovery_success(self):
        self.recovery_failures = 0
        self.next_recovery_at = 0.0

    def _small_quality(self, proxy_port=None):
        '''Measure a 64 KiB body through the local mixed proxy.

        Effective throughput includes proxy/TLS setup, first-byte wait, and the
        transfer, and is the health threshold. Body throughput is retained as a
        diagnostic for distinguishing slow setup from slow transfer. A
        short/incomplete body is a stream interruption and triggers recovery.
        '''
        connection = http.client.HTTPSConnection(
            '127.0.0.1', proxy_port or self.mixed_port, timeout=self.timeout,
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
                'first_byte_ms': int((first_byte - started) * 1000),
                'total_ms': int((ended - started) * 1000),
                'effective_throughput_kbps': round(len(body) / max(.001, ended - started) / 1024, 1),
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
        assessment = route_policy.assess(result, self.policy)
        result['health'] = assessment['health']
        result['score'] = assessment['score']
        result['health_reasons'] = assessment['reasons']
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

    def _member_for_tag(self, data, tag, fallback=None):
        names = data.get('names') or {}
        for region, info in (data.get('region_groups') or {}).items():
            if tag in info.get('nodes', []):
                return {'tag': tag, 'name': names.get(tag, tag),
                        'region': region, 'group': info['group']}
        if isinstance(fallback, dict):
            return {**fallback, 'tag': tag,
                    'name': fallback.get('name') or names.get(tag, tag)}
        return {'tag': tag, 'name': names.get(tag, tag), 'region': '', 'group': ''}

    def _recovery_candidates(self, current, info, data):
        """Use the last full scan's shortlist instead of rescanning a region."""
        selected = []
        try:
            snapshot = json.loads(
                (self.root / 'state/sakura-selection.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            snapshot = {}
        for item in snapshot.get('candidates') or []:
            if not isinstance(item, dict):
                continue
            tag = item.get('tag')
            if tag and tag != current and tag not in selected:
                selected.append(tag)
        for tag in info.get('nodes', []):
            if tag != current and tag not in selected and '倍消耗' not in (
                    (data.get('names') or {}).get(tag, tag)):
                selected.append(tag)
        members = []
        for tag in [current] + selected[:3]:
            item = self._member_for_tag(data, tag)
            if item['group']:
                members.append(item)
        return members

    def _route_view(self, item):
        if not item:
            return None
        assessment = route_policy.assess(item, self.policy)
        return {
            'tag': item.get('tag'),
            'name': item.get('name'),
            'region': item.get('region'),
            'group': item.get('group'),
            'delay': item.get('delay', 0),
            'ok': bool(item.get('ok')),
            'complete': bool(item.get('complete', item.get('ok'))),
            'stability': item.get('stability', 1.0),
            'throughput_kbps': item.get('throughput_kbps', 0),
            'effective_throughput_kbps': measured_throughput_kbps(item),
            'first_byte_ms': item.get('first_byte_ms', 0),
            'chatgpt_ok': item.get('chatgpt_ok', False),
            'chatgpt_status': item.get('chatgpt_status'),
            'chatgpt_ms': item.get('chatgpt_ms', 0),
            'health': assessment['health'],
            'score': assessment['score'],
            'health_reasons': assessment['reasons'],
        }

    def _fallback_route(self, data, tag, region='', group=''):
        if not tag:
            return None
        return {
            'tag': tag,
            'name': (data.get('names') or {}).get(tag, tag),
            'region': region,
            'group': group,
            **self.last_health,
        }

    def _quality_verified(self, item):
        if self.tun_validation and item.get('tun_download_ok') is not None:
            return bool(item.get('tun_download_ok') and item.get('tun_chatgpt_ok')
                        and measured_tun_throughput_kbps(item)
                        >= self.policy.minimum_effective_kbps)
        return bool(item.get('health') == 'healthy' and item.get('chatgpt_ok'))

    def _write_selection(self, regions, active, candidate, ranked, applied,
                         reachable, total, error=None):
        active = active or {}
        assessment = route_policy.assess(active, self.policy)
        snapshot = {
            'ok': error is None,
            'regions': regions,
            'region': active.get('region'),
            'group': active.get('group'),
            'tag': active.get('tag'),
            'name': active.get('name'),
            'delay': active.get('delay', 0),
            'throughput_kbps': active.get('throughput_kbps', 0),
            'effective_throughput_kbps': measured_throughput_kbps(active),
            'first_byte_ms': active.get('first_byte_ms', 0),
            'chatgpt_ok': active.get('chatgpt_ok', False),
            'chatgpt_status': active.get('chatgpt_status'),
            'chatgpt_ms': active.get('chatgpt_ms', 0),
            'tun_verified': bool(active.get('tun_download_ok')),
            'tun_throughput_kbps': active.get('tun_throughput_kbps', 0),
            'tun_effective_throughput_kbps': measured_tun_throughput_kbps(active),
            'tun_first_byte_ms': active.get('tun_first_byte_ms', 0),
            'tun_chatgpt_ok': active.get('tun_chatgpt_ok', False),
            'tun_chatgpt_ms': active.get('tun_chatgpt_ms', 0),
            'quality_verified': self._quality_verified(active),
            'minimum_throughput_kbps': self.minimum_throughput_kbps,
            'minimum_effective_kbps': self.policy.minimum_effective_kbps,
            'score': assessment['score'],
            'health': assessment['health'],
            'health_reasons': assessment['reasons'],
            'applied': bool(applied),
            'candidate': self._route_view(candidate),
            'candidates': [self._route_view(item) for item in ranked[:5]],
            'reachable': reachable,
            'total': total,
            'verified': error is None,
            'probe_isolated': True,
            'probe_samples': (candidate or {}).get('probe_samples', []),
            'interval_seconds': self.interval,
            'health_check_seconds': self.health_interval,
            'health_probe_bytes': HEALTH_BYTES,
            'health_failure_limit': HEALTH_FAILURE_LIMIT,
            'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        if error:
            snapshot['error'] = error
        if active.get('tag'):
            snapshot['active_tag'] = active['tag']
        if candidate and candidate.get('tag') != active.get('tag'):
            snapshot['candidate_tag'] = candidate.get('tag')
        write_json(self.root / 'state/sakura-selection.json', snapshot)
        return snapshot

    def select_once(self, candidates=None, apply=True):
        top = self.request('/proxies/sakura')
        if str(top.get('type', '')).lower() != 'selector':
            return None
        data = self.state()
        if candidates is not None:
            regions = list(self.selection_regions)
            members = []
            seen = set()
            for item in candidates:
                source = item if isinstance(item, dict) else None
                tag = source.get('tag') if source else str(item)
                if not tag or tag in seen:
                    continue
                seen.add(tag)
                member = self._member_for_tag(data, tag, source)
                if member.get('group'):
                    members.append(member)
        elif self.selection_regions:
            regions, members = self._selection_members(data)
        else:
            previous_region, previous_info, _ = self.active_region(top)
            region = previous_region or ''
            info = previous_info or {}
            names = data.get('names') or {}
            regions = [region] if region else []
            members = [{'tag': tag, 'name': names.get(tag, tag), 'region': region,
                        'group': info.get('group')} for tag in info.get('nodes', [])]
        if not members:
            return None

        previous_region, previous_info, _ = self.active_region(top)
        previous_group = str((previous_info or {}).get('group') or '')
        previous = ''
        if previous_group:
            group_state = self.request('/proxies/' + urllib.parse.quote(previous_group))
            previous = str(group_state.get('now') or '')
        if previous and previous not in {item['tag'] for item in members}:
            member = self._member_for_tag(data, previous, {
                'name': (data.get('names') or {}).get(previous, previous),
                'region': previous_region or '',
                'group': previous_group,
            })
            if member.get('group'):
                members.append(member)
        previous_route = self._fallback_route(
            data, previous, previous_region or '', previous_group)

        members = [item for item in members if item['tag'] == previous
                   or self.rejected_candidates.get(item['tag'], 0) <= time.time()]
        if not members:
            return None
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
            self._write_selection(regions, previous_route, None, [], False, 0,
                                  len(members), 'no reachable nodes')
            return None

        results.sort(key=lambda item: (item['delay'], item['tag']))
        try:
            verified = self._measure_candidates(results)
        except (OSError, ValueError, KeyError) as error:
            logging.warning('Isolated node probes unavailable: %s; keeping current route',
                            type(error).__name__)
            return None
        for item in verified:
            item.update(route_policy.assess(item, self.policy))
        chatgpt_ready = [item for item in verified if item.get('chatgpt_ok')]
        ranked = sorted(chatgpt_ready, key=lambda item: (
            item.get('score', 0), measured_throughput_kbps(item),
            item.get('throughput_kbps', 0), -item.get('delay', 0)), reverse=True)
        if not ranked:
            self._write_selection(regions, previous_route, None, [], False,
                                  len(results), len(members),
                                  'no node passed the end-to-end check')
            return None

        current = next((item for item in ranked if item['tag'] == previous), None)
        chosen = ranked[0]
        if not apply:
            active = current or {
                'tag': previous, 'name': previous, 'region': previous_region or '',
                'group': previous_group,
                **self.last_health,
            }
            self._write_selection(regions, active, chosen, ranked, False,
                                  len(results), len(members))
            return self._route_view(chosen)

        if current is not None and chosen['tag'] == current['tag'] and not self.tun_validation:
            logging.info('Sakura selector: keep %s/%s (already best, score %.3f)',
                         current['region'], current['tag'], current.get('score', 0))
            return self._write_selection(regions, current, chosen, ranked, False,
                                          len(results), len(members))
        if (current is not None and chosen['tag'] != current['tag']
                and not route_policy.is_meaningfully_better(chosen, current, self.policy)):
            logging.info('Sakura selector: keep %s/%s; %s not meaningfully better '
                         '(score %.3f vs %.3f)', current['region'], current['tag'],
                         chosen['tag'], chosen.get('score', 0), current.get('score', 0))
            return self._write_selection(regions, current, chosen, ranked, False,
                                          len(results), len(members))

        finalists = [chosen] + [item for item in ranked[:3]
                                if item['tag'] != chosen['tag']]
        if current is not None and current['tag'] not in {item['tag'] for item in finalists}:
            finalists.append(current)
        selected = chosen
        if self.tun_validation:
            selected = None
            for candidate in finalists[:3]:
                try:
                    self._set_group(candidate['group'], candidate['tag'])
                    self._set_top_region(candidate['group'])
                    actual = self.verify_via_tun(candidate['tag'], candidate['group'])
                except (OSError, http.client.HTTPException, ValueError):
                    continue
                if actual.get('tun_download_ok') and actual.get('tun_chatgpt_ok'):
                    selected = {**candidate, **actual}
                    break
            if selected is None:
                if previous_group and previous:
                    try:
                        self._set_group(previous_group, previous)
                        self._set_top_region(previous_group)
                    except (OSError, http.client.HTTPException, ValueError):
                        pass
                self._write_selection(regions, previous_route, chosen, ranked,
                                      False, len(results), len(members),
                                      'no finalist passed the TUN check')
                return None

        self._set_group(selected['group'], selected['tag'])
        self._set_top_region(selected['group'])
        snapshot = self._write_selection(regions, selected, selected, ranked, True,
                                         len(results), len(members))
        self._reset_health_state(selected['tag'])
        logging.info('Sakura selector: %s %s / %s (%s ms, effective %s KB/s, '
                     'body %s KB/s, score %.3f)',
                     selected['region'], selected['tag'], selected['name'],
                     selected['delay'], measured_throughput_kbps(selected),
                     selected.get('throughput_kbps', 0), selected.get('score', 0))
        return snapshot

    def _candidate_quality(self, port):
        result = self._small_quality(proxy_port=port)
        if result.get('ok'):
            result.update(self.verify_chatgpt(proxy_port=port))
        result['ok'] = bool(result.get('complete') and result.get('chatgpt_ok'))
        return result

    def _measure_candidates(self, candidates):
        """Compare nodes without any production selector changes.

        The best three receive three samples each. Median speed reduces the
        influence of a lucky small transfer; downloads remain sequential so
        the benchmark does not make candidates compete for local bandwidth.
        """
        verified = []
        with IsolatedProbePool(self.root, [item['tag'] for item in candidates]) as probes:
            for candidate in candidates:
                quality = self._candidate_quality(probes.ports[candidate['tag']])
                if quality.get('ok'):
                    verified.append({**candidate, **quality})
            verified.sort(key=lambda item: (
                measured_throughput_kbps(item), item.get('throughput_kbps', 0)), reverse=True)
            for candidate in verified[:3]:
                samples = [dict(candidate)] + [self._candidate_quality(probes.ports[candidate['tag']])
                                         for _ in range(2)]
                successful = [item for item in samples if item.get('ok')]
                candidate['ok'] = len(successful) >= 2
                candidate['effective_throughput_kbps'] = statistics.median(
                    [measured_throughput_kbps(item) if item.get('ok') else 0
                     for item in samples])
                candidate['throughput_kbps'] = statistics.median(
                    [float(item.get('throughput_kbps', 0)) if item.get('ok') else 0
                     for item in samples])
                candidate['first_byte_ms'] = statistics.median(
                    [float(item.get('first_byte_ms', 0)) if item.get('ok') else 0
                     for item in samples])
                candidate['probe_samples'] = [
                    {'ok': item.get('ok', False),
                     'effective_throughput_kbps': item.get('effective_throughput_kbps', 0),
                     'throughput_kbps': item.get('throughput_kbps', 0),
                     'first_byte_ms': item.get('first_byte_ms', 0)}
                    for item in samples]
        return [item for item in verified if item.get('ok')]

    def verify_through_proxy(self):
        return self._candidate_quality(self.mixed_port)

    def verify_chatgpt(self, proxy_port=None):
        """Verify the route used by the bundled ChatGPT client.

        `/robots.txt` is a stable small response. The site root may legitimately
        return a challenge or 403 and is therefore unsuitable as a reachability
        test.
        """
        connection = http.client.HTTPSConnection('127.0.0.1', proxy_port or self.mixed_port,
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
        self._set_group(group, tag)
        self._set_top_region(group)
        if os.name == 'nt':
            try:
                subprocess.run(['ipconfig', '/flushdns'], capture_output=True,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                pass
        download = self._https_via_tun(
            'speed.cloudflare.com',
            f'/__down?bytes=131072&selector={time.time_ns()}',
            self.timeout, 131072, expected_bytes=131072)
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
            'tun_effective_throughput_kbps': download.get('effective_throughput_kbps', 0),
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

    def _https_via_tun(self, host, path, timeout, limit, expected_bytes=None):
        connection = http.client.HTTPSConnection(host, timeout=timeout,
                                                 context=ssl.create_default_context())
        started = time.monotonic()
        try:
            connection.request('GET', path, headers={
                'User-Agent': 'UnifiedSelectorTUN/2', 'Accept-Encoding': 'identity',
                'Connection': 'close'})
            response = connection.getresponse()
            first = time.monotonic()
            # HTTPResponse decodes chunked transfer framing and detects
            # truncated Content-Length bodies; raw recv() did neither.
            body = response.read(limit + 1)
            end = time.monotonic()
            complete = (len(body) == expected_bytes if expected_bytes is not None else bool(body))
            return {
                'ok': response.status == 200 and complete,
                'complete': complete,
                'status': response.status,
                'first_byte_ms': int((first - started) * 1000),
                'total_ms': int((end - started) * 1000),
                'bytes': len(body),
                'throughput_kbps': round(len(body) / max(.001, end - first) / 1024, 1),
                'effective_throughput_kbps': round(len(body) / max(.001, end - started) / 1024, 1),
            }
        except (OSError, ValueError, http.client.HTTPException) as error:
            return {'ok': False, 'complete': False, 'error': type(error).__name__,
                    'elapsed_ms': int((time.monotonic() - started) * 1000)}
        finally:
            connection.close()

    @staticmethod
    def _meaningfully_better(candidate, baseline):
        """Compatibility wrapper; new code passes the route policy explicitly."""
        return route_policy.is_meaningfully_better(candidate, baseline,
                                                   route_policy.DEFAULT_POLICY)

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
                        or measured_throughput_kbps(quality)
                        < self.policy.minimum_effective_kbps):
                    if quality.get('health') == 'disconnected':
                        self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
                    continue
                chatgpt = self.verify_chatgpt()
                if not chatgpt.get('ok'):
                    self.rejected_candidates[tag] = time.time() + HEALTH_REJECTION_SECONDS
                    continue
                logging.info('Sakura health: cross-region fallback %s/%s '
                             '(%s ms, effective %s KB/s, body %s KB/s)',
                             region, tag, candidate['delay'],
                             measured_throughput_kbps(quality), quality.get('throughput_kbps', 0))
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
                if not route_policy.is_meaningfully_better(
                        quality, baseline, self.policy):
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
        self._set_group(chosen['group'], chosen['tag'])
        self._set_top_region(chosen['group'])
        baseline_result = {
            key: baseline.get(key) for key in (
                'ok', 'complete', 'chatgpt_ok', 'effective_throughput_kbps',
                'throughput_kbps', 'first_byte_ms', 'stability', 'health')
            if key in baseline
        }
        if 'ok' not in baseline_result:
            baseline_result['ok'] = baseline.get('health') != 'disconnected'
        if 'complete' not in baseline_result:
            baseline_result['complete'] = baseline_result['ok']
        if 'chatgpt_ok' not in baseline_result:
            baseline_result['chatgpt_ok'] = baseline.get('health') != 'blocked'
        self.pending_switch = {
            'previous_region': region,
            'previous_group': info['group'],
            'previous_tag': current,
            'switched_region': chosen['region'],
            'switched_group': chosen['group'],
            'switched_tag': chosen['tag'],
            'cross_region': chosen['region'] != region,
            'baseline_kbps': measured_throughput_kbps(baseline),
            'baseline_health': baseline.get('health', 'disconnected'),
            'baseline': baseline_result,
            'samples': [],
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        self.health_failures = 0
        self._note_recovery_success()
        logging.info(
            'Sakura health: switch %s/%s -> %s/%s '
            '(effective %s KB/s, body %s KB/s); observing two rounds',
            region, current, chosen['region'], chosen['tag'],
            measured_throughput_kbps(chosen), chosen.get('throughput_kbps', 0))
        return {'action': 'switch', **chosen, 'previous_region': region,
                'previous_tag': current}

    def _recover_current(self, region, info, data, current, baseline):
        if self.selection_regions:
            # Targeted recovery: compare the live node with the last scan's
            # shortlist only. A failed node no longer triggers a full US/SG
            # rescan, and candidate probes never change the production route.
            members = self._recovery_candidates(current, info, data)
            selected = self.select_once(candidates=members, apply=False)
            if selected:
                baseline_health = baseline.get('health', 'disconnected')
                eligible = (baseline_health in ('blocked', 'disconnected')
                            and bool(selected.get('ok') and selected.get('chatgpt_ok')))
                if not eligible and selected.get('tag') != current:
                    eligible = route_policy.is_meaningfully_better(
                        selected, baseline, self.policy)
                if selected.get('tag') == current:
                    self._note_recovery_success()
                    return {'action': 'keep', **selected}
                if eligible:
                    # Keep two-round observation before accepting recovery.
                    return self._begin_pending_switch(
                        region, info, current, selected, baseline)
                try:
                    self._set_top_region(info['group'])
                    self._set_group(info['group'], current)
                except (OSError, http.client.HTTPException, ValueError):
                    pass
                self._note_recovery_failure()
                return {'action': 'keep', 'region': region, 'tag': current,
                        'reason': 'candidate did not meaningfully improve a working route'}
            try:
                self._set_top_region(info['group'])
                self._set_group(info['group'], current)
            except (OSError, http.client.HTTPException, ValueError):
                pass
            self._note_recovery_failure()
            return {'action': 'keep', 'region': region, 'tag': current,
                    'reason': 'no targeted candidate passed'}
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
                self._note_recovery_failure()
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
            'effective_throughput_kbps': self.last_health.get('effective_throughput_kbps', 0),
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
            samples = decision.get('samples') or []
            measured = [measured_throughput_kbps(item) if isinstance(item, dict)
                        else float(item) for item in samples]
            logging.info('Sakura health: kept %s/%s after two rounds (%s KB/s)',
                         switched_region, switched,
                         statistics.median(measured) if measured else 0)
        self.pending_switch = None
        self.health_failures = 0
        restore = decision.get('restore', False)
        if restore:
            self._note_recovery_failure()
        else:
            self._note_recovery_success()
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
        samples.append(dict(result))
        if result.get('health') == 'disconnected':
            decision.update(restore=True)
            return self._finish_pending_switch(decision, result, 'switched node disconnected')
        if len(samples) < HEALTH_FAILURE_LIMIT:
            self.pending_switch = decision
            self._save_health_state()
            return {'action': 'observe', 'tag': current, 'samples': samples}
        baseline = decision.get('baseline')
        if not isinstance(baseline, dict):
            baseline_health = decision.get('baseline_health', 'disconnected')
            baseline = {
                'ok': baseline_health != 'disconnected',
                'complete': baseline_health != 'disconnected',
                'chatgpt_ok': baseline_health != 'blocked',
                'effective_throughput_kbps': decision.get('baseline_kbps', 0),
                'throughput_kbps': decision.get('baseline_kbps', 0),
                'health': baseline_health,
            }
        accepted, reason = route_policy.pending_decision(
            baseline, samples, result, self.policy)
        decision.update(restore=not accepted)
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
            if self._recovery_due():
                outcome = self._recover_current(region, info, data, current, result)
            else:
                outcome = {'action': 'backoff', 'tag': current,
                           'next_recovery_at': self.next_recovery_at}
        elif result['health'] in ('low', 'blocked'):
            self.health_failures += 1
            logging.info('Sakura health: %s %s (effective %s KB/s, body %s KB/s, ChatGPT=%s), %d/%d',
                         current, result['health'], measured_throughput_kbps(result),
                         result.get('throughput_kbps', 0), result.get('chatgpt_ok', False),
                         self.health_failures, HEALTH_FAILURE_LIMIT)
            if self.health_failures >= HEALTH_FAILURE_LIMIT:
                if self._recovery_due():
                    outcome = self._recover_current(region, info, data, current, result)
                else:
                    outcome = {'action': 'backoff', 'tag': current,
                               'next_recovery_at': self.next_recovery_at}
        else:
            self.health_failures = 0
            self._note_recovery_success()
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
