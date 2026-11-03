"""Held payments (intent AUTHORIZE): the expiring-hold leak, its recovery, and the "waiting for PayPal" verdict."""

import isolation
import asyncio
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from app import investigate, recovery
from app.schemas import Investigation, RecoveryRequest

ORDER_ID = "5O190127TN364715T"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
CATALOG = {"tee": Decimal("18.00")}


def claim():
    return {"order_id": ORDER_ID, "status": "PAID", "total": "18.00", "currency": "USD",
            "items": [{"product_id": "tee", "name": "PayRecover T-shirt", "quantity": 1, "unit_price": "18.00"}]}


def held_order(status="CREATED", expires_in=timedelta(days=2), captures=None):
    hold = {"id": "AUTH1", "status": status, "amount": {"currency_code": "USD", "value": "18.00"},
            "create_time": "2026-10-07T12:00:00Z",
            "expiration_time": (NOW + expires_in).strftime("%Y-%m-%dT%H:%M:%SZ")}
    payments = {"authorizations": [hold]}
    if captures:
        payments["captures"] = captures
    return {"id": ORDER_ID, "status": "COMPLETED", "intent": "AUTHORIZE",
            "payer": {"email_address": "buyer@example.com"},
            "purchase_units": [{"amount": {"currency_code": "USD", "value": "18.00"}, "payments": payments}]}


def judge(order):
    return investigate.judge(claim(), CATALOG, order, siblings=[], now=NOW)


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class HoldLeakTests(unittest.TestCase):
    def test_a_hold_nobody_captured_is_an_urgent_leak_with_its_deadline(self):
        judged = judge(held_order())
        (leak,) = judged["leaks"]
        self.assertEqual((leak.kind, leak.recovery_action, leak.urgency), ("hold_expiring", "capture_hold", "now"))
        self.assertEqual(leak.targets[0].authorization_id, "AUTH1")
        self.assertEqual(leak.expires_at, NOW + timedelta(days=2))
        self.assertIn("expires in 2 days", leak.title)
        hold_check = next(c for c in judged["checks"] if c.name == "payment_hold")
        self.assertEqual(hold_check.status, "failed")

    def test_a_hold_with_weeks_left_is_soon_not_now(self):
        (leak,) = judge(held_order(expires_in=timedelta(days=20)))["leaks"]
        self.assertEqual(leak.urgency, "soon")

    def test_an_expired_hold_is_invoiced_because_it_can_no_longer_be_captured(self):
        (leak,) = judge(held_order(expires_in=timedelta(hours=-1)))["leaks"]
        self.assertEqual((leak.kind, leak.recovery_action), ("hold_expired", "invoice"))
        self.assertEqual(leak.targets[0].order_id, ORDER_ID)

    def test_a_captured_hold_is_healthy(self):
        captured = [{"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "18.00"}}]
        judged = judge(held_order(status="CAPTURED", captures=captured))
        self.assertEqual((judged["leaks"], judged["verdict"]), ([], "healthy"))

    def test_the_paypal_summary_shows_the_hold(self):
        summary = judge(held_order())["summary"]
        self.assertEqual((summary["authorization_id"], summary["authorization_status"]), ("AUTH1", "CREATED"))


class WaitingTests(unittest.TestCase):
    def test_a_payment_paypal_still_holds_is_waiting_not_healthy(self):
        order = {"id": ORDER_ID, "status": "COMPLETED", "intent": "CAPTURE", "purchase_units": [{
            "amount": {"currency_code": "USD", "value": "18.00"},
            "payments": {"captures": [{"id": "CAP1", "status": "PENDING",
                                       "amount": {"currency_code": "USD", "value": "18.00"}}]}}]}
        judged = judge(order)
        self.assertEqual((judged["verdict"], judged["leaks"]), ("waiting", []))
        self.assertIn("PayPal is holding this payment", judged["waiting_reason"])


class CaptureHoldRecoveryTests(unittest.TestCase):
    def test_approving_captures_the_hold_and_confirms_it_from_paypal(self):
        found = Investigation(order_id=ORDER_ID, verdict="leaking", checked_at=NOW, store_url="http://store",
                              checks=[], leaks=judge(held_order())["leaks"], money_at_risk="18.00", currency="USD")
        calls = []

        async def capture(authorization_id, request_id):
            calls.append((authorization_id, request_id))
            return 201, {"id": "CAP9", "status": "COMPLETED"}

        async def get_hold(_authorization_id):
            return 200, {"id": "AUTH1", "status": "CAPTURED"}

        async def fetch(_order_id):
            captured = [{"id": "CAP9", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "18.00"}}]
            return held_order(status="CAPTURED", captures=captured)

        async def fake_investigate(_request):
            return found

        with (
            patch.object(recovery.investigate, "investigate", fake_investigate),
            patch.object(recovery.paypal, "capture_authorization", capture),
            patch.object(recovery.paypal, "get_authorization", get_hold),
            patch.object(recovery.paypal, "fetch_order", fetch),
            patch.object(recovery, "_log_path", return_value=Path(tempfile.mkdtemp()) / "recoveries.json"),
        ):
            record = asyncio.run(recovery.recover(
                RecoveryRequest(order_id=ORDER_ID, kind="hold_expiring", approved=True), "local"))
        self.assertEqual(calls, [("AUTH1", "payrecover-hold-AUTH1")])
        self.assertEqual((record.status, record.action, record.paypal["capture_id"]), ("recovered", "capture_hold", "CAP9"))
        self.assertTrue(all(check.status == "passed" for check in record.verification))


if __name__ == "__main__":
    unittest.main()
