"""Ask PayRecover: a Gemini agent that answers a merchant's questions with PayPal tools.

Gemini plans the steps and calls tools on its own:
  * PayPal's Agent Toolkit, read-only tools only (orders, disputes, transactions, invoices);
  * PayRecover's own checks (scan, investigate, disputes with evidence, unrecorded payments).
It can never move money. To recover a leak or answer a dispute it can only propose the step;
the proposal reaches the merchant as an Approve button, which runs the same approved,
verified path as the dashboard. Every tool call is returned, so the merchant sees what was read.
"""

import json
from typing import Any

from app import ai, disputes, investigate, paypal, paypal_toolkit, recovery, scan, unrecorded
from app.schemas import (
    AgentProposal,
    AgentReply,
    AgentRequest,
    AgentToolCall,
    InvestigationRequest,
    LEAK_KINDS,
    ScanRequest,
)

MAX_STEPS = 6
MAX_RESULT_CHARS = 6000
ORDER_ID = {"type": "string", "pattern": "^[A-Z0-9]{8,40}$", "description": "A PayPal order id."}
SYSTEM = (
    "You are PayRecover, an assistant for a merchant who sells through PayPal (Sandbox). You find money the "
    "store is losing on PayPal and help get it back. Use the tools for every fact: never guess amounts, ids, "
    "or statuses. You cannot move money. To collect, refund, or invoice, call propose_recovery; to answer a "
    "dispute, call propose_dispute_answer. Both only show the merchant an Approve button; say so, and never "
    "claim money has moved. Tools marked [PayPal Agent Toolkit] read PayPal directly. Answer in short, plain "
    "sentences with the amounts and order ids that matter, and say what the merchant should approve next. "
    "Write plain text without Markdown."
)

PAYRECOVER_TOOLS = [
    {"name": "scan_store", "description": "Check the store's recent paid orders against PayPal and list every "
     "leak (never captured, fake paid, wrong amount, duplicate charge) with amounts and urgency.",
     "parametersJsonSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "How many recent orders."}}}},
    {"name": "investigate_order", "description": "Every check for one order: the store's claim next to "
     "PayPal's record, and any leak.",
     "parametersJsonSchema": {"type": "object", "properties": {"order_id": ORDER_ID}, "required": ["order_id"]}},
    {"name": "list_recoveries", "description": "Money PayRecover has already recovered, with totals.",
     "parametersJsonSchema": {"type": "object", "properties": {}}},
    {"name": "list_dispute_cases", "description": "Open PayPal disputes, each linked to its store order, with "
     "PayRecover's evidence and a recommended answer.",
     "parametersJsonSchema": {"type": "object", "properties": {}}},
    {"name": "find_unrecorded_payments", "description": "Payments PayPal completed for this store that the "
     "store never recorded (the buyer was charged, the store does not know).",
     "parametersJsonSchema": {"type": "object", "properties": {}}},
    {"name": "propose_recovery", "description": "Propose recovering one leak (PayRecover picks capture, refund "
     "or invoice). Shows the merchant an Approve button; nothing runs until they press it.",
     "parametersJsonSchema": {"type": "object", "properties": {
         "order_id": ORDER_ID, "leak_kind": {"type": "string", "enum": list(LEAK_KINDS)}},
         "required": ["order_id", "leak_kind"]}},
    {"name": "propose_dispute_answer", "description": "Propose an answer to a PayPal dispute: send evidence, "
     "or accept the claim (refunds the buyer). Shows the merchant an Approve button.",
     "parametersJsonSchema": {"type": "object", "properties": {
         "dispute_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,255}$"},
         "action": {"type": "string", "enum": ["provide_evidence", "accept_claim"]},
         "note": {"type": "string", "description": "The message to PayPal, factual, 10 to 2000 characters."}},
         "required": ["dispute_id", "action", "note"]}},
]


class ToolRefused(ValueError):
    """The tool call was not allowed or its arguments were wrong."""


