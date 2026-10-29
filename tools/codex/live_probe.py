"""Real same-thread official/API/official switching with synthetic prompts only."""
import argparse
import json
from pathlib import Path
import time
from probe import Rpc, ROOT


def turn(client, thread, model, prompt):
    start = time.monotonic()
    client.events.clear()
    result = client.call('turn/start', {'threadId': thread, 'model': model, 'effort': 'high',
                                      'input': [{'type': 'text', 'text': prompt}]})
    turn_id = result['turn']['id']
    events = list(client.events)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        message = client.receive(max(1, deadline - time.monotonic()))
        events.append(message)
        if message.get('method') == 'turn/completed' and message['params']['turn']['id'] == turn_id:
            result = message['params']['turn']
            texts = [e['params']['item'].get('text', '') for e in events
                     if e.get('method') == 'item/completed'
                     and e.get('params', {}).get('item', {}).get('type') == 'agentMessage']
            tools = [e.get('params', {}).get('tool') for e in events if e.get('method') == 'item/tool/call']
            safe = {'model': model, 'status': result['status'], 'output': '\n'.join(texts),
                    'tools': tools, 'seconds': round(time.monotonic() - start, 2)}
            if result.get('error'):
                safe['error'] = result['error']
            print(json.dumps(safe, ensure_ascii=False), flush=True)
            return safe
    raise TimeoutError('Inference turn timeout')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18799)
    parser.add_argument('--no-tools', action='store_true')
    parser.add_argument('--env-file', action='store_true')
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--shell-test', action='store_true')
    args = parser.parse_args()
    overrides = [f'openai_base_url="http://127.0.0.1:{args.port}/codex"',
                 'model_catalog_json="C:/code/local-gateway/state/codex-models.generated.json"',
                 'model_providers.openai.env_http_headers={"X-Unified-Codex-Key"="UNIFIED_CODEX_GATEWAY_KEY"}',
                 'features.apps=false']
    # Built-in provider IDs are reserved; a dedicated provider is used if the
    # deployed build rejects an attempted auth-header override.
    overrides = [x for x in overrides if not x.startswith('model_providers.openai.')]
    overrides += ['model_provider="unified_probe"',
                  f'model_providers.unified_probe.base_url="http://127.0.0.1:{args.port}/codex"',
                  'model_providers.unified_probe.name="Unified local probe"',
                  'model_providers.unified_probe.wire_api="responses"',
                  'model_providers.unified_probe.requires_openai_auth=true',
                  'model_providers.unified_probe.supports_websockets=false',
                  'model_providers.unified_probe.env_http_headers={"X-Unified-Codex-Key"="UNIFIED_CODEX_GATEWAY_KEY"}']
    if args.installed:
        overrides = []
    client = Rpc(overrides, inject_secret=not args.env_file)
    results = []
    try:
        models = client.call('model/list', {'limit': 100})
        print(json.dumps({'visible_models': [m['id'] for m in models['data']]}, ensure_ascii=False), flush=True)
        params = {'model': 'gpt-6.1-sol', 'cwd': str(ROOT), 'ephemeral': True,
                  'approvalPolicy': 'never', 'sandbox': 'read-only',
                  'baseInstructions': 'You are a local gateway connection test. Follow the requested output exactly. '
                                      'Only use gateway_echo when requested. Do not access files, apps, or the network.',
                  'developerInstructions': 'All prompts are synthetic. Never use another tool.'}
        if not args.no_tools:
            params['dynamicTools'] = [{'name': 'gateway_echo', 'description': 'Return a fixed connection-test marker.',
                                       'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]
        result = client.call('thread/start', params)
        thread = result['thread']['id']
        if args.shell_test:
            fixture = ROOT / 'fixture.txt'
            fixture.write_text('builtin-shell-ok-7291', encoding='utf-8')
            params.pop('dynamicTools', None)
            params['model'] = 'ds/deepseek-v4.1-flash'
            params['baseInstructions'] = 'You are a connection test. Use the built-in shell tool only to read the exact fixture path supplied. Do not access any other files or network. Reply with its contents.'
            params['developerInstructions'] = 'Use Get-Content -LiteralPath on the supplied fixture. Do not use another tool.'
            thread = client.call('thread/start', params)['thread']['id']
            results.append(turn(client, thread, 'ds/deepseek-v4.1-flash',
                                f'Read {fixture} using the built-in shell tool, then reply with exactly its file contents.'))
            (ROOT / 'shell-result.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            return
        prompts = [('gpt-6.1-sol', 'Remember the test number 7291. Reply exactly: official-ok'),
                   ('ds/deepseek-v4.1-flash', 'Reply exactly: api-ok'),
                   ('ds/deepseek-v4.1-flash', 'Use gateway_echo once, then reply with exactly its returned text.'
                    if not args.no_tools else 'Reply with only the test number from our earlier conversation.'),
                   ('gpt-6.1-sol', 'Reply with only the test number from our earlier conversation.')]
        for model, prompt in prompts:
            results.append(turn(client, thread, model, prompt))
            if results[-1]['status'] != 'completed':
                break
        (ROOT / 'live-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        client.close()


if __name__ == '__main__':
    main()
