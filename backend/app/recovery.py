"""PayRecover's recovery actions: capture, refund or invoice one leak, after the merchant approves.

Rules that keep money movements safe:
  * Nothing runs without an explicit approval (RecoveryRequest.approved must be true).
  * The leak, amount and target are recomputed from the store and PayPal at approval time;
    the request only names the order and the kind of leak, never an amount or an id to act on.
  * Every call goes to the fixed PayPal Sandbox address with a PayPal-Request-Id, so a retry
    never charges or refunds twice, and a recovery already done is returned, not repeated.
  * Success is decided by a fresh read of PayPal after the action, not by the action's reply.
  * The buyer's email is used only to address an invoice; it is never stored or returned.
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import ai, investigate, paypal
from app.schemas import (
    CheckStatus,
    InvestigationRequest,
    Leak,
    PaymentCheck,
    RecoveryRecord,
    RecoveryRequest,
)

DONE_STATUSES = {"recovered", "invoice_sent", "refunded"}
INVOICING_HELP = (
    "PayPal refused to create the invoice (NOT_AUTHORIZED). Turn on Invoicing for your Sandbox app: "
    "developer.paypal.com > Apps & Credentials > your app > Features > Invoicing, then save."
)


class RecoveryNotPossible(RuntimeError):
    """The leak no longer exists, so there is nothing to recover."""


def _log_path() -> Path:
    configured = os.environ.get("TESTPILOT_RECOVERY_LOG_PATH")
    if configured:
        return Path(configured)
    database = os.environ.get("TESTPILOT_DB_PATH")
    folder = Path(database).parent if database else Path(__file__).resolve().parents[1] / "data"
    return folder / "recoveries.json"


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(_log_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save(records: list[dict[str, Any]]) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(records, handle, indent=2, default=str)
    os.replace(handle.name, path)


def list_recoveries(owner: str) -> dict[str, Any]:
    """The caller's recoveries, newest first, with totals by outcome."""
    records = [r for r in _load() if r.get("owner") == owner]
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    totals = {"collected": Decimal(0), "invoiced": Decimal(0), "refunded": Decimal(0)}
    for record in records:
        key = {"recovered": "collected", "invoice_sent": "invoiced", "refunded": "refunded"}.get(record["status"])
        if key:
            totals[key] += Decimal(record["amount"])
    return {
        "records": [RecoveryRecord.model_validate({k: v for k, v in r.items() if k != "owner"}) for r in records],
        "totals": {key: str(value.quantize(Decimal("0.01"))) for key, value in totals.items()},
    }


def clear_recoveries(owner: str) -> int:
    """Delete the caller's recovery records (only the log; nothing at PayPal changes). Returns how many."""
    records = _load()
    kept = [r for r in records if r.get("owner") != owner]
    _save(kept)
    return len(records) - len(kept)


def _check(name: str, ok: bool, message: str, evidence: dict | None = None) -> PaymentCheck:
    return PaymentCheck(
        name=name, status=CheckStatus.PASSED if ok else CheckStatus.FAILED, message=message, evidence=evidence or {}
    )


def _issue(body: Any, status: int) -> str:
    if isinstance(body, dict):
        details = body.get("details") or [{}]
        return str(details[0].get("issue") or body.get("name") or f"HTTP {status}")
    return f"HTTP {status}"


