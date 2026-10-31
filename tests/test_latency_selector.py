import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

        def request(path, method='GET', body=None, timeout=None):
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
        selector._measure_candidates = lambda candidates: [
            {**item, **quality[item['tag']]} for item in candidates if quality[item['tag']].get('ok')]
        return selector, selected

    def test_quality_floor_skips_a_faster_but_slow_download(self):
        selector, selected = self.selector_with(
            {'a': 200, 'b': 350},
            {'a': {'ok': True, 'throughput_kbps': 20, 'chatgpt_ok': True},
             'b': {'ok': True, 'throughput_kbps': 150, 'chatgpt_ok': True}})
        result = selector.select_once()
        self.assertEqual(result['tag'], 'b')
        self.assertTrue(result['quality_verified'])
        self.assertEqual(selected, ['b'])

    def test_highest_throughput_valid_node_is_kept_when_all_are_slow(self):
        selector, selected = self.selector_with(
            {'a': 200, 'b': 350},
            {'a': {'ok': True, 'throughput_kbps': 20, 'chatgpt_ok': True},
             'b': {'ok': True, 'throughput_kbps': 55, 'chatgpt_ok': True}})
        result = selector.select_once()
        self.assertEqual(result['tag'], 'b')
        self.assertFalse(result['quality_verified'])
        self.assertEqual(selected, ['b'])

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

        def request(path, method='GET', body=None, timeout=None):
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
        selector._measure_candidates = lambda candidates: [
            {**item, **quality[item['tag']]} for item in candidates if quality[item['tag']].get('ok')]
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
        self.assertEqual(selected, ['sg-a'])

    def test_selection_pool_skips_multiplier_and_unlisted_regions(self):
        selector, selected = self.selector_with(
            {'us-a': 200, 'us-b': 250, 'sg-a': 300},
            {'us-a': {'ok': True, 'throughput_kbps': 100, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 120, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 80, 'chatgpt_ok': True}})
        selector.select_once()
        self.assertNotIn('sg-multiplier', selected)
        self.assertNotIn('jp-a', selected)

    def test_selection_pool_respects_rejected_candidate_cooldown(self):
        selector, selected = self.selector_with(
            {'us-a': 200, 'us-b': 250, 'sg-a': 300},
            {'us-a': {'ok': True, 'throughput_kbps': 100, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 120, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 900, 'chatgpt_ok': True}})
        selector.rejected_candidates['sg-a'] = ls.time.time() + 1800
        result = selector.select_once()
        self.assertEqual(result['tag'], 'us-b')
        self.assertEqual(selected, ['us-b'])

    def test_japan_node_can_win_when_configured_in_pool(self):
        selector, selected = self.selector_with(
            {'us-a': 100, 'us-b': 200, 'sg-a': 150, 'jp-a': 130},
            {'us-a': {'ok': True, 'throughput_kbps': 70, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 80, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 90, 'chatgpt_ok': True},
             'jp-a': {'ok': True, 'throughput_kbps': 220, 'chatgpt_ok': True}})
        selector.selection_regions = ['美国', '新加坡', '日本']
        result = selector.select_once()
        self.assertEqual(result['tag'], 'jp-a')
        self.assertEqual(result['region'], '日本')
        self.assertEqual(selected, ['jp-a'])

    def test_quality_stage_is_bounded_by_the_candidate_limit(self):
        selector, _ = self.selector_with(
            {'us-a': 100, 'us-b': 110, 'sg-a': 120, 'jp-a': 130},
            {'us-a': {'ok': True, 'throughput_kbps': 70, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 80, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 90, 'chatgpt_ok': True},
             'jp-a': {'ok': True, 'throughput_kbps': 95, 'chatgpt_ok': True}})
        selector.selection_regions = ['美国', '新加坡', '日本']
        selector.candidate_probe_limit = 2
        captured = {}

        def measure(candidates):
            captured['tags'] = [item['tag'] for item in candidates]
            return [{**item, 'ok': True, 'chatgpt_ok': True,
                     'throughput_kbps': 100} for item in candidates]

        selector._measure_candidates = measure
        selector.select_once()
        self.assertEqual(captured['tags'], ['us-a', 'us-b'])

    def test_recovery_scans_same_us_singapore_pool(self):
        selector, _ = self.selector_with(
            {'us-a': 100, 'us-b': 200, 'sg-a': 150},
            {'us-a': {'ok': True, 'throughput_kbps': 100, 'chatgpt_ok': True},
             'us-b': {'ok': True, 'throughput_kbps': 80, 'chatgpt_ok': True},
             'sg-a': {'ok': True, 'throughput_kbps': 90, 'chatgpt_ok': True}})
        selector.selection_regions = ['美国', '新加坡']
        selector.select_once = Mock(return_value={
            'region': '新加坡', 'group': 'region-sg', 'tag': 'sg-a',
            'ok': True, 'chatgpt_ok': True})
        data = selector.state()
        outcome = selector._recover_current(
            '美国', data['region_groups']['美国'], data, 'us-a',
            {'health': 'blocked', 'throughput_kbps': 0})
        self.assertEqual(outcome['action'], 'switch')
        self.assertEqual(outcome['tag'], 'sg-a')
        self.assertEqual(selector.pending_switch['previous_group'], 'region-us')
        self.assertEqual(selector.pending_switch['switched_group'], 'region-sg')
        selector.select_once.assert_called_once()
        args, kwargs = selector.select_once.call_args
        self.assertEqual(args, ())
        self.assertFalse(kwargs['apply'])
        self.assertEqual([item['tag'] for item in kwargs['candidates']],
                         ['us-a', 'us-b', 'sg-a'])


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
        selector._verify_live_node = Mock(return_value={'ok': True, 'entry': 'us'})
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


