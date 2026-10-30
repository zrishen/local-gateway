import gzip
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import anthropic_codex_bridge as bridge

OFFICIAL = 'openai/gpt-6.1-sol'
SETTINGS = {
    'CodexAnthropicBridgeEnabled': True,
    'CodexAnthropicOfficialModels': [OFFICIAL],
    'CodexAnthropicBridgeMaxBodyBytes': 1024 * 1024,
    'CodexAnthropicBridgeClientVersion': '9.9.9',
    'CodexOfficialHeadersTimeoutSeconds': 30,
    'CodexOfficialStreamIdleSeconds': 30,
    'CodexOfficialUploadStallSeconds': 5,
}


class FakeSock:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def shutdown(self, *args):
        pass


class FakeHandler:
    command = 'POST'

    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.sent_headers[name] = value

    def end_headers(self):
        pass

    @property
    def body(self):
        return self.wfile.getvalue()


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = FakeSock()
        self.route_lane = 'official'
        self.route_node = 'sakura-042'
        self.closed = False
        self.request_args = None
        self.headers_timeout = None

    def connect(self):
        pass

    def close(self):
        self.closed = True

    def request(self, method, target, body=None, headers=None):
        self.request_args = (method, target, body, headers)

    def getresponse(self):
        return self.response


class FakeResponse:
    def __init__(self, status=200, chunks=(), headers=None, body=b''):
        self.status = status
        self.chunks = list(chunks)
        self._headers = dict(headers or {})
        self._body = body
        self._position = 0

    def getheader(self, name, default=None):
        for key, value in self._headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def read1(self, size):
        if self._position >= len(self.chunks):
            return b''
        chunk = self.chunks[self._position]
        self._position += 1
        return chunk


def sse(event, payload):
    return ('event: %s\ndata: %s\n\n'
            % (event, json.dumps(payload, ensure_ascii=False))).encode('utf-8')


def message_request(stream=True, **extra):
    data = {
        'model': OFFICIAL,
        'max_tokens': 1024,
        'system': [{'type': 'text', 'text': 'system prompt',
                    'cache_control': {'type': 'ephemeral'}}],
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}],
        'stream': stream,
    }
    data.update(extra)
    return data


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth_root = Path(self.temp.name)
        self.auth_path = self.auth_root / 'auth.json'
        self.auth_path.write_text(json.dumps({
            'auth_mode': 'chatgpt',
            'tokens': {'access_token': 'secret-token', 'account_id': 'account-1'},
        }), encoding='utf-8')
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.auth_root)})
        self.env.start()
        bridge.reset_credentials_cache()

    def tearDown(self):
        self.env.stop()
        bridge.reset_credentials_cache()
        self.temp.cleanup()


