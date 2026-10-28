"""End-to-end HTTP tests: authentication, per-user ownership, history and imports."""

import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import CheckName, CheckResult, CheckStatus
from app.storage import save_artifact
from isolation import restore_database, use_temporary_database

setUpModule = use_temporary_database
tearDownModule = restore_database

ALICE = "a" * 40
BOB = "b" * 40
AUTH = {"TESTPILOT_AUTH_ENABLED": "true", "TESTPILOT_API_TOKENS": f"alice:{ALICE},bob:{BOB}"}
WEBSITE = {"url": "https://example.com/", "authorization_confirmed": True, "checks": ["h1"]}


def fake_checks(status: CheckStatus, artifact: str | None = None):
    async def run(_request):
        evidence = {"count": 1 if status == CheckStatus.PASSED else 0}
        if artifact:
            evidence["screenshot_artifact_id"] = artifact
        return [CheckResult(check_id=CheckName.H1, status=status, message="Observed H1 headings.", evidence=evidence)]
    return run


def as_user(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def run_site(self, status: CheckStatus, headers: dict | None = None, artifact: str | None = None) -> dict:
        with patch("app.execution.run_website_checks", fake_checks(status, artifact)):
            response = self.client.post("/api/tests/website", json=WEBSITE, headers=headers or {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_responses_carry_security_headers(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_auth_is_required_and_each_user_sees_only_their_runs(self):
        with patch.dict(os.environ, AUTH):
            self.assertEqual(self.client.get("/api/runs").status_code, 401)
            self.assertEqual(self.client.get("/api/runs", headers=as_user("wrong")).status_code, 401)
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            artifact = str(uuid4())
            save_artifact(artifact, b"\xff\xd8fake-jpeg")
            alice_run = self.run_site(CheckStatus.FAILED, as_user(ALICE), artifact)
            run_id = alice_run["run_id"]

            self.assertEqual(self.client.get("/api/me", headers=as_user(ALICE)).json()["owner"], "alice")
            self.assertEqual(self.client.get(f"/api/runs/{run_id}", headers=as_user(ALICE)).status_code, 200)
            self.assertEqual(self.client.get(f"/api/runs/{run_id}", headers=as_user(BOB)).status_code, 404)
            self.assertEqual(self.client.get(f"/api/runs/{run_id}/html", headers=as_user(BOB)).status_code, 404)
            self.assertEqual(self.client.get(f"/api/artifacts/{artifact}", headers=as_user(ALICE)).status_code, 200)
            self.assertEqual(self.client.get(f"/api/artifacts/{artifact}", headers=as_user(BOB)).status_code, 404)
            self.assertNotIn(run_id, json.dumps(self.client.get("/api/runs", headers=as_user(BOB)).json()))
            self.assertEqual(self.client.delete(f"/api/runs/{run_id}", headers=as_user(BOB)).status_code, 404)

            html = self.client.get(f"/api/runs/{run_id}/html", headers=as_user(ALICE)).text
            self.assertIn("data:image/jpeg;base64,", html)

    def test_history_compare_delete_and_clear(self):
        first = self.run_site(CheckStatus.FAILED)
        second = self.run_site(CheckStatus.PASSED)
        comparison = self.client.get(
            "/api/runs/compare", params={"base": first["run_id"], "head": second["run_id"]}
        ).json()
        self.assertEqual(comparison["fixes"], 1)
        self.assertEqual(comparison["changes"][0]["change"], "fixed")
        self.assertTrue(comparison["same_target"])

        listed = self.client.get("/api/runs").json()
        self.assertEqual(listed[0]["checks"], ["h1"])
        self.assertEqual(self.client.delete(f"/api/runs/{first['run_id']}").json(), {"deleted": True})
        self.assertEqual(self.client.get(f"/api/runs/{first['run_id']}").status_code, 404)
        self.assertGreaterEqual(self.client.delete("/api/runs").json()["deleted"], 1)
        self.assertEqual(self.client.get("/api/runs").json(), [])

    def test_openapi_import_endpoint_rejects_non_specs(self):
        response = self.client.post("/api/openapi/import", json={"spec": "{\"hello\": 1}"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("openapi", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
