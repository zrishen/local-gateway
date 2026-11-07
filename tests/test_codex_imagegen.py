import contextlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/codex-imagegen'))
import generate

PNG = b'\x89PNG\r\n\x1a\n'


def image_event(path):
    return json.dumps({
        'type': 'item.completed',
        'item': {'type': 'Extension', 'kind': 'image_gen.generation',
                 'savedPath': str(path)}}) + '\n'


class Hanging:
    """Never-ending iterator used to exercise the timeout path."""

    def __init__(self):
        self.stop = False

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(0.02)
        if self.stop:
            raise StopIteration
        return ''


class FakeProc:
    def __init__(self, lines=(), returncode=0, stderr_lines=()):
        self.stdout = iter(lines)
        self.stderr = iter(stderr_lines)
        self.returncode = returncode
        self.pid = 424242

    def poll(self):
        return self.returncode if self.returncode is not None else 0

    def wait(self, timeout=None):
        return self.returncode


class HelperTests(unittest.TestCase):
    def test_normalize_path_accepts_codex_absolute_links(self):
        self.assertEqual(generate.normalize_path('/C:/a%20b.png'), 'C:/a b.png')
        self.assertEqual(generate.normalize_path('<C:\\a\\b.png>'), 'C:\\a\\b.png')
        self.assertEqual(generate.normalize_path('`relative.png`'), 'relative.png')

    def test_extract_path_reads_markdown_and_plain_paths(self):
        self.assertEqual(
            generate.extract_path('[image](/C:/work/a.png)'),
            'C:/work/a.png')
        self.assertEqual(generate.extract_path('C:\\work\\a.PNG'),
                         'C:\\work\\a.PNG')
        self.assertIsNone(generate.extract_path('no image here'))

    def test_valid_image_checks_magic_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, data, expected in (
                    ('a.png', PNG + b'x' * 16, True),
                    ('a.jpg', b'\xff\xd8\xff' + b'x' * 16, True),
                    ('a.webp', b'RIFF' + b'0000' + b'WEBP' + b'x', True),
                    ('a.txt', b'not an image', False)):
                path = root / name
                path.write_bytes(data)
                self.assertEqual(generate.valid_image(path), expected, name)

    def test_image_files_only_returns_image_suffixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'a.png').write_bytes(PNG)
            (root / 'b.txt').write_text('x')
            (root / 'sub').mkdir()
            (root / 'sub' / 'c.webp').write_bytes(b'RIFF0000WEBP')
            self.assertEqual({path.name for path in generate.image_files(root)},
                             {'a.png', 'c.webp'})

    def test_saved_paths_reads_nested_image_events(self):
        event = {'type': 'event_msg', 'payload': {
            'type': 'item_completed',
            'item': {'type': 'Extension', 'kind': 'image_gen.generation',
                     'savedPath': 'C:/x/a.png'}}}
        self.assertEqual(list(generate.saved_paths(event)), ['C:/x/a.png'])

    def test_saved_paths_ignores_unrelated_dicts(self):
        event = {'type': 'item.completed',
                 'item': {'type': 'CommandExecution', 'savedPath': 'C:/x/a.png'}}
        self.assertEqual(list(generate.saved_paths(event)), [])

    def test_newest_image_prefers_recent_valid_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / 'session-id'
            session.mkdir()
            old = session / 'old.png'
            old.write_bytes(PNG + b'x' * 16)
            new = session / 'new.png'
            new.write_bytes(PNG + b'y' * 16)
            now = time.time()
            import os
            os.utime(old, (now - 3600, now - 3600))
            os.utime(new, (now, now))
            self.assertEqual(generate.newest_image(root, now - 2), new)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / 'generated' / 'image.png'
        self.generated = self.root / 'codex-home' / 'generated_images'
        self.generated.mkdir(parents=True)

    def run_wrapper(self, proc_factory, extra=()):
        commands = []
        prompts = []
        proc_holder = {}

        def fake_popen(command, **kwargs):
            commands.append(command)
            prompts.append(kwargs['stdin'].read())
            proc = proc_factory(command)
            proc_holder['proc'] = proc
            return proc

        argv = ['generate.py', '--prompt', 'a test image',
                '--out', str(self.out), '--cd', str(self.root), *extra]
        output = StringIO()
        with patch.object(generate.subprocess, 'Popen', side_effect=fake_popen), \
                patch.object(generate, 'terminate_tree'), \
                patch.object(generate, 'GENERATED_ROOT', self.generated), \
                patch.object(generate.shutil, 'which', return_value=None), \
                patch.object(generate.sys, 'argv', argv), \
                contextlib.redirect_stdout(output):
            code = generate.main()
        return code, json.loads(output.getvalue().strip()), commands, prompts

    def test_streamed_saved_path_is_copied_without_waiting(self):
        source = self.generated / 'session' / 'exec-uuid.png'
        source.parent.mkdir()
        source.write_bytes(PNG + b'x' * 32)

        def factory(command):
            return FakeProc([image_event(source)])

        code, result, commands, prompts = self.run_wrapper(factory)
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(Path(result['path']), self.out)
        self.assertEqual(self.out.read_bytes(), PNG + b'x' * 32)
        self.assertIn('--sandbox', commands[0])
        self.assertEqual(commands[0][commands[0].index('--sandbox') + 1], 'read-only')
        self.assertIn('Do not copy, move, verify, or list files.', prompts[0])
        self.assertNotIn(str(self.out), prompts[0])

    def test_workspace_write_is_passed_through(self):
        source = self.generated / 'session' / 'exec-uuid.png'
        source.parent.mkdir()
        source.write_bytes(PNG + b'x' * 32)

        code, result, commands, _ = self.run_wrapper(
            lambda command: FakeProc([image_event(source)]),
            ('--sandbox', 'workspace-write'))
        self.assertEqual(code, 0)
        self.assertEqual(commands[0][commands[0].index('--sandbox') + 1],
                         'workspace-write')

    def test_image_reference_is_forwarded(self):
        source = self.generated / 'session' / 'exec-uuid.png'
        source.parent.mkdir()
        source.write_bytes(PNG + b'x' * 32)
        reference = self.root / 'ref.png'
        reference.write_bytes(PNG + b'r' * 16)

        code, result, commands, _ = self.run_wrapper(
            lambda command: FakeProc([image_event(source)]),
            ('--image', str(reference)))
        self.assertEqual(code, 0)
        self.assertIn('-i', commands[0])
        self.assertEqual(commands[0][commands[0].index('-i') + 1], str(reference))

    def test_timeout_still_delivers_an_image_already_generated(self):
        source = self.generated / 'session' / 'exec-uuid.png'
        source.parent.mkdir()
        source.write_bytes(PNG + b'x' * 32)
        hanging = Hanging()

        def factory(command):
            self.addCleanup(setattr, hanging, 'stop', True)
            proc = FakeProc()
            proc.stdout = hanging
            proc.stderr = Hanging()
            return proc

        code, result, _, _ = self.run_wrapper(factory, ('--timeout', '1'))
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(self.out.read_bytes(), PNG + b'x' * 32)

    def test_timeout_without_image_is_an_error(self):
        hanging = Hanging()

        def factory(command):
            self.addCleanup(setattr, hanging, 'stop', True)
            proc = FakeProc()
            proc.stdout = hanging
            proc.stderr = Hanging()
            return proc

        code, result, _, _ = self.run_wrapper(factory, ('--timeout', '1'))
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertIn('timed out', result['error'])

    def test_last_message_path_is_used_when_no_event_arrives(self):
        source = self.root / 'elsewhere' / 'picked.png'
        source.parent.mkdir()
        source.write_bytes(PNG + b'z' * 32)

        def factory(command):
            last = Path(command[command.index('-o') + 1])
            last.write_text(f'[image]({source})\n', encoding='utf-8')
            return FakeProc([])

        code, result, _, _ = self.run_wrapper(factory)
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(self.out.read_bytes(), PNG + b'z' * 32)

    def test_recent_file_in_codex_home_is_used_as_fallback(self):
        source = self.generated / 'session' / 'exec-uuid.png'
        source.parent.mkdir()
        source.write_bytes(PNG + b'f' * 32)

        code, result, _, _ = self.run_wrapper(lambda command: FakeProc([]))
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])

    def test_missing_output_is_an_error(self):
        code, result, _, _ = self.run_wrapper(lambda command: FakeProc([]))
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'codex exec failed')

    def test_nonzero_exit_is_reported(self):
        code, result, _, _ = self.run_wrapper(
            lambda command: FakeProc([], returncode=1, stderr_lines=['denied\n']))
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'codex exec failed')

    def test_error_event_is_reported_in_detail(self):
        line = json.dumps({'type': 'error',
                           'message': 'model not supported'}) + '\n'
        code, result, _, _ = self.run_wrapper(
            lambda command: FakeProc([line], returncode=1))
        self.assertEqual(code, 1)
        self.assertEqual(result['error'], 'codex exec failed')
        self.assertIn('model not supported', result['detail'][-1])


if __name__ == '__main__':
    unittest.main()