class BuildRequestTests(unittest.TestCase):
    def test_prefix_and_suffix_are_removed_for_official_endpoint(self):
        request = bridge.build_responses_request(
            message_request(model=OFFICIAL + '[1M]'))
        self.assertEqual(request['model'], 'gpt-6.1-sol')
        self.assertFalse(request['store'])
        self.assertTrue(request['stream'])
        self.assertEqual(request['instructions'], 'system prompt')

    def test_messages_tools_and_tool_choice_are_converted(self):
        request = bridge.build_responses_request({
            'model': OFFICIAL,
            'max_tokens': 4096,
            'messages': [
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'read it'},
                    {'type': 'image', 'source': {
                        'type': 'base64', 'media_type': 'image/png', 'data': 'AAA'}},
                ]},
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'ok'},
                    {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read',
                     'input': {'path': 'a'}},
                ]},
                {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_1',
                     'content': [{'type': 'text', 'text': 'file'}]},
                    {'type': 'text', 'text': 'next'},
                ]},
            ],
            'tools': [{
                'name': 'Read', 'description': 'read a file',
                'input_schema': {
                    'type': 'object',
                    'propertyNames': {'type': 'string'},
                    'properties': {'path': {'const': 'a'}},
                },
            }],
            'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': True},
            'thinking': {'type': 'enabled', 'budget_tokens': 16000},
        })
        self.assertEqual(request['max_output_tokens'], 4096)
        self.assertEqual(request['reasoning'], {'effort': 'high'})
        self.assertEqual(request['tool_choice'], 'auto')
        self.assertFalse(request['parallel_tool_calls'])
        self.assertEqual(request['tools'][0]['type'], 'function')
        schema = request['tools'][0]['parameters']
        self.assertNotIn('propertyNames', schema)
        self.assertEqual(schema['properties']['path'], {'enum': ['a']})
        kinds = [item['type'] for item in request['input']]
        self.assertEqual(kinds, ['message', 'message', 'message',
                                 'function_call', 'function_call_output', 'message'])
        self.assertEqual(request['input'][1]['content'][0]['type'], 'input_image')
        self.assertEqual(request['input'][3]['call_id'], 'toolu_1')
        self.assertEqual(request['input'][3]['arguments'], '{"path":"a"}')
        self.assertEqual(request['input'][4]['output'], 'file')
        self.assertEqual(request['input'][5]['content'][0]['text'], 'next')

    def test_tool_result_error_and_image_placeholder(self):
        request = bridge.build_responses_request({
            'model': OFFICIAL,
            'messages': [
                {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': 'toolu_2', 'name': 'View',
                     'input': {}}]},
                {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_2',
                     'is_error': True,
                     'content': [{'type': 'text', 'text': 'nope'},
                                 {'type': 'image', 'source': {
                                     'type': 'base64', 'media_type': 'image/png',
                                     'data': 'AAA'}}]}]},
            ],
        })
        output = request['input'][-1]
        self.assertEqual(output['type'], 'function_call_output')
        self.assertTrue(output['output'].startswith('[tool error] '))
        self.assertIn('[image omitted', output['output'])

    def test_tool_choice_and_reasoning_mapping(self):
        request = bridge.build_responses_request({
            'model': OFFICIAL,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{'name': 'Bash', 'input_schema': {'type': 'object'}}],
            'tool_choice': {'type': 'tool', 'name': 'Bash'},
            'output_config': {'effort': 'high'},
        })
        self.assertEqual(request['tool_choice'],
                         {'type': 'function', 'name': 'Bash'})
        self.assertEqual(request['reasoning'], {'effort': 'high'})

    def test_unknown_tool_choice_is_dropped(self):
        request = bridge.build_responses_request({
            'model': OFFICIAL,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{'name': 'Bash', 'input_schema': {'type': 'object'}}],
            'tool_choice': {'type': 'tool', 'name': 'Missing'},
        })
        self.assertNotIn('tool_choice', request)

    def test_thinking_budget_maps_to_effort(self):
        for budget, effort in ((1024, 'low'), (4096, 'medium'), (32000, 'high')):
            request = bridge.build_responses_request({
                'model': OFFICIAL,
                'messages': [{'role': 'user', 'content': 'hi'}],
                'thinking': {'type': 'enabled', 'budget_tokens': budget},
            })
            self.assertEqual(request['reasoning'], {'effort': effort})

    def test_anthropic_only_fields_are_dropped(self):
        request = bridge.build_responses_request(
            message_request(temperature=0.2, top_p=0.9, top_k=5,
                            metadata={'user_id': 'x'}, stop_sequences=['END']))
        for key in ('temperature', 'top_p', 'top_k', 'metadata', 'stop_sequences'):
            self.assertNotIn(key, request)
        self.assertNotIn('cache_control', json.dumps(request))

    def test_empty_messages_are_rejected(self):
        with self.assertRaises(ValueError):
            bridge.build_responses_request({'model': OFFICIAL, 'messages': []})

    def test_image_url_is_preserved(self):
        request = bridge.build_responses_request({
            'model': OFFICIAL,
            'messages': [{'role': 'user', 'content': [
                {'type': 'image', 'source': {
                    'type': 'url', 'url': 'https://example.com/a.png'}}]}],
        })
        self.assertEqual(request['input'][0]['content'][0]['image_url'],
                         'https://example.com/a.png')


