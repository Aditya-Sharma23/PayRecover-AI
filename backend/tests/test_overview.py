"""The dashboard overview: each store's latest scan, money at risk and recovered, and open disputes."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import overview, paypal_toolkit
from app.schemas import ScanResult, StoreSecurity, TriagePlan


def scan(store, verdict, at_risk="0.00", checked=3, findings=()):
    return ScanResult(store=store, scanned_at=datetime.now(timezone.utc), orders_listed=checked, orders_checked=checked,
                      leaking_orders=1 if verdict == "leaking" else 0, money_at_risk=at_risk, currency="USD", items=[],
                      plan=TriagePlan(summary="s", steps=[], source="rules", notes=[]),
                      security=StoreSecurity(checked=True, findings=list(findings)), verdict=verdict)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"TESTPILOT_ALERTS_DIR": folder.name,
                                      "TESTPILOT_RECOVERY_LOG_PATH": os.path.join(folder.name, "recoveries.json")})
        env.start()
        self.addCleanup(env.stop)
        overview._disputes_cache.update(at=0.0, count=None)
        self.addCleanup(overview._disputes_cache.update, at=0.0, count=None)

    def build(self, disputes=None, error=None):
        async def fake_toolkit(method, params):
            if error:
                raise error
            return {"items": [{}] * (disputes if params["dispute_state"] == "REQUIRED_ACTION" else 0)}

        with patch("app.paypal_toolkit.run", fake_toolkit):
            return asyncio.run(overview.build("local"))

    def test_before_any_scan_every_store_is_unscanned(self):
        view = self.build(disputes=0)
        self.assertEqual([(s.store, s.verdict) for s in view.stores], [("main", None), ("vulnerable", None), ("risky", None)])
        self.assertEqual((view.orders_scanned, view.money_at_risk, view.money_recovered), (0, "0.00", "0.00"))
        self.assertIsNone(view.last_activity)

    def test_the_latest_scan_of_each_store_adds_up(self):
        overview.remember_scan(scan("vulnerable", "leaking", "18.00", 5))
        overview.remember_scan(scan("main", "clear", checked=4))
        overview.remember_scan(scan("main", "clear", checked=6))  # replaces the earlier main scan
        view = self.build(disputes=2)
        verdicts = {s.store: s.verdict for s in view.stores}
        self.assertEqual(verdicts, {"main": "clear", "vulnerable": "leaking", "risky": None})
        self.assertEqual((view.orders_scanned, view.money_at_risk, view.open_disputes), (11, "18.00", 2))
        self.assertIsNotNone(view.last_activity)

    def test_paypal_refusing_disputes_shows_unknown_not_an_error(self):
        view = self.build(error=paypal_toolkit.ToolkitError("refused", 403))
        self.assertIsNone(view.open_disputes)


if __name__ == "__main__":
    unittest.main()
