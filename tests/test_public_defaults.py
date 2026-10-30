import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicDefaultsTests(unittest.TestCase):
    def test_tracked_settings_contain_no_machine_identity(self):
        settings = json.loads((ROOT/'settings.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(settings['CompanyDomains'], [])
        self.assertEqual(settings['CompanyDns'], '')
        self.assertEqual(settings['CompanyDnsAlternative'], '')
        self.assertEqual(settings['PhysicalInterface'], '')
        self.assertEqual(settings['PythonPath'], '')
        self.assertEqual(settings['AiUpstream'], '')

    def test_upstream_dns_override_default_is_empty(self):
        value = json.loads((ROOT/'gateway/upstream-dns-overrides.json').read_text(encoding='utf-8'))
        self.assertEqual(value, {})

    def test_official_anthropic_bridge_defaults(self):
        settings = json.loads((ROOT/'settings.json').read_text(encoding='utf-8-sig'))
        self.assertTrue(settings['CodexAnthropicBridgeEnabled'])
        self.assertEqual(settings['CodexAnthropicOfficialModels'],
                         ['openai/gpt-6.1-sol'])
        self.assertEqual(settings['CodexAnthropicBridgeMaxBodyBytes'], 33554432)
        self.assertEqual(settings['CodexAnthropicBridgeClientVersion'], '')


if __name__ == '__main__':
    unittest.main()