class InspectionTests(unittest.TestCase):
    def test_official_model_is_selected_and_third_party_is_not(self):
        body = json.dumps({'model': OFFICIAL, 'messages': []}).encode()
        data, official = bridge.inspect_request(body, {}, SETTINGS)
        self.assertTrue(official)
        self.assertEqual(data['model'], OFFICIAL)
        body = json.dumps({'model': 'openai/gpt-6-luna', 'messages': []}).encode()
        self.assertEqual(bridge.inspect_request(body, {}, SETTINGS), (None, False))

    def test_gzip_body_is_inspected(self):
        raw = json.dumps({'model': OFFICIAL, 'messages': []}).encode()
        data, official = bridge.inspect_request(
            gzip.compress(raw), {'Content-Encoding': 'gzip'}, SETTINGS)
        self.assertTrue(official)
        self.assertEqual(data['model'], OFFICIAL)

    def test_unsupported_or_broken_encoding_is_rejected(self):
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.inspect_request(b'garbage', {'Content-Encoding': 'br'}, SETTINGS)
        self.assertEqual(caught.exception.status, 415)
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.inspect_request(b'garbage', {'Content-Encoding': 'gzip'}, SETTINGS)
        self.assertEqual(caught.exception.status, 400)

    def test_error_message_redacts_bearer_tokens(self):
        self.assertEqual(bridge._clean_message('bad Bearer abc.def'),
                         'bad Bearer [redacted]')

    def test_body_limit_applies_only_to_official_models(self):
        settings = dict(SETTINGS, CodexAnthropicBridgeMaxBodyBytes=64)
        body = json.dumps({'model': OFFICIAL, 'messages': [
            {'role': 'user', 'content': 'x' * 200}]}).encode()
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.inspect_request(body, {}, settings)
        self.assertEqual(caught.exception.status, 413)
        third_party = json.dumps({'model': 'openai/gpt-6-luna',
                                  'messages': [{'role': 'user', 'content': 'x' * 200}]}).encode()
        self.assertEqual(bridge.inspect_request(third_party, {}, settings),
                         (None, False))

    def test_token_estimate_is_positive(self):
        self.assertGreaterEqual(bridge.estimate_input_tokens(
            message_request()), 1)


