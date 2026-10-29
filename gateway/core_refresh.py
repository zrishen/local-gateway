"""Stage endpoint updates until the live route needs repair and AI is idle."""
import datetime
import json
from pathlib import Path
import time
import urllib.request


def reload_decision(health, active_requests, now=None):
    now = time.time() if now is None else now
    if active_requests is None or active_requests > 0:
        return {'reload': False, 'reason': 'AI requests active or gateway unavailable'}
    if health.get('pending_switch'):
        return {'reload': False, 'reason': 'Node switch under observation'}
    last = health.get('last_health') or {}
    try:
        age = now - datetime.datetime.fromisoformat(last['checked_at']).timestamp()
    except (KeyError, ValueError, TypeError):
        age = float('inf')
    if not 0 <= age <= 900:
        return {'reload': False, 'reason': 'Waiting for fresh route health'}
    failed = (last.get('health') == 'disconnected'
              or (last.get('health') in ('low', 'blocked')
                  and health.get('consecutive_below_threshold', 0) >= 2))
    return {'reload': failed,
            'reason': 'Route recovery exhausted; AI idle' if failed else 'Preserving a working route'}


def active_request_count(settings, opener):
    """Sum active streams across private backends; stay conservative if any is down."""
    configured = settings.get('GatewayBackendPorts') or [settings['PublicPort']]
    if not isinstance(configured, (list, tuple)):
        configured = [configured]
    ports = []
    for value in configured:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return None
        if 0 < port < 65536 and port not in ports:
            ports.append(port)
    if not ports:
        return None
    total = 0
    for port in ports:
        try:
            with opener.open(f'http://127.0.0.1:{port}/__status', timeout=2) as response:
                value = int(json.load(response).get('active_ai_requests'))
        except (OSError, ValueError, TypeError):
            return None
        total += max(0, value)
    return total


def main():
    from settings_config import load_settings
    root = Path(__file__).resolve().parents[1]
    active = None
    health = {}
    try:
        settings = load_settings(root)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        active = active_request_count(settings, opener)
        health = json.loads((root / 'state/sakura-health.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    print(json.dumps(reload_decision(health, active)))


if __name__ == '__main__':
    main()
