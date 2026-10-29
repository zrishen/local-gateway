"""Keep one loopback entry alive while forwarding to hot gateway backends.

The public socket is intentionally protocol agnostic. It copies HTTP,
CONNECT, SOCKS5 and opaque tunnel bytes unchanged, so the existing gateway
backend remains responsible for protocol handling and AI routing.
"""
import argparse
import json
import logging
from pathlib import Path
import select
import socket
import socketserver
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def backend_ports_from_settings(settings):
    values = settings.get('GatewayBackendPorts') or []
    if isinstance(values, (str, int)):
        values = [values]
    ports = []
    for value in values:
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < port < 65536 and port not in ports:
            ports.append(port)
    while len(ports) < 2:
        port = 18801 + len(ports)
        if port not in ports:
            ports.append(port)
    return ports[:2]


class BackendPool:
    """Prefer the primary backend and retain a live standby as fallback."""

    def __init__(self, ports, connect_timeout=.25, retry_interval=.05,
                 failure_cooldown=2.0, route_path=None):
        self.ports = tuple(dict.fromkeys(int(port) for port in ports))
        if not self.ports:
            raise ValueError('At least one backend port is required')
        self.connect_timeout = max(.05, float(connect_timeout))
        self.retry_interval = max(.01, float(retry_interval))
        self.failure_cooldown = max(.1, float(failure_cooldown))
        self.route_path = Path(route_path) if route_path else None
        self.active_port = self.ports[0]
        self._down_until = {}
        self._lock = threading.Lock()

    def _route_state(self):
        """Read optional preferred/draining ports without making traffic depend on the file."""
        if self.route_path is None:
            return self.ports[0], set()
        try:
            state = json.loads(self.route_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return self.ports[0], set()
        if not isinstance(state, dict):
            return self.ports[0], set()
        try:
            preferred = int(state.get('PreferredPort'))
        except (TypeError, ValueError):
            preferred = self.ports[0]
        if preferred not in self.ports:
            preferred = self.ports[0]
        draining = set()
        for value in state.get('DrainingPorts') or []:
            try:
                port = int(value)
            except (TypeError, ValueError):
                continue
            if port in self.ports:
                draining.add(port)
        return preferred, draining

    def _order(self):
        now = time.monotonic()
        preferred, draining = self._route_state()
        with self._lock:
            active = self.active_port if (
                self.active_port not in draining
                and self._down_until.get(self.active_port, 0) <= now) else None
        candidates = []
        if preferred not in draining and self._down_until.get(preferred, 0) <= now:
            candidates.append(preferred)
        if active and active not in candidates:
            candidates.append(active)
        candidates.extend(
            port for port in self.ports
            if port not in draining and port not in candidates
            and self._down_until.get(port, 0) <= now
        )
        # During a simultaneous outage, still try known-down ports so a
        # fast restart can recover before the cooldown expires.
        if not candidates:
            candidates.extend(port for port in self.ports if port not in draining)
        return candidates or list(self.ports)

    def acquire(self, wait_seconds=5.0):
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        last_error = None
        while True:
            for port in self._order():
                try:
                    upstream = socket.create_connection(
                        ('127.0.0.1', port), timeout=self.connect_timeout)
                except OSError as error:
                    last_error = error
                    self.mark_failure(port)
                    continue
                try:
                    upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                with self._lock:
                    self.active_port = port
                    self._down_until.pop(port, None)
                return upstream, port
            if time.monotonic() >= deadline:
                raise OSError('All gateway backends are unavailable') from last_error
            time.sleep(self.retry_interval)

    def mark_failure(self, port):
        port = int(port)
        with self._lock:
            self._down_until[port] = time.monotonic() + self.failure_cooldown
            alternatives = [item for item in self.ports if item != port]
            if alternatives:
                self.active_port = alternatives[0]

    @staticmethod
    def _probe(port, timeout=.25):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=timeout):
                return True
        except OSError:
            return False

    def monitor(self, stop):
        last_preferred_probe = 0.0
        while not stop.wait(1.0):
            preferred, draining = self._route_state()
            with self._lock:
                active = self.active_port
            healthy = [port for port in self.ports
                       if port not in draining and self._probe(port)]
            if preferred in healthy:
                now = time.monotonic()
                if active != preferred and now - last_preferred_probe >= 5.0:
                    last_preferred_probe = now
                    with self._lock:
                        self.active_port = preferred
                        self._down_until.pop(preferred, None)
                continue
            if not healthy:
                continue
            with self._lock:
                self.active_port = healthy[0]
                for port in healthy:
                    self._down_until.pop(port, None)


def _relay(client, upstream, backend_port, pool):
    client.settimeout(30)
    upstream.settimeout(30)
    sources = {client: upstream, upstream: client}
    try:
        while sources:
            readable, _, _ = select.select(list(sources), [], [], 600)
            if not readable:
                return
            for source in readable:
                try:
                    chunk = source.recv(65536)
                except OSError:
                    if source is upstream:
                        pool.mark_failure(backend_port)
                    return
                if not chunk:
                    destination = sources.pop(source)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    try:
                        sources[source].sendall(chunk)
                    except OSError:
                        destination = sources[source]
                        if destination is upstream:
                            pool.mark_failure(backend_port)
                        return
    finally:
        upstream.close()


class EntryHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            upstream, backend_port = self.server.pool.acquire()
            _relay(self.request, upstream, backend_port, self.server.pool)
        except OSError as error:
            logging.warning('Gateway entry could not relay to a backend: %s', error)
        except ValueError as error:
            logging.debug('Gateway entry connection closed: %s', error)


class EntryServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 256


def make_handler(pool):
    class Handler(EntryHandler):
        def __init__(self, request, client_address, server):
            server.pool = pool
            super().__init__(request, client_address, server)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int)
    parser.add_argument('--backend-port', dest='backend_ports', type=int, action='append')
    args = parser.parse_args()

    from settings_config import load_settings
    settings = load_settings(ROOT)
    port = int(args.port or settings.get('PublicPort', 8799))
    ports = args.backend_ports or backend_ports_from_settings(settings)
    if len(set(ports)) < 2:
        ports = backend_ports_from_settings({'GatewayBackendPorts': ports})
    pool = BackendPool(ports, route_path=ROOT / 'state/gateway-entry-route.json')
    stop = threading.Event()
    threading.Thread(target=pool.monitor, args=(stop,), daemon=True,
                     name='gateway-backend-monitor').start()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        while True:
            try:
                with EntryServer(('127.0.0.1', port), make_handler(pool)) as server:
                    logging.info('Stable gateway entry listening on 127.0.0.1:%d -> %s',
                                 port, ', '.join(str(item) for item in ports))
                    server.serve_forever(poll_interval=.2)
            except OSError as error:
                logging.exception('Stable gateway entry unavailable; rebinding: %s', error)
                time.sleep(.2)
    finally:
        stop.set()


if __name__ == '__main__':
    main()
