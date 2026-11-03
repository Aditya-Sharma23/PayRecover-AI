"""PayPal setup check: features PayPal refuses are reported as off, with where to switch them on."""

import isolation
import asyncio
import unittest
from unittest.mock import patch

from app import paypal, paypal_setup, paypal_toolkit


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class SetupTests(unittest.TestCase):
    def setUp(self):
        paypal_setup._cache.update(at=None, value=None)

    def run_check(self, invoices=200, refused_tools=(), credentials_ok=True):
        async def call(method, path, body=None, request_id=None):
            if not credentials_ok:
                raise paypal.PayPalConfigurationError("PayPal rejected the Sandbox credentials.")
            return (invoices, {}) if "invoicing" in path else (401, {})

        async def toolkit(method, params):
            if method in refused_tools:
                raise paypal_toolkit.ToolkitError("refused", 403, "NOT_AUTHORIZED")
            return {"items": []}

        with (
            patch.object(paypal_setup.paypal, "paypal_call", call),
            patch.object(paypal_setup.paypal_toolkit, "run", toolkit),
            patch.object(paypal_setup.paypal, "forget_token", lambda: None),
        ):
            return asyncio.run(paypal_setup.check(refresh=True))

    def test_everything_on_is_ready(self):
        result = self.run_check()
        self.assertTrue(result.ready)
        self.assertEqual({item.id for item in result.items if item.ok},
                         {"credentials", "transaction_search", "disputes", "invoicing"})

    def test_a_refused_feature_is_off_and_says_where_to_turn_it_on(self):
        result = self.run_check(invoices=403, refused_tools=("list_disputes",))
        self.assertFalse(result.ready)
        items = {item.id: item for item in result.items}
        self.assertFalse(items["disputes"].ok)
        self.assertIn("Features > Disputes", items["disputes"].fix)
        self.assertIn("NOT_AUTHORIZED", items["disputes"].detail)
        self.assertFalse(items["invoicing"].ok)
        self.assertTrue(items["transaction_search"].ok)

    def test_webhooks_are_optional_and_do_not_block_ready(self):
        webhooks = next(item for item in self.run_check().items if item.id == "webhooks")
        self.assertFalse(webhooks.required)

    def test_rejected_keys_stop_the_check_early(self):
        result = self.run_check(credentials_ok=False)
        self.assertFalse(result.ready)
        self.assertEqual([item.id for item in result.items], ["credentials"])


if __name__ == "__main__":
    unittest.main()
