import datetime
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from core_refresh import active_request_count, reload_decision


class CoreRefreshTests(unittest.TestCase):
    def health(self, state, failures=0):
        return {'last_health': {'health': state, 'checked_at': datetime.datetime.fromtimestamp(
            1000, datetime.timezone.utc).isoformat()}, 'consecutive_below_threshold': failures}

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
                count = self.counts.get(port)
                if count is None:
                    raise OSError('backend down')
                return BytesIO(json.dumps({'active_ai_requests': count}).encode())

        opener = Opener({18801: 1, 18802: 2})
        settings = {'PublicPort': 8799, 'GatewayBackendPorts': [18801, 18802]}
        self.assertEqual(active_request_count(settings, opener), 3)
        self.assertEqual(len(opener.urls), 2)
        self.assertIsNone(active_request_count(settings, Opener({18801: 0})))
