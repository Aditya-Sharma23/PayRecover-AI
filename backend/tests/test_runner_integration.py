import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from app.schemas import CheckName, CheckStatus, WebsiteRunRequest
from app.storage import get_artifact
from app.website_runner import run_website_checks
from isolation import restore_database, use_temporary_database

setUpModule = use_temporary_database
tearDownModule = restore_database
DEMO_SITE = Path(__file__).resolve().parents[2] / "demo-site"


async def fake_validate_target_url(_url: str) -> tuple[str, ...]:
    return ("127.0.0.1",)


def run_fixture(pages: dict[str, bytes], requested: list[str] | None = None, **options):
    """Serve fixture pages by path through the runner's pinned-fetch seam."""

    async def fake_fetch_pinned(url: str, _method: str, _timeout_seconds: int):
        path = url.split("fixture.test", 1)[1] or "/"
        if requested is not None:
            requested.append(url)
        if path in pages:
            return 200, {"content-type": "text/html"}, pages[path]
        return 404, {"content-type": "text/plain"}, b"missing"

    request = WebsiteRunRequest(
        url="https://fixture.test/",
        authorization_confirmed=True,
        timeout_seconds=10,
        **options,
    )
    with (
        patch("app.website_runner.validate_target_url", fake_validate_target_url),
        patch("app.website_runner._fetch_pinned", fake_fetch_pinned),
    ):
        return {result.check_id: result for result in asyncio.run(run_website_checks(request))}


