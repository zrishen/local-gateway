"""Read a private gateway backend's active AI request count."""
import json
from pathlib import Path
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def active_count(port, timeout=2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{int(port)}/__status', timeout=timeout) as response:
        value = json.load(response).get('active_ai_requests', 0)
    return max(0, int(value))


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: status_probe.py PORT')
    print(active_count(sys.argv[1]))


if __name__ == '__main__':
    main()
