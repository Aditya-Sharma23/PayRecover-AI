"""Evidence pack: a fresh, escaped, fingerprinted page with the order's recoveries and no buyer details."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import evidence
from app.main import app
from app.schemas import Investigation, Leak, PaymentCheck, RecoveryTarget

ORDER_ID = "5O190127TN364715T"


def investigation(note="<script>x</script>"):
    leak = Leak(kind="never_captured", title="Never captured", amount="18.00", currency="USD", root_cause=note,
                recovery_action="capture", recovery="Capture it.", urgency="now",
                targets=[RecoveryTarget(order_id=ORDER_ID, amount="18.00")])
    return Investigation(
        order_id=ORDER_ID, verdict="leaking", store="vulnerable", checked_at="2026-10-31T00:00:00Z",
        store_url="http://127.0.0.1:8200", store_claim={"status": "PAID", "total": "18.00", "currency": "USD"},
        paypal_order={"status": "APPROVED", "amount": "18.00", "capture_status": None},
        checks=[PaymentCheck(name="capture_completed", status="failed", message="No completed capture.")],
        leaks=[leak], money_at_risk="18.00", currency="USD",
    )


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.log = Path(folder.name) / "recoveries.json"
        env = patch.dict(os.environ, {"TESTPILOT_RECOVERY_LOG_PATH": str(self.log)})
        env.start()
        self.addCleanup(env.stop)

    def build(self, found=None):
        async def fake_investigate(_request):
            return found or investigation()

        with patch("app.investigate.investigate", fake_investigate):
            return asyncio.run(evidence.build(ORDER_ID, "vulnerable", "local"))

    def test_the_pack_shows_both_sides_checks_and_leaks_escaped(self):
        page, digest = self.build()
        self.assertIn("Order 5O190127TN364715T", page)
        self.assertIn("The store's claim", page)
        self.assertIn("No completed capture.", page)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)
        self.assertNotIn("<script>x</script>", page)
        self.assertIn(digest, page)
        self.assertEqual(len(digest), 64)

    def test_the_fingerprint_changes_when_the_evidence_changes(self):
        _, first = self.build()
        _, again = self.build()
        _, other = self.build(investigation(note="a different cause"))
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_recoveries_for_the_order_are_included(self):
        self.log.write_text("""[{"recovery_id": "r1", "order_id": "5O190127TN364715T", "store": "vulnerable",
          "kind": "never_captured", "action": "capture", "amount": "18.00", "currency": "USD", "status": "recovered",
          "message": "Collected 18.00 USD.", "paypal": {"capture_id": "CAP9"}, "verification": [],
          "created_at": "2026-10-31T01:00:00Z", "owner": "local"}]""", encoding="utf-8")
        page, _ = self.build()
        self.assertIn("Recovered: capture 18.00 USD", page)
        self.assertIn("CAP9", page)

    def test_the_endpoint_serves_html_with_the_fingerprint_header(self):
        async def fake_investigate(_request):
            return investigation()

        with patch("app.investigate.investigate", fake_investigate):
            response = TestClient(app).get(f"/api/orders/{ORDER_ID}/evidence", params={"store": "vulnerable"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.headers["x-evidence-sha256"]), 64)
        self.assertEqual(TestClient(app).get("/api/orders/bad-id/evidence").status_code, 422)


if __name__ == "__main__":
    unittest.main()
