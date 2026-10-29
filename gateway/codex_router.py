"""Loopback Codex model router: ChatGPT credentials stay on the OpenAI route.

Only /codex is handled. The existing AI/HTTP/SOCKS entry remains independent.
Logs contain routing metadata only, never headers, prompts, or credentials.
"""
import argparse
from compression import zstd
import gzip
import hmac
import http.client
import http.server
import json
from pathlib import Path
import socket
import sqlite3
import threading
import time
import urllib.parse
from tool_search_compat import ToolSearchCompat
from stream_metrics import StreamMetrics
from official_route import load_route_state, read_core_pid
from codex_catalog import GENERATED_CATALOG, local_catalog

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '/codex'
OFFICIAL_HOST = 'chatgpt.com'
OFFICIAL_BASE = '/backend-api/codex'
API_MODELS = {model['slug'] for model in local_catalog()['models']}
MAX_BODY_MIB = 128
MAX_BODY = MAX_BODY_MIB * 1024 * 1024
HOP_HEADERS = {'host', 'connection', 'transfer-encoding', 'content-length',
               'keep-alive', 'proxy-connection', 'proxy-authorization', 'upgrade',
               'x-unified-codex-key'}


class RequestBodyTooLarge(ValueError):
    pass


def official_reasoning_item(item):
    """Return whether an opaque reasoning item has the official wire shape."""
    if not isinstance(item, dict) or item.get('type') != 'reasoning':
        return False
    encrypted = item.get('encrypted_content')
    item_id = item.get('id')
    return (isinstance(encrypted, str) and len(encrypted) >= 128
            and encrypted.startswith('gAAAA')
            and isinstance(item_id, str) and item_id.startswith('rs_'))


_CREDENTIAL_CACHE = {'value': None, 'expires': 0.0}
_CREDENTIAL_LOCK = threading.Lock()


def api_credential():
    """Reuse the existing localhost API credential without copying it into config."""
    now = time.monotonic()
    with _CREDENTIAL_LOCK:
        if _CREDENTIAL_CACHE['value'] and now < _CREDENTIAL_CACHE['expires']:
            return _CREDENTIAL_CACHE['value']
    path = Path.home() / '.cc-switch/cc-switch.db'
    try:
        with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=3) as db:
            for raw, in db.execute("SELECT settings_config FROM providers WHERE app_type='claude'"):
                env = json.loads(raw).get('env', {})
                base = urllib.parse.urlsplit(str(env.get('ANTHROPIC_BASE_URL', '')))
                if base.hostname in ('127.0.0.1', 'localhost') and base.port == 8799:
                    key = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')
                    if key:
                        with _CREDENTIAL_LOCK:
                            _CREDENTIAL_CACHE.update(value=key, expires=now + 300)
                        return key
    except (OSError, sqlite3.Error, ValueError):
        with _CREDENTIAL_LOCK:
            _CREDENTIAL_CACHE.update(value=None, expires=0.0)
        raise
    with _CREDENTIAL_LOCK:
        _CREDENTIAL_CACHE.update(value=None, expires=0.0)
    raise RuntimeError('Existing localhost API credential unavailable')


def read_body(handler, decode=True):
    if handler.headers.get('Transfer-Encoding', '').lower() == 'chunked':
        chunks, total = [], 0
        while True:
            line = handler.rfile.readline(128)
            if not line.endswith(b'\r\n'):
                raise ValueError('Invalid HTTP chunk framing')
            size = int(line.split(b';', 1)[0], 16)
            if not size:
                while handler.rfile.readline(8192) not in (b'\r\n', b''):
                    pass
                break
            total += size
            if total > MAX_BODY:
                raise RequestBodyTooLarge('Request body exceeds limit')
            chunk = handler.rfile.read(size)
            if len(chunk) != size or handler.rfile.read(2) != b'\r\n':
                raise ValueError('Incomplete HTTP chunk')
            chunks.append(chunk)
        body = b''.join(chunks)
    else:
        size = int(handler.headers.get('Content-Length', 0))
        if size < 0 or size > MAX_BODY:
            raise RequestBodyTooLarge('Request body exceeds limit')
        body = handler.rfile.read(size)
        if len(body) != size:
            raise ValueError('Incomplete request body')
    if not decode:
        return body
    encoding = handler.headers.get('Content-Encoding', '').lower()
    body, _ = decode_body(body, encoding)
    if len(body) > MAX_BODY:
        raise RequestBodyTooLarge('Decoded request body exceeds limit')
    return body


