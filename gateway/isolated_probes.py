"""Short-lived node probes with no TUN, system proxy or production selectors."""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time

# Both the browser selector and the official Codex lane launch short-lived
# sing-box probes. Serialize them so two probe cores cannot compete for the
# same upstream bandwidth or leave orphan processes.
PROBE_LOCK = threading.RLock()


def probe_config(source, ports):
    by_tag = {item['tag']: item for item in source['outbounds']}
    required = {'direct', *ports}
    pending = list(required)
    while pending:
        item = by_tag[pending.pop()]
        for tag in item.get('outbounds', []) + ([item['detour']] if item.get('detour') else []):
            if tag not in required:
                required.add(tag)
                pending.append(tag)
    # Every candidate gets its own inbound. No controller or mutable selector
    # is exposed, and candidate testing never changes the real routing table.
    return {
        'log': {'disabled': True},
        'dns': {'servers': [{'type': 'udp', 'tag': 'china-dns', 'server': '223.5.5.5',
                             'detour': 'direct'}], 'final': 'china-dns', 'strategy': 'ipv4_only'},
        'inbounds': [{'type': 'mixed', 'tag': 'probe-' + tag, 'listen': '127.0.0.1',
                      'listen_port': port} for tag, port in ports.items()],
        'outbounds': [copy.deepcopy(item) for item in source['outbounds'] if item['tag'] in required],
        'route': {'auto_detect_interface': True, 'default_domain_resolver': 'china-dns',
                  'final': 'direct', 'rules': [
                      {'inbound': ['probe-' + tag], 'outbound': tag} for tag in ports]},
    }


class IsolatedProbePool:
    def __init__(self, root, tags):
        self.root = Path(root)
        self.tags = list(tags)
        self.ports = {}
        self.process = None
        self.temporary = None
        self._locked = False

    def __enter__(self):
        reserved = []
        try:
            PROBE_LOCK.acquire()
            self._locked = True
            for tag in self.tags:
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                reserved.append(sock)
                self.ports[tag] = sock.getsockname()[1]
            source = json.loads((self.root / 'config.json').read_text(encoding='utf-8-sig'))
            config = probe_config(source, self.ports)
            work = self.root / 'work'
            work.mkdir(exist_ok=True)
            self.temporary = tempfile.TemporaryDirectory(
                prefix='isolated-probes-', dir=work, ignore_cleanup_errors=True)
            path = Path(self.temporary.name) / 'config.json'
            path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            for sock in reserved:
                sock.close()
            self.process = subprocess.Popen(
                [str(self.root / 'bin/sing-box.exe'), 'run', '-D', str(self.root), '-c', str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                (Path(self.temporary.name) / 'pid.txt').write_text(
                    str(self.process.pid), encoding='ascii')
            except OSError:
                pass
            deadline = time.monotonic() + 8
            remaining = set(self.ports.values())
            while remaining and time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise OSError('Isolated probe core exited')
                for port in list(remaining):
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.1):
                            remaining.remove(port)
                    except OSError:
                        pass
                if remaining:
                    time.sleep(.05)
            if remaining:
                raise TimeoutError('Isolated probe core did not become ready')
            return self
        except BaseException:
            self.close()
            raise
        finally:
            for sock in reserved:
                sock.close()

    def close(self):
        if self.process is not None:
            pid = self.process.pid
            if self.process.poll() is None:
                if os.name == 'nt':
                    try:
                        subprocess.run(
                            ['taskkill', '/PID', str(pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    except (OSError, subprocess.SubprocessError):
                        pass
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            self.process = None
        if self.temporary is not None:
            try:
                self.temporary.cleanup()
            except OSError:
                pass
            self.temporary = None
        if self._locked:
            self._locked = False
            try:
                PROBE_LOCK.release()
            except RuntimeError:
                pass

    def __exit__(self, *exc):
        self.close()
