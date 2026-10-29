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
from codex_catalog import GENERATED_CATALOG, local_catalog

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '/codex'
OFFICIAL_HOST = 'chatgpt.com'
OFFICIAL_BASE = '/backend-api/codex'
API_MODELS = {model['slug'] for model in local_catalog()['models']}
MAX_BODY = 32 * 1024 * 1024
HOP_HEADERS = {'host', 'connection', 'transfer-encoding', 'content-length',
               'keep-alive', 'proxy-connection', 'proxy-authorization', 'upgrade',
               'x-unified-codex-key'}


def api_credential():
    """Reuse the existing localhost API credential without copying it into config."""
    path = Path.home() / '.cc-switch/cc-switch.db'
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=3) as db:
        for raw, in db.execute("SELECT settings_config FROM providers WHERE app_type='claude'"):
            env = json.loads(raw).get('env', {})
            base = urllib.parse.urlsplit(str(env.get('ANTHROPIC_BASE_URL', '')))
            if base.hostname in ('127.0.0.1', 'localhost') and base.port == 8799:
                key = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')
                if key:
                    return key
    raise RuntimeError('Existing localhost API credential unavailable')


def read_body(handler):
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
                raise ValueError('Request body exceeds limit')
            chunk = handler.rfile.read(size)
            if len(chunk) != size or handler.rfile.read(2) != b'\r\n':
                raise ValueError('Incomplete HTTP chunk')
            chunks.append(chunk)
        body = b''.join(chunks)
    else:
        size = int(handler.headers.get('Content-Length', 0))
        if size < 0 or size > MAX_BODY:
            raise ValueError('Request body exceeds limit')
        body = handler.rfile.read(size)
        if len(body) != size:
            raise ValueError('Incomplete request body')
    encoding = handler.headers.get('Content-Encoding', '').lower()
    if encoding == 'zstd':
        body = zstd.decompress(body)
    elif encoding == 'gzip':
        body = gzip.decompress(body)
    elif encoding not in ('', 'identity'):
        raise ValueError('Unsupported request content encoding')
    if len(body) > MAX_BODY:
        raise ValueError('Decoded request body exceeds limit')
    return body


def official_connection():
    """Use Sakura's local HTTP tunnel when available; otherwise use native routing."""
    from settings_config import load_settings
    settings = load_settings(ROOT)
    host = settings.get('SakuraHost', '127.0.0.1')
    port = settings.get('SakuraPort', 12450)
    try:
        with socket.create_connection((host, port), timeout=.3):
            pass
    except OSError:
        return http.client.HTTPSConnection(OFFICIAL_HOST, timeout=180)
    connection = http.client.HTTPSConnection(host, port, timeout=180)
    connection.set_tunnel(OFFICIAL_HOST, 443)
    return connection


def reply(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.wfile.write(body)


class Router:
    def __init__(self, catalog_path=None, secret_path=None, api_port=8799, event_path=None):
        self.catalog_path = Path(catalog_path or GENERATED_CATALOG)
        self.secret_path = Path(secret_path or ROOT / 'state/codex-router-secret.txt')
        self.api_port = api_port
        self.event_path = Path(event_path or ROOT / 'logs/codex-routes.jsonl')
        self.lock = threading.Lock()

    def catalog(self):
        return json.loads(self.catalog_path.read_text(encoding='utf-8'))

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
        if method != 'POST' or path not in ('/responses', '/responses/compact'):
            reply(handler, 404, {'error': {'message': 'Unsupported Codex router endpoint'}})
            return True
        try:
            body = read_body(handler)
            data = json.loads(body)
            model = data.get('model', '')
            if not isinstance(model, str) or not model:
                reply(handler, 400, {'error': {'message': 'Codex model must be a non-empty string'}})
                return True
            # Official availability is decided by the service, not a stale menu.
            # Namespaced third-party models require an explicit local route.
            if '/' in model and model not in API_MODELS:
                reply(handler, 400, {'error': {'message': 'Third-party model is not in the configured catalog'}})
                return True
        except (ValueError, TypeError, AttributeError, OSError):
            reply(handler, 400, {'error': {'message': 'Invalid Codex request body or catalog'}})
            return True
        third_party = model in API_MODELS
        if third_party and path != '/responses':
            reply(handler, 400, {'error': {'message': 'Third-party compact endpoint is not enabled'}})
            return True
        connection = None
        compatibility = None
        started = time.monotonic()
        status, total = None, 0
        route = 'third-party' if third_party else 'official-subscription'
        try:
            if third_party:
                # Deliberate allowlist: no ChatGPT token, account ID, cookies, or routing
                # headers are allowed onto the third-party network path.
                headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                           'Authorization': 'Bearer ' + api_credential()}
                connection = http.client.HTTPConnection('127.0.0.1', self.api_port, timeout=180)
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
                        tool.get('type') == 'tool_search' for tool in data.get('tools', [])):
                    compatibility = ToolSearchCompat()
                    data = compatibility.request(data)
                body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            else:
                if not handler.headers.get('Authorization', '').startswith('Bearer '):
                    reply(handler, 401, {'error': {'message': 'ChatGPT sign-in is required for official models'}})
                    return True
                headers = {k: v for k, v in handler.headers.items()
                           if k.lower() not in HOP_HEADERS | {'content-encoding'}}
                # Third-party providers can return plaintext reasoning items with
                # content fields that the official Codex backend rejects. Keep
                # official opaque reasoning; conversation text/tool history stays.
                inputs = data.get('input')
                if isinstance(inputs, list):
                    filtered = [item for item in inputs if not isinstance(item, dict)
                                or item.get('type') != 'reasoning'
                                or (item.get('encrypted_content') and not item.get('content'))]
                    if len(filtered) != len(inputs):
                        data['input'] = filtered
                        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                connection = official_connection()
                target = OFFICIAL_BASE + path
                if parsed.query:
                    target += '?' + parsed.query
            connection.request('POST', target, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            handler.send_response(status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and not (compatibility and key.lower() == 'content-length'):
                    handler.send_header(key, value)
            handler.send_header('Connection', 'close')
            handler.end_headers()
            chunks = compatibility.stream(response) if compatibility and status == 200 and 'text/event-stream' in response.getheader('Content-Type', '') else iter(lambda: response.read1(65536), b'')
            for chunk in chunks:
                handler.wfile.write(chunk)
                handler.wfile.flush()
                total += len(chunk)
        except (OSError, http.client.HTTPException, RuntimeError, sqlite3.Error):
            if status is None:
                reply(handler, 502, {'error': {'message': 'Selected upstream route unavailable; see local route status'}})
        finally:
            if connection:
                connection.close()
            event = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'model': model,
                     'route': route, 'status': status, 'bytes': total,
                     'seconds': round(time.monotonic() - started, 3)}
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
            if not router.handle(self, 'POST'):
                reply(self, 404, {'error': {'message': 'Unknown test endpoint'}})

    http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
