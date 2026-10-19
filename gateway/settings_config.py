"""Load tracked defaults with optional machine-local overrides."""
import json
from pathlib import Path
import shutil
import subprocess
import sys


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, TypeError):
        return {}


def default_interface():
    if sys.platform != 'win32':
        return ''
    script = ("$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue "
              "| Sort-Object RouteMetric,InterfaceMetric | Select-Object -First 1; "
              "if ($route) {(Get-NetAdapter -InterfaceIndex $route.InterfaceIndex).Name}")
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def python_path():
    return shutil.which('python') or sys.executable or 'python'


def merge_settings(base, override):
    value = dict(base or {})
    value.update(override or {})
    return value


def load_settings(root=None, settings_path=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    base_path = Path(settings_path or root / 'settings.json')
    local_path = base_path.with_name('settings.local.json')
    settings = merge_settings(_read(base_path), _read(local_path))
    if not settings.get('PhysicalInterface'):
        detected = default_interface()
        if detected:
            settings['PhysicalInterface'] = detected
    if not settings.get('PythonPath'):
        settings['PythonPath'] = python_path()
    settings.setdefault('CompanyDomains', [])
    settings.setdefault('CompanyDns', '')
    settings.setdefault('CompanyDnsAlternative', '')
    settings.setdefault('AiUpstream', '')
    return settings
