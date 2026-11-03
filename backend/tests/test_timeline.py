"""The timeline: scans coloured by result, recoveries, and disputes running to PayPal's response deadline."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import paypal_toolkit, timeline
from app.schemas import ScanResult, StoreSecurity, TriagePlan

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def scan(store, verdict, at, at_risk="0.00"):
    return ScanResult(store=store, scanned_at=at, orders_listed=2, orders_checked=2,
                      leaking_orders=1 if verdict == "leaking" else 0, money_at_risk=at_risk, currency="USD", items=[],
                      plan=TriagePlan(summary="s", steps=[], source="rules", notes=[]),
                      security=StoreSecurity(checked=True), verdict=verdict)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"TESTPILOT_ALERTS_DIR": folder.name,
                                      "TESTPILOT_RECOVERY_LOG_PATH": os.path.join(folder.name, "recoveries.json")})
        env.start()
        self.addCleanup(env.stop)
        timeline._disputes_cache.update(at=None, items=None)
        self.addCleanup(timeline._disputes_cache.update, at=None, items=None)

    def build(self, disputes=(), error=None):
        async def fake_toolkit(method, params):
            if error:
                raise error
            if method == "list_disputes":
                return {"items": [{"dispute_id": d["dispute_id"]} for d in disputes]}
            return next(d for d in disputes if d["dispute_id"] == params["dispute_id"])

        with patch("app.paypal_toolkit.run", fake_toolkit):
            return asyncio.run(timeline.build("local", now=NOW))

    def test_rows_are_the_stores_and_paypal_disputes(self):
        view = self.build()
        self.assertEqual([r.id for r in view.resources], ["main", "vulnerable", "risky", "disputes"])
        self.assertEqual((view.start, view.end), (NOW - timedelta(days=3), NOW + timedelta(days=10)))

    def test_scans_are_coloured_by_result_and_old_ones_are_left_out(self):
        timeline.record_scan(scan("vulnerable", "leaking", NOW - timedelta(hours=2), "18.00"))
        timeline.record_scan(scan("risky", "vulnerable", NOW - timedelta(hours=1)))
        timeline.record_scan(scan("main", "clear", NOW - timedelta(days=9)))
        events = self.build().events
        self.assertEqual([(e.resource_id, e.tone, e.detail) for e in events],
                         [("vulnerable", "red", "18.00 USD at risk"), ("risky", "orange", "0 security issue(s)")])

    def test_a_dispute_runs_to_its_deadline_and_turns_red_when_close(self):
        due_soon = {"dispute_id": "PP-D-1", "create_time": "2026-11-01T10:00:00Z", "status": "WAITING_FOR_SELLER_RESPONSE",
                    "seller_response_due_date": "2026-11-05T10:00:00Z", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    "dispute_amount": {"value": "93.97", "currency_code": "USD"}}
        later = {**due_soon, "dispute_id": "PP-D-2", "seller_response_due_date": "2026-11-12T10:00:00Z"}
        closed = {**due_soon, "dispute_id": "PP-D-3", "status": "RESOLVED"}
        events = self.build(disputes=[due_soon, later, closed]).events
        self.assertEqual([(e.id, e.tone) for e in events], [("dispute-PP-D-1", "red"), ("dispute-PP-D-2", "orange")])
        self.assertEqual(events[0].end, datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc))
        self.assertIn("respond by 05 Nov", events[0].name)

    def test_paypal_refusing_disputes_leaves_a_note_not_an_error(self):
        view = self.build(error=paypal_toolkit.ToolkitError("refused", 403))
        self.assertEqual(view.events, [])
        self.assertIn("could not be read", view.note)


if __name__ == "__main__":
    unittest.main()
