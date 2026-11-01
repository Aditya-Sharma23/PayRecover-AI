"""PayPal Sandbox verifier: order evaluation, endpoint behaviour and request validation."""

import isolation
import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import paypal
from app.main import app
from app.schemas import CheckStatus, PaymentVerificationRequest

ORDER_ID = "5O190127TN364715T"


def sandbox_order(status="COMPLETED", amount="48.50", capture_status="COMPLETED", capture_amount=None):
    unit = {"amount": {"currency_code": "USD", "value": amount}, "payee": {"email_address": "shop@example.com"}}
    if capture_status:
        unit["payments"] = {"captures": [{
            "id": "3C679366HH908993F",
            "status": capture_status,
            "amount": {"currency_code": "USD", "value": capture_amount or amount},
        }]}
    return {
        "id": ORDER_ID,
        "status": status,
        "intent": "CAPTURE",
        "purchase_units": [unit],
        "payer": {"email_address": "buyer@example.com", "name": {"given_name": "Ada"}},
    }


def checks_by_name(verification):
    return {check.name: check.status for check in verification.checks}


def verify(order, **expected):
    async def fake_fetch(_order_id):
        return order

    with patch("app.paypal.fetch_order", fake_fetch):
        return asyncio.run(paypal.verify_order(PaymentVerificationRequest(order_id=ORDER_ID, **expected)))


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class PayPalVerifierTests(unittest.TestCase):
    def test_completed_capture_with_the_right_amount_is_verified(self):
        result = verify(sandbox_order(), expected_amount="48.50")
        self.assertTrue(result.verified)
        self.assertEqual(
            checks_by_name(result),
            {
                "order_found": CheckStatus.PASSED,
                "order_status": CheckStatus.PASSED,
                "capture_completed": CheckStatus.PASSED,
                "amount_matches": CheckStatus.PASSED,
            },
        )
        self.assertEqual(result.order["capture_id"], "3C679366HH908993F")

    def test_approved_but_never_captured_order_fails(self):
        result = verify(sandbox_order(status="APPROVED", capture_status=None))
        self.assertFalse(result.verified)
        self.assertEqual(checks_by_name(result)["order_status"], CheckStatus.FAILED)
        self.assertEqual(checks_by_name(result)["capture_completed"], CheckStatus.FAILED)

    def test_tampered_amount_fails_even_when_payment_completed(self):
        result = verify(sandbox_order(amount="12.52"), expected_amount="48.50")
        self.assertFalse(result.verified)
        self.assertEqual(checks_by_name(result)["amount_matches"], CheckStatus.FAILED)

    def test_capture_amount_must_also_match(self):
        result = verify(sandbox_order(capture_amount="1.00"), expected_amount="48.50")
        self.assertEqual(checks_by_name(result)["amount_matches"], CheckStatus.FAILED)

    def test_cancelled_flow_can_expect_an_uncaptured_order(self):
        result = verify(sandbox_order(status="CREATED", capture_status=None), expected_status="CREATED", require_capture=False)
        self.assertTrue(result.verified)

    def test_missing_order_is_a_failed_check_not_an_error(self):
        async def not_found(order_id):
            raise paypal.PayPalOrderNotFound(f"PayPal Sandbox has no order {order_id}.")

        with patch("app.paypal.fetch_order", not_found):
            result = asyncio.run(paypal.verify_order(PaymentVerificationRequest(order_id=ORDER_ID)))
        self.assertFalse(result.verified)
        self.assertEqual(checks_by_name(result), {"order_found": CheckStatus.FAILED})

    def test_evidence_never_contains_payer_or_payee_details(self):
        dumped = verify(sandbox_order()).model_dump_json()
        self.assertNotIn("buyer@example.com", dumped)
        self.assertNotIn("shop@example.com", dumped)
        self.assertNotIn("Ada", dumped)

    def test_order_ids_and_amounts_are_validated(self):
        for bad in ("../../v1/oauth2", "abc", "5O190127TN364715T?x=1"):
            with self.assertRaises(ValidationError):
                PaymentVerificationRequest(order_id=bad)
        with self.assertRaises(ValidationError):
            PaymentVerificationRequest(order_id=ORDER_ID, expected_amount="1e3")

    def test_the_api_base_is_always_the_sandbox(self):
        self.assertEqual(paypal.SANDBOX_API, "https://api-m.sandbox.paypal.com")
        with patch.dict(os.environ, {"TESTPILOT_PAYPAL_CLIENT_ID": "id", "TESTPILOT_PAYPAL_CLIENT_SECRET": "s"}):
            self.assertEqual(paypal.configuration_status()["environment"], "sandbox")


class PayPalEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_status_never_returns_credentials(self):
        with patch.dict(os.environ, {"TESTPILOT_PAYPAL_CLIENT_ID": "id-123", "TESTPILOT_PAYPAL_CLIENT_SECRET": "secret-456"}):
            body = self.client.get("/api/paypal/status").json()
        self.assertTrue(body["configured"])
        self.assertNotIn("id-123", str(body))
        self.assertNotIn("secret-456", str(body))

    def test_verify_without_credentials_is_503(self):
        with patch.dict(os.environ, {"TESTPILOT_PAYPAL_CLIENT_ID": "", "TESTPILOT_PAYPAL_CLIENT_SECRET": ""}):
            response = self.client.post("/api/paypal/verify", json={"order_id": ORDER_ID})
        self.assertEqual(response.status_code, 503)

    def test_verify_returns_the_evaluated_order(self):
        async def fake_fetch(_order_id):
            return sandbox_order(status="APPROVED", capture_status=None)

        with patch("app.paypal.fetch_order", fake_fetch):
            response = self.client.post("/api/paypal/verify", json={"order_id": ORDER_ID, "expected_amount": "48.50"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["verified"])
        self.assertEqual(response.json()["source"], "paypal_sandbox_orders_api")

    def test_provider_failure_is_502(self):
        async def down(_order_id):
            raise paypal.PayPalProviderError("PayPal Sandbox could not be reached.")

        with patch("app.paypal.fetch_order", down):
            response = self.client.post("/api/paypal/verify", json={"order_id": ORDER_ID})
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
