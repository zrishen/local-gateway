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


class EmbeddedGraceTests(unittest.TestCase):
    def make_monitor(self, root, settings):
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('secret', encoding='utf-8')
        monitor = mode_monitor.ModeMonitor(root, settings)
        mode = {'value': 'Rule'}
        def controller(path, method='GET', body=None):
            if method == 'PATCH':
                mode['value'] = body['mode']
                return None
            return {'mode': mode['value']}
        monitor.controller = controller
        monitor.upstream_state = Mock(return_value=(True, 3, {}))
        monitor.selection_state = Mock(return_value={})
        monitor.set_public_proxy = Mock()
        return monitor, mode

    def test_short_control_timeout_keeps_rule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = dict(SETTINGS, EmbeddedStateGraceSeconds=30)
            monitor, mode = self.make_monitor(root, settings)
            monitor.core_embedded_state = Mock(side_effect=[
                (True, 'sakura-001', 'sakura-region-us'),
                (False, '', ''),
            ])
            with patch.object(mode_monitor, 'read_proxy', return_value=(True, '127.0.0.1:8799')), \
                 patch.object(mode_monitor, 'backend_ready', return_value=False), \
                 patch.object(mode_monitor.time, 'monotonic', side_effect=[100.0, 101.0]):
                monitor.tick()
                monitor.tick()
            self.assertEqual(mode['value'], 'Rule')
            state = json.loads((root / 'state/mode.json').read_text(encoding='utf-8'))
            self.assertEqual(state['embedded_state'], 'grace')

    def test_persistent_control_failure_eventually_switches_direct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = dict(SETTINGS, EmbeddedStateGraceSeconds=30)
            monitor, mode = self.make_monitor(root, settings)
            monitor.core_embedded_state = Mock(side_effect=[
                (True, 'sakura-001', 'sakura-region-us'),
                (False, '', ''),
                (False, '', ''),
            ])
            with patch.object(mode_monitor, 'read_proxy', return_value=(True, '127.0.0.1:8799')), \
                 patch.object(mode_monitor, 'backend_ready', return_value=False), \
                 patch.object(mode_monitor.time, 'monotonic', side_effect=[100.0, 110.0, 140.0]):
                monitor.tick()
                monitor.tick()
                monitor.tick()
            self.assertEqual(mode['value'], 'Direct')
            state = json.loads((root / 'state/mode.json').read_text(encoding='utf-8'))
            self.assertEqual(state['embedded_state'], 'unavailable')


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

    def test_live_controller_selection_wins_over_stale_quality_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = self.make_monitor(root)
            monitor.core_embedded_state = Mock(
                return_value=(True, 'sakura-067', 'sakura-region-us'))
            monitor.selection_state = Mock(return_value={
                'region': '美国', 'tag': 'sakura-065', 'name': 'Old'})
            monitor.upstream_state = Mock(return_value=(True, 9, {'sakura-067': 'New'}))
            monitor.region_for_group = Mock(return_value='韩国')
            with patch.object(mode_monitor, 'read_proxy', return_value=(True, '127.0.0.1:8799')), \
                 patch.object(mode_monitor, 'backend_ready', return_value=False):
                monitor.tick()
            state = json.loads((root / 'state/mode.json').read_text(encoding='utf-8'))
            self.assertEqual(state['node'], 'sakura-067')
            self.assertEqual(state['node_name'], 'New')
            self.assertEqual(state['region'], '韩国')

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
