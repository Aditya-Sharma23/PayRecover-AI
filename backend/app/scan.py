"""Store scan: every recent paid order checked against PayPal, leaks ranked, Gemini writes the plan.

The ranking is deterministic: urgency first ("now" before "soon" before "normal"), then the
amount at risk, then whoever has waited longest. Gemini only words the plan for that fixed
order; it cannot add, drop or reorder leaks, and a built-in plan is used when it is unavailable.

A cart charged twice makes each of its orders name the other as the duplicate. Recovering
both would refund everything, so the scan keeps one duplicate-charge leak per cart: the one
found on the cart's earliest order, which refunds the later charges.
"""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app import ai, investigate
from app.schemas import InvestigationRequest, ScanItem, ScanRequest, ScanResult, TriagePlan, TriageStep

MAX_CONCURRENT = 4
URGENCY_RANK = {"now": 0, "soon": 1, "normal": 2}
URGENCY_WHY = {
    "now": "the buyer approved this payment and it is still uncollected; capture it before the approval lapses",
    "soon": "act before the order ships or the buyer opens a chargeback",
    "normal": "money is missing, but nothing gets worse by waiting a little",
}


async def recent_orders(limit: int) -> list[dict[str, Any]]:
    """The store's newest orders, read from the operator-configured store only."""
    async with investigate._session() as session:
        status, listed = await investigate._store_json(session, "GET", f"/api/recent-orders?limit={limit}")
    if status == 404 or not isinstance(listed, list):
        raise investigate.StoreUnavailable("This store does not list its recent orders (GET /api/recent-orders).")
    return [order for order in listed if isinstance(order, dict) and order.get("order_id")]


def _priority(item: ScanItem) -> tuple:
    leak = item.leaks[0]
    waited = leak.waiting_since or datetime.max.replace(tzinfo=timezone.utc)
    return URGENCY_RANK[leak.urgency], -Decimal(item.money_at_risk), waited


def _keep_one_duplicate_per_cart(items: list[ScanItem]) -> None:
    seen: set[str] = set()
    for item in sorted(items, key=lambda i: i.created_at or ""):
        if not item.cart_id:
            continue
        if item.cart_id in seen:
            item.leaks = [leak for leak in item.leaks if leak.kind != "duplicate_charge"]
        elif any(leak.kind == "duplicate_charge" for leak in item.leaks):
            seen.add(item.cart_id)


def builtin_plan(steps: list[TriageStep], money: str, currency: str, reason: str | None = None) -> TriagePlan:
    if not steps:
        return TriagePlan(summary="No leaks found: every paid order matches PayPal's record.", steps=[],
                          source="Built-in summary", notes=[reason] if reason else [])
    first = steps[0]
    return TriagePlan(
        summary=(f"{len(steps)} leak(s), {money} {currency} at risk. Start with order {first.order_id} "
                 f"({first.amount} {currency}): {first.why}."),
        steps=steps, source="Built-in summary", notes=[reason] if reason else [],
    )


async def triage(steps: list[TriageStep], money: str, currency: str) -> TriagePlan:
    """Gemini words the plan for the fixed ranking; anything that changes the ranking is rejected."""
    if not steps:
        return builtin_plan(steps, money, currency)
    facts = [step.model_dump(exclude={"why"}) for step in steps]
    prompt = f"""Return only JSON matching this schema:
{{"summary": "string", "why": ["string"]}}
You are helping a small online merchant recover money. These leaks are already ranked; keep this exact order.
Write "summary": two short, plain sentences a shop owner would say out loud: what is wrong, the total at risk
({money} {currency}), and what to approve first. No jargon, no talk about ranking or "leaks ranked first".
Write "why": exactly {len(steps)} short reasons (under 20 words each), one per leak in the same order, saying what
happens if the merchant waits, e.g. "The buyer's approval can lapse, and then this money is gone."
Never explain the order itself, and never say a leak is first because it is the only one.
Use only these facts. Never add, drop or reorder leaks, and never change an amount. Recoveries need the merchant's approval.
Leaks: {json.dumps(facts, default=str)}"""
    try:
        value = ai.fit_to_limits(ai._extract_json(await ai._generate(prompt)), (), ("summary",))
        reasons = value.get("why")
        if not isinstance(reasons, list) or len(reasons) != len(steps) or not str(value.get("summary", "")).strip():
            raise ai.AIProviderError("The AI plan did not keep one reason per leak.")
    except (ai.AIConfigurationError, ai.AIProviderError) as exc:
        return builtin_plan(steps, money, currency, str(exc)[:300])
    worded = [step.model_copy(update={"why": str(reason).strip()[:400] or step.why}) for step, reason in zip(steps, reasons, strict=True)]
    return TriagePlan(summary=str(value["summary"]).strip(), steps=worded, source=ai.answer_source(), notes=[])


async def scan_store(request: ScanRequest) -> ScanResult:
    """Check every recent order the store calls PAID, then rank the leaks and plan the recovery."""
    investigate._active_store.set(request.store)
    orders = await recent_orders(request.limit)
    paid = [order for order in orders if order.get("status") == "PAID"]
    gate = asyncio.Semaphore(MAX_CONCURRENT)

    async def check(order: dict[str, Any]) -> ScanItem:
        async with gate:
            result = await investigate.investigate(InvestigationRequest(
                order_id=order["order_id"], store=request.store,
                probe_store=False, website_checks=False, xss_probe=False, explain=False,
            ))
        return ScanItem(
            order_id=result.order_id, cart_id=order.get("cart_id"), created_at=order.get("created_at"),
            store_status=order.get("status"), total=order.get("total"), verdict=result.verdict,
            leaks=result.leaks, money_at_risk=result.money_at_risk,
        )

    items = list(await asyncio.gather(*(check(order) for order in paid)))
    _keep_one_duplicate_per_cart(items)
    for item in items:
        item.leaks.sort(key=lambda leak: (URGENCY_RANK[leak.urgency], -Decimal(leak.amount)))
        item.money_at_risk = investigate._money(sum((Decimal(leak.amount) for leak in item.leaks), Decimal(0)))
        if not item.leaks and item.verdict == "leaking":
            item.verdict = "healthy"
    leaking = sorted((item for item in items if item.leaks), key=_priority)
    currency = next((leak.currency for item in leaking for leak in item.leaks), "USD")
    money = investigate._money(sum((Decimal(item.money_at_risk) for item in leaking), Decimal(0)))
    steps = [
        TriageStep(order_id=item.order_id, kind=leak.kind, amount=leak.amount, urgency=leak.urgency,
                   # No target means PayPal has nothing to act on (the buyer never approved): hold the order instead.
                   action=leak.recovery_action if leak.targets else "hold_shipment",
                   waiting_since=leak.waiting_since, why=URGENCY_WHY[leak.urgency])
        for item in leaking for leak in item.leaks
    ]
    plan = await triage(steps, money, currency) if request.explain else builtin_plan(steps, money, currency)
    return ScanResult(
        store=request.store, scanned_at=datetime.now(timezone.utc), orders_listed=len(orders),
        orders_checked=len(paid), leaking_orders=len(leaking), money_at_risk=money, currency=currency,
        items=leaking + [item for item in items if not item.leaks], plan=plan,
    )