class HealthAwareSelectionTests(unittest.TestCase):
    def selector_with(self, current='a', failover_only=False):
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
            'SakuraFailoverOnly': failover_only,
        })
        selected = {'tag': current}
        selector._set_group = Mock(side_effect=lambda group, tag: selected.update(tag=tag))

        def request(path, method='GET', body=None, timeout=None):
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
        self.assertEqual(ls.classify_health(
            {'ok': True, 'complete': True, 'throughput_kbps': 125,
             'effective_throughput_kbps': 27}, 50), 'low')

    def test_effective_speed_is_used_for_selection(self):
        selector, selected = self.selector_with()
        selector.measure = lambda tag: 100
        selector._measure_candidates = lambda candidates: [
            {**candidate, 'ok': True, 'chatgpt_ok': True,
             'throughput_kbps': speed, 'effective_throughput_kbps': effective}
            for candidate, speed, effective in (
                (candidates[0], 400, 35),
                (candidates[1], 150, 90))]
        result = selector.select_once()
        self.assertEqual(result['tag'], 'b')
        self.assertEqual(result['effective_throughput_kbps'], 90)

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

    def test_failover_only_keeps_a_low_connected_route(self):
        selector, _ = self.selector_with(failover_only=True)
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 12, 'health': 'low'})
        selector._recover_current = Mock()
        first = selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(first['failure_count'], 0)
        self.assertEqual(second['failure_count'], 0)
        self.assertIsNone(first['outcome'])
        self.assertIsNone(second['outcome'])
        selector._recover_current.assert_not_called()

    def test_failover_only_still_recovers_when_chatgpt_is_blocked(self):
        selector, _ = self.selector_with(failover_only=True)
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 80, 'health': 'healthy'})
        selector.verify_chatgpt = Mock(return_value={
            'ok': False, 'chatgpt_ok': False, 'chatgpt_error': 'TimeoutError'})
        selector._recover_current = Mock(return_value={'action': 'switch'})
        first = selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(first['failure_count'], 1)
        self.assertEqual(second['failure_count'], 2)
        selector._recover_current.assert_called_once()

    def test_failover_only_full_scans_after_targeted_recovery_fails(self):
        selector, _ = self.selector_with(failover_only=True)
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 80, 'health': 'healthy'})
        selector.verify_chatgpt = Mock(return_value={
            'ok': False, 'chatgpt_ok': False, 'chatgpt_error': 'TimeoutError'})
        selector._recover_current = Mock(return_value={
            'action': 'keep', 'reason': 'no targeted candidate passed'})
        selector.select_once = Mock(return_value={'tag': 'b'})
        selector.health_check_once()
        outcome = selector.health_check_once()
        selector.select_once.assert_called_once_with()
        self.assertEqual(outcome['outcome']['action'], 'switch')
        self.assertEqual(outcome['outcome']['reason'], 'full pool recovery')

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
        selector._verify_live_node = Mock(return_value={'ok': True, 'entry': 'a'})
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

    def test_pending_switch_keeps_low_connected_route_after_an_outage(self):
        selector, _ = self.selector_with(current='b')
        selector.pending_switch = {
            'previous_tag': 'a', 'switched_tag': 'b', 'baseline_kbps': 0,
            'baseline_health': 'disconnected', 'samples': []}
        values = iter([
            {'ok': True, 'complete': True, 'effective_throughput_kbps': 34,
             'throughput_kbps': 90, 'health': 'low'},
            {'ok': True, 'complete': True, 'effective_throughput_kbps': 38,
             'throughput_kbps': 95, 'health': 'low'},
        ])
        selector._small_quality = Mock(side_effect=lambda: next(values))
        selector.health_check_once()
        second = selector.health_check_once()
        self.assertEqual(second['outcome']['action'], 'accept')
        self.assertEqual(second['outcome']['reason'], 'service restored')
        selector._set_group.assert_not_called()

    def test_working_low_route_is_not_replaced_by_another_low_route(self):
        selector, _ = self.selector_with(current='a')
        selector.selection_regions = ['Region']
        data = selector.state()
        selector.select_once = Mock(return_value={
            'region': 'Region', 'group': 'region-group', 'tag': 'b',
            'ok': True, 'complete': True, 'chatgpt_ok': True,
            'effective_throughput_kbps': 42, 'throughput_kbps': 90,
            'health': 'low'})
        baseline = {'health': 'low', 'effective_throughput_kbps': 40,
                    'throughput_kbps': 85}
        outcome = selector._recover_current(
            'Region', data['region_groups']['Region'], data, 'a', baseline)
        self.assertEqual(outcome['action'], 'keep')
        self.assertIn('meaningfully improve', outcome['reason'])
        selector._set_group.assert_called_with('region-group', 'a')

    def test_pending_switch_restores_previous_after_two_weak_samples(self):
        selector, _ = self.selector_with(current='b')
        selector.pending_switch = {
            'previous_tag': 'a', 'switched_tag': 'b', 'baseline_kbps': 18,
            'baseline_health': 'low', 'samples': []}
        selector._small_quality = Mock(return_value={
            'ok': True, 'complete': True, 'throughput_kbps': 22, 'health': 'healthy'})
        selector._verify_live_node = Mock(return_value={'ok': True, 'entry': 'a'})
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


