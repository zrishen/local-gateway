"""One loopback TCP port: AI API + transparent HTTP/SOCKS proxy handoff.

Proxy traffic is copied to sing-box without TLS interception. Origin-form HTTP
requests retain the existing AI model mapping/schema cleanup/Responses bridge.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import select
import socket
import socketserver
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def classify(prefix, public_port):
    if prefix[:1] == b'\x05':
        return 'proxy'
    if b'\n' not in prefix:
        return None
    parts = prefix.split(b'\n', 1)[0].rstrip(b'\r').split(b' ')
    if len(parts) != 3:
        return 'invalid'
    method, raw_target, _ = parts
    if method == b'CONNECT':
        return 'proxy'
    if raw_target.lower().startswith((b'http://', b'https://')):
        try:
            target = urllib.parse.urlsplit(raw_target.decode('ascii'))
            if target.hostname in ('127.0.0.1', 'localhost', '::1') and target.port == public_port:
                return 'ai'
        except (ValueError, UnicodeError):
            return 'invalid'
        return 'proxy'
    return 'ai' if raw_target.startswith(b'/') else 'invalid'


def relay(client, mixed_port):
    with socket.create_connection(('127.0.0.1', mixed_port), timeout=5) as upstream:
        client.settimeout(30)
        upstream.settimeout(30)
        sources = {client: upstream, upstream: client}
        while sources:
            readable, _, _ = select.select(list(sources), [], [], 600)
            if not readable:
                return
            for source in readable:
                chunk = source.recv(65536)
                if chunk:
                    sources[source].sendall(chunk)
                else:
                    destination = sources.pop(source)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass


def make_handler(settings):
    import sanitize_proxy
    from codex_router import Router
    sanitize_proxy.PORT = settings['PublicPort']
    codex = Router(api_port=settings['PublicPort'])

    class AIHandler(sanitize_proxy.Handler):
        server_version = 'unified-gateway/2'

        def parse_request(self):
            if not super().parse_request():
                return False
            if self.path.lower().startswith(('http://', 'https://')):
                target = urllib.parse.urlsplit(self.path)
                self.path = target.path or '/'
                if target.query:
                    self.path += '?' + target.query
            return True

        def do_GET(self):
            if codex.handle(self, 'GET'):
                return
            if self.path.split('?')[0] == '/__status':
                try:
                    state = json.loads((ROOT / 'state/mode.json').read_text())
                except (OSError, ValueError):
                    state = {'mode': 'starting'}
                body = json.dumps({'service': 'unified-gateway', **state}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return super().do_GET()

        def do_POST(self):
            if codex.handle(self, 'POST'):
                return
            return super().do_POST()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            client = self.request
            client.settimeout(10)
            deadline = time.monotonic() + 10
            try:
                kind = None
                while kind is None:
                    prefix = client.recv(8192, socket.MSG_PEEK)
                    if not prefix:
                        return
                    kind = classify(prefix, settings['PublicPort'])
                    if kind is None:
                        if len(prefix) >= 8192 or time.monotonic() >= deadline:
                            return
                        time.sleep(.01)
                if kind == 'proxy':
                    relay(client, settings['MixedPort'])
                elif kind == 'ai':
                    client.settimeout(600)
                    AIHandler(client, self.client_address, self.server)
                else:
                    client.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except (OSError, ValueError) as error:
                logging.debug('Connection closed: %s', error)

    return Handler


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int)
    parser.add_argument('--mixed-port', type=int)
    parser.add_argument('--no-monitor', action='store_true')
    args = parser.parse_args()
    from settings_config import load_settings
    settings = load_settings(ROOT)
    if args.port:
        settings['PublicPort'] = args.port
    if args.mixed_port:
        settings['MixedPort'] = args.mixed_port
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not args.no_monitor:
        from codex_catalog import CatalogSync
        threading.Thread(target=CatalogSync().run, daemon=True,
                         name='codex-model-catalog').start()
        from mode_monitor import ModeMonitor
        monitor = ModeMonitor(ROOT, settings)
        threading.Thread(target=monitor.run, daemon=True, name='sakura-mode-monitor').start()
        from latency_selector import LatencySelector
        try:
            selector = LatencySelector(ROOT, settings)
            threading.Thread(target=selector.run, daemon=True, name='sakura-latency-selector').start()
        except OSError as error:
            logging.warning('Sakura latency selector not started: %s', error)
    with Server(('127.0.0.1', settings['PublicPort']), make_handler(settings)) as server:
        logging.info('Unified AI/HTTP/SOCKS entry listening on 127.0.0.1:%d', settings['PublicPort'])
        server.serve_forever(poll_interval=.5)


if __name__ == '__main__':
    main()
