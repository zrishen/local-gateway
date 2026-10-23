import http.client
import http.server
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import sanitize_proxy as proxy
import upstream_transport as transport


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.event_path = Path(self.temp.name) / 'events.jsonl'
        self.patches = [patch.object(transport, 'EVENT_PATH', self.event_path),
                        patch.object(transport.time, 'sleep')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def connection(self, status=200):
        conn = Mock()
        conn.getresponse.return_value.status = status
        return conn

    def request(self, factory):
        return transport.request_upstream(factory, 'POST', '/v1/responses',
                                          b'private-prompt', {'Authorization': 'secret-key'})

    def test_connect_timeout_retries_then_recovers(self):
        failed, success = self.connection(), self.connection()
        failed.connect.side_effect = TimeoutError('private hostname and address')
        factory = Mock(side_effect=[failed, success])
        conn, response, request_id, attempts = self.request(factory)
        self.assertIs(conn, success)
        self.assertEqual(attempts, 2)
        failed.request.assert_not_called()
        failed.close.assert_called_once()
        success.sock.settimeout.assert_called_with(600)
        events = self.event_path.read_text()
        for private in ('private hostname', 'secret-key', 'private-prompt', '/v1/responses'):
            self.assertNotIn(private, events)
        self.assertEqual(json.loads(events.splitlines()[0])['phase'], 'connect')

    def test_persistent_connect_failure_is_bounded_and_structured(self):
        conns = [self.connection(), self.connection()]
        for conn in conns:
            conn.connect.side_effect = socket.gaierror('private DNS details')
        factory = Mock(side_effect=conns)
        with self.assertRaises(transport.UpstreamUnavailable) as caught:
            self.request(factory)
        self.assertEqual(factory.call_count, 2)
        error = caught.exception.payload()['error']
        self.assertEqual(error['phase'], 'connect')
        self.assertEqual(error['cause'], 'gaierror')
        self.assertEqual(error['attempts'], 2)
        self.assertNotIn('private', json.dumps(error))

    def test_no_replay_after_send_or_wait_failure(self):
        for phase in ('request', 'getresponse'):
            with self.subTest(phase=phase):
                conn = self.connection()
                getattr(conn, phase).side_effect = ConnectionResetError('secret details')
                factory = Mock(return_value=conn)
                with self.assertRaises(transport.UpstreamUnavailable):
                    self.request(factory)
                self.assertEqual(factory.call_count, 1)

    def test_certificate_failure_not_retried(self):
        conn = self.connection()
        conn.connect.side_effect = ssl.SSLCertVerificationError('private host')
        factory = Mock(return_value=conn)
        with self.assertRaises(transport.UpstreamUnavailable):
            self.request(factory)
        self.assertEqual(factory.call_count, 1)

    def test_http_failure_retries_once_but_auth_and_rate_limits_do_not(self):
        for status in (400, 401, 403, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                first, last = self.connection(status), self.connection(200)
                factory = Mock(side_effect=[first, last])
                conn, response, _, attempts = self.request(factory)
                self.assertEqual(attempts, 2 if status in (502, 503, 504) else 1)
                self.assertEqual(response.status, 200 if attempts == 2 else status)
                if attempts == 2:
                    first.getresponse.return_value.close.assert_called_once()

    def test_successful_stream_never_replayed(self):
        conn = self.connection()
        conn.getresponse.return_value.read1.side_effect = ConnectionResetError()
        factory = Mock(return_value=conn)
        _, response, _, _ = self.request(factory)
        with self.assertRaises(ConnectionResetError):
            response.read1(8192)
        self.assertEqual(factory.call_count, 1)

    def test_explicit_dns_override_bypasses_all_resolver_answers(self):
        for scheme in ('http', 'https'):
            for answers in ([], [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.9', 443))]):
                with self.subTest(scheme=scheme, answers=answers), \
                        patch.object(proxy, 'UPSTREAM_DNS_OVERRIDES', {'api.example.test': '192.0.2.8'}), \
                        patch.object(socket, 'getaddrinfo', return_value=answers) as resolver:
                    with patch('socket.create_connection', return_value=Mock()) as connect:
                        # The HTTPConnection constructor retains the factory, so
                        # construct it inside the patch for a real socket-free test.
                        conn = proxy.create_upstream_connection(urllib.parse.urlsplit(scheme + '://api.example.test'))
                        conn._create_connection(('api.example.test', 443), 12)
                        self.assertEqual(connect.call_args.args[0], ('192.0.2.8', 443))
                    self.assertEqual(conn.host, 'api.example.test')
                    resolver.assert_not_called()


class Upstream(http.server.BaseHTTPRequestHandler):
    statuses = []
    seen = 0
    error_body = b''

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        type(self).seen += 1
        status = self.statuses.pop(0)
        self.send_response(status)
        if status == 200:
            body = b'data: {"ok":true}\n\ndata: [DONE]\n\n'
            self.send_header('Content-Type', 'text/event-stream')
        else:
            body = self.error_body
        # Closing-delimited errors cover empty bodies without Content-Length.
        self.end_headers()
        self.wfile.write(body)


class GatewayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.gateway = http.server.ThreadingHTTPServer(('127.0.0.1', 0), proxy.Handler)
        Upstream.statuses, Upstream.seen, Upstream.error_body = [], 0, b''
        self.patches = [patch.object(proxy, 'UPSTREAM', f'http://127.0.0.1:{self.upstream.server_port}'),
                        patch.object(transport, 'EVENT_PATH', Path(self.temp.name) / 'events'),
                        patch.object(transport.time, 'sleep')]
        for p in self.patches:
            p.start()
        for server in (self.upstream, self.gateway):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.gateway, self.upstream):
            server.shutdown()
            server.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def request(self, model='ds/deepseek-v4.1-flash'):
        client = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=5)
        client.request('POST', '/v1/responses', json.dumps({'model': model, 'input': [], 'stream': True}),
                       {'Content-Type': 'application/json'})
        response = client.getresponse()
        result = response.status, response.read()
        client.close()
        return result

    def test_empty_502_recovers_before_any_client_output(self):
        Upstream.statuses = [502, 200]
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b'"ok":true', body)
        self.assertEqual(Upstream.seen, 2)

    def test_persistent_empty_502_has_json_error(self):
        Upstream.statuses = [502, 502]
        status, body = self.request()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['error']['type'], 'upstream_http_error')
        self.assertEqual(Upstream.seen, 2)

    def test_existing_upstream_error_body_is_preserved(self):
        Upstream.statuses = [502, 502]
        Upstream.error_body = b'{"error":"upstream failure details"}'
        self.assertEqual(self.request(), (502, Upstream.error_body))

    def test_connection_failure_has_json_error(self):
        conn = Mock()
        conn.connect.side_effect = TimeoutError('private address')
        with patch.object(proxy, 'create_upstream_connection', return_value=conn):
            status, body = self.request()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['error']['type'], 'upstream_connection_error')
        self.assertNotIn(b'private address', body)

    def test_bridge_uses_same_bounded_retry_and_json_error(self):
        Upstream.statuses = [502, 502]
        status, body = self.request('google/gemini-3.8-flash')
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)['error']['attempts'], 2)
        self.assertEqual(Upstream.seen, 2)

    def test_interrupted_stream_keeps_partial_output_without_replay(self):
        conn = Mock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheaders.return_value = [('Content-Type', 'text/event-stream')]
        response.read1.side_effect = [b'data: partial\n\n', http.client.IncompleteRead(b'')]
        with patch.object(proxy, 'create_upstream_connection', return_value=conn) as factory:
            status, body = self.request()
        self.assertEqual((status, body), (200, b'data: partial\n\n'))
        self.assertEqual(factory.call_count, 1)
        events = [json.loads(line) for line in (Path(self.temp.name) / 'events').read_text().splitlines()]
        self.assertEqual(events[-1]['phase'], 'stream')
        self.assertFalse(events[-1]['retry'])


if __name__ == '__main__':
    unittest.main()
