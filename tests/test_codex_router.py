import http.client
import http.server
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from compression import zstd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import codex_router as cr


class Upstream(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        payload = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class CodexRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'secret').write_text('local-only-secret', encoding='utf-8')
        (root / 'models').write_text(json.dumps({'models': [
            {'slug': 'gpt-6.1-sol'}] + [{'slug': m} for m in sorted(cr.API_MODELS)]}), encoding='utf-8')
        Upstream.seen = []
        self.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        router = cr.Router(root / 'models', root / 'secret', self.upstream.server_port, root / 'events')
        self.router = router

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                router.handle(self, 'GET')

            def do_POST(self):
                router.handle(self, 'POST')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        for server in (self.server, self.upstream):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.key_patch = patch.object(cr, 'api_credential', return_value='third-party-key')
        self.connection_patch = patch.object(cr, 'official_connection',
            side_effect=lambda: http.client.HTTPConnection('127.0.0.1', self.upstream.server_port))
        self.key_patch.start()
        self.connection_patch.start()

    def tearDown(self):
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        self.connection_patch.stop()
        self.key_patch.stop()
        self.temp.cleanup()

    def request(self, model=None, path='/codex/responses', headers=None, compressed=False):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        data = {'model': model, 'stream': True, 'tools': [
            {'type': 'function', 'name': 'image_gen.imagegen', 'parameters': {}},
            {'type': 'function', 'name': 'ordinary_tool', 'parameters': {}}], 'input': [
            {'type': 'reasoning', 'content': [{'type': 'reasoning_text', 'text': 'synthetic'}]},
            {'type': 'reasoning', 'encrypted_content': 'official-opaque-state', 'content': []},
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'test'}]}]}
        body = json.dumps(data).encode()
        outgoing = {'X-Unified-Codex-Key': 'local-only-secret', 'Content-Type': 'application/json',
                    'Authorization': 'Bearer official-chatgpt-token', 'ChatGPT-Account-ID': 'official-account',
                    'Cookie': 'official-cookie', 'X-Routing-Private': 'official-metadata'}
        if headers:
            outgoing.update(headers)
        if compressed:
            body = zstd.compress(body)
            outgoing['Content-Encoding'] = 'zstd'
        client.request('POST' if model is not None else 'GET', path,
                       body=body if model is not None else None, headers=outgoing)
        response = client.getresponse()
        result = response.status, response.read()
        client.close()
        return result

    def test_catalog_contains_both_routes(self):
        status, body = self.request(path='/codex/models?client_version=0.163')
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)['models']), 1 + len(cr.API_MODELS))
        self.assertEqual(Upstream.seen, [])

    def test_local_secret_required_before_any_upstream(self):
        self.assertEqual(self.request('ds/deepseek-v4.1-flash', headers={'X-Unified-Codex-Key': 'wrong'})[0], 401)
        self.assertEqual(Upstream.seen, [])

    def test_third_party_never_receives_chatgpt_identity(self):
        status, body = self.request('ds/deepseek-v4.1-flash')
        self.assertEqual(status, 200)
        self.assertIn(b'response.completed', body)
        path, headers, data = Upstream.seen[-1]
        self.assertEqual(path, '/v1/responses')
        self.assertEqual(headers['authorization'], 'Bearer third-party-key')
        for name in ('chatgpt-account-id', 'cookie', 'x-unified-codex-key', 'x-routing-private'):
            self.assertNotIn(name, headers)
        self.assertFalse(any(item.get('type') == 'reasoning' for item in data['input']))

    def test_official_credentials_and_opaque_reasoning_preserved(self):
        self.assertEqual(self.request('gpt-6.1-sol')[0], 200)
        path, headers, data = Upstream.seen[-1]
        self.assertEqual(path, '/backend-api/codex/responses')
        self.assertEqual(headers['authorization'], 'Bearer official-chatgpt-token')
        self.assertEqual(headers['chatgpt-account-id'], 'official-account')
        self.assertNotIn('x-unified-codex-key', headers)
        self.assertEqual([x['type'] for x in data['input']], ['reasoning', 'message'])
        self.assertEqual(data['input'][0]['encrypted_content'], 'official-opaque-state')

    def test_added_models_use_api_and_preserve_exact_model_id(self):
        for model in ('glm-req/glm-5.3', 'glm-req/glm-5.3-flash', 'google/gemini-3.8-flash', 'openai/gpt-6-luna'):
            with self.subTest(model=model):
                self.assertEqual(self.request(model)[0], 200)
                path, headers, data = Upstream.seen[-1]
                self.assertEqual(path, '/v1/responses')
                self.assertEqual(data['model'], model)
                self.assertEqual(headers['authorization'], 'Bearer third-party-key')
                self.assertNotIn('chatgpt-account-id', headers)

    def test_compressed_request_decoded_and_reframed(self):
        self.assertEqual(self.request('gpt-6.1-sol', compressed=True)[0], 200)
        self.assertNotIn('content-encoding', Upstream.seen[-1][1])
        self.assertEqual(Upstream.seen[-1][2]['model'], 'gpt-6.1-sol')

    def test_unknown_third_party_model_never_falls_through_to_other_provider(self):
        self.assertEqual(self.request('unconfigured/model')[0], 400)
        self.assertEqual(Upstream.seen, [])

    def test_api_luna_omits_reserved_imagegen_but_keeps_other_tools(self):
        self.assertEqual(self.request('openai/gpt-6-luna')[0], 200)
        self.assertEqual([t['name'] for t in Upstream.seen[-1][2]['tools']], ['ordinary_tool'])

    def test_third_party_compact_does_not_reach_official(self):
        self.assertEqual(self.request('ds/deepseek-v4.1-flash', '/codex/responses/compact')[0], 400)
        self.assertEqual(Upstream.seen, [])

    def test_official_inference_does_not_depend_on_catalog(self):
        with patch.object(self.router, 'catalog', side_effect=OSError('synthetic')):
            status, _ = self.request('gpt-6.1-terra')
        self.assertEqual(status, 200)
        self.assertEqual(Upstream.seen[-1][2]['model'], 'gpt-6.1-terra')
        self.assertEqual(Upstream.seen[-1][1]['authorization'], 'Bearer official-chatgpt-token')


    def test_invalid_catalog_returns_service_unavailable_for_menu(self):
        with patch.object(self.router, 'catalog', side_effect=OSError('synthetic')):
            status, body = self.request(path='/codex/models')
        self.assertEqual(status, 503)
        self.assertIn('catalog is unavailable', json.loads(body)['error']['message'])


    def test_new_official_model_is_validated_by_official_service(self):
        self.assertEqual(self.request('gpt-future-official-model')[0], 200)
        self.assertEqual(Upstream.seen[-1][0], '/backend-api/codex/responses')
        self.assertEqual(Upstream.seen[-1][2]['model'], 'gpt-future-official-model')



if __name__ == '__main__':
    unittest.main()
