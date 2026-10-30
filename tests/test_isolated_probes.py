import copy
import io
import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from isolated_probes import configured_tags, probe_config
from latency_selector import LatencySelector


class ProbeTests(unittest.TestCase):
    def selector(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'state').mkdir()
        (root / 'state/controller-secret.txt').write_text('test-only')
        return LatencySelector(root, {'ControllerPort': 1, 'MixedPort': 2})

    def test_configured_tags_reads_generated_outbounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config.json').write_text(json.dumps({'outbounds': [
                {'type': 'direct', 'tag': 'direct'},
                {'type': 'trojan', 'tag': 'a'},
                {'type': 'selector', 'tag': 'group'},
            ]}), encoding='utf-8')
            self.assertEqual(configured_tags(root), {'direct', 'a', 'group'})
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(configured_tags(Path(temporary)), set())

    def test_isolated_config_preserves_entries_without_production_tun_or_controller(self):
        source = {'outbounds': [
            {'type': 'direct', 'tag': 'direct', 'bind_interface': 'physical'},
            {'type': 'urltest', 'tag': 'a', 'outbounds': ['a-ep1']},
            {'type': 'trojan', 'tag': 'a-ep1', 'bind_interface': 'physical'},
            {'type': 'selector', 'tag': 'unrelated', 'outbounds': ['a']}],
            'inbounds': [{'type': 'tun'}], 'experimental': {'clash_api': {}}}
        before = copy.deepcopy(source)
        result = probe_config(source, {'a': 16001})
        self.assertEqual(source, before)
        self.assertEqual({x['tag'] for x in result['outbounds']}, {'direct', 'a', 'a-ep1'})
        self.assertNotIn('experimental', result)
        self.assertEqual(result['inbounds'][0]['listen'], '127.0.0.1')
        self.assertTrue(all(x['type'] != 'tun' for x in result['inbounds']))
        self.assertEqual(result['outbounds'][-1]['bind_interface'], 'physical')

    def test_candidate_samples_use_median_without_switching_live_route(self):
        selector = self.selector()
        selector._set_group = Mock()
        selector._set_top_region = Mock()
        values = {1: iter([1000, 20, 30]), 2: iter([100, 110, 120])}
        selector._candidate_quality = lambda port: {
            'ok': True, 'throughput_kbps': next(values[port]), 'chatgpt_ok': True}
        context = Mock()
        context.__enter__ = Mock(return_value=SimpleNamespace(ports={'a': 1, 'b': 2}))
        context.__exit__ = Mock(return_value=False)
        with patch('latency_selector.IsolatedProbePool', return_value=context):
            results = selector._measure_candidates([{'tag': 'a'}, {'tag': 'b'}])
        speeds = {x['tag']: x['throughput_kbps'] for x in results}
        self.assertEqual(speeds, {'a': 30, 'b': 110})
        selector._set_group.assert_not_called()
        selector._set_top_region.assert_not_called()

    def test_tun_parser_decodes_chunking_and_rejects_truncated_download(self):
        selector = self.selector()
        for raw, expected, ok in [
            (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n0\r\n\r\n', 4, True),
            (b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\ndata', 12, False),
            (b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata', 12, False)]:
            with self.subTest(raw=raw):
                sock = SimpleNamespace(makefile=lambda *args, **kwargs: io.BytesIO(raw))
                response = http.client.HTTPResponse(sock)
                response.begin()
                conn = Mock()
                conn.getresponse.return_value = response
                with patch('latency_selector.http.client.HTTPSConnection', return_value=conn):
                    result = selector._https_via_tun('test.invalid', '/', 1, expected, expected_bytes=expected)
                self.assertEqual(result['ok'], ok)
                if ok:
                    self.assertEqual(result['bytes'], 4)
