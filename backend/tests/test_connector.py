"""Connect your own store: the AI's mapping is only proposed; PayRecover's checks and PayPal decide."""

import isolation
import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import connector, investigate, paypal
from app.main import app
from app.schemas import ConnectorRequest, StoreMapping

PAID_ID = "5O190127TN364715T"
OTHER_ID = "9ZZ00000000000002"
SALES = {"data": {"sales": [
    {"state": "complete", "payment": {"orderRef": PAID_ID, "captureRef": "CAP1"}, "amount": {"totalCents": 1800, "currency": "USD"},
     "lines": [{"sku": "tee", "qty": 1, "priceCents": 1800}], "basket": "cart-1", "contact": "buyer@example.com"},
    {"state": "cancelled", "payment": {"orderRef": OTHER_ID}, "amount": {"totalCents": 450, "currency": "USD"},
     "lines": [], "basket": "cart-2"},
]}}
CATALOG = {"products": [{"sku": "tee", "title": "T-shirt", "priceCents": 1800}]}
AI_ANSWER = {
    "orders_list": "data.sales", "products_list": "products", "money": "cents", "currency": "USD",
    "paid_statuses": ["complete"],
    "order_fields": {"order_id": "payment.orderRef", "status": "state", "total": "amount.totalCents",
                     "currency": "amount.currency", "items": "lines", "item_product_id": "sku", "item_quantity": "qty",
                     "item_unit_price": "priceCents", "cart_id": "basket", "capture_id": "payment.captureRef"},
    "product_fields": {"id": "sku", "price": "priceCents", "name": "title"},
}
REQUEST = ConnectorRequest(base_url="http://shop.example", orders_path="/v2/sales", products_path="/v2/catalog")


def fake_store(answers):
    async def fetch(url):
        for path, answer in answers.items():
            if url.endswith(path):
                return 200, answer
        return 404, None
    return fetch


def paypal_order(amount="18.00"):
    return {"id": PAID_ID, "status": "COMPLETED", "intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": "USD", "value": amount}}]}


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        connector._cache.clear()
        connector.forget("local")

    def propose(self, ai_answer=AI_ANSWER, amount="18.00"):
        prompts = []

        async def ask(prompt):
            prompts.append(prompt)
            return ai_answer

        async def fetch_order(_order_id):
            return paypal_order(amount)

        with (
            patch.object(connector, "fetch_json", fake_store({"/v2/sales": SALES, "/v2/catalog": CATALOG})),
            patch.object(connector.ai, "ask_json", ask),
            patch.object(connector.paypal, "fetch_order", fetch_order),
        ):
            return asyncio.run(connector.propose(REQUEST)), prompts

    def test_a_correct_mapping_reads_every_order_and_matches_paypal(self):
        proposal, _ = self.propose()
        self.assertTrue(proposal.ready)
        self.assertEqual(proposal.preview[0], {
            "order_id": PAID_ID, "status": "PAID", "total": "18.00", "currency": "USD",
            "items": [{"product_id": "tee", "name": "tee", "quantity": 1, "unit_price": "18.00"}],
            "cart_id": "cart-1", "created_at": None, "capture_id": "CAP1"})
        self.assertEqual(proposal.preview[1]["status"], "CANCELLED")
        cross = [check for check in proposal.checks if check.name == "paypal_cross_check"]
        self.assertEqual([check.status for check in cross], ["passed"])

    def test_paypal_overrules_a_mapping_that_reads_the_wrong_amount(self):
        proposal, _ = self.propose(ai_answer={**AI_ANSWER, "money": "decimal"})  # 1800 read as 1800.00
        self.assertFalse(proposal.ready)
        self.assertIn("1800.00", next(c.message for c in proposal.checks if c.name == "paypal_cross_check"))

    def test_the_ai_sees_every_status_but_no_emails(self):
        _, (prompt,) = self.propose()
        self.assertIn('"state": ["cancelled", "complete"]', prompt)
        self.assertNotIn("buyer@example.com", prompt)
        self.assertIn("<email>", prompt)

    def test_an_unusable_ai_answer_is_an_error_not_a_saved_store(self):
        with self.assertRaises(connector.ConnectorError):
            self.propose(ai_answer={**AI_ANSWER, "paid_statuses": []})
        self.assertFalse(connector.connected("local"))

    def test_the_connected_store_is_read_through_the_mapping_by_every_check(self):
        proposal, _ = self.propose()
        connector.save(proposal.mapping, "local")
        token = investigate._active_store.set("connected")
        self.addCleanup(investigate._active_store.reset, token)
        with patch.object(connector, "fetch_json", fake_store({"/v2/sales": SALES, "/v2/catalog": CATALOG})):
            status, recent = asyncio.run(connector.store_json("GET", "/api/recent-orders?limit=1"))
            _, one = asyncio.run(connector.store_json("GET", f"/api/orders/{OTHER_ID}"))
            _, cart = asyncio.run(connector.store_json("GET", "/api/carts/cart-1/orders"))
            _, products = asyncio.run(connector.store_json("GET", "/api/products"))
            write, _ = asyncio.run(connector.store_json("POST", "/api/orders"))
        self.assertEqual((status, [o["order_id"] for o in recent]), (200, [PAID_ID]))
        self.assertEqual(one["total"], "4.50")
        self.assertEqual([o["order_id"] for o in cart], [PAID_ID])
        self.assertEqual(products, [{"id": "tee", "name": "T-shirt", "price": "18.00", "market": "T-shirt"}])
        self.assertEqual(write, 405)
        self.assertEqual(investigate.store_url(), "http://shop.example")

    def test_each_user_has_their_own_connected_store(self):
        proposal, _ = self.propose()
        connector.save(proposal.mapping, "alice")
        self.assertTrue(connector.connected("alice"))
        self.assertFalse(connector.connected("bob"))
        connector.forget("alice")

    def test_a_public_server_will_not_read_a_private_address(self):
        with patch.dict(os.environ, {"TESTPILOT_PUBLIC_DEPLOYMENT": "true"}):
            with self.assertRaises(connector.ConnectorError) as raised:
                asyncio.run(connector.fetch_json("http://127.0.0.1:8200/orders"))
        self.assertIn("will not read", str(raised.exception))

    def test_saving_rechecks_and_refuses_a_mapping_that_fails(self):
        client = TestClient(app)
        bad = StoreMapping.model_validate({**AI_ANSWER, "money": "decimal", "base_url": "http://shop.example",
                                           "orders_path": "/v2/sales", "products_path": "/v2/catalog"})

        async def fetch_order(_order_id):
            return paypal_order()

        with (
            patch.object(connector, "fetch_json", fake_store({"/v2/sales": SALES, "/v2/catalog": CATALOG})),
            patch.object(paypal, "fetch_order", fetch_order),
        ):
            refused = client.put("/api/connector", json=bad.model_dump(mode="json"))
            good = client.put("/api/connector", json={**bad.model_dump(mode="json"), "money": "cents"})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(good.status_code, 200)
        self.assertTrue(client.get("/api/connector").json()["connected"])
        self.assertTrue(client.delete("/api/connector").json()["removed"])


if __name__ == "__main__":
    unittest.main()
