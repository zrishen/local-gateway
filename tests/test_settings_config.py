from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import settings_config


class SettingsConfigTests(unittest.TestCase):
    def test_local_file_overrides_without_mutating_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'settings.json').write_text(json.dumps({
                'PublicPort': 8799, 'PhysicalInterface': 'auto', 'CompanyDomains': []
            }), encoding='utf-8')
            (root/'settings.local.json').write_text(json.dumps({
                'PhysicalInterface': 'Ethernet', 'CompanyDomains': ['internal.example']
            }), encoding='utf-8')
            settings = settings_config.load_settings(root)
            self.assertEqual(settings['PublicPort'], 8799)
            self.assertEqual(settings['PhysicalInterface'], 'Ethernet')
            self.assertEqual(settings['CompanyDomains'], ['internal.example'])

    def test_missing_local_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'settings.json').write_text(json.dumps({
                'PublicPort': 8799, 'PhysicalInterface': 'Ethernet', 'PythonPath': 'python'
            }), encoding='utf-8')
            settings = settings_config.load_settings(root)
            self.assertEqual(settings['PublicPort'], 8799)
            self.assertEqual(settings['CompanyDomains'], [])
            self.assertEqual(settings['AiUpstream'], '')


if __name__ == '__main__':
    unittest.main()
