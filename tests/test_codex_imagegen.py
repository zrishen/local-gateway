import contextlib
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/codex-imagegen'))
import generate

PNG = b'\x89PNG\r\n\x1a\n'


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


class WrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / 'generated' / 'image.png'

    def run_wrapper(self, fake_run, extra=()):
        argv = ['generate.py', '--prompt', 'a test image',
                '--out', str(self.out), '--cd', str(self.root), *extra]
        output = StringIO()
        with patch.object(generate.subprocess, 'run', side_effect=fake_run), \
                patch.object(generate.shutil, 'which', return_value=None), \
                patch.object(generate.sys, 'argv', argv), \
                contextlib.redirect_stdout(output):
            code = generate.main()
        return code, json.loads(output.getvalue().strip())

    def test_success_writes_the_requested_image(self):
        commands = []
        prompts = []

        def fake_run(command, **kwargs):
            commands.append(command)
            prompts.append(kwargs['stdin'].read())
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_bytes(PNG + b'x' * 32)
            return SimpleNamespace(returncode=0, stderr='')

        code, result = self.run_wrapper(fake_run, ('--image', str(self.root / 'ref.png')))
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['path'], str(self.out))
        command = commands[0]
        self.assertIn('-i', command)
        self.assertEqual(command[-1], '-')
        self.assertTrue(prompts[0].startswith('a test image'))

    def test_failure_is_reported_without_an_image(self):
        def fake_run(command, **kwargs):
            return SimpleNamespace(returncode=1, stderr='denied')

        code, result = self.run_wrapper(fake_run)
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'codex exec failed')

    def test_missing_output_is_an_error(self):
        def fake_run(command, **kwargs):
            return SimpleNamespace(returncode=0, stderr='')

        code, result = self.run_wrapper(fake_run)
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'no generated image found')

    def test_timeout_is_an_error(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        code, result = self.run_wrapper(fake_run)
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertIn('timed out', result['error'])


if __name__ == '__main__':
    unittest.main()
