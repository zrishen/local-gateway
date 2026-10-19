"""Translate client-side tool discovery into ordinary function calls.

Only schemas and wire item types change. Discovery still executes in Codex,
with its original tool authorization; no remote tool is run by this adapter.
"""
import copy
import hashlib
import json

SEARCH = 'codex_local_tool_search'


class ToolSearchCompat:
    def __init__(self):
        self.names = {}
        self.search_ids = set()

    def tools(self, tools):
        result = []
        for tool in tools or []:
            if tool.get('type') == 'namespace':
                namespace = tool['name']
                for inner in tool.get('tools', []):
                    if inner.get('type') != 'function':
                        continue
                    item = copy.deepcopy(inner)
                    name = 'ns_' + hashlib.sha256((namespace + '\0' + item['name']).encode()).hexdigest()[:24]
                    self.names[name] = (namespace, item['name'])
                    item['name'] = name
                    item['description'] = f"{namespace}.{inner['name']}: " + item.get('description', '')
                    result.append(item)
            elif tool.get('type') == 'tool_search':
                result.append({'type': 'function', 'name': SEARCH,
                               'description': tool.get('description', 'Find available tools locally.'),
                               'parameters': tool.get('parameters', {'type': 'object', 'properties': {
                                   'query': {'type': 'string'}, 'limit': {'type': 'integer'}}, 'required': ['query']})})
            else:
                result.append(copy.deepcopy(tool))
        return result

    def request(self, data):
        data = copy.deepcopy(data)
        tools = self.tools(data.get('tools', []))
        inputs = data.get('input', [])
        if not isinstance(inputs, list):
            return data
        # Discoveries from previous turns must remain callable after a reload.
        for item in inputs:
            if isinstance(item, dict) and item.get('type') == 'tool_search_output':
                tools.extend(self.tools(item.get('tools', [])))
        reverse = {pair: name for name, pair in self.names.items()}
        converted = []
        for item in inputs:
            if not isinstance(item, dict):
                converted.append(item)
                continue
            kind = item.get('type')
            if kind == 'tool_search_call':
                item = {'type': 'function_call', 'id': item.get('id'),
                        'call_id': item.get('call_id') or item.get('id'), 'name': SEARCH,
                        'arguments': json.dumps(item.get('arguments', {}), ensure_ascii=False)}
            elif kind == 'tool_search_output':
                item = {'type': 'function_call_output', 'call_id': item.get('call_id'),
                        'output': json.dumps({'tools': self.tools(item.get('tools', []))}, ensure_ascii=False)}
            elif kind == 'function_call' and item.get('namespace'):
                pair = (item['namespace'], item.get('name'))
                if pair in reverse:
                    item = dict(item, name=reverse[pair])
                    item.pop('namespace', None)
            converted.append(item)
        data['input'] = converted
        # A search may return an already loaded function; send each schema once.
        unique = {}
        for tool in tools:
            unique[(tool.get('type'), tool.get('name'))] = tool
        data['tools'] = list(unique.values())
        return data

    def item(self, item):
        if item.get('type') != 'function_call':
            return item
        name = item.get('name')
        if name == SEARCH:
            try:
                arguments = json.loads(item.get('arguments') or '{}')
            except ValueError:
                arguments = {'query': '', 'limit': 1}
            return {'type': 'tool_search_call', 'id': item.get('id'),
                    'call_id': item.get('call_id'), 'status': item.get('status', 'completed'),
                    'execution': 'client', 'arguments': arguments}
        if name in self.names:
            namespace, original = self.names[name]
            return dict(item, name=original, namespace=namespace)
        return item

    def event(self, event):
        kind = event.get('type', '')
        item = event.get('item', {})
        if kind == 'response.output_item.added' and item.get('name') == SEARCH:
            self.search_ids.add(item.get('id'))
            # Emit a complete discovery item once its JSON arguments are ready.
            return None
        if kind.startswith('response.function_call_arguments.') and event.get('item_id') in self.search_ids:
            return None
        if kind in ('response.output_item.added', 'response.output_item.done'):
            event['item'] = self.item(item)
        if kind == 'response.completed':
            response = event.get('response', {})
            response['output'] = [self.item(item) for item in response.get('output', [])]
        return event

    def stream(self, lines):
        """Convert SSE frames incrementally, retaining all unrelated events."""
        frame = []
        for line in lines:
            if line.strip():
                frame.append(line)
                continue
            if not frame:
                continue
            raw = b''.join(frame)
            frame = []
            payload = b'\n'.join(line[6:].rstrip(b'\r\n') for line in raw.splitlines(keepends=True)
                                 if line.startswith(b'data: '))
            try:
                event = json.loads(payload)
            except ValueError:
                yield raw + b'\n'
                continue
            event = self.event(event)
            if event is not None:
                if event.get('type') == 'response.output_item.done' and event.get('item', {}).get('type') == 'tool_search_call':
                    added = dict(event, type='response.output_item.added', item=dict(event['item'], status='in_progress'))
                    yield ('event: response.output_item.added\ndata: ' + json.dumps(added, ensure_ascii=False) + '\n\n').encode()
                yield ('event: ' + event.get('type', 'message') + '\ndata: ' +
                       json.dumps(event, ensure_ascii=False) + '\n\n').encode()
        if frame:
            yield b''.join(frame)
