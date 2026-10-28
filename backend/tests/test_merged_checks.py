"""Checks merged in from the single-URL scanner build."""

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import contract_runner, security_checks
from app.api_runner import matches_json_shape
from app.gemma import builtin_explanation, explain_with_fallback, risk_level
from app.schemas import (
    API_CHECK_NAMES,
    FULL_SCAN_CHECKS,
    WEBSITE_CHECK_NAMES,
    ApiRunRequest,
    CheckName,
    CheckStatus,
)
from app.static_a11y import static_accessibility_scan
from app.storage import dashboard_stats, save_report
from app.website_runner import evaluate_security_headers
from isolation import restore_database, use_temporary_database
from test_runner_integration import run_fixture

setUpModule = use_temporary_database
tearDownModule = restore_database
DEMO_SITE = Path(__file__).resolve().parents[2] / "demo-site"


def fake_probe(responses: dict[str, security_checks.ProbeResponse]):
    async def fetch(url: str, method: str = "GET", body_limit: int = 0):
        path = url.split("fixture.test", 1)[1].split("?", 1)[0] or "/"
        if path in responses:
            return responses[path]
        return security_checks.ProbeResponse(404, {"content-type": "text/plain"}, [], b"missing")
    return fetch


class CatalogTests(unittest.TestCase):
    def test_every_check_from_both_builds_is_available(self):
        website = {str(name) for name in WEBSITE_CHECK_NAMES}
        api = {str(name) for name in API_CHECK_NAMES}
        self.assertEqual(len(website), 17)
        self.assertEqual(len(api), 9)
        self.assertTrue({"cookie_flags", "exposed_resources", "active_probes", "keyboard_focus",
                         "response_time", "body_not_empty", "openapi_contract"} <= website)
        self.assertTrue({"api_json_shape", "openapi_contract"} <= api)

    def test_full_scan_needs_no_extra_input_and_keeps_active_probes_opt_in(self):
        self.assertNotIn(CheckName.ACTIVE_PROBES, FULL_SCAN_CHECKS)
        self.assertNotIn(CheckName.TEXT_PRESENT, FULL_SCAN_CHECKS)
        self.assertIn(CheckName.COOKIE_FLAGS, FULL_SCAN_CHECKS)


