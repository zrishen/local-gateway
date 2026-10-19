import json
from pathlib import Path
import struct
from datetime import datetime, timedelta, timezone
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import endpoint_catalog as ec


SOURCE = {
    'count': 2,
    'names': {'sakura-001': 'Node A', 'sakura-002': 'Node B'},
    'outbounds': [
        {'type': 'trojan', 'tag': 'sakura-001', 'server': 'node.example', 'server_port': 443,
         'password': 'secret-a', 'tls': {'enabled': True, 'server_name': 'sni.example'}},
        {'type': 'trojan', 'tag': 'sakura-002', 'server': 'other.example', 'server_port': 8443,
         'password': 'secret-b', 'tls': {'enabled': True, 'server_name': 'other.example'}},
        {'type': 'selector', 'tag': 'region', 'outbounds': ['sakura-001', 'sakura-002']},
        {'type': 'selector', 'tag': 'sakura', 'outbounds': ['region']},
    ],
    'region_groups': {'US': {'group': 'region', 'nodes': ['sakura-001', 'sakura-002']}},
}


def catalog(probes_a, probes_b=None):
    return {
        'updated_at': '2026-10-19T19:00:00+0800',
        'hosts': {},
        'nodes': {
            'sakura-001': {'probes': probes_a},
            'sakura-002': {'probes': probes_b or {}},
        },
    }


class RenderTests(unittest.TestCase):
    def test_healthy_endpoint_becomes_per_node_urltest(self):
        stamp = ec.now_text()
        value = catalog({
            '203.0.113.2': {'ok': True, 'total_ms': 80, 'checked_at': stamp},
            '203.0.113.1': {'ok': True, 'total_ms': 40, 'checked_at': stamp},
            '203.0.113.3': {'ok': False, 'error': 'TimeoutError', 'checked_at': stamp},
        })
        rendered = ec.render(SOURCE, value)
        by_tag = {x['tag']: x for x in rendered['outbounds']}
        node = by_tag['sakura-001']
        self.assertEqual(node['type'], 'urltest')
        self.assertEqual(node['outbounds'], ['sakura-001-ep1', 'sakura-001-ep2'])
        self.assertEqual([by_tag[x]['server'] for x in node['outbounds']], ['203.0.113.1', '203.0.113.2'])
        self.assertEqual(by_tag['region']['outbounds'], ['sakura-001', 'sakura-002'])
        self.assertEqual(rendered['endpoint_selection']['nodes_with_candidates'], 1)

    def test_no_healthy_endpoint_keeps_original_hostname(self):
        stamp = ec.now_text()
        value = catalog({'203.0.113.1': {'ok': False, 'checked_at': stamp}})
        rendered = ec.render(SOURCE, value)
        node = next(x for x in rendered['outbounds'] if x['tag'] == 'sakura-001')
        self.assertEqual(node['type'], 'trojan')
        self.assertEqual(node['server'], 'node.example')
        self.assertEqual(rendered['endpoint_selection']['nodes_with_candidates'], 0)

    def test_stale_probe_is_not_rendered(self):
        rendered = ec.render(SOURCE, catalog({
            '203.0.113.1': {'ok': True, 'total_ms': 30, 'checked_at': '2020-01-01T00:00:00+0800'}
        }))
        node = next(x for x in rendered['outbounds'] if x['tag'] == 'sakura-001')
        self.assertEqual(node['type'], 'trojan')


