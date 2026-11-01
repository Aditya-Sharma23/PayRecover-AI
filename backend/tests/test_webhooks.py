"""PayPal webhooks: verified with PayPal first, then checks run and alerts go out; nothing moves money."""

import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import webhooks
from app.main import app
from app.schemas import Investigation

ORDER_ID = "5O190127TN364715T"
HEADERS = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
           "paypal-transmission-id": "t1", "paypal-transmission-sig": "sig", "paypal-transmission-time": "now"}
CAPTURE = {"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
    "id": "CAP1", "custom_id": "payrecover-vulnerable", "amount": {"value": "18.00", "currency_code": "USD"},
    "supplementary_data": {"related_ids": {"order_id": ORDER_ID}}}}


def investigation(claim):
    return Investigation(order_id=ORDER_ID, verdict="healthy", store="vulnerable",
                         checked_at=datetime.now(timezone.utc), store_url="http://127.0.0.1:8200", store_claim=claim,
                         checks=[], leaks=[], money_at_risk="0.00", currency="USD")


class WebhookTests(unittest.TestCase):
    def verify_with(self, verification_status, headers=HEADERS):
        calls = []

        async def fake_call(method, path, body=None, request_id=None):
            calls.append(body)
            return 200, {"verification_status": verification_status}

        with patch.dict(os.environ, {"TESTPILOT_PAYPAL_WEBHOOK_ID": "WH-1"}), patch("app.paypal.paypal_call", fake_call):
            asyncio.run(webhooks.verify(headers, CAPTURE))
        return calls

    def test_paypal_confirms_the_signature_with_the_configured_webhook_id(self):
        body = self.verify_with("SUCCESS")[0]
        self.assertEqual((body["webhook_id"], body["transmission_id"]), ("WH-1", "t1"))
        self.assertEqual(body["webhook_event"], CAPTURE)

    def test_a_failed_or_unsigned_event_is_rejected(self):
        with self.assertRaises(webhooks.WebhookRejected):
            self.verify_with("FAILURE")
        with self.assertRaises(webhooks.WebhookRejected):
            self.verify_with("SUCCESS", headers={})

    def test_without_a_webhook_id_events_are_refused(self):
        with patch.dict(os.environ, {"TESTPILOT_PAYPAL_WEBHOOK_ID": ""}):
            response = TestClient(app).post("/api/paypal/webhook", json=CAPTURE, headers=HEADERS)
        self.assertEqual(response.status_code, 503)

    def capture_alerts(self, claim):
        async def fake_investigate(_request, *_):
            return investigation(claim)

        with (
            patch("app.investigate.investigate", fake_investigate),
            patch("app.webhooks.STORE_SETTLE_SECONDS", 0),
        ):
            return asyncio.run(webhooks.on_capture(CAPTURE["resource"], "local"))

    def test_a_payment_the_store_never_recorded_raises_an_alert(self):
        alerts = self.capture_alerts({"status": "PENDING", "capture_id": None})
        self.assertEqual(alerts[0]["event"], "unrecorded_payment")
        self.assertEqual((alerts[0]["amount"], alerts[0]["store"]), ("18.00", "vulnerable"))

    def test_a_recorded_healthy_payment_raises_nothing(self):
        self.assertEqual(self.capture_alerts({"status": "PAID", "capture_id": "CAP1"}), [])

    def test_payments_from_other_shops_are_ignored(self):
        other = {**CAPTURE["resource"], "custom_id": "someone-else"}
        self.assertEqual(asyncio.run(webhooks.on_capture(other, "local")), [])


if __name__ == "__main__":
    unittest.main()
