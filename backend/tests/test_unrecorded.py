"""PayPal's side of the scan: payments tagged for the store but missing from its paid orders."""

import asyncio
import unittest
from unittest.mock import patch

from app import paypal_toolkit, unrecorded


def transaction(transaction_id, value, tag="payrecover-vulnerable", reference=None, email="buyer@example.com"):
    return {"transaction_info": {"transaction_id": transaction_id, "custom_field": tag,
                                 "transaction_amount": {"value": value, "currency_code": "USD"},
                                 "transaction_initiation_date": "2026-11-01T10:00:00+0000",
                                 "paypal_reference_id": reference},
            "payer_info": {"email_address": email}}


class UnrecordedTests(unittest.TestCase):
    def find(self, transactions, orders, error=None):
        async def fake_toolkit(method, params):
            self.params = params
            if error:
                raise error
            return {"transaction_details": transactions}

        async def fake_orders(_limit):
            return orders

        with patch("app.paypal_toolkit.run", fake_toolkit), patch("app.scan.recent_orders", fake_orders):
            return asyncio.run(unrecorded.find("vulnerable"))

    def test_a_tagged_payment_missing_from_the_store_is_reported_with_a_masked_email(self):
        report = self.find([transaction("CAPLOST", "18.00"), transaction("CAPKNOWN", "5.00")],
                           [{"order_id": "A", "status": "PAID", "capture_id": "CAPKNOWN"}])
        self.assertTrue(report.checked)
        self.assertEqual([p.transaction_id for p in report.payments], ["CAPLOST"])
        self.assertEqual((report.money, report.payments[0].payer_email_masked), ("18.00", "b***@example.com"))
        self.assertTrue(self.params["start_date"].endswith("Z"))

    def test_other_stores_and_refunded_payments_are_left_out(self):
        report = self.find([transaction("CAPMAIN", "9.00", tag="payrecover-main"),
                            transaction("CAPBACK", "7.00"), transaction("REFUND1", "-7.00", reference="CAPBACK")], [])
        self.assertEqual(report.payments, [])

    def test_transaction_search_switched_off_explains_how_to_turn_it_on(self):
        report = self.find([], [], paypal_toolkit.ToolkitError("refused", 403, "NOT_AUTHORIZED"))
        self.assertFalse(report.checked)
        self.assertIn("Features > Transaction search", report.note)


class ToolkitTests(unittest.TestCase):
    def test_declarations_use_the_toolkit_descriptions_and_schemas(self):
        declared = {d["name"]: d for d in paypal_toolkit.declarations()}
        self.assertEqual(set(declared), set(paypal_toolkit.READ_ONLY_TOOLS))
        self.assertTrue(declared["get_dispute"]["description"].startswith("[PayPal Agent Toolkit]"))
        self.assertIn("dispute_id", declared["get_dispute"]["parametersJsonSchema"]["required"])


if __name__ == "__main__":
    unittest.main()