class RetentionTests(unittest.TestCase):
    @staticmethod
    def stamp(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S%z')

    def value(self, ips):
        return {
            'hosts': {'node.example': {'ips': ips}},
            'nodes': {'sakura-001': {'host': 'node.example', 'probes': {
                ip: {'ok': bool(item.get('last_success')), 'checked_at': self.stamp(0)}
                for ip, item in ips.items()
            }}},
        }

    def nodes(self):
        return {'sakura-001': {'host': 'node.example', 'port': 443, 'sni': 'sni.example'}}

    def test_recent_success_is_retained(self):
        value = self.value({'1.1.1.1': {'first_seen': self.stamp(40),
                                        'last_seen': self.stamp(40),
                                        'last_success': self.stamp(20)}})
        cleanup = ec.prune_catalog(value, self.nodes(), {})
        self.assertEqual(set(value['hosts']['node.example']['ips']), {'1.1.1.1'})
        self.assertEqual(cleanup['removed_expired'], 0)

    def test_old_success_is_expired(self):
        value = self.value({'1.1.1.1': {'first_seen': self.stamp(90),
                                        'last_seen': self.stamp(0),
                                        'last_success': self.stamp(45)}})
        cleanup = ec.prune_catalog(value, self.nodes(), {})
        self.assertEqual(value['hosts']['node.example']['ips'], {})
        self.assertEqual(value['nodes']['sakura-001']['probes'], {})
        self.assertGreaterEqual(cleanup['removed_expired'], 1)

    def test_recent_failure_without_success_is_retained_temporarily(self):
        value = self.value({'1.1.1.1': {'first_seen': self.stamp(2),
                                        'last_seen': self.stamp(0),
                                        'last_failure': self.stamp(0)}})
        ec.prune_catalog(value, self.nodes(), {})
        self.assertEqual(set(value['hosts']['node.example']['ips']), {'1.1.1.1'})

    def test_long_failed_endpoint_is_expired_even_if_dns_still_sees_it(self):
        value = self.value({'1.1.1.1': {'first_seen': self.stamp(8),
                                        'last_seen': self.stamp(0),
                                        'last_failure': self.stamp(0)}})
        ec.prune_catalog(value, self.nodes(), {})
        self.assertEqual(value['hosts']['node.example']['ips'], {})
        self.assertIn('1.1.1.1', value['hosts']['node.example']['retired'])

    def test_retired_dead_ip_is_suppressed_when_dns_returns_it_again(self):
        value = self.value({'1.1.1.1': {'first_seen': self.stamp(8),
                                        'last_seen': self.stamp(0),
                                        'last_failure': self.stamp(0)}})
        ec.prune_catalog(value, self.nodes(), {})
        ec.merge_host_ips(value, 'node.example', ['1.1.1.1'], 'dns')
        result = ec.filter_retired(value, self.nodes(), {})
        self.assertEqual(value['hosts']['node.example']['ips'], {})
        self.assertEqual(value['nodes']['sakura-001']['probes'], {})
        self.assertEqual(result['suppressed_retired'], 1)

    def test_tombstone_expires_after_retention_window(self):
        value = self.value({})
        value['hosts']['node.example']['retired'] = {
            '1.1.1.1': {'retired_at': self.stamp(8), 'reason': 'expired_history'}
        }
        result = ec.filter_retired(value, self.nodes(), {})
        self.assertEqual(value['hosts']['node.example']['retired'], {})
        self.assertEqual(result['expired_tombstones'], 1)

    def test_per_host_cap_keeps_recent_success(self):
        ips = {}
        for index in range(5):
            ips[f'203.0.113.{index}'] = {'first_seen': self.stamp(1),
                                       'last_seen': self.stamp(1),
                                       'last_success': self.stamp(index + 1)}
        value = self.value(ips)
        cleanup = ec.prune_catalog(value, self.nodes(),
                                   {'SakuraEndpointHistoryPerHost': 3})
        self.assertEqual(len(value['hosts']['node.example']['ips']), 3)
        self.assertEqual(set(value['hosts']['node.example']['ips']),
                         {'203.0.113.0', '203.0.113.1', '203.0.113.2'})
        self.assertEqual(cleanup['removed_capped'], 2)

    def test_active_and_tombstones_share_one_per_host_cap(self):
        ips = {}
        for index in range(3):
            ips[f'203.0.113.{index}'] = {'first_seen': self.stamp(1),
                                       'last_seen': self.stamp(1),
                                       'last_success': self.stamp(1)}
        value = self.value(ips)
        value['hosts']['node.example']['retired'] = {
            f'10.0.1.{index}': {'retired_at': self.stamp(index), 'reason': 'expired_history'}
            for index in range(3)
        }
        ec.prune_catalog(value, self.nodes(), {'SakuraEndpointHistoryPerHost': 4})
        host = value['hosts']['node.example']
        self.assertEqual(len(host['ips']) + len(host['retired']), 4)
        self.assertEqual(len(host['ips']), 3)
        self.assertEqual(len(host['retired']), 1)

    def test_removed_subscription_node_is_cleaned(self):
        value = self.value({})
        value['nodes']['removed-node'] = {'host': 'node.example', 'probes': {}}
        cleanup = ec.prune_catalog(value, self.nodes(), {})
        self.assertNotIn('removed-node', value['nodes'])
        self.assertEqual(cleanup['removed_nodes'], 1)


class DiscoveryTests(unittest.TestCase):
    def test_merge_host_ips_accumulates_sources_without_duplicates(self):
        value = {'hosts': {}}
        ec.merge_host_ips(value, 'node.example', ['1.1.1.1', '1.1.1.1'], 'dns')
        ec.merge_host_ips(value, 'node.example', ['1.1.1.1', '2.2.2.2'], 'sakuracat-observed')
        ips = value['hosts']['node.example']['ips']
        self.assertEqual(set(ips), {'1.1.1.1', '2.2.2.2'})
        self.assertEqual(ips['1.1.1.1']['sources'], ['dns', 'sakuracat-observed'])

    def test_dns_parser_collects_all_ipv4_answers(self):
        def name(value):
            return b''.join(bytes([len(p)]) + p.encode() for p in value.split('.')) + b'\0'
        header = struct.pack('!6H', 1234, 0x8180, 1, 2, 0, 0)
        question = name('node.example') + struct.pack('!HH', 1, 1)
        answers = b''
        for ip in ('1.2.3.4', '5.6.7.8'):
            answers += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + bytes(map(int, ip.split('.')))
        self.assertEqual(ec.parse_dns_answers(header + question + answers),
                         ['1.2.3.4', '5.6.7.8'])


if __name__ == '__main__':
    unittest.main()
