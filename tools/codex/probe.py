"""Isolated Codex app-server checks; never prints credentials or request bodies."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

PROJECT = Path(__file__).resolve().parents[2]
ROOT = PROJECT / 'work/codex-switch'
ROOT.mkdir(parents=True, exist_ok=True)
BINARY = Path(os.environ.get('CODEX_BINARY') or Path.home() / 'AppData/Local/OpenAI/Codex/bin/896a0166b774037a/codex.exe')


class Rpc:
    def __init__(self, overrides=(), inject_secret=True, stderr_path=None):
        if not BINARY.is_file():
            raise FileNotFoundError('App backend unavailable. Set CODEX_BINARY to the App-bundled codex.exe; match its version to the model catalog.')
        command = [str(BINARY), 'app-server', '--listen', 'stdio://']
        for value in overrides:
            command += ['-c', value]
        self.stderr = Path(stderr_path or ROOT / 'probe.stderr.log').open('w', encoding='utf-8')
        environment = os.environ.copy()
        secret = ROOT.parents[1] / 'state/codex-router-secret.txt'
        if inject_secret and secret.exists():
            environment['UNIFIED_CODEX_GATEWAY_KEY'] = secret.read_text(encoding='utf-8').strip()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, env=environment,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        self.number = 0
        self.events = []
        threading.Thread(target=self.read, daemon=True).start()
        self.call('initialize', {'clientInfo': {'name': 'local_gateway_probe', 'version': '1.0'},
                                 'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized', 'params': {}})

    def read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                pass
        self.messages.put({'eof': True})

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        self.process.stdin.flush()

    def receive(self, timeout=90):
        message = self.messages.get(timeout=timeout)
        if message.get('eof'):
            raise RuntimeError('app-server exited; inspect probe.stderr.log')
        if 'method' in message and 'id' in message:
            if message['method'] == 'item/tool/call' and message.get('params', {}).get('tool') == 'gateway_echo':
                self.send({'id': message['id'], 'result': {'success': True, 'contentItems': [
                    {'type': 'inputText', 'text': 'tool-ok-7291'}]}})
            else:
                # Tests never approve app actions or execute arbitrary tools.
                self.send({'id': message['id'], 'error': {'code': -32000, 'message': 'Disabled in isolated test'}})
        return message

    def call(self, method, params, timeout=90):
        self.number += 1
        number = self.number
        self.send({'id': number, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.receive(max(1, deadline - time.monotonic()))
            if message.get('id') == number:
                if 'error' in message:
                    raise RuntimeError(json.dumps(message['error'], ensure_ascii=False))
                return message.get('result')
            self.events.append(message)
        raise TimeoutError(method)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.stderr.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', action='append', default=[])
    args = parser.parse_args()
    client = Rpc(args.c)
    try:
        account = client.call('account/read', {'refreshToken': False})
        print(json.dumps({'requiresOpenaiAuth': account.get('requiresOpenaiAuth'),
                          'accountType': (account.get('account') or {}).get('type')}, ensure_ascii=False))
        result = client.call('model/list', {'limit': 100, 'includeHidden': False})
        print(json.dumps([{'id': m.get('id'), 'name': m.get('displayName')}
                          for m in result['data']], ensure_ascii=False))
    finally:
        client.close()


if __name__ == '__main__':
    main()
