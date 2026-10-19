import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import stable_json


class StableJsonTests(unittest.TestCase):
    def test_different_property_order_produces_identical_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root/'first.json', root/'second.json'
            first.write_text('{"b":2,"a":{"d":4,"c":3}}', encoding='utf-8')
            second.write_text('{"a":{"c":3,"d":4},"b":2}', encoding='utf-8')
            stable_json.canonicalize(first)
            stable_json.canonicalize(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(json.loads(first.read_text(encoding='utf-8')),
                             {'a': {'c': 3, 'd': 4}, 'b': 2})

    def test_list_order_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'config.json'
            path.write_text('{"items":[3,1,2]}', encoding='utf-8')
            stable_json.canonicalize(path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['items'],
                             [3, 1, 2])


if __name__ == '__main__':
    unittest.main()
