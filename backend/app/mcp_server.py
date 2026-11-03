"""PayRecover as an MCP server: any AI assistant can ask whether a store is losing money, and propose fixes.

The Model Context Protocol (MCP) lets assistants such as Claude, ChatGPT or an agent framework call
tools on a server. PayRecover serves it at POST /api/mcp with the Streamable HTTP transport, in its
simplest form: each JSON-RPC request gets one JSON reply (no server-sent events, no session state).
It sits under /api, so the same API token, rate limit and audit log apply as for the dashboard.

The tools are the ones the dashboard's own assistant uses (app.agent), so an outside assistant reads
exactly what PayRecover's checks decide. It can never move money: propose_recovery only returns what
would happen and where the merchant approves it (a signed approval link when one is configured).
"""

import json
from typing import Any

from app import agent, demo, notify, paypal_setup
from app.schemas import STORE_KEYS

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "payrecover", "title": "PayRecover AI", "version": "1.0.0"}
INSTRUCTIONS = (
    "PayRecover checks a PayPal merchant's orders against PayPal's own record and finds money the store lost. "
    "Use scan_store first, then investigate_order for detail. Every amount and status comes from PayRecover's "
    "checks; never guess them. You cannot move money: propose_recovery returns where the merchant approves. "
    "All payments are in the PayPal Sandbox. run_demo_scenario shows each kind of leak on made-up data."
)
# The dashboard assistant's tools that make sense outside the dashboard. propose_dispute_answer is left
# out: its approval is a button in the dashboard chat, which an outside assistant cannot show.
SHARED_TOOLS = ("scan_store", "investigate_order", "find_unrecorded_payments", "list_dispute_cases",
                "list_recoveries", "check_market_prices", "propose_recovery")
EXTRA_TOOLS = [
    {"name": "check_paypal_setup", "description": "Which PayPal Sandbox app features PayRecover can use (Transaction "
     "search, Disputes, Invoicing, webhooks), and how to switch on any that are off.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_demo_scenario", "description": "Run one made-up order through PayRecover's real rules, to see what "
     "a leak looks like without a store or PayPal. Scenarios: " + ", ".join(demo.SCENARIOS) + ".",
     "inputSchema": {"type": "object", "properties": {"scenario": {"type": "string", "enum": list(demo.SCENARIOS)}},
                     "required": ["scenario"]}},
]
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def tools() -> list[dict[str, Any]]:
    """MCP tool definitions, from the dashboard assistant's own declarations."""
    declared = []
    for tool in agent.PAYRECOVER_TOOLS:
        if tool["name"] in SHARED_TOOLS:
            declared.append({"name": tool["name"], "description": tool["description"],
                             "inputSchema": tool["parametersJsonSchema"], "annotations": READ_ONLY})
    return declared + [{**tool, "annotations": READ_ONLY} for tool in EXTRA_TOOLS]


def _text(value: Any, is_error: bool = False) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(value, dict) and not is_error:
        result["structuredContent"] = value
    return result


async def call_tool(name: str, arguments: dict[str, Any], owner: str) -> dict[str, Any]:
    if name == "check_paypal_setup":
        return _text((await paypal_setup.check()).model_dump(mode="json"))
    if name == "run_demo_scenario":
        scenario = arguments.get("scenario")
        if scenario not in demo.SCENARIOS:
            return _text(f"Error: unknown scenario {scenario!r}; use one of {', '.join(demo.SCENARIOS)}.", True)
        found = demo.investigation(scenario)
        return _text({"sample_data": True, "verdict": found.verdict, "money_at_risk": f"{found.money_at_risk} USD",
                      "leaks": agent._leak_view(found.leaks), "waiting_reason": found.waiting_reason})
    if name not in SHARED_TOOLS:
        raise RpcError(-32602, f"Unknown tool: {name}")
    store = arguments.get("store") or "main"
    if store not in STORE_KEYS:
        return _text(f"Error: unknown store {store!r}; use one of {', '.join(STORE_KEYS)}.", True)
    session = agent.Session(store, owner)
    try:
        reply = await session.run(name, arguments)
    except Exception as exc:  # e.g. PayPal keys missing: the assistant gets the reason, not a protocol error
        return _text(f"Error: {exc}", True)
    if "error" in reply:
        return _text(reply["error"], True)
    if name == "propose_recovery" and session.proposals:
        proposal = session.proposals[-1]
        token = notify.approval_token(proposal.order_id, proposal.store, proposal.leak_kind, owner)
        return _text({"proposed": proposal.title, "executed": False, "amount": f"{proposal.amount} {proposal.currency}",
                      "approve": notify.approval_url(token) if token else
                      "Open the PayRecover dashboard and press Approve on this leak.",
                      "note": "Nothing has moved. The merchant must approve; PayRecover then acts once and "
                              "confirms the result with a fresh read of PayPal."})
    return {"content": [{"type": "text", "text": reply["result"]}], "isError": False}


async def _dispatch(method: str, params: dict[str, Any], owner: str) -> dict[str, Any]:
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools()}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise RpcError(-32602, "tools/call needs a tool name and an arguments object.")
        return await call_tool(name, arguments, owner)
    raise RpcError(-32601, f"Method not found: {method}")


async def handle(message: Any, owner: str) -> dict[str, Any] | None:
    """One JSON-RPC message in, its reply out (None for a notification, which gets no reply)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32600, "message": "Invalid JSON-RPC request."}}
    if "id" not in message:
        return None
    params = message.get("params") or {}
    try:
        if not isinstance(params, dict):
            raise RpcError(-32602, "params must be an object.")
        result = await _dispatch(message["method"], params, owner)
    except RpcError as exc:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": exc.code, "message": exc.message}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}
