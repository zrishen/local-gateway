"""Install/remove only the marked Codex gateway settings, preserving other config."""
import argparse
import json
from pathlib import Path
import re
import secrets
import tomllib

ROOT = Path(__file__).resolve().parent
CODEX = Path.home() / '.codex'
BACKUP = ROOT / 'work/codex-switch/backup'
MARK = 'UNIFIED-CODEX-SWITCH'


def remove_block(text, suffix):
    return re.sub(r'(?m)^# BEGIN ' + MARK + '-' + suffix + r'\r?\n.*?^# END ' + MARK + '-' + suffix + r'\r?\n?',
                  '', text, flags=re.S)


def configure(rollback=False, env_only=False):
    config_path = CODEX / 'config.toml'
    env_path = CODEX / '.env'
    config = config_path.read_text(encoding='utf-8-sig')
    env = env_path.read_text(encoding='utf-8-sig') if env_path.exists() else ''
    clean_config = remove_block(remove_block(config, 'SETTINGS'), 'PROVIDER')
    clean_env = remove_block(env, 'ENV')
    if rollback:
        if clean_config != config:
            tomllib.loads(clean_config)
            config_path.write_text(clean_config, encoding='utf-8')
        if clean_env != env:
            original = json.loads((BACKUP / 'manifest.json').read_text(encoding='utf-8'))
            if not clean_env.strip() and not original['env_existed']:
                env_path.unlink()
            else:
                env_path.write_text(clean_env, encoding='utf-8')
        print('Removed unified Codex settings; other config and official login preserved. Restart the App.')
        return
    parsed = tomllib.loads(clean_config)
    if parsed.get('model_provider') not in (None, 'openai') or parsed.get('model_catalog_json'):
        raise RuntimeError('A different active provider/catalog is configured; no changes made')
    secret_path = ROOT / 'state/codex-router-secret.txt'
    if not secret_path.exists():
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    secret = secret_path.read_text(encoding='utf-8').strip()
    env_block = (f'# BEGIN {MARK}-ENV\nUNIFIED_CODEX_GATEWAY_KEY={secret}\n# END {MARK}-ENV\n')
    env_path.write_text(clean_env.rstrip() + ('\n\n' if clean_env.strip() else '') + env_block, encoding='utf-8')
    if env_only:
        print('Prepared local gateway environment; active model configuration unchanged.')
        return
    settings = (f'# BEGIN {MARK}-SETTINGS\n'
                'model_provider = "unified_local"\n'
                'model_catalog_json = "C:/code/local-gateway/state/codex-models.json"\n'
                f'# END {MARK}-SETTINGS\n')
    provider = (f'# BEGIN {MARK}-PROVIDER\n'
                '[model_providers.unified_local]\n'
                'name = "Official subscription + local API"\n'
                'base_url = "http://127.0.0.1:8799/codex"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n'
                'supports_websockets = false\n'
                'env_http_headers = { "X-Unified-Codex-Key" = "UNIFIED_CODEX_GATEWAY_KEY" }\n'
                f'# END {MARK}-PROVIDER\n')
    # Top-level settings must precede all TOML tables.
    output = settings + clean_config.rstrip() + '\n\n' + provider
    tomllib.loads(output)
    config_path.write_text(output, encoding='utf-8')
    print('Configured official/API picker for new App chats; restart required once. Official login unchanged.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--env-only', action='store_true')
    args = parser.parse_args()
    configure(args.rollback, args.env_only)
