import datetime
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from core_refresh import active_request_count, active_request_stats, reload_decision


class CoreRefreshTests(unittest.TestCase):
    def health(self, state, failures=0, recovery_failures=0):
        return {'last_health': {'health': state, 'checked_at': datetime.datetime.fromtimestamp(
            1000, datetime.timezone.utc).isoformat()},
            'consecutive_below_threshold': failures,
            'recovery_failures': recovery_failures}

    def test_healthy_route_or_active_ai_always_defers_refresh(self):
        self.assertFalse(reload_decision(self.health('healthy'), 0, now=1001)['reload'])
        self.assertFalse(reload_decision(self.health('disconnected'), 1, now=1001)['reload'])
        self.assertFalse(reload_decision(self.health('disconnected'), None, now=1001)['reload'])

    def test_reload_only_after_recovery_failure_with_fresh_health(self):
        self.assertFalse(reload_decision(self.health('low', 1), 0, now=1001)['reload'])
        self.assertTrue(reload_decision(self.health('low', 2), 0, now=1001)['reload'])
        self.assertTrue(reload_decision(self.health('disconnected'), 0, now=1001)['reload'])
        self.assertFalse(reload_decision(self.health('disconnected'), 0, now=3000)['reload'])
        pending = {**self.health('low', 2), 'pending_switch': {'samples': []}}
        self.assertFalse(reload_decision(pending, 0, now=1001)['reload'])

    def test_active_requests_are_summed_across_private_backends(self):
        class Opener:
            def __init__(self, counts):
                self.counts = counts
                self.urls = []

            def open(self, url, timeout=0):
                self.urls.append(url)
                port = int(url.rsplit(':', 1)[1].split('/', 1)[0])
                value = self.counts.get(port)
                if value is None:
                    raise OSError('backend down')
                if isinstance(value, tuple):
                    active, oldest = value
                    payload = {'active_ai_requests': active,
                               'oldest_ai_request_age_seconds': oldest}
                else:
                    payload = {'active_ai_requests': value}
                return BytesIO(json.dumps(payload).encode())

        opener = Opener({18801: (1, 640), 18802: (2, 120)})
        settings = {'PublicPort': 8799, 'GatewayBackendPorts': [18801, 18802]}
        self.assertEqual(active_request_count(settings, opener), 3)
        self.assertEqual(active_request_stats(settings, opener), (3, 640))
        self.assertEqual(len(opener.urls), 4)
        self.assertIsNone(active_request_count(settings, Opener({18801: 0})))

    def test_stale_active_request_allows_a_disconnected_reload(self):
        health = self.health('disconnected', recovery_failures=2)
        self.assertTrue(reload_decision(
            health, 1, oldest_request_age=700, now=1001,
            stale_seconds=600, recovery_failure_limit=2)['reload'])
        self.assertFalse(reload_decision(
            health, 1, oldest_request_age=100, now=1001,
            stale_seconds=600, recovery_failure_limit=2)['reload'])
        self.assertFalse(reload_decision(
            health, 1, oldest_request_age=700, now=1001,
            stale_seconds=600, recovery_failure_limit=3)['reload'])
        self.assertFalse(reload_decision(
            self.health('low', 2, recovery_failures=2), 1,
            oldest_request_age=700, now=1001,
            stale_seconds=600)['reload'])
        self.assertFalse(reload_decision(
            health, 1, oldest_request_age=700, now=1001,
            stale_seconds=0)['reload'])
        self.assertFalse(reload_decision(
            health, 1, oldest_request_age=700, now=3000,
            stale_seconds=600)['reload'])
