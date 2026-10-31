"""PayPal Server SDK adapter: Sandbox only, PayPal's JSON passed through, errors as statuses."""

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from paypalserversdk.configuration import Environment
from paypalserversdk.exceptions.api_exception import ApiException

from app import paypal, paypal_sdk

KEYS = {"TESTPILOT_PAYPAL_CLIENT_ID": "id", "TESTPILOT_PAYPAL_CLIENT_SECRET": "secret"}


class AdapterTests(unittest.TestCase):
    def test_the_client_is_always_sandbox(self):
        client = paypal_sdk._client("id", "secret")
        self.assertEqual(client.config.environment, Environment.SANDBOX)
        self.assertIs(paypal_sdk._client("id", "secret"), client)

    def test_a_reply_is_passed_through_as_status_and_json(self):
        reply = SimpleNamespace(status_code=200, text='{"id": "ORDER1", "status": "COMPLETED"}')
        self.assertEqual(paypal_sdk._call(lambda: reply), (200, {"id": "ORDER1", "status": "COMPLETED"}))

    def test_paypal_errors_become_statuses_not_exceptions(self):
        def fail():
            raise ApiException("not found", SimpleNamespace(status_code=404, text='{"name": "RESOURCE_NOT_FOUND"}'))

        self.assertEqual(paypal_sdk._call(fail), (404, {"name": "RESOURCE_NOT_FOUND"}))


class FetchOrderTests(unittest.TestCase):
    def fetch(self, answer):
        def fake_get_order(_client_id, _secret, _order_id):
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.dict(os.environ, KEYS), patch("app.paypal_sdk.get_order", fake_get_order):
            return asyncio.run(paypal.fetch_order("ORDER123456"))

    def test_an_order_is_read_through_the_sdk(self):
        self.assertEqual(self.fetch((200, {"id": "ORDER123456"})), {"id": "ORDER123456"})

    def test_missing_orders_bad_keys_and_network_failures_are_told_apart(self):
        with self.assertRaises(paypal.PayPalOrderNotFound):
            self.fetch((404, {}))
        with self.assertRaises(paypal.PayPalConfigurationError):
            self.fetch((401, {}))
        with self.assertRaises(paypal.PayPalProviderError):
            self.fetch(ConnectionError("down"))


if __name__ == "__main__":
    unittest.main()
