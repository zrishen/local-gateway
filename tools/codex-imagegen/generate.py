"""Generate or edit an image with Codex CLI's built-in image_gen tool.

This wrapper only orchestrates `codex exec`; authentication stays in the
Codex CLI's own ChatGPT OAuth store and is never read or logged here.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
PATH_RE = re.compile(r'\[[^\]]*\]\(([^)]+)\)')


def image_files(root):
    root = Path(root)
    if not root.exists():
        return set()
    return {path for path in root.rglob('*')
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES}


def valid_image(path):
    try:
        with Path(path).open('rb') as stream:
            head = stream.read(16)
    except OSError:
        return False
    return (head.startswith(b'\x89PNG\r\n\x1a\n')
            or head.startswith(b'\xff\xd8\xff')
            or (head.startswith(b'RIFF') and head[8:12] == b'WEBP'))


def normalize_path(value):
    value = urllib.parse.unquote(value.strip().strip('`<> '))
    if re.match(r'^/[A-Za-z]:[/\\]', value):
        value = value[1:]
    return value


def extract_path(text):
    match = PATH_RE.search(text or '')
    if match:
        return normalize_path(match.group(1))
    for line in (text or '').splitlines():
        value = normalize_path(line)
        if value and Path(value).suffix.lower() in IMAGE_SUFFIXES:
            return value
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--cd', default='')
    parser.add_argument('--image', action='append', default=[])
    parser.add_argument('--model', default='')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(args.cd).resolve() if args.cd else out.parent
    if not workdir.is_dir():
        raise SystemExit(f'working directory does not exist: {workdir}')
    before = image_files(out.parent) | image_files(workdir)

    prompt = (
        f'{args.prompt}\n\n'
        'Use the built-in image_gen tool only; do not use the CLI fallback. '
        f'Save the final image at this exact path: {out}. '
        'Create parent directories if needed, verify the file is a valid image, '
        'and reply with its saved path.'
    )
    with tempfile.TemporaryDirectory() as temporary:
        last = Path(temporary) / 'last.txt'
        events = Path(temporary) / 'events.jsonl'
        prompt_file = Path(temporary) / 'prompt.txt'
        prompt_file.write_text(prompt, encoding='utf-8')
        codex = shutil.which('codex') or 'codex'
        command = [
            'exec', '--skip-git-repo-check', '--ephemeral',
            '-C', str(workdir), '--sandbox', 'workspace-write',
            '--json', '-o', str(last),
        ]
        if workdir != out.parent:
            command.extend(['--add-dir', str(out.parent)])
        if args.model:
            command.extend(['-m', args.model])
        for image in args.image:
            command.extend(['-i', str(Path(image).resolve())])
        command.append('-')
        if codex.lower().endswith(('.cmd', '.bat')):
            command = [os.environ.get('COMSPEC') or 'cmd.exe', '/c', codex, *command]
        else:
            command = [codex, *command]
        with events.open('w', encoding='utf-8') as log, \
                prompt_file.open('r', encoding='utf-8') as prompt_stream:
            try:
                result = subprocess.run(
                    command, stdin=prompt_stream, stdout=log,
                    stderr=subprocess.PIPE, text=True, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                print(json.dumps({'ok': False, 'error': 'codex exec timed out'}))
                return 1
            except OSError as error:
                print(json.dumps({'ok': False, 'error': type(error).__name__}))
                return 1
        if result.returncode != 0:
            detail = (result.stderr or '').strip().splitlines()
            print(json.dumps({'ok': False, 'error': 'codex exec failed',
                              'detail': detail[-3:] if detail else []}))
            return 1
        last_text = last.read_text(encoding='utf-8', errors='replace') if last.exists() else ''

    if not valid_image(out):
        reported = extract_path(last_text)
        candidate = None
        if reported:
            candidate = Path(reported)
            if not candidate.is_absolute():
                candidate = (workdir / candidate).resolve()
            if not candidate.exists():
                candidate = None
        if candidate is None:
            changed = sorted(
                (image_files(out.parent) | image_files(workdir)) - before,
                key=lambda path: path.stat().st_mtime, reverse=True)
            candidate = changed[0] if changed else None
        if candidate is None or not valid_image(candidate):
            print(json.dumps({'ok': False, 'error': 'no generated image found'}))
            return 1
        if candidate.resolve() != out:
            shutil.copy2(candidate, out)
    if not valid_image(out):
        print(json.dumps({'ok': False, 'error': 'output is not a valid image'}))
        return 1
    print(json.dumps({'ok': True, 'path': str(out),
                      'bytes': out.stat().st_size}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
