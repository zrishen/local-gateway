"""Stage endpoint updates until the live route needs repair and AI is idle."""
import datetime
import json
from pathlib import Path
import time
import urllib.request


def _port_list(settings):
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
    return ports or None


def active_request_stats(settings, opener):
    """Sum active streams and keep the oldest age across private backends."""
    ports = _port_list(settings)
    if not ports:
        return None
    total = 0
    oldest = 0.0
    for port in ports:
        try:
            with opener.open(f'http://127.0.0.1:{port}/__status', timeout=2) as response:
                data = json.load(response)
            value = int(data.get('active_ai_requests'))
            age = data.get('oldest_ai_request_age_seconds')
        except (OSError, ValueError, TypeError):
            return None
        total += max(0, value)
        if isinstance(age, (int, float)):
            oldest = max(oldest, max(0.0, float(age)))
    return total, oldest


def active_request_count(settings, opener):
    """Compatibility wrapper used by older callers."""
    stats = active_request_stats(settings, opener)
    return stats[0] if stats else None


def reload_decision(health, active_requests, oldest_request_age=None, now=None,
                    stale_seconds=600, recovery_failure_limit=2):
    now = time.time() if now is None else now
    if active_requests is None:
        return {'reload': False, 'reason': 'gateway unavailable'}
    if active_requests > 0:
        try:
            stale = float(stale_seconds)
        except (TypeError, ValueError):
            stale = 0.0
        if (stale <= 0 or oldest_request_age is None
                or float(oldest_request_age) < stale):
            return {'reload': False, 'reason': 'AI requests active'}
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
    if not failed:
        return {'reload': False, 'reason': 'Preserving a working route'}
    if active_requests > 0:
        try:
            limit = max(1, int(float(recovery_failure_limit)))
        except (TypeError, ValueError):
            limit = 2
        recovery_failures = health.get('recovery_failures', 0)
        if (last.get('health') != 'disconnected'
                or not isinstance(recovery_failures, (int, float))
                or recovery_failures < limit):
            return {'reload': False,
                    'reason': 'Route recovery needs idle AI'}
        return {'reload': True,
                'reason': 'Route disconnected and oldest AI request is stale'}
    return {'reload': True, 'reason': 'Route recovery exhausted; AI idle'}


def _setting_int(settings, key, default):
    try:
        return max(0, int(float(settings.get(key, default))))
    except (TypeError, ValueError):
        return default


def main():
    from settings_config import load_settings
    root = Path(__file__).resolve().parents[1]
    active = None
    oldest = None
    health = {}
    stale_seconds = 600
    recovery_failure_limit = 2
    try:
        settings = load_settings(root)
        stale_seconds = _setting_int(
            settings, 'SakuraEndpointReloadStaleRequestSeconds', 600)
        recovery_failure_limit = _setting_int(
            settings, 'SakuraEndpointReloadRecoveryFailures', 2)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        stats = active_request_stats(settings, opener)
        if stats:
            active, oldest = stats
        health = json.loads((root / 'state/sakura-health.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    print(json.dumps(reload_decision(
        health, active, oldest, stale_seconds=stale_seconds,
        recovery_failure_limit=recovery_failure_limit)))


if __name__ == '__main__':
    main()
