import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import route_policy as rp


def result(effective, body=None, first=900, chatgpt=True, ok=True,
           complete=True, stability=1.0):
    value = {
        'ok': ok, 'complete': complete, 'chatgpt_ok': chatgpt,
        'effective_throughput_kbps': effective,
        'throughput_kbps': effective if body is None else body,
        'first_byte_ms': first, 'stability': stability,
    }
    return value


class AssessmentTests(unittest.TestCase):
    def test_fast_node_is_healthy(self):
        checked = rp.assess(result(33.2, 90.9, 1089))
        self.assertEqual(checked['health'], 'healthy')
        self.assertGreaterEqual(checked['score'], 0.43)
        self.assertIn('healthy', checked['reasons'])

    def test_slow_node_is_low(self):
        checked = rp.assess(result(12.7, 42.3, 1500))
        self.assertEqual(checked['health'], 'low')
        self.assertIn('score_below_healthy', checked['reasons'])

    def test_effective_floor_rejects_a_high_body_score(self):
        checked = rp.assess(result(10, 500, 200))
        self.assertEqual(checked['health'], 'low')
        self.assertIn('effective_below_floor', checked['reasons'])

    def test_body_floor_rejects_a_fast_first_byte(self):
        checked = rp.assess(result(40, 10, 200))
        self.assertEqual(checked['health'], 'low')
        self.assertIn('body_below_floor', checked['reasons'])

    def test_chatgpt_blocked_is_not_a_speed_problem(self):
        checked = rp.assess(result(500, 500, 100, chatgpt=False))
        self.assertEqual(checked['health'], 'blocked')
        self.assertEqual(checked['score'], 0.0)

    def test_disconnected_is_not_a_speed_problem(self):
        checked = rp.assess(result(0, 0, 0, ok=False, complete=False))
        self.assertEqual(checked['health'], 'disconnected')

    def test_stability_lowers_the_score(self):
        stable = rp.assess(result(40, 80, 500, stability=1.0))
        unstable = rp.assess(result(40, 80, 500, stability=0.2))
        self.assertGreater(stable['score'], unstable['score'])


class BetterTests(unittest.TestCase):
    def test_low_route_can_be_replaced_by_a_much_faster_low_route(self):
        self.assertTrue(rp.is_meaningfully_better(
            result(33.2, 90.9, 1089), result(12.7, 42.3, 1500)))

    def test_two_similar_low_routes_do_not_oscillate(self):
        self.assertFalse(rp.is_meaningfully_better(
            result(42, 105, 1000), result(40, 100, 1000)))

    def test_healthy_route_needs_a_real_advantage(self):
        self.assertFalse(rp.is_meaningfully_better(
            result(40, 100, 1000), result(37, 95, 1000)))
        self.assertTrue(rp.is_meaningfully_better(
            result(62, 160, 800), result(37, 95, 1000)))

    def test_any_working_candidate_recovers_an_outage(self):
        self.assertTrue(rp.is_meaningfully_better(
            result(8, 20, 2000), result(0, 0, 0, ok=False)))

    def test_blocked_candidate_is_never_selected(self):
        self.assertFalse(rp.is_meaningfully_better(
            result(500, 500, 100, chatgpt=False), result(0, 0, 0, ok=False)))


class PendingTests(unittest.TestCase):
    def test_low_but_faster_samples_are_accepted(self):
        baseline = result(18, 40, 1500)
        samples = [result(55, 70, 1200), result(65, 80, 1100)]
        accepted, reason = rp.pending_decision(baseline, samples, samples[-1])
        self.assertTrue(accepted)
        self.assertEqual(reason, 'throughput improved')

    def test_similar_weak_samples_restore_the_previous_route(self):
        baseline = result(18, 40, 1500)
        samples = [result(22, 42, 1500), result(23, 44, 1500)]
        accepted, reason = rp.pending_decision(baseline, samples, samples[-1])
        self.assertFalse(accepted)
        self.assertEqual(reason, 'no meaningful improvement')

    def test_outage_recovery_accepts_a_connected_low_route(self):
        baseline = result(0, 0, 0, ok=False, complete=False)
        current = result(34, 90, 1500)
        accepted, reason = rp.pending_decision(baseline, [current], current)
        self.assertTrue(accepted)
        self.assertEqual(reason, 'service restored')

    def test_blocked_switched_node_is_restored(self):
        baseline = result(20, 50, 1500)
        current = result(120, 200, 500, chatgpt=False)
        accepted, reason = rp.pending_decision(baseline, [current], current)
        self.assertFalse(accepted)
        self.assertEqual(reason, 'switched node did not restore ChatGPT service')


class CompatibilityTests(unittest.TestCase):
    def test_legacy_threshold_classification(self):
        self.assertEqual(rp.classify_health(result(25, 25), 20), 'healthy')
        self.assertEqual(rp.classify_health(result(12, 12), 20), 'low')
        self.assertEqual(rp.classify_health(
            result(0, 0, ok=False, complete=False), 20), 'disconnected')
        self.assertEqual(rp.classify_health(
            result(27, 125), 50), 'low')

    def test_policy_reads_new_keys_and_falls_back_to_legacy(self):
        policy = rp.policy_from_settings({'SakuraMinimumThroughputKBps': 96})
        self.assertEqual(policy.minimum_effective_kbps, 96)
        policy = rp.policy_from_settings({
            'SakuraMinimumThroughputKBps': 96,
            'SakuraMinimumEffectiveKBps': 15,
            'SakuraMinimumBodyKBps': 30,
        })
        self.assertEqual(policy.minimum_effective_kbps, 15)
        self.assertEqual(policy.minimum_body_kbps, 30)

    def test_backoff_is_bounded(self):
        policy = rp.RoutePolicy(recovery_backoff_seconds=(60, 120, 300))
        self.assertEqual(rp.backoff_seconds(1, policy), 60)
        self.assertEqual(rp.backoff_seconds(2, policy), 120)
        self.assertEqual(rp.backoff_seconds(20, policy), 300)


if __name__ == '__main__':
    unittest.main()