class EntryRetryTests(unittest.TestCase):
    def selector(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('test-only', encoding='utf-8')
        selector = ls.LatencySelector(root, {
            'ControllerPort': 1, 'MixedPort': 2,
            'SakuraMinimumThroughputKBps': 20, 'SakuraTunValidation': False})
        return selector

    def test_measure_retries_other_entries_before_rejecting_the_node(self):
        selector = self.selector()
        selector._entry_tags = lambda tag: ['dead', 'live']

        def request(path, method='GET', body=None, timeout=None):
            if path.startswith('/proxies/dead/'):
                raise OSError('entry down')
            if path.startswith('/proxies/live/'):
                return {'delay': 120}
            raise AssertionError((path, method, body))

        selector.request = request
        self.assertEqual(selector.measure('a'), 120)
        self.assertNotIn('a', selector.rejected_candidates)
        self.assertIn('dead', selector.rejected_entries)

    def test_reject_node_keeps_a_node_when_one_entry_is_still_reachable(self):
        selector = self.selector()
        selector._verify_live_node = Mock(return_value={'ok': True, 'entry': 'live'})
        self.assertFalse(selector._reject_node(
            'a', 'quality sample disconnected', verify_entries=True))
        self.assertNotIn('a', selector.rejected_candidates)
        selector._verify_live_node = Mock(return_value={'ok': False, 'reason': 'no live entry'})
        self.assertTrue(selector._reject_node(
            'a', 'quality sample disconnected', verify_entries=True))
        self.assertIn('a', selector.rejected_candidates)


class RestoreVerificationTests(unittest.TestCase):
    def selector(self):
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
            'SakuraMinimumThroughputKBps': 20, 'SakuraTunValidation': False})
        selector._set_group = Mock()
        selector._set_top_region = Mock()
        return selector

    def pending(self, restore=True):
        decision = {
            'previous_region': 'Region', 'previous_group': 'region-group',
            'previous_tag': 'a', 'switched_region': 'Region',
            'switched_group': 'region-group', 'switched_tag': 'b',
            'baseline_kbps': 0, 'baseline_health': 'disconnected', 'samples': [],
        }
        if restore:
            decision['restore'] = True
        return decision

    def test_unreachable_controller_keeps_the_observation_without_rejecting(self):
        selector = self.selector()
        selector._verify_live_node = Mock(
            return_value={'ok': False, 'reason': 'controller_unreachable'})
        outcome = selector._finish_pending_switch(
            self.pending(), {'health': 'disconnected'}, 'switched node disconnected')
        self.assertEqual(outcome['action'], 'pending')
        self.assertIsNotNone(selector.pending_switch)
        selector._set_group.assert_not_called()
        selector._set_top_region.assert_not_called()
        self.assertNotIn('a', selector.rejected_candidates)

    def test_previous_node_wins_when_it_is_still_reachable(self):
        selector = self.selector()
        selector._verify_live_node = Mock(side_effect=[
            {'ok': True, 'entry': 'a'}, {'ok': False, 'reason': 'no live entry'}])
        outcome = selector._finish_pending_switch(
            self.pending(), {'health': 'blocked', 'chatgpt_ok': False},
            'switched node did not restore ChatGPT service')
        self.assertEqual(outcome['action'], 'restore')
        selector._set_group.assert_called_once_with('region-group', 'a')
        selector._set_top_region.assert_called_once_with('region-group')
        self.assertIn('b', selector.rejected_candidates)
        self.assertNotIn('a', selector.rejected_candidates)

    def test_slow_but_usable_previous_node_is_not_rejected(self):
        selector = self.selector()
        selector._verify_live_node = Mock(side_effect=[
            {'ok': True, 'entry': 'a'}, {'ok': False, 'reason': 'no live entry'}])
        outcome = selector._finish_pending_switch(
            self.pending(), {'health': 'low'}, 'no meaningful improvement')
        self.assertEqual(outcome['action'], 'restore')
        self.assertNotIn('b', selector.rejected_candidates)

    def test_dead_previous_node_keeps_a_verified_switched_node(self):
        selector = self.selector()
        selector._verify_live_node = Mock(side_effect=[
            {'ok': False, 'reason': 'no live entry'}, {'ok': True, 'entry': 'b'}])
        outcome = selector._finish_pending_switch(
            self.pending(), {'health': 'healthy'}, 'previous node failed')
        self.assertEqual(outcome['action'], 'accept')
        self.assertEqual(outcome['tag'], 'b')
        self.assertIn('a', selector.rejected_candidates)
        self.assertNotIn('b', selector.rejected_candidates)
        selector._set_group.assert_not_called()

    def test_two_unverified_nodes_request_immediate_recovery(self):
        selector = self.selector()
        selector._verify_live_node = Mock(
            return_value={'ok': False, 'reason': 'no live entry'})
        selector.recovery_failures = 0
        selector.next_recovery_at = 999
        outcome = selector._finish_pending_switch(
            self.pending(), {'health': 'disconnected'}, 'switched node disconnected')
        self.assertEqual(outcome['action'], 'recover')
        self.assertTrue(outcome['needs_recovery'])
        self.assertEqual(selector.recovery_failures, 1)
        self.assertEqual(selector.next_recovery_at, 0)
        self.assertIn('a', selector.rejected_candidates)
        self.assertIn('b', selector.rejected_candidates)
        self.assertIsNone(selector.pending_switch)

    def test_health_check_runs_recovery_immediately_after_both_fail(self):
        selector = self.selector()
        selector.request = Mock(return_value={'type': 'Selector', 'now': 'sakura'})
        selector._region_context = Mock(return_value=(
            'Region', {'group': 'region-group', 'nodes': ['a', 'b']},
            selector.state(), 'b'))
        selector.pending_switch = self.pending()
        selector._small_quality = Mock(return_value={
            'ok': False, 'complete': False, 'throughput_kbps': 0,
            'health': 'disconnected'})
        selector._verify_live_node = Mock(
            return_value={'ok': False, 'reason': 'no live entry'})
        selector._recover_current = Mock(return_value={'action': 'switch', 'tag': 'c'})
        outcome = selector.health_check_once()
        self.assertTrue(outcome['outcome']['needs_recovery'])
        self.assertEqual(outcome['outcome']['recovery'],
                         {'action': 'switch', 'tag': 'c'})
        selector._recover_current.assert_called_once()