async def _capture(leak: Leak) -> tuple[str, str, dict, list[PaymentCheck]]:
    target = leak.targets[0]
    status, body = await paypal.capture_order(target.order_id, f"payrecover-capture-{target.order_id}")
    if status >= 400:
        return "failed", f"PayPal refused the capture ({_issue(body, status)}). An approval expires after a few hours.", {}, []
    order = await paypal.fetch_order(target.order_id)
    summary = paypal.order_summary(order)
    captured = summary["capture_status"] == "COMPLETED"
    amount_ok = summary["capture_amount"] is not None and Decimal(summary["capture_amount"]) == Decimal(target.amount)
    checks = [
        _check("capture_completed", captured, f"PayPal now shows capture {summary['capture_id']} as {summary['capture_status']}.",
               {"capture_id": summary["capture_id"], "capture_status": summary["capture_status"]}),
        _check("amount_collected", amount_ok, f"PayPal captured {summary['capture_amount']} {leak.currency}; "
               f"the leak was {target.amount} {leak.currency}.", {"captured": summary["capture_amount"], "expected": target.amount}),
    ]
    ok = captured and amount_ok
    return ("recovered" if ok else "unverified",
            f"Collected {target.amount} {leak.currency} from order {target.order_id}." if ok
            else "PayPal accepted the capture, but a fresh read does not confirm the full amount.",
            {"capture_id": summary["capture_id"]}, checks)


async def _refund(leak: Leak) -> tuple[str, str, dict, list[PaymentCheck]]:
    refunds, checks = [], []
    for target in leak.targets:
        status, body = await paypal.refund_capture(
            target.capture_id, target.amount, leak.currency,
            "Refund of a duplicate charge, found by PayRecover.", f"payrecover-refund-{target.capture_id}",
        )
        if status >= 400:
            checks.append(_check("refund_accepted", False, f"PayPal refused the refund of capture {target.capture_id} "
                                 f"({_issue(body, status)}).", {"capture_id": target.capture_id}))
            continue
        refund_id = (body or {}).get("id")
        _, fresh = await paypal.get_refund(refund_id)
        state = (fresh or {}).get("status")
        refunds.append({"capture_id": target.capture_id, "refund_id": refund_id, "status": state})
        checks.append(_check("refund_confirmed", state in {"COMPLETED", "PENDING"},
                             f"PayPal shows refund {refund_id} of {target.amount} {leak.currency} as {state}.",
                             {"refund_id": refund_id, "status": state, "capture_id": target.capture_id}))
    if not checks:
        return "failed", "This leak has no capture to refund.", {}, []
    ok = all(c.status == CheckStatus.PASSED for c in checks)
    return ("refunded" if ok else "failed" if not refunds else "unverified",
            f"Refunded {leak.amount} {leak.currency} to the buyer." if ok else "Not every refund could be confirmed.",
            {"refunds": refunds}, checks)


def _buyer_note_prompt(leak: Leak, target) -> str:
    return (
        "Write a short, polite note from a shop to a customer whose PayPal payment came up short, to go on an "
        f"invoice for the balance. Facts: order {target.order_id}, balance due {target.amount} {leak.currency}, "
        f"cause: {leak.root_cause} Do not blame the customer, promise discounts, or add facts. Under 400 characters. "
        'Return JSON only: {"note": "..."}'
    )


async def buyer_note(leak: Leak, target) -> str:
    """The invoice note: a friendly message written by the AI, then the facts line PayRecover always adds."""
    facts = f"Balance due for order {target.order_id}: {target.amount} {leak.currency}."
    try:
        text = str((await ai.ask_json(_buyer_note_prompt(leak, target))).get("note") or "").strip()[:600]
    except (ai.AIConfigurationError, ai.AIProviderError):
        text = ""
    return f"{text}\n\n{facts}" if text else facts


