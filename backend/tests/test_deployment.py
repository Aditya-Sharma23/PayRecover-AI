import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.deployment import deployment_readiness

TOKEN = "t" * 40
READY = {
    "TESTPILOT_PUBLIC_DEPLOYMENT": "true",
    "TESTPILOT_AUTH_ENABLED": "true",
    "TESTPILOT_API_TOKENS": f"alice:{TOKEN}",
    "TESTPILOT_RATE_LIMITING": "true",
    "TESTPILOT_AUDIT_LOGGING": "true",
    "TESTPILOT_PUBLIC_URL": "https://testpilot.example.org",
}


def readiness(values: dict, env_file_exists: bool = False) -> dict:
    with patch.dict(os.environ, values, clear=True), \
            patch("app.deployment.ENV_FILE", Path("missing.env") if not env_file_exists else Path(__file__)):
        return deployment_readiness()


class DeploymentTests(unittest.TestCase):
    def test_public_deployment_fails_closed_without_gates(self):
        result = readiness({"TESTPILOT_PUBLIC_DEPLOYMENT": "true"})
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["blockers"]), 5)

    def test_local_mode_is_not_blocked_by_production_gates(self):
        result = readiness({})
        self.assertTrue(result["ready"])
        self.assertFalse(result["public_deployment_requested"])

    def test_fully_configured_public_deployment_is_ready(self):
        result = readiness(READY)
        self.assertTrue(result["ready"], result["blockers"])
        self.assertTrue(result["controls"]["worker_process_isolation"])

    def test_short_tokens_plain_http_and_wildcard_cors_are_blocked(self):
        for change, expected in (
            ({"TESTPILOT_API_TOKENS": "alice:short"}, "at least 32 characters"),
            ({"TESTPILOT_PUBLIC_URL": "http://testpilot.example.org"}, "TLS is required"),
            ({"TESTPILOT_CORS_ORIGINS": "*"}, "explicit https:// origins"),
            ({"TESTPILOT_AUTH_ENABLED": "false"}, "TESTPILOT_AUTH_ENABLED"),
        ):
            result = readiness({**READY, **change})
            self.assertFalse(result["ready"])
            self.assertTrue(any(expected in blocker for blocker in result["blockers"]), result["blockers"])

    def test_a_local_env_file_blocks_public_deployment(self):
        result = readiness(READY, env_file_exists=True)
        self.assertIn("backend/.env must not exist", result["blockers"][0])

    def test_server_refuses_to_start_when_public_gates_fail(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with patch.dict(os.environ, {"TESTPILOT_PUBLIC_DEPLOYMENT": "true"}, clear=True):
            with self.assertRaises(RuntimeError):
                with TestClient(app):
                    pass


if __name__ == "__main__":
    unittest.main()
