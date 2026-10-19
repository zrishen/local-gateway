"""Connectivity checks; no credentials, TLS bypass, or permanent DNS changes."""
import argparse
import concurrent.futures
import http.client
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import ssl
import struct
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'gateway'))
from settings_config import load_settings

SETTINGS = load_settings(ROOT)


def dns_query(name, server='127.0.0.1', port=None):
    port = port or SETTINGS['DnsPort']
    transaction = secrets.randbelow(65536)
    packet = struct.pack('!6H', transaction, 0x100, 1, 0, 0, 0)
    packet += b''.join(bytes([len(part)]) + part.encode('ascii') for part in name.split('.'))
    packet += b'\0\0\1\0\1'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(8)
        client.sendto(packet, (server, port))
        data, _ = client.recvfrom(8192)
    if struct.unpack_from('!H', data)[0] != transaction:
        raise ValueError('DNS transaction mismatch')

    def skip_name(offset):
        while True:
            length = data[offset]
            if length & 0xc0 == 0xc0:
                return offset + 2
            offset += 1
            if length == 0:
                return offset
            offset += length

    questions, answers = struct.unpack_from('!HH', data, 4)
    offset = 12
    for _ in range(questions):
        offset = skip_name(offset) + 4
    addresses = []
    for _ in range(answers):
        offset = skip_name(offset)
        kind, _, _, length = struct.unpack_from('!HHIH', data, offset)
        offset += 10
        if kind == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    return {'rcode': data[3] & 15, 'addresses': addresses}


def https_head(host, path='/', proxy_port=None):
    target = '127.0.0.1' if proxy_port else host
    client = http.client.HTTPSConnection(target, proxy_port or 443, timeout=10,
                                         context=ssl.create_default_context())
    try:
        if proxy_port:
            client.set_tunnel(host, 443)
        client.request('HEAD', path, headers={'User-Agent': 'UnifiedSingBoxHealth/1'})
        response = client.getresponse()
        return {'http_status': response.status, 'tls_verified': True}
    finally:
        client.close()


def upstream_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    port = SETTINGS.get('PublicPort', 8799)
    with opener.open(f'http://127.0.0.1:{port}/__health', timeout=5) as response:
        return {'http_status': response.status}


def real_dns(name, company=False, system=False):
    result = ({'rcode': 0, 'addresses': sorted({x[4][0] for x in socket.getaddrinfo(name,443,type=socket.SOCK_STREAM)})}
              if system else dns_query(name))
    if result['rcode'] != 0 or not result['addresses']:
        raise ValueError(f'No successful address answer: {result}')
    for address in result['addresses']:
        ip = ipaddress.ip_address(address)
        if ip.version == 4 and ip in ipaddress.ip_network('198.18.0.0/15'):
            raise ValueError(f'DNS still returned FakeIP: {result}')
        if company and not ip.is_private:
            raise ValueError(f'Company DNS did not return private addresses: {result}')
    return result


def run_one(label, callback):
    start = time.monotonic()
    try:
        value = callback()
        return {'check': label, 'ok': True, 'seconds': round(time.monotonic()-start, 2), **value}
    except Exception as error:
        return {'check': label, 'ok': False, 'error': f'{type(error).__name__}: {error}'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    try:
        active_mode = json.loads((ROOT/'state/mode.json').read_text())['mode']
    except (OSError, ValueError, KeyError):
        active_mode = 'Rule'
    try:
        upstream = json.loads((ROOT/'state/upstream.json').read_text())
    except (OSError, ValueError):
        upstream = {}
    upstream_mode = str(upstream.get('Mode') or upstream.get('mode') or 'unknown')
    public_port = SETTINGS.get('PublicPort', SETTINGS['MixedPort'])
    public_host = 'chatgpt.com' if active_mode == 'Rule' else 'www.baidu.com'
    jobs = [
        ('unified-internet', lambda: https_head(public_host, proxy_port=public_port)),
        ('public-dns', lambda: real_dns(public_host)),
        ('local-api', upstream_health),
    ]
    if active_mode == 'Rule' and upstream_mode != 'subscription':
        jobs.append(('sakura-external', lambda: https_head('chatgpt.com', proxy_port=SETTINGS['SakuraPort'])))
    if not args.preflight:
        company_probe_hosts = [str(x).strip() for x in SETTINGS.get('CompanyProbeHosts', []) if str(x).strip()]
        if not company_probe_hosts and SETTINGS.get('CompanyProbeHost'):
            company_probe_hosts = [str(SETTINGS['CompanyProbeHost']).strip()]
        for name in company_probe_hosts:
            def company_dns(name=name):
                return real_dns(name, company=True)
            jobs.append((f'company-dns:{name}', company_dns))
        jobs.append(('tun-internet', lambda: https_head(public_host)))
        if company_probe_hosts:
            company_probe = company_probe_hosts[0]
            jobs += [
                ('tun-company', lambda: https_head(company_probe)),
                ('unified-company', lambda: https_head(company_probe, proxy_port=public_port)),
                ('system-company-dns', lambda: real_dns(company_probe, company=True, system=True)),
            ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(run_one, label, callback) for label, callback in jobs]
        results = [future.result() for future in futures]
    output = {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'mode': active_mode, 'upstream': upstream_mode, 'scope': 'preflight' if args.preflight else 'full', 'checks': results}
    path = ROOT / 'state' / ('health.preflight.json' if args.preflight else 'health.json')
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0 if all(item['ok'] for item in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
