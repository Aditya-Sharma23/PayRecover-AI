"""docs/openapi.json, the file APIMatic imports, matches what the backend serves."""

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def exporter():
    spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenApiExportTests(unittest.TestCase):
    def test_the_committed_description_is_current(self):
        with patch.dict(os.environ, {"TESTPILOT_PUBLIC_URL": ""}):
            fresh = exporter().render()
        committed = (ROOT / "docs" / "openapi.json").read_text(encoding="utf-8")
        self.assertEqual(committed, fresh, r"Run: backend\.venv\Scripts\python scripts\export_openapi.py")

    def test_it_is_ready_for_sdk_generation(self):
        with patch.dict(os.environ, {"TESTPILOT_PUBLIC_URL": ""}):
            spec = exporter().spec()
        ids = [op["operationId"] for methods in spec["paths"].values() for op in methods.values()]
        self.assertEqual(len(ids), len(set(ids)))
        for name in ("investigate_order", "scan_store", "approve_recovery", "list_recoveries", "evidence_pack"):
            self.assertIn(name, ids)
        self.assertTrue(spec["servers"][0]["url"].startswith("http"))
        self.assertIn("bearerAuth", spec["components"]["securitySchemes"])


if __name__ == "__main__":
    unittest.main()