class SnapshotAndRestartTests(unittest.TestCase):
    def selector(self, config=None, failover_only=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('test-only', encoding='utf-8')
        (root / 'state/sakura-outbounds.json').write_text(json.dumps({
            'names': {'a': 'Node A', 'b': 'Node B'},
            'region_groups': {'Region': {'group': 'region-group', 'nodes': ['a', 'b']}},
        }), encoding='utf-8')
        if config is not None:
            (root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        return ls.LatencySelector(root, {
            'ControllerPort': 1, 'MixedPort': 2,
            'SakuraMinimumThroughputKBps': 20, 'SakuraTunValidation': False,
            'SakuraFailoverOnly': failover_only})

    def test_dry_run_selection_never_writes_the_live_snapshot(self):
        selector = self.selector()
        selector.request = Mock(side_effect=[
            {'type': 'Selector', 'now': 'region-group'},
            {'now': 'a'}])
        selector.measure = lambda tag: 100
        selector._measure_candidates = lambda candidates: [
            {**item, 'ok': True, 'chatgpt_ok': True, 'throughput_kbps': 100}
            for item in candidates]
        result = selector.select_once(apply=False)
        self.assertIsNotNone(result)
        self.assertFalse((selector.root / 'state/sakura-selection.json').exists())

    def test_failed_scan_keeps_the_previous_shortlist(self):
        selector = self.selector()
        path = selector.root / 'state/sakura-selection.json'
        path.write_text(json.dumps({
            'candidates': [{'tag': 'b', 'score': 0.5}],
            'candidate_tag': 'b'}), encoding='utf-8')
        selector.request = Mock(side_effect=[
            {'type': 'Selector', 'now': 'region-group'},
            {'now': 'a'}])
        selector.measure = Mock(side_effect=ValueError('all entries failed'))
        self.assertIsNone(selector.select_once())
        snapshot = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(snapshot['stale_candidates'])
        self.assertEqual([item['tag'] for item in snapshot['candidates']], ['b'])
        self.assertEqual(snapshot.get('candidate_tag'), 'b')

    def test_quality_stage_skips_candidates_missing_from_the_generated_config(self):
        selector = self.selector({'outbounds': [{'tag': 'a'}, {'tag': 'b'}]})
        selector._candidate_quality = Mock(return_value={
            'ok': True, 'chatgpt_ok': True, 'throughput_kbps': 100})
        context = Mock()
        context.__enter__ = Mock(return_value=SimpleNamespace(ports={'a': 1}))
        context.__exit__ = Mock(return_value=False)
        with patch('latency_selector.IsolatedProbePool', return_value=context) as pool:
            result = selector._measure_candidates([{'tag': 'a'}, {'tag': 'missing'}])
        self.assertEqual([item['tag'] for item in result], ['a'])
        self.assertEqual(pool.call_args[0][1], ['a'])

    def test_scan_budget_stops_the_quality_stage(self):
        selector = self.selector({'outbounds': [{'tag': 'a'}, {'tag': 'b'}]})
        selector.scan_budget_seconds = 10
        selector._candidate_quality = Mock(return_value={
            'ok': True, 'chatgpt_ok': True, 'throughput_kbps': 100})
        context = Mock()
        context.__enter__ = Mock(return_value=SimpleNamespace(ports={'a': 1, 'b': 2}))
        context.__exit__ = Mock(return_value=False)
        with patch('latency_selector.IsolatedProbePool', return_value=context), \
                patch('latency_selector.time.monotonic',
                      side_effect=[0.0, 0.0, 11.0, 12.0]):
            result = selector._measure_candidates([{'tag': 'a'}, {'tag': 'b'}])
        self.assertEqual([item['tag'] for item in result], ['a'])

    def test_legacy_rejection_ttl_is_clamped_to_the_configured_window(self):
        selector = self.selector()
        now = ls.time.time()
        (selector.root / 'state/sakura-health.json').write_text(json.dumps({
            'rejected_candidates': {'a': now + 86400}}), encoding='utf-8')
        reloaded = ls.LatencySelector(selector.root, {
            'ControllerPort': 1, 'MixedPort': 2, 'SakuraRejectionSeconds': 300})
        self.assertLessEqual(reloaded.rejected_candidates['a'], now + 300 + 5)
        self.assertGreater(reloaded.rejected_candidates['a'], now)

    def test_initial_refresh_resumes_pending_observation_then_scans(self):
        selector = self.selector()
        selector.pending_switch = {'previous_tag': 'a', 'switched_tag': 'b'}
        selector.health_check_once = Mock(return_value={'outcome': {'action': 'observe'}})
        selector.select_once = Mock(return_value={'tag': 'b'})
        selector.stop.wait = Mock(return_value=False)
        self.assertEqual(selector._initial_refresh(), {'tag': 'b'})
        selector.health_check_once.assert_called_once_with()
        selector.stop.wait.assert_called_once_with(30)
        selector.select_once.assert_called_once_with()

    def test_initial_refresh_without_pending_switch_scans_immediately(self):
        selector = self.selector()
        selector.health_check_once = Mock()
        selector.select_once = Mock(return_value={'tag': 'a'})
        selector.stop.wait = Mock()
        self.assertEqual(selector._initial_refresh(), {'tag': 'a'})
        selector.health_check_once.assert_not_called()
        selector.stop.wait.assert_not_called()

    def test_failover_only_initial_refresh_keeps_a_working_route(self):
        selector = self.selector(failover_only=True)
        result = {'current': {'health': 'low'}, 'outcome': None}
        selector.health_check_once = Mock(return_value=result)
        selector.select_once = Mock()
        self.assertEqual(selector._initial_refresh(), result)
        selector.health_check_once.assert_called_once_with()
        selector.select_once.assert_not_called()

    def test_failover_only_initial_refresh_scans_when_recovery_kept_a_bad_route(self):
        selector = self.selector(failover_only=True)
        selector.health_check_once = Mock(return_value={
            'current': {'health': 'disconnected'},
            'outcome': {'action': 'keep'}})
        selector.select_once = Mock(return_value={'tag': 'b'})
        self.assertEqual(selector._initial_refresh(), {'tag': 'b'})
        selector.select_once.assert_called_once_with()

    def test_failover_only_initial_refresh_scans_on_first_blocked_sample(self):
        selector = self.selector(failover_only=True)
        selector.health_check_once = Mock(return_value={
            'current': {'health': 'blocked'}, 'outcome': None})
        selector.select_once = Mock(return_value={'tag': 'b'})
        self.assertEqual(selector._initial_refresh(), {'tag': 'b'})
        selector.select_once.assert_called_once_with()

    def test_failover_only_resumes_pending_without_a_full_scan(self):
        selector = self.selector(failover_only=True)
        selector.pending_switch = {'previous_tag': 'a', 'switched_tag': 'b'}
        result = {'outcome': {'action': 'observe'}}
        selector.health_check_once = Mock(return_value=result)
        selector.select_once = Mock()
        selector.stop.wait = Mock(return_value=False)
        self.assertEqual(selector._initial_refresh(), result)
        self.assertEqual(selector.health_check_once.call_count, 2)
        selector.select_once.assert_not_called()

    def test_failover_only_skips_periodic_full_scans(self):
        selector = self.selector(failover_only=True)
        selector._initial_refresh = Mock(return_value={'current': {'health': 'healthy'}})
        selector.select_once = Mock()
        selector.health_check_once = Mock()
        waits = []

        class Stop:
            def is_set(self):
                return len(waits) >= 3

            def wait(self, seconds):
                waits.append(seconds)
                return self.is_set()

        selector.stop = Stop()
        with patch.object(ls.time, 'monotonic', side_effect=[0.0, 0.0, 7200.0, 7200.0]):
            selector.run()
        selector.select_once.assert_not_called()
        selector._initial_refresh.assert_called_once_with()
        selector.health_check_once.assert_called_once_with()
        self.assertEqual(waits, [30, 30, 30])

    def test_periodic_full_scans_still_run_without_failover_only(self):
        selector = self.selector()
        selector._initial_refresh = Mock(return_value={'tag': 'a'})
        selector.select_once = Mock(return_value={'tag': 'a'})
        selector.health_check_once = Mock()
        selector._try_preferred_region = Mock(return_value=None)
        waits = []

        class Stop:
            def is_set(self):
                return len(waits) >= 2

            def wait(self, seconds):
                waits.append(seconds)
                return self.is_set()

        selector.stop = Stop()
        with patch.object(ls.time, 'monotonic', side_effect=[0.0, 0.0, 7200.0, 7200.0]):
            selector.run()
        selector.select_once.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
