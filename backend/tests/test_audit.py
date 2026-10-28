import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.audit import write_event


class AuditTests(unittest.TestCase):
    def test_audit_event_is_structured_and_does_not_require_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.log"
            with patch.dict("os.environ", {"TESTPILOT_AUDIT_LOG_PATH": str(path)}, clear=True):
                write_event({"method": "POST", "path": "/api/jobs", "status_code": 202})
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["status_code"], 202)
        self.assertIn("timestamp", payload)


if __name__ == "__main__":
    unittest.main()
