"""Zapier events and signed approval links."""

import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import notify
from app.main import app
from app.schemas import Investigation, Leak, RecoveryRecord, RecoveryTarget

SECRET = {"TESTPILOT_APPROVAL_SECRET": "s" * 40}
ORDER_ID = "5O190127TN364715T"


def leaking() -> Investigation:
    leak = Leak(kind="never_captured", title="Never captured", amount="18.00", currency="USD", root_cause="cause",
                recovery_action="capture", recovery="Capture it.", targets=[RecoveryTarget(order_id=ORDER_ID, amount="18.00")])
    return Investigation(order_id=ORDER_ID, verdict="leaking", store="vulnerable", checked_at="2026-10-31T00:00:00Z",
                         store_url="http://127.0.0.1:8200", checks=[], leaks=[leak], money_at_risk="18.00", currency="USD")


class TokenTests(unittest.TestCase):
    def test_a_token_round_trips_and_names_the_owner(self):
        with patch.dict(os.environ, SECRET):
            token = notify.approval_token(ORDER_ID, "main", "never_captured", "alice")
            self.assertEqual(notify.read_approval(token),
                             {"order_id": ORDER_ID, "store": "main", "kind": "never_captured", "owner": "alice"})

    def test_tampered_and_expired_tokens_are_refused(self):
        with patch.dict(os.environ, SECRET):
            token = notify.approval_token(ORDER_ID, "main", "never_captured", "alice", now=1000)
            payload, signature = token.split(".")
            forged = notify._b64(notify._unb64(payload).replace(b"alice", b"mallo")) + "." + signature
            with self.assertRaises(notify.InvalidApproval):
                notify.read_approval(forged, now=1001)
            with self.assertRaises(notify.InvalidApproval):
                notify.read_approval(token, now=1000 + notify.APPROVAL_TTL_SECONDS + 1)

    def test_without_a_strong_secret_there_are_no_links(self):
        with patch.dict(os.environ, {"TESTPILOT_APPROVAL_SECRET": "short"}):
            self.assertIsNone(notify.approval_token(ORDER_ID, "main", "never_captured", "alice"))


class EventTests(unittest.TestCase):
    def test_a_leak_event_has_an_approval_link_per_leak(self):
        with patch.dict(os.environ, {**SECRET, "TESTPILOT_PUBLIC_URL": "https://payrecover.example"}):
            event = notify.leak_event(leaking(), "local")
        self.assertEqual(event["event"], "leak_found")
        self.assertEqual(event["money_at_risk"], "18.00")
        self.assertTrue(event["leaks"][0]["approve_url"].startswith("https://payrecover.example/approve?token="))

    def test_nothing_is_sent_without_an_https_webhook(self):
        with patch.dict(os.environ, {"TESTPILOT_ZAPIER_WEBHOOK_URL": "http://hooks.zapier.com/x"}):
            self.assertFalse(asyncio.run(notify.send({"event": "leak_found"})))


class ApprovalPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_opening_the_link_only_asks(self):
        calls = []

        async def fake_recover(request, owner):
            calls.append((request, owner))

        with patch.dict(os.environ, SECRET), patch("app.recovery.recover", fake_recover):
            token = notify.approval_token(ORDER_ID, "main", "never_captured", "alice")
            page = self.client.get("/approve", params={"token": token})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Approve recovery</button>", page.text)
        self.assertEqual(calls, [])

    def test_pressing_approve_recovers_for_the_token_owner(self):
        calls = []

        async def fake_recover(request, owner):
            calls.append((request, owner))
            return RecoveryRecord(recovery_id="r1", order_id=ORDER_ID, store="main", kind="never_captured", action="capture",
                                  amount="18.00", currency="USD", status="recovered", message="Collected 18.00 USD.",
                                  created_at="2026-10-31T00:00:00Z")

        with patch.dict(os.environ, SECRET), patch("app.recovery.recover", fake_recover):
            token = notify.approval_token(ORDER_ID, "main", "never_captured", "alice")
            page = self.client.post("/approve", data={"token": token})
        self.assertIn("Recovery done", page.text)
        self.assertEqual(calls[0][1], "alice")
        self.assertTrue(calls[0][0].approved)

    def test_a_bad_link_is_refused(self):
        with patch.dict(os.environ, SECRET):
            self.assertEqual(self.client.post("/approve", data={"token": "nope.nope"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
