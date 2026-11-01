"""Alerts: the dashboard feed always records, email and Zapier send only when set up, and settings are checked."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import alerts
from app.main import app
from app.schemas import AlertSettings

EVENT = {"event": "leak_found", "summary": "18.00 USD never captured", "message": "Approve: https://x/approve",
         "order_id": "5O190127TN364715T", "store": "vulnerable"}
ZAPIER = "https://hooks.zapier.com/hooks/catch/123/abc/"


class AlertTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"TESTPILOT_ALERTS_DIR": folder.name, "TESTPILOT_ZAPIER_WEBHOOK_URL": "",
                                      "TESTPILOT_BREVO_API_KEY": "key", "TESTPILOT_ALERT_SENDER": "alerts@shop.example"})
        env.start()
        self.addCleanup(env.stop)
        self.emails, self.hooks = [], []

        async def fake_email(to, subject, text):
            self.emails.append((to, subject, text))
            return True

        async def fake_hook(event, url=None):
            self.hooks.append(url)
            return True

        for patcher in (patch("app.alerts.send_email", fake_email), patch("app.notify.send", fake_hook)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(alerts._tests.clear)

    def test_without_settings_the_alert_still_reaches_the_dashboard(self):
        item = asyncio.run(alerts.deliver(EVENT, "local"))
        self.assertEqual(item.channels, {})
        self.assertEqual([a.summary for a in alerts.view("local").alerts], ["18.00 USD never captured"])
        self.assertEqual((self.emails, self.hooks), ([], []))

    def test_email_and_the_users_own_zapier_get_the_alert(self):
        alerts.save_settings("local", AlertSettings(email="owner@shop.example", zapier_url=ZAPIER))
        item = asyncio.run(alerts.deliver(EVENT, "local"))
        self.assertEqual(self.emails[0][:2], ("owner@shop.example", "18.00 USD never captured"))
        self.assertEqual(self.hooks, [ZAPIER])
        self.assertEqual(item.channels, {"email": "sent to o***@shop.example", "zapier": "sent"})

    def test_email_without_a_server_key_is_reported_not_sent(self):
        alerts.save_settings("local", AlertSettings(email="owner@shop.example"))
        with patch.dict(os.environ, {"TESTPILOT_BREVO_API_KEY": ""}):
            item = asyncio.run(alerts.deliver(EVENT, "local"))
        self.assertEqual(item.channels["email"], "not set up on the server")
        self.assertEqual(self.emails, [])

    def test_each_owner_keeps_their_own_settings_and_feed(self):
        alerts.save_settings("alice", AlertSettings(email="alice@shop.example"))
        asyncio.run(alerts.deliver(EVENT, "alice"))
        self.assertIsNone(alerts.settings("bob").email)
        self.assertEqual(alerts.view("bob").alerts, [])

    def test_bad_addresses_are_refused(self):
        with self.assertRaises(alerts.InvalidAlertSettings):
            alerts.save_settings("local", AlertSettings(email="not-an-email"))
        for url in ("https://evil.example/hook", "http://hooks.zapier.com/hooks/catch/1/a",
                    "https://hooks.zapier.com.evil.example/hooks/catch/1/a"):
            with self.assertRaises(alerts.InvalidAlertSettings):
                alerts.save_settings("local", AlertSettings(zapier_url=url))

    def test_store_events_reach_everyone_who_set_up_alerts(self):
        alerts.save_settings("alice", AlertSettings(email="alice@shop.example"))
        delivered = asyncio.run(alerts.deliver_to_store_owners(EVENT, "local"))
        self.assertEqual(len(delivered), 2)
        self.assertEqual(len(alerts.view("local").alerts), 1)

    def test_the_api_saves_settings_sends_a_test_and_limits_tests(self):
        client = TestClient(app)
        saved = client.put("/api/alerts/settings", json={"email": "owner@shop.example", "zapier_url": None})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(client.put("/api/alerts/settings", json={"zapier_url": "https://evil.example"}).status_code, 422)
        for _ in range(alerts.TESTS_PER_HOUR):
            self.assertEqual(client.post("/api/alerts/test").status_code, 200)
        self.assertEqual(client.post("/api/alerts/test").status_code, 429)
        view = client.get("/api/alerts").json()
        self.assertTrue(view["email_available"])
        self.assertEqual(len(view["alerts"]), alerts.TESTS_PER_HOUR)
        self.assertEqual(client.delete("/api/alerts").json(), {"deleted": alerts.TESTS_PER_HOUR})


if __name__ == "__main__":
    unittest.main()
