"""Sync account-specific official models; keep only third-party entries locally.

The generated catalog is consumed at Codex startup. Official model metadata is
copied intact, including future fields; authentication never enters the cache.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
LOCAL_CATALOG = ROOT / 'gateway/codex-api-models.json'
GENERATED_CATALOG = ROOT / 'state/codex-models.generated.json'
SYNC_TIMES = ((9, 10), (18, 0))
SYNC_TIMEZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')
POLL_SECONDS = 60
MAX_CATALOG_BYTES = 16 * 1024 * 1024
_version_cache = {}


def scheduled_sync_times(now):
    """Return the latest due slot and next slot in the user's Beijing time."""
    local = datetime.fromtimestamp(now, SYNC_TIMEZONE)
    slots = [local.replace(hour=hour, minute=minute, second=0, microsecond=0)
             for hour, minute in SYNC_TIMES]
    previous = max([slots[-1] - timedelta(days=1)]
                   + [slot for slot in slots if slot <= local])
    following = min([slots[0] + timedelta(days=1)]
                    + [slot for slot in slots if slot > local])
    return previous.timestamp(), following.timestamp()


def atomic_json(path, value):
    """Never expose a partial catalog to Codex or another gateway process."""
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    try:
        if path.read_text(encoding='utf-8') == payload:
            return
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(payload)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def optional_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def validate_catalog(value, official=False):
    if not isinstance(value, dict) or not isinstance(value.get('models'), list):
        raise ValueError('Invalid model catalog')
    if official and not value['models']:
        raise ValueError('Empty official model catalog')
    seen = set()
    for model in value['models']:
        if not isinstance(model, dict):
            raise ValueError('Invalid model entry')
        slug = model.get('slug')
        if not isinstance(slug, str) or not slug or slug in seen:
            raise ValueError('Invalid or duplicate model slug')
        if ('/' not in slug) != official:
            raise ValueError('Model is in the wrong catalog')
        seen.add(slug)
        if official and (not isinstance(model.get('display_name'), str)
                         or not isinstance(model.get('context_window'), int)
                         or model['context_window'] <= 0
                         or not isinstance(model.get('supported_reasoning_levels'), list)
                         or not model['supported_reasoning_levels']):
            raise ValueError('Incomplete official model metadata')
    return value


def local_catalog(path=LOCAL_CATALOG):
    return validate_catalog(json.loads(Path(path).read_text(encoding='utf-8')))


def merge_catalogs(official, local):
    validate_catalog(official, official=True)
    validate_catalog(local)
    return {**official, 'models': official['models'] + local['models']}


def account_credentials(auth_path):
    auth = json.loads(Path(auth_path).read_text(encoding='utf-8'))
    if not isinstance(auth, dict):
        raise ValueError('Invalid ChatGPT sign-in file')
    tokens = auth.get('tokens') or {}
    if not isinstance(tokens, dict):
        raise ValueError('Invalid ChatGPT tokens')
    token, account = tokens.get('access_token'), tokens.get('account_id')
    if (auth.get('auth_mode') != 'chatgpt' or not isinstance(token, str) or not token
            or not isinstance(account, str) or not account):
        raise ValueError('ChatGPT sign-in unavailable')
    identity = hashlib.sha256(account.encode('utf-8')).hexdigest()
    return identity, {'Authorization': 'Bearer ' + token, 'ChatGPT-Account-Id': account}


