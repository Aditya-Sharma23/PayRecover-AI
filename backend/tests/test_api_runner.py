import asyncio
import json
import unittest
from unittest.mock import patch

from app.api_runner import ApiResponse, run_api_checks
from app.schemas import ApiRunRequest, CheckName, CheckStatus
from pydantic import ValidationError

USERS = {"data": {"items": [{"id": 7, "name": "Ada", "active": True}], "total": 1}, "next": None}


def run_with(response: ApiResponse, captured: dict | None = None, **options):
    request = ApiRunRequest(url="https://example.com/api", authorization_confirmed=True, **options)

    async def fake_fetch(_url: str, _timeout_seconds: int, headers=None):
        if captured is not None:
            captured["headers"] = headers
        return response

    with patch("app.api_runner._fetch_api", fake_fetch):
        return asyncio.run(run_api_checks(request))


def json_response(document, status: int = 200) -> ApiResponse:
    return ApiResponse(status, {"content-type": "application/json"}, 12, json.dumps(document).encode())


class ApiRunnerTests(unittest.TestCase):
    def test_content_type_check_requires_an_explicit_expectation(self):
        with self.assertRaises(ValidationError):
            ApiRunRequest(
                url="https://example.com/",
                authorization_confirmed=True,
                checks=[CheckName.API_CONTENT_TYPE],
            )

    def test_api_checks_evaluate_observed_response(self):
        results = run_with(
            ApiResponse(200, {"content-type": "application/json; charset=utf-8"}, 42, b"x" * 128),
            checks=[
                CheckName.API_STATUS,
                CheckName.API_RESPONSE_TIME,
                CheckName.API_CONTENT_TYPE,
                CheckName.API_REDIRECTS,
                CheckName.API_RESPONSE_SIZE,
            ],
            expected_status_code=200,
            expected_content_type="application/json",
            max_response_time_ms=1000,
        )
        self.assertEqual([result.status for result in results], [CheckStatus.PASSED] * 5)
        self.assertEqual(results[0].evidence["status_code"], 200)
        self.assertEqual(results[1].evidence["response_time_ms"], 42)
        self.assertEqual(results[4].evidence["response_size_bytes"], 128)

    def test_api_status_failure_is_evidence_backed(self):
        result = run_with(ApiResponse(200, {}, 12, b""), checks=[CheckName.API_STATUS], expected_status_code=201)[0]
        self.assertEqual(result.status, CheckStatus.FAILED)
        self.assertEqual(result.evidence["expected_status_code"], 201)

    def test_json_assertions_pass_and_fail_on_observed_fields(self):
        assertions = [
            {"path": "data.items.0.id", "operator": "equals", "expected": 7},
            {"path": "data.items.0.name", "operator": "contains", "expected": "Ad"},
            {"path": "data.items", "operator": "length", "expected": 1},
            {"path": "data.items.0.active", "operator": "type", "expected": "boolean"},
            {"path": "next", "operator": "exists"},
            {"path": "error", "operator": "not_exists"},
        ]
        passed = run_with(json_response(USERS), checks=[CheckName.API_JSON_ASSERTIONS], json_assertions=assertions)[0]
        self.assertEqual(passed.status, CheckStatus.PASSED, passed.evidence)

        failing = run_with(
            json_response(USERS),
            checks=[CheckName.API_JSON_ASSERTIONS],
            json_assertions=[
                {"path": "data.total", "operator": "equals", "expected": 2},
                {"path": "data.items.0.active", "operator": "equals", "expected": 1},
                {"path": "data.missing", "operator": "exists"},
            ],
        )[0]
        self.assertEqual(failing.status, CheckStatus.FAILED)
        self.assertEqual([item["passed"] for item in failing.evidence["assertions"]], [False, False, False])
        self.assertEqual(failing.evidence["assertions"][0]["actual"], 1)

    def test_non_json_body_fails_json_checks(self):
        result = run_with(
            ApiResponse(200, {"content-type": "text/html"}, 5, b"<html>"),
            checks=[CheckName.API_JSON_ASSERTIONS],
            json_assertions=[{"path": "id", "operator": "exists"}],
        )[0]
        self.assertEqual(result.status, CheckStatus.FAILED)
        self.assertIn("not valid JSON", result.message)

    def test_json_schema_violations_are_listed(self):
        schema = {
            "type": "object",
            "required": ["data"],
            "properties": {"data": {"type": "object", "required": ["total"], "properties": {"total": {"type": "string"}}}},
        }
        result = run_with(json_response(USERS), checks=[CheckName.API_JSON_SCHEMA], response_schema=schema)[0]
        self.assertEqual(result.status, CheckStatus.FAILED)
        self.assertEqual(result.evidence["errors"][0]["path"], "data.total")
        schema["properties"]["data"]["properties"]["total"]["type"] = "integer"
        ok = run_with(json_response(USERS), checks=[CheckName.API_JSON_SCHEMA], response_schema=schema)[0]
        self.assertEqual(ok.status, CheckStatus.PASSED)

    def test_custom_headers_are_sent_but_never_recorded(self):
        captured: dict = {}
        results = run_with(
            ApiResponse(200, {}, 5, b"{}"),
            captured,
            checks=[CheckName.API_STATUS],
            request_headers={"Authorization": "Bearer very-secret"},
        )
        self.assertEqual(captured["headers"], {"Authorization": "Bearer very-secret"})
        self.assertNotIn("very-secret", results[0].model_dump_json())

    def test_unsafe_headers_and_bad_inputs_are_rejected(self):
        base = {"url": "https://example.com/", "authorization_confirmed": True}
        for headers in ({"Host": "internal"}, {"X-A": "a\r\nInjected: 1"}, {"Bad Name": "x"}):
            with self.assertRaises(ValidationError):
                ApiRunRequest(**base, checks=[CheckName.API_STATUS], request_headers=headers)
        with self.assertRaises(ValidationError):
            ApiRunRequest(**base, checks=[CheckName.API_JSON_ASSERTIONS])
        with self.assertRaises(ValidationError):
            ApiRunRequest(**base, checks=[CheckName.API_JSON_SCHEMA], response_schema={"type": 12})
        with self.assertRaises(ValidationError):
            ApiRunRequest(**base, checks=[CheckName.API_JSON_ASSERTIONS],
                          json_assertions=[{"path": "a..b", "operator": "exists"}])


if __name__ == "__main__":
    unittest.main()
