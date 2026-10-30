"""Keep official Codex on its own measured US lane.

The global ``sakura`` selector follows the US/Singapore pool used by browser
traffic. Official Codex needs a stable US egress, so config generation creates
a second selector (``codex-official``) and a dedicated loopback inbound. This
module owns only that selector; ``latency_selector`` never changes it.

Probes are isolated: they use separate sing-box inbounds and never modify the
production selector while measuring candidates. The live selector changes only
after a candidate wins the score comparison.
"""
import argparse
import concurrent.futures
import http.client
import json
import logging
import os
from pathlib import Path
import ssl
import threading
import time
import urllib.parse
import urllib.request

import route_policy
from isolated_probes import IsolatedProbePool

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTOR = 'codex-official'
DEFAULT_GROUP = 'sakura-region-us'
DEFAULT_PORT = 12551
PROBE_DOWNLOAD = ('speed.cloudflare.com', '/__down?bytes=65536', 65536)
PROBE_CHATGPT = ('chatgpt.com', '/robots.txt', 8192)
PROBE_CODEX_PATH = '/backend-api/codex/responses'
# A request this large is limited by the user's context, not by the node's
# health. Do not churn the selector when such an upload misses its budget.
LARGE_OFFICIAL_UPLOAD_BYTES = 8 * 1024 * 1024
FAILED_NODE_COOLDOWN_SECONDS = 300


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    os.replace(temporary, path)


def read_core_pid(root):
    try:
        value = json.loads((Path(root) / 'state/runtime.json').read_text(encoding='utf-8'))
        return int(value.get('CorePid') or 0)
    except (OSError, ValueError, TypeError):
        return 0


def load_route_state(root, max_age=None, core_pid=None):
    """Read the official-lane snapshot and reject stale observations.

    A state file written by the primary backend is useful to the standby
    backend too. It becomes invalid when the core restarts because the new
    selector starts from its configured default.
    """
    path = Path(root) / 'state/codex-official-route.json'
    try:
        raw = path.read_text(encoding='utf-8')
        value = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if max_age is not None:
        try:
            observed = float(value.get('observed_epoch') or 0)
        except (TypeError, ValueError):
            observed = 0.0
        if observed <= 0 or time.time() - observed > max(1.0, float(max_age)):
            return {}
    if core_pid:
        try:
            if int(value.get('core_pid') or 0) != int(core_pid):
                return {}
        except (TypeError, ValueError):
            return {}
    return value


def upload_budget_seconds(nbytes, settings):
    """Total upload budget from size and a minimum sustained rate."""
    try:
        minimum = max(1.0, float(settings.get('CodexOfficialUploadMinimumKBps', 8)))
    except (TypeError, ValueError):
        minimum = 8.0
    try:
        low = max(5.0, float(settings.get('CodexOfficialUploadMinSeconds', 30)))
    except (TypeError, ValueError):
        low = 30.0
    try:
        high = max(low, float(settings.get('CodexOfficialUploadMaxSeconds', 180)))
    except (TypeError, ValueError):
        high = 180.0
    required = max(0.0, float(nbytes)) / (minimum * 1024)
    return max(low, min(high, required))


def _proxy_request(proxy_port, host, method, path, body=None, headers=None,
                   read_limit=65536, timeout=12):
    connection = http.client.HTTPSConnection(
        '127.0.0.1', proxy_port, timeout=timeout,
        context=ssl.create_default_context())
    started = time.monotonic()
    try:
        connection.set_tunnel(host, 443)
        connection.request(method, path, body=body, headers=headers or {
            'User-Agent': 'UnifiedOfficial/1', 'Accept-Encoding': 'identity',
            'Connection': 'close'})
        uploaded = time.monotonic()
        response = connection.getresponse()
        first = time.monotonic()
        payload = response.read(read_limit) if method != 'HEAD' else b''
        ended = time.monotonic()
        return {
            'ok': True,
            'status': response.status,
            'bytes': len(payload),
            'upload_seconds': round(uploaded - started, 3),
            'first_byte_ms': int((first - started) * 1000),
            'total_ms': int((ended - started) * 1000),
            'effective_throughput_kbps': round(
                len(payload) / max(.001, ended - started) / 1024, 1),
            'throughput_kbps': round(
                len(payload) / max(.001, ended - first) / 1024, 1),
            'content_encoding': response.getheader('Content-Encoding'),
        }
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as error:
        return {'ok': False, 'error': type(error).__name__,
                'elapsed_ms': int((time.monotonic() - started) * 1000)}
    finally:
        connection.close()


