"""Fetch a SakuraCat subscription and convert its trojan nodes to sing-box outbounds.

The subscription token and node credentials are written only to state/ and are
never printed by this tool. The generated file is consumed by Generate-Config.ps1.

Outbounds are arranged as nested selectors:
  sakura (active region) -> sakura-region-xx (node selector) -> node

This keeps every supported region available while only the active region is
measured by the local latency selector.
"""
import argparse
import base64
from datetime import datetime
import gzip
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL_FILE = ROOT / 'state/sakura-subscription-url.txt'
DEFAULT_OUTPUT = ROOT / 'state/sakura-outbounds.json'
DEFAULT_STATUS = ROOT / 'state/sakura-subscription.json'
DEFAULT_SETTINGS = ROOT / 'settings.json'
MAX_BODY = 8 * 1024 * 1024

REGION_ALIASES = [
    ('印度尼西亚', '印度尼西亚'),
    ('澳大利亚', '澳洲'),
    ('尼日利亚', '尼日利亚'),
    ('马来西亚', '马来西亚'),
    ('中国香港', '香港'),
    ('中国台湾', '台湾'),
    ('香港家宽', '香港'),
    ('台湾家宽', '台湾'),
    ('日本原生', '日本'),
    ('日本星链家宽', '日本'),
    ('韩国家宽', '韩国'),
    ('越南家宽', '越南'),
    ('美国', '美国'),
    ('香港', '香港'),
    ('台湾', '台湾'),
    ('日本', '日本'),
    ('新加坡', '新加坡'),
    ('韩国', '韩国'),
    ('英国', '英国'),
    ('法国', '法国'),
    ('德国', '德国'),
    ('加拿大', '加拿大'),
    ('澳洲', '澳洲'),
    ('越南', '越南'),
    ('泰国', '泰国'),
    ('印度', '印度'),
    ('菲律宾', '菲律宾'),
    ('土耳其', '土耳其'),
    ('阿联酋', '阿联酋'),
    ('挪威', '挪威'),
    ('意大利', '意大利'),
    ('乌克兰', '乌克兰'),
    ('阿根廷', '阿根廷'),
    ('巴西', '巴西'),
    ('澳门', '澳门'),
]

REGION_CODES = {
    '美国': 'us', '香港': 'hk', '台湾': 'tw', '日本': 'jp', '新加坡': 'sg',
    '韩国': 'kr', '英国': 'uk', '法国': 'fr', '德国': 'de', '加拿大': 'ca',
    '澳洲': 'au', '越南': 'vn', '泰国': 'th', '马来西亚': 'my', '印度': 'in',
    '印度尼西亚': 'id', '菲律宾': 'ph', '土耳其': 'tr', '阿联酋': 'ae',
    '挪威': 'no', '意大利': 'it', '乌克兰': 'ua', '阿根廷': 'ar', '巴西': 'br',
    '澳门': 'mo', '尼日利亚': 'ng', '其他': 'other',
}

REGION_FLAGS = {
    '美国': '🇺🇸', '香港': '🇭🇰', '台湾': '🇹🇼', '日本': '🇯🇵',
    '新加坡': '🇸🇬', '韩国': '🇰🇷', '英国': '🇬🇧', '法国': '🇫🇷',
    '德国': '🇩🇪', '加拿大': '🇨🇦', '澳洲': '🇦🇺',
    '越南': '🇻🇳', '泰国': '🇹🇭', '马来西亚': '🇲🇾', '印度': '🇮🇳',
    '印度尼西亚': '🇮🇩', '菲律宾': '🇵🇭', '土耳其': '🇹🇷', '阿联酋': '🇦🇪',
    '挪威': '🇳🇴', '意大利': '🇮🇹', '乌克兰': '🇺🇦', '阿根廷': '🇦🇷',
    '巴西': '🇧🇷', '澳门': '🇲🇴', '尼日利亚': '🇳🇬',
}


def timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def is_fresh(path, max_age_hours):
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        updated = datetime.strptime(payload['updated_at'], '%Y-%m-%dT%H:%M:%S%z')
        return (datetime.now(updated.tzinfo) - updated).total_seconds() <= max_age_hours * 3600
    except (OSError, ValueError, KeyError, TypeError):
        return False


