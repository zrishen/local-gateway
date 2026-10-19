"""Discover and validate alternative entry IPs for subscription nodes.

The subscription supplies a hostname, port, TLS SNI and credential. DNS may
return different entry IPs over time, including unreachable answers. This
module keeps a small, health-checked endpoint pool and renders a per-node
urltest so sing-box rechecks the chosen entry every few minutes.

Endpoint credentials never enter the catalog. Only hostnames, IPs, ports, SNI
and timing/health metadata are persisted in state/sakura-endpoints.json.
"""
import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'state/sakura-endpoints.json'
SEEDS = ROOT / 'state/sakura-endpoint-seeds.json'
SOURCE = ROOT / 'state/sakura-outbounds.json'
RENDERED = ROOT / 'state/sakura-outbounds.runtime.json'
DEFAULT_RESOLVERS = ('223.5.5.5', '119.29.29.29', '114.114.114.114')
MAX_ENDPOINTS_PER_NODE = 3
DEFAULT_HISTORY_PER_HOST = 24
DEFAULT_SUCCESS_RETENTION_DAYS = 30
DEFAULT_FAILURE_RETENTION_DAYS = 7
DEFAULT_PROBE_LIMIT_PER_NODE = 8


def now_text():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def parse_time(value):
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S'):
        try:
            return time.mktime(time.strptime(value, fmt))
        except (TypeError, ValueError):
            pass
    return 0


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return default


def skip_dns_name(data, offset):
    while True:
        length = data[offset]
        if length & 0xc0 == 0xc0:
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length


def query_dns(name, server, timeout=2.0):
    transaction = int.from_bytes(os.urandom(2), 'big')
    packet = struct.pack('!6H', transaction, 0x0100, 1, 0, 0, 0)
    for part in name.rstrip('.').split('.'):
        encoded = part.encode('ascii')
        packet += bytes([len(encoded)]) + encoded
    packet += b'\0\0\1\0\1'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        client.sendto(packet, (server, 53))
        data, _ = client.recvfrom(8192)
    if len(data) < 12 or struct.unpack_from('!H', data)[0] != transaction:
        raise ValueError('DNS transaction mismatch')
    return parse_dns_answers(data)


def parse_dns_answers(data):
    questions, answers = struct.unpack_from('!HH', data, 4)
    offset = 12
    for _ in range(questions):
        offset = skip_dns_name(data, offset) + 4
    addresses = []
    for _ in range(answers):
        offset = skip_dns_name(data, offset)
        kind, _, _, length = struct.unpack_from('!HHIH', data, offset)
        offset += 10
        if kind == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    return addresses


def discover_dns(host, resolvers=DEFAULT_RESOLVERS):
    found = set()
    for server in resolvers:
        try:
            found.update(query_dns(host, server))
        except OSError:
            pass
    try:
        found.update(x[4][0] for x in socket.getaddrinfo(
            host, None, socket.AF_INET, socket.SOCK_STREAM))
    except OSError:
        pass
    return sorted(found)


def physical_address(interface):
    script = ("Get-NetIPAddress -InterfaceAlias $env:ENDPOINT_IFACE -AddressFamily IPv4 "
              "-ErrorAction Stop | Where-Object {$_.AddressState -eq 'Preferred'} | "
              "Select-Object -First 1 -ExpandProperty IPAddress")
    env = dict(os.environ, ENDPOINT_IFACE=interface)
    result = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=10, env=env,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    address = result.stdout.strip()
    if result.returncode or not address:
        raise RuntimeError('Could not resolve the physical interface IPv4 address')
    return address


def probe(ip, port, sni, bind_address, timeout=2.5):
    started = time.monotonic()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tls_socket = None
    try:
        raw.bind((bind_address, 0))
        raw.settimeout(timeout)
        raw.connect((ip, int(port)))
        connected = time.monotonic()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls_socket = context.wrap_socket(raw, server_hostname=sni or ip)
        finished = time.monotonic()
        return {'ok': True, 'tcp_ms': round((connected-started)*1000),
                'tls_ms': round((finished-connected)*1000),
                'total_ms': round((finished-started)*1000)}
    except Exception as error:
        return {'ok': False, 'error': type(error).__name__}
    finally:
        if tls_socket is not None:
            tls_socket.close()
        raw.close()


def node_definitions(source):
    nodes = {}
    for item in source.get('outbounds', []):
        if item.get('type') in ('selector', 'urltest'):
            continue
        if not item.get('tag') or not item.get('server') or not item.get('server_port'):
            continue
        nodes[item['tag']] = {
            'tag': item['tag'],
            'host': item['server'],
            'port': int(item['server_port']),
            'sni': (item.get('tls') or {}).get('server_name') or item['server'],
            'name': (source.get('names') or {}).get(item['tag'], item['tag']),
        }
    return nodes


