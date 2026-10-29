"""Vulnerable store tests: each demo bug really happens, and only when switched on."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import store_app
from store_app import app

CART = {"items": [{"product_id": "tee", "quantity": 1, "unit_price": "1.00"}], "cart_id": "cart-demo-0001"}


class FakePayPal:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, path, body=None, request_id=None):
        self.calls.append((method, path, body, request_id))
        if path == "/v2/checkout/orders":
            return {"id": f"7AB0000000000{len(self.calls):03d}", "status": "CREATED"}
        if path.endswith("/capture"):
            return {"purchase_units": [{"payments": {"captures": [{"id": "CAP1", "status": "COMPLETED"}]}}]}
        raise AssertionError(f"unexpected PayPal call {method} {path}")


class VulnerableStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.paypal = FakePayPal()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (
            patch("store_app.paypal_request", self.paypal),
            patch.dict(os.environ, {"VULN_STORE_DATA_FILE": str(Path(folder.name) / "store.json")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        store_app.orders.clear()
        store_app.active_bugs.clear()

    def bugs(self, *names):
        self.assertEqual(self.client.post("/api/bugs", json={"bugs": list(names)}).status_code, 200)

    def pay(self):
        order_id = self.client.post("/api/orders", json=CART).json()["id"]
        return order_id, self.client.post(f"/api/orders/{order_id}/capture")

    def test_without_bugs_it_charges_the_catalog_price_and_locks_the_cart(self):
        order_id, paid = self.pay()
        self.assertEqual(paid.json()["total"], "18.00")
        self.assertEqual(self.client.post("/api/orders", json=CART).status_code, 409)

    def test_trust_client_price_charges_what_the_browser_sends(self):
        self.bugs("trust_client_price")
        _, paid = self.pay()
        self.assertEqual(paid.json()["total"], "1.00")
        self.assertEqual(self.paypal.calls[0][2]["purchase_units"][0]["amount"]["value"], "1.00")

    def test_confirm_without_capture_never_asks_paypal(self):
        self.bugs("confirm_without_capture")
        _, paid = self.pay()
        self.assertEqual(paid.json()["status"], "PAID")
        self.assertFalse(any(call[1].endswith("/capture") for call in self.paypal.calls))

    def test_allow_double_payment_charges_one_cart_twice(self):
        self.bugs("allow_double_payment")
        self.pay()
        _, again = self.pay()
        self.assertEqual(again.json()["status"], "PAID")
        statuses = [order["status"] for order in self.client.get("/api/carts/cart-demo-0001/orders").json()]
        self.assertEqual(statuses, ["PAID", "PAID"])

    def test_search_box_reflects_input_unescaped_and_no_security_headers_are_sent(self):
        response = self.client.get("/", params={"q": "<b>x</b>"})
        self.assertIn("You searched for: <b>x</b>", response.text)
        self.assertNotIn("content-security-policy", response.headers)
        self.assertNotIn("x-frame-options", response.headers)

    def test_unknown_bugs_are_rejected(self):
        self.assertEqual(self.client.post("/api/bugs", json={"bugs": ["rm_rf"]}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