def fetch(url, timeout):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'sing-box/1.14.3',
        'Accept': '*/*',
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BODY + 1)
        if response.headers.get('Content-Encoding', '').lower() == 'gzip':
            body = gzip.decompress(body)
    if len(body) > MAX_BODY:
        raise ValueError('subscription response is too large')
    return body


def fetch_with_retry(url, timeout, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return fetch(url, timeout)
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    raise last_error


def decode_payload(body):
    text = body.decode('utf-8-sig', errors='strict')
    if '://' in text:
        return text
    compact = ''.join(text.split())
    compact += '=' * ((4 - len(compact) % 4) % 4)
    try:
        decoded = base64.b64decode(compact, altchars=b'-_', validate=False).decode('utf-8')
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('subscription is neither a node list nor a base64 node list') from error
    if '://' not in decoded:
        raise ValueError('decoded subscription does not contain node URIs')
    return decoded


def parse_trojan(line, index):
    parsed = urllib.parse.urlsplit(line)
    if parsed.scheme.lower() != 'trojan':
        raise ValueError(f'unsupported node scheme: {parsed.scheme}')
    if not parsed.hostname or not parsed.port:
        raise ValueError(f'node {index} has no host or port')
    if not 1 <= parsed.port <= 65535:
        raise ValueError(f'node {index} has an invalid port')
    password = urllib.parse.unquote(parsed.username or '')
    if not password:
        raise ValueError(f'node {index} has no password')
    query = urllib.parse.parse_qs(parsed.query)
    server_name = (query.get('sni') or query.get('peer') or [parsed.hostname])[0]
    insecure = (query.get('allowInsecure') or ['0'])[0].lower() in ('1', 'true')
    name = urllib.parse.unquote(parsed.fragment).strip() or f'{parsed.hostname}:{parsed.port}'
    outbound = {
        'type': 'trojan',
        'server': parsed.hostname,
        'server_port': parsed.port,
        'password': password,
        'domain_resolver': 'china-dns',
        'tls': {
            'enabled': True,
            'server_name': server_name,
            'insecure': insecure,
        },
    }
    return outbound, name


def detect_region(name):
    for alias, region in sorted(REGION_ALIASES, key=lambda item: len(item[0]), reverse=True):
        if alias in name:
            return region
    for region, flag in REGION_FLAGS.items():
        if flag in name:
            return region
    return '其他'


def region_group_tag(region):
    code = REGION_CODES.get(region, REGION_CODES['其他'])
    return f'sakura-region-{code}'


def parse_nodes(text, include_multiplier=False):
    nodes = []
    names = {}
    schemes = {}
    seen = set()
    regions = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '://' not in line:
            continue
        scheme = line.split('://', 1)[0].lower()
        schemes[scheme] = schemes.get(scheme, 0) + 1
        if scheme != 'trojan':
            raise ValueError(f'unsupported node scheme: {scheme}')
        outbound, name = parse_trojan(line, len(nodes))
        if re.search(r'(剩余流量|剩余|到期|过期|重置流量|官网|客服|订阅)', name):
            continue
        if not include_multiplier and '倍消耗' in name:
            continue
        key = (outbound['server'], outbound['server_port'], outbound['password'],
               outbound['tls']['server_name'], outbound['tls']['insecure'])
        if key in seen:
            continue
        seen.add(key)
        tag = f'sakura-{len(nodes):03d}'
        outbound['tag'] = tag
        region = detect_region(name)
        nodes.append(outbound)
        names[tag] = name
        info = regions.setdefault(region, {
            'name': region,
            'code': REGION_CODES.get(region, REGION_CODES['其他']),
            'group': region_group_tag(region),
            'nodes': [],
        })
        info['nodes'].append(tag)
    if not nodes:
        raise ValueError('no supported nodes found')

    order = {name: index for index, name in enumerate(REGION_CODES)}
    region_names = sorted(regions, key=lambda name: order.get(name, 999))
    outbounds = list(nodes)
    region_groups = {}
    for region in region_names:
        info = regions[region]
        info['count'] = len(info['nodes'])
        group = {
            'type': 'selector',
            'tag': info['group'],
            'outbounds': info['nodes'],
            'default': info['nodes'][0],
            'interrupt_exist_connections': False,
        }
        outbounds.append(group)
        region_groups[region] = info
    top = {
        'type': 'selector',
        'tag': 'sakura',
        'outbounds': [region_groups[region]['group'] for region in region_names],
        'default': region_groups[region_names[0]]['group'],
        'interrupt_exist_connections': False,
    }
    outbounds.append(top)
    metadata = {
        'regions': [region_groups[region] for region in region_names],
        'region_groups': region_groups,
    }
    return outbounds, names, schemes, metadata


def configured_region(settings_path=DEFAULT_SETTINGS):
    try:
        from settings_config import load_settings
        settings = load_settings(ROOT, settings_path=settings_path)
        region = settings.get('SakuraRegion')
        if not region:
            legacy = settings.get('SakuraRegions') or []
            region = legacy[0] if legacy else None
        return region or '美国'
    except (OSError, ValueError, TypeError):
        return '美国'


def apply_active_region(outbounds, metadata, active_region, default_tag=None):
    region_groups = metadata.get('region_groups', {})
    if not region_groups:
        return
    if active_region not in region_groups:
        active_region = next(iter(region_groups))
    for region, info in region_groups.items():
        group = next((item for item in outbounds if item.get('tag') == info['group']), None)
        if not group:
            continue
        if region == active_region and default_tag in info['nodes']:
            group['default'] = default_tag
        else:
            group['default'] = info['nodes'][0]
    top = next((item for item in outbounds if item.get('tag') == 'sakura'), None)
    if top:
        top['default'] = region_groups[active_region]['group']
    metadata['active_region'] = active_region


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url-file', type=Path, default=DEFAULT_URL_FILE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--status', type=Path, default=DEFAULT_STATUS)
    parser.add_argument('--settings', type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument('--active-region')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--max-age-hours', type=float, default=12)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--include-multiplier', action='store_true')
    args = parser.parse_args()

    if not args.force and is_fresh(args.output, args.max_age_hours):
        print('subscription outbounds are fresh')
        return 0
    try:
        url = args.url_file.read_text(encoding='utf-8').strip()
    except OSError:
        write_json(args.status, {'updated_at': timestamp(), 'ok': False,
                                 'error': 'subscription URL is not configured'})
        print('subscription URL is not configured; SakuraCat fallback remains available')
        return 0
    if not url.startswith(('http://', 'https://')):
        write_json(args.status, {'updated_at': timestamp(), 'ok': False,
                                 'error': 'subscription URL is invalid'})
        print('subscription URL is invalid', file=sys.stderr)
        return 1

    try:
        body = fetch_with_retry(url, args.timeout)
        text = decode_payload(body)
        active_region = args.active_region or configured_region(args.settings)
        default_tag = None
        try:
            default_tag = json.loads((ROOT / 'state/sakura-selection.json').read_text(encoding='utf-8')).get('tag')
        except (OSError, ValueError, TypeError):
            pass
        outbounds, names, schemes, metadata = parse_nodes(text, args.include_multiplier)
        apply_active_region(outbounds, metadata, active_region, default_tag)
        active_region = metadata.get('active_region', active_region)
        updated = timestamp()
        payload = {
            'version': 2,
            'updated_at': updated,
            'count': len(names),
            'entries': sum(schemes.values()),
            'outbounds': outbounds,
            'names': names,
            'regions': metadata['regions'],
            'region_groups': metadata['region_groups'],
            'active_region': active_region,
        }
        write_json(args.output, payload)
        write_json(args.status, {
            'updated_at': updated,
            'ok': True,
            'count': payload['count'],
            'entries': payload['entries'],
            'schemes': schemes,
            'active_region': active_region,
            'regions': metadata['regions'],
            'output': str(args.output),
            'error': None,
        })
        print(f"subscription updated: {payload['count']} nodes, active region {active_region}")
        return 0
    except Exception as error:
        try:
            previous = json.loads(args.output.read_text(encoding='utf-8'))
            previous_count = int(previous.get('count', 0))
        except (OSError, ValueError, TypeError):
            previous_count = 0
        write_json(args.status, {
            'updated_at': timestamp(),
            'ok': False,
            'stale': previous_count > 0,
            'usable': previous_count > 0,
            'count': previous_count,
            'error': f'{type(error).__name__}: {error}',
            'output': str(args.output),
        })
        print(f'subscription update failed: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())