def codex_client_version():
    """Use the installed desktop backend, never a pinned historical version."""
    override = os.environ.get('CODEX_BINARY')
    if override:
        binary = Path(override)
    else:
        appdata = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
        binaries = list((appdata / 'OpenAI/Codex/bin').glob('*/codex.exe'))
        if binaries:
            binary = max(binaries, key=lambda path: path.stat().st_mtime_ns)
        else:
            installed = shutil.which('codex.exe')
            if not installed:
                raise FileNotFoundError('Installed Codex backend unavailable')
            binary = Path(installed)
    stat = binary.stat()
    key = (str(binary), stat.st_mtime_ns, stat.st_size)
    if key not in _version_cache:
        result = subprocess.run([str(binary), '--version'], capture_output=True,
                                text=True, check=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        match = re.fullmatch(r'codex-cli (\d+\.\d+\.\d+(?:[-+][\w.-]+)?)', result.stdout.strip())
        if not match:
            raise ValueError('Unrecognized Codex version')
        _version_cache.clear()
        _version_cache[key] = match.group(1)
    return _version_cache[key]


class CatalogHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('Official model catalog request failed')


def fetch_official_catalog(version, credentials):
    # Reuse the fixed ChatGPT host and official proxy lane. Do not follow
    # redirects or pass credentials to any endpoint supplied by a response.
    import codex_router
    target = '/backend-api/codex/models?' + urllib.parse.urlencode({'client_version': version})
    headers = {**credentials, 'Accept': 'application/json', 'Accept-Encoding': 'identity',
               'User-Agent': 'codex_cli_rs/' + version, 'originator': 'codex_cli_rs'}
    for attempt in range(2):
        connection = None
        try:
            connection = codex_router.official_connection()
            connection.timeout = 12
            connection.connect()
            connection.sock.settimeout(20)
            connection.request('GET', target, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise CatalogHTTPError(response.status)
            raw = response.read(MAX_CATALOG_BYTES + 1)
            if len(raw) > MAX_CATALOG_BYTES:
                raise ValueError('Official model catalog too large')
            return validate_catalog(json.loads(raw), official=True)
        except (OSError, http.client.HTTPException):
            mark_failed = getattr(codex_router, 'mark_official_lane_failed', None)
            if connection is not None and mark_failed is not None:
                mark_failed(connection)
            if attempt:
                raise
        finally:
            if connection is not None:
                connection.close()


class CatalogSync:
    def __init__(self, root=ROOT, local_path=LOCAL_CATALOG, auth_path=None,
                 fetcher=fetch_official_catalog, version_provider=codex_client_version,
                 clock=time.time):
        self.root = Path(root)
        self.local_path = Path(local_path)
        auth_root = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.auth_path = Path(auth_path) if auth_path is not None else auth_root / 'auth.json'
        self.output_path = self.root / 'state/codex-models.generated.json'
        self.cache_path = self.root / 'state/codex-official-models.json'
        self.status_path = self.root / 'state/codex-catalog-sync.json'
        self.fetcher, self.version_provider, self.clock = fetcher, version_provider, clock
        self.lock = threading.Lock()

    def refresh(self, force=False):
        with self.lock:
            return self._refresh(force)

    def _refresh(self, force):
        now = self.clock()
        latest_slot, next_slot = scheduled_sync_times(now)
        status = {'last_attempt': now, 'ok': False,
                  'schedule_timezone': 'Asia/Shanghai',
                  'schedule_times': [f'{hour:02d}:{minute:02d}' for hour, minute in SYNC_TIMES],
                  'next_scheduled_sync': next_slot}
        cache = optional_json(self.cache_path)
        if not isinstance(cache, dict):
            cache = {}
        try:
            local = local_catalog(self.local_path)
            # Bootstrap third-party choices even before the first sign-in.
            if not self.output_path.exists():
                atomic_json(self.output_path, local)
            identity, headers = account_credentials(self.auth_path)
            if cache.get('account') == identity:
                try:
                    validate_catalog(cache.get('catalog'), official=True)
                    if (not isinstance(cache.get('fetched_at'), (int, float))
                            or not isinstance(cache.get('client_version'), str)):
                        raise ValueError('Invalid model cache metadata')
                except ValueError:
                    cache = {}
            else:
                cache = {}
                # Never offer another account's cached models after a switch.
                atomic_json(self.output_path, local)
            if cache:
                atomic_json(self.output_path, merge_catalogs(cache['catalog'], local))
                status['last_success'] = cache['fetched_at']
            version = self.version_provider()
            status['client_version'] = version
            fetched_at = cache.get('fetched_at', 0)
            if (force or not cache or cache.get('client_version') != version
                    or fetched_at < latest_slot or fetched_at > now):
                official = validate_catalog(self.fetcher(version, headers), official=True)
                current_identity, _ = account_credentials(self.auth_path)
                if current_identity != identity:
                    atomic_json(self.output_path, local)
                    raise ValueError('ChatGPT account changed during sync')
                cache = {'account': identity, 'client_version': version,
                         'fetched_at': self.clock(), 'catalog': official}
                atomic_json(self.cache_path, cache)
                atomic_json(self.output_path, merge_catalogs(official, local))
            status.update(ok=True, last_success=cache['fetched_at'],
                          official_models=len(cache['catalog']['models']),
                          third_party_models=len(local['models']))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError,
                http.client.HTTPException, subprocess.SubprocessError) as error:
            # No response bodies, URLs, headers, token values, or account IDs.
            status['error'] = type(error).__name__
            if isinstance(error, CatalogHTTPError):
                status['http_status'] = error.status
        atomic_json(self.status_path, status)
        return status

    def run(self, stop=None):
        stop = stop if stop is not None else threading.Event()
        while not stop.is_set():
            try:
                status = self.refresh()
                if not status['ok']:
                    logging.warning('Codex model catalog sync deferred: %s', status.get('error'))
            except OSError as error:
                logging.warning('Codex model catalog state unavailable: %s', type(error).__name__)
            stop.wait(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    status = CatalogSync().refresh(force=args.force)
    print(json.dumps(status))
    raise SystemExit(0 if status['ok'] else 1)


if __name__ == '__main__':
    main()
