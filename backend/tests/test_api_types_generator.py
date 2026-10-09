import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "apps/mobile/scripts/generate-api-types.py"
spec = importlib.util.spec_from_file_location("api_type_generator", SCRIPT)
generator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(generator)


class ApiTypeGeneratorTests(unittest.TestCase):
    def test_openapi_boolean_literals_render_as_typescript_literals(self):
        self.assertEqual(generator.render({"const": False}), "false")
        self.assertEqual(generator.render({"const": True}), "true")
        self.assertEqual(generator.render({"enum": [False, True]}), "false | true")
