import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from responses_bridge import build_chat_request


class BridgeImageTests(unittest.TestCase):
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
