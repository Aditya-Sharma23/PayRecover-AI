import unittest
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from app.schemas import (
    CheckName,
    CheckResult,
    CheckStatus,
    OverallStatus,
    SummaryCounts,
    WebsiteRunReport,
    WebsiteRunRequest,
)


class SchemaTests(unittest.TestCase):
    def test_request_requires_authorization_and_unique_checks(self):
        with self.assertRaises(ValidationError):
            WebsiteRunRequest(
                url="https://example.com/",
                authorization_confirmed=False,
                checks=[CheckName.H1],
            )
        with self.assertRaises(ValidationError):
            WebsiteRunRequest(
                url="https://example.com/",
                authorization_confirmed=True,
                checks=[CheckName.H1, CheckName.H1],
            )

    def test_text_check_requires_an_explicit_expectation(self):
        with self.assertRaises(ValidationError):
            WebsiteRunRequest(
                url="https://example.com/",
                authorization_confirmed=True,
                checks=[CheckName.TEXT_PRESENT],
            )
        request = WebsiteRunRequest(
            url="https://example.com/",
            authorization_confirmed=True,
            checks=[CheckName.TEXT_PRESENT],
            expected_text="Welcome",
        )
        self.assertFalse(request.capture_screenshot_on_failure)

    def test_report_rejects_inconsistent_summary(self):
        now = datetime.now(timezone.utc)
        check = CheckResult(
            check_id=CheckName.H1,
            status=CheckStatus.PASSED,
            message="Observed one H1.",
        )
        with self.assertRaises(ValidationError):
            WebsiteRunReport(
                run_id=uuid4(),
                target_url="https://example.com/",
                started_at=now,
                finished_at=now,
                overall_status=OverallStatus.PASSED,
                summary=SummaryCounts(passed=0),
                checks=[check],
            )

    def test_report_accepts_evidence_derived_status(self):
        now = datetime.now(timezone.utc)
        report = WebsiteRunReport(
            run_id=uuid4(),
            target_url="https://example.com/",
            started_at=now,
            finished_at=now,
            overall_status=OverallStatus.WARNING,
            summary=SummaryCounts(passed=1, warnings=1),
            checks=[
                CheckResult(
                    check_id=CheckName.HTTP_STATUS,
                    status=CheckStatus.PASSED,
                    message="Observed HTTP status 200.",
                ),
                CheckResult(
                    check_id=CheckName.SAME_ORIGIN_LINKS,
                    status=CheckStatus.WARNING,
                    message="Link inventory captured.",
                ),
            ],
        )
        self.assertEqual(report.overall_status, OverallStatus.WARNING)

    def test_user_flow_steps_must_be_complete_and_decide_an_outcome(self):
        base = {"url": "https://example.com/", "authorization_confirmed": True, "checks": [CheckName.USER_FLOW]}
        ok = WebsiteRunRequest(**base, flow_steps=[
            {"action": "fill", "selector": "#q", "value": "shoes"},
            {"action": "press", "value": "Enter"},
            {"action": "expect_url", "value": "/search"},
        ])
        self.assertEqual(len(ok.flow_steps), 3)
        for steps in (
            None,
            [{"action": "click", "selector": "#a"}],
            [{"action": "click"}, {"action": "expect_text", "value": "x"}],
            [{"action": "press", "value": "rm -rf"}, {"action": "expect_text", "value": "x"}],
            [{"action": "navigate", "value": "https://evil.test"}],
            [{"action": "expect_text", "value": "x"}] * 11,
        ):
            with self.assertRaises(ValidationError):
                WebsiteRunRequest(**base, flow_steps=steps)
        with self.assertRaises(ValidationError):
            WebsiteRunRequest(url="https://example.com/", authorization_confirmed=True, checks=[CheckName.H1],
                              flow_steps=[{"action": "expect_text", "value": "x"}])


if __name__ == "__main__":
    unittest.main()
