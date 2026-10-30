"""Minimal Anthropic Messages -> official Codex Responses bridge.

Only models listed in ``CodexAnthropicOfficialModels`` are handled here.
Every other model keeps the existing sanitize/relay path in
``sanitize_proxy``. The ChatGPT access token is read from
``$CODEX_HOME/auth.json`` (default ``~/.codex/auth.json``), cached in memory
for at most 60 seconds, and never written to logs, state files, or errors.
"""
import gzip
import http.client
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

import codex_router

try:
    from compression import zstd
except ImportError:  # pragma: no cover - only older Python versions
    zstd = None

DEFAULT_OFFICIAL_MODELS = ('openai/gpt-6.1-sol',)
DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024
OFFICIAL_RESPONSES_PATH = '/backend-api/codex/responses'
SUFFIX_RE = re.compile(r'\[1m\]$', re.IGNORECASE)
CREDENTIAL_SECONDS = 60.0
VERSION_SECONDS = 600.0

_CREDENTIAL_CACHE = {'path': '', 'headers': None, 'expires': 0.0}
_CREDENTIAL_LOCK = threading.Lock()
_VERSION_CACHE = {'value': '', 'expires': 0.0}
_VERSION_LOCK = threading.Lock()


class BridgeError(Exception):
    def __init__(self, status, error_type, message):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


def log(message):
    print('[%s] bridge %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), message), flush=True)


def strip_1m(name):
    return SUFFIX_RE.sub('', name or '')


def bridge_enabled(settings):
    value = (settings or {}).get('CodexAnthropicBridgeEnabled', True)
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'no', 'off', '')
    return bool(value)


def official_models(settings):
    value = (settings or {}).get('CodexAnthropicOfficialModels', DEFAULT_OFFICIAL_MODELS)
    if isinstance(value, str):
        value = [item.strip() for item in value.split(',')]
    if not isinstance(value, (list, tuple)):
        value = DEFAULT_OFFICIAL_MODELS
    result = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(strip_1m(item.strip()))
    return tuple(result)


def is_official_model(model, settings):
    return bool(model) and strip_1m(model) in official_models(settings)


def upstream_model(model):
    name = strip_1m(model)
    return name[len('openai/'):] if name.startswith('openai/') else name


