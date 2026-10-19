import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import latency_selector as ls


class PickLowestTests(unittest.TestCase):
    def test_picks_smallest_positive_delay(self):
        results = [
            {'tag': 'sakura-000', 'delay': 0},
            {'tag': 'sakura-001', 'delay': 812},
            {'tag': 'sakura-002', 'delay': 645},
            {'tag': 'sakura-003', 'delay': None},
            {'tag': 'sakura-004', 'delay': 645},
        ]
        self.assertEqual(ls.pick_lowest(results)['tag'], 'sakura-002')

    def test_returns_none_without_positive_delay(self):
        self.assertIsNone(ls.pick_lowest([{'tag': 'sakura-000', 'delay': 0}]))

    def test_interval_defaults_come_from_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'state').mkdir()
            (root / 'state/controller-secret.txt').write_text('test-only', encoding='utf-8')
            settings = {'ControllerPort': 12540, 'MixedPort': 12550,
                        'SakuraLatencyIntervalMinutes': 30,
                        'SakuraHealthCheckMinutes': 5}
            selector = ls.LatencySelector(root, settings)
            self.assertEqual(selector.interval, 1800)
            self.assertEqual(selector.health_interval, 300)
            overridden = ls.LatencySelector(root, settings, interval=120)
            self.assertEqual(overridden.interval, 120)


class SelectionQualityTests(unittest.TestCase):
    def selector_with(self, delays, quality):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('test-only', encoding='utf-8')
        (root / 'state/sakura-outbounds.json').write_text(
            json.dumps({
                'names': {'a': 'Node A', 'b': 'Node B'},
                'region_groups': {'Region': {'group': 'region-group', 'nodes': ['a', 'b']}},
            }), encoding='utf-8')
        selector = ls.LatencySelector(root, {'ControllerPort': 1, 'MixedPort': 2,
                                             'SakuraMinimumThroughputKBps': 96,
                                             'SakuraTunValidation': False})
        selected = []

        def request(path, method='GET', body=None):
            if path == '/proxies/sakura':
                return {'type': 'Selector', 'now': 'region-group'}
            if path == '/proxies/region-group' and method == 'GET':
                return {'now': selected[-1] if selected else 'a'}
            if path == '/proxies/region-group' and method == 'PUT':
                selected.append(body['name'])
                return None
            raise AssertionError((path, method, body))

        selector.request = request
        selector.measure = lambda tag: delays[tag]
        selector.verify_through_proxy = lambda: quality[selected[-1]]
        return selector, selected

    def test_quality_floor_skips_a_faster_but_slow_download(self):
        selector, selected = self.selector_with(
            {'a': 200, 'b': 350},
            {'a': {'ok': True, 'throughput_kbps': 20, 'chatgpt_ok': True},
             'b': {'ok': True, 'throughput_kbps': 150, 'chatgpt_ok': True}})
        result = selector.select_once()
        self.assertEqual(result['tag'], 'b')
        self.assertTrue(result['quality_verified'])
        self.assertEqual(selected, ['a', 'b'])

    def test_highest_throughput_valid_node_is_kept_when_all_are_slow(self):
        selector, selected = self.selector_with(
            {'a': 200, 'b': 350},
            {'a': {'ok': True, 'throughput_kbps': 20, 'chatgpt_ok': True},
             'b': {'ok': True, 'throughput_kbps': 30, 'chatgpt_ok': True}})
        result = selector.select_once()
        self.assertEqual(result['tag'], 'b')
        self.assertFalse(result['quality_verified'])
        self.assertEqual(selected, ['a', 'b'])

    def test_chatgpt_reachable_node_beats_a_faster_blocked_download(self):
        selector, selected = self.selector_with(
            {'a': 100, 'b': 400},
            {'a': {'ok': True, 'throughput_kbps': 500, 'chatgpt_ok': False},
             'b': {'ok': True, 'throughput_kbps': 150, 'chatgpt_ok': True}})
        result = selector.select_once()
        self.assertEqual(result['tag'], 'b')
        self.assertTrue(result['quality_verified'])


    def test_tun_finalist_with_chatgpt_access_wins(self):
        selector, _ = self.selector_with(
            {'a': 100, 'b': 400},
            {'a': {'ok': True, 'throughput_kbps': 500, 'chatgpt_ok': True},
             'b': {'ok': True, 'throughput_kbps': 300, 'chatgpt_ok': True}})
        selector.tun_validation = True
        actual = {
            'a': {'tun_download_ok': True, 'tun_throughput_kbps': 5,
                  'tun_chatgpt_ok': False},
            'b': {'tun_download_ok': True, 'tun_throughput_kbps': 150,
                  'tun_chatgpt_ok': True},
        }
        selector.verify_via_tun = lambda tag, group: actual[tag]
        result = selector.select_once()
        self.assertEqual(result['tag'], 'b')
        self.assertTrue(result['tun_verified'])
        self.assertTrue(result['quality_verified'])


if __name__ == '__main__':
    unittest.main()