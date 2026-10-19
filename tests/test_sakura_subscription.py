import base64
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import sakura_subscription as ss


def trojan(port, name):
    return f'trojan://dummy@example.com:{port}?sni=example.com#{name}'


class ParseTests(unittest.TestCase):
    def test_trojan_is_converted_and_deduplicated(self):
        first = 'trojan://dummy-password@example.com:443?allowInsecure=1&sni=cdn.example.com#Node%201'
        outbounds, names, schemes, metadata = ss.parse_nodes(first + '\n' + first)
        self.assertEqual(len(outbounds), 3)  # node + region group + top group
        self.assertEqual(outbounds[0]['type'], 'trojan')
        self.assertEqual(outbounds[0]['server'], 'example.com')
        self.assertEqual(outbounds[0]['server_port'], 443)
        self.assertEqual(outbounds[0]['password'], 'dummy-password')
        self.assertEqual(outbounds[0]['domain_resolver'], 'china-dns')
        self.assertTrue(outbounds[0]['tls']['insecure'])
        self.assertEqual(outbounds[0]['tls']['server_name'], 'cdn.example.com')
        self.assertEqual(outbounds[1]['type'], 'selector')
        self.assertEqual(outbounds[1]['outbounds'], [outbounds[0]['tag']])
        self.assertEqual(outbounds[2]['tag'], 'sakura')
        self.assertEqual(outbounds[2]['outbounds'], [outbounds[1]['tag']])
        self.assertEqual(names[outbounds[0]['tag']], 'Node 1')
        self.assertEqual(schemes, {'trojan': 2})
        self.assertEqual(metadata['regions'][0]['name'], '其他')

    def test_base64_payload_is_accepted(self):
        raw = b'trojan://dummy@example.com:443?sni=example.com#node'
        encoded = base64.b64encode(raw).decode('ascii')
        self.assertIn('trojan://', ss.decode_payload(encoded.encode('ascii')))

    def test_unsupported_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            ss.parse_nodes('ss://bm9kZQ==@example.com:8388#node')

    def test_invalid_node_is_rejected(self):
        with self.assertRaises(ValueError):
            ss.parse_nodes('trojan://@example.com:443#missing-password')

    def test_information_pseudo_node_is_skipped(self):
        info = 'trojan://dummy@example.com:443?sni=example.com#%E5%89%A9%E4%BD%99%E6%B5%81%E9%87%8F%EF%BC%9A62.72%20GB'
        real = trojan(444, 'HK')
        outbounds, names, _, _ = ss.parse_nodes(info + '\n' + real)
        self.assertEqual(outbounds[0]['server_port'], 444)
        self.assertEqual(names[outbounds[0]['tag']], 'HK')

    def test_multiplier_nodes_are_skipped_by_default(self):
        normal = trojan(443, 'HK')
        expensive = trojan(444, 'US%205%E5%80%8D%E6%B6%88%E8%80%97')
        outbounds, _, _, _ = ss.parse_nodes(normal + '\n' + expensive)
        self.assertEqual([n['server_port'] for n in outbounds if n['type'] == 'trojan'], [443])
        outbounds, _, _, _ = ss.parse_nodes(normal + '\n' + expensive, include_multiplier=True)
        self.assertEqual([n['server_port'] for n in outbounds if n['type'] == 'trojan'], [443, 444])

    def test_regions_are_grouped_and_all_kept(self):
        us = trojan(443, '%F0%9F%87%BA%F0%9F%87%B8%7C%E7%BE%8E%E5%9B%BD-IEPL%2001')
        hk = trojan(444, '%F0%9F%87%AD%F0%9F%87%B0%7C%E9%A6%99%E6%B8%AF-%E7%9B%B4%E8%BF%9E')
        outbounds, _, _, metadata = ss.parse_nodes(us + '\n' + hk)
        self.assertEqual([item['name'] for item in metadata['regions']], ['美国', '香港'])
        self.assertEqual(metadata['regions'][0]['count'], 1)
        self.assertEqual(metadata['regions'][1]['count'], 1)
        top = next(item for item in outbounds if item['tag'] == 'sakura')
        self.assertEqual(top['outbounds'], ['sakura-region-us', 'sakura-region-hk'])

    def test_active_region_selects_its_group(self):
        us = trojan(443, '美国-直连')
        hk = trojan(444, '香港-直连')
        outbounds, _, _, metadata = ss.parse_nodes(us + '\n' + hk)
        ss.apply_active_region(outbounds, metadata, '香港')
        top = next(item for item in outbounds if item['tag'] == 'sakura')
        self.assertEqual(top['default'], 'sakura-region-hk')
        self.assertEqual(metadata['active_region'], '香港')

    def test_indonesia_is_not_confused_with_india(self):
        self.assertEqual(ss.detect_region('🇮🇩|印度尼西亚-IEPL 01'), '印度尼西亚')
        self.assertEqual(ss.detect_region('🇮🇳|印度-IEPL 01'), '印度')


if __name__ == '__main__':
    unittest.main()