def probe_candidate(proxy_port, timeout=12):
    """Probe download speed, ChatGPT reachability and the real Codex path."""
    download = _proxy_request(proxy_port, PROBE_DOWNLOAD[0], 'GET',
                              PROBE_DOWNLOAD[1], read_limit=PROBE_DOWNLOAD[2],
                              timeout=timeout)
    chatgpt = _proxy_request(proxy_port, PROBE_CHATGPT[0], 'GET',
                             PROBE_CHATGPT[1], read_limit=PROBE_CHATGPT[2],
                             timeout=min(timeout, 8))
    post_body = json.dumps({'model': 'gpt-6.1-sol', 'input': []}).encode('utf-8')
    codex = _proxy_request(
        proxy_port, 'chatgpt.com', 'POST', PROBE_CODEX_PATH, body=post_body,
        headers={'User-Agent': 'UnifiedOfficial/1', 'Content-Type': 'application/json',
                 'Content-Length': str(len(post_body)), 'Accept': 'application/json',
                 'Connection': 'close'},
        read_limit=8192, timeout=min(timeout, 8))
    complete = bool(download.get('ok') and download.get('bytes') == PROBE_DOWNLOAD[2])
    chatgpt_ok = bool(chatgpt.get('ok') and chatgpt.get('status') == 200)
    # Unauthenticated official inference is expected to return 401/403; a 5xx
    # or a connection failure means the request path itself is unavailable.
    post_ok = bool(codex.get('ok') and int(codex.get('status') or 0) < 500)
    return {
        'ok': complete and chatgpt_ok and post_ok,
        'complete': complete,
        'effective_throughput_kbps': download.get('effective_throughput_kbps', 0),
        'throughput_kbps': download.get('throughput_kbps', 0),
        'first_byte_ms': download.get('first_byte_ms', 0),
        'chatgpt_ok': chatgpt_ok,
        'chatgpt_status': chatgpt.get('status'),
        'chatgpt_ms': chatgpt.get('total_ms', 0),
        'codex_path_ok': post_ok,
        'codex_path_status': codex.get('status'),
        'codex_path_ms': codex.get('total_ms', 0),
        'upload_seconds': codex.get('upload_seconds'),
        'error': download.get('error') or chatgpt.get('error') or codex.get('error'),
    }


