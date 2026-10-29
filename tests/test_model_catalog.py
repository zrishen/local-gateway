import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ModelCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = {m['slug']: m for m in json.loads((ROOT / 'gateway/codex-api-models.json').read_text(encoding='utf-8'))['models']}

    def levels(self, slug):
        return [item['effort'] for item in self.models[slug]['supported_reasoning_levels']]

    def test_deepseek_exposes_only_verified_high_and_max(self):
        model = self.models['ds/deepseek-v4.1-flash']
        self.assertEqual(model['default_reasoning_level'], 'max')
        self.assertEqual(self.levels('ds/deepseek-v4.1-flash'), ['high', 'max'])

    def test_glm_exposes_verified_high_and_max(self):
        for slug in ('glm-req/glm-5.3', 'glm-req/glm-5.3-flash'):
            self.assertEqual(self.models[slug]['default_reasoning_level'], 'max')
            self.assertEqual(self.levels(slug), ['high', 'max'], slug)

    def test_luna_api_defaults_high_with_max_and_ultra(self):
        model = self.models['openai/gpt-6-luna']
        self.assertEqual(model['default_reasoning_level'], 'high')
        self.assertEqual(self.levels('openai/gpt-6-luna'),
                         ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'])

    def test_gemini_exposes_verified_all_reasoning_levels(self):
        self.assertEqual(self.models['google/gemini-3.8-flash']['default_reasoning_level'], 'high')
        self.assertEqual(self.levels('google/gemini-3.8-flash'),
                         ['low', 'medium', 'high', 'xhigh', 'max'])

    def test_third_party_models_use_475k_effective_context(self):
        for slug, model in self.models.items():
            with self.subTest(model=slug):
                self.assertEqual(model['context_window'], 500000)
                self.assertEqual(model['max_context_window'], 500000)
                self.assertEqual(model['effective_context_window_percent'], 95)
                self.assertEqual(
                    model['context_window'] * model['effective_context_window_percent'] // 100,
                    475000)

    def test_local_catalog_contains_only_namespaced_third_party_models(self):
        self.assertTrue(self.models)
        self.assertTrue(all('/' in slug for slug in self.models))


if __name__ == '__main__':
    unittest.main()
