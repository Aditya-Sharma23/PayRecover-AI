"""Demo store tests with PayPal replaced by a recording fake; no network is used."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import store_app
from store_app import app

CART = {"items": [{"product_id": "tee", "quantity": 2, "unit_price": "0.01"}, {"product_id": "mug", "quantity": 1}]}


class FakePayPal:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, path, body=None, request_id=None):
        self.calls.append((method, path, body, request_id))
        if path == "/v2/checkout/orders":
            return {"id": "5O190127TN364715T", "status": "CREATED"}
        if path.endswith("/capture"):
            return {"purchase_units": [{"payments": {"captures": [{"id": "3C679366HH908993F", "status": "COMPLETED"}]}}]}
        raise AssertionError(f"unexpected PayPal call {method} {path}")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.paypal = FakePayPal()
        store_app.orders.clear()
        store_app._active_defects.clear()
        patcher = patch("store_app.paypal_request", self.paypal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self) -> dict:
        response = self.client.post("/api/orders", json=CART)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_server_prices_the_cart_and_ignores_the_browser_price(self):
        order = self.create()
        self.assertEqual(order["total"], "48.50")
        unit = self.paypal.calls[0][2]["purchase_units"][0]
        self.assertEqual(unit["amount"]["value"], "48.50")
        self.assertEqual(unit["items"][0]["unit_amount"]["value"], "18.00")

    def test_capture_records_paypal_capture_and_is_idempotent(self):
        order_id = self.create()["id"]
        paid = self.client.post(f"/api/orders/{order_id}/capture").json()
        self.assertEqual(paid["status"], "PAID")
        self.assertEqual(paid["capture_id"], "3C679366HH908993F")
        self.assertEqual(self.paypal.calls[1][3], f"capture-{order_id}")
        self.client.post(f"/api/orders/{order_id}/capture")
        self.assertEqual(len(self.paypal.calls), 2)

    def test_cancel_marks_only_pending_orders(self):
        order_id = self.create()["id"]
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/cancel").json()["status"], "CANCELLED")
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").json()["status"], "CANCELLED")

    def test_unknown_orders_and_products_are_rejected(self):
        self.assertEqual(self.client.get("/api/orders/NOPE1234").status_code, 404)
        bad = self.client.post("/api/orders", json={"items": [{"product_id": "car", "quantity": 1}]})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.paypal.calls, [])

    def test_trust_client_price_defect_lets_the_browser_set_the_amount(self):
        store_app._active_defects.add("trust_client_price")
        self.assertEqual(self.create()["total"], "12.52")

    def test_confirm_without_capture_defect_never_asks_paypal(self):
        store_app._active_defects.add("confirm_without_capture")
        order_id = self.create()["id"]
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/capture").json()["status"], "PAID")
        self.assertEqual(len(self.paypal.calls), 1)

    def test_runtime_defect_switch_needs_the_admin_token(self):
        with patch.dict(os.environ, {"STORE_ADMIN_TOKEN": ""}):
            self.assertEqual(self.client.post("/api/defects", json={"defects": []}).status_code, 404)
        with patch.dict(os.environ, {"STORE_ADMIN_TOKEN": "t" * 32}):
            denied = self.client.post("/api/defects", json={"defects": ["trust_client_price"]})
            self.assertEqual(denied.status_code, 401)
            allowed = self.client.post(
                "/api/defects", json={"defects": ["trust_client_price"]}, headers={"X-Store-Admin-Token": "t" * 32}
            )
            self.assertEqual(allowed.json()["active"], ["trust_client_price"])
            unknown = self.client.post(
                "/api/defects", json={"defects": ["rm_rf"]}, headers={"X-Store-Admin-Token": "t" * 32}
            )
            self.assertEqual(unknown.status_code, 422)

    def test_config_exposes_client_id_but_never_the_secret(self):
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "public-id", "PAYPAL_CLIENT_SECRET": "very-secret"}):
            body = self.client.get("/api/config").text
        self.assertIn("public-id", body)
        self.assertNotIn("very-secret", body)


if __name__ == "__main__":
    unittest.main()
