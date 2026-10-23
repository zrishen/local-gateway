import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

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
            self.assertEqual(selector.minimum_throughput_kbps, 50)
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
            if path == '/proxies/sakura' and method == 'GET':
                return {'type': 'Selector', 'now': 'region-group'}
            if path == '/proxies/sakura' and method == 'PUT':
                return None
            if path == '/proxies/region-group' and method == 'GET':
                return {'now': selected[-1] if selected else 'a'}
            if path == '/proxies/region-group' and method == 'PUT':
                if not selected or selected[-1] != body['name']:
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


class SelectionPoolTests(unittest.TestCase):
    def selector_with(self, delays, quality):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('test-only', encoding='utf-8')
        (root / 'state/sakura-outbounds.json').write_text(json.dumps({
            'names': {
                'us-a': 'US A', 'us-b': 'US B', 'sg-a': 'SG A',
                'sg-multiplier': 'SG Multiplier 5倍消耗', 'jp-a': 'JP A',
            },
            'region_groups': {
                '美国': {'group': 'region-us', 'nodes': ['us-a', 'us-b']},
                '新加坡': {'group': 'region-sg', 'nodes': ['sg-a', 'sg-multiplier']},
                '日本': {'group': 'region-jp', 'nodes': ['jp-a']},
            },
        }), encoding='utf-8')
        selector = ls.LatencySelector(root, {
            'ControllerPort': 1, 'MixedPort': 2,
            'SakuraRegion': '美国',
            'SakuraSelectionRegions': ['美国', '新加坡'],
            'SakuraMinimumThroughputKBps': 20,
            'SakuraTunValidation': False,
        })
        selected = []
        region = {'name': 'region-us'}

        def request(path, method='GET', body=None):
            if path == '/proxies/sakura' and method == 'GET':
                return {'type': 'Selector', 'now': region['name']}
            if path == '/proxies/sakura' and method == 'PUT':
                region['name'] = body['name']
                return None
            if path.startswith('/proxies/region-') and method == 'GET':
                return {'now': selected[-1] if selected else 'us-a'}
            if path.startswith('/proxies/region-') and method == 'PUT':
                if not selected or selected[-1] != body['name']:
                    selected.append(body['name'])
                return None
            raise AssertionError((path, method, body))

        selector.request = request
        selector.measure = lambda tag: delays[tag]
        selector.verify_through_proxy = lambda: quality[selected[-1]]
        return selector, selected

    def test_fastest_verified_node_wins_across_us_and_singapore(self):
        selector, selected = self.selector_with(
            {'us-a': 120, 'us-b': 500, 'sg-a': 180},
            {'us-a': {'ok': True, 'throughput_kbps': 70, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 90, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 180, 'chatgpt_ok': True}})
        result = selector.select_once()
        self.assertEqual(result['tag'], 'sg-a')
        self.assertEqual(result['region'], '新加坡')
        self.assertEqual(result['regions'], ['美国', '新加坡'])
        self.assertEqual(selected, ['us-a', 'sg-a', 'us-b', 'sg-a'])

    def test_selection_pool_skips_multiplier_and_unlisted_regions(self):
        selector, selected = self.selector_with(
            {'us-a': 200, 'us-b': 250, 'sg-a': 300},
            {'us-a': {'ok': True, 'throughput_kbps': 100, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 120, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 80, 'chatgpt_ok': True}})
        selector.select_once()
        self.assertNotIn('sg-multiplier', selected)
        self.assertNotIn('jp-a', selected)

    def test_recovery_scans_same_us_singapore_pool(self):
        selector, _ = self.selector_with(
            {'us-a': 100, 'us-b': 200, 'sg-a': 150},
            {'us-a': {'ok': True, 'throughput_kbps': 100, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 80, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 90, 'chatgpt_ok': True}})
        selector.select_once = Mock(return_value={
            'region': '新加坡', 'group': 'region-sg', 'tag': 'sg-a'})
        data = selector.state()
        outcome = selector._recover_current(
            '美国', data['region_groups']['美国'], data, 'us-a',
            {'health': 'blocked', 'throughput_kbps': 0})
        self.assertEqual(outcome['action'], 'switch')
        self.assertEqual(outcome['tag'], 'sg-a')
        self.assertEqual(selector.pending_switch['previous_group'], 'region-us')
        self.assertEqual(selector.pending_switch['switched_group'], 'region-sg')
        selector.select_once.assert_called_once_with()


