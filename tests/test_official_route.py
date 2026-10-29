import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import official_route as oroute


class FakeController:
    def __init__(self, tags, current='a'):
        self.state = {'type': 'Selector', 'all': list(tags), 'now': current}
        self.selected = []

    def __call__(self, path, method='GET', body=None):
        if path == '/proxies/codex-official' and method == 'GET':
            return dict(self.state)
        if path == '/proxies/codex-official' and method == 'PUT':
            self.selected.append(body['name'])
            self.state['now'] = body['name']
            return None
        raise AssertionError((path, method, body))


class OfficialRouteTests(unittest.TestCase):
    def temp_root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'state').mkdir()
        (root / 'state/sakura-outbounds.json').write_text(json.dumps({
            'names': {'a': 'US A', 'b': 'US B'},
        }), encoding='utf-8')
        return root

    def manager(self, root, controller, results, **settings):
        values = {
            'ControllerPort': 1, 'CodexOfficialEnabled': True,
            'CodexOfficialGroup': 'sakura-region-us',
            'SakuraMinimumEffectiveKBps': 15,
            'SakuraMinimumBodyKBps': 30,
            'SakuraTunValidation': False,
            **settings,
        }
        manager = oroute.OfficialRouteManager(
            root, values, probe=lambda port, timeout: {}, request=controller)
        manager.probe_members = lambda members: [
            {**member, **results[member['tag']]} for member in members
            if results[member['tag']].get('ok')]
        return manager

    @staticmethod
    def good(effective=90, body=140):
        return {'ok': True, 'complete': True, 'chatgpt_ok': True,
                'effective_throughput_kbps': effective, 'throughput_kbps': body,
                'first_byte_ms': 500, 'codex_path_ok': True}

    def test_better_us_candidate_replaces_a_low_current_node(self):
        root = self.temp_root()
        controller = FakeController(['a', 'b'])
        manager = self.manager(root, controller, {
            'a': self.good(20, 30), 'b': self.good(90, 150)})
        selected = manager.select_once()
        self.assertEqual(selected['tag'], 'b')
        self.assertEqual(controller.selected, ['b'])
        state = json.loads((root / 'state/codex-official-route.json').read_text())
        self.assertTrue(state['ready'])
        self.assertEqual(state['active_tag'], 'b')
        self.assertEqual(state['selector'], 'codex-official')
        self.assertFalse(state['degraded'])

    def test_similar_candidate_does_not_switch(self):
        root = self.temp_root()
        controller = FakeController(['a', 'b'])
        manager = self.manager(root, controller, {
            'a': self.good(40, 100), 'b': self.good(42, 105)})
        selected = manager.select_once()
        self.assertEqual(selected['tag'], 'a')
        self.assertEqual(controller.selected, [])

    def test_missing_selector_reports_not_ready_without_crashing(self):
        root = self.temp_root()
        manager = oroute.OfficialRouteManager(
            root, {'ControllerPort': 1}, request=lambda *a, **k: (_ for _ in ()).throw(
                OSError('missing')))
        self.assertIsNone(manager.select_once())
        state = json.loads((root / 'state/codex-official-route.json').read_text())
        self.assertFalse(state['ready'])
        self.assertIn('selector unavailable', state['error'])

    def test_upload_timeout_marks_the_lane_degraded_and_requests_a_scan(self):
        root = self.temp_root()
        manager = self.manager(root, FakeController(['a']), {'a': self.good()})
        manager.select_once()
        manager.record_request(upload_seconds=181, failure_stage='send',
                               upload_timed_out=True)
        state = json.loads((root / 'state/codex-official-route.json').read_text())
        self.assertTrue(state['degraded'])
        self.assertFalse(state['ready'])
        self.assertTrue(manager._scan_now.is_set())
        manager.record_request(failure_stage=None)
        self.assertFalse(manager._degraded)

    def test_two_weak_health_checks_trigger_a_candidate_scan_without_outage(self):
        root = self.temp_root()
        controller = FakeController(['a'])
        manager = self.manager(root, controller, {'a': self.good(90, 150)})
        manager.select_once()
        manager.probe_members = lambda members: [
            {**members[0], **self.good(10, 15)}]
        self.assertFalse(manager.health_check())  # first weak sample is observed
        self.assertFalse(manager.health_check())
        self.assertFalse(manager._degraded)
        self.assertTrue(manager._scan_now.is_set())
        state = json.loads((root / 'state/codex-official-route.json').read_text())
        self.assertTrue(state['ready'])
        manager.record_request(failure_stage=None)
        state = json.loads((root / 'state/codex-official-route.json').read_text())
        self.assertTrue(state['ready'])

    def test_fast_failover_uses_the_cached_alternative(self):
        root = self.temp_root()
        controller = FakeController(['a', 'b'])
        manager = self.manager(root, controller, {
            'a': self.good(40, 100), 'b': self.good(42, 105)})
        self.assertEqual(manager.select_once()['tag'], 'a')
        chosen = manager.failover_after_connect_failure('a')
        self.assertEqual(chosen['tag'], 'b')
        self.assertEqual(controller.selected, ['b'])
        state = json.loads((root / 'state/codex-official-route.json').read_text())
        self.assertTrue(state['ready'])
        self.assertFalse(state['degraded'])
        self.assertEqual(state['active_tag'], 'b')

    def test_oversized_upload_does_not_blame_the_selected_node(self):
        root = self.temp_root()
        controller = FakeController(['a'])
        manager = self.manager(root, controller, {'a': self.good(90, 150)})
        manager.select_once()
        manager.record_request(
            upload_seconds=15, failure_stage='send', upload_timed_out=True,
            lane='codex-official', tag='a', request_bytes=46 * 1024 * 1024)
        state = json.loads((root / 'state/codex-official-route.json').read_text())
        self.assertTrue(state['ready'])
        self.assertFalse(state['degraded'])
        self.assertTrue(state['last_request']['oversized_upload'])
        self.assertFalse(manager._degraded)

    def test_repeated_all_lane_connect_failure_requests_a_scan(self):
        root = self.temp_root()
        controller = FakeController(['a'])
        manager = self.manager(root, controller, {'a': self.good(90, 150)})
        manager.select_once()
        manager.record_request(failure_stage='connect', lane='project',
                               connect_attempts=4)
        self.assertTrue(manager._scan_now.is_set())
        self.assertFalse(manager._degraded)
        manager.record_request(failure_stage='connect', lane='project',
                               connect_attempts=4)
        self.assertTrue(manager._degraded)

    def test_probe_candidate_hits_download_chatgpt_and_codex_path(self):
        calls = []

        def fake(proxy_port, host, method, path, **kwargs):
            calls.append((host, method, path))
            if host == 'speed.cloudflare.com':
                return {'ok': True, 'bytes': 65536, 'status': 200,
                        'effective_throughput_kbps': 40, 'throughput_kbps': 100,
                        'first_byte_ms': 500}
            if host == 'chatgpt.com' and method == 'GET':
                return {'ok': True, 'status': 200, 'total_ms': 700}
            return {'ok': True, 'status': 401, 'total_ms': 600,
                    'upload_seconds': .2}

        with patch.object(oroute, '_proxy_request', side_effect=fake):
            result = oroute.probe_candidate(123, 9)
        self.assertTrue(result['ok'])
        self.assertEqual(calls, [
            ('speed.cloudflare.com', 'GET', '/__down?bytes=65536'),
            ('chatgpt.com', 'GET', '/robots.txt'),
            ('chatgpt.com', 'POST', '/backend-api/codex/responses')])

    def test_state_loader_rejects_stale_or_other_core_snapshots(self):
        root = self.temp_root()
        path = root / 'state/codex-official-route.json'
        value = {'ready': True, 'observed_epoch': time.time(), 'core_pid': 11}
        path.write_text(json.dumps(value), encoding='utf-8')
        self.assertTrue(oroute.load_route_state(root))
        self.assertTrue(oroute.load_route_state(root, core_pid=11))
        self.assertFalse(oroute.load_route_state(root, core_pid=12))
        value['observed_epoch'] = time.time() - 999
        path.write_text(json.dumps(value), encoding='utf-8')
        self.assertFalse(oroute.load_route_state(root, max_age=120))

    def test_upload_budget_is_size_based_and_bounded(self):
        settings = {'CodexOfficialUploadMinSeconds': 30,
                    'CodexOfficialUploadMaxSeconds': 180,
                    'CodexOfficialUploadMinimumKBps': 8}
        self.assertEqual(oroute.upload_budget_seconds(0, settings), 30)
        self.assertEqual(oroute.upload_budget_seconds(1024 * 1024, settings), 128)
        self.assertEqual(oroute.upload_budget_seconds(1024 * 1024 * 100, settings), 180)


if __name__ == '__main__':
    unittest.main()