def decode_body(body, encoding=''):
    """Decode a wire body while retaining the original bytes at the caller."""
    encoding = (encoding or '').lower()
    if encoding == 'zstd':
        return zstd.decompress(body), encoding
    if encoding == 'gzip':
        return gzip.decompress(body), encoding
    if encoding in ('', 'identity'):
        return body, ''
    raise ValueError('Unsupported request content encoding')


def encode_body(body, encoding, level=3):
    encoding = (encoding or '').lower()
    if encoding == 'zstd':
        try:
            return zstd.compress(body, level=level)
        except TypeError:
            return zstd.compress(body)
    if encoding == 'gzip':
        return gzip.compress(body, compresslevel=max(1, min(9, int(level))))
    return body


class UploadTimeout(TimeoutError):
    pass


def upload_limit_seconds(nbytes, settings, official=True):
    if not official:
        try:
            return max(30.0, float(settings.get('AiUpstreamUploadSeconds', 180)))
        except (TypeError, ValueError):
            return 180.0
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
    return max(low, min(high, max(0.0, float(nbytes)) / (minimum * 1024)))


def _socket_for(connection, response=None):
    holders = [connection]
    stream = getattr(response, 'fp', None)
    holders.append(stream)
    holders.append(getattr(stream, 'raw', None))
    for holder in holders:
        if holder is None:
            continue
        sock = getattr(holder, 'sock', None) or getattr(holder, '_sock', None)
        if sock is not None:
            return sock
    return None


def send_with_budget(connection, method, target, body, headers, limit_seconds,
                     stall_seconds=None):
    """Send a request body with a hard total budget and a stall timeout."""
    outcome = {}

    def worker():
        try:
            connection.request(method, target, body=body, headers=headers)
            outcome['done'] = True
        except BaseException as error:  # re-raised in the request thread
            outcome['error'] = error

    sock = _socket_for(connection)
    if sock is not None and stall_seconds:
        try:
            sock.settimeout(float(stall_seconds))
        except OSError:
            pass
    worker_thread = threading.Thread(target=worker, daemon=True,
                                     name='codex-upload')
    worker_thread.start()
    worker_thread.join(max(1.0, float(limit_seconds)))
    if worker_thread.is_alive():
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            connection.close()
        except OSError:
            pass
        worker_thread.join(2)
        raise UploadTimeout('request upload exceeded its time budget')
    error = outcome.get('error')
    if error is not None:
        if isinstance(error, TimeoutError):
            raise UploadTimeout('request upload stalled') from error
        raise error
    return outcome.get('done', False)


_OFFICIAL_LANE_FAILURES = {}
_LANE_FAILURE_LOCK = threading.Lock()


def mark_official_lane_failed(connection, seconds=30):
    lane = getattr(connection, 'route_lane', None)
    if not isinstance(lane, str) or not lane:
        return
    with _LANE_FAILURE_LOCK:
        _OFFICIAL_LANE_FAILURES[lane] = time.monotonic() + max(1, seconds)


def _lane_available(lane):
    with _LANE_FAILURE_LOCK:
        until = _OFFICIAL_LANE_FAILURES.get(lane, 0.0)
        if until <= time.monotonic():
            _OFFICIAL_LANE_FAILURES.pop(lane, None)
            return True
        return False