class CrossRegionFallbackTests(unittest.TestCase):
    def selector_with(self, cross_region=True, current_region='美国'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('test-only', encoding='utf-8')
        data = {
            'names': {'us': 'US Node', 'kr': 'KR Node', 'jp': 'JP Node'},
            'region_groups': {
                '美国': {'group': 'region-us', 'nodes': ['us']},
                '韩国': {'group': 'region-kr', 'nodes': ['kr']},
                '日本': {'group': 'region-jp', 'nodes': ['jp']},
            },
        }
        (root / 'state/sakura-outbounds.json').write_text(json.dumps(data), encoding='utf-8')
        selector = ls.LatencySelector(root, {
            'ControllerPort': 1, 'MixedPort': 2,
            'SakuraRegion': '美国',
            'SakuraCrossRegionFallback': cross_region,
            'SakuraMinimumThroughputKBps': 20,
            'SakuraTunValidation': False,
        })
        selector._set_top_region = Mock()
        selector._set_group = Mock()
        return selector, data

    def test_cross_region_is_disabled_by_setting(self):
        selector, data = self.selector_with(cross_region=False)
        self.assertEqual(selector._fallback_region_names(data, '美国'), [])

    def test_preferred_fallback_node_wins_even_with_higher_control_delay(self):
        selector, data = self.selector_with()
        selector.preferred_fallback_nodes = ['kr']
        selector._chatgpt_probe = Mock(side_effect={'kr': 700, 'jp': 100}.get)
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 80, 'health': 'healthy'})
        selector.verify_chatgpt = Mock(return_value={
            'ok': True, 'chatgpt_ok': True, 'chatgpt_status': 200})
        chosen = selector._find_cross_region_candidate(data, '美国')
        self.assertEqual(chosen['tag'], 'kr')
        self.assertTrue(chosen['preferred'])

    def test_multiplier_nodes_are_not_used_as_preferred_fallbacks(self):
        selector, data = self.selector_with()
        data['names']['kr'] = 'KR Node 5倍消耗'
        selector.preferred_fallback_nodes = ['kr']
        selector._chatgpt_probe = Mock(side_effect={'jp': 200}.get)
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 80, 'health': 'healthy'})
        selector.verify_chatgpt = Mock(return_value={
            'ok': True, 'chatgpt_ok': True, 'chatgpt_status': 200})
        chosen = selector._find_cross_region_candidate(data, '美国')
        self.assertEqual(chosen['tag'], 'jp')
        selector._chatgpt_probe.assert_called_once_with('jp')

    def test_only_uses_another_region_after_local_candidates_fail(self):
        selector, data = self.selector_with()
        selector._chatgpt_probe = Mock(side_effect={'kr': 300, 'jp': 700}.get)
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 90, 'health': 'healthy'})
        selector.verify_chatgpt = Mock(return_value={
            'ok': True, 'chatgpt_ok': True, 'chatgpt_status': 200})
        chosen = selector._find_cross_region_candidate(data, '美国')
        self.assertEqual((chosen['region'], chosen['tag']), ('韩国', 'kr'))
        selector._set_top_region.assert_called_once_with('region-kr')
        selector._set_group.assert_called_once_with('region-kr', 'kr')

    def test_cross_region_switch_records_both_groups_for_restore(self):
        selector, data = self.selector_with()
        info = data['region_groups']['美国']
        selector._find_recovery_candidate = Mock(return_value=None)
        selector._find_cross_region_candidate = Mock(return_value={
            'region': '韩国', 'group': 'region-kr', 'tag': 'kr', 'name': 'KR Node',
            'delay': 300, 'throughput_kbps': 90, 'health': 'healthy'})
        result = selector._recover_current(
            '美国', info, data, 'us', {'health': 'blocked', 'throughput_kbps': 0})
        self.assertEqual(result['action'], 'switch')
        self.assertEqual(selector.pending_switch['previous_group'], 'region-us')
        self.assertEqual(selector.pending_switch['switched_group'], 'region-kr')
        self.assertTrue(selector.pending_switch['cross_region'])

    def test_rejected_cross_region_node_restores_both_selectors(self):
        selector, _ = self.selector_with()
        selector.pending_switch = {
            'previous_region': '美国', 'previous_group': 'region-us', 'previous_tag': 'us',
            'switched_region': '韩国', 'switched_group': 'region-kr', 'switched_tag': 'kr',
            'baseline_kbps': 0, 'baseline_health': 'blocked', 'samples': [40, 50],
        }
        outcome = selector._finish_pending_switch(
            {**selector.pending_switch, 'restore': True},
            {'health': 'healthy', 'throughput_kbps': 50, 'chatgpt_ok': True},
            'no meaningful improvement')
        self.assertEqual(outcome['action'], 'restore')
        selector._set_top_region.assert_called_with('region-us')
        selector._set_group.assert_called_with('region-us', 'us')

    def test_primary_region_is_tried_before_other_fallbacks(self):
        selector, data = self.selector_with(current_region='韩国')
        names = selector._fallback_region_names(data, '韩国')
        self.assertEqual(names[0], '美国')

    def test_returning_to_primary_creates_cross_region_observation(self):
        selector, data = self.selector_with(current_region='韩国')
        selector.request = Mock(return_value={'now': 'region-kr'})
        selector._region_context = Mock(return_value=(
            '韩国', data['region_groups']['韩国'], data, 'kr'))
        selector._find_cross_region_candidate = Mock(return_value={
            'region': '美国', 'group': 'region-us', 'tag': 'us', 'name': 'US Node',
            'delay': 500, 'throughput_kbps': 80, 'health': 'healthy'})
        outcome = selector._try_preferred_region()
        self.assertEqual(outcome['action'], 'switch')
        self.assertEqual(selector.pending_switch['previous_group'], 'region-kr')
        self.assertEqual(selector.pending_switch['switched_group'], 'region-us')
        self.assertTrue(selector.pending_switch['cross_region'])



