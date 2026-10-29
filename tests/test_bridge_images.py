import sys
from pathlib import Path
import unittest
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from responses_bridge import build_chat_request, stream_events


class BridgeImageTests(unittest.TestCase):
    def test_string_input_remains_one_complete_message(self):
        result = build_chat_request({'input': '请保留完整问题，而不是逐字拆分。'})
        self.assertEqual(result['messages'], [{'role': 'user', 'content': '请保留完整问题，而不是逐字拆分。'}])

    def test_reasoning_tools_and_structured_output_are_preserved(self):
        result = build_chat_request({'input': 'test', 'reasoning': {'effort': 'high'},
            'parallel_tool_calls': False,
            'tools': [{'type': 'function', 'name': 'test', 'strict': True,
                       'parameters': {'type': 'object', 'properties': {}}}],
            'text': {'format': {'type': 'json_schema', 'name': 'answer', 'strict': True,
                                'schema': {'type': 'object', 'properties': {}}}}})
        self.assertEqual(result['reasoning_effort'], 'high')
        self.assertFalse(result['parallel_tool_calls'])
        self.assertTrue(result['tools'][0]['function']['strict'])
        self.assertTrue(result['response_format']['json_schema']['strict'])

    def test_truncated_tool_arguments_never_become_completed_tool_calls(self):
        for finish in (None, 'length'):
            with self.subTest(finish=finish):
                chunks = [{'choices': [{'delta': {'tool_calls': [{'index': 0,
                    'id': 'call_test', 'function': {'name': 'test', 'arguments': '{"partial":'}}]},
                    'finish_reason': finish}]}]
                events = list(stream_events(chunks, 'test', None))
                wire = b''.join(events)
                self.assertNotIn(b'event: response.completed\n', wire)
                self.assertNotIn(b'event: response.function_call_arguments.done\n', wire)
                final = json.loads(events[-1].split(b'data: ', 1)[1])
                self.assertEqual(final['type'], 'response.failed' if finish is None else 'response.incomplete')

    def test_complete_stream_preserves_text_and_usage(self):
        chunks = [{'choices': [{'delta': {'content': '完整回答'}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 9, 'completion_tokens': 4, 'total_tokens': 13,
                             'prompt_tokens_details': {'cached_tokens': 3},
                             'completion_tokens_details': {'reasoning_tokens': 2}}}]
        final = json.loads(list(stream_events(chunks, 'test', None))[-1].split(b'data: ', 1)[1])
        self.assertEqual(final['type'], 'response.completed')
        self.assertEqual(final['response']['output'][0]['content'][0]['text'], '完整回答')
        self.assertEqual(final['response']['usage']['output_tokens_details']['reasoning_tokens'], 2)
        self.assertEqual(final['response']['usage']['input_tokens_details']['cached_tokens'], 3)

    def test_image_and_text_order_survive_conversion(self):
        image = 'data:image/png;base64,c3ludGhldGlj'
        result = build_chat_request({'input': [{'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'first'},
            {'type': 'input_image', 'image_url': image, 'detail': 'high'},
            {'type': 'input_text', 'text': 'last'}]}]})
        content = result['messages'][0]['content']
        self.assertEqual([c['type'] for c in content], ['text', 'image_url', 'text'])
        self.assertEqual(content[1]['image_url'], {'url': image, 'detail': 'high'})
        self.assertEqual(content[-1]['text'], 'last')

    def test_image_only_message_without_type_is_preserved(self):
        result = build_chat_request({'input': [{'role': 'user', 'content': [
            {'type': 'input_image', 'image_url': 'https://example.test/image.png'}]}]})
        self.assertEqual(result['messages'][0]['content'][0]['image_url']['url'],
                         'https://example.test/image.png')

    def test_text_only_history_keeps_existing_wire_format(self):
        result = build_chat_request({'input': [{'type': 'message', 'role': 'developer',
            'content': [{'type': 'input_text', 'text': 'one'}, {'type': 'input_text', 'text': 'two'}]}]})
        self.assertEqual(result['messages'], [{'role': 'system', 'content': 'one\ntwo'}])


if __name__ == '__main__':
    unittest.main()
