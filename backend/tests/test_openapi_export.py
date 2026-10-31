"""docs/openapi.json, the file APIMatic imports, matches what the backend serves."""

import importlib.util
import json
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


def shape(spec: dict) -> dict:
    """Endpoints with their methods and operation ids, plus the names of the data models."""
    return {
        "operations": sorted((path, method, op.get("operationId"))
                             for path, methods in spec["paths"].items() for method, op in methods.items()),
        "models": sorted(spec.get("components", {}).get("schemas", {})),
        "tags": [tag["name"] for tag in spec.get("tags", [])],
    }


class OpenApiExportTests(unittest.TestCase):
    def test_the_committed_description_is_current(self):
        # Compare the API's shape, not the exact text, so a newer FastAPI or pydantic formatting
        # detail cannot fail CI while a forgotten re-export after an endpoint change still does.
        with patch.dict(os.environ, {"TESTPILOT_PUBLIC_URL": ""}):
            fresh = exporter().spec()
        committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(shape(committed), shape(fresh), "Run scripts/export_openapi.py and commit docs/openapi.json.")

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
