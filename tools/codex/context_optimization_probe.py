"""Compare synthetic fresh-chat requests without retaining prompts or credentials."""
import argparse
import base64
import http.client
import http.server
import io
import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from probe import Rpc, ROOT
sys.path.insert(0, str(ROOT.parents[1] / 'gateway'))
from codex_router import read_body, Router


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--search-only', action='store_true')
    parser.add_argument('--model', default='ds/deepseek-v4.1-flash')
    parser.add_argument('--all-checks', action='store_true')
    parser.add_argument('--staging-router', action='store_true')
    parser.add_argument('--deferred-only', action='store_true')
    args = parser.parse_args()
    args.catalog = str(Path(args.catalog).resolve())
    metrics = []
    router = Router(catalog_path=args.catalog, event_path=ROOT / 'context-staging-routes.jsonl')

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = read_body(self)
            data = json.loads(body)
            metrics.append({'model': data.get('model'), 'body_bytes': len(body),
                            'instructions_chars': len(data.get('instructions', '')),
                            'input_chars': len(json.dumps(data.get('input'), ensure_ascii=False)),
                            'tools_chars': len(json.dumps(data.get('tools', []), ensure_ascii=False)),
                            'tool_count': len(data.get('tools', []))})
            metrics[-1]['input_item_types'] = dict(Counter(i.get('type', 'message') for i in data.get('input', []) if isinstance(i, dict)))
            metrics[-1]['tool_types'] = dict(Counter(t.get('type') for t in data.get('tools', [])))
            if args.staging_router:
                for name in ('Content-Encoding', 'Transfer-Encoding', 'Content-Length'):
                    if name in self.headers:
                        del self.headers[name]
                self.headers['Content-Length'] = str(len(body))
                self.rfile = io.BytesIO(body)
                router.handle(self, 'POST')
                return
            connection = http.client.HTTPConnection('127.0.0.1', 8799, timeout=180)
            try:
                headers = {k: v for k, v in self.headers.items() if k.lower() not in
                           ('host', 'content-length', 'content-encoding', 'transfer-encoding', 'connection')}
                connection.request('POST', self.path, body, headers)
                response = connection.getresponse()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ('transfer-encoding', 'connection'):
                        self.send_header(key, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                connection.close()

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = Rpc([f'model_catalog_json="{Path(args.catalog).as_posix()}"',
                  f'model_providers.unified_local.base_url="http://127.0.0.1:{server.server_port}/codex"'],
                 inject_secret=False, stderr_path=ROOT / f'{args.label}.stderr.log')
    results = []
    model = args.model
    try:
        params = {'model': model, 'ephemeral': True, 'cwd': str(ROOT),
                  'approvalPolicy': 'never', 'sandbox': 'read-only'}
        if args.deferred_only:
            params['dynamicTools'] = [{'type': 'namespace', 'name': 'local_probe', 'description': 'Local synthetic test only.', 'tools': [
                {'type': 'function', 'name': 'gateway_echo', 'description': 'Return a fixed local test marker.',
                 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'deferLoading': True}]}]
        thread = client.call('thread/start', params)['thread']['id']
        fixture = ROOT / 'context-optimization-fixture.txt'
        fixture.write_text('fixture-nimbus-583091', encoding='utf-8')
        tasks = [('fresh', [{'type': 'text', 'text': 'Reply exactly: ready. Do not use tools.'}])]
        if args.search_only:
            tasks = [('discovery', [{'type': 'text', 'text': 'Use the local tool discovery/search tool to find GitHub tools for reading repository metadata. Reply with the name of one discovered tool. Do not invoke any discovered GitHub tool, access remote data, or call any other tool.'}])]
        if args.full or args.all_checks:
            tasks += [('shell', [{'type': 'text', 'text': f'Use the built-in shell to read only {fixture}. Reply with its exact contents. Do not access any other files, apps, or network.'}])]
            modalities = next(m for m in client.call('model/list', {'limit': 100})['data'] if m['id'] == model)['inputModalities']
            if 'image' in modalities:
                tasks += [('image', [{'type': 'text', 'text': 'Name the LEFT half and RIGHT half colors in this image, in that order. Answer only two color names. Do not use tools.'},
                                 {'type': 'image', 'url': 'data:image/png;base64,' + base64.b64encode((Path(__file__).parent / 'fixtures/vision.png').read_bytes()).decode()}])]
        if args.all_checks:
            tasks += [('discovery', [{'type': 'text', 'text': 'Use the local tool discovery/search tool to find GitHub tools for reading repository metadata. Reply with the name of one discovered tool. Do not invoke any discovered GitHub tool, access remote data, or call any other tool.'}])]
        if args.deferred_only:
            tasks = [('deferred_execution', [{'type': 'text', 'text': 'Use local tool metadata search with query "gateway echo fixed local test marker" and limit 5 to find local_probe.gateway_echo, then call this discovered tool once with no arguments. If empty, retry query "local_probe". Reply with its exact returned text. Do not call any other tool or access files, apps, or network.'}])]
        for name, inputs in tasks:
            client.events.clear()
            start = time.monotonic()
            turn = client.call('turn/start', {'threadId': thread, 'model': model, 'effort': 'high', 'input': inputs})['turn']['id']
            events = list(client.events)
            deadline = start + 180
            while time.monotonic() < deadline:
                message = client.receive(max(1, deadline - time.monotonic()))
                events.append(message)
                if message.get('method') == 'turn/completed' and message['params']['turn']['id'] == turn:
                    outcome = message['params']['turn']
                    items = [e['params']['item'] for e in events if e.get('method') == 'item/completed']
                    counts = [e['params'].get('tokenUsage') for e in events if e.get('method') == 'thread/tokenUsage/updated']
                    result = {'test': name, 'status': outcome['status'], 'error': outcome.get('error'),
                              'answer': '\n'.join(i.get('text', '') for i in items if i.get('type') == 'agentMessage'),
                              'item_types': dict(Counter(i.get('type') for i in items)),
                              'dynamic_calls': [{'namespace': e['params'].get('namespace'), 'tool': e['params'].get('tool')} for e in events if e.get('method') == 'item/tool/call'],
                              'usage': counts[-1] if counts else None, 'seconds': round(time.monotonic() - start, 2)}
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    break
            else:
                raise TimeoutError(name)
            if results[-1]['status'] != 'completed':
                break
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        report = {'label': args.label, 'results': results, 'requests': metrics}
        (ROOT / f'context-{args.label}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'label': args.label, 'requests': metrics}), flush=True)


if __name__ == '__main__':
    main()
