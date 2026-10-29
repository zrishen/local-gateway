import copy
from datetime import datetime
import http.client
import http.server
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import codex_catalog as cc
import codex_router as cr


def official(slug='gpt-new', **fields):
    return {'models': [{'slug': slug, 'display_name': 'Official name',
                        'context_window': 272000, 'max_context_window': 872000,
                        'default_reasoning_level': 'low',
                        'supported_reasoning_levels': [{'effort': 'max'}],
                        'future_capability': {'enabled': True}, **fields}]}


def beijing_timestamp(text):
    return datetime.fromisoformat(text + '+08:00').timestamp()


class CatalogScheduleTests(unittest.TestCase):
    def test_beijing_schedule_boundaries_and_midnight(self):
        cases = [
            ('2026-10-29T09:09:59', '2026-10-28T18:00:00', '2026-10-29T09:10:00'),
            ('2026-10-29T09:10:00', '2026-10-29T09:10:00', '2026-10-29T18:00:00'),
            ('2026-10-29T12:00:00', '2026-10-29T09:10:00', '2026-10-29T18:00:00'),
            ('2026-10-29T17:59:59', '2026-10-29T09:10:00', '2026-10-29T18:00:00'),
            ('2026-10-29T18:00:00', '2026-10-29T18:00:00', '2026-10-30T09:10:00'),
            ('2026-10-30T00:00:00', '2026-10-29T18:00:00', '2026-10-30T09:10:00'),
        ]
        for current, latest, following in cases:
            with self.subTest(current=current):
                self.assertEqual(cc.scheduled_sync_times(beijing_timestamp(current)),
                                 (beijing_timestamp(latest), beijing_timestamp(following)))

    def test_schedule_uses_beijing_time_for_utc_inputs(self):
        utc_midnight = datetime.fromisoformat('2026-10-29T00:00:00+00:00').timestamp()
        self.assertEqual(cc.scheduled_sync_times(utc_midnight),
                         (beijing_timestamp('2026-10-28T18:00:00'),
                          beijing_timestamp('2026-10-29T09:10:00')))


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local_path = self.root / 'local.json'
        self.local = {'models': [{'slug': 'provider/local', 'context_window': 500000}]}
        cc.atomic_json(self.local_path, self.local)
        self.auth_path = self.root / 'auth.json'
        self.sign_in('first-account')
        self.now = beijing_timestamp('2026-10-29T09:00:00')
        self.fetch = Mock(return_value=official())
        self.version = Mock(return_value='0.164.0-alpha.11.1')
        self.sync = cc.CatalogSync(self.root, self.local_path, self.auth_path,
                                   self.fetch, self.version, lambda: self.now)

    def sign_in(self, account):
        cc.atomic_json(self.auth_path, {'auth_mode': 'chatgpt', 'tokens': {
            'access_token': 'private-token', 'account_id': account}})

    def output(self):
        return json.loads(self.sync.output_path.read_text(encoding='utf-8'))

    def test_new_models_and_retirements_follow_official_with_metadata_intact(self):
        source = copy.deepcopy(self.fetch.return_value)
        self.assertTrue(self.sync.refresh()['ok'])
        self.assertEqual(self.output(), cc.merge_catalogs(source, self.local))
        self.assertEqual(self.fetch.return_value, source)
        self.now = cc.scheduled_sync_times(self.now)[1]
        self.fetch.return_value = official('gpt-6.1-terra', context_window=500000)
        self.assertTrue(self.sync.refresh()['ok'])
        self.assertEqual([m['slug'] for m in self.output()['models']],
                         ['gpt-6.1-terra', 'provider/local'])
        self.assertEqual(self.output()['models'][0], self.fetch.return_value['models'][0])

    def test_cache_avoids_network_and_local_edits_are_still_published(self):
        self.sync.refresh()
        self.now += 60
        self.local['models'].append({'slug': 'provider/extra'})
        cc.atomic_json(self.local_path, self.local)
        self.assertTrue(self.sync.refresh()['ok'])
        self.fetch.assert_called_once()
        self.assertEqual(self.output()['models'][-1]['slug'], 'provider/extra')
        self.sync.output_path.unlink()
        self.sync.refresh()
        self.assertEqual(self.output()['models'][0]['slug'], 'gpt-new')
        self.fetch.assert_called_once()

    def test_client_upgrade_refreshes_before_scheduled_time(self):
        self.sync.refresh()
        self.version.return_value = '0.165.0'
        self.sync.refresh()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.fetch.call_args.args[0], '0.165.0')

    def test_failed_refresh_preserves_last_good_catalog_and_retries(self):
        self.sync.refresh()
        last_success = self.now
        snapshot = self.sync.output_path.read_bytes()
        self.now = cc.scheduled_sync_times(self.now)[1]
        self.fetch.side_effect = cc.CatalogHTTPError(401)
        status = self.sync.refresh()
        self.assertFalse(status['ok'])
        self.assertEqual(status['http_status'], 401)
        self.assertEqual(status['last_success'], last_success)
        self.assertEqual(self.sync.output_path.read_bytes(), snapshot)
        self.fetch.side_effect = None
        self.fetch.return_value = official('gpt-next')
        self.assertTrue(self.sync.refresh()['ok'])
        self.assertEqual(self.output()['models'][0]['slug'], 'gpt-next')

    def test_only_morning_and_evening_slots_trigger_routine_fetches(self):
        self.now = beijing_timestamp('2026-10-29T09:09:00')
        self.sync.refresh()
        for stamp, expected in (
                ('2026-10-29T09:10:00', 2),
                ('2026-10-29T09:11:00', 2),
                ('2026-10-29T09:41:00', 2),
                ('2026-10-29T12:00:00', 2),
                ('2026-10-29T17:59:00', 2),
                ('2026-10-29T18:00:00', 3),
                ('2026-10-29T20:00:00', 3),
                ('2026-10-30T09:09:00', 3),
                ('2026-10-30T09:10:00', 4)):
            with self.subTest(stamp=stamp):
                self.now = beijing_timestamp(stamp)
                self.assertTrue(self.sync.refresh()['ok'])
                self.assertEqual(self.fetch.call_count, expected)

    def test_restart_after_missed_slots_catches_up_with_one_fetch(self):
        self.sync.refresh()
        self.now = beijing_timestamp('2026-11-02T21:00:00')
        self.assertTrue(self.sync.refresh()['ok'])
        self.assertEqual(self.fetch.call_count, 2)
        self.sync.refresh()
        self.assertEqual(self.fetch.call_count, 2)

    def test_invalid_or_empty_success_response_does_not_replace_cache(self):
        self.sync.refresh()
        snapshot = self.sync.output_path.read_bytes()
        cache = self.sync.cache_path.read_bytes()
        for bad in ({}, {'models': []}, {'models': [{'slug': 'gpt-new'}]},
                    official('provider/impersonation'),
                    {'models': official()['models'] * 2}):
            with self.subTest(response=bad):
                self.fetch.return_value = bad
                self.assertFalse(self.sync.refresh(force=True)['ok'])
                self.assertEqual(self.sync.output_path.read_bytes(), snapshot)
                self.assertEqual(self.sync.cache_path.read_bytes(), cache)

    def test_account_switch_never_uses_other_accounts_models(self):
        self.sync.refresh()
        self.sign_in('second-account')
        self.fetch.side_effect = OSError('private-server-details')
        self.assertFalse(self.sync.refresh()['ok'])
        self.assertEqual(self.output(), self.local)
        self.fetch.side_effect = None
        self.fetch.return_value = official('gpt-second-account')
        self.sync.refresh()
        self.assertEqual(self.output()['models'][0]['slug'], 'gpt-second-account')

    def test_account_change_during_fetch_cannot_publish_previous_account(self):
        self.sync.refresh()
        def switched(*_):
            self.sign_in('second-account')
            return official('gpt-stale-account')
        self.fetch.side_effect = switched
        self.assertFalse(self.sync.refresh(force=True)['ok'])
        self.assertEqual(self.output(), self.local)

    def test_missing_auth_bootstraps_third_party_models(self):
        self.auth_path.unlink()
        self.assertFalse(self.sync.refresh()['ok'])
        self.assertEqual(self.output(), self.local)
        self.fetch.assert_not_called()

    def test_malformed_sign_in_does_not_break_refresh_or_destroy_cache(self):
        self.sync.refresh()
        snapshot = self.sync.output_path.read_bytes()
        for auth in (None, [], {'auth_mode': 'chatgpt', 'tokens': 'invalid'},
                     {'auth_mode': 'chatgpt', 'tokens': {'access_token': 42, 'account_id': []}}):
            with self.subTest(auth=auth):
                cc.atomic_json(self.auth_path, auth)
                self.assertFalse(self.sync.refresh()['ok'])
                self.assertEqual(self.sync.output_path.read_bytes(), snapshot)

    def test_corrupt_cache_metadata_is_refetched_instead_of_stuck(self):
        self.sync.refresh()
        for stamp in (None, 'not-a-timestamp'):
            cache = json.loads(self.sync.cache_path.read_text(encoding='utf-8'))
            cache['fetched_at'] = stamp
            cc.atomic_json(self.sync.cache_path, cache)
            self.assertTrue(self.sync.refresh()['ok'])
        self.assertEqual(self.fetch.call_count, 3)

    def test_local_source_cannot_override_official_models(self):
        cc.atomic_json(self.local_path, official())
        self.assertFalse(self.sync.refresh()['ok'])
        self.fetch.assert_not_called()

    def test_credentials_are_not_persisted_and_status_omits_error_details(self):
        self.sync.refresh()
        self.fetch.side_effect = RuntimeError('private-token first-account')
        self.sync.refresh(force=True)
        for path in (self.sync.output_path, self.sync.cache_path, self.sync.status_path):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('private-token', text)
            self.assertNotIn('first-account', text)
            self.assertNotIn('Authorization', text)
        self.assertEqual(self.fetch.call_args.args[1]['Authorization'], 'Bearer private-token')

    def test_atomic_replace_failure_keeps_previous_file(self):
        self.sync.refresh()
        snapshot = self.sync.output_path.read_bytes()
        with patch.object(cc.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                cc.atomic_json(self.sync.output_path, self.local)
        self.assertEqual(self.sync.output_path.read_bytes(), snapshot)
        self.assertEqual(list(self.sync.output_path.parent.glob('*.tmp')), [])

    def test_unchanged_output_is_not_replaced(self):
        self.sync.refresh()
        with patch.object(cc.os, 'replace') as replace:
            cc.atomic_json(self.sync.output_path, self.output())
        replace.assert_not_called()


class CatalogTransportTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.seen.append((self.path, dict(self.headers)))
                body = json.dumps(official()).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_fetch_uses_fixed_official_path_and_closes_connection(self):
        connections = []
        def factory():
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
            connections.append(connection)
            return connection
        with patch.object(cr, 'official_connection', side_effect=factory):
            data = cc.fetch_official_catalog('0.164.0-alpha.11.1', {
                'Authorization': 'Bearer synthetic-token', 'ChatGPT-Account-Id': 'synthetic-account'})
        self.assertEqual(data, official())
        path, headers = self.seen[0]
        self.assertEqual(path, '/backend-api/codex/models?client_version=0.164.0-alpha.11.1')
        self.assertEqual(headers['Authorization'], 'Bearer synthetic-token')
        self.assertEqual(headers['originator'], 'codex_cli_rs')
        self.assertNotIn('X-Unified-Codex-Key', headers)
        self.assertIsNone(connections[0].sock)


if __name__ == '__main__':
    unittest.main()
