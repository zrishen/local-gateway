"""Track SakuraCat intent and switch the independent TUN without restarting it."""
import json
import logging
from pathlib import Path
import socket
import threading
import time
import urllib.request
import winreg

REG_PATH = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'


def read_proxy():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
        def value(name, default):
            try:
                return winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                return default
        return bool(value('ProxyEnable', 0)), str(value('ProxyServer', ''))


def is_proxy(server, port):
    return any(address in server.lower() for address in (f'127.0.0.1:{port}', f'localhost:{port}'))


def proxy_policy(enabled, server, settings):
    """Classify the active proxy without taking ownership from SakuraCat.

    Disabled means the project should turn its own entry on. A SakuraCat,
    project, or unrelated active proxy is left untouched.
    """
    if not enabled:
        return 'enable_project'
    if is_proxy(server, settings['PublicPort']) or is_proxy(server, settings['MixedPort']):
        return 'project'
    if is_proxy(server, settings['SakuraPort']):
        return 'sakuracat'
    return 'external'


def backend_ready(settings):
    try:
        with socket.create_connection((settings['SakuraHost'], settings['SakuraPort']), timeout=.5):
            return True
    except OSError:
        return False


class ModeMonitor:
    def __init__(self, root, settings):
        self.root = Path(root)
        self.settings = settings
        self.stop = threading.Event()
        self.intent = None
        self.last_mode = None
        self.last_enabled = None
        self.last_available = None
        self.last_embedded_ok = 0.0
        self.last_embedded_state = None
        self.embedded_grace_seconds = max(
            0.0, float(settings.get('EmbeddedStateGraceSeconds', 30)))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.secret = (self.root / 'state/controller-secret.txt').read_text().strip()

    def controller(self, path, method='GET', body=None):
        headers = {'Authorization': 'Bearer ' + self.secret}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.settings['ControllerPort']}{path}", data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=2) as response:
            data = response.read()
            return json.loads(data) if data else None

    def set_public_proxy(self):
        import ctypes
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'ProxyServer', 0, winreg.REG_SZ, f"127.0.0.1:{self.settings['PublicPort']}")
            winreg.SetValueEx(key, 'ProxyOverride', 0, winreg.REG_SZ, '<local>;localhost;127.*;[::1]')
            winreg.SetValueEx(key, 'ProxyEnable', 0, winreg.REG_DWORD, 1)
        for option in (39, 37):
            ctypes.windll.wininet.InternetSetOptionW(None, option, None, 0)

    def upstream_state(self):
        try:
            data = json.loads((self.root / 'state/sakura-outbounds.json').read_text(encoding='utf-8'))
            count = int(data.get('count', 0))
            names = data.get('names') if isinstance(data.get('names'), dict) else {}
            return count > 0 and bool(data.get('outbounds')), count, names
        except (OSError, ValueError, TypeError):
            return False, 0, {}

    def core_embedded_state(self):
        try:
            proxy = self.controller('/proxies/sakura')
            embedded = str(proxy.get('type', '')).lower() in ('urltest', 'selector')
            current = str(proxy.get('now') or '')
            region_group = ''
            if current.startswith('sakura-region-'):
                region_group = current
                inner = self.controller('/proxies/' + current)
                current = str(inner.get('now') or '')
            return embedded, current, region_group
        except Exception as error:
            logging.debug('Embedded core state unavailable: %s', error)
            return False, '', ''

    def selection_state(self):
        try:
            return json.loads((self.root / 'state/sakura-selection.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return {}

    def tick(self):
        enabled, server = read_proxy()
        proxy_state = proxy_policy(enabled, server, self.settings)
        if proxy_state == 'enable_project':
            self.set_public_proxy()
            enabled, server = True, f"127.0.0.1:{self.settings['PublicPort']}"
            proxy_state = 'project'
        file_ready, node_count, names = self.upstream_state()
        core_embedded, selected, region_group = self.core_embedded_state()
        selection = self.selection_state()
        now_monotonic = time.monotonic()
        if file_ready and core_embedded:
            embedded = True
            embedded_state = 'ready'
            self.last_embedded_ok = now_monotonic
        elif (file_ready and self.last_embedded_ok and
              now_monotonic - self.last_embedded_ok <= self.embedded_grace_seconds):
            # A local control-interface timeout must not make working proxy
            # traffic fall back to Direct. Hold the last good state briefly.
            embedded = True
            embedded_state = 'grace'
        else:
            embedded = False
            embedded_state = 'unavailable'
        sakura_available = backend_ready(self.settings)
        available = embedded or sakura_available
        native = is_proxy(server, self.settings['SakuraPort'])
        ours = is_proxy(server, self.settings['PublicPort']) or is_proxy(server, self.settings['MixedPort'])
        if embedded:
            self.intent = True
        elif enabled and native:
            self.intent = True
        elif not enabled and self.last_enabled is True:
            self.intent = False
        elif enabled and not ours:
            self.intent = False
        elif self.intent is None or (sakura_available and self.last_available is False):
            self.intent = sakura_available
        desired = 'Rule' if self.intent and available else 'Direct'
        current = self.controller('/configs').get('mode')
        if current.lower() != desired.lower():
            self.controller('/configs', 'PATCH', {'mode': desired})
        # SakuraCat is allowed to take over the system proxy while enabled.
        if desired != self.last_mode:
            logging.info('Network mode: %s (embedded subscription=%s/%s, SakuraCat available=%s)',
                         desired, embedded, embedded_state, sakura_available)
            self.last_mode = desired
        if embedded_state != self.last_embedded_state:
            logging.info('Embedded state: %s (file_ready=%s, core_ready=%s, grace=%.0fs)',
                         embedded_state, file_ready, core_embedded, self.embedded_grace_seconds)
            self.last_embedded_state = embedded_state
        snapshot = {
            'mode': desired,
            'upstream': 'subscription' if embedded else 'sakuracat',
            'embedded_state': embedded_state,
            'embedded_grace_seconds': self.embedded_grace_seconds,
            'nodes': node_count if embedded else 0,
            'region': selection.get('region') if embedded else None,
            'region_group': region_group if embedded else None,
            'node': selection.get('tag') or (selected if embedded else None),
            'node_name': selection.get('name') or (names.get(selected) if embedded else None),
            'delay': selection.get('delay') if embedded else None,
            'sakura_available': sakura_available,
            'sakura_requested': bool(self.intent),
            'system_proxy': proxy_state,
            'system_proxy_server': server,
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        target = self.root / 'state/mode.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
        temporary.replace(target)
        self.last_enabled = enabled
        self.last_available = sakura_available

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:
                logging.debug('Mode check unavailable: %s', error)
            self.stop.wait(1)