def merge_host_ips(catalog, host, ips, source):
    host_item = catalog.setdefault('hosts', {}).setdefault(host, {'ips': {}})
    stamp = now_text()
    for ip in ips:
        item = host_item['ips'].setdefault(ip, {'sources': [], 'first_seen': stamp})
        if source not in item['sources']:
            item['sources'].append(source)
        item['last_seen'] = stamp


def merge_seeds(catalog, seeds):
    for host, value in (seeds or {}).items():
        if isinstance(value, list):
            ips, source = value, 'seed'
        else:
            ips, source = value.get('ips', []), value.get('source', 'seed')
        merge_host_ips(catalog, host, ips, source)


def merge_sakura_observed(catalog, settings, nodes):
    port = settings.get('SakuraControllerPort')
    if not port:
        return 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/connections', timeout=2) as response:
            payload = json.loads(response.read() or b'{}')
    except (OSError, ValueError):
        return 0
    by_name = {}
    for node in nodes.values():
        by_name.setdefault(node['name'], []).append(node)
    added = 0
    for connection in payload.get('connections', []):
        metadata = connection.get('metadata') or {}
        ip, port_value = metadata.get('destinationIP'), metadata.get('destinationPort')
        if not ip or not port_value:
            continue
        for chain in connection.get('chains') or []:
            for node in by_name.get(chain, []):
                if int(port_value) == node['port']:
                    merge_host_ips(catalog, node['host'], [ip], 'sakuracat-observed')
                    added += 1
    return added


def due_for_refresh(catalog, minutes):
    last = parse_time(catalog.get('last_full_probe', ''))
    return not last or time.time() - last >= max(1, minutes) * 60


def refresh(settings, source, force=False):
    catalog = load_json(CATALOG, {'version': 2, 'hosts': {}, 'nodes': {}})
    nodes = node_definitions(source)
    catalog['version'] = 2
    if not catalog.get('initialized_at'):
        merge_seeds(catalog, load_json(SEEDS, {}))
        catalog['initialized_at'] = now_text()
    refresh_minutes = float(settings.get('SakuraEndpointRefreshMinutes', 60))
    probe_all = force or due_for_refresh(catalog, refresh_minutes)
    hosts = sorted({node['host'] for node in nodes.values()})
    if probe_all:
        merge_sakura_observed(catalog, settings, nodes)
        resolvers = tuple(settings.get('NodeEndpointDnsResolvers') or DEFAULT_RESOLVERS)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(hosts) or 1)) as pool:
            resolved = dict(zip(hosts, pool.map(lambda h: discover_dns(h, resolvers), hosts)))
        for host, ips in resolved.items():
            merge_host_ips(catalog, host, ips, 'dns')
    retired = filter_retired(catalog, nodes, settings)
    bind_address = physical_address(settings['PhysicalInterface'])
    jobs = []
    for tag, node in nodes.items():
        node_item = catalog.setdefault('nodes', {}).setdefault(tag, {'probes': {}})
        node_item.update({k: node[k] for k in ('host', 'port', 'sni', 'name')})
        probe_limit = max(1, int(settings.get(
            'SakuraEndpointProbeCandidatesPerNode', DEFAULT_PROBE_LIMIT_PER_NODE)))
        for ip in probe_candidates(catalog, node, node_item, probe_limit):
            checked = node_item['probes'].get(ip)
            if not probe_all and checked and checked.get('checked_at'):
                continue
            jobs.append((tag, node, ip))
    results = []
    if jobs:
        def run(job):
            tag, node, ip = job
            return tag, ip, probe(ip, node['port'], node['sni'], bind_address)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(24, len(jobs))) as pool:
            results = list(pool.map(run, jobs))
    stamp = now_text()
    successful = 0
    for tag, ip, result in results:
        result['checked_at'] = stamp
        catalog['nodes'][tag]['probes'][ip] = result
        endpoint = catalog['hosts'][nodes[tag]['host']]['ips'][ip]
        if result.get('ok'):
            endpoint['last_success'] = stamp
            endpoint['last_latency_ms'] = result['total_ms']
            successful += 1
        else:
            endpoint['last_failure'] = stamp
            endpoint['last_error'] = result.get('error')
    if probe_all:
        catalog['last_full_probe'] = stamp
    cleanup = prune_catalog(catalog, nodes, settings)
    catalog['updated_at'] = stamp
    catalog['last_run'] = {
        'nodes': len(nodes), 'hosts': len(hosts), 'probes': len(jobs),
        'successful': successful, 'refresh_minutes': refresh_minutes,
        'retired_filter': retired, 'cleanup': cleanup,
    }
    write_json(CATALOG, catalog)
    return catalog, nodes


