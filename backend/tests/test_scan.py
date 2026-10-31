"""Store scan: only paid orders are checked, leaks are ranked, duplicates are kept once per cart."""

import asyncio
import json
import unittest
from unittest.mock import patch

from app import ai, scan
from app.schemas import Investigation, Leak, RecoveryTarget, ScanRequest


def leak(kind, amount, urgency, waiting=None, target_order="X"):
    action = {"never_captured": "capture", "duplicate_charge": "refund"}.get(kind, "invoice")
    return Leak(kind=kind, title=kind, amount=amount, currency="USD", root_cause="cause", recovery_action=action,
                recovery="fix", urgency=urgency, waiting_since=waiting,
                targets=[RecoveryTarget(order_id=target_order, amount=amount)])


ORDERS = [  # newest first, as the store lists them
    {"order_id": "ORDERDDD0004", "status": "PAID", "cart_id": "cart-b", "created_at": "2026-10-31T04:00:00", "total": "18.00"},
    {"order_id": "ORDERCCC0003", "status": "PENDING", "cart_id": "cart-x", "created_at": "2026-10-31T03:00:00", "total": "4.99"},
    {"order_id": "ORDERBBB0002", "status": "PAID", "cart_id": "cart-b", "created_at": "2026-10-31T02:00:00", "total": "18.00"},
    {"order_id": "ORDERAAA0001", "status": "PAID", "cart_id": "cart-a", "created_at": "2026-10-31T01:00:00", "total": "12.50"},
    {"order_id": "ORDEREEE0005", "status": "PAID", "cart_id": "cart-e", "created_at": "2026-10-31T00:30:00", "total": "18.00"},
]
LEAKS = {
    # cart-b was charged twice: each of its orders names the other as the duplicate.
    "ORDERDDD0004": [leak("duplicate_charge", "18.00", "soon", target_order="ORDERBBB0002")],
    "ORDERBBB0002": [leak("duplicate_charge", "18.00", "soon", target_order="ORDERDDD0004")],
    "ORDERAAA0001": [leak("never_captured", "12.50", "now", "2026-10-31T01:05:00Z")],
    "ORDEREEE0005": [],
}


def run_scan(explain=False, generate=None):
    checked = []

    async def fake_recent(limit):
        return ORDERS[:limit]

    async def fake_investigate(request):
        checked.append(request.order_id)
        found = LEAKS[request.order_id]
        return Investigation(order_id=request.order_id, verdict="leaking" if found else "healthy",
                             checked_at="2026-10-31T05:00:00Z", store_url="http://127.0.0.1:8200", checks=[],
                             leaks=[item.model_copy() for item in found],
                             money_at_risk=found[0].amount if found else "0.00", currency="USD")

    patches = [patch("app.scan.recent_orders", fake_recent), patch("app.investigate.investigate", fake_investigate)]
    if generate:
        patches.append(patch("app.ai._generate", generate))
    for p in patches:
        p.start()
    try:
        return asyncio.run(scan.scan_store(ScanRequest(store="vulnerable", explain=explain))), checked
    finally:
        for p in patches:
            p.stop()


class ScanTests(unittest.TestCase):
    def test_only_paid_orders_are_checked(self):
        result, checked = run_scan()
        self.assertNotIn("ORDERCCC0003", checked)
        self.assertEqual((result.orders_listed, result.orders_checked), (5, 4))

    def test_a_double_charged_cart_yields_one_refund_of_the_later_charge(self):
        result, _ = run_scan()
        duplicates = [item for item in result.items for found in item.leaks if found.kind == "duplicate_charge"]
        self.assertEqual([item.order_id for item in duplicates], ["ORDERBBB0002"])
        self.assertEqual(duplicates[0].leaks[0].targets[0].order_id, "ORDERDDD0004")
        self.assertEqual(result.money_at_risk, "30.50")
        self.assertEqual(result.leaking_orders, 2)

    def test_urgent_uncaptured_money_comes_first(self):
        result, _ = run_scan()
        self.assertEqual([step.order_id for step in result.plan.steps], ["ORDERAAA0001", "ORDERBBB0002"])
        self.assertIn("ORDERAAA0001", result.plan.summary)
        self.assertEqual(result.items[-1].verdict, "healthy")

    def test_gemini_words_the_plan_but_keeps_the_order(self):
        async def generate(_prompt):
            return json.dumps({"summary": "Capture first, then refund.", "why": ["approval is fresh", "refund the double"]})

        with patch.dict("os.environ", {"TESTPILOT_GOOGLE_API_KEY": "k", "TESTPILOT_AI_MODEL": "gemini-test"}):
            result, _ = run_scan(explain=True, generate=generate)
        self.assertEqual(result.plan.summary, "Capture first, then refund.")
        self.assertEqual([s.why for s in result.plan.steps], ["approval is fresh", "refund the double"])
        self.assertEqual([s.order_id for s in result.plan.steps], ["ORDERAAA0001", "ORDERBBB0002"])

    def test_a_plan_that_drops_a_leak_is_replaced_by_the_built_in_one(self):
        async def generate(_prompt):
            return json.dumps({"summary": "Only one thing matters.", "why": ["just this"]})

        result, _ = run_scan(explain=True, generate=generate)
        self.assertEqual(result.plan.source, "Built-in summary")
        self.assertEqual(len(result.plan.steps), 2)

    def test_gemini_failing_still_gives_a_plan(self):
        async def generate(_prompt):
            raise ai.AIProviderError("busy")

        result, _ = run_scan(explain=True, generate=generate)
        self.assertEqual(result.plan.source, "Built-in summary")
        self.assertIn("2 leak(s), 30.50 USD at risk", result.plan.summary)


if __name__ == "__main__":
    unittest.main()