def max_body_bytes(settings):
    try:
        return max(1, int(float((settings or {}).get(
            'CodexAnthropicBridgeMaxBodyBytes', DEFAULT_MAX_BODY_BYTES))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BODY_BYTES


def _float_setting(settings, key, default):
    try:
        return max(1.0, float((settings or {}).get(key, default)))
    except (TypeError, ValueError, OverflowError):
        return float(default)


def auth_path():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'auth.json'


def reset_credentials_cache():
    with _CREDENTIAL_LOCK:
        _CREDENTIAL_CACHE.update(path='', headers=None, expires=0.0)


def invalidate_credentials():
    with _CREDENTIAL_LOCK:
        _CREDENTIAL_CACHE.update(headers=None, expires=0.0)


def credential_headers():
    path = auth_path()
    key = str(path)
    now = time.monotonic()
    with _CREDENTIAL_LOCK:
        if (_CREDENTIAL_CACHE['headers'] and _CREDENTIAL_CACHE['path'] == key
                and now < _CREDENTIAL_CACHE['expires']):
            return dict(_CREDENTIAL_CACHE['headers'])
    try:
        import codex_catalog
        _, headers = codex_catalog.account_credentials(path)
    except Exception:
        raise BridgeError(
            401, 'authentication_error',
            'ChatGPT sign-in is required for official Codex models; run `codex login` and retry.') from None
    if not isinstance(headers, dict) or not headers:
        raise BridgeError(
            401, 'authentication_error',
            'ChatGPT sign-in is required for official Codex models; run `codex login` and retry.')
    with _CREDENTIAL_LOCK:
        _CREDENTIAL_CACHE.update(path=key, headers=dict(headers),
                                 expires=now + CREDENTIAL_SECONDS)
    return dict(headers)


def client_version(settings):
    override = (settings or {}).get('CodexAnthropicBridgeClientVersion')
    if isinstance(override, str) and override.strip():
        return override.strip()
    now = time.monotonic()
    with _VERSION_LOCK:
        if _VERSION_CACHE['value'] and now < _VERSION_CACHE['expires']:
            return _VERSION_CACHE['value']
    try:
        import codex_catalog
        value = codex_catalog.codex_client_version()
    except Exception:
        value = '0.0.0'
    with _VERSION_LOCK:
        _VERSION_CACHE.update(value=value, expires=now + VERSION_SECONDS)
    return value


def request_headers(settings):
    version = client_version(settings)
    headers = dict(credential_headers())
    headers.update({
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'Accept-Encoding': 'identity',
        'User-Agent': 'codex_cli_rs/' + version,
        'originator': 'codex_cli_rs',
        'version': version,
        'OpenAI-Beta': 'responses=experimental',
        'session_id': str(uuid.uuid4()),
    })
    return headers


def inspect_request(body, headers, settings):
    """Return ``(parsed_body, True)`` when the model belongs to the bridge.

    ``(None, False)`` means the caller should keep the existing upstream path
    (non-official models and non-JSON bodies). Broken or unsupported content
    encodings raise ``BridgeError`` before the body reaches any upstream.
    """
    encoding = (headers.get('Content-Encoding') or '').strip().lower()
    if encoding in ('', 'identity'):
        raw = body
    elif encoding == 'gzip':
        try:
            raw = gzip.decompress(body)
        except (OSError, EOFError):
            raise BridgeError(400, 'invalid_request_error',
                              'Invalid gzip-encoded request body') from None
    elif encoding == 'zstd' and zstd is not None:
        try:
            raw = zstd.decompress(body)
        except Exception:
            raise BridgeError(400, 'invalid_request_error',
                              'Invalid zstd-encoded request body') from None
    else:
        raise BridgeError(415, 'invalid_request_error',
                          'Unsupported request content encoding')
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None, False
    if not isinstance(data, dict) or not is_official_model(data.get('model'), settings):
        return None, False
    if len(raw) > max_body_bytes(settings):
        raise BridgeError(
            413, 'invalid_request_error',
            'Request body exceeds the Claude Code official bridge limit')
    return data, True


def system_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and block.get('type') == 'text':
                text = block.get('text')
                if isinstance(text, str):
                    parts.append(text)
        return '\n\n'.join(parts)
    return ''


def tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get('type') == 'text' and isinstance(item.get('text'), str):
                    parts.append(item['text'])
                elif item.get('type') == 'image':
                    parts.append('[image omitted from tool result]')
        return '\n'.join(parts)
    if content is None:
        return ''
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def image_part(block):
    source = block.get('source')
    if not isinstance(source, dict):
        return None
    if source.get('type') == 'base64':
        data = source.get('data')
        media = source.get('media_type') or 'image/png'
        if isinstance(data, str) and data:
            return {'type': 'input_image',
                    'image_url': 'data:%s;base64,%s' % (media, data)}
    if source.get('type') == 'url':
        url = source.get('url')
        if isinstance(url, str) and url:
            return {'type': 'input_image', 'image_url': url}
    return None


def function_call_item(block):
    name = block.get('name')
    if not isinstance(name, str) or not name:
        return None
    call_id = block.get('id')
    if not isinstance(call_id, str) or not call_id:
        call_id = 'toolu_' + uuid.uuid4().hex
    value = block.get('input')
    if not isinstance(value, dict):
        value = {}
    try:
        arguments = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (TypeError, ValueError):
        arguments = '{}'
    return {'type': 'function_call', 'call_id': call_id, 'name': name,
            'arguments': arguments}


def function_call_output_item(block):
    call_id = block.get('tool_use_id')
    if not isinstance(call_id, str) or not call_id:
        return None
    output = tool_result_text(block.get('content'))
    if block.get('is_error'):
        output = '[tool error] ' + output
    return {'type': 'function_call_output', 'call_id': call_id, 'output': output}


def convert_messages(messages):
    items = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = 'assistant' if message.get('role') == 'assistant' else 'user'
        content = message.get('content')
        if isinstance(content, str):
            blocks = [{'type': 'text', 'text': content}]
        elif isinstance(content, list):
            blocks = content
        else:
            blocks = []
        pending = []

        def flush():
            if not pending:
                return
            text = ''.join(pending)
            pending.clear()
            if text:
                items.append({
                    'type': 'message', 'role': role,
                    'content': [{'type': 'output_text' if role == 'assistant'
                                 else 'input_text', 'text': text}]})

        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'text':
                text = block.get('text')
                if isinstance(text, str):
                    pending.append(text)
            elif kind == 'image' and role == 'user':
                flush()
                part = image_part(block)
                if part is not None:
                    items.append({'type': 'message', 'role': 'user',
                                  'content': [part]})
            elif kind == 'tool_use' and role == 'assistant':
                flush()
                item = function_call_item(block)
                if item is not None:
                    items.append(item)
            elif kind == 'tool_result' and role == 'user':
                flush()
                item = function_call_output_item(block)
                if item is not None:
                    items.append(item)
            # thinking / redacted_thinking and unknown block types are dropped.
        flush()
    return items


def convert_tools(tools):
    if not isinstance(tools, list):
        return []
    from sanitize_proxy import clean_schema
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get('name')
        schema = tool.get('input_schema')
        if not isinstance(name, str) or not name or not isinstance(schema, dict):
            continue
        entry = {'type': 'function', 'name': name,
                 'parameters': clean_schema(schema, [0])}
        description = tool.get('description')
        if isinstance(description, str) and description:
            entry['description'] = description
        result.append(entry)
    return result


def convert_tool_choice(choice, tools):
    if not isinstance(choice, dict):
        return None
    kind = choice.get('type')
    if kind == 'auto':
        return 'auto'
    if kind == 'any':
        return 'required'
    if kind == 'none':
        return 'none'
    if kind == 'tool':
        name = choice.get('name')
        if isinstance(name, str) and any(tool.get('name') == name for tool in tools):
            return {'type': 'function', 'name': name}
    return None


def reasoning_effort(data):
    output = data.get('output_config')
    if isinstance(output, dict):
        effort = output.get('effort')
        if isinstance(effort, str):
            value = effort.strip().lower()
            if value in ('minimal', 'low', 'medium', 'high'):
                return value
            if value in ('xhigh', 'max'):
                return 'high'
    thinking = data.get('thinking')
    if isinstance(thinking, dict) and thinking.get('type') == 'enabled':
        budget = thinking.get('budget_tokens')
        if isinstance(budget, (int, float)) and not isinstance(budget, bool):
            if budget <= 2048:
                return 'low'
            if budget <= 8192:
                return 'medium'
            return 'high'
    return None


def build_responses_request(data):
    model = strip_1m(data.get('model') if isinstance(data.get('model'), str) else '')
    if not model:
        raise ValueError('model must be a non-empty string')
    messages = data.get('messages')
    if not isinstance(messages, list) or not messages:
        raise ValueError('messages must be a non-empty list')
    items = convert_messages(messages)
    if not items:
        raise ValueError('messages contain no convertible content')
    request = {
        'model': upstream_model(model),
        'input': items,
        'store': False,
        'stream': True,
    }
    instructions = system_text(data.get('system'))
    if instructions:
        request['instructions'] = instructions
    max_tokens = data.get('max_tokens')
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        request['max_output_tokens'] = max_tokens
    tools = convert_tools(data.get('tools'))
    if tools:
        request['tools'] = tools
        choice = convert_tool_choice(data.get('tool_choice'), tools)
        if choice is not None:
            request['tool_choice'] = choice
        tool_choice = data.get('tool_choice')
        if isinstance(tool_choice, dict) and tool_choice.get('disable_parallel_tool_use'):
            request['parallel_tool_calls'] = False
    effort = reasoning_effort(data)
    if effort:
        request['reasoning'] = {'effort': effort}
    return request


def _text_tokens(text):
    if not isinstance(text, str) or not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def estimate_input_tokens(data):
    total = _text_tokens(system_text(data.get('system')))
    messages = data.get('messages')
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get('content')
            if isinstance(content, str):
                blocks = [{'type': 'text', 'text': content}]
            elif isinstance(content, list):
                blocks = content
            else:
                blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get('type')
                if kind == 'text':
                    total += _text_tokens(block.get('text'))
                elif kind == 'image':
                    total += 1600
                elif kind == 'tool_use':
                    total += _text_tokens(block.get('name'))
                    try:
                        total += _text_tokens(json.dumps(block.get('input'),
                                                         ensure_ascii=False))
                    except (TypeError, ValueError):
                        pass
                elif kind == 'tool_result':
                    total += _text_tokens(tool_result_text(block.get('content')))
    tools = data.get('tools')
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            total += _text_tokens(tool.get('name'))
            total += _text_tokens(tool.get('description'))
            schema = tool.get('input_schema')
            if isinstance(schema, dict):
                try:
                    total += _text_tokens(json.dumps(schema, ensure_ascii=False))
                except (TypeError, ValueError):
                    pass
    return max(1, total)


BEARER_RE = re.compile(r'(Bearer\s+)[A-Za-z0-9._~+/=-]+', re.IGNORECASE)


def _clean_message(message):
    if not isinstance(message, str):
        return ''
    text = BEARER_RE.sub(r'\1[redacted]', ' '.join(message.split()))
    return text[:500]


def _upstream_message(raw):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, UnicodeError):
        return ''
    if not isinstance(data, dict):
        return ''
    error = data.get('error')
    if isinstance(error, dict):
        message = error.get('message')
        if not isinstance(message, str):
            message = error.get('code')
    elif isinstance(error, str):
        message = error
    else:
        message = data.get('message') or data.get('detail')
    return _clean_message(message)


STATUS_ERROR_TYPES = {
    400: 'invalid_request_error',
    401: 'authentication_error',
    403: 'permission_error',
    404: 'not_found_error',
    413: 'invalid_request_error',
    429: 'rate_limit_error',
}


def http_error(status, raw):
    error_type = STATUS_ERROR_TYPES.get(status, 'api_error')
    if status == 401:
        message = 'ChatGPT sign-in expired or was rejected; run `codex login` and retry.'
    elif status == 403:
        message = 'This ChatGPT account is not permitted to use Codex official models.'
    elif status == 429:
        message = 'Codex official rate limit reached; retry later.'
    else:
        message = _upstream_message(raw)
        if not message:
            message = 'Official Codex request failed with HTTP %d' % status
    return BridgeError(status if status < 500 else 502, error_type, message)


def send_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.close_connection = True
    if getattr(handler, 'command', 'POST') != 'HEAD':
        try:
            handler.wfile.write(body)
        except OSError:
            pass
    return True


def send_error(handler, error):
    payload = {'type': 'error',
               'error': {'type': error.error_type, 'message': error.message}}
    return send_json(handler, error.status, payload)


def sse_frame(name, payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return ('event: %s\ndata: %s\n\n' % (name, data)).encode('utf-8')


def write_sse(handler, name, payload):
    handler.wfile.write(sse_frame(name, payload))
    handler.wfile.flush()


def write_stream_error(handler, message, error_type='api_error'):
    text = _clean_message(message) or 'Official Codex request failed'
    try:
        write_sse(handler, 'error',
                  {'type': 'error', 'error': {'type': error_type, 'message': text}})
    except OSError:
        pass


class SSEDecoder:
    """Incremental SSE parser for the official Responses stream."""

    def __init__(self):
        self.buffer = b''
        self.event = None
        self.data = []

    def _line(self, line):
        if line.endswith(b'\r'):
            line = line[:-1]
        if not line:
            if not self.data:
                self.event = None
                return []
            event = (self.event or '', '\n'.join(self.data))
            self.event = None
            self.data = []
            return [event]
        if line.startswith(b':'):
            return []
        field, _, value = line.partition(b':')
        if value.startswith(b' '):
            value = value[1:]
        if field == b'event':
            self.event = value.decode('utf-8', 'replace')
        elif field == b'data':
            self.data.append(value.decode('utf-8', 'replace'))
        return []

    def feed(self, chunk):
        self.buffer += chunk
        events = []
        while True:
            position = self.buffer.find(b'\n')
            if position < 0:
                break
            line = self.buffer[:position]
            self.buffer = self.buffer[position + 1:]
            events.extend(self._line(line))
        return events

    def flush(self):
        events = []
        if self.buffer:
            events.extend(self._line(self.buffer))
            self.buffer = b''
        if self.data:
            events.append((self.event or '', '\n'.join(self.data)))
        self.event = None
        self.data = []
        return events


def _error_type_from_code(code):
    value = (code or '').lower() if isinstance(code, str) else ''
    if 'rate' in value or 'quota' in value:
        return 'rate_limit_error'
    if 'auth' in value:
        return 'authentication_error'
    if 'invalid' in value or 'bad_request' in value:
        return 'invalid_request_error'
    return 'api_error'


class StreamTranslator:
    """Translate official Responses SSE events into Anthropic SSE events."""

    def __init__(self, model, input_tokens=0):
        self.model = model
        self.input_tokens = int(input_tokens or 0)
        self.message_id = 'msg_' + uuid.uuid4().hex
        self.blocks = []
        self.by_key = {}
        self.next_index = 0
        self.started = False
        self.finished = False
        self.stop_reason = None
        self.output_tokens = 0
        self.final_input_tokens = 0
        self.error_message = None

    def _ensure_message(self):
        if self.started:
            return []
        self.started = True
        return [('message_start', {
            'type': 'message_start',
            'message': {
                'id': self.message_id, 'type': 'message', 'role': 'assistant',
                'model': self.model, 'content': [],
                'stop_reason': None, 'stop_sequence': None,
                'usage': {'input_tokens': self.input_tokens, 'output_tokens': 1},
            },
        })]

    def _new_block(self, key, kind):
        block = {'index': self.next_index, 'kind': kind, 'key': key,
                 'closed': False, 'text': '', 'json_parts': [],
                 'saw_delta': False, 'id': '', 'name': ''}
        self.next_index += 1
        self.blocks.append(block)
        self.by_key[key] = block
        return block

    def _start_text(self, key):
        block = self.by_key.get(key)
        if block is not None:
            return [], block
        frames = self._ensure_message()
        block = self._new_block(key, 'text')
        frames.append(('content_block_start', {
            'type': 'content_block_start', 'index': block['index'],
            'content_block': {'type': 'text', 'text': ''},
        }))
        return frames, block

    def _start_tool(self, key, call_id, name):
        block = self.by_key.get(key)
        if block is not None:
            return [], block
        frames = self._ensure_message()
        block = self._new_block(key, 'tool_use')
        block['id'] = call_id if isinstance(call_id, str) and call_id else 'toolu_' + uuid.uuid4().hex
        block['name'] = name if isinstance(name, str) else ''
        frames.append(('content_block_start', {
            'type': 'content_block_start', 'index': block['index'],
            'content_block': {'type': 'tool_use', 'id': block['id'],
                              'name': block['name'], 'input': {}},
        }))
        return frames, block

    def _text_delta(self, key, text):
        if not isinstance(text, str) or not text:
            return []
        frames, block = self._start_text(key)
        if block['closed']:
            return frames
        block['text'] += text
        block['saw_delta'] = True
        frames.append(('content_block_delta', {
            'type': 'content_block_delta', 'index': block['index'],
            'delta': {'type': 'text_delta', 'text': text},
        }))
        return frames

    def _json_delta(self, key, text):
        if not isinstance(text, str) or not text:
            return []
        frames, block = self._start_tool(key, None, None)
        if block['closed']:
            return frames
        block['json_parts'].append(text)
        block['saw_delta'] = True
        frames.append(('content_block_delta', {
            'type': 'content_block_delta', 'index': block['index'],
            'delta': {'type': 'input_json_delta', 'partial_json': text},
        }))
        return frames

    def _close_block(self, block):
        if block is None or block['closed']:
            return []
        block['closed'] = True
        return [('content_block_stop', {'type': 'content_block_stop',
                                        'index': block['index']})]

    def _close_output(self, output_index):
        frames = []
        for block in self.blocks:
            key = block['key']
            if (isinstance(key, tuple) and len(key) == 3
                    and key[0] == 'text' and key[1] == output_index):
                frames.extend(self._close_block(block))
        return frames

    @staticmethod
    def _index(data, item=None):
        value = data.get('output_index')
        if value is None and isinstance(item, dict):
            value = item.get('id')
        if value is None:
            value = data.get('item_id')
        if value is None:
            value = id(data)
        return value

    @staticmethod
    def _message_text(item):
        content = item.get('content')
        if isinstance(content, str):
            return content
        parts = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get('type') in ('output_text', 'text'):
                    text = part.get('text')
                    if isinstance(text, str):
                        parts.append(text)
        return ''.join(parts)

    def _synthesize(self, output):
        frames = []
        if not isinstance(output, list):
            return frames
        for item in output:
            if not isinstance(item, dict):
                continue
            kind = item.get('type')
            if kind == 'message':
                text = self._message_text(item)
                if text:
                    counter = len(self.blocks)
                    frames.extend(self._text_delta(('synth', counter), text))
            elif kind == 'function_call':
                key = ('synth', len(self.blocks))
                frames2, block = self._start_tool(
                    key, item.get('call_id') or item.get('id'), item.get('name'))
                frames.extend(frames2)
                arguments = item.get('arguments')
                if isinstance(arguments, str) and arguments:
                    block['json_parts'].append(arguments)
                    block['saw_delta'] = True
                    frames.append(('content_block_delta', {
                        'type': 'content_block_delta', 'index': block['index'],
                        'delta': {'type': 'input_json_delta',
                                  'partial_json': arguments},
                    }))
        return frames

    def _complete(self, data):
        response = data.get('response')
        if not isinstance(response, dict):
            response = {}
        usage = response.get('usage')
        if isinstance(usage, dict):
            output_tokens = usage.get('output_tokens')
            if isinstance(output_tokens, (int, float)):
                self.output_tokens = int(output_tokens)
            input_tokens = usage.get('input_tokens')
            if isinstance(input_tokens, (int, float)):
                self.final_input_tokens = int(input_tokens)
        incomplete = (data.get('type') == 'response.incomplete'
                      or response.get('status') == 'incomplete')
        frames = []
        if not self.blocks:
            frames.extend(self._synthesize(response.get('output')))
        frames.extend(self._ensure_message())
        for block in self.blocks:
            frames.extend(self._close_block(block))
        if incomplete:
            self.stop_reason = 'max_tokens'
        elif any(block['kind'] == 'tool_use' for block in self.blocks):
            self.stop_reason = 'tool_use'
        else:
            self.stop_reason = 'end_turn'
        frames.append(('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': self.stop_reason, 'stop_sequence': None},
            'usage': {'input_tokens': self.final_input_tokens or self.input_tokens,
                      'output_tokens': self.output_tokens},
        }))
        frames.append(('message_stop', {'type': 'message_stop'}))
        self.finished = True
        return frames

    def _fail(self, message, code=''):
        self.error_message = _clean_message(message) or 'Official Codex request failed'
        self.finished = True
        return [('error', {'type': 'error',
                           'error': {'type': _error_type_from_code(code),
                                     'message': self.error_message}})]

    def handle(self, event, payload):
        if self.finished:
            return []
        if isinstance(payload, dict):
            data = payload
        else:
            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                return []
        if not isinstance(data, dict):
            return []
        event = event or data.get('type') or ''
        handler = getattr(self, '_on_' + event.replace('.', '_'), None)
        if handler is None:
            return []
        try:
            return handler(data)
        except (KeyError, TypeError, ValueError):
            return []

    def _on_response_created(self, data):
        return self._ensure_message()

    def _on_response_output_item_added(self, data):
        item = data.get('item')
        if not isinstance(item, dict):
            return []
        if item.get('type') != 'function_call':
            return []
        index = self._index(data, item)
        frames, _ = self._start_tool(
            ('tool', index), item.get('call_id') or item.get('id'), item.get('name'))
        return frames

    def _on_response_content_part_added(self, data):
        part = data.get('part')
        if not isinstance(part, dict) or part.get('type') not in ('output_text', 'refusal'):
            return []
        index = self._index(data)
        key = ('text', index, data.get('content_index', 0))
        frames, block = self._start_text(key)
        text = part.get('text')
        if isinstance(text, str) and text and not block['saw_delta']:
            frames.extend(self._text_delta(key, text))
        return frames

    def _on_response_output_text_delta(self, data):
        key = ('text', self._index(data), data.get('content_index', 0))
        return self._text_delta(key, data.get('delta'))

    def _on_response_refusal_delta(self, data):
        key = ('text', self._index(data), data.get('content_index', 0))
        return self._text_delta(key, data.get('delta'))

    def _on_response_output_text_done(self, data):
        key = ('text', self._index(data), data.get('content_index', 0))
        block = self.by_key.get(key)
        if block is None or not block['saw_delta']:
            return self._text_delta(key, data.get('text'))
        return []

    def _on_response_refusal_done(self, data):
        return self._on_response_output_text_done(data)

    def _on_response_function_call_arguments_delta(self, data):
        key = ('tool', self._index(data))
        block = self.by_key.get(key)
        if block is None:
            return []
        return self._json_delta(key, data.get('delta'))

    def _on_response_function_call_arguments_done(self, data):
        key = ('tool', self._index(data))
        block = self.by_key.get(key)
        if block is not None and not block['saw_delta']:
            return self._json_delta(key, data.get('arguments'))
        return []

    def _on_response_output_item_done(self, data):
        item = data.get('item')
        if not isinstance(item, dict):
            return []
        kind = item.get('type')
        index = self._index(data, item)
        if kind == 'function_call':
            key = ('tool', index)
            block = self.by_key.get(key)
            frames = []
            if block is None:
                frames, block = self._start_tool(
                    key, item.get('call_id') or item.get('id'), item.get('name'))
            arguments = item.get('arguments')
            if isinstance(arguments, str) and arguments and not block['saw_delta']:
                block['json_parts'].append(arguments)
                block['saw_delta'] = True
                frames.append(('content_block_delta', {
                    'type': 'content_block_delta', 'index': block['index'],
                    'delta': {'type': 'input_json_delta',
                              'partial_json': arguments},
                }))
            frames.extend(self._close_block(block))
            return frames
        if kind == 'message':
            frames = []
            has_text = any(isinstance(block['key'], tuple) and len(block['key']) == 3
                           and block['key'][0] == 'text' and block['key'][1] == index
                           for block in self.blocks)
            if not has_text:
                text = self._message_text(item)
                if text:
                    frames.extend(self._text_delta(('text', index, 0), text))
            frames.extend(self._close_output(index))
            return frames
        return []

    def _on_response_completed(self, data):
        return self._complete(data)

    def _on_response_incomplete(self, data):
        return self._complete(data)

    def _on_response_failed(self, data):
        response = data.get('response')
        error = response.get('error') if isinstance(response, dict) else None
        message, code = '', ''
        if isinstance(error, dict):
            message = error.get('message') or ''
            code = error.get('code') or ''
        elif isinstance(error, str):
            message = error
        return self._fail(message, code)

    def _on_error(self, data):
        error = data.get('error')
        message, code = '', ''
        if isinstance(error, dict):
            message = error.get('message') or ''
            code = error.get('code') or ''
        elif isinstance(error, str):
            message = error
        else:
            message = data.get('message') or ''
            code = data.get('code') or ''
        return self._fail(message, code)

    def finish(self):
        if self.finished:
            return []
        if self.started:
            message = 'Official Codex stream ended unexpectedly'
        else:
            message = 'Official Codex stream ended before a response was received'
        return self._fail(message)

    def message(self):
        content = []
        for block in self.blocks:
            if block['kind'] == 'text':
                if block['text']:
                    content.append({'type': 'text', 'text': block['text']})
                continue
            arguments = ''.join(block['json_parts'])
            try:
                value = json.loads(arguments) if arguments else {}
            except (ValueError, TypeError):
                value = {}
            if not isinstance(value, dict):
                value = {'value': value}
            content.append({'type': 'tool_use', 'id': block['id'],
                            'name': block['name'], 'input': value})
        if not content:
            content = [{'type': 'text', 'text': ''}]
        stop_reason = self.stop_reason
        if stop_reason not in ('end_turn', 'tool_use', 'max_tokens',
                               'stop_sequence', 'pause_turn', 'refusal'):
            stop_reason = ('tool_use' if any(block['kind'] == 'tool_use'
                                             for block in self.blocks)
                           else 'end_turn')
        return {
            'id': self.message_id, 'type': 'message', 'role': 'assistant',
            'model': self.model, 'content': content,
            'stop_reason': stop_reason, 'stop_sequence': None,
            'usage': {'input_tokens': self.final_input_tokens or self.input_tokens,
                      'output_tokens': self.output_tokens},
        }


