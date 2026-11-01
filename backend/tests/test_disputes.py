"""Dispute defender: disputes link to their order and store, the AI only advises, and answers need approval."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError

from app import ai, disputes
from app.schemas import DisputeResponseRequest, Investigation, Leak, RecoveryTarget

DISPUTE_ID = "PP-D-12345"
CAPTURE_ID = "CAP2DUPLICATE0001"
ORDER_ID = "5O190127TN364715T"


def dispute(status="WAITING_FOR_SELLER_RESPONSE", actions=("provide_evidence", "accept_claim")):
    return {
        "dispute_id": DISPUTE_ID, "reason": "DUPLICATE_TRANSACTION", "status": status,
        "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"value": "18.00", "currency_code": "USD"},
        "seller_response_due_date": "2026-11-20T00:00:00Z",
        "disputed_transactions": [{"seller_transaction_id": CAPTURE_ID}],
        "links": [{"rel": "self"}] + [{"rel": action} for action in actions],
    }


def investigation(claim=None, leaks=()):
    return Investigation(
        order_id=ORDER_ID, verdict="leaking" if leaks else "healthy", store="vulnerable",
        checked_at=datetime.now(timezone.utc), store_url="http://127.0.0.1:8200", store_claim=claim,
        checks=[], leaks=list(leaks), money_at_risk="0.00", currency="USD",
    )


DUPLICATE = Leak(kind="duplicate_charge", title="Charged twice", amount="18.00", currency="USD", root_cause="r",
                 recovery_action="refund", recovery="Refund", targets=[RecoveryTarget(order_id=ORDER_ID,
                                                                                       capture_id=CAPTURE_ID,
                                                                                       amount="18.00")])


class FakePayPal:
    def __init__(self, after_status="UNDER_REVIEW"):
        self.toolkit_calls = []
        self.after_status = after_status
        self.answered = False

    async def toolkit(self, method, params):
        self.toolkit_calls.append((method, params))
        if method == "list_disputes":
            return {"items": [{"dispute_id": DISPUTE_ID}]}
        if method == "get_dispute":
            return dispute(self.after_status, ()) if self.answered else dispute()
        if method == "accept_dispute_claim":
            self.answered = True
            return {}
        raise AssertionError(method)

    async def call(self, method, path, body=None, request_id=None):
        if path == f"/v2/payments/captures/{CAPTURE_ID}":
            return 200, {"status": "COMPLETED", "custom_id": "payrecover-vulnerable",
                         "supplementary_data": {"related_ids": {"order_id": ORDER_ID}}}
        raise AssertionError(path)

    async def multipart(self, path, payload):
        self.answered = True
        self.evidence = (path, payload)
        return 200, {}


class DisputeTests(unittest.TestCase):
    def run_with(self, coroutine, paypal, found=None, advice=None):
        async def fake_investigate(request, *_):
            self.investigated = request
            return found or investigation({"status": "PAID", "total": "18.00", "currency": "USD"})

        async def fake_ask(_prompt):
            if advice is None:
                raise ai.AIProviderError("no model in tests")
            return advice

        with (
            patch("app.paypal_toolkit.run", paypal.toolkit),
            patch("app.paypal.paypal_call", paypal.call),
            patch("app.paypal.paypal_multipart", paypal.multipart),
            patch("app.investigate.investigate", fake_investigate),
            patch("app.ai.ask_json", fake_ask),
        ):
            return asyncio.run(coroutine)

    def test_a_dispute_is_linked_to_its_order_and_store_through_the_capture(self):
        listed = self.run_with(disputes.list_cases(), FakePayPal())
        case = listed.disputes[0]
        self.assertEqual((case.order_id, case.store, case.capture_id), (ORDER_ID, "vulnerable", CAPTURE_ID))
        self.assertEqual(self.investigated.store, "vulnerable")
        self.assertEqual(case.allowed_actions, ["provide_evidence", "accept_claim"])

    def test_rules_accept_a_dispute_about_a_real_duplicate_charge(self):
        case = self.run_with(disputes.get_case(DISPUTE_ID), FakePayPal(),
                             investigation({"status": "PAID"}, [DUPLICATE]))
        self.assertEqual(case.advice.recommendation, "accept_claim")
        self.assertEqual(case.advice.source, "PayRecover rules")

    def test_the_ai_recommendation_is_used_only_when_paypal_offers_that_action(self):
        advice = {"recommendation": "provide_evidence", "reasoning": ["Matches"], "note": "The order was paid."}
        case = self.run_with(disputes.get_case(DISPUTE_ID), FakePayPal(), advice=advice)
        self.assertEqual(case.advice.recommendation, "provide_evidence")
        bad = {"recommendation": "make_offer", "reasoning": [], "note": "Offer 5 USD"}
        case = self.run_with(disputes.get_case(DISPUTE_ID), FakePayPal(), advice=bad)
        self.assertEqual(case.advice.source, "PayRecover rules")

    def test_answering_needs_approval(self):
        with self.assertRaises(ValidationError):
            DisputeResponseRequest(action="accept_claim", note="We agree with the buyer.", approved=False)

    def test_accepting_a_claim_uses_the_toolkit_and_is_confirmed_by_a_fresh_read(self):
        paypal = FakePayPal(after_status="RESOLVED")
        request = DisputeResponseRequest(action="accept_claim", note="We agree with the buyer.", approved=True)
        result = self.run_with(disputes.respond(DISPUTE_ID, request), paypal)
        self.assertEqual((result.status, result.dispute_status_after), ("sent", "RESOLVED"))
        self.assertIn(("accept_dispute_claim", {"dispute_id": DISPUTE_ID, "note": "We agree with the buyer."}),
                      paypal.toolkit_calls)

    def test_evidence_is_sent_as_paypal_multipart_input(self):
        paypal = FakePayPal()
        request = DisputeResponseRequest(action="provide_evidence", note="Order paid and shipped.", approved=True)
        result = self.run_with(disputes.respond(DISPUTE_ID, request), paypal)
        self.assertEqual(result.status, "sent")
        path, payload = paypal.evidence
        self.assertEqual(path, f"/v1/customer/disputes/{DISPUTE_ID}/provide-evidence")
        self.assertEqual(payload["evidences"][0], {"evidence_type": "OTHER", "notes": "Order paid and shipped."})

    def test_an_action_paypal_no_longer_offers_is_refused(self):
        paypal = FakePayPal()
        paypal.answered = True  # already answered: PayPal offers no actions
        request = DisputeResponseRequest(action="accept_claim", note="We agree with the buyer.", approved=True)
        with self.assertRaises(disputes.DisputeNotActionable):
            self.run_with(disputes.respond(DISPUTE_ID, request), paypal)


if __name__ == "__main__":
    unittest.main()
