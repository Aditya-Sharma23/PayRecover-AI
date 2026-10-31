r"""Write PayRecover's OpenAPI description to docs/openapi.json, the file APIMatic imports.

Run from the repository root after changing an endpoint:
    backend\.venv\Scripts\python scripts\export_openapi.py
backend/tests/test_openapi_export.py fails while the committed file is out of date.
"""

import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "openapi.json"


def spec() -> dict:
    sys.path.insert(0, str(ROOT / "backend"))
    warnings.simplefilter("ignore")
    from app.main import app

    app.openapi_schema = None  # rebuild, so the default local server address is used
    return app.openapi()


def render() -> str:
    return json.dumps(spec(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
