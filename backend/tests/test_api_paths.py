import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from isolation import restore_database, use_temporary_database

from app.main import (
    _overall_status,
    _redact_target_url,
    _summary,
    run_website,
    target_preflight,
)
from app.schemas import (
    CheckName,
    CheckResult,
    CheckStatus,
    OverallStatus,
    TargetPreflightRequest,
    WebsiteRunRequest,
    ApiRunRequest,
)


LOCAL = SimpleNamespace(state=SimpleNamespace())
setUpModule = use_temporary_database
tearDownModule = restore_database


def make_request(*checks: CheckName) -> WebsiteRunRequest:
    return WebsiteRunRequest(
        url="https://example.com/",
        authorization_confirmed=True,
        checks=list(checks),
    )


class ApiPathTests(unittest.TestCase):
    def test_summary_and_overall_status_are_derived_from_records(self):
        checks = [
            CheckResult(
                check_id=CheckName.HTTP_STATUS,
                status=CheckStatus.PASSED,
                message="Observed HTTP status 200.",
            ),
            CheckResult(
                check_id=CheckName.H1,
                status=CheckStatus.FAILED,
                message="Observed zero H1 headings.",
            ),
        ]
        self.assertEqual(_summary(checks).model_dump(), {
            "passed": 1,
            "failed": 1,
            "warnings": 0,
            "errors": 0,
            "skipped": 0,
        })
        self.assertEqual(_overall_status(checks), OverallStatus.FAILED)
        self.assertEqual(_overall_status([]), OverallStatus.BLOCKED)

    def test_target_url_redacts_sensitive_query_values_and_fragment(self):
        redacted = _redact_target_url(
            "https://example.com/check?token=secret&keep=value&API_KEY=private#fragment"
        )
        self.assertEqual(
            redacted,
            "https://example.com/check?token=%5Bredacted%5D&keep=value&API_KEY=%5Bredacted%5D",
        )
        self.assertNotIn("secret", redacted)
        self.assertNotIn("private", redacted)
        self.assertNotIn("fragment", redacted)

    def test_preflight_converts_policy_failure_to_http_422(self):
        request = TargetPreflightRequest(
            url="https://example.com/",
            authorization_confirmed=True,
        )

        async def reject(_url: str):
            from app.url_policy import UnsafeTarget

            raise UnsafeTarget("fixture policy rejection")

        with patch("app.main.validate_target_url", reject):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(target_preflight(request))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, "fixture policy rejection")

    def test_run_converts_policy_failure_to_blocked_report(self):
        request = make_request(CheckName.HTTP_STATUS, CheckName.H1)

        async def reject(_request):
            from app.url_policy import UnsafeTarget

            raise UnsafeTarget("fixture policy rejection")

        with patch("app.execution.run_website_checks", reject):
            report = asyncio.run(run_website(request, LOCAL))

        self.assertEqual(report.overall_status, OverallStatus.BLOCKED)
        self.assertEqual(report.summary.skipped, 2)
        self.assertTrue(all(check.status == CheckStatus.SKIPPED for check in report.checks))
        self.assertEqual(report.checks[0].evidence["reason"], "fixture policy rejection")

    def test_run_converts_unexpected_failure_to_error_report(self):
        request = make_request(CheckName.HTTP_STATUS)

        async def fail(_request):
            raise RuntimeError("fixture runner failure")

        with patch("app.execution.run_website_checks", fail):
            report = asyncio.run(run_website(request, LOCAL))

        self.assertEqual(report.overall_status, OverallStatus.ERROR)
        self.assertEqual(report.summary.errors, 1)
        self.assertEqual(report.checks[0].evidence, {"error_type": "RuntimeError"})

    def test_api_run_converts_policy_failure_to_blocked_report(self):
        request = ApiRunRequest(
            url="https://example.com/",
            authorization_confirmed=True,
            checks=[CheckName.API_STATUS],
        )

        async def reject(_request):
            from app.url_policy import UnsafeTarget

            raise UnsafeTarget("fixture API policy rejection")

        with patch("app.execution.run_api_checks", reject):
            from app.main import run_api

            report = asyncio.run(run_api(request, LOCAL))

        self.assertEqual(report.overall_status, OverallStatus.BLOCKED)
        self.assertEqual(report.checks[0].status, CheckStatus.SKIPPED)


if __name__ == "__main__":
    unittest.main()
