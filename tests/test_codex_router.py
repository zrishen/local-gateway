import http.client
import http.server
import gzip
import json
from pathlib import Path
import sys
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from compression import zstd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import codex_router as cr

OFFICIAL_OPAQUE = 'gAAAA' + ('synthetic-opaque-state-' * 12)


class Upstream(http.server.BaseHTTPRequestHandler):
    seen = []
    stream_gate = None
    stream_finished = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if (self.headers.get('Upgrade', '').lower() == 'websocket'
                or self.path.split('?')[0] not in ('/v1/responses', '/backend-api/codex/responses',
                                                 '/backend-api/codex/responses/compact')):
            self.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, raw))
            if self.headers.get('Upgrade', '').lower() == 'websocket':
                self.send_response(101)
                self.send_header('Connection', 'Upgrade')
                self.send_header('Upgrade', 'websocket')
                self.send_header('Sec-WebSocket-Accept', 'test-accept')
                self.end_headers()
                # Send the first frame with the headers to exercise HTTP parser
                # read-ahead; neither buffered reader may lose these bytes.
                self.wfile.write(b'\x81\x02ok')
                self.wfile.flush()
                frame = self.rfile.read(8)
                self.seen.append(('websocket-frame', {}, frame))
                self.wfile.write(b'\x81\x02hi')
                self.wfile.flush()
                self.close_connection = True
                return
            payload = b'\x89PNG\r\n\x1a\nsynthetic-image'
            self.send_response(404 if '/missing' in self.path else 200)
            self.send_header('Content-Type', 'image/png')
            if '/compressed' in self.path:
                payload = gzip.compress(payload)
                self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Request-Id', 'official-test-request')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)
            return
        encoding = (self.headers.get('Content-Encoding') or '').lower()
        if encoding == 'zstd':
            raw = zstd.decompress(raw)
        elif encoding == 'gzip':
            raw = gzip.decompress(raw)
        body = json.loads(raw)
        self.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.stream_gate is not None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"first"}\n\n')
            self.wfile.flush()
            self.stream_gate.wait(3)
            self.stream_finished.set()
            self.wfile.write(b'data: {"type":"response.completed"}\n\n')
            self.wfile.flush()
            self.close_connection = True
            return
        payload = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_HEAD = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = do_POST


class CodexRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'secret').write_text('local-only-secret', encoding='utf-8')
        (root / 'models').write_text(json.dumps({'models': [
            {'slug': 'gpt-6.1-sol'}] + [{'slug': m} for m in sorted(cr.API_MODELS)]}), encoding='utf-8')
        Upstream.seen = []
        Upstream.stream_gate = Upstream.stream_finished = None
        self.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        router = cr.Router(root / 'models', root / 'secret', self.upstream.server_port, root / 'events')
        self.router = router

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                router.handle(self, 'GET')

            def do_POST(self):
                router.handle(self, self.command)

            do_HEAD = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = do_POST

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
            {'type': 'reasoning', 'id': 'rs_server_test', 'encrypted_content': OFFICIAL_OPAQUE,
             'content': []},
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

    def test_official_connection_prefers_measured_project_proxy(self):
        # Stop the per-test factory patch temporarily to exercise real routing.
        self.connection_patch.stop()
        try:
            with patch('settings_config.load_settings', return_value={
                    'MixedPort': 12550, 'SakuraPort': 12450}), \
                 patch.object(cr, 'load_route_state', return_value={}), \
                 patch.object(cr.socket, 'create_connection', return_value=MagicMock()) as probe, \
                 patch.object(cr.http.client, 'HTTPSConnection') as connection:
                cr.official_connection()
                self.assertEqual(probe.call_args.args[0], ('127.0.0.1', 12550))
                connection.assert_called_once_with('127.0.0.1', 12550, timeout=12)
                connection.return_value.set_tunnel.assert_called_once_with('chatgpt.com', 443)
        finally:
            self.connection_patch.start()

    def test_official_route_candidates_put_dedicated_lane_first(self):
        settings = {'MixedPort': 12550, 'SakuraPort': 12450,
                    'CodexOfficialEnabled': True, 'CodexOfficialPort': 12551}
        state = {'ready': True, 'degraded': False, 'port': 12551,
                 'active_tag': 'sakura-us-1'}
        lanes = cr.official_route_candidates(settings, state)
        self.assertEqual([item['lane'] for item in lanes],
                         ['codex-official', 'project', 'sakuracat', 'direct'])
        self.assertEqual(lanes[0]['node'], 'sakura-us-1')

    def test_dedicated_connect_failure_rotates_before_falling_back(self):
        cr._OFFICIAL_LANE_FAILURES.clear()
        manager = MagicMock()
        manager.failover_after_connect_failure.return_value = {
            'tag': 'sakura-us-2'}
        self.router.official_manager = manager
        failed = http.client.HTTPConnection('127.0.0.1', 1, timeout=.2)
        failed.route_lane = 'codex-official'
        failed.route_node = 'sakura-us-1'
        recovered = http.client.HTTPConnection(
            '127.0.0.1', self.upstream.server_port)
        recovered.route_lane = 'codex-official'
        recovered.route_node = 'sakura-us-2'
        self.connection_patch.stop()
        try:
            with patch.object(cr, 'official_connection',
                              side_effect=[failed, recovered]):
                status, _ = self.request('gpt-6.1-sol')
        finally:
            self.connection_patch.start()
            cr._OFFICIAL_LANE_FAILURES.clear()
        self.assertEqual(status, 200)
        manager.failover_after_connect_failure.assert_called_once_with(
            'sakura-us-1')
        event = json.loads(
            self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['route_lane'], 'codex-official')
        self.assertEqual(event['route_node'], 'sakura-us-2')
        self.assertEqual(event['upstream_connect_attempts'], 2)

    def test_failed_lane_is_temporarily_skipped_before_body_is_sent(self):
        settings = {'MixedPort': 12550, 'SakuraPort': 12450,
                    'CodexOfficialEnabled': True, 'CodexOfficialPort': 12551}
        state = {'ready': True, 'degraded': False, 'port': 12551,
                 'active_tag': 'sakura-us-1'}
        failed = MagicMock()
        failed.route_lane = 'codex-official'
        cr.mark_official_lane_failed(failed, seconds=60)
        try:
            lanes = cr.official_route_candidates(settings, state)
        finally:
            cr._OFFICIAL_LANE_FAILURES.clear()
        self.assertEqual([item['lane'] for item in lanes],
                         ['project', 'sakuracat', 'direct'])

    def test_official_connection_uses_ready_dedicated_lane(self):
        settings = {'MixedPort': 12550, 'SakuraPort': 12450,
                    'CodexOfficialEnabled': True, 'CodexOfficialPort': 12551}
        state = {'ready': True, 'degraded': False, 'port': 12551,
                 'active_tag': 'sakura-us-1'}
        self.connection_patch.stop()
        try:
            with patch('settings_config.load_settings', return_value=settings), \
                 patch.object(cr.socket, 'create_connection', return_value=MagicMock()) as probe, \
                 patch.object(cr.http.client, 'HTTPSConnection') as connection:
                result = cr.official_connection(route_state=state)
        finally:
            self.connection_patch.start()
        self.assertEqual(probe.call_args.args[0], ('127.0.0.1', 12551))
        self.assertEqual(result.route_lane, 'codex-official')
        self.assertEqual(result.route_node, 'sakura-us-1')
        connection.return_value.set_tunnel.assert_called_once_with('chatgpt.com', 443)

    def test_official_connect_setup_failure_retries_before_request(self):
        calls = []
        real_connect = cr.connect_upstream

        def flaky_connect(connection):
            calls.append(connection)
            if len(calls) == 1:
                raise ConnectionResetError('synthetic setup failure')
            return real_connect(connection)

        with patch.object(cr, 'connect_upstream', side_effect=flaky_connect), \
                patch.object(cr.time, 'sleep', return_value=None):
            self.assertEqual(self.request('gpt-6.1-sol')[0], 200)
        self.assertEqual(len(calls), 2)
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['upstream_connect_attempts'], 2)
        self.assertEqual(event['status'], 200)

    def test_request_send_failure_is_not_replayed(self):
        fake = MagicMock()
        fake.request.side_effect = ConnectionResetError('synthetic send failure')
        with patch.object(cr, 'official_connection', return_value=fake) as factory:
            status, _ = self.request('gpt-6.1-sol')
        self.assertEqual(status, 502)
        factory.assert_called_once()
        fake.request.assert_called_once()

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
        self.assertEqual(data['input'][0]['encrypted_content'], OFFICIAL_OPAQUE)

    def test_official_payload_preserves_quality_controls_images_tools_and_opaque_state(self):
        payload = {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'high'},
                   'instructions': 'keep all constraints', 'max_output_tokens': 12345,
                   'prompt_cache_key': 'synthetic-cache', 'service_tier': 'priority',
                   'tools': [{'type': 'function', 'name': 'test', 'parameters': {'type': 'object'}}],
                   'input': [{'type': 'reasoning', 'id': 'rs_test',
                              'encrypted_content': OFFICIAL_OPAQUE,
                              'content': [{'type': 'reasoning_text', 'text': 'incompatible'}]},
                             {'role': 'user', 'content': [{'type': 'input_image',
                               'image_url': 'data:image/png;base64,c3ludGhldGlj'}]}]}
        self.assertEqual(self.raw_request('POST', '/codex/responses', json.dumps(payload).encode())[0], 200)
        forwarded = Upstream.seen[-1][2]
        expected = json.loads(json.dumps(payload))
        expected['input'][0].pop('content')
        self.assertEqual(forwarded, expected)

    def test_model_switch_drops_third_party_encrypted_reasoning(self):
        payload = {'model': 'gpt-6.1-sol', 'stream': True, 'input': [
            {'type': 'reasoning', 'id': '108d2570-4028-4266-86d6-5ff64b7c1c82',
             'encrypted_content': '98e04cff-d487-4d0b-acda-b74807dfc979-0',
             'summary': []},
            {'type': 'reasoning', 'id': 'rs_official', 'encrypted_content': OFFICIAL_OPAQUE,
             'content': [{'type': 'reasoning_text', 'text': 'stale plaintext'}]},
            {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'continue'}]}]}
        self.assertEqual(self.raw_request('POST', '/codex/responses',
                         json.dumps(payload).encode())[0], 200)
        forwarded = Upstream.seen[-1][2]['input']
        self.assertEqual([item['type'] for item in forwarded],
                         ['reasoning', 'message'])
        self.assertEqual(forwarded[0]['id'], 'rs_official')
        self.assertNotIn('content', forwarded[0])
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['reasoning_items_filtered'], 1)

    def test_first_stream_chunk_arrives_before_upstream_completion(self):
        Upstream.stream_gate, Upstream.stream_finished = threading.Event(), threading.Event()
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            client.request('POST', '/codex/responses', json.dumps({'model': 'gpt-6.1-sol', 'input': 'test'}),
                           {'X-Unified-Codex-Key': 'local-only-secret',
                            'Authorization': 'Bearer official-chatgpt-token'})
            response = client.getresponse()
            first = response.read1(65536)
            self.assertIn(b'response.output_text.delta', first)
            self.assertFalse(Upstream.stream_finished.is_set())
            Upstream.stream_gate.set()
            self.assertIn(b'response.completed', response.read())
        finally:
            Upstream.stream_gate.set()
            client.close()

    def test_added_models_use_api_and_preserve_exact_model_id(self):
        for model in ('glm-req/glm-5.3', 'glm-req/glm-5.3-flash', 'google/gemini-3.8-flash', 'openai/gpt-6-luna'):
            with self.subTest(model=model):
                self.assertEqual(self.request(model)[0], 200)
                path, headers, data = Upstream.seen[-1]
                self.assertEqual(path, '/v1/responses')
                self.assertEqual(data['model'], model)
                self.assertEqual(headers['authorization'], 'Bearer third-party-key')
                self.assertNotIn('chatgpt-account-id', headers)

    def test_invalid_json_has_specific_safe_diagnostic(self):
        status, body, _ = self.raw_request(
            'POST', '/codex/responses?private=not-logged', b'{',
            {'Content-Type': 'application/json'})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)['error']['message'], 'Invalid Codex request JSON')
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['route'], 'codex-validation')
        self.assertEqual(event['failure_stage'], 'json_parse')
        self.assertEqual(event['path'], '/responses')
        self.assertNotIn('private', json.dumps(event))

    def test_default_body_limit_is_128_mib(self):
        self.assertEqual(cr.MAX_BODY_MIB, 128)
        self.assertEqual(cr.MAX_BODY, 128 * 1024 * 1024)

    def test_oversized_body_returns_413_with_safe_limit_diagnostic(self):
        with patch.object(cr, 'MAX_BODY', 8):
            status, body, _ = self.raw_request(
                'POST', '/codex/responses', b'123456789',
                {'Content-Type': 'application/json'})
        self.assertEqual(status, 413)
        self.assertIn('gateway limit', json.loads(body)['error']['message'])
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['failure_stage'], 'body_too_large')
        self.assertEqual(event['status'], 413)
        self.assertEqual(event['content_length'], '9')

    def test_invalid_compression_reports_body_stage(self):
        status, body, _ = self.raw_request(
            'POST', '/codex/responses', b'not-zstd',
            {'Content-Type': 'application/json', 'Content-Encoding': 'zstd'})
        self.assertEqual(status, 400)
        self.assertIn('reading or decompressing', json.loads(body)['error']['message'])
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['failure_stage'], 'body_read')
        self.assertEqual(event['content_encoding'], 'zstd')

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

    def test_compressed_request_preserves_zstd_after_reasoning_rewrite(self):
        self.assertEqual(self.request('gpt-6.1-sol', compressed=True)[0], 200)
        self.assertEqual(Upstream.seen[-1][1]['content-encoding'], 'zstd')
        self.assertEqual(Upstream.seen[-1][2]['model'], 'gpt-6.1-sol')
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['request_encoding'], 'zstd')
        self.assertGreater(event['decoded_request_bytes'], event['forwarded_request_bytes'])
        self.assertGreater(event['request_bytes'], 0)

    def test_unmodified_compressed_request_is_forwarded_byte_exact(self):
        payload = {'model': 'gpt-6.1-sol', 'input': 'hello'}
        compressed = zstd.compress(json.dumps(payload).encode())
        status, _, _ = self.raw_request(
            'POST', '/codex/responses', compressed,
            {'Content-Type': 'application/json', 'Content-Encoding': 'zstd'})
        self.assertEqual(status, 200)
        self.assertEqual(Upstream.seen[-1][1]['content-encoding'], 'zstd')
        self.assertEqual(Upstream.seen[-1][2], payload)
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['request_bytes'], event['forwarded_request_bytes'])
        self.assertEqual(event['request_bytes'], len(compressed))

    def test_compression_can_be_disabled_for_rewritten_request(self):
        self.router.settings['CodexRequestCompression'] = False
        self.assertEqual(self.request('gpt-6.1-sol', compressed=True)[0], 200)
        self.assertNotIn('content-encoding', Upstream.seen[-1][1])
        self.assertEqual(Upstream.seen[-1][2]['model'], 'gpt-6.1-sol')

    def test_upload_budget_is_size_based_and_bounded(self):
        settings = {'CodexOfficialUploadMinSeconds': 30,
                    'CodexOfficialUploadMaxSeconds': 180,
                    'CodexOfficialUploadMinimumKBps': 8}
        self.assertEqual(cr.upload_limit_seconds(0, settings), 30)
        self.assertEqual(cr.upload_limit_seconds(1024 * 1024, settings), 128)
        self.assertEqual(cr.upload_limit_seconds(1024 * 1024 * 100, settings), 180)

    def test_upload_timeout_is_reported_without_waiting_for_upstream(self):
        with patch.object(cr, 'send_with_budget',
                          side_effect=cr.UploadTimeout('synthetic upload timeout')):
            status, _ = self.request('gpt-6.1-sol')
        self.assertEqual(status, 504)
        # The response can reach the client before the server's finally block
        # finishes writing its diagnostic record.
        deadline = time.monotonic() + 2
        while (not self.router.event_path.exists() or not self.router.event_path.stat().st_size):
            if time.monotonic() >= deadline:
                self.fail('Upload timeout diagnostic was not written')
            time.sleep(.01)
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertTrue(event['upload_timed_out'])
        self.assertEqual(event['failure_stage'], 'send')
        self.assertEqual(event['error'], 'UploadTimeout')

    def test_official_events_include_lane_node_and_byte_metrics(self):
        def factory():
            connection = http.client.HTTPConnection(
                '127.0.0.1', self.upstream.server_port)
            connection.route_lane = 'codex-official'
            connection.route_node = 'sakura-us-1'
            return connection

        with patch.object(cr, 'official_connection', side_effect=factory):
            self.assertEqual(self.request('gpt-6.1-sol')[0], 200)
        event = json.loads(self.router.event_path.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['route_lane'], 'codex-official')
        self.assertEqual(event['route_node'], 'sakura-us-1')
        self.assertGreater(event['request_bytes'], 0)
        self.assertGreater(event['forwarded_request_bytes'], 0)
        self.assertIsNone(event['failure_stage'])
        self.assertFalse(event['upload_timed_out'])

    def test_unknown_third_party_model_never_falls_through_to_other_provider(self):
        self.assertEqual(self.request('unconfigured/model')[0], 400)
        self.assertEqual(Upstream.seen, [])

    def test_new_official_model_is_validated_by_official_service(self):
        self.assertEqual(self.request('gpt-future-official-model')[0], 200)
        self.assertEqual(Upstream.seen[-1][0], '/backend-api/codex/responses')
        self.assertEqual(Upstream.seen[-1][2]['model'], 'gpt-future-official-model')

    def test_api_luna_omits_reserved_imagegen_but_keeps_other_tools(self):
        self.assertEqual(self.request('openai/gpt-6-luna')[0], 200)
        self.assertEqual([t['name'] for t in Upstream.seen[-1][2]['tools']], ['ordinary_tool'])

    def test_third_party_compact_does_not_reach_official(self):
        self.assertEqual(self.request('ds/deepseek-v4.1-flash', '/codex/responses/compact')[0], 400)
        self.assertEqual(Upstream.seen, [])

    def latest_route_event(self, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.router.event_path.exists():
                lines = self.router.event_path.read_text(encoding='utf-8').splitlines()
                if lines:
                    return json.loads(lines[-1]), lines[-1]
            time.sleep(.01)
        self.fail('route event was not written')

    def raw_request(self, method, path, body=None, headers=None, chunked=False, port=None):
        outgoing = {'X-Unified-Codex-Key': 'local-only-secret',
                    'Authorization': 'Bearer official-chatgpt-token',
                    'ChatGPT-Account-ID': 'official-account'}
        outgoing.update(headers or {})
        client = http.client.HTTPConnection('127.0.0.1', port or self.server.server_port, timeout=5)
        client.request(method, path, body=body, headers=outgoing, encode_chunked=chunked)
        response = client.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        client.close()
        return result

    def test_official_auxiliary_methods_paths_and_query_are_forwarded(self):
        for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            with self.subTest(method=method):
                status, body, headers = self.raw_request(
                    method, '/codex/future/feature?id=a%2Fb&mode=image')
                self.assertEqual(status, 200)
                self.assertEqual(body, b'' if method == 'HEAD' else b'\x89PNG\r\n\x1a\nsynthetic-image')
                self.assertEqual(headers['X-Request-Id'], 'official-test-request')
                self.assertEqual(int(headers['Content-Length']), len(b'\x89PNG\r\n\x1a\nsynthetic-image'))
                target, sent_headers, _ = Upstream.seen[-1]
                self.assertEqual(target, '/backend-api/codex/future/feature?id=a%2Fb&mode=image')
                self.assertEqual(sent_headers['authorization'], 'Bearer official-chatgpt-token')
                self.assertEqual(sent_headers['chatgpt-account-id'], 'official-account')
                self.assertNotIn('x-unified-codex-key', sent_headers)

    def test_image_request_bypasses_text_model_catalog(self):
        body = b'{"model":"image-service-model","prompt":"synthetic"}'
        self.assertEqual(self.raw_request('POST', '/codex/images', body,
                         {'Content-Type': 'application/json'})[0], 200)
        self.assertEqual(Upstream.seen[-1][2], body)

    def test_multipart_image_upload_is_byte_exact(self):
        body = b'--boundary\r\nContent-Disposition: form-data; name="image"\r\n\r\n\x00\xff\x89PNG\r\n--boundary--\r\n'
        self.raw_request('POST', '/codex/images/edits', body,
                         {'Content-Type': 'multipart/form-data; boundary=boundary'})
        self.assertEqual(Upstream.seen[-1][2], body)
        self.assertEqual(Upstream.seen[-1][1]['content-type'],
                         'multipart/form-data; boundary=boundary')

    def test_compressed_auxiliary_request_and_response_are_preserved(self):
        payload = gzip.compress(b'{"image":"synthetic"}')
        status, body, headers = self.raw_request('POST', '/codex/images/compressed', payload,
                                                {'Content-Encoding': 'gzip'})
        self.assertEqual(status, 200)
        self.assertEqual(Upstream.seen[-1][2], payload)
        self.assertEqual(Upstream.seen[-1][1]['content-encoding'], 'gzip')
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(gzip.decompress(body), b'\x89PNG\r\n\x1a\nsynthetic-image')

    def test_chunked_binary_upload_is_reframed_without_changing_body(self):
        self.raw_request('PUT', '/codex/files/upload', iter([b'\x00\xff', b'file-data']), chunked=True)
        self.assertEqual(Upstream.seen[-1][2], b'\x00\xfffile-data')
        self.assertNotIn('transfer-encoding', Upstream.seen[-1][1])

    def test_auxiliary_endpoints_require_both_local_and_official_auth(self):
        for headers in ({'X-Unified-Codex-Key': 'wrong'}, {'Authorization': ''}):
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_request('GET', '/codex/files', headers=headers)[0], 401)
        self.assertEqual(Upstream.seen, [])

    def test_official_errors_are_relayed_instead_of_local_endpoint_rejection(self):
        status, body, _ = self.raw_request('GET', '/codex/missing')
        self.assertEqual(status, 404)
        self.assertEqual(body, b'\x89PNG\r\n\x1a\nsynthetic-image')
        self.assertEqual(len(Upstream.seen), 1)

    def test_auxiliary_connect_failure_retries_before_body_is_sent(self):
        calls = []
        real_connect = cr.connect_upstream

        def flaky_connect(connection):
            calls.append(connection)
            if len(calls) == 1:
                raise ConnectionResetError('synthetic setup failure')
            return real_connect(connection)

        body = b'{"prompt":"must-not-appear-in-diagnostics"}'
        with patch.object(cr, 'connect_upstream', side_effect=flaky_connect), \
                patch.object(cr.time, 'sleep', return_value=None):
            status, response_body, _ = self.raw_request(
                'POST', '/codex/images/generations', body,
                {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(response_body, b'\x89PNG\r\n\x1a\nsynthetic-image')
        self.assertEqual(len(calls), 2)
        event, raw_event = self.latest_route_event()
        self.assertEqual(event['route'], 'official-passthrough')
        self.assertEqual(event['path'], '/images/generations')
        self.assertEqual(event['upstream_connect_attempts'], 2)
        self.assertIsNone(event['failure_stage'])
        self.assertIsNone(event['error'])
        self.assertEqual(event['connect_retry_error'], 'ConnectionResetError')
        self.assertEqual(event['body_bytes'], len(body))
        self.assertEqual(event['request_json_keys'], ['prompt'])
        self.assertEqual(event['prompt_chars'], len('must-not-appear-in-diagnostics'))
        self.assertNotIn('must-not-appear-in-diagnostics', raw_event)
        self.assertNotIn('official-chatgpt-token', raw_event)
        self.assertNotIn('official-account', raw_event)

    def test_auxiliary_send_failure_is_not_replayed(self):
        fake = MagicMock()
        fake.request.side_effect = ConnectionResetError('synthetic send failure')
        body = b'{"prompt":"synthetic"}'
        with patch.object(cr, 'official_connection', return_value=fake) as factory:
            status, _, _ = self.raw_request(
                'POST', '/codex/images/generations', body,
                {'Content-Type': 'application/json'})
        self.assertEqual(status, 502)
        factory.assert_called_once()
        fake.request.assert_called_once()
        event, _ = self.latest_route_event()
        self.assertEqual(event['failure_stage'], 'send')
        self.assertEqual(event['error'], 'ConnectionResetError')
        self.assertEqual(event['body_bytes'], len(body))

    def test_auxiliary_header_failure_is_not_replayed(self):
        fake = MagicMock()
        fake.getresponse.side_effect = ConnectionResetError('synthetic header failure')
        with patch.object(cr, 'official_connection', return_value=fake) as factory:
            status, _, _ = self.raw_request(
                'POST', '/codex/images/generations', b'{"prompt":"synthetic"}',
                {'Content-Type': 'application/json'})
        self.assertEqual(status, 502)
        factory.assert_called_once()
        fake.request.assert_called_once()
        event, _ = self.latest_route_event()
        self.assertEqual(event['failure_stage'], 'headers')
        self.assertEqual(event['error'], 'ConnectionResetError')

    def test_connection_scoped_headers_are_not_forwarded(self):
        self.raw_request('GET', '/codex/files', headers={
            'Connection': 'keep-alive, X-Private-Hop', 'X-Private-Hop': 'remove-me'})
        self.assertNotIn('x-private-hop', Upstream.seen[-1][1])

    def test_websocket_upgrade_and_frames_are_relayed(self):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5) as sock:
            sock.sendall(b'GET /codex/responses?model=test HTTP/1.1\r\nHost: localhost\r\n'
                         b'X-Unified-Codex-Key: local-only-secret\r\n'
                         b'Authorization: Bearer official-chatgpt-token\r\n'
                         b'Connection: Upgrade\r\nUpgrade: websocket\r\n'
                         b'Sec-WebSocket-Key: test-key\r\nSec-WebSocket-Version: 13\r\n\r\n')
            with sock.makefile('rb') as stream:
                self.assertIn(b'101', stream.readline())
                while stream.readline() != b'\r\n':
                    pass
                self.assertEqual(stream.read(4), b'\x81\x02ok')
                masked = b'\x81\x82mask' + bytes([ord('h') ^ ord('m'), ord('i') ^ ord('a')])
                sock.sendall(masked)
                self.assertEqual(stream.read(4), b'\x81\x02hi')
        self.assertEqual(Upstream.seen[-1][2], masked)

    def test_shared_gateway_dispatches_auxiliary_methods(self):
        import unified_gateway as ug
        with patch.object(cr, 'Router', return_value=self.router):
            server = ug.Server(('127.0.0.1', 0), ug.make_handler({'PublicPort': 0, 'MixedPort': 1}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for method in ('HEAD', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
                with self.subTest(method=method):
                    self.assertEqual(self.raw_request(method, '/codex/files',
                                                     port=server.server_address[1])[0], 200)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
