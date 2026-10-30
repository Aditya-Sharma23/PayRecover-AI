"""Recovery actions: approval, recomputed targets, PayPal calls, verification and the log."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import recovery
from app.main import app
from app.schemas import Investigation, Leak, RecoveryRequest, RecoveryTarget

ORDER_ID = "5O190127TN364715T"


def investigation(*leaks: Leak) -> Investigation:
    return Investigation(
        order_id=ORDER_ID, verdict="leaking" if leaks else "healthy", checked_at="2026-10-31T00:00:00Z",
        store_url="http://127.0.0.1:8100", checks=[], leaks=list(leaks), money_at_risk="0.00", currency="USD",
    )


def leak(kind, action, amount, *targets):
    return Leak(kind=kind, title=kind, amount=amount, currency="USD", root_cause="cause", recovery_action=action,
                recovery="do it", targets=list(targets))


NEVER_CAPTURED = leak("never_captured", "capture", "18.00", RecoveryTarget(order_id=ORDER_ID, amount="18.00"))
WRONG_AMOUNT = leak("wrong_amount", "invoice", "17.00", RecoveryTarget(order_id=ORDER_ID, amount="17.00"))
DUPLICATE = leak("duplicate_charge", "refund", "18.00",
                 RecoveryTarget(order_id="9ZZ00000000000002", capture_id="CAP2", amount="18.00"))


class FakePayPal:
    """Records every call; answers like the Sandbox, with switches for failures."""

    def __init__(self, invoicing_allowed=True, capture_amount="18.00", payer_email="buyer@example.com"):
        self.calls = []
        self.invoicing_allowed = invoicing_allowed
        self.capture_amount = capture_amount
        self.payer_email = payer_email
        self.captured = False

    async def call(self, method, path, body=None, request_id=None):
        self.calls.append((method, path, body, request_id))
        if path.endswith("/capture"):
            self.captured = True
            return 201, {"status": "COMPLETED"}
        if path.endswith("/refund"):
            return 201, {"id": "REF1", "status": "COMPLETED"}
        if path == "/v2/payments/refunds/REF1":
            return 200, {"id": "REF1", "status": "COMPLETED"}
        if path == "/v2/invoicing/invoices":
            if not self.invoicing_allowed:
                return 403, {"name": "NOT_AUTHORIZED"}
            return 201, {"href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-AAAA"}
        if path.endswith("/send"):
            return 202, None
        if path.endswith("INV2-AAAA"):
            return 200, {"status": "SENT", "amount": {"value": "17.00", "currency_code": "USD"}}
        raise AssertionError(f"unexpected PayPal call {method} {path}")

    async def fetch(self, order_id):
        unit = {"amount": {"currency_code": "USD", "value": "18.00"}}
        if self.captured:
            unit["payments"] = {"captures": [{"id": "CAP9", "status": "COMPLETED",
                                              "amount": {"currency_code": "USD", "value": self.capture_amount}}]}
        payer = {"email_address": self.payer_email} if self.payer_email else {}
        return {"id": order_id, "status": "COMPLETED" if self.captured else "APPROVED", "purchase_units": [unit],
                "payer": payer}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"TESTPILOT_RECOVERY_LOG_PATH": str(Path(folder.name) / "recoveries.json")})
        env.start()
        self.addCleanup(env.stop)

    def recover(self, found_leak, paypal=None, kind=None):
        paypal = paypal or FakePayPal()

        async def fake_investigate(_request):
            return investigation(found_leak) if found_leak else investigation()

        request = RecoveryRequest(order_id=ORDER_ID, kind=kind or found_leak.kind, approved=True)
        with (
            patch("app.investigate.investigate", fake_investigate),
            patch("app.paypal.paypal_call", paypal.call),
            patch("app.paypal.fetch_order", paypal.fetch),
        ):
            return asyncio.run(recovery.recover(request, "local")), paypal

    def test_nothing_happens_without_approval(self):
        with self.assertRaises(ValidationError):
            RecoveryRequest(order_id=ORDER_ID, kind="never_captured", approved=False)

    def test_a_never_captured_order_is_captured_and_verified(self):
        record, paypal = self.recover(NEVER_CAPTURED)
        self.assertEqual(record.status, "recovered")
        self.assertEqual(paypal.calls[0][1], f"/v2/checkout/orders/{ORDER_ID}/capture")
        self.assertEqual(paypal.calls[0][3], f"payrecover-capture-{ORDER_ID}")
        self.assertTrue(all(check.status == "passed" for check in record.verification))

    def test_a_capture_paypal_does_not_confirm_is_unverified(self):
        record, _ = self.recover(NEVER_CAPTURED, FakePayPal(capture_amount="1.00"))
        self.assertEqual(record.status, "unverified")

    def test_a_duplicate_is_refunded_from_the_other_order(self):
        record, paypal = self.recover(DUPLICATE)
        self.assertEqual(record.status, "refunded")
        method, path, body, request_id = paypal.calls[0]
        self.assertEqual(path, "/v2/payments/captures/CAP2/refund")
        self.assertEqual(body["amount"], {"value": "18.00", "currency_code": "USD"})
        self.assertEqual(request_id, "payrecover-refund-CAP2")

    def test_a_short_payment_is_invoiced_and_the_email_is_not_kept(self):
        record, paypal = self.recover(WRONG_AMOUNT)
        self.assertEqual(record.status, "invoice_sent")
        self.assertEqual(paypal.calls[0][2]["items"][0]["unit_amount"]["value"], "17.00")
        self.assertNotIn("buyer@example.com", record.model_dump_json())
        self.assertNotIn("buyer@example.com", Path(os.environ["TESTPILOT_RECOVERY_LOG_PATH"]).read_text())

    def test_invoicing_switched_off_explains_how_to_turn_it_on(self):
        record, _ = self.recover(WRONG_AMOUNT, FakePayPal(invoicing_allowed=False))
        self.assertEqual(record.status, "failed")
        self.assertIn("Features > Invoicing", record.message)

    def test_an_order_paypal_never_approved_cannot_be_invoiced(self):
        record, paypal = self.recover(WRONG_AMOUNT, FakePayPal(payer_email=None))
        self.assertEqual(record.status, "failed")
        self.assertEqual(paypal.calls, [])

    def test_a_recovery_is_never_repeated(self):
        first, _ = self.recover(NEVER_CAPTURED)
        second, paypal = self.recover(NEVER_CAPTURED)
        self.assertEqual(second.recovery_id, first.recovery_id)
        self.assertEqual(paypal.calls, [])

    def test_a_leak_that_is_gone_cannot_be_recovered(self):
        with self.assertRaises(recovery.RecoveryNotPossible):
            self.recover(None, kind="never_captured")

    def test_totals_add_up_by_outcome(self):
        self.recover(NEVER_CAPTURED)
        self.recover(DUPLICATE)
        listing = recovery.list_recoveries("local")
        self.assertEqual(listing["totals"], {"collected": "18.00", "invoiced": "0.00", "refunded": "18.00"})
        self.assertEqual(recovery.list_recoveries("someone-else")["records"], [])


    def test_clearing_removes_only_the_callers_log(self):
        self.recover(NEVER_CAPTURED)
        records = recovery._load()
        recovery._save(records + [{**records[0], "recovery_id": "other", "owner": "someone-else"}])
        self.assertEqual(recovery.clear_recoveries("local"), 1)
        self.assertEqual(recovery.list_recoveries("local")["records"], [])
        self.assertEqual(len(recovery.list_recoveries("someone-else")["records"]), 1)


class RecoveryEndpointTests(unittest.TestCase):
    def test_unapproved_requests_are_rejected(self):
        response = TestClient(app).post("/api/recoveries", json={"order_id": ORDER_ID, "kind": "never_captured",
                                                                 "approved": False})
        self.assertEqual(response.status_code, 422)

    def test_an_amount_cannot_be_sent(self):
        response = TestClient(app).post("/api/recoveries", json={"order_id": ORDER_ID, "kind": "wrong_amount",
                                                                 "approved": True, "amount": "9999.00"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
