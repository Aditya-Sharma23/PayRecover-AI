"""Ask PayRecover: Gemini calls tools in a loop, toolkit tools stay read-only, and money moves only as proposals."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import agent, paypal_toolkit
from app.schemas import AgentRequest, Investigation, Leak, RecoveryTarget

ORDER_ID = "5O190127TN364715T"
LEAK = Leak(kind="never_captured", title="Never captured", amount="68.97", currency="USD", root_cause="r",
            recovery_action="capture", recovery="Capture", targets=[RecoveryTarget(order_id=ORDER_ID, amount="68.97")])


def investigation(leaks=(LEAK,)):
    return Investigation(order_id=ORDER_ID, verdict="leaking", checked_at=datetime.now(timezone.utc),
                         store_url="http://127.0.0.1:8100", checks=[], leaks=list(leaks), money_at_risk="68.97",
                         currency="USD")


def call(name, args, call_id=None):
    part = {"functionCall": {"name": name, "args": args}}
    if call_id:
        part["functionCall"]["id"] = call_id
    return part


class AgentTests(unittest.TestCase):
    def converse(self, turns, toolkit=None):
        """turns: the model's parts for each step, in order."""
        self.seen = []
        script = list(turns)

        async def fake_turn(contents, tools, system):
            self.seen.append((list(contents), tools))
            return script.pop(0)

        async def fake_toolkit(method, params):
            self.toolkit_calls.append((method, params))
            return toolkit or {"items": []}

        async def fake_investigate(_request, *_):
            return investigation()

        self.toolkit_calls = []
        request = AgentRequest(messages=[{"role": "user", "text": "What am I losing?"}])
        with (
            patch("app.ai.generate_turn", fake_turn),
            patch("app.paypal_toolkit.run", fake_toolkit),
            patch("app.investigate.investigate", fake_investigate),
            patch("app.ai.answer_source", lambda: "Gemini (test)"),
        ):
            return asyncio.run(agent.answer(request, "local"))

    def test_toolkit_tools_are_called_and_results_go_back_to_the_model(self):
        reply = self.converse([[call("list_disputes", {}, "c1")], [{"text": "No disputes."}]])
        self.assertEqual(reply.text, "No disputes.")
        self.assertEqual(self.toolkit_calls, [("list_disputes", {})])
        self.assertEqual(reply.tool_calls[0].source, "paypal_agent_toolkit")
        returned = self.seen[1][0][-1]["parts"][0]["functionResponse"]
        self.assertEqual((returned["name"], returned["id"]), ("list_disputes", "c1"))

    def test_only_read_only_toolkit_tools_are_offered(self):
        self.converse([[{"text": "Hi"}]])
        offered = {tool["name"] for tool in self.seen[0][1]}
        self.assertTrue(set(paypal_toolkit.READ_ONLY_TOOLS) <= offered)
        for money_tool in ("pay_order", "accept_dispute_claim", "send_invoice", "create_order", "create_invoice"):
            self.assertNotIn(money_tool, offered)

    def test_a_money_moving_tool_the_model_invents_is_refused(self):
        reply = self.converse([[call("pay_order", {"order_id": ORDER_ID})], [{"text": "I cannot."}]])
        self.assertFalse(reply.tool_calls[0].ok)
        self.assertEqual(self.toolkit_calls, [])

    def test_a_recovery_is_only_proposed_never_run(self):
        reply = self.converse([[call("propose_recovery", {"order_id": ORDER_ID, "leak_kind": "never_captured"})],
                               [{"text": "Approve the capture."}]])
        self.assertEqual(len(reply.proposals), 1)
        proposal = reply.proposals[0]
        self.assertEqual((proposal.kind, proposal.order_id, proposal.amount), ("recovery", ORDER_ID, "68.97"))
        self.assertIn('"executed": false', reply.tool_calls[0].summary)

    def test_a_proposal_for_a_leak_that_does_not_exist_is_refused(self):
        reply = self.converse([[call("propose_recovery", {"order_id": ORDER_ID, "leak_kind": "wrong_amount"})],
                               [{"text": "No such leak."}]])
        self.assertEqual(reply.proposals, [])
        self.assertFalse(reply.tool_calls[0].ok)

    def test_the_loop_stops_after_the_step_limit(self):
        reply = self.converse([[call("list_disputes", {})]] * agent.MAX_STEPS)
        self.assertIn("more steps", reply.text)


if __name__ == "__main__":
    unittest.main()