async def _invoice(leak: Leak) -> tuple[str, str, dict, list[PaymentCheck]]:
    target = leak.targets[0]
    try:
        order = await paypal.fetch_order(target.order_id)
    except paypal.PayPalOrderNotFound:
        order = {}
    # Used only to address the invoice; never stored or returned.
    email = ((order.get("payer") or {}).get("email_address") or "").strip()
    if not email:
        return ("failed", "PayPal has no buyer email for this order (the buyer never approved it), so it cannot be "
                "invoiced. Contact the buyer, or hold the shipment.", {}, [])
    invoice = {
        "detail": {"currency_code": leak.currency, "note": await buyer_note(leak, target),
                   "reference": target.order_id},
        "primary_recipients": [{"billing_info": {"email_address": email}}],
        "items": [{"name": f"Balance due for order {target.order_id}", "quantity": "1",
                   "unit_amount": {"currency_code": leak.currency, "value": target.amount}}],
    }
    status, body = await paypal.paypal_call("POST", "/v2/invoicing/invoices", invoice, f"payrecover-invoice-{target.order_id}")
    if status in {401, 403}:
        # A cached token keeps the permissions it was issued with; Invoicing switched on since then
        # only shows up in a fresh token, so ask for one once before giving up.
        paypal.forget_token()
        status, body = await paypal.paypal_call("POST", "/v2/invoicing/invoices", invoice,
                                                f"payrecover-invoice-{target.order_id}")
    if status in {401, 403}:
        return "failed", INVOICING_HELP, {}, []
    if status >= 400 or not isinstance(body, dict) or not body.get("href"):
        return "failed", f"PayPal could not create the invoice ({_issue(body, status)}).", {}, []
    href = body["href"]
    invoice_id = href.rstrip("/").rsplit("/", 1)[-1]
    status, body = await paypal.paypal_call("POST", f"{href}/send", {"send_to_invoicer": False},
                                            f"payrecover-invoice-send-{target.order_id}")
    if status >= 400:
        return "failed", f"The invoice was created but PayPal would not send it ({_issue(body, status)}).", \
            {"invoice_id": invoice_id}, []
    _, fresh = await paypal.paypal_call("GET", href)
    state = (fresh or {}).get("status")
    amount = (((fresh or {}).get("amount") or {}).get("value"))
    checks = [
        _check("invoice_sent", state in {"SENT", "UNPAID", "PAID", "PARTIALLY_PAID"},
               f"PayPal shows invoice {invoice_id} as {state}.", {"invoice_id": invoice_id, "status": state}),
        _check("invoice_amount", amount is not None and Decimal(amount) == Decimal(target.amount),
               f"The invoice asks for {amount} {leak.currency}; the leak was {target.amount} {leak.currency}.",
               {"invoiced": amount, "expected": target.amount}),
    ]
    ok = all(c.status == CheckStatus.PASSED for c in checks)
    return ("invoice_sent" if ok else "unverified",
            f"PayPal invoiced the buyer for {target.amount} {leak.currency}." if ok
            else "The invoice was sent, but a fresh read does not confirm it.", {"invoice_id": invoice_id}, checks)


ACTIONS = {"capture": _capture, "refund": _refund, "invoice": _invoice}


async def recover(request: RecoveryRequest, owner: str) -> RecoveryRecord:
    """Recompute the leak, act at PayPal once, verify with a fresh read, and record the outcome."""
    records = _load()
    previous = next((r for r in records if r.get("owner") == owner and r["order_id"] == request.order_id
                     and r["kind"] == request.kind and r["status"] in DONE_STATUSES), None)
    if previous is not None:
        return RecoveryRecord.model_validate({k: v for k, v in previous.items() if k != "owner"})

    found = await investigate.investigate(InvestigationRequest(
        order_id=request.order_id, store=request.store,
        probe_store=False, website_checks=False, xss_probe=False, explain=False,
    ))
    leak = next((item for item in found.leaks if item.kind == request.kind), None)
    if leak is None or not leak.targets:
        raise RecoveryNotPossible(f"Order {request.order_id} no longer has a {request.kind.replace('_', ' ')} leak to recover.")

    status, message, ids, checks = await ACTIONS[leak.recovery_action](leak)
    record = RecoveryRecord(
        recovery_id=str(uuid.uuid4()), order_id=request.order_id, store=request.store, kind=leak.kind,
        action=leak.recovery_action, amount=leak.amount, currency=leak.currency, status=status, message=message,
        paypal=ids, verification=checks, created_at=datetime.now(timezone.utc),
    )
    records.append({**record.model_dump(mode="json"), "owner": owner})
    _save(records)
    return record
