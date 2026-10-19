import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import mode_monitor


SETTINGS = {
    'PublicPort': 8799,
    'MixedPort': 12550,
    'SakuraPort': 12450,
}


class ProxyPolicyTests(unittest.TestCase):
    def test_disabled_requests_project_proxy(self):
        self.assertEqual(mode_monitor.proxy_policy(False, '', SETTINGS), 'enable_project')

    def test_project_proxy_is_preserved(self):
        for server in ('127.0.0.1:8799', 'localhost:12550'):
            self.assertEqual(mode_monitor.proxy_policy(True, server, SETTINGS), 'project')

    def test_sakuracat_proxy_wins_while_enabled(self):
        self.assertEqual(mode_monitor.proxy_policy(True, '127.0.0.1:12450', SETTINGS), 'sakuracat')

    def test_external_proxy_is_not_overwritten(self):
        self.assertEqual(mode_monitor.proxy_policy(True, '127.0.0.1:18080', SETTINGS), 'external')


class TickTests(unittest.TestCase):
    def make_monitor(self, root):
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('secret', encoding='utf-8')
        monitor = mode_monitor.ModeMonitor(root, SETTINGS)
        monitor.controller = Mock(return_value={'mode': 'Rule'})
        monitor.upstream_state = Mock(return_value=(True, 3, {}))
        monitor.core_embedded_state = Mock(return_value=(True, 'sakura-001', 'sakura-region-us'))
        monitor.selection_state = Mock(return_value={})
        return monitor

    def test_disabled_proxy_is_enabled_by_tick(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self.make_monitor(root)
            monitor.set_public_proxy = Mock()
            with patch.object(mode_monitor, 'read_proxy', return_value=(False, '')), \
                 patch.object(mode_monitor, 'backend_ready', return_value=False):
                monitor.tick()
            monitor.set_public_proxy.assert_called_once_with()
            state = json.loads((root / 'state/mode.json').read_text(encoding='utf-8'))
            self.assertEqual(state['system_proxy'], 'project')
            self.assertEqual(state['system_proxy_server'], '127.0.0.1:8799')

    def test_sakuracat_proxy_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self.make_monitor(root)
            monitor.set_public_proxy = Mock()
            with patch.object(mode_monitor, 'read_proxy', return_value=(True, '127.0.0.1:12450')), \
                 patch.object(mode_monitor, 'backend_ready', return_value=True):
                monitor.tick()
            monitor.set_public_proxy.assert_not_called()
            state = json.loads((root / 'state/mode.json').read_text(encoding='utf-8'))
            self.assertEqual(state['system_proxy'], 'sakuracat')
            self.assertEqual(state['system_proxy_server'], '127.0.0.1:12450')


if __name__ == '__main__':
    unittest.main()
