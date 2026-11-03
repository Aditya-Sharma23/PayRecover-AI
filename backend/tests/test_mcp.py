"""PayRecover's MCP server: the JSON-RPC handshake, the tool list, tool calls, and the Origin guard."""

import isolation
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import agent, mcp_server
from app.main import app


def rpc(method, params=None, message_id=1):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def setUpModule():
    isolation.isolate_alerts()


def tearDownModule():
    isolation.restore_alerts()


class McpTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def post(self, body, **headers):
        return self.client.post("/api/mcp", json=body, headers=headers)

    def test_initialize_agrees_a_protocol_version_and_offers_tools(self):
        result = self.post(rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}})).json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "payrecover")

    def test_an_unknown_protocol_version_gets_the_newest_one(self):
        result = self.post(rpc("initialize", {"protocolVersion": "1999-01-01"})).json()["result"]
        self.assertEqual(result["protocolVersion"], mcp_server.PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_reply(self):
        response = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((response.status_code, response.content), (202, b""))

    def test_tools_are_the_assistants_own_without_the_dispute_answer(self):
        tools = {tool["name"]: tool for tool in self.post(rpc("tools/list")).json()["result"]["tools"]}
        self.assertIn("scan_store", tools)
        self.assertIn("propose_recovery", tools)
        self.assertIn("run_demo_scenario", tools)
        self.assertNotIn("propose_dispute_answer", tools)
        self.assertEqual(tools["investigate_order"]["inputSchema"]["required"], ["order_id"])

    def test_a_demo_scenario_runs_without_paypal(self):
        result = self.post(rpc("tools/call", {"name": "run_demo_scenario",
                                              "arguments": {"scenario": "hold_expiring"}})).json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["leaks"][0]["kind"], "hold_expiring")

    def test_tool_calls_go_through_the_assistants_tools(self):
        async def fake(self, name, args):
            return {"store": args.get("store"), "leaking_orders": 0}

        with patch.object(agent.Session, "payrecover", fake):
            result = self.post(rpc("tools/call", {"name": "scan_store",
                                                  "arguments": {"store": "risky"}})).json()["result"]
        self.assertFalse(result["isError"])
        self.assertIn('"store": "risky"', result["content"][0]["text"])

    def test_a_proposal_never_moves_money_and_says_where_to_approve(self):
        async def fake(self, name, args):
            self.proposals.append(agent.AgentProposal(kind="recovery", title="Capture 18.00 USD", order_id="5O190127TN364715T",
                                                      leak_kind="never_captured", amount="18.00", currency="USD",
                                                      store="main"))
            return {"proposed": True}

        with patch.object(agent.Session, "payrecover", fake), patch.dict(os.environ, {"TESTPILOT_APPROVAL_SECRET": ""}):
            result = self.post(rpc("tools/call", {"name": "propose_recovery", "arguments": {
                "order_id": "5O190127TN364715T", "leak_kind": "never_captured"}})).json()["result"]
        self.assertFalse(result["structuredContent"]["executed"])
        self.assertIn("dashboard", result["structuredContent"]["approve"])

    def test_unknown_tools_and_methods_are_protocol_errors(self):
        self.assertEqual(self.post(rpc("tools/call", {"name": "move_money", "arguments": {}})).json()["error"]["code"],
                         -32602)
        self.assertEqual(self.post(rpc("resources/list")).json()["error"]["code"], -32601)
        self.assertEqual(self.post({"id": 3}).json()["error"]["code"], -32600)

    def test_a_web_page_from_another_site_is_refused(self):
        response = self.post(rpc("tools/list"), origin="https://evil.example")
        self.assertEqual(response.status_code, 403)

    def test_there_is_no_event_stream(self):
        self.assertEqual(self.client.get("/api/mcp").status_code, 405)


if __name__ == "__main__":
    unittest.main()