class TranslatorTests(unittest.TestCase):
    def feed(self, translator, event, payload):
        return translator.handle(event, json.dumps(payload))

    def test_text_stream(self):
        translator = bridge.StreamTranslator(OFFICIAL, 11)
        events = self.feed(translator, 'response.created', {'type': 'response.created'})
        events += self.feed(translator, 'response.output_text.delta', {
            'output_index': 0, 'content_index': 0, 'delta': 'Hello'})
        events += self.feed(translator, 'response.output_item.done', {
            'output_index': 0,
            'item': {'type': 'message', 'id': 'm1', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'Hello'}]}})
        events += self.feed(translator, 'response.completed', {
            'type': 'response.completed',
            'response': {'status': 'completed',
                         'usage': {'input_tokens': 12, 'output_tokens': 2}}})
        names = [name for name, _ in events]
        self.assertEqual(names[0], 'message_start')
        self.assertIn('content_block_delta', names)
        self.assertEqual(names[-2:], ['message_delta', 'message_stop'])
        message = translator.message()
        self.assertEqual(message['content'], [{'type': 'text', 'text': 'Hello'}])
        self.assertEqual(message['stop_reason'], 'end_turn')
        self.assertEqual(message['usage'], {'input_tokens': 12, 'output_tokens': 2})

    def test_parallel_tool_calls(self):
        translator = bridge.StreamTranslator(OFFICIAL)
        events = []
        events += self.feed(translator, 'response.created', {'type': 'response.created'})
        events += self.feed(translator, 'response.output_item.added', {
            'output_index': 0,
            'item': {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                     'name': 'Read', 'arguments': ''}})
        events += self.feed(translator, 'response.function_call_arguments.delta', {
            'output_index': 0, 'delta': '{"path":"a"}'})
        events += self.feed(translator, 'response.output_item.added', {
            'output_index': 1,
            'item': {'type': 'function_call', 'id': 'fc_2', 'call_id': 'call_2',
                     'name': 'Bash', 'arguments': ''}})
        events += self.feed(translator, 'response.function_call_arguments.delta', {
            'output_index': 1, 'delta': '{"cmd":"ls"}'})
        events += self.feed(translator, 'response.output_item.done', {
            'output_index': 0,
            'item': {'type': 'function_call', 'call_id': 'call_1', 'name': 'Read',
                     'arguments': '{"path":"a"}'}})
        events += self.feed(translator, 'response.output_item.done', {
            'output_index': 1,
            'item': {'type': 'function_call', 'call_id': 'call_2', 'name': 'Bash',
                     'arguments': '{"cmd":"ls"}'}})
        events += self.feed(translator, 'response.completed', {
            'type': 'response.completed',
            'response': {'status': 'completed', 'usage': {'output_tokens': 9}}})
        message = translator.message()
        self.assertEqual(message['stop_reason'], 'tool_use')
        self.assertEqual([block['type'] for block in message['content']],
                         ['tool_use', 'tool_use'])
        self.assertEqual([block['input'] for block in message['content']],
                         [{'path': 'a'}, {'cmd': 'ls'}])
        indices = [payload['index'] for name, payload in events
                   if name == 'content_block_start']
        self.assertEqual(indices, [0, 1])

    def test_full_arguments_in_output_item_without_deltas(self):
        translator = bridge.StreamTranslator(OFFICIAL)
        self.feed(translator, 'response.output_item.added', {
            'output_index': 0,
            'item': {'type': 'function_call', 'call_id': 'call_1',
                     'name': 'Read', 'arguments': ''}})
        events = self.feed(translator, 'response.output_item.done', {
            'output_index': 0,
            'item': {'type': 'function_call', 'call_id': 'call_1',
                     'name': 'Read', 'arguments': '{"path":"b"}'}})
        deltas = [payload['delta']['partial_json'] for name, payload in events
                  if name == 'content_block_delta']
        self.assertEqual(deltas, ['{"path":"b"}'])
        self.assertEqual(translator.message()['content'][0]['input'],
                         {'path': 'b'})

    def test_incomplete_reports_max_tokens(self):
        translator = bridge.StreamTranslator(OFFICIAL)
        self.feed(translator, 'response.created', {'type': 'response.created'})
        self.feed(translator, 'response.output_text.delta', {
            'output_index': 0, 'content_index': 0, 'delta': 'partial'})
        events = self.feed(translator, 'response.incomplete', {
            'type': 'response.incomplete',
            'response': {'status': 'incomplete',
                         'incomplete_details': {'reason': 'max_output_tokens'},
                         'usage': {'output_tokens': 7}}})
        delta = next(payload for name, payload in events if name == 'message_delta')
        self.assertEqual(delta['delta']['stop_reason'], 'max_tokens')
        self.assertEqual(translator.message()['stop_reason'], 'max_tokens')

    def test_failed_reports_anthropic_error(self):
        translator = bridge.StreamTranslator(OFFICIAL)
        events = self.feed(translator, 'response.failed', {
            'type': 'response.failed',
            'response': {'error': {'code': 'rate_limit_exceeded',
                                   'message': 'slow down'}}})
        self.assertEqual(events, [('error', {
            'type': 'error',
            'error': {'type': 'rate_limit_error', 'message': 'slow down'}})])
        self.assertEqual(translator.error_message, 'slow down')

    def test_completed_output_is_synthesized_when_no_deltas_arrive(self):
        translator = bridge.StreamTranslator(OFFICIAL, 4)
        events = self.feed(translator, 'response.completed', {
            'type': 'response.completed',
            'response': {
                'status': 'completed',
                'output': [
                    {'type': 'message', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'done'}]},
                    {'type': 'function_call', 'call_id': 'call_9',
                     'name': 'Read', 'arguments': '{"path":"c"}'},
                ],
                'usage': {'input_tokens': 5, 'output_tokens': 6},
            }})
        names = [name for name, _ in events]
        self.assertEqual(names[0], 'message_start')
        self.assertIn('content_block_start', names)
        self.assertEqual(names[-1], 'message_stop')
        message = translator.message()
        self.assertEqual(message['content'][0], {'type': 'text', 'text': 'done'})
        self.assertEqual(message['content'][1]['input'], {'path': 'c'})
        self.assertEqual(message['stop_reason'], 'tool_use')

    def test_truncated_stream_is_an_error(self):
        translator = bridge.StreamTranslator(OFFICIAL)
        self.feed(translator, 'response.created', {'type': 'response.created'})
        events = translator.finish()
        self.assertEqual(events[0][0], 'error')
        self.assertIsNotNone(translator.error_message)

    def test_sse_decoder_handles_split_chunks_and_crlf(self):
        decoder = bridge.SSEDecoder()
        payload = sse('response.output_text.delta', {'delta': 'x'})
        events = []
        for byte in payload:
            events.extend(decoder.feed(bytes([byte])))
        self.assertEqual(events, [('response.output_text.delta',
                                   json.dumps({'delta': 'x'}))])
        crlf = bridge.SSEDecoder()
        self.assertEqual(crlf.feed(b'event: e\r\ndata: {"a": 1}\r\n\r\n'),
                         [('e', '{"a": 1}')])