def official_route_candidates(settings, route_state=None):
    """Return ordered dedicated/project/SakuraCat/direct candidate records."""
    if route_state is None:
        state = {}
        if settings.get('CodexOfficialEnabled', True):
            try:
                heartbeat = max(120, int(2 * float(
                    settings.get('CodexOfficialHealthMinutes', 3)) * 60))
                state = load_route_state(ROOT, max_age=heartbeat,
                                         core_pid=read_core_pid(ROOT) or None)
            except (OSError, ValueError, TypeError):
                state = {}
    else:
        state = route_state
    candidates = []
    official_lane = str(settings.get('CodexOfficialSelector') or 'codex-official')
    if (settings.get('CodexOfficialEnabled', True)
            and state.get('ready') and not state.get('degraded')):
        candidates.append({
            'lane': official_lane,
            'host': '127.0.0.1',
            'port': int(state.get('port') or settings.get('CodexOfficialPort', 12551)),
            'node': state.get('active_tag'),
        })
    candidates.extend([
        {'lane': 'project', 'host': '127.0.0.1',
         'port': int(settings.get('MixedPort', 12550)), 'node': None},
        {'lane': 'sakuracat', 'host': settings.get('SakuraHost', '127.0.0.1'),
         'port': int(settings.get('SakuraPort', 12450)), 'node': None},
        {'lane': 'direct', 'host': OFFICIAL_HOST, 'port': None, 'node': None},
    ])
    available = [item for item in candidates if _lane_available(item['lane'])]
    return available or candidates[-1:]


def official_connection(route_state=None):
    """Use the official lane first, then the measured project route."""
    from settings_config import load_settings
    settings = load_settings(ROOT)
    for candidate in official_route_candidates(settings, route_state):
        host, port = candidate['host'], candidate['port']
        if port is None:
            connection = http.client.HTTPSConnection(OFFICIAL_HOST, timeout=12)
            connection.route_lane = candidate['lane']
            connection.route_node = None
            return connection
        try:
            with socket.create_connection((host, port), timeout=.3):
                pass
        except OSError:
            continue
        connection = http.client.HTTPSConnection(host, port, timeout=12)
        connection.set_tunnel(OFFICIAL_HOST, 443)
        connection.route_lane = candidate['lane']
        connection.route_node = candidate['node']
        return connection
    connection = http.client.HTTPSConnection(OFFICIAL_HOST, timeout=12)
    connection.route_lane = 'direct'
    connection.route_node = None
    return connection


def connect_upstream(connection):
    # Bound TCP/TLS setup without timing out long model reasoning or images.
    connection.timeout = 12
    connection.connect()
    try:
        connection.sock.settimeout(float(getattr(connection, 'headers_timeout', 360)))
    except (OSError, TypeError, ValueError):
        pass