def _age_seconds(value, now):
    parsed = parse_time(value or '')
    return now - parsed if parsed else None


def filter_retired(catalog, nodes, settings):
    """Suppress recently expired dead IPs even if DNS returns them again."""
    now = time.time()
    retention_days = max(1, float(settings.get(
        'SakuraEndpointFailureRetentionDays', DEFAULT_FAILURE_RETENTION_DAYS)))
    removed, expired = 0, 0
    for host, host_value in (catalog.get('hosts') or {}).items():
        retired = host_value.get('retired') or {}
        active = {}
        for ip, item in retired.items():
            age = _age_seconds(item.get('retired_at'), now)
            if age is None or age <= retention_days * 86400:
                active[ip] = item
            else:
                expired += 1
        host_value['retired'] = active
        ips = host_value.get('ips') or {}
        for ip in list(ips):
            if ip in active:
                del ips[ip]
                removed += 1
        for tag, node in nodes.items():
            if node['host'] != host:
                continue
            probes = ((catalog.get('nodes') or {}).get(tag) or {}).get('probes') or {}
            for ip in active:
                probes.pop(ip, None)
    return {'suppressed_retired': removed, 'expired_tombstones': expired}


def prune_catalog(catalog, nodes, settings):
    """Expire stale history and cap each host without losing recent successes.

    A successful endpoint survives for the success-retention window. DNS-seen
    or failed-only endpoints survive for the shorter failure window, so they
    are not re-probed forever when DNS keeps returning a dead address.
    """
    now = time.time()
    success_days = max(1, float(settings.get(
        'SakuraEndpointSuccessRetentionDays', DEFAULT_SUCCESS_RETENTION_DAYS)))
    failure_days = max(1, float(settings.get(
        'SakuraEndpointFailureRetentionDays', DEFAULT_FAILURE_RETENTION_DAYS)))
    per_host = max(1, int(settings.get(
        'SakuraEndpointHistoryPerHost', DEFAULT_HISTORY_PER_HOST)))
    removed_expired, removed_capped, removed_nodes = [], [], []

    for host, host_value in list((catalog.get('hosts') or {}).items()):
        ips = host_value.get('ips') or {}
        retained = {}
        for ip, item in ips.items():
            success_age = _age_seconds(item.get('last_success'), now)
            first_age = _age_seconds(item.get('first_seen'), now)
            if success_age is not None:
                keep = success_age <= success_days * 86400
            else:
                # Failed-only entries expire from the moment they entered the
                # catalog, even if DNS keeps returning the same dead address.
                keep = first_age is not None and first_age <= failure_days * 86400
            if keep:
                retained[ip] = item
            else:
                removed_expired.append({'host': host, 'ip': ip})
                host_value.setdefault('retired', {})[ip] = {
                    'retired_at': now_text(),
                    'reason': 'expired_history',
                }
        def priority(entry):
            ip, item = entry
            return (
                bool(item.get('last_success')),
                parse_time(item.get('last_success') or ''),
                parse_time(item.get('last_seen') or ''),
                parse_time(item.get('first_seen') or ''),
                ip,
            )
        retired = host_value.get('retired') or {}
        if len(retained) + len(retired) > per_host:
            active_ordered = sorted(retained.items(), key=priority, reverse=True)
            keep_active = active_ordered[:min(len(active_ordered), per_host)]
            remaining = per_host - len(keep_active)
            retired_ordered = sorted(
                retired.items(),
                key=lambda entry: (parse_time(entry[1].get('retired_at') or ''), entry[0]),
                reverse=True)
            keep_retired = retired_ordered[:remaining]
            for ip, _ in active_ordered[len(keep_active):]:
                removed_capped.append({'host': host, 'ip': ip})
            for ip, _ in retired_ordered[len(keep_retired):]:
                removed_capped.append({'host': host, 'ip': ip, 'retired': True})
            retained = dict(keep_active)
            retired = dict(keep_retired)
        host_value['ips'] = retained
        host_value['retired'] = retired

    current_tags = set(nodes)
    for tag in list((catalog.get('nodes') or {}).keys()):
        if tag not in current_tags:
            removed_nodes.append(tag)
            del catalog['nodes'][tag]
            continue
        host = nodes[tag]['host']
        host_ips = set(((catalog.get('hosts') or {}).get(host) or {}).get('ips') or {})
        probes = catalog['nodes'][tag].get('probes') or {}
        catalog['nodes'][tag]['probes'] = {ip: value for ip, value in probes.items()
                                           if ip in host_ips}
    cleanup = {
        'checked_at': now_text(),
        'success_retention_days': success_days,
        'failure_retention_days': failure_days,
        'history_per_host': per_host,
        'removed_expired': len(removed_expired),
        'removed_capped': len(removed_capped),
        'removed_nodes': len(removed_nodes),
        'remaining_hosts': len(catalog.get('hosts') or {}),
        'remaining_endpoints': sum(len((value.get('ips') or {}))
                                   for value in (catalog.get('hosts') or {}).values()),
        'retired_tombstones': sum(len((value.get('retired') or {}))
                                  for value in (catalog.get('hosts') or {}).values()),
    }
    if removed_expired or removed_capped or removed_nodes:
        cleanup['samples'] = {
            'expired': removed_expired[:5],
            'capped': removed_capped[:5],
            'nodes': removed_nodes[:5],
        }
    catalog['last_cleanup'] = cleanup
    return cleanup