def _open_connection(settings):
    headers_timeout = _float_setting(settings, 'CodexOfficialHeadersTimeoutSeconds', 360)
    last_error = None
    for attempt in range(3):
        connection = codex_router.official_connection()
        connection.headers_timeout = headers_timeout
        try:
            codex_router.connect_upstream(connection)
            return connection
        except (OSError, http.client.HTTPException) as error:
            last_error = error
            try:
                codex_router.mark_official_lane_failed(connection)
            except Exception:
                pass
            try:
                connection.close()
            except OSError:
                pass
            if attempt < 2:
                time.sleep(.15)
    raise last_error or OSError('Official Codex connection unavailable')


def _send_stream_headers(handler):
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('Connection', 'close')
    handler.send_header('X-Local-Gateway-Bridge', 'codex-official')
    handler.end_headers()
    handler.close_connection = True


def _set_socket_timeout(connection, response, seconds):
    sock = codex_router._socket_for(connection, response)
    if sock is not None:
        try:
            sock.settimeout(seconds)
        except OSError:
            pass


def handle_messages(handler, data, settings):
    if not bridge_enabled(settings):
        return send_error(handler, BridgeError(
            503, 'api_error', 'The Claude Code official Codex bridge is disabled'))
    model = strip_1m(data.get('model') if isinstance(data.get('model'), str) else '')
    try:
        payload = build_responses_request(data)
    except ValueError as error:
        return send_error(handler, BridgeError(400, 'invalid_request_error', str(error)))
    stream = bool(data.get('stream'))
    started_at = time.monotonic()
    connection = None
    status = None
    total = 0
    route_lane = None
    stream_started = False
    translator = StreamTranslator(model, estimate_input_tokens(data))
    try:
        headers = request_headers(settings)
        connection = _open_connection(settings)
        route_lane = getattr(connection, 'route_lane', None)
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')
        budget = codex_router.upload_limit_seconds(len(body), settings, official=True)
        stall = _float_setting(settings, 'CodexOfficialUploadStallSeconds', 15)
        codex_router.send_with_budget(
            connection, 'POST', OFFICIAL_RESPONSES_PATH, body, headers, budget, stall)
        _set_socket_timeout(connection, None,
                            _float_setting(settings, 'CodexOfficialHeadersTimeoutSeconds', 360))
        response = connection.getresponse()
        status = response.status
        if status != 200:
            raw = response.read(65536)
            if status == 401:
                invalidate_credentials()
            return send_error(handler, http_error(status, raw))
        encoding = (response.getheader('Content-Encoding') or '').strip().lower()
        if encoding not in ('', 'identity'):
            return send_error(handler, BridgeError(
                502, 'api_error',
                'Official Codex returned an unsupported content encoding'))
        if stream:
            _send_stream_headers(handler)
            stream_started = True
        _set_socket_timeout(
            connection, response,
            _float_setting(settings, 'CodexOfficialStreamIdleSeconds', 180))
        decoder = SSEDecoder()
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            for event, raw in decoder.feed(chunk):
                for name, item in translator.handle(event, raw):
                    if stream:
                        write_sse(handler, name, item)
            if translator.finished:
                break
        if not translator.finished:
            for event, raw in decoder.flush():
                for name, item in translator.handle(event, raw):
                    if stream:
                        write_sse(handler, name, item)
            for name, item in translator.finish():
                if stream:
                    write_sse(handler, name, item)
        if not stream:
            if translator.error_message:
                return send_error(handler, BridgeError(
                    502, 'api_error', translator.error_message))
            return send_json(handler, 200, translator.message())
    except BridgeError as error:
        if stream_started:
            write_stream_error(handler, error.message, error.error_type)
        else:
            send_error(handler, error)
    except codex_router.UploadTimeout:
        error = BridgeError(504, 'api_error',
                            'Official Codex upload exceeded its gateway time budget')
        if stream_started:
            write_stream_error(handler, error.message, error.error_type)
        else:
            send_error(handler, error)
    except (OSError, http.client.HTTPException):
        message = ('Official Codex connection failed' if status is None
                   else 'Official Codex stream interrupted')
        error = BridgeError(502, 'api_error', message)
        if stream_started:
            write_stream_error(handler, error.message, error.error_type)
        else:
            send_error(handler, error)
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        log('model=%s stream=%s status=%s bytes=%d seconds=%.2f lane=%s'
            % (model, stream, status, total, time.monotonic() - started_at,
               route_lane or '-'))
    return True


def handle_count_tokens(handler, data, settings):
    if not bridge_enabled(settings):
        return send_error(handler, BridgeError(
            503, 'api_error', 'The Claude Code official Codex bridge is disabled'))
    return send_json(handler, 200, {'input_tokens': estimate_input_tokens(data)})
