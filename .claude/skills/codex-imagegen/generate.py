"""Generate or edit an image with Codex CLI's built-in image_gen tool.

This wrapper only orchestrates `codex exec`; authentication stays in the
Codex CLI's own ChatGPT OAuth store and is never read or logged here.

The wrapper never asks the Codex agent to save the image. It watches the JSON
event stream for the image_gen result, copies the reported file to the
requested output path, and ends the Codex process as soon as the image is
valid. This keeps a stalled agent turn from blocking the caller.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
PATH_RE = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
CODEX_HOME = Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex'))
GENERATED_ROOT = CODEX_HOME / 'generated_images'
IS_WINDOWS = os.name == 'nt'


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


def saved_paths(value):
    """Yield image_gen saved paths from a decoded JSON event."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            path = current.get('savedPath')
            kind = str(current.get('kind') or '')
            if isinstance(path, str) and path and 'image' in kind.lower():
                yield path
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def newest_image(root, since):
    """Return the newest valid image under two levels of root, newer than since."""
    root = Path(root)
    if not root.exists():
        return None
    best = None
    best_mtime = 0.0
    for entry in root.iterdir():
        candidates = [entry] if entry.is_file() else [
            item for item in entry.iterdir() if item.is_file()]
        for path in candidates:
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= since and mtime > best_mtime and valid_image(path):
                best, best_mtime = path, mtime
    return best


def terminate_tree(proc):
    """Stop the Codex process and its children without touching other processes."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15)
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_codex(command, prompt_stream, timeout):
    """Run codex, returning (returncode, saved_image, timed_out, stderr_tail).

    The event stream is consumed live so the caller can stop as soon as the
    image_gen tool reports a valid file.
    """
    creation_flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) if IS_WINDOWS else 0
    proc = subprocess.Popen(
        command, stdin=prompt_stream, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding='utf-8',
        errors='replace', bufsize=1, creationflags=creation_flags)

    events = queue.Queue()
    stderr_tail = []

    def pump_stdout():
        try:
            for line in proc.stdout:
                events.put(line)
        finally:
            events.put(None)

    def pump_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            if len(stderr_tail) > 40:
                del stderr_tail[0]

    threading.Thread(target=pump_stdout, daemon=True).start()
    threading.Thread(target=pump_stderr, daemon=True).start()

    deadline = time.monotonic() + timeout
    saved = None
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            line = events.get(timeout=min(remaining, 0.25))
        except queue.Empty:
            continue
        if line is None:
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for path in saved_paths(event):
            if valid_image(path):
                saved = Path(path)
                break
        if saved is not None:
            break
        if event.get('type') == 'error':
            message = event.get('message') or event.get('error')
            if message:
                stderr_tail.append(str(message)[:300])
                if len(stderr_tail) > 40:
                    del stderr_tail[0]

    if saved is not None or timed_out:
        terminate_tree(proc)
    try:
        returncode = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        terminate_tree(proc)
        returncode = None
    return returncode, saved, timed_out, stderr_tail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--cd', default='')
    parser.add_argument('--image', action='append', default=[])
    parser.add_argument('--model', default='')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--sandbox', choices=('read-only', 'workspace-write'),
                        default='read-only',
                        help='Codex shell sandbox; read-only keeps the agent from moving files')
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(args.cd).resolve() if args.cd else out.parent
    if not workdir.is_dir():
        raise SystemExit(f'working directory does not exist: {workdir}')
    started = time.time()
    before = image_files(out.parent) | image_files(workdir)

    prompt = (
        f'{args.prompt}\n\n'
        'Call the built-in image_gen tool exactly once and produce one image. '
        'Do not run shell commands. Do not copy, move, verify, or list files. '
        'End your turn immediately after the tool returns.'
    )
    with tempfile.TemporaryDirectory() as temporary:
        last = Path(temporary) / 'last.txt'
        prompt_file = Path(temporary) / 'prompt.txt'
        prompt_file.write_text(prompt, encoding='utf-8')
        codex = shutil.which('codex') or 'codex'
        command = [
            'exec', '--skip-git-repo-check', '--ephemeral',
            '-C', str(workdir), '--sandbox', args.sandbox,
            '--json', '-o', str(last),
        ]
        if workdir != out.parent and args.sandbox == 'workspace-write':
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
        with prompt_file.open('r', encoding='utf-8') as prompt_stream:
            try:
                returncode, saved, timed_out, stderr_tail = run_codex(
                    command, prompt_stream, args.timeout)
            except OSError as error:
                print(json.dumps({'ok': False, 'error': type(error).__name__}))
                return 1
        last_text = last.read_text(encoding='utf-8', errors='replace') if last.exists() else ''

    candidate = saved
    if candidate is None:
        reported = extract_path(last_text)
        if reported:
            candidate = Path(reported)
            if not candidate.is_absolute():
                candidate = (workdir / candidate).resolve()
            if not candidate.exists():
                candidate = None
    if candidate is None or not valid_image(candidate):
        candidate = newest_image(GENERATED_ROOT, started - 2)
    if candidate is None or not valid_image(candidate):
        changed = sorted(
            (image_files(out.parent) | image_files(workdir)) - before,
            key=lambda path: path.stat().st_mtime, reverse=True)
        candidate = changed[0] if changed else None

    if candidate is None or not valid_image(candidate):
        if timed_out:
            print(json.dumps({'ok': False, 'error': 'codex exec timed out'}))
            return 1
        detail = stderr_tail[-3:]
        if args.sandbox == 'read-only' and any('sandbox' in line.lower() for line in stderr_tail):
            detail.append('hint: retry with --sandbox workspace-write')
        print(json.dumps({'ok': False, 'error': 'codex exec failed',
                          'detail': detail}))
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