if __name__ == '__main__':
    unittest.main()

class HealthAwareSelectionTests(unittest.TestCase):
    def selector_with(self, current='a'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('test-only', encoding='utf-8')
        (root / 'state/sakura-outbounds.json').write_text(json.dumps({
            'names': {'a': 'Node A', 'b': 'Node B'},
            'region_groups': {'Region': {'group': 'region-group', 'nodes': ['a', 'b']}},
        }), encoding='utf-8')
        selector = ls.LatencySelector(root, {
            'ControllerPort': 1, 'MixedPort': 2,
            'SakuraMinimumThroughputKBps': 20,
            'SakuraTunValidation': False,
        })
        selected = {'tag': current}
        selector._set_group = Mock(side_effect=lambda group, tag: selected.update(tag=tag))

        def request(path, method='GET', body=None):
            if path == '/proxies/sakura':
                return {'type': 'Selector', 'now': 'region-group'}
            if path == '/proxies/region-group' and method == 'GET':
                return {'now': selected['tag']}
            raise AssertionError((path, method, body))

        selector.request = request
        selector.verify_chatgpt = Mock(return_value={
            'ok': True, 'chatgpt_ok': True, 'chatgpt_status': 200})
        return selector, selected

    def test_health_classification(self):
        minimum = 20
        self.assertEqual(ls.classify_health(
            {'ok': True, 'complete': True, 'throughput_kbps': 25}, minimum), 'healthy')
        self.assertEqual(ls.classify_health(
            {'ok': True, 'complete': True, 'throughput_kbps': 12}, minimum), 'low')
        self.assertEqual(ls.classify_health(
            {'ok': False, 'complete': False, 'throughput_kbps': 0}, minimum), 'disconnected')

    def test_two_low_samples_are_required_before_switch(self):
        selector, _ = self.selector_with()
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 12, 'health': 'low'})
        def recover(*args, **kwargs):
            selector.health_failures = 0
            return {'action': 'switch'}
        selector._recover_current = Mock(side_effect=recover)
        first = selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(first['failure_count'], 1)
        self.assertEqual(second['failure_count'], 0)
        self.assertEqual(selector._recover_current.call_count, 1)

    def test_disconnected_current_triggers_recovery_immediately(self):
        selector, _ = self.selector_with()
        selector._small_quality = Mock(return_value={
            'ok': False, 'complete': False, 'throughput_kbps': 0, 'health': 'disconnected'})
        selector._recover_current = Mock(return_value={'action': 'switch'})
        outcome = selector.health_check_once()
        self.assertEqual(outcome['outcome']['action'], 'switch')
        self.assertEqual(selector._recover_current.call_count, 1)
        selector.verify_chatgpt.assert_not_called()

    def test_chatgpt_blocked_requires_two_checks(self):
        selector, _ = self.selector_with()
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 80, 'health': 'healthy'})
        selector.verify_chatgpt = Mock(return_value={
            'ok': False, 'chatgpt_ok': False, 'chatgpt_error': 'TimeoutError'})
        def recover(*args, **kwargs):
            selector.health_failures = 0
            return {'action': 'switch'}
        selector._recover_current = Mock(side_effect=recover)
        first = selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(first['current']['health'], 'blocked')
        self.assertTrue(first['current']['chatgpt_ok'] is False)
        self.assertEqual(first['failure_count'], 1)
        self.assertEqual(selector._recover_current.call_count, 1)

    def test_pending_switch_restores_when_chatgpt_stays_blocked(self):
        selector, _ = self.selector_with(current='b')
        selector.pending_switch = {
            'previous_tag': 'a', 'switched_tag': 'b', 'baseline_kbps': 30,
            'baseline_health': 'blocked', 'samples': []}
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 120, 'health': 'healthy'})
        selector.verify_chatgpt = Mock(return_value={
            'ok': False, 'chatgpt_ok': False, 'chatgpt_error': 'TimeoutError'})
        selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(second['outcome']['action'], 'restore')
        self.assertEqual(second['outcome']['reason'],
                         'switched node did not restore ChatGPT service')
        selector._set_group.assert_called_with('region-group', 'a')

    def test_candidate_must_be_meaningfully_faster(self):
        baseline = {'health': 'low', 'throughput_kbps': 20}
        self.assertTrue(ls.LatencySelector._meaningfully_better(
            {'health': 'healthy', 'throughput_kbps': 42}, baseline))
        self.assertFalse(ls.LatencySelector._meaningfully_better(
            {'health': 'healthy', 'throughput_kbps': 27}, baseline))

    def test_pending_switch_restores_previous_after_two_weak_samples(self):
        selector, _ = self.selector_with(current='b')
        selector.pending_switch = {
            'previous_tag': 'a', 'switched_tag': 'b', 'baseline_kbps': 18,
            'baseline_health': 'low', 'samples': []}
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 22, 'health': 'healthy'})
        first = selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(first['outcome']['action'], 'observe')
        self.assertEqual(second['outcome']['action'], 'restore')
        selector._set_group.assert_called_with('region-group', 'a')

    def test_pending_switch_is_accepted_after_two_improved_samples(self):
        selector, _ = self.selector_with(current='b')
        selector.pending_switch = {
            'previous_tag': 'a', 'switched_tag': 'b', 'baseline_kbps': 18,
            'baseline_health': 'low', 'samples': []}
        values = iter([
            {'ok': True, 'complete': True, 'throughput_kbps': 55, 'health': 'healthy'},
            {'ok': True, 'complete': True, 'throughput_kbps': 65, 'health': 'healthy'},
        ])
        selector._small_quality = Mock(side_effect=lambda: next(values))
        selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(second['outcome']['action'], 'accept')
        selector._set_group.assert_not_called()


if __name__ == '__main__':
    unittest.main()
