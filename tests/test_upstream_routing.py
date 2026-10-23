import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from upstream_routing import load_dns_overrides, tun_route_exclusions


class UpstreamRoutingTests(unittest.TestCase):
    def test_only_current_private_api_is_excluded(self):
        routes = tun_route_exclusions('https://api.example.test/prefix', {
            'api.example.test': '10.20.30.40', 'unused.example.test': '10.20.30.41'})
        networks = [ipaddress.ip_network(route) for route in routes]
        self.assertTrue(any(ipaddress.ip_address('10.20.30.40') in network
                            for network in networks if network.version == 4))
        for address in ('10.20.30.41', '10.20.30.39', '8.8.8.8'):
            self.assertFalse(any(ipaddress.ip_address(address) in network
                                 for network in networks if network.version == 4))

    def test_each_private_range_and_literal_url(self):
        for address in ('10.1.2.3', '172.16.2.3', '192.168.2.3', 'fd00::1234'):
            with self.subTest(address=address):
                host = f'[{address}]' if ':' in address else address
                routes = tun_route_exclusions(f'https://{host}:443', {})
                suffix = '/128' if ':' in address else '/32'
                self.assertIn(address + suffix, routes)

    def test_public_fakeip_and_unconfigured_domain_keep_tun_routing(self):
        for address in ('8.8.8.8', '198.18.1.2', '192.0.2.8', '127.0.0.1', '::1'):
            with self.subTest(address=address):
                self.assertEqual(tun_route_exclusions('https://api.example.test', {
                    'api.example.test': address}), ['127.0.0.0/8', '::1/128'])
        self.assertEqual(tun_route_exclusions('https://api.example.test', {}),
                         ['127.0.0.0/8', '::1/128'])
        self.assertEqual(tun_route_exclusions('', {}), ['127.0.0.0/8', '::1/128'])

    def test_local_mapping_wins_and_bom_is_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            gateway = Path(folder) / 'gateway'
            gateway.mkdir()
            (gateway / 'upstream-dns-overrides.json').write_text(
                json.dumps({'api.example.test': '192.168.1.3'}))
            (gateway / 'upstream-dns-overrides.local.json').write_text(
                json.dumps({'api.example.test': '192.168.1.4'}), encoding='utf-8-sig')
            overrides = load_dns_overrides(folder)
            self.assertIn('192.168.1.4/32', tun_route_exclusions(
                'https://api.example.test', overrides))
            self.assertNotIn('192.168.1.3/32', tun_route_exclusions(
                'https://api.example.test', overrides))

    def test_invalid_override_does_not_silently_start_unprotected(self):
        with self.assertRaises(ValueError):
            tun_route_exclusions('https://api.example.test', {'api.example.test': 'not-an-ip'})


if __name__ == '__main__':
    unittest.main()
