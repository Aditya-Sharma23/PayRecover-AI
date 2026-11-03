"""Demo scenarios: every result on made-up data, through the real rules, with no store or PayPal calls."""

import isolation
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import demo, paypal
from app.main import app

EXPECTED = {
    "never_captured": ("leaking", ["never_captured"]),
    "fake_paid": ("leaking", ["fake_paid"]),
    "wrong_amount": ("leaking", ["wrong_amount"]),
    "duplicate_charge": ("leaking", ["duplicate_charge"]),
    "hold_expiring": ("leaking", ["hold_expiring"]),
    "waiting": ("waiting", []),
    "healthy": ("healthy", []),
}


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class DemoTests(unittest.TestCase):
    def test_each_scenario_gives_its_result_and_is_marked_sample_data(self):
        self.assertEqual(set(demo.SCENARIOS), set(EXPECTED))
        for name, (verdict, kinds) in EXPECTED.items():
            with self.subTest(name):
                found = demo.investigation(name)
                self.assertEqual((found.verdict, [leak.kind for leak in found.leaks]), (verdict, kinds))
                self.assertTrue(found.sample_data)
                self.assertEqual(found.store, "demo")

    def test_scenarios_never_call_paypal(self):
        async def refuse(*_args, **_kwargs):
            raise AssertionError("a demo scenario called PayPal")

        with patch.object(paypal, "fetch_order", refuse), patch.object(paypal, "_sdk", refuse):
            for name in demo.SCENARIOS:
                demo.investigation(name)

    def test_the_sample_dispute_recommends_accepting_a_duplicate_charge(self):
        case = demo.dispute()
        self.assertEqual((case.advice.recommendation, case.advice.source), ("accept_claim", "PayRecover rules"))
        self.assertTrue(any("charged" in fact for fact in case.facts))

    def test_http_lists_runs_and_rejects_unknown_scenarios(self):
        client = TestClient(app)
        listed = client.get("/api/demo/scenarios").json()
        self.assertIn("hold_expiring", [item["id"] for item in listed])
        found = client.get("/api/demo/scenarios/duplicate_charge").json()
        self.assertEqual((found["verdict"], found["sample_data"]), ("leaking", True))
        self.assertEqual(client.get("/api/demo/scenarios/nothing_here").status_code, 404)
        self.assertEqual(client.get("/api/demo/dispute").json()["dispute_id"], "PP-D-DEMO-0001")


if __name__ == "__main__":
    unittest.main()