def probe_candidates(catalog, node, node_item, limit):
    """Choose a bounded mix of unseen, recently seen and successful endpoints."""
    host_ips = ((catalog.get('hosts') or {}).get(node['host']) or {}).get('ips') or {}
    probes = node_item.get('probes') or {}

    def priority(ip):
        endpoint = host_ips.get(ip) or {}
        probe = probes.get(ip) or {}
        unseen = not probe.get('checked_at')
        return (
            unseen,
            parse_time(endpoint.get('last_seen') or ''),
            parse_time(endpoint.get('last_success') or ''),
            ip,
        )
    return sorted(host_ips, key=priority, reverse=True)[:limit]


def healthy_endpoints(catalog, nodes, refresh_minutes=60):
    result = {}
    now = time.time()
    for tag, node in nodes.items():
        probes = (catalog.get('nodes', {}).get(tag) or {}).get('probes') or {}
        good = []
        for ip, item in probes.items():
            if not item.get('ok'):
                continue
            if now - parse_time(item.get('checked_at', '')) > refresh_minutes * 60 * 2:
                continue
            good.append((int(item.get('total_ms', 999999)), ip, item))
        good.sort()
        result[tag] = good[:MAX_ENDPOINTS_PER_NODE]
    return result


def render(source, catalog, health_minutes=5, refresh_minutes=60):
    nodes = node_definitions(source)
    healthy = healthy_endpoints(catalog, nodes, refresh_minutes)
    output, summary = [], {'nodes_with_candidates': 0, 'endpoint_count': 0, 'nodes': {}}
    for item in source.get('outbounds', []):
        tag = item.get('tag')
        if tag not in nodes or item.get('type') in ('selector', 'urltest'):
            output.append(item)
            continue
        candidates = healthy.get(tag) or []
        if not candidates:
            output.append(item)
            continue
        children = []
        for index, (_, ip, probe_item) in enumerate(candidates, 1):
            child = json.loads(json.dumps(item))
            child['tag'] = f'{tag}-ep{index}'
            child['server'] = ip
            children.append(child)
        output.extend(children)
        output.append({
            'type': 'urltest', 'tag': tag, 'outbounds': [x['tag'] for x in children],
            'url': 'https://www.gstatic.com/generate_204',
            'interval': f'{max(1, int(health_minutes))}m', 'tolerance': 50,
            'interrupt_exist_connections': False,
        })
        summary['nodes_with_candidates'] += 1
        summary['endpoint_count'] += len(candidates)
        summary['nodes'][tag] = [
            {'ip': ip, 'probe_ms': probe_item.get('total_ms')} for _, ip, probe_item in candidates
        ]
    rendered = json.loads(json.dumps(source))
    rendered['outbounds'] = output
    rendered['endpoint_selection'] = {
        'updated_at': catalog.get('updated_at'), **summary,
        'health_check_minutes': health_minutes,
        'refresh_minutes': refresh_minutes,
        'history_cleanup': catalog.get('last_cleanup'),
    }
    return rendered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=SOURCE)
    parser.add_argument('--output', type=Path, default=RENDERED)
    parser.add_argument('--settings', type=Path, default=ROOT/'settings.json')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    from settings_config import load_settings
    settings = load_settings(ROOT, settings_path=args.settings)
    source = json.loads(args.input.read_text(encoding='utf-8'))
    catalog, _ = refresh(settings, source, force=args.force)
    rendered = render(source, catalog,
                      health_minutes=settings.get('SakuraHealthCheckMinutes', 5),
                      refresh_minutes=settings.get('SakuraEndpointRefreshMinutes', 60))
    write_json(args.output, rendered)
    print(json.dumps({
        'ok': True,
        'nodes_with_candidates': rendered['endpoint_selection']['nodes_with_candidates'],
        'endpoint_count': rendered['endpoint_selection']['endpoint_count'],
        'last_run': catalog.get('last_run'),
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