def reply(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Connection', 'close')
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(body)


def passthrough_request_shape(headers, body, path):
    """Record image request schema only; never persist prompts, media, or auth values."""
    if not (path.startswith('/images/generations') or path.startswith('/images/edits')):
        return {}
    result = {
        'request_header_names': sorted({key.lower() for key in headers.keys()}),
    }
    for name in ('x-codex-image-turn-id', 'x-codex-turn-metadata', 'user-agent',
                 'content-type', 'content-encoding', 'accept'):
        value = headers.get(name)
        result[name.replace('-', '_') + '_length'] = len(value) if value else 0
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return result
    if isinstance(value, dict):
        result['request_json_keys'] = sorted(value)
        prompt = value.get('prompt')
        if isinstance(prompt, str):
            result['prompt_chars'] = len(prompt)
        for key in ('model', 'quality', 'size', 'background'):
            item = value.get(key)
            if isinstance(item, str) and len(item) <= 64:
                result[key] = item
        images = value.get('images')
        if isinstance(images, list):
            result['image_items'] = len(images)
    return result

def forwarding_headers(headers, decoded=False, websocket=False):
    excluded = HOP_HEADERS | {
        token.strip().lower() for token in headers.get('Connection', '').split(',')
        if token.strip()
    }
    if decoded:
        excluded.add('content-encoding')
    result = {k: v for k, v in headers.items() if k.lower() not in excluded}
    if websocket:
        result.update({'Connection': 'Upgrade', 'Upgrade': 'websocket'})
    return result


def relay_websocket(handler, upstream, response):
    """Copy upgrade bytes without interpreting frames or buffering a response.

    Use the existing buffered readers: an HTTP parser may already have read
    part of the first frame together with the handshake headers.
    """
    client = handler.connection
    handler.close_connection = True

    def stop():
        for sock in (upstream, client):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def upload():
        try:
            while chunk := handler.rfile.read1(65536):
                upstream.sendall(chunk)
        except OSError:
            pass
        finally:
            stop()

    worker = threading.Thread(target=upload, daemon=True)
    worker.start()
    total = 0
    try:
        while chunk := response.fp.read1(65536):
            client.sendall(chunk)
            total += len(chunk)
    finally:
        stop()
        worker.join(timeout=1)
    return total


class Router:
    def __init__(self, catalog_path=None, secret_path=None, api_port=8799,
                 event_path=None, settings=None, official_manager=None):
        self.catalog_path = Path(catalog_path or GENERATED_CATALOG)
        self.secret_path = Path(secret_path or ROOT / 'state/codex-router-secret.txt')
        self.api_port = api_port
        self.event_path = Path(event_path or ROOT / 'logs/codex-routes.jsonl')
        self.lock = threading.Lock()
        self._catalog_cache = (None, None)
        self.settings = dict(settings or {})
        self.official_selector = str(
            self.settings.get('CodexOfficialSelector') or 'codex-official')
        # The running manager is shared with the backend that owns the official
        # selector. Never create a second state writer here.
        self.official_manager = official_manager

    def _handle_connect_failure(self, connection):
        """Rotate the dedicated selector before falling back to a shared lane."""
        lane = getattr(connection, 'route_lane', None)
        node = getattr(connection, 'route_node', None)
        if lane == self.official_selector and self.official_manager is not None:
            try:
                if self.official_manager.failover_after_connect_failure(node):
                    return True
            except (OSError, ValueError, http.client.HTTPException):
                pass
        mark_official_lane_failed(connection)
        return False

    def catalog(self):
        try:
            stat = self.catalog_path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        if key is not None and self._catalog_cache[0] == key:
            return self._catalog_cache[1]
        value = json.loads(self.catalog_path.read_text(encoding='utf-8'))
        self._catalog_cache = (key, value)
        return value

    def record_validation_failure(self, handler, path, stage, error, status):
        """Record request-shape failures without logging prompts or credentials."""
        event = {
            'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'route': 'codex-validation',
            'method': handler.command,
            'path': path,
            'status': status,
            'failure_stage': stage,
            'error': type(error).__name__ if error else None,
            'content_length': handler.headers.get('Content-Length'),
            'content_encoding': handler.headers.get('Content-Encoding'),
            'transfer_encoding': handler.headers.get('Transfer-Encoding'),
        }
        with self.lock:
            with self.event_path.open('a', encoding='utf-8') as log:
                log.write(json.dumps(event) + '\n')

    def handle(self, handler, method):
        parsed = urllib.parse.urlsplit(handler.path)
        if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + '/'):
            return False
        try:
            secret = self.secret_path.read_text(encoding='utf-8').strip()
        except OSError:
            reply(handler, 503, {'error': {'message': 'Codex router has not been configured'}})
            return True
        supplied = handler.headers.get('X-Unified-Codex-Key', '')
        if not secret or not hmac.compare_digest(secret, supplied):
            reply(handler, 401, {'error': {'message': 'Local Codex router authentication required'}})
            return True
        path = parsed.path[len(PREFIX):]
        if method == 'GET' and path == '/models':
            try:
                reply(handler, 200, self.catalog())
            except (OSError, ValueError, TypeError, KeyError):
                reply(handler, 503, {'error': {'message': 'Codex model catalog is unavailable or invalid'}})
            return True
        inference = method == 'POST' and path in ('/responses', '/responses/compact')
        if not inference:
            return self.forward_official(handler, method, parsed)
        request_encoding = (handler.headers.get('Content-Encoding') or '').lower()
        try:
            wire_body = read_body(handler, decode=False)
            body, request_encoding = decode_body(wire_body, request_encoding)
            if len(body) > MAX_BODY:
                raise RequestBodyTooLarge('Decoded request body exceeds limit')
        except RequestBodyTooLarge as error:
            self.record_validation_failure(handler, path, 'body_too_large', error, 413)
            reply(handler, 413, {'error': {
                'message': f'Codex request body exceeds the {MAX_BODY_MIB} MiB gateway limit'}})
            return True
        except Exception as error:
            self.record_validation_failure(handler, path, 'body_read', error, 400)
            reply(handler, 400, {'error': {
                'message': 'Invalid Codex request body: reading or decompressing failed'}})
            return True
        request_bytes = len(wire_body)
        decoded_request_bytes = len(body)
        try:
            data = json.loads(body)
        except Exception as error:
            self.record_validation_failure(handler, path, 'json_parse', error, 400)
            reply(handler, 400, {'error': {'message': 'Invalid Codex request JSON'}})
            return True
        if not isinstance(data, dict):
            error = TypeError('request payload is not an object')
            self.record_validation_failure(handler, path, 'payload_shape', error, 400)
            reply(handler, 400, {'error': {'message': 'Codex request body must be a JSON object'}})
            return True
        model = data.get('model', '')
        if not isinstance(model, str) or not model:
            error = TypeError('model is not a non-empty string')
            self.record_validation_failure(handler, path, 'model_shape', error, 400)
            reply(handler, 400, {'error': {'message': 'Codex model must be a non-empty string'}})
            return True
        # Official availability is decided by the official service. A stale or
        # unavailable menu must not block newly released models or old chats.
        # Namespaced third-party models still require an explicit local route.
        if '/' in model and model not in API_MODELS:
            self.record_validation_failure(
                handler, path, 'model_not_found', None, 400)
            reply(handler, 400, {'error': {'message': 'Third-party model is not in the configured catalog'}})
            return True
        third_party = model in API_MODELS
        if third_party and path != '/responses':
            reply(handler, 400, {'error': {'message': 'Third-party compact endpoint is not enabled'}})
            return True
        connection = None
        compatibility = None
        started = time.monotonic()
        metrics = StreamMetrics(started)
        status, total = None, 0
        reasoning_items_filtered = 0
        connect_attempts = 0
        route = 'third-party' if third_party else 'official-subscription'
        route_lane = 'third-party' if third_party else 'unknown'
        route_node = None
        failure_stage = 'connect'
        upload_timed_out = False
        official = not third_party
        try:
            connect_timeout = float(self.settings.get(
                'CodexOfficialConnectTimeoutSeconds' if official
                else 'AiUpstreamConnectTimeoutSeconds', 12))
            headers_timeout = float(self.settings.get(
                'CodexOfficialHeadersTimeoutSeconds' if official
                else 'AiUpstreamHeadersTimeoutSeconds', 360))
            stream_idle = float(self.settings.get(
                'CodexOfficialStreamIdleSeconds' if official
                else 'AiUpstreamStreamIdleSeconds', 180 if official else 360))
            if third_party:
                # Deliberate allowlist: no ChatGPT token, account ID, cookies, or routing
                # headers are allowed onto the third-party network path.
                headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                           'Authorization': 'Bearer ' + api_credential()}
                connection = http.client.HTTPConnection(
                    '127.0.0.1', self.api_port, timeout=connect_timeout)
                target = '/v1/responses'
                # Account-specific opaque reasoning state cannot cross providers.
                data.pop('previous_response_id', None)
                data.pop('prompt_cache_key', None)
                data.pop('service_tier', None)
                if model == 'openai/gpt-6-luna' and isinstance(data.get('tools'), list):
                    # This API backend reserves the internal imagegen name and
                    # rejects Codex's desktop-specific function schema for it.
                    data['tools'] = [tool for tool in data['tools']
                                     if tool.get('name') not in ('image_gen.imagegen', 'image_gen')]
                inputs = data.get('input')
                if isinstance(inputs, list):
                    data['input'] = [item for item in inputs if not isinstance(item, dict)
                                     or item.get('type') != 'reasoning']
                if model in {'glm-req/glm-5.3', 'glm-req/glm-5.3-flash', 'google/gemini-3.8-flash'} and any(
                        tool.get('type') in ('tool_search', 'namespace') for tool in data.get('tools', [])):
                    compatibility = ToolSearchCompat()
                    data = compatibility.request(data)
                body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            else:
                if not handler.headers.get('Authorization', '').startswith('Bearer '):
                    reply(handler, 401, {'error': {'message': 'ChatGPT sign-in is required for official models'}})
                    return True
                # Third-party providers also emit "encrypted_content", but in a
                # different wire shape (for example a short UUID). Sending that
                # state after a model switch makes the official backend reject
                # the entire request. Keep only official opaque items; text and
                # tool history remain available to preserve conversation flow.
                inputs = data.get('input')
                changed = False
                if isinstance(inputs, list):
                    filtered = []
                    for item in inputs:
                        if isinstance(item, dict) and item.get('type') == 'reasoning':
                            if not official_reasoning_item(item):
                                reasoning_items_filtered += 1
                                continue
                            # Some providers attach plaintext content to an
                            # otherwise valid opaque item. Keep its state and
                            # metadata; remove only the incompatible field.
                            if item.get('content'):
                                item = {k: v for k, v in item.items() if k != 'content'}
                        filtered.append(item)
                    if filtered != inputs:
                        data['input'] = filtered
                        changed = True
                if changed:
                    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                compression = bool(self.settings.get('CodexRequestCompression', True))
                level = self.settings.get('CodexCompressionLevel', 3)
                preserve_encoding = False
                if compression and request_encoding in ('zstd', 'gzip'):
                    if not changed:
                        body = wire_body
                    else:
                        body = encode_body(body, request_encoding, level)
                    preserve_encoding = True
                headers = forwarding_headers(handler.headers,
                                             decoded=not preserve_encoding)
                connection = official_connection()
                rtt_lane = getattr(connection, 'route_lane', None)
                if isinstance(rtt_lane, str) and rtt_lane:
                    route_lane = rtt_lane
                node = getattr(connection, 'route_node', None)
                route_node = node if isinstance(node, str) else None
                target = OFFICIAL_BASE + path
                if parsed.query:
                    target += '?' + parsed.query
            # A failed CONNECT/TLS setup happens before an inference request
            # is sent, so retrying it cannot duplicate model work. Do not retry
            # after request transmission starts because completion is ambiguous.
            while True:
                connect_attempts += 1
                connection.headers_timeout = headers_timeout
                try:
                    connect_upstream(connection)
                    break
                except (OSError, http.client.HTTPException):
                    if not third_party:
                        self._handle_connect_failure(connection)
                    if third_party or connect_attempts >= 4:
                        raise
                    connection.close()
                    time.sleep(.15)
                    connection = official_connection()
                    rtt_lane = getattr(connection, 'route_lane', None)
                    if isinstance(rtt_lane, str):
                        route_lane = rtt_lane
                    node = getattr(connection, 'route_node', None)
                    route_node = node if isinstance(node, str) else None
                    connection.headers_timeout = headers_timeout
            metrics.connect_seconds = time.monotonic() - started
            failure_stage = 'send'
            send_started = time.monotonic()
            if official:
                budget = upload_limit_seconds(len(body), self.settings, official=True)
                stall = float(self.settings.get(
                    'CodexOfficialUploadStallSeconds', 15))
                send_with_budget(connection, 'POST', target, body, headers, budget, stall)
            else:
                connection.request('POST', target, body=body, headers=headers)
            metrics.upload_seconds = time.monotonic() - send_started
            failure_stage = 'headers'
            sock = _socket_for(connection)
            if sock is not None:
                try:
                    sock.settimeout(headers_timeout)
                except OSError:
                    pass
            response = connection.getresponse()
            metrics.headers(response.getheader('Content-Type', ''), response.getheader('Content-Encoding', ''))
            status = response.status
            failure_stage = 'client_write'
            handler.send_response(status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and not (compatibility and key.lower() == 'content-length'):
                    handler.send_header(key, value)
            handler.send_header('Connection', 'close')
            handler.end_headers()
            failure_stage = 'response_body'
            sock = _socket_for(connection, response)
            if sock is not None:
                try:
                    sock.settimeout(stream_idle)
                except OSError:
                    pass
            chunks = compatibility.stream(response) if compatibility and status == 200 and 'text/event-stream' in response.getheader('Content-Type', '') else iter(lambda: response.read1(65536), b'')
            for chunk in chunks:
                metrics.feed(chunk)
                handler.wfile.write(chunk)
                handler.wfile.flush()
                total += len(chunk)
            metrics.eof = True
            failure_stage = None
            if metrics.sse and metrics.terminal_event is None:
                metrics.error = 'MissingTerminalEvent'
        except UploadTimeout as error:
            metrics.error = type(error).__name__
            upload_timed_out = True
            failure_stage = 'send'
            if status is None:
                reply(handler, 504, {'error': {
                    'message': 'Official Codex upload exceeded its gateway time budget'}})
        except (OSError, http.client.HTTPException, RuntimeError, sqlite3.Error) as error:
            metrics.error = type(error).__name__
            if status is None:
                reply(handler, 502, {'error': {'message': 'Selected upstream route unavailable; see local route status'}})
        finally:
            if connection:
                connection.close()
            forwarded_request_bytes = len(body) if isinstance(body, (bytes, bytearray)) else 0
            event = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'model': model,
                     'route': route, 'route_lane': route_lane,
                     'route_node': route_node,
                     'status': status, 'bytes': total,
                     'seconds': round(time.monotonic() - started, 3),
                     'reasoning_items_filtered': reasoning_items_filtered,
                     'upstream_connect_attempts': connect_attempts,
                     'failure_stage': failure_stage,
                     'upload_timed_out': upload_timed_out,
                     'request_bytes': request_bytes,
                     'decoded_request_bytes': decoded_request_bytes,
                     'forwarded_request_bytes': forwarded_request_bytes,
                     'request_encoding': request_encoding,
                     'error': metrics.error,
                     **metrics.snapshot()}
            if self.official_manager is not None and official:
                try:
                    self.official_manager.record_request(
                        upload_seconds=metrics.upload_seconds,
                        headers_seconds=metrics.headers_seconds,
                        failure_stage=failure_stage,
                        upload_timed_out=upload_timed_out,
                        stream_error=metrics.error,
                        tag=route_node, lane=route_lane,
                        request_bytes=forwarded_request_bytes,
                        connect_attempts=connect_attempts)
                except (OSError, ValueError, TypeError):
                    pass
            with self.lock:
                with self.event_path.open('a', encoding='utf-8') as log:
                    log.write(json.dumps(event) + '\n')
        return True

    def forward_official(self, handler, method, parsed):
        """Forward auxiliary and future Codex endpoints to the fixed official host.

        Only inference POSTs enter model routing. Auxiliary endpoints may use
        multipart, binary, compressed, or model-less payloads; preserve them.
        """
        if not handler.headers.get('Authorization', '').startswith('Bearer '):
            reply(handler, 401, {'error': {'message': 'ChatGPT sign-in is required for official endpoints'}})
            return True
        try:
            body = read_body(handler, decode=False)
        except (ValueError, OSError):
            reply(handler, 400, {'error': {'message': 'Invalid Codex request body'}})
            return True
        path = parsed.path[len(PREFIX):]
        target = OFFICIAL_BASE + path
        if parsed.query:
            target += '?' + parsed.query
        websocket = handler.headers.get('Upgrade', '').lower() == 'websocket'
        headers = forwarding_headers(handler.headers, websocket=websocket)
        request_shape = passthrough_request_shape(handler.headers, body, path)
        connection = response = None
        started = time.monotonic()
        metrics = StreamMetrics(started)
        status, total = None, 0
        connect_attempts = 0
        failure_stage = 'connect'
        error_name = connect_retry_error = None
        route_lane, route_node = 'unknown', None
        upload_timed_out = False
        headers_timeout = float(self.settings.get(
            'CodexOfficialHeadersTimeoutSeconds', 360))
        stream_idle = float(self.settings.get(
            'CodexOfficialStreamIdleSeconds', 180))
        try:
            # A failed factory or CONNECT/TLS setup happens before any request
            # bytes are sent, so one fresh connection cannot duplicate work.
            while True:
                connect_attempts += 1
                try:
                    connection = official_connection()
                    rtt_lane = getattr(connection, 'route_lane', None)
                    if isinstance(rtt_lane, str):
                        route_lane = rtt_lane
                    node = getattr(connection, 'route_node', None)
                    route_node = node if isinstance(node, str) else None
                    connection.headers_timeout = headers_timeout
                    connect_upstream(connection)
                    break
                except (OSError, http.client.HTTPException, RuntimeError) as error:
                    error_name = connect_retry_error = type(error).__name__
                    if connection:
                        self._handle_connect_failure(connection)
                        connection.close()
                        connection = None
                    if connect_attempts >= 4:
                        raise
                    time.sleep(.15)
            error_name = None
            metrics.connect_seconds = time.monotonic() - started
            failure_stage = 'send'
            send_started = time.monotonic()
            budget = upload_limit_seconds(len(body), self.settings, official=True)
            stall = float(self.settings.get('CodexOfficialUploadStallSeconds', 15))
            send_with_budget(connection, method, target, body or None, headers,
                             budget, stall)
            metrics.upload_seconds = time.monotonic() - send_started
            # getresponse() can detach connection.sock for close-delimited
            # replies. The response reader still owns the live transport.
            upgrade_socket = connection.sock
            failure_stage = 'headers'
            sock = _socket_for(connection)
            if sock is not None:
                try:
                    sock.settimeout(headers_timeout)
                except OSError:
                    pass
            response = connection.getresponse()
            metrics.headers(response.getheader('Content-Type', ''),
                            response.getheader('Content-Encoding', ''))
            status = response.status
            failure_stage = 'client_write'
            handler.send_response(status, response.reason)
            if status == 101:
                # Connection and Upgrade are required on a WebSocket handshake.
                for key, value in response.getheaders():
                    if key.lower() not in HOP_HEADERS or key.lower() in ('connection', 'upgrade'):
                        handler.send_header(key, value)
                handler.end_headers()
                handler.wfile.flush()
                failure_stage = 'response_body'
                total = relay_websocket(handler, upgrade_socket, response)
            else:
                for key, value in response.getheaders():
                    if key.lower() not in HOP_HEADERS or key.lower() == 'content-length':
                        handler.send_header(key, value)
                # http.client decodes transfer framing. Close-delimit the body
                # so binary, compressed and SSE responses all remain intact.
                handler.send_header('Connection', 'close')
                handler.end_headers()
                handler.close_connection = True
                if method != 'HEAD':
                    failure_stage = 'response_body'
                    sock = _socket_for(connection, response)
                    if sock is not None:
                        try:
                            sock.settimeout(stream_idle)
                        except OSError:
                            pass
                    while chunk := response.read1(65536):
                        metrics.feed(chunk)
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
                        total += len(chunk)
            metrics.eof = True
            failure_stage = None
        except UploadTimeout as error:
            error_name = type(error).__name__
            metrics.error = error_name
            upload_timed_out = True
            failure_stage = 'send'
            if status is None:
                reply(handler, 504, {'error': {
                    'message': 'Official Codex upload exceeded its gateway time budget'}})
        except (OSError, http.client.HTTPException, RuntimeError) as error:
            error_name = type(error).__name__
            metrics.error = error_name
            if status is None:
                reply(handler, 502, {'error': {'message': 'Official Codex route unavailable'}})
        finally:
            if response:
                response.close()
            if connection:
                connection.close()
            event = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                     'route': 'official-passthrough', 'method': method,
                     'route_lane': route_lane, 'route_node': route_node,
                     'path': path, 'status': status, 'bytes': total,
                     'body_bytes': len(body),
                     'request_bytes': len(body),
                     'forwarded_request_bytes': len(body),
                     'seconds': round(time.monotonic() - started, 3),
                     'upstream_connect_attempts': connect_attempts,
                     'failure_stage': failure_stage,
                     'upload_timed_out': upload_timed_out,
                     'error': error_name,
                     'connect_retry_error': connect_retry_error,
                     **request_shape,
                     **metrics.snapshot()}
            if self.official_manager is not None:
                try:
                    self.official_manager.record_request(
                        upload_seconds=metrics.upload_seconds,
                        headers_seconds=metrics.headers_seconds,
                        failure_stage=failure_stage,
                        upload_timed_out=upload_timed_out,
                        stream_error=metrics.error,
                        tag=route_node, lane=route_lane,
                        request_bytes=len(body),
                        connect_attempts=connect_attempts)
                except (OSError, ValueError, TypeError):
                    pass
            with self.lock:
                with self.event_path.open('a', encoding='utf-8') as log:
                    log.write(json.dumps(event) + '\n')
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18799)
    args = parser.parse_args()
    router = Router()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if not router.handle(self, 'GET'):
                reply(self, 404, {'error': {'message': 'Unknown test endpoint'}})

        def do_POST(self):
            if not router.handle(self, self.command):
                reply(self, 404, {'error': {'message': 'Unknown test endpoint'}})

        do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_POST

    http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
