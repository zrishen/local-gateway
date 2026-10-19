"""Write JSON with deterministic key ordering for safe content hashes."""
import argparse
import json
import os
from pathlib import Path


def canonicalize(path):
    path = Path(path)
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    canonicalize(args.path)


if __name__ == '__main__':
    main()