class OfficialRouteManager:
    def __init__(self, root=ROOT, settings=None, probe=None, request=None,
                 state_path=None):
        self.root = Path(root)
        from settings_config import load_settings
        self.settings = settings or load_settings(self.root)
        self.selector = str(self.settings.get('CodexOfficialSelector') or DEFAULT_SELECTOR)
        self.group = str(self.settings.get('CodexOfficialGroup') or DEFAULT_GROUP)
        self.port = int(self.settings.get('CodexOfficialPort') or DEFAULT_PORT)
        self.enabled = bool(self.settings.get('CodexOfficialEnabled', True))
        self.policy = route_policy.policy_from_settings(self.settings)
        self.scan_interval = max(300, int(float(
            self.settings.get('CodexOfficialScanMinutes', 30)) * 60))
        self.health_interval = max(60, int(float(
            self.settings.get('CodexOfficialHealthMinutes', 3)) * 60))
        self.sticky_seconds = max(0, int(float(
            self.settings.get('SakuraStickySeconds', 600))))
        self.state_path = Path(state_path or self.root / 'state/codex-official-route.json')
        self._probe = probe or probe_candidate
        self._request = request or self._controller_request
        self._stop = threading.Event()
        self._scan_now = threading.Event()
        self._lock = threading.RLock()
        self._feedback = {}
        self._consecutive_failures = 0
        self._low_streak = 0
        self._failed_nodes = {}
        self._ranked = []
        self._active = None
        self._last_switch = 0.0
        self._last_scan = 0.0
        self._last_health = 0.0
        self._next_scan = 0.0
        self._degraded = False
        self._last_request = {}
        self._state = {}
        self._last_state_write = 0.0

    def _controller_request(self, path, method='GET', body=None):
        secret = (self.root / 'state/controller-secret.txt').read_text().strip()
        headers = {'Authorization': 'Bearer ' + secret}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(
            f'http://127.0.0.1:{int(self.settings["ControllerPort"])}{path}',
            data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=6) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def selector_state(self):
        try:
            return self._request('/proxies/' + urllib.parse.quote(self.selector))
        except (OSError, ValueError, http.client.HTTPException):
            return None

    def _members(self):
        state = self.selector_state()
        if not isinstance(state, dict) or str(state.get('type', '')).lower() != 'selector':
            return None, []
        try:
            catalog = json.loads(
                (self.root / 'state/sakura-outbounds.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            catalog = {}
        names = catalog.get('names') or {}
        return state, [
            {'tag': tag, 'name': names.get(tag, tag), 'region': self.group,
             'group': self.selector}
            for tag in state.get('all') or [] if isinstance(tag, str)
        ]

    def probe_members(self, members):
        """Probe all candidates through isolated probe inbounds."""
        if not members:
            return []
        tags = [item['tag'] for item in members]
        timeout = float(self.settings.get('SakuraProbeTimeoutSeconds', 12))
        with IsolatedProbePool(self.root, tags) as probes:
            def measure(item):
                try:
                    return self._probe(probes.ports[item['tag']], timeout)
                except (OSError, ValueError) as error:
                    return {'ok': False, 'error': type(error).__name__}

            # A small parallel batch bounds startup latency while still
            # serializing the probes into a single isolated core.
            results = []
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(3, len(members))) as executor:
                measured = list(executor.map(measure, members))
            for item, result in zip(members, measured):
                if result.get('ok'):
                    results.append({**item, **result})
            return results

    @staticmethod
    def _route(item):
        return {
            'tag': item.get('tag'),
            'name': item.get('name'),
            'region': item.get('region'),
            'score': item.get('score'),
            'health': item.get('health'),
            'effective_throughput_kbps': item.get('effective_throughput_kbps', 0),
            'throughput_kbps': item.get('throughput_kbps', 0),
            'first_byte_ms': item.get('first_byte_ms', 0),
            'chatgpt_ok': item.get('chatgpt_ok', False),
            'codex_path_ok': item.get('codex_path_ok', False),
            'codex_path_status': item.get('codex_path_status'),
            'upload_seconds': item.get('upload_seconds'),
        }

    @staticmethod
    def _rank_key(item):
        return (item.get('score', 0),
                item.get('effective_throughput_kbps', 0),
                -item.get('first_byte_ms', 0))

    def _ranked_with(self, active=None):
        """Keep the last scan's alternatives instead of collapsing to one node."""
        ranked = [item for item in self._ranked if isinstance(item, dict)]
        if isinstance(active, dict) and active.get('tag'):
            ranked = [item for item in ranked
                      if item.get('tag') != active.get('tag')]
            ranked.append(active)
        ranked.sort(key=self._rank_key, reverse=True)
        self._ranked = ranked[:5]
        return self._ranked

    def _recently_failed(self, tag):
        if not tag:
            return False
        until = self._failed_nodes.get(tag, 0.0)
        if until <= time.monotonic():
            self._failed_nodes.pop(tag, None)
            return False
        return True

    def _write(self, ready, active=None, candidate=None, ranked=None, error=None,
               health=None):
        now = time.time()
        active = active or {}
        with self._lock:
            self._state = {
                'enabled': self.enabled,
                'ready': bool(ready and self.enabled and not self._degraded),
                'degraded': bool(self._degraded),
                'selector': self.selector,
                'group': self.group,
                'port': self.port,
                'active_tag': active.get('tag'),
                'active_name': active.get('name'),
                'active': self._route(active) if active else None,
                'candidate': self._route(candidate) if candidate else None,
                'candidates': [self._route(item) for item in (ranked or [])[:5]],
                'score': active.get('score'),
                'health': active.get('health'),
                'last_probe_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'observed_epoch': now,
                'last_health': health,
                'last_request': self._last_request,
                'failures': self._consecutive_failures,
                'next_scan_epoch': self._next_scan,
                'core_pid': read_core_pid(self.root),
                'error': error,
            }
            value = dict(self._state)
            self._last_state_write = time.monotonic()
        write_json(self.state_path, value)
        return value

    def _note_failure(self, reason):
        self._consecutive_failures += 1
        self._degraded = True
        self._last_request = {**self._last_request, 'degraded_reason': reason}
        self._scan_now.set()

    def record_request(self, upload_seconds=None, headers_seconds=None,
                       failure_stage=None, upload_timed_out=False,
                       stream_error=None, tag=None, lane=None,
                       request_bytes=None, connect_attempts=0):
        """Close the loop with real official inference requests.

        Feedback from fallback lanes must not mark the dedicated selector
        healthy or unhealthy. An oversized request that misses its upload
        budget is a client-context problem, not evidence that the node is bad.
        """
        if not self.enabled:
            return
        lane_is_official = not lane or lane == self.selector
        effective_tag = tag or ((self._active or {}).get('tag')
                                if lane_is_official else None)
        large_upload = bool(request_bytes and request_bytes >= LARGE_OFFICIAL_UPLOAD_BYTES)
        connect_failed = (failure_stage == 'connect' and connect_attempts >= 4
                          and lane_is_official)
        with self._lock:
            was_degraded = self._degraded
            self._last_request = {
                'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'lane': lane,
                'tag': effective_tag,
                'upload_seconds': upload_seconds,
                'headers_seconds': headers_seconds,
                'failure_stage': failure_stage,
                'upload_timed_out': bool(upload_timed_out),
                'stream_error': stream_error,
                'request_bytes': request_bytes,
                'large_upload': large_upload,
            }
            if connect_failed:
                # All candidate lanes failed before sending any inference work.
                # Ask for a fresh comparison, but do not blame the fallback lane.
                if effective_tag:
                    self._failed_nodes[effective_tag] = (
                        time.monotonic() + FAILED_NODE_COOLDOWN_SECONDS)
                self._consecutive_failures += 1
                if self._consecutive_failures >= 2:
                    self._degraded = True
                self._scan_now.set()
                self._last_request['degraded_reason'] = 'all official connect attempts failed'
            elif lane and lane != self.selector:
                # A fallback lane says nothing about the dedicated selector.
                # A failed connect still asks for a fresh comparison, but it
                # must not blame the official node or degrade the lane.
                if failure_stage == 'connect':
                    self._scan_now.set()
                return
            elif large_upload and (upload_timed_out or failure_stage == 'send'):
                # The body is too large for the available link. Keep serving
                # normal requests on this node and surface the limitation.
                self._last_request['oversized_upload'] = True
            elif upload_timed_out or failure_stage == 'send':
                self._note_failure('official request upload failed')
            elif failure_stage in ('headers', 'response_body'):
                self._consecutive_failures += 1
                if self._consecutive_failures >= 2:
                    self._note_failure('repeated official request failures')
            elif failure_stage is None and not stream_error:
                self._consecutive_failures = 0
                self._low_streak = 0
                self._degraded = False
                if effective_tag:
                    self._failed_nodes.pop(effective_tag, None)
        if self._degraded:
            self._write(False, self._active, None,
                        self._ranked_with(self._active))
        elif (was_degraded or connect_failed or large_upload
              or time.monotonic() - self._last_state_write >= 5):
            self._write(bool(self._active), self._active, None,
                        self._ranked_with(self._active))

    def _select(self):
        state, members = self._members()
        if state is None:
            self._write(False, None, error='codex-official selector unavailable')
            return None
        current_tag = str(state.get('now') or '')
        results = self.probe_members(members)
        for item in results:
            item.update(route_policy.assess(item, self.policy))
        ranked = sorted(
            [item for item in results if item.get('chatgpt_ok')],
            key=self._rank_key, reverse=True)
        if not ranked:
            self._write(False, None, error='no official candidate passed',
                        ranked=results)
            return None
        # A node that just reset a live connection should not be selected again
        # from the same scan. Ignore the cooldown if it is the only candidate.
        preferred = [item for item in ranked
                     if not self._recently_failed(item.get('tag'))]
        if preferred:
            ranked = preferred
        self._ranked = ranked[:5]
        current = next((item for item in ranked if item['tag'] == current_tag), None)
        chosen = ranked[0]
        if (current is not None and chosen['tag'] != current['tag']
                and time.time() - self._last_switch < self.sticky_seconds
                and not route_policy.is_meaningfully_better(
                    chosen, current, self.policy)):
            chosen = current
        if current is not None and chosen['tag'] == current['tag']:
            chosen = current
        elif current is not None and not route_policy.is_meaningfully_better(
                chosen, current, self.policy):
            chosen = current
        try:
            if chosen['tag'] != current_tag:
                self._request('/proxies/' + urllib.parse.quote(self.selector),
                              'PUT', {'name': chosen['tag']})
        except (OSError, ValueError, http.client.HTTPException) as error:
            self._write(False, current, chosen, ranked,
                        error=f'selector update failed: {type(error).__name__}')
            return None
        if chosen['tag'] != current_tag:
            self._last_switch = time.time()
        self._active = chosen
        self._consecutive_failures = 0
        self._low_streak = 0
        self._degraded = False
        self._last_scan = time.monotonic()
        self._next_scan = time.time() + self.scan_interval
        self._write(True, chosen, chosen, ranked)
        logging.info('Official Codex lane: %s (%s KB/s, score %.3f)',
                     chosen['tag'], chosen.get('effective_throughput_kbps', 0),
                     chosen.get('score', 0))
        return chosen

    def select_once(self):
        try:
            return self._select()
        except (OSError, ValueError, KeyError, http.client.HTTPException) as error:
            logging.warning('Official Codex lane unavailable: %s', type(error).__name__)
            self._write(False, self._active, error=type(error).__name__)
            return None

    def failover_after_connect_failure(self, failed_tag=None):
        """Rotate to the next measured candidate before the next retry.

        This is deliberately synchronous and uses the last scan's ranking, not
        a new network probe: the client is waiting, and a known alternative is
        almost always better than reusing a node that just reset CONNECT/TLS.
        """
        if not self.enabled:
            return None
        with self._lock:
            if failed_tag:
                self._failed_nodes[failed_tag] = (
                    time.monotonic() + FAILED_NODE_COOLDOWN_SECONDS)
            current_tag = failed_tag or (self._active or {}).get('tag')
            candidates = []
            source = self._ranked or (self._state.get('candidates') or [])
            for item in source:
                if not isinstance(item, dict):
                    continue
                tag = item.get('tag')
                if not tag or tag == current_tag or tag == failed_tag:
                    continue
                if item.get('health') in ('blocked', 'disconnected'):
                    continue
                if item.get('chatgpt_ok') is False:
                    continue
                candidates.append(item)
            candidates.sort(key=self._rank_key, reverse=True)
            if not candidates:
                self._scan_now.set()
                return None
            chosen = candidates[0]
            try:
                self._request('/proxies/' + urllib.parse.quote(self.selector),
                              'PUT', {'name': chosen['tag']})
            except (OSError, ValueError, http.client.HTTPException):
                self._scan_now.set()
                return None
            self._last_switch = time.time()
            self._active = chosen
            self._degraded = False
            self._consecutive_failures = 0
            self._low_streak = 0
            self._ranked = candidates[:5]
            self._write(True, chosen, chosen, self._ranked)
            logging.warning(
                'Official Codex lane: fast failover %s -> %s after connect failure',
                failed_tag or current_tag, chosen['tag'])
            return chosen

    def health_check(self):
        state, members = self._members()
        if state is None:
            self._write(False, None, error='codex-official selector unavailable')
            self._scan_now.set()
            return False
        current_tag = str(state.get('now') or '')
        current = next((item for item in members if item['tag'] == current_tag), None)
        if current is None:
            self._scan_now.set()
            return False
        result = self.probe_members([current])
        if result:
            checked = result[0]
            checked.update(route_policy.assess(checked, self.policy))
            health = checked.get('health')
            if health in ('blocked', 'disconnected'):
                self._consecutive_failures += 1
                if self._consecutive_failures >= 2:
                    self._degraded = True
                    self._scan_now.set()
            elif health == 'low':
                # A low probe score under concurrent real traffic is not a
                # reason to take the lane out of service. Compare candidates
                # on the next scan without interrupting working requests.
                self._consecutive_failures = 0
                self._low_streak += 1
                if self._low_streak >= 2:
                    self._low_streak = 0
                    self._scan_now.set()
            else:
                self._consecutive_failures = 0
                self._low_streak = 0
            self._active = checked
            ranked = self._ranked_with(checked)
            self._write(not self._degraded, checked, checked, ranked,
                        health=health)
            return health == 'healthy'
        self._consecutive_failures += 1
        if self._consecutive_failures >= 2:
            self._degraded = True
            self._scan_now.set()
        self._write(not self._degraded, self._active, None,
                    self._ranked_with(), error='official health probe failed')
        return False

    def run(self):
        if not self.enabled:
            self._write(False, None, error='disabled by settings')
            return
        # Let the ordinary route monitor take the shared probe lock first at
        # startup; the official lane can initialize a few seconds later.
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if self._scan_now.is_set():
                    self._scan_now.clear()
                    selected = self.select_once()
                    # The controller and selector can still be starting during
                    # a service restart. Retry soon instead of waiting a full
                    # scan interval before the lane becomes usable.
                    self._last_scan = (time.monotonic() if selected
                                       else time.monotonic() - self.scan_interval + 30)
                    self._last_health = time.monotonic()
                elif not self._last_scan or now - self._last_scan >= self.scan_interval:
                    selected = self.select_once()
                    self._last_scan = (time.monotonic() if selected
                                       else time.monotonic() - self.scan_interval + 30)
                    self._last_health = time.monotonic()
                elif now - self._last_health >= self.health_interval:
                    self.health_check()
                    self._last_health = time.monotonic()
            except Exception as error:
                logging.warning('Official Codex lane monitor error: %s', error)
            self._stop.wait(15)

    def stop(self):
        self._stop.set()

    def snapshot(self):
        return load_route_state(self.root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    from settings_config import load_settings
    manager = OfficialRouteManager(ROOT, load_settings(ROOT))
    if args.once:
        result = manager.select_once()
        print(json.dumps(result or {}, ensure_ascii=True))
        return 0 if result else 1
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    manager.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