def _compact(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _leak_view(leaks) -> list[dict[str, Any]]:
    return [{"kind": leak.kind, "title": leak.title, "amount": leak.amount, "currency": leak.currency,
             "urgency": leak.urgency, "recovery": leak.recovery_action if leak.targets else "hold_shipment",
             "why": leak.root_cause} for leak in leaks]


class Session:
    """One assistant reply: runs tools for one store and collects proposals."""

    def __init__(self, store: str, owner: str):
        self.store = store
        self.owner = owner
        self.proposals: list[AgentProposal] = []
        self.calls: list[AgentToolCall] = []

    async def _investigate(self, order_id: str):
        return await investigate.investigate(InvestigationRequest(
            order_id=order_id, store=self.store, probe_store=False, website_checks=False, xss_probe=False,
            explain=False,
        ))

    async def payrecover(self, name: str, args: dict[str, Any]) -> Any:
        if name == "scan_store":
            result = await scan.scan_store(ScanRequest(store=self.store, limit=int(args.get("limit") or 25),
                                                       explain=False, check_paypal_side=False))
            return {"orders_checked": result.orders_checked, "leaking_orders": result.leaking_orders,
                    "money_at_risk": f"{result.money_at_risk} {result.currency}",
                    "leaks": [{"order_id": item.order_id, "leaks": _leak_view(item.leaks)}
                              for item in result.items if item.leaks]}
        if name == "investigate_order":
            found = await self._investigate(str(args["order_id"]))
            return {"order_id": found.order_id, "verdict": found.verdict, "store_claim": found.store_claim,
                    "paypal": found.paypal_order, "leaks": _leak_view(found.leaks),
                    "failed_checks": [c.message for c in found.checks if c.status in {"failed", "error"}]}
        if name == "list_recoveries":
            listed = recovery.list_recoveries(self.owner)
            return {"totals": listed["totals"], "latest": [
                {"order_id": r.order_id, "action": r.action, "amount": r.amount, "status": r.status}
                for r in listed["records"][:5]]}
        if name == "list_dispute_cases":
            cases = await disputes.list_cases()
            return {"note": cases.note, "disputes": [
                {"dispute_id": c.dispute_id, "reason": c.reason, "status": c.status, "amount": c.amount,
                 "currency": c.currency, "order_id": c.order_id, "store": c.store, "facts": c.facts,
                 "allowed_actions": c.allowed_actions, "advice": _compact(c.advice)} for c in cases.disputes]}
        if name == "find_unrecorded_payments":
            return _compact(await unrecorded.find(self.store))
        if name == "propose_recovery":
            found = await self._investigate(str(args["order_id"]))
            leak = next((item for item in found.leaks if item.kind == args.get("leak_kind")), None)
            if leak is None:
                raise ToolRefused(f"Order {found.order_id} has no {args.get('leak_kind')} leak to recover.")
            if not leak.targets:
                raise ToolRefused("PayPal has nothing to act on for this leak (the buyer never approved); "
                                  "advise holding the shipment instead.")
            self.proposals.append(AgentProposal(
                kind="recovery", title=f"{leak.recovery_action.title()} {leak.amount} {leak.currency}: {leak.title}",
                order_id=found.order_id, leak_kind=leak.kind, amount=leak.amount, currency=leak.currency,
                store=self.store,
            ))
            return {"proposed": True, "executed": False, "note": "Shown to the merchant with an Approve button."}
        if name == "propose_dispute_answer":
            note = str(args.get("note") or "").strip()
            if not 10 <= len(note) <= 2000:
                raise ToolRefused("The note must be 10 to 2000 characters.")
            case = await disputes.get_case(str(args["dispute_id"]))
            if args.get("action") not in case.allowed_actions:
                raise ToolRefused(f"PayPal does not offer {args.get('action')} for this dispute now.")
            self.proposals.append(AgentProposal(
                kind="dispute", title=f"{str(args['action']).replace('_', ' ').capitalize()} on dispute "
                f"{case.dispute_id} ({case.amount} {case.currency})", dispute_id=case.dispute_id,
                dispute_action=args["action"], note=note, amount=case.amount, currency=case.currency,
                store=case.store or self.store,
            ))
            return {"proposed": True, "executed": False, "note": "Shown to the merchant with an Approve button."}
        raise ToolRefused(f"Unknown tool {name}.")

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call; errors go back to the model as results, so it can recover or explain."""
        toolkit = name in paypal_toolkit.READ_ONLY_TOOLS
        try:
            result = await paypal_toolkit.run(name, args) if toolkit else await self.payrecover(name, args)
            ok, text = True, json.dumps(_compact(result), default=str)[:MAX_RESULT_CHARS]
        except (ToolRefused, paypal_toolkit.ToolkitError, paypal_toolkit.ToolkitUnavailable,
                investigate.StoreUnavailable, paypal.PayPalProviderError, paypal.PayPalOrderNotFound,
                KeyError, ValueError) as exc:
            ok, text = False, f"Error: {exc}"
        self.calls.append(AgentToolCall(
            name=name, source="paypal_agent_toolkit" if toolkit else "payrecover", arguments=args, ok=ok,
            summary=text[:200],
        ))
        return {"result": text} if ok else {"error": text}


def tool_declarations() -> list[dict[str, Any]]:
    declared = list(PAYRECOVER_TOOLS)
    if paypal_toolkit.available():
        declared += paypal_toolkit.declarations()
    return declared


async def answer(request: AgentRequest, owner: str) -> AgentReply:
    """Let Gemini plan and call tools until it can answer, within MAX_STEPS rounds."""
    investigate._active_store.set(request.store)
    session = Session(request.store, owner)
    contents = [{"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.text}]}
                for m in request.messages]
    system = f"{SYSTEM} The merchant is looking at the {request.store} store."
    tools = tool_declarations()
    for _ in range(MAX_STEPS):
        parts = await ai.generate_turn(contents, tools, system)
        contents.append({"role": "model", "parts": parts})
        calls = [part["functionCall"] for part in parts if isinstance(part.get("functionCall"), dict)]
        if not calls:
            text = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
            return AgentReply(text=text or "I could not find an answer.", tool_calls=session.calls,
                              proposals=session.proposals, source=ai.answer_source())
        responses = []
        for call in calls:
            response = {"name": call.get("name"), "response": await session.run(str(call.get("name")),
                                                                                 call.get("args") or {})}
            if call.get("id"):
                response["id"] = call["id"]
            responses.append({"functionResponse": response})
        contents.append({"role": "user", "parts": responses})
    return AgentReply(text="That took more steps than allowed. Try a narrower question.",
                      tool_calls=session.calls, proposals=session.proposals, source=ai.answer_source())