class SecurityHeaderTests(unittest.TestCase):
    def test_six_controls_with_clickjacking_from_frame_ancestors(self):
        policy = evaluate_security_headers(
            {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'", "X-Content-Type-Options": "nosniff"},
            "https",
        )
        self.assertIn("clickjacking-protection", policy["present"])
        self.assertEqual(set(policy["missing"]), {"strict-transport-security", "referrer-policy", "permissions-policy"})
        self.assertEqual(len(policy["findings"]), 3)

    def test_hsts_does_not_apply_over_http(self):
        policy = evaluate_security_headers({}, "http")
        self.assertEqual(policy["not_applicable"], ["strict-transport-security"])
        self.assertEqual(len(policy["missing"]), 5)


class ProbeTests(unittest.TestCase):
    def test_cookie_flags_report_missing_attributes_without_values(self):
        cookies, findings = security_checks.cookie_flag_results(
            ["sid=SECRETVALUE; Path=/; Secure", "pref=1; Secure; HttpOnly; SameSite=Lax"]
        )
        self.assertEqual(cookies[0]["missing"], ["httponly", "samesite"])
        self.assertEqual(cookies[1]["missing"], [])
        self.assertEqual(findings[0]["severity"], "medium")
        self.assertNotIn("SECRETVALUE", str(findings))

    def test_exposed_env_file_fails_the_check(self):
        responses = {"/.env": security_checks.ProbeResponse(200, {}, [], b"")}
        with patch("app.security_checks.fetch_probe", fake_probe(responses)):
            result = asyncio.run(security_checks.exposure_check("https://fixture.test/"))
        exposed = [item for item in result["probes"] if item.get("exposed")]
        self.assertEqual([item["path"] for item in exposed], ["/.env"])
        self.assertEqual(len(result["findings"]), 1)

    def test_active_probes_flag_open_admin_route_but_not_login_page(self):
        responses = {
            "/": security_checks.ProbeResponse(200, {}, [], b"<p>hello</p>"),
            "/admin": security_checks.ProbeResponse(200, {}, [], b"<h1>Users</h1>"),
            "/account": security_checks.ProbeResponse(200, {}, [], b"Please sign in"),
        }
        with patch("app.security_checks.fetch_probe", fake_probe(responses)):
            result = asyncio.run(security_checks.active_probe_check("https://fixture.test/"))
        self.assertFalse(result["canary"]["reflected"])
        flagged = [route["path"] for route in result["routes"] if route.get("needs_review")]
        self.assertEqual(flagged, ["/admin"])

    def test_probe_checks_run_through_the_website_runner(self):
        page = {"/": DEMO_SITE.joinpath("index.html").read_bytes()}
        responses = {"/": security_checks.ProbeResponse(200, {}, ["sid=1; Path=/"], b"")}
        with patch("app.security_checks.fetch_probe", fake_probe(responses)):
            results = run_fixture(page, checks=[CheckName.COOKIE_FLAGS, CheckName.EXPOSED_RESOURCES, CheckName.OPENAPI_CONTRACT])
        self.assertEqual(results[CheckName.COOKIE_FLAGS].status, CheckStatus.WARNING)
        self.assertEqual(results[CheckName.EXPOSED_RESOURCES].status, CheckStatus.PASSED)
        self.assertEqual(results[CheckName.OPENAPI_CONTRACT].status, CheckStatus.SKIPPED)


class BrowserExtraTests(unittest.TestCase):
    def test_load_time_body_headers_and_keyboard_focus(self):
        page = {"/": DEMO_SITE.joinpath("index.html").read_bytes()}
        results = run_fixture(page, checks=[
            CheckName.RESPONSE_TIME, CheckName.RESPONSE_HEADERS, CheckName.BODY_NOT_EMPTY, CheckName.KEYBOARD_FOCUS,
            CheckName.SECURITY_HEADERS,
        ])
        for name in (CheckName.RESPONSE_TIME, CheckName.RESPONSE_HEADERS, CheckName.BODY_NOT_EMPTY, CheckName.KEYBOARD_FOCUS):
            self.assertEqual(results[name].status, CheckStatus.PASSED, (name, results[name].evidence))
        self.assertEqual(results[CheckName.SECURITY_HEADERS].status, CheckStatus.WARNING)

    def test_keyboard_focus_fails_when_tab_cannot_reach_controls(self):
        page = {"/": b"<html><head><title>T</title></head><body><h1>H</h1>"
                     b"<a href='/x' tabindex='-1'>x</a><button tabindex='-1'>b</button></body></html>"}
        results = run_fixture(page, checks=[CheckName.KEYBOARD_FOCUS])
        self.assertEqual(results[CheckName.KEYBOARD_FOCUS].status, CheckStatus.FAILED)


class StaticAccessibilityTests(unittest.TestCase):
    def test_static_rules_find_broken_demo_page_problems(self):
        scan = static_accessibility_scan(DEMO_SITE.joinpath("broken.html").read_text(encoding="utf-8"))
        self.assertTrue(scan["applicable"])
        self.assertIn("image-alt", {item["id"] for item in scan["violations"]})

    def test_non_html_is_not_applicable(self):
        self.assertFalse(static_accessibility_scan('{"ok": true}')["applicable"])


class JsonShapeTests(unittest.TestCase):
    def test_shape_compares_keys_and_types_not_values(self):
        self.assertTrue(matches_json_shape({"id": 9, "tags": ["b"]}, {"id": 1, "tags": ["a"]}))
        self.assertFalse(matches_json_shape({"id": "9"}, {"id": 1}))
        self.assertFalse(matches_json_shape({"id": 1, "extra": 2}, {"id": 1}))
        self.assertFalse(matches_json_shape({"ok": 1}, {"ok": True}))

    def test_shape_check_requires_a_sample(self):
        with self.assertRaises(ValueError):
            ApiRunRequest(url="https://api.example.com/x", authorization_confirmed=True, checks=["api_json_shape"])


class ContractTests(unittest.TestCase):
    def test_candidates_cover_standard_schema_locations(self):
        candidates = contract_runner.schema_candidates("https://fixture.test/api/v1/items")
        self.assertIn("https://fixture.test/api/v1/openapi.json", candidates)
        self.assertIn("https://fixture.test/v3/api-docs", candidates)

    def test_remote_refs_are_detected(self):
        self.assertTrue(contract_runner.has_remote_refs({"a": {"$ref": "https://evil.test/x.json"}}))
        self.assertFalse(contract_runner.has_remote_refs({"a": {"$ref": "#/components/schemas/X"}}))


def report(checks: list[dict]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {"run_id": str(uuid4()), "target_url": "https://fixture.test/", "started_at": now, "finished_at": now,
            "overall_status": "failed", "summary": {"passed": 0, "failed": 1, "warnings": 0, "errors": 0, "skipped": 0},
            "checks": checks, "warnings": []}


class SummaryTests(unittest.TestCase):
    def test_failed_security_check_is_high_risk(self):
        data = report([{"check_id": "exposed_resources", "status": "failed", "message": "1 of 3", "evidence": {
            "findings": [{"title": "Potentially exposed .env file", "severity": "medium", "detail": "d", "recommendation": "Remove it."}]}}])
        self.assertEqual(risk_level(data), "HIGH")
        explanation = builtin_explanation(data)
        self.assertEqual(explanation.source, "Built-in summary")
        self.assertIn("Potentially exposed .env file", explanation.findings[0])

    def test_explain_falls_back_when_gemma_is_not_configured(self):
        data = report([{"check_id": "h1", "status": "failed", "message": "0 headings", "evidence": {}}])
        with patch.dict("os.environ", {"TESTPILOT_GOOGLE_API_KEY": "", "GEMINI_API_KEY": "", "TESTPILOT_GEMMA_PROVIDER": "google"}):
            explanation = asyncio.run(explain_with_fallback(data))
        self.assertEqual(explanation.source, "Built-in summary")
        self.assertEqual(explanation.risk_level, "REVIEW")

    def test_dashboard_counts_findings_from_stored_runs(self):
        from app.schemas import WebsiteRunReport

        stored = WebsiteRunReport.model_validate(report([
            {"check_id": "cookie_flags", "status": "failed", "message": "m",
             "evidence": {"findings": [{"title": "t", "severity": "low", "detail": "d", "recommendation": "r"}]}},
        ]))
        save_report(stored)
        stats = dashboard_stats()
        self.assertGreaterEqual(stats["total_runs"], 1)
        self.assertGreaterEqual(stats["security_issues"], 1)
        self.assertGreaterEqual(stats["vulnerabilities"], 1)


if __name__ == "__main__":
    unittest.main()
