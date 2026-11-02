"""Auto-scan: on/off and timing are saved, every store is scanned, and alerts go out only on a change."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import autoscan, investigate, store_security
from app.main import app
from app.schemas import AutoscanSettings, ScanResult, StoreSecurity, TriagePlan

XSS = store_security.xss_finding("XSS found.")


def result(store, leaking=0, at_risk="0.00"):
    return ScanResult(store=store, scanned_at=datetime.now(timezone.utc), orders_listed=2, orders_checked=2,
                      leaking_orders=leaking, money_at_risk=at_risk, currency="USD", items=[],
                      plan=TriagePlan(summary="Capture first.", steps=[], source="rules", notes=[]))


class AutoscanTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"TESTPILOT_ALERTS_DIR": folder.name})
        env.start()
        self.addCleanup(env.stop)

    def run_scans(self, outcomes):
        """outcomes: store -> (leaking orders, money at risk, security findings), or an exception to raise."""
        sent = []

        async def fake_scan(request):
            outcome = outcomes[request.store]
            if isinstance(outcome, Exception):
                raise outcome
            return result(request.store, outcome[0], outcome[1])

        async def fake_check(store):
            return StoreSecurity(checked=True, findings=list(outcomes[store][2]))

        async def fake_deliver(event, owner):
            sent.append(event["event"])
            return []

        with (
            patch("app.scan.scan_store", fake_scan), patch("app.store_security.check", fake_check),
            patch("app.alerts.deliver_to_store_owners", fake_deliver),
        ):
            asyncio.run(autoscan.run_once())
        return sent

    def test_it_is_off_until_turned_on_and_turning_it_on_schedules_a_scan_now(self):
        self.assertFalse(autoscan.status().enabled)
        self.assertFalse(autoscan.due())
        now = datetime.now(timezone.utc)
        status = autoscan.configure(AutoscanSettings(enabled=True, interval_minutes=5), now=now)
        self.assertEqual((status.enabled, status.interval_minutes, status.next_run), (True, 5, now))
        self.assertTrue(autoscan.due(now))
        self.assertIsNone(autoscan.configure(AutoscanSettings(enabled=False)).next_run)
        self.assertFalse(autoscan.due(now + timedelta(hours=1)))

    def test_a_run_scans_every_store_alerts_once_and_schedules_the_next(self):
        autoscan.configure(AutoscanSettings(enabled=True, interval_minutes=10))
        outcomes = {"main": (0, "0.00", []), "vulnerable": (1, "18.00", [XSS]), "risky": (0, "0.00", [XSS])}
        self.assertEqual(sorted(self.run_scans(outcomes)), ["money_leaking", "store_vulnerable"])
        status = autoscan.status()
        self.assertAlmostEqual((status.next_run - status.last_run).total_seconds(), 600, delta=1)
        self.assertEqual(self.run_scans(outcomes), [])  # nothing changed, so nothing is sent again

    def test_a_change_is_alerted_again(self):
        self.run_scans({"main": (0, "0.00", []), "vulnerable": (1, "18.00", []), "risky": (0, "0.00", [])})
        sent = self.run_scans({"main": (0, "0.00", []), "vulnerable": (2, "30.00", []), "risky": (0, "0.00", [])})
        self.assertEqual(sent, ["money_leaking"])

    def test_an_unreachable_store_is_noted_and_the_others_still_run(self):
        sent = self.run_scans({"main": investigate.StoreUnavailable("down"), "vulnerable": (1, "18.00", []),
                               "risky": (0, "0.00", [])})
        self.assertEqual(sent, ["money_leaking"])
        self.assertIn("main: down", autoscan.status().last_error)

    def test_the_api_turns_it_on_and_off_and_rejects_odd_intervals(self):
        client = TestClient(app)
        self.assertTrue(client.put("/api/autoscan", json={"enabled": True, "interval_minutes": 30}).json()["enabled"])
        self.assertEqual(client.get("/api/autoscan").json()["interval_minutes"], 30)
        self.assertEqual(client.put("/api/autoscan", json={"enabled": True, "interval_minutes": 7}).status_code, 422)
        self.assertFalse(client.put("/api/autoscan", json={"enabled": False}).json()["enabled"])


if __name__ == "__main__":
    unittest.main()
