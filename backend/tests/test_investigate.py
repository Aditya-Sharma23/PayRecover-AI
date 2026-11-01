"""PayRecover order investigation: leak detection from the store's claim and PayPal's record."""

import isolation
import asyncio
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import investigate, paypal
from app.main import app
from app.schemas import FULL_SCAN_CHECKS, CheckStatus, InvestigationRequest, PaymentCheck
from app.url_policy import UnsafeTarget, validate_target_url

ORDER_ID = "5O190127TN364715T"
CATALOG = {"tee": Decimal("18.00"), "mug": Decimal("12.50")}


def claim(status="PAID", total="18.00", unit_price="18.00"):
    return {
        "order_id": ORDER_ID, "status": status, "total": total, "currency": "USD",
        "items": [{"product_id": "tee", "name": "PayRecover T-shirt", "quantity": 1, "unit_price": unit_price}],
    }


def paypal_order(status="COMPLETED", amount="18.00", captures=("18.00",)):
    unit = {"amount": {"currency_code": "USD", "value": amount}}
    if captures:
        unit["payments"] = {"captures": [
            {"id": f"CAP{index}", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": value}}
            for index, value in enumerate(captures)
        ]}
    return {"id": ORDER_ID, "status": status, "intent": "CAPTURE", "purchase_units": [unit]}


def run(store_claim, order, probe=None, catalog=CATALOG, run_website=None, website_checks=False, xss=None,
        siblings=None, store="main"):
    async def fake_store(_order_id):
        return store_claim, catalog, None if store_claim else "The store has no record of this order."

    async def fake_fetch(_order_id):
        if order is None:
            raise paypal.PayPalOrderNotFound(f"PayPal Sandbox has no order {ORDER_ID}.")
        return order

    async def fake_probe(_catalog):
        return probe or []

    async def fake_xss():
        return xss

    async def fake_siblings(_claim):
        if siblings is None:
            return [], "The store did not record a cart for this order."
        return siblings, None

    with (
        patch("app.investigate.read_store", fake_store),
        patch("app.paypal.fetch_order", fake_fetch),
        patch("app.investigate.probe_store", fake_probe),
        patch("app.investigate.scan_xss", fake_xss),
        patch("app.investigate.sibling_payments", fake_siblings),
    ):
        request = InvestigationRequest(
            order_id=ORDER_ID, explain=False, website_checks=website_checks, xss_probe=xss is not None, store=store
        )
        return asyncio.run(investigate.investigate(request, run_website))


def kinds(result):
    return [leak.kind for leak in result.leaks]


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class InvestigationTests(unittest.TestCase):
    def test_paid_and_captured_order_is_healthy(self):
        result = run(claim(), paypal_order())
        self.assertEqual(result.verdict, "healthy")
        self.assertEqual(result.leaks, [])
        self.assertEqual(result.money_at_risk, "0.00")

    def test_paid_but_only_approved_is_never_captured(self):
        result = run(claim(), paypal_order(status="APPROVED", captures=()))
        self.assertEqual(result.verdict, "leaking")
        self.assertEqual(kinds(result), ["never_captured"])
        self.assertEqual(result.leaks[0].recovery_action, "capture")
        self.assertEqual(result.money_at_risk, "18.00")

    def test_a_never_captured_payment_is_urgent_and_timed_from_paypal(self):
        order = {**paypal_order(status="APPROVED", captures=()), "update_time": "2026-10-31T09:15:42Z"}
        leak = run(claim(), order).leaks[0]
        self.assertEqual(leak.urgency, "now")
        self.assertEqual(leak.waiting_since.isoformat(), "2026-10-31T09:15:42+00:00")

    def test_a_refunded_order_is_not_a_never_captured_leak(self):
        order = paypal_order()
        order["purchase_units"][0]["payments"]["captures"][0]["status"] = "REFUNDED"
        result = run(claim(), order)
        self.assertEqual(result.leaks, [])
        self.assertEqual(result.money_at_risk, "0.00")

    def test_a_partly_refunded_capture_still_counts_as_collected(self):
        order = paypal_order()
        order["purchase_units"][0]["payments"]["captures"][0]["status"] = "PARTIALLY_REFUNDED"
        self.assertEqual(run(claim(), order).leaks, [])
        short = paypal_order(amount="10.00", captures=("10.00",))
        short["purchase_units"][0]["payments"]["captures"][0]["status"] = "PARTIALLY_REFUNDED"
        self.assertEqual(kinds(run(claim(), short)), ["wrong_amount"])

    def test_a_fake_paid_order_the_buyer_never_approved_cannot_be_invoiced(self):
        leak = run(claim(), paypal_order(status="CREATED", captures=())).leaks[0]
        self.assertEqual(leak.kind, "fake_paid")
        self.assertEqual(leak.targets, [])
        self.assertIn("Hold the shipment", leak.recovery)

    def test_paid_but_missing_at_paypal_is_fake_paid(self):
        result = run(claim(), None)
        self.assertEqual(kinds(result), ["fake_paid"])
        self.assertEqual(result.leaks[0].recovery_action, "invoice")

    def test_tampered_price_is_wrong_amount_with_price_root_cause(self):
        probe = [PaymentCheck(name="price_tampering_probe", status=CheckStatus.FAILED, message="trusted", evidence={})]
        result = run(claim(total="0.01", unit_price="0.01"), paypal_order(amount="0.01", captures=("0.01",)), probe)
        self.assertEqual(kinds(result), ["wrong_amount"])
        self.assertEqual(result.money_at_risk, "17.99")
        self.assertIn("browser", result.leaks[0].root_cause)
        statuses = {check.name: check.status for check in result.checks}
        self.assertEqual(statuses["price_integrity"], CheckStatus.FAILED)
        self.assertEqual(statuses["amount_matches"], CheckStatus.FAILED)

    def test_two_captures_is_a_duplicate_charge(self):
        result = run(claim(), paypal_order(captures=("18.00", "18.00")))
        self.assertEqual(kinds(result), ["duplicate_charge"])
        self.assertEqual(result.leaks[0].recovery_action, "refund")

    def test_unpaid_store_order_is_not_a_leak(self):
        result = run(claim(status="CANCELLED"), paypal_order(status="CREATED", captures=()))
        self.assertEqual(result.leaks, [])
        statuses = {check.name: check.status for check in result.checks}
        self.assertEqual(statuses["store_claim_matches"], CheckStatus.PASSED)

    def test_unknown_store_order_needs_review(self):
        result = run(None, paypal_order())
        self.assertEqual(result.verdict, "review")
        self.assertEqual(result.leaks, [])

    def test_builtin_explanation_names_money_at_risk(self):
        result = run(claim(), paypal_order(status="APPROVED", captures=()))
        explanation = investigate.builtin_explanation(result.model_dump(mode="json"))
        self.assertEqual(explanation.risk_level, "HIGH")
        self.assertIn("18.00", explanation.summary)
        self.assertEqual(explanation.source, "Built-in summary")


class WebsiteScanTests(unittest.TestCase):
    def test_store_page_is_scanned_with_the_full_preset_past_the_private_address_rule(self):
        seen = {}

        async def fake_runner(website):
            seen["request"] = website
            seen["addresses"] = await validate_target_url(str(website.url))
            return "report"

        with patch.dict(os.environ, {"TESTPILOT_STORE_URL": "http://127.0.0.1:8100"}):
            result = asyncio.run(investigate.scan_store(fake_runner))
        self.assertEqual(result, "report")
        self.assertEqual(str(seen["request"].url), "http://127.0.0.1:8100/")
        self.assertEqual(seen["request"].checks, list(FULL_SCAN_CHECKS))
        self.assertEqual(seen["addresses"], ("127.0.0.1",))
        with self.assertRaises(UnsafeTarget):
            asyncio.run(validate_target_url("http://127.0.0.1:8100/"))

    def test_website_scan_is_skipped_when_the_store_has_no_record(self):
        calls = []

        async def fake_runner(website):
            calls.append(website)

        result = run(None, paypal_order(), run_website=fake_runner, website_checks=True)
        self.assertEqual(calls, [])
        self.assertIsNone(result.website_report)


class XssSectionTests(unittest.TestCase):
    FOUND = {
        "status": "failed", "summary": "XSS found: 1 of 15 payloads came back unescaped in HTML.",
        "content_security_policy": False, "payload_count": 15, "parameters": ["q"], "requests": 18,
        "payloads": [{"label": "Raw HTML tag", "context": "HTML body", "outcome": "unescaped", "reflected_in": ["q"]}],
        "findings": [{"title": "Reflected XSS: Raw HTML tag", "severity": "high", "detail": "d",
                      "recommendation": "Encode output for its exact context."}],
        "limitations": [],
    }

    def test_xss_is_its_own_section_and_does_not_change_the_money_verdict(self):
        result = run(claim(), paypal_order(), xss=self.FOUND)
        self.assertEqual(result.verdict, "healthy")
        self.assertEqual(result.xss_scan["status"], "failed")
        self.assertNotIn("xss_probe", [check.name for check in result.checks])

    def test_found_xss_raises_the_risk_and_names_the_fix(self):
        result = run(claim(), paypal_order(), xss=self.FOUND)
        digest = result.model_dump(mode="json")
        explanation = investigate.builtin_explanation(digest)
        self.assertEqual(explanation.risk_level, "HIGH")
        self.assertIn("Encode output for its exact context.", explanation.next_steps)

    def test_xss_probe_can_be_turned_off(self):
        self.assertIsNone(run(claim(), paypal_order()).xss_scan)


class DuplicateChargeTests(unittest.TestCase):
    SECOND = {"order_id": "9ZZ00000000000002", "store_status": "PAID", "total": "18.00",
              "paypal_status": "COMPLETED", "captured": "18.00", "capture_ids": ["CAP2"]}

    def statuses(self, result):
        return {check.name: check.status for check in result.checks}

    def test_a_cart_charged_by_a_second_order_is_a_duplicate(self):
        result = run(claim(), paypal_order(), siblings=[self.SECOND])
        self.assertEqual(kinds(result), ["duplicate_charge"])
        self.assertEqual(result.leaks[0].amount, "18.00")
        self.assertIn("9ZZ00000000000002", result.leaks[0].recovery)
        self.assertEqual(self.statuses(result)["no_duplicate_charge"], CheckStatus.FAILED)

    def test_a_second_order_that_was_never_paid_is_fine(self):
        unpaid = {**self.SECOND, "store_status": "PENDING", "paypal_status": "CREATED", "captured": "0.00", "capture_ids": []}
        result = run(claim(), paypal_order(), siblings=[unpaid])
        self.assertEqual(result.leaks, [])
        self.assertEqual(result.verdict, "healthy")
        self.assertEqual(self.statuses(result)["no_duplicate_charge"], CheckStatus.PASSED)

    def test_no_duplicate_when_this_order_itself_was_not_charged(self):
        result = run(claim(), paypal_order(status="APPROVED", captures=()), siblings=[self.SECOND])
        self.assertEqual(kinds(result), ["never_captured"])

    def test_without_a_cart_the_check_is_skipped_not_failed(self):
        result = run(claim(), paypal_order())
        self.assertEqual(self.statuses(result)["no_duplicate_charge"], CheckStatus.SKIPPED)
        self.assertEqual(result.verdict, "healthy")

    def test_siblings_are_read_from_the_store_and_checked_at_paypal(self):
        async def fake_store_json(_session, method, path, body=None):
            self.assertEqual(path, "/api/carts/cart-0001-abcd/orders")
            return 200, [{"order_id": ORDER_ID, "status": "PAID"}, {"order_id": "9ZZ00000000000002", "status": "PAID"}]

        async def fake_fetch(order_id):
            self.assertEqual(order_id, "9ZZ00000000000002")
            return paypal_order()

        with patch("app.investigate._store_json", fake_store_json), patch("app.paypal.fetch_order", fake_fetch):
            siblings, note = asyncio.run(investigate.sibling_payments({**claim(), "cart_id": "cart-0001-abcd"}))
        self.assertIsNone(note)
        self.assertEqual([(s["order_id"], s["captured"]) for s in siblings], [("9ZZ00000000000002", "18.00")])


class StorePickerTests(unittest.TestCase):
    def test_each_store_has_its_own_operator_setting(self):
        env = {"TESTPILOT_STORE_URL": "http://127.0.0.1:8100", "TESTPILOT_VULN_STORE_URL": "http://127.0.0.1:8200"}
        with patch.dict(os.environ, env):
            result = run(claim(), paypal_order(), store="vulnerable")
        self.assertEqual((result.store, result.store_url), ("vulnerable", "http://127.0.0.1:8200"))

    def test_a_request_cannot_name_a_url(self):
        with self.assertRaises(ValidationError):
            InvestigationRequest(order_id=ORDER_ID, store="http://169.254.169.254/")


class InvestigationEndpointTests(unittest.TestCase):
    def test_missing_paypal_keys_returns_503(self):
        async def fake_store(_order_id):
            return claim(), CATALOG, None

        with (
            patch.dict(os.environ, {"TESTPILOT_PAYPAL_CLIENT_ID": "", "TESTPILOT_PAYPAL_CLIENT_SECRET": ""}),
            patch("app.investigate.read_store", fake_store),
        ):
            response = TestClient(app).post("/api/orders/investigate", json={"order_id": ORDER_ID, "explain": False})
        self.assertEqual(response.status_code, 503)

    def test_invalid_order_id_is_rejected(self):
        response = TestClient(app).post("/api/orders/investigate", json={"order_id": "../admin"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
