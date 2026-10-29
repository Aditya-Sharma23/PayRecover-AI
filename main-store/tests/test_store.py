"""Main store tests with PayPal replaced by a recording fake; no network is used."""

import json
import os
import tempfile
import unittest
from pathlib import Path
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
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.orders_file = Path(folder.name) / "orders.json"
        for patcher in (
            patch("store_app.paypal_request", self.paypal),
            patch.dict(os.environ, {"STORE_ORDERS_FILE": str(self.orders_file)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        store_app.orders.clear()

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

    def test_an_order_is_paid_only_after_paypal_captures_it(self):
        order_id = self.create()["id"]
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").json()["status"], "PENDING")
        self.client.post(f"/api/orders/{order_id}/capture")
        self.assertEqual([call[1] for call in self.paypal.calls][-1], f"/v2/checkout/orders/{order_id}/capture")

    def test_cancel_marks_only_pending_orders(self):
        order_id = self.create()["id"]
        self.assertEqual(self.client.post(f"/api/orders/{order_id}/cancel").json()["status"], "CANCELLED")
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").json()["status"], "CANCELLED")

    def test_unknown_orders_and_products_are_rejected(self):
        self.assertEqual(self.client.get("/api/orders/NOPE1234").status_code, 404)
        self.assertEqual(self.client.get("/api/orders/not-an-id").status_code, 422)
        bad = self.client.post("/api/orders", json={"items": [{"product_id": "car", "quantity": 1}]})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.paypal.calls, [])

    def test_orders_survive_a_restart(self):
        order_id = self.create()["id"]
        saved = json.loads(self.orders_file.read_text(encoding="utf-8"))
        self.assertEqual(saved[order_id]["total"], "48.50")
        store_app.orders.clear()
        store_app.orders.update(store_app._load_orders())
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").json()["status"], "PENDING")

    def test_the_old_defect_switch_is_gone(self):
        self.assertEqual(self.client.get("/api/defects").status_code, 404)
        self.assertFalse(hasattr(store_app, "KNOWN_DEFECTS"))

    def test_every_response_sends_security_headers(self):
        for path in ("/", "/api/products", "/api/orders/NOPE1234", "/static/store.js"):
            headers = self.client.get(path).headers
            for name in ("content-security-policy", "x-content-type-options", "referrer-policy",
                         "permissions-policy", "x-frame-options"):
                self.assertIn(name, headers, f"{path} is missing {name}")
        self.assertEqual(self.client.get("/static/store.js").headers["cache-control"], "no-cache")
        csp = self.client.get("/").headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("https://*.paypal.com", csp)

    def test_the_page_gets_a_fresh_nonce_that_matches_its_csp(self):
        first, second = self.client.get("/"), self.client.get("/")
        nonce = first.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', first.text)
        self.assertNotIn("{{CSP_NONCE}}", first.text)
        self.assertNotIn(nonce, second.text)
        self.assertNotIn("'unsafe-inline'", first.headers["content-security-policy"].split("script-src")[1].split(";")[0])

    def test_favicon_does_not_404(self):
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)

    def test_every_error_code_is_documented(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("404", paths["/api/orders/{order_id}"]["get"]["responses"])
        self.assertIn("502", paths["/api/orders"]["post"]["responses"])
        self.assertIn("503", paths["/api/config"]["get"]["responses"])

    def test_config_exposes_client_id_but_never_the_secret(self):
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "public-id", "PAYPAL_CLIENT_SECRET": "very-secret"}):
            body = self.client.get("/api/config").text
        self.assertIn("public-id", body)
        self.assertNotIn("very-secret", body)


if __name__ == "__main__":
    unittest.main()
