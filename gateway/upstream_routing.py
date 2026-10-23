"""Keep explicitly configured private API endpoints outside the project TUN."""
import ipaddress
import json
import os
from pathlib import Path
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


def load_dns_overrides(root=ROOT):
    overrides = {}
    for name in ('upstream-dns-overrides.json', 'upstream-dns-overrides.local.json'):
        path = Path(root) / 'gateway' / name
        if path.exists():
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError('Upstream DNS overrides must be an object')
            overrides.update(value)
    return overrides


def tun_route_exclusions(upstream, overrides):
    routes = ['127.0.0.0/8', '::1/128']
    host = urllib.parse.urlsplit(upstream or '').hostname
    target = overrides.get(host, host)
    if not target:
        return routes
    try:
        address = ipaddress.ip_address(target)
    except ValueError:
        if host in overrides:
            raise ValueError('Configured upstream override must be an IP address') from None
        return routes  # Do not resolve or pin ordinary public upstream domains.
    if any(address.version == network.version and address in network
           for network in PRIVATE_NETWORKS):
        routes.append(str(ipaddress.ip_network(f'{address}/{address.max_prefixlen}')))
    return routes


def main():
    from settings_config import load_settings
    settings = load_settings(ROOT)
    upstream = os.environ.get('SANITIZE_UPSTREAM') or settings.get('AiUpstream') or ''
    print(json.dumps(tun_route_exclusions(upstream, load_dns_overrides(ROOT))))


if __name__ == '__main__':
    main()