class HandlerTests(AuthTestCase):
    def run_bridge(self, request, response, settings=None, official_manager=None,
                   connections=None, connect_side_effect=None):
        handler = FakeHandler()
        connections = list(connections or [FakeConnection(response)])
        sent = {}

        def fake_send(conn, method, target, body, headers, budget, stall):
            sent.update(conn=conn, method=method, target=target, body=body,
                        headers=headers, budget=budget, stall=stall)

        with patch.object(bridge.codex_router, 'official_connection',
                          side_effect=connections), \
                patch.object(bridge.codex_router, 'connect_upstream',
                             side_effect=connect_side_effect), \
                patch.object(bridge.codex_router, 'send_with_budget',
                             side_effect=fake_send):
            result = bridge.handle_messages(
                handler, request, settings or SETTINGS,
                official_manager=official_manager)
        return handler, sent, result

    def streaming_chunks(self):
        return [
            sse('response.created', {'type': 'response.created'}),
            sse('response.output_text.delta', {
                'output_index': 0, 'content_index': 0, 'delta': 'Hi'}),
            sse('response.completed', {
                'type': 'response.completed',
                'response': {'status': 'completed',
                             'usage': {'input_tokens': 3, 'output_tokens': 1}}}),
        ]

    def test_streaming_request_uses_official_route_and_credentials(self):
        handler, sent, result = self.run_bridge(
            message_request(stream=True), FakeResponse(chunks=self.streaming_chunks()))
        self.assertTrue(result)
        self.assertEqual(handler.status, 200)
        self.assertIn('text/event-stream', handler.sent_headers['Content-Type'])
        body = handler.body
        self.assertIn(b'event: message_start', body)
        self.assertIn(b'event: content_block_delta', body)
        self.assertIn(b'event: message_stop', body)
        self.assertEqual(sent['method'], 'POST')
        self.assertEqual(sent['target'], '/backend-api/codex/responses')
        forwarded = json.loads(sent['body'])
        self.assertEqual(forwarded['model'], 'gpt-6.1-sol')
        self.assertTrue(forwarded['stream'])
        self.assertFalse(forwarded['store'])
        self.assertEqual(sent['headers']['Authorization'], 'Bearer secret-token')
        self.assertEqual(sent['headers']['ChatGPT-Account-Id'], 'account-1')
        self.assertEqual(sent['headers']['originator'], 'codex_cli_rs')
        self.assertEqual(sent['headers']['User-Agent'], 'codex_cli_rs/9.9.9')
        self.assertEqual(sent['headers']['version'], '9.9.9')

    def test_non_streaming_request_returns_message_json(self):
        handler, _, _ = self.run_bridge(
            message_request(stream=False), FakeResponse(chunks=self.streaming_chunks()))
        self.assertEqual(handler.status, 200)
        payload = json.loads(handler.body)
        self.assertEqual(payload['type'], 'message')
        self.assertEqual(payload['model'], OFFICIAL)
        self.assertEqual(payload['content'], [{'type': 'text', 'text': 'Hi'}])
        self.assertEqual(payload['stop_reason'], 'end_turn')

    def test_rate_limit_is_mapped_without_leaking_token(self):
        handler, _, _ = self.run_bridge(
            message_request(), FakeResponse(
                status=429, body=b'{"error":{"message":"too many"}}'))
        self.assertEqual(handler.status, 429)
        payload = json.loads(handler.body)
        self.assertEqual(payload['error']['type'], 'rate_limit_error')
        self.assertNotIn(b'secret-token', handler.body)

    def test_connect_failure_rotates_official_node_before_retry(self):
        settings = dict(SETTINGS, CodexOfficialSelector='official')
        first = FakeConnection(FakeResponse())
        second = FakeConnection(FakeResponse(chunks=self.streaming_chunks()))
        manager = Mock()
        manager.failover_after_connect_failure.return_value = {'tag': 'other'}
        with patch.object(bridge.codex_router, 'mark_official_lane_failed') as marked:
            handler, sent, _ = self.run_bridge(
                message_request(), None, settings=settings,
                official_manager=manager, connections=[first, second],
                connect_side_effect=[OSError('reset'), None])
        self.assertEqual(handler.status, 200)
        manager.failover_after_connect_failure.assert_called_once_with('sakura-042')
        marked.assert_not_called()
        self.assertIs(sent['conn'], second)

    def test_all_connect_failures_are_reported_to_official_manager(self):
        settings = dict(SETTINGS, CodexOfficialSelector='official')
        connections = [FakeConnection(FakeResponse()) for _ in range(4)]
        manager = Mock()
        manager.failover_after_connect_failure.return_value = None
        with patch.object(bridge.codex_router, 'mark_official_lane_failed'):
            handler, _, _ = self.run_bridge(
                message_request(), None, settings=settings,
                official_manager=manager, connections=connections,
                connect_side_effect=[OSError('reset')] * 4)
        self.assertEqual(handler.status, 502)
        manager.record_request.assert_called_once()
        kwargs = manager.record_request.call_args.kwargs
        self.assertEqual(kwargs['failure_stage'], 'connect')
        self.assertEqual(kwargs['connect_attempts'], 4)
        self.assertIsNone(kwargs['lane'])

    def test_upstream_401_invalidates_cached_credentials(self):
        bridge.credential_headers()
        handler, _, _ = self.run_bridge(
            message_request(), FakeResponse(
                status=401, body=b'{"error":{"message":"unauthorized"}}'))
        self.assertEqual(handler.status, 401)
        self.assertIsNone(bridge._CREDENTIAL_CACHE['headers'])

    def test_disabled_bridge_returns_503(self):
        handler = FakeHandler()
        settings = dict(SETTINGS, CodexAnthropicBridgeEnabled=False)
        self.assertTrue(bridge.handle_messages(handler, message_request(), settings))
        self.assertEqual(handler.status, 503)
        self.assertEqual(json.loads(handler.body)['error']['type'], 'api_error')

    def test_missing_sign_in_returns_401_without_token(self):
        self.auth_path.unlink()
        bridge.reset_credentials_cache()
        handler = FakeHandler()
        self.assertTrue(bridge.handle_messages(handler, message_request(), SETTINGS))
        self.assertEqual(handler.status, 401)
        self.assertIn(b'codex login', handler.body)
        self.assertNotIn(b'secret-token', handler.body)

    def test_count_tokens_does_not_contact_upstream(self):
        handler = FakeHandler()
        bridge.handle_count_tokens(handler, message_request(), SETTINGS)
        self.assertEqual(handler.status, 200)
        payload = json.loads(handler.body)
        self.assertGreater(payload['input_tokens'], 0)


if __name__ == '__main__':
    unittest.main()
