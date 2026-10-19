"""Keep the active Sakura region on its lowest-latency reachable node.

The top-level `sakura` selector points at one region group. This helper reads
that group from the Clash API, measures only the nodes in the active region,
and switches the region group to the fastest node that also passes a real
HTTPS request through the local proxy.
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


def pick_lowest(results):
    valid = [item for item in results if item.get('delay') and item['delay'] > 0]
    return min(valid, key=lambda item: (item['delay'], item['tag'])) if valid else None


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
        self.minimum_throughput_kbps = max(0, int(settings.get('SakuraMinimumThroughputKBps', 20)))
        self.tun_validation = bool(settings.get('SakuraTunValidation', True))
        self.stop = threading.Event()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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

    def select_once(self):
        top = self.request('/proxies/sakura')
        if str(top.get('type', '')).lower() != 'selector':
            return None
        region, info, data = self.active_region(top)
        if not region or not info:
            return None
        names = data.get('names') or {}
        members = [{'tag': tag, 'name': names.get(tag, tag)} for tag in info.get('nodes', [])]
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
            write_json(self.root / 'state/sakura-selection.json', {
                'ok': False,
                'region': region,
                'group': info['group'],
                'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'error': 'no reachable nodes',
            })
            return None
        group = self.request('/proxies/' + urllib.parse.quote(info['group']))
        previous = str(group.get('now') or '')
        results.sort(key=lambda item: (item['delay'], item['tag']))
        verified = []
        for candidate in results:
            self.request('/proxies/' + urllib.parse.quote(info['group']), 'PUT', {'name': candidate['tag']})
            quality = self.verify_through_proxy()
            if quality.get('ok'):
                verified.append({**candidate, **quality})
        chatgpt_ready = [item for item in verified if item.get('chatgpt_ok')]
        pool = chatgpt_ready or verified
        ranked = sorted(pool, key=lambda item: (item.get('throughput_kbps', 0),
                                                -item.get('delay', 0)), reverse=True)
        names_by_tag = {item['tag']: item for item in members}
        finalists = []
        if previous in names_by_tag:
            current = next((item for item in results if item['tag'] == previous), None)
            finalists.append(current or {**names_by_tag[previous], 'delay': 0})
        for candidate in ranked:
            if candidate['tag'] not in {item['tag'] for item in finalists}:
                finalists.append(candidate)
            if len(finalists) >= 3:
                break
        if self.tun_validation and finalists:
            tun_results = []
            for candidate in finalists:
                actual = self.verify_via_tun(candidate['tag'], info['group'])
                if actual.get('tun_download_ok'):
                    tun_results.append({**candidate, **actual})
            if tun_results:
                prefer_chatgpt = [item for item in tun_results if item.get('tun_chatgpt_ok')]
                tun_pool = prefer_chatgpt or tun_results
                chosen = max(tun_pool, key=lambda item: (item.get('tun_throughput_kbps', 0),
                                                         -item.get('delay', 0)))
                self.request('/proxies/' + urllib.parse.quote(info['group']), 'PUT', {'name': chosen['tag']})
            else:
                chosen = ranked[0] if ranked else None
        else:
            chosen = ranked[0] if ranked else None
        if not chosen:
            if previous:
                try:
                    self.request('/proxies/' + urllib.parse.quote(info['group']), 'PUT', {'name': previous})
                except Exception:
                    pass
            write_json(self.root / 'state/sakura-selection.json', {
                'ok': False,
                'region': region,
                'group': info['group'],
                'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'error': 'no node passed the end-to-end check',
                'reachable': len(results),
                'total': len(members),
            })
            return None
        snapshot = {
            'ok': True,
            'region': region,
            'group': info['group'],
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
            'tested_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        write_json(self.root / 'state/sakura-selection.json', snapshot)
        logging.info('Sakura selector: %s %s / %s (%s ms, %s KB/s)', region, chosen['tag'], chosen['name'], chosen['delay'], chosen.get('throughput_kbps', 0))
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
        self.request('/proxies/' + urllib.parse.quote(group), 'PUT', {'name': tag})
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
                if last_full is None or now - last_full >= self.interval:
                    self.select_once()
                    last_full = time.monotonic()
                    last_health = last_full
                elif self.health_interval and now - last_health >= self.health_interval:
                    last_health = now
                    if self.check_current() is None:
                        logging.info('Sakura current node failed; re-selecting')
                        self.select_once()
                        last_full = time.monotonic()
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