class RunnerIntegrationTests(unittest.TestCase):
    def test_controlled_fixture_produces_observed_results(self):
        async def fake_validate_target_url(_url: str) -> tuple[str, ...]:
            return ("127.0.0.1",)

        async def fake_fetch_pinned(
            _url: str, _method: str, _timeout_seconds: int
        ) -> tuple[int, dict[str, str], bytes]:
            body = b"""
                <!doctype html>
                <html>
                  <head><title>Controlled fixture</title></head>
                  <body>
                    <h1>Fixture heading</h1>
                    <a href="https://fixture.test/inside">Inside</a>
                    <a href="https://external.test/outside">Outside</a>
                  </body>
                </html>
            """
            return 200, {"content-type": "text/html"}, body

        request = WebsiteRunRequest(
            url="https://fixture.test/",
            authorization_confirmed=True,
            checks=[
                CheckName.HTTP_STATUS,
                CheckName.PAGE_TITLE,
                CheckName.H1,
                CheckName.CONSOLE_ERRORS,
                CheckName.SAME_ORIGIN_LINKS,
            ],
            max_links=10,
            timeout_seconds=10,
        )

        with (
            patch("app.website_runner.validate_target_url", fake_validate_target_url),
            patch("app.website_runner._fetch_pinned", fake_fetch_pinned),
        ):
            results = asyncio.run(run_website_checks(request))

        statuses = {result.check_id: result.status for result in results}
        self.assertEqual(statuses[CheckName.HTTP_STATUS], CheckStatus.PASSED)
        self.assertEqual(statuses[CheckName.PAGE_TITLE], CheckStatus.PASSED)
        self.assertEqual(statuses[CheckName.H1], CheckStatus.PASSED)
        self.assertEqual(statuses[CheckName.CONSOLE_ERRORS], CheckStatus.PASSED)
        self.assertEqual(statuses[CheckName.SAME_ORIGIN_LINKS], CheckStatus.WARNING)

        link_result = next(
            result for result in results if result.check_id == CheckName.SAME_ORIGIN_LINKS
        )
        self.assertEqual(link_result.evidence["same_origin_count"], 1)
        self.assertEqual(link_result.evidence["external_count"], 1)

    def test_missing_h1_and_console_error_are_real_failures(self):
        results = run_fixture(
            {"/": b"<html><head><title>T</title></head><body><p>No heading</p>"
                  b"<script>console.error('fixture boom')</script></body></html>"},
            checks=[CheckName.H1, CheckName.CONSOLE_ERRORS],
        )
        self.assertEqual(results[CheckName.H1].status, CheckStatus.FAILED)
        self.assertEqual(results[CheckName.H1].evidence["count"], 0)
        self.assertEqual(results[CheckName.CONSOLE_ERRORS].status, CheckStatus.FAILED)
        self.assertIn("fixture boom", results[CheckName.CONSOLE_ERRORS].evidence["errors"][0])

    def test_expected_text_is_matched_against_visible_text(self):
        page = {"/": b"<html><body><h1>Sign in</h1><p>Email is required</p></body></html>"}
        found = run_fixture(page, checks=[CheckName.TEXT_PRESENT], expected_text="Email is required")
        missing = run_fixture(page, checks=[CheckName.TEXT_PRESENT], expected_text="Password is required")
        self.assertEqual(found[CheckName.TEXT_PRESENT].status, CheckStatus.PASSED)
        self.assertEqual(missing[CheckName.TEXT_PRESENT].status, CheckStatus.FAILED)
        self.assertFalse(missing[CheckName.TEXT_PRESENT].evidence["found"])

    def test_demo_site_validation_message_appears_after_one_click(self):
        page = {"/": DEMO_SITE.joinpath("index.html").read_bytes()}
        clicked = run_fixture(
            page,
            checks=[CheckName.H1, CheckName.TEXT_PRESENT],
            expected_text="Email is required",
            click_selector="#sign-in-button",
        )
        not_clicked = run_fixture(page, checks=[CheckName.TEXT_PRESENT], expected_text="Email is required")
        bad_selector = run_fixture(
            page, checks=[CheckName.TEXT_PRESENT], expected_text="Email is required", click_selector="#missing",
        )
        self.assertEqual(clicked[CheckName.H1].status, CheckStatus.PASSED)
        self.assertEqual(clicked[CheckName.TEXT_PRESENT].status, CheckStatus.PASSED)
        self.assertEqual(not_clicked[CheckName.TEXT_PRESENT].status, CheckStatus.FAILED)
        self.assertEqual(bad_selector[CheckName.TEXT_PRESENT].status, CheckStatus.ERROR)

    def test_demo_broken_page_fails_with_evidence(self):
        results = run_fixture(
            {"/": DEMO_SITE.joinpath("broken.html").read_bytes()},
            checks=[CheckName.PAGE_TITLE, CheckName.H1, CheckName.CONSOLE_ERRORS],
        )
        self.assertEqual(results[CheckName.PAGE_TITLE].status, CheckStatus.PASSED)
        self.assertEqual(results[CheckName.H1].status, CheckStatus.FAILED)
        self.assertEqual(results[CheckName.CONSOLE_ERRORS].status, CheckStatus.FAILED)

    def test_failure_screenshot_is_opt_in_and_stored_as_artifact(self):
        page = {"/": b"<html><head><title>T</title></head><body><p>No heading</p></body></html>"}
        with_shot = run_fixture(page, checks=[CheckName.H1, CheckName.PAGE_TITLE], capture_screenshot_on_failure=True)
        without_shot = run_fixture(page, checks=[CheckName.H1])
        artifact_id = with_shot[CheckName.H1].evidence["screenshot_artifact_id"]
        self.assertTrue(get_artifact(UUID(artifact_id)).startswith(b"\xff\xd8"))
        self.assertNotIn("screenshot_artifact_id", with_shot[CheckName.PAGE_TITLE].evidence)
        self.assertNotIn("screenshot_artifact_id", without_shot[CheckName.H1].evidence)

    def test_cross_origin_subresources_are_never_fetched(self):
        requested: list[str] = []
        results = run_fixture(
            {"/": b"<html><head><title>T</title><script src='https://evil.test/x.js'></script>"
                  b"</head><body><h1>H</h1><img src='/local.png'></body></html>"},
            requested,
            checks=[CheckName.HTTP_STATUS],
        )
        self.assertEqual(results[CheckName.HTTP_STATUS].status, CheckStatus.PASSED)
        self.assertTrue(requested)
        self.assertTrue(all("evil.test" not in url for url in requested))

    def test_accessibility_passes_on_demo_sign_in_and_fails_on_broken_page(self):
        good = run_fixture({"/": DEMO_SITE.joinpath("index.html").read_bytes()}, checks=[CheckName.ACCESSIBILITY])
        bad = run_fixture({"/": DEMO_SITE.joinpath("broken.html").read_bytes()}, checks=[CheckName.ACCESSIBILITY])
        self.assertEqual(good[CheckName.ACCESSIBILITY].status, CheckStatus.PASSED, good[CheckName.ACCESSIBILITY].evidence)
        result = bad[CheckName.ACCESSIBILITY]
        self.assertEqual(result.status, CheckStatus.FAILED)
        rules = {violation["id"] for violation in result.evidence["violations"]}
        self.assertTrue({"image-alt", "button-name"} <= rules, rules)

    def test_user_flow_passes_fails_and_errors_on_real_page_behaviour(self):
        page = {"/": DEMO_SITE.joinpath("index.html").read_bytes()}
        signed_in = [
            {"action": "fill", "selector": "#email", "value": "ada@example.com"},
            {"action": "fill", "selector": "#password", "value": "hunter2-secret"},
            {"action": "click", "selector": "#sign-in-button"},
            {"action": "expect_text", "value": "credentials are never sent"},
        ]
        passed = run_fixture(page, checks=[CheckName.USER_FLOW], flow_steps=signed_in)[CheckName.USER_FLOW]
        self.assertEqual(passed.status, CheckStatus.PASSED, passed.evidence)
        self.assertNotIn("hunter2-secret", passed.model_dump_json())
        self.assertEqual(passed.evidence["steps"][1]["value_length"], 14)

        missing_password = signed_in[:1] + signed_in[2:]
        failed = run_fixture(page, checks=[CheckName.USER_FLOW], flow_steps=missing_password,
                             capture_screenshot_on_failure=True)[CheckName.USER_FLOW]
        self.assertEqual(failed.status, CheckStatus.FAILED)
        self.assertEqual(failed.evidence["steps"][-1]["status"], "failed")
        self.assertTrue(get_artifact(UUID(failed.evidence["screenshot_artifact_id"])).startswith(b"\xff\xd8"))

        broken = run_fixture(page, checks=[CheckName.USER_FLOW], flow_steps=[
            {"action": "click", "selector": "#does-not-exist"},
            {"action": "expect_text", "value": "anything"},
        ])[CheckName.USER_FLOW]
        self.assertEqual(broken.status, CheckStatus.ERROR)
        self.assertEqual([step["status"] for step in broken.evidence["steps"]], ["error", "not_run"])


if __name__ == "__main__":
    unittest.main()
