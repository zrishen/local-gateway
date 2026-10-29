"""Isolated subscription context test; only synthetic data and safe metrics persist."""
import argparse
import hashlib
import http.client
import http.server
import json
import queue
import secrets
import sys
import threading
import time
from pathlib import Path

from probe import ROOT, Rpc
sys.path.insert(0, str(ROOT.parents[1] / 'gateway'))
from codex_router import read_body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--padding-tokens', type=int, default=780000)
    args = parser.parse_args()
    catalog = json.loads((ROOT.parents[1] / 'state/codex-models.generated.json').read_text(encoding='utf-8'))
    for model in catalog['models']:
        if model['slug'] == 'gpt-6-luna':
            model['context_window'] = 872000
    path = ROOT / 'catalog-official-872k-test.json'
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    start_code, end_code = secrets.token_hex(5), secrets.token_hex(5)
    prompt = ('Synthetic context-capacity test. Read the data below and return the values of '
              'START_CODE and END_CODE separated by one space. Do not use tools.\n'
              f'START_CODE={start_code}\nDATA:' + '中\n' * (args.padding_tokens // 2) +
              f'\nEND_CODE={end_code}\nReturn only the two codes in their original order.')
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = read_body(self)
            data = json.loads(body)
            inputs = json.dumps(data.get('input'), ensure_ascii=False)
            stat = {'path': self.path, 'model': data.get('model'), 'request_bytes': len(body),
                    'contains_start_marker': f'START_CODE={start_code}' in inputs,
                    'contains_end_marker': f'END_CODE={end_code}' in inputs,
                    'padding_lines': inputs.count('中\\n'),
                    'input_sha256': hashlib.sha256(inputs.encode()).hexdigest()}
            requests.append(stat)
            (ROOT / 'official-context-progress.json').write_text(json.dumps(stat, ensure_ascii=False), encoding='utf-8')
            if self.path.endswith('/compact'):
                reply = b'{"error":{"message":"Compaction blocked in synthetic capacity test"}}'
                self.send_response(400)
                self.send_header('Content-Length', str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)
                return
            connection = http.client.HTTPConnection('127.0.0.1', 8799, timeout=180)
            try:
                headers = {k: v for k, v in self.headers.items() if k.lower() not in
                           ('host', 'content-length', 'content-encoding', 'transfer-encoding', 'connection')}
                connection.request('POST', self.path, body, headers)
                response = connection.getresponse()
                stat['http_status'] = response.status
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
    client = Rpc([f'model_catalog_json="{path.as_posix()}"',
                  f'model_providers.unified_local.base_url="http://127.0.0.1:{server.server_port}/codex"'],
                 inject_secret=False, stderr_path=ROOT / 'official-context-probe.stderr.log')
    result = {'model': 'gpt-6-luna', 'configured_window': 872000,
              'padding_repetitions_requested': args.padding_tokens, 'requests': requests}
    try:
        thread = client.call('thread/start', {'model': 'gpt-6-luna', 'ephemeral': True,
                                             'cwd': str(ROOT), 'approvalPolicy': 'never',
                                             'sandbox': 'read-only'})
        result['provider'] = thread.get('modelProvider')
        result['account_type'] = (client.call('account/read', {'refreshToken': False}).get('account') or {}).get('type')
        began = time.monotonic()
        client.events.clear()
        turn = client.call('turn/start', {'threadId': thread['thread']['id'], 'model': 'gpt-6-luna',
                                          'effort': 'low', 'input': [{'type': 'text', 'text': prompt}]})['turn']['id']
        events = list(client.events)
        deadline = began + 300
        while time.monotonic() < deadline:
            try:
                message = client.receive(min(30, max(1, deadline - time.monotonic())))
            except queue.Empty:
                continue
            events.append(message)
            if message.get('method') == 'thread/tokenUsage/updated':
                result['usage'] = message['params'].get('tokenUsage')
            if message.get('method') == 'turn/completed' and message['params']['turn']['id'] == turn:
                outcome = message['params']['turn']
                answer = '\n'.join(e['params']['item'].get('text', '') for e in events
                                   if e.get('method') == 'item/completed' and e['params']['item'].get('type') == 'agentMessage')
                result.update(status=outcome['status'], error=outcome.get('error'), answer=answer,
                              markers_correct=answer.strip() == start_code + ' ' + end_code,
                              seconds=round(time.monotonic() - began, 2))
                break
        else:
            result['status'] = 'timeout'
    except Exception as error:
        result.update(status='probe_error', error=str(error))
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        destination = ROOT / f'official-context-{args.padding_tokens}-result.json'
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
