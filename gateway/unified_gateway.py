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
from gateway_activity import ai_request, request_stats

ROOT = Path(__file__).resolve().parents[1]


def _status_summary(name):
    try:
        value = json.loads((ROOT / 'state' / name).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    keys = ('ready', 'degraded', 'active_tag', 'tag', 'candidate_tag',
            'region', 'score', 'health', 'next_scan_epoch', 'applied',
            'last_probe_at', 'observed_epoch')
    return {key: value.get(key) for key in keys if key in value}


def classify(prefix, public_port, listen_port=None):
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
            local_ports = {int(public_port)}
            if listen_port is not None:
                local_ports.add(int(listen_port))
            if target.hostname in ('127.0.0.1', 'localhost', '::1') and target.port in local_ports:
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


def make_handler(settings, official_manager=None):
    import sanitize_proxy
    from codex_router import Router
    # PublicPort is the stable client-facing entry. ListenPort is this
    # particular backend's private socket, so third-party self-calls stay on
    # the same process and do not bounce through the stable entry.
    listen_port = int(settings.get('ListenPort') or settings['PublicPort'])
    sanitize_proxy.PORT = int(settings['PublicPort'])
    codex = Router(api_port=listen_port, settings=settings,
                   official_manager=official_manager)

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
                active, oldest = request_stats()
                real_active = max(0, active - 1)
                body = json.dumps({
                    'service': 'unified-gateway', **state,
                    'active_ai_requests': real_active,
                    **({'oldest_ai_request_age_seconds': round(oldest, 1)}
                       if real_active > 0 else {}),
                    'codex_official': _status_summary('codex-official-route.json'),
                    'sakura_route': _status_summary('sakura-selection.json'),
                }).encode()
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

        def do_codex_method(self):
            if codex.handle(self, self.command):
                return
            fallback = getattr(super(), 'do_' + self.command, None)
            if fallback is not None:
                return fallback()
            self.send_error(501, 'Unsupported method')

        do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_codex_method

    # The bridge uses the same dedicated selector as the Codex router so a
    # failed CONNECT can rotate the official node before falling back.
    AIHandler.official_manager = official_manager

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
                    kind = classify(prefix, settings['PublicPort'], listen_port)
                    if kind is None:
                        if len(prefix) >= 8192 or time.monotonic() >= deadline:
                            return
                        time.sleep(.01)
                if kind == 'proxy':
                    relay(client, settings['MixedPort'])
                elif kind == 'ai':
                    client.settimeout(600)
                    with ai_request():
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
        settings['ListenPort'] = args.port
    else:
        settings['ListenPort'] = settings['PublicPort']
    if args.mixed_port:
        settings['MixedPort'] = args.mixed_port
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    official_manager = None
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
        from official_route import OfficialRouteManager
        try:
            official_manager = OfficialRouteManager(ROOT, settings)
            threading.Thread(target=official_manager.run, daemon=True,
                             name='codex-official-route').start()
        except (OSError, ValueError, TypeError) as error:
            logging.warning('Official Codex route manager not started: %s', error)
    with Server(('127.0.0.1', settings['ListenPort']),
                make_handler(settings, official_manager)) as server:
        logging.info('Unified AI/HTTP/SOCKS backend listening on 127.0.0.1:%d (entry %d)',
                     settings['ListenPort'], settings['PublicPort'])
        server.serve_forever(poll_interval=.5)


if __name__ == '__main__':
    main()
