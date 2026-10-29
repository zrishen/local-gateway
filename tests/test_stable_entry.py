import json
from pathlib import Path
import tempfile
import socket
import socketserver
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import stable_entry


class MarkerHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if self.request.recv(1):
            self.request.sendall(self.server.marker)


class MarkerServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, marker):
        self.marker = marker
        super().__init__(address, MarkerHandler)


class StableEntryTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()

    def start_server(self, address, marker):
        server = MarkerServer(address, marker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server

    def request(self, port):
        with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
            client.sendall(b'x')
            return client.recv(8)

    def test_settings_ports_are_validated_and_completed(self):
        self.assertEqual(stable_entry.backend_ports_from_settings(
            {'GatewayBackendPorts': ['18801', 0, '18801', 18802, 18803]}),
            [18801, 18802])
        self.assertEqual(stable_entry.backend_ports_from_settings({}), [18801, 18802])

    def test_public_entry_switches_to_standby_when_primary_exits(self):
        primary = self.start_server(('127.0.0.1', 0), b'primary')
        secondary = self.start_server(('127.0.0.1', 0), b'standby')
        pool = stable_entry.BackendPool(
            [primary.server_address[1], secondary.server_address[1]],
            connect_timeout=.05, retry_interval=.01)
        entry = stable_entry.EntryServer(
            ('127.0.0.1', 0), stable_entry.make_handler(pool))
        threading.Thread(target=entry.serve_forever, daemon=True).start()
        self.servers.append(entry)

        self.assertEqual(self.request(entry.server_address[1]), b'primary')
        primary.shutdown()
        primary.server_close()
        self.servers.remove(primary)

        self.assertEqual(self.request(entry.server_address[1]), b'standby')
        self.assertEqual(pool.active_port, secondary.server_address[1])

    def test_route_file_switches_new_connections_without_restarting_entry(self):
        primary = self.start_server(('127.0.0.1', 0), b'primary')
        secondary = self.start_server(('127.0.0.1', 0), b'standby')
        primary_port = primary.server_address[1]
        secondary_port = secondary.server_address[1]
        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / 'route.json'
            route_path.write_text(json.dumps({
                'PreferredPort': secondary_port, 'DrainingPorts': [primary_port]}),
                encoding='utf-8')
            pool = stable_entry.BackendPool(
                [primary_port, secondary_port], connect_timeout=.05,
                retry_interval=.01, route_path=route_path)
            entry = stable_entry.EntryServer(
                ('127.0.0.1', 0), stable_entry.make_handler(pool))
            threading.Thread(target=entry.serve_forever, daemon=True).start()
            self.servers.append(entry)
            entry_port = entry.server_address[1]

            self.assertEqual(self.request(entry_port), b'standby')
            route_path.write_text(json.dumps({
                'PreferredPort': primary_port, 'DrainingPorts': [primary_port]}),
                encoding='utf-8')
            self.assertEqual(self.request(entry_port), b'standby')
            route_path.write_text(json.dumps({
                'PreferredPort': primary_port, 'DrainingPorts': []}),
                encoding='utf-8')
            self.assertEqual(self.request(entry_port), b'primary')

    def test_health_monitor_prefers_recovered_primary(self):
        first = self.start_server(('127.0.0.1', 0), b'primary')
        second = self.start_server(('127.0.0.1', 0), b'standby')
        pool = stable_entry.BackendPool(
            [first.server_address[1], second.server_address[1]],
            connect_timeout=.05, retry_interval=.01)
        pool.active_port = second.server_address[1]
        stop = threading.Event()
        threading.Thread(target=pool.monitor, args=(stop,), daemon=True).start()
        try:
            deadline = time.monotonic() + 2
            while pool.active_port != first.server_address[1] and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(pool.active_port, first.server_address[1])
        finally:
            stop.set()


if __name__ == '__main__':
    unittest.main()
