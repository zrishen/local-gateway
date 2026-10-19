import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from tool_search_compat import ToolSearchCompat, SEARCH


class ToolSearchCompatibilityTests(unittest.TestCase):
    def test_discovery_replay_exposes_only_discovered_tools_and_keeps_identity(self):
        schema = {'type': 'namespace', 'name': 'example', 'tools': [
            {'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}}]}
        original = {'tools': [{'type': 'tool_search', 'parameters': {'type': 'object'}}], 'input': [
            {'type': 'tool_search_call', 'id': 's1', 'call_id': 'c1', 'arguments': {'query': 'read'}},
            {'type': 'tool_search_output', 'call_id': 'c1', 'tools': [schema]},
            {'type': 'function_call', 'call_id': 'c2', 'name': 'read', 'namespace': 'example', 'arguments': '{}'}]}
        compat = ToolSearchCompat()
        request = compat.request(original)
        names = [t['name'] for t in request['tools']]
        self.assertEqual(names[0], SEARCH)
        self.assertEqual(len(names), 2)
        self.assertEqual(request['input'][0]['name'], SEARCH)
        self.assertEqual(request['input'][2]['name'], names[1])
        self.assertNotIn('namespace', request['input'][2])
        self.assertEqual(compat.item(request['input'][2])['namespace'], 'example')
        self.assertEqual(compat.item(request['input'][2])['name'], 'read')
        self.assertEqual(original['input'][0]['type'], 'tool_search_call')

    def test_stream_restores_native_discovery_and_preserves_unrelated_usage(self):
        compat = ToolSearchCompat()
        item = {'type': 'function_call', 'id': 's1', 'call_id': 'c1', 'name': SEARCH, 'arguments': ''}
        done = dict(item, arguments='{"query":"read","limit":2}', status='completed')
        events = [
            {'type': 'response.output_item.added', 'item': item, 'output_index': 0},
            {'type': 'response.function_call_arguments.delta', 'item_id': 's1', 'delta': '{}'},
            {'type': 'response.output_item.done', 'item': done, 'output_index': 0},
            {'type': 'response.completed', 'response': {'output': [done], 'usage': {'input_tokens': 99}}}]
        wire = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
        output = b''.join(compat.stream(io.BytesIO(wire)))
        parsed = [json.loads(line[6:]) for line in output.splitlines() if line.startswith(b'data: ')]
        self.assertEqual([e['type'] for e in parsed], ['response.output_item.added', 'response.output_item.done', 'response.completed'])
        self.assertEqual(parsed[1]['item']['type'], 'tool_search_call')
        self.assertEqual(parsed[1]['item']['arguments'], {'query': 'read', 'limit': 2})
        self.assertEqual(parsed[-1]['response']['usage']['input_tokens'], 99)


if __name__ == '__main__':
    unittest.main()
