"""The scan's security check and its three results: red (money leaking), orange (vulnerable), green (all clear)."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import notify, store_security
from app.main import app
from app.schemas import (
    CheckStatus,
    Investigation,
    PaymentCheck,
    ScanResult,
    SecurityFinding,
    StoreSecurity,
    TriagePlan,
)

XSS = store_security.xss_finding("XSS found: 3 of 15 payloads came back unescaped in HTML.")
HEADERS = store_security.headers_finding(["Content-Security-Policy"])


def scan_result(leaking: int) -> ScanResult:
    return ScanResult(store="vulnerable", scanned_at=datetime.now(timezone.utc), orders_listed=3, orders_checked=3,
                      leaking_orders=leaking, money_at_risk="18.00" if leaking else "0.00", currency="USD", items=[],
                      plan=TriagePlan(summary="Capture first.", steps=[], source="rules", notes=[]))


class SecurityCheckTests(unittest.TestCase):
    def test_missing_headers_are_named_and_csp_frame_ancestors_counts_as_clickjacking_protection(self):
        self.assertEqual(store_security.missing_headers({}),
                         ["Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options"])
        safe = {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff"}
        self.assertEqual(store_security.missing_headers(safe), [])

    def test_the_store_check_reports_each_hole(self):
        async def catalog():
            return {"tee": "18.00"}

        async def probe(_catalog):
            return [PaymentCheck(name="price_tampering_probe", status=CheckStatus.FAILED, message="Charged 0.01.",
                                 evidence={})]

        async def xss():
            return {"status": "failed", "summary": "XSS found."}

        async def headers():
            return {"Content-Type": "text/html"}

        with (
            patch("app.store_security._catalog", catalog), patch("app.investigate.probe_store", probe),
            patch("app.investigate.scan_xss", xss), patch("app.store_security._page_headers", headers),
        ):
            security = asyncio.run(store_security.check("vulnerable"))
        self.assertTrue(security.checked)
        self.assertEqual([f.name for f in security.findings], ["price_tampering", "xss", "security_headers"])

    def test_an_unreachable_store_is_reported_not_raised(self):
        with patch.dict(os.environ, {"TESTPILOT_STORE_URL": "http://127.0.0.1:9"}):
            security = asyncio.run(store_security.check("main"))
        self.assertFalse(security.checked)
        self.assertIn("could not be reached", security.note)

    def test_investigation_probes_become_findings(self):
        result = Investigation(
            order_id="5O190127TN364715T", verdict="healthy", checked_at=datetime.now(timezone.utc),
            store_url="http://127.0.0.1:8200", checks=[PaymentCheck(
                name="price_tampering_probe", status=CheckStatus.FAILED, message="Charged 0.01.", evidence={})],
            xss_scan={"status": "failed", "summary": "XSS found."}, leaks=[], money_at_risk="0.00", currency="USD",
        )
        self.assertEqual([f.name for f in store_security.from_investigation(result).findings], ["price_tampering", "xss"])

    def test_the_verdict_puts_leaks_first_then_holes(self):
        holes = StoreSecurity(checked=True, findings=[XSS])
        self.assertEqual(store_security.verdict(True, holes), "leaking")
        self.assertEqual(store_security.verdict(False, holes), "vulnerable")
        self.assertEqual(store_security.verdict(False, StoreSecurity(checked=True)), "clear")
        self.assertEqual(store_security.verdict(False, None), "clear")


class AlertMessageTests(unittest.TestCase):
    def test_red_alert_names_the_money_and_the_holes_behind_it(self):
        result = scan_result(1)
        result.security = StoreSecurity(checked=True, findings=[XSS])
        event = notify.scan_event(result)
        self.assertEqual((event["event"], event["severity"]), ("money_leaking", "red"))
        self.assertTrue(event["message"].startswith("MONEY LEAKING"))
        self.assertIn("Holes in the store that let this happen", event["message"])

    def test_orange_alert_says_no_money_leaked_and_how_to_fix_each_hole(self):
        event = notify.vulnerable_event("vulnerable", StoreSecurity(checked=True, findings=[XSS, HEADERS]),
                                        "PayRecover checked 3 paid order(s).")
        self.assertEqual((event["event"], event["severity"]), ("store_vulnerable", "orange"))
        self.assertIn("no money has leaked yet", event["message"])
        self.assertIn(f"Fix: {store_security.XSS_FIX}", event["message"])


class ScanAlertRoutingTests(unittest.TestCase):
    """A scan sends a red alert, an orange alert, or nothing at all."""

    def scan_alerts(self, leaking: int, findings: list[SecurityFinding]):
        sent = []

        async def fake_scan(_request):
            return scan_result(leaking)

        async def fake_check(_store):
            return StoreSecurity(checked=True, findings=findings)

        async def fake_deliver(event, owner):
            sent.append(event)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with (
            patch("app.scan.scan_store", fake_scan), patch("app.store_security.check", fake_check),
            patch("app.alerts.deliver", fake_deliver),
            patch.dict(os.environ, {"TESTPILOT_ALERTS_DIR": folder.name}),
        ):
            body = TestClient(app).post("/api/stores/scan", json={"store": "vulnerable", "check_paypal_side": False}).json()
        return body["verdict"], [event["event"] for event in sent]

    def test_money_leaking_sends_red(self):
        self.assertEqual(self.scan_alerts(1, [XSS]), ("leaking", ["money_leaking"]))

    def test_no_leak_but_holes_sends_orange(self):
        self.assertEqual(self.scan_alerts(0, [XSS]), ("vulnerable", ["store_vulnerable"]))

    def test_all_clear_sends_nothing(self):
        self.assertEqual(self.scan_alerts(0, []), ("clear", []))


if __name__ == "__main__":
    unittest.main()
