import concurrent.futures
import http.client
import http.server
import json
from pathlib import Path
import socket
import socketserver
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import sanitize_proxy
import unified_gateway
import mode_monitor


class MockAI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({'path': self.path, 'auth': self.headers.get('Authorization')}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(200)
        if self.path.endswith('/chat/completions') and data.get('stream'):
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            chunk = {'id':'test','choices':[{'index':0,'delta':{'content':'ok'},'finish_reason':None}]}
            self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
            chunk['choices'][0] = {'index':0,'delta':{},'finish_reason':'stop'}
            self.wfile.write(('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode())
        else:
            body = json.dumps(data).encode()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class MockProxy(socketserver.BaseRequestHandler):
    def handle(self):
        first = self.request.recv(8192)
        if first[:1] == b'\x05':
            self.request.sendall(b'\x05\x00')
            return
        while b'\r\n\r\n' not in first:
            first += self.request.recv(8192)
        if first.startswith(b'CONNECT '):
            self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            while True:
                payload = self.request.recv(4096)
                if not payload:
                    return
                self.request.sendall(payload)
        else:
            body = first.split(b'\r\n',1)[0]
            self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ai = http.server.ThreadingHTTPServer(('127.0.0.1',0), MockAI)
        cls.proxy = unified_gateway.Server(('127.0.0.1',0), MockProxy)
        sanitize_proxy.UPSTREAM = f'http://127.0.0.1:{cls.ai.server_port}/upstream'
        cls.gateway = unified_gateway.Server(('127.0.0.1',0), MockProxy)
        cls.port = cls.gateway.server_address[1]
        cls.gateway.RequestHandlerClass = unified_gateway.make_handler({'PublicPort':cls.port,'MixedPort':cls.proxy.server_address[1]})
        for server in (cls.ai, cls.proxy, cls.gateway):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.gateway,cls.proxy,cls.ai):
            server.shutdown()
            server.server_close()

    def request(self, method, path, data=None):
        status, body = self.request_status(method, path, data)
        self.assertEqual(status, 200)
        return body

    def request_status(self, method, path, data=None):
        client = http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        client.request(method,path,body=json.dumps(data).encode() if data is not None else None,
                       headers={'Authorization':'Bearer test-local-only','Content-Type':'application/json'})
        response=client.getresponse()
        body=response.read()
        client.close()
        return response.status, body

    def test_api_auth_and_path_preserved(self):
        result=json.loads(self.request('GET','/v1/models?x=1'))
        self.assertEqual(result,{'path':'/upstream/v1/models?x=1','auth':'Bearer test-local-only'})

    def test_private_backend_recognizes_stable_public_entry(self):
        request = b'GET http://127.0.0.1:8799/v1/models HTTP/1.1\r\nHost: 127.0.0.1:8799\r\n\r\n'
        self.assertEqual(unified_gateway.classify(request, 8799, 18801), 'ai')
        internal = b'GET http://127.0.0.1:18801/__status HTTP/1.1\r\nHost: 127.0.0.1:18801\r\n\r\n'
        self.assertEqual(unified_gateway.classify(internal, 8799, 18801), 'ai')

    def test_absolute_self_api_does_not_proxy_loop(self):
        result=json.loads(self.request('GET',f'http://127.0.0.1:{self.port}/v1/models'))
        self.assertEqual(result['path'],'/upstream/v1/models')

    def test_mapping_and_tool_cleanup_preserved(self):
        result=json.loads(self.request('POST','/v1/messages',{'model':'claude-sonnet-5[1M]','tools':[{'input_schema':{'type':'object','propertyNames':{'type':'string'},'properties':{'a':{'const':'b'}}}}]}))
        self.assertEqual(result['model'],'google/gemini-3.8-flash')
        schema=result['tools'][0]['input_schema']
        self.assertNotIn('propertyNames',schema)
        self.assertEqual(schema['properties']['a'],{'enum':['b']})

    def test_responses_stream_bridge_preserved(self):
        body=self.request('POST','/v1/responses',{'model':'google/gemini-3.8-flash','input':'test','stream':True})
        self.assertIn(b'response.output_text.delta',body)
        self.assertIn(b'response.completed',body)

    def test_official_anthropic_model_dispatches_to_bridge(self):
        import anthropic_codex_bridge as bridge
        calls = {}

        def fake_bridge(handler, data, settings, official_manager=None):
            calls['data'] = data
            calls['official_manager'] = official_manager
            body = json.dumps({'bridged': data['model']}).encode()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(body)))
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.wfile.write(body)
            return True

        with patch.object(bridge, 'handle_messages', side_effect=fake_bridge):
            status, body = self.request_status('POST', '/v1/messages', {
                'model': 'openai/gpt-6.1-sol',
                'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'bridged': 'openai/gpt-6.1-sol'})
        self.assertEqual(calls['data']['messages'][0]['content'], 'hi')

    def test_third_party_anthropic_model_keeps_upstream_path(self):
        result = json.loads(self.request('POST', '/v1/messages', {
            'model': 'openai/gpt-6-luna',
            'messages': [{'role': 'user', 'content': 'hi'}]}))
        self.assertEqual(result['model'], 'openai/gpt-6-luna')

    def test_official_count_tokens_is_answered_locally(self):
        status, body = self.request_status('POST', '/v1/messages/count_tokens', {
            'model': 'openai/gpt-6.1-sol',
            'messages': [{'role': 'user', 'content': 'hello world'}]})
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(body)['input_tokens'], 0)

    def test_official_bridge_can_be_disabled(self):
        settings = dict(sanitize_proxy.PROJECT_SETTINGS,
                        CodexAnthropicBridgeEnabled=False)
        with patch.object(sanitize_proxy, 'PROJECT_SETTINGS', settings):
            status, body = self.request_status('POST', '/v1/messages', {
                'model': 'openai/gpt-6.1-sol',
                'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)['error']['type'], 'api_error')

    def test_http_forward_proxy_absolute_target_preserved(self):
        self.assertEqual(self.request('GET','http://example.test/a?b=2'),b'GET http://example.test/a?b=2 HTTP/1.1')

    def test_fragmented_connect_then_opaque_bytes(self):
        with socket.create_connection(('127.0.0.1',self.port),timeout=5) as client:
            client.sendall(b'CON')
            time.sleep(.03)
            client.sendall(b'NECT example.test:443 HTTP/1.1\r\nHost: example.test\r\n\r\n')
            self.assertIn(b'200 Connection Established',client.recv(4096))
            payload=b'\x16\x03\x01opaque-TLS-bytes\0\xff'
            client.sendall(payload)
            self.assertEqual(client.recv(4096),payload)

    def test_socks5_passthrough_on_same_port(self):
        with socket.create_connection(('127.0.0.1',self.port),timeout=5) as client:
            client.sendall(b'\x05\x01\x00')
            self.assertEqual(client.recv(2),b'\x05\x00')

    def test_parallel_api_and_proxy(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            jobs=[pool.submit(self.request,'GET',path) for path in ['/v1/models','http://example.test/a']*4]
            self.assertTrue(all(job.result() for job in jobs))


class ModeSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root/'state').mkdir()
        (root/'state/controller-secret.txt').write_text('test-local-only')
        self.monitor = mode_monitor.ModeMonitor(root, {'ControllerPort':12540,'PublicPort':8799,'MixedPort':12550,'SakuraPort':12450})
        self.current_mode = 'Direct'
        def controller(path, method='GET', body=None):
            if method == 'PATCH':
                self.current_mode = body['mode']
            if path == '/proxies/sakura':
                return {'type':'URLTest','now':'sakura-000','all':['sakura-000']}
            return {'mode':self.current_mode}
        self.monitor.controller = controller
        self.monitor.set_public_proxy = Mock()

    def tearDown(self):
        self.temporary.cleanup()

    def tick(self, enabled, server, available):
        with patch.object(mode_monitor,'read_proxy',return_value=(enabled,server)), patch.object(mode_monitor,'backend_ready',return_value=available):
            self.monitor.tick()
        return self.current_mode

    def test_disabled_proxy_is_reopened_and_sakura_override_is_respected(self):
        self.assertEqual(self.tick(True,'127.0.0.1:12450',True),'Rule')
        self.assertEqual(self.tick(True,'127.0.0.1:8799',True),'Rule')
        self.monitor.set_public_proxy.assert_not_called()
        self.assertEqual(self.tick(False,'127.0.0.1:8799',True),'Rule')
        self.assertEqual(self.tick(False,'127.0.0.1:8799',True),'Rule')
        self.assertGreaterEqual(self.monitor.set_public_proxy.call_count, 2)
        self.assertEqual(self.tick(True,'127.0.0.1:12450',True),'Rule')

    def test_backend_loss_and_return_switch_without_stopping_tun(self):
        self.assertEqual(self.tick(True,'127.0.0.1:12450',True),'Rule')
        self.assertEqual(self.tick(True,'127.0.0.1:8799',False),'Direct')
        self.assertEqual(self.tick(True,'127.0.0.1:8799',True),'Rule')

    def test_boot_without_sakura_and_start_later(self):
        self.assertEqual(self.tick(False,'',False),'Direct')
        self.assertEqual(self.tick(True,'127.0.0.1:12450',True),'Rule')


    def test_embedded_subscription_keeps_rule_without_sakura(self):
        (self.monitor.root/'state/sakura-outbounds.json').write_text(
            json.dumps({'count':2,'outbounds':[{'tag':'sakura-000'},{'tag':'sakura'}],
                        'names':{'sakura-000':'HK test node'}}), encoding='utf-8')
        self.assertEqual(self.tick(False,'',False),'Rule')
        snapshot = json.loads((self.monitor.root/'state/mode.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot['upstream'],'subscription')
        self.assertEqual(snapshot['nodes'],2)
        self.assertEqual(snapshot['node'],'sakura-000')
        self.assertEqual(snapshot['node_name'],'HK test node')


if __name__=='__main__':
    unittest.main()
