import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway'))
spec = importlib.util.spec_from_file_location('codex_configure', ROOT / 'Configure-Codex-Switch.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class CodexConfigureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.codex = self.root / 'codex'
        self.codex.mkdir()
        (self.root / 'gateway').mkdir()
        (self.root / 'gateway/codex-api-models.json').write_text(
            json.dumps({'models': [{'slug': 'provider/test'}]}), encoding='utf-8')
        self.config_path = self.codex / 'config.toml'
        self.original = 'model = "chosen-model"\nmodel_reasoning_effort = "high"\n[features]\napps = false\n'
        self.config_path.write_text(self.original, encoding='utf-8')
        for name, value in (('ROOT', self.root), ('CODEX', self.codex)):
            patcher = patch.object(configure, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_first_install_creates_generated_catalog_without_sign_in(self):
        configure.configure()
        settings = tomllib.loads(self.config_path.read_text(encoding='utf-8'))
        catalog = Path(settings['model_catalog_json'])
        self.assertEqual(catalog, self.root / 'state/codex-models.generated.json')
        self.assertEqual(json.loads(catalog.read_text(encoding='utf-8')),
                         {'models': [{'slug': 'provider/test'}]})
        self.assertEqual(settings['model'], 'chosen-model')
        self.assertEqual(settings['model_reasoning_effort'], 'high')
        self.assertFalse(settings['features']['apps'])

    def test_upgrade_replaces_old_catalog_path_and_is_repeatable(self):
        old = ('# BEGIN UNIFIED-CODEX-SWITCH-SETTINGS\n'
               'model_provider = "unified_local"\n'
               'model_catalog_json = "old-static-catalog.json"\n'
               '# END UNIFIED-CODEX-SWITCH-SETTINGS\n')
        self.config_path.write_text(old + self.original, encoding='utf-8')
        configure.configure()
        first = self.config_path.read_text(encoding='utf-8')
        configure.configure()
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), first)
        self.assertNotIn('old-static-catalog.json', first)
        self.assertEqual(first.count('model_catalog_json'), 1)


if __name__ == '__main__':
    unittest.main()
