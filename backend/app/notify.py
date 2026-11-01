"""Zapier hand-off: tell the merchant about leaks and recoveries, with signed approval links.

PayRecover posts JSON events to the operator's Zapier "Catch Hook" URL
(TESTPILOT_ZAPIER_WEBHOOK_URL); a Zap then forwards them by email, Slack, SMS or into a
sheet. Leak events carry one approval link per leak. A link is an HMAC-signed token
(TESTPILOT_APPROVAL_SECRET) that names the order, store, leak and owner and expires after
a day. Opening it only shows a confirmation page: money moves when the merchant presses
the button, because mail scanners open links on their own.

Events never contain buyer details, and a failing webhook never fails an investigation.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import aiohttp

from app.schemas import DisputeCase, Investigation, RecoveryRecord, ScanResult, UnrecordedPayment

APPROVAL_TTL_SECONDS = 24 * 60 * 60
WEBHOOK_TIMEOUT_SECONDS = 8
LOCAL_BASE_URL = "http://127.0.0.1:8000"


class InvalidApproval(ValueError):
    """The approval token is malformed, tampered with, or expired."""


def _secret() -> bytes | None:
    secret = os.environ.get("TESTPILOT_APPROVAL_SECRET", "").strip()
    return secret.encode() if len(secret) >= 32 else None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def approval_token(order_id: str, store: str, kind: str, owner: str, now: float | None = None) -> str | None:
    """A signed, expiring approval for one leak, or None when no approval secret is configured."""
    secret = _secret()
    if secret is None:
        return None
    payload = _b64(json.dumps({"o": order_id, "s": store, "k": kind, "w": owner,
                               "e": int((now or time.time()) + APPROVAL_TTL_SECONDS)}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def read_approval(token: str, now: float | None = None) -> dict[str, str]:
    """Check the signature and expiry; return order_id, store, kind and owner."""
    secret = _secret()
    if secret is None:
        raise InvalidApproval("Approval links are switched off on this server.")
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise InvalidApproval("This approval link is not valid.")
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidApproval("This approval link is not valid.") from exc
    if int(data.get("e", 0)) < (now or time.time()):
        raise InvalidApproval("This approval link has expired. Run the investigation again for a new one.")
    return {"order_id": data["o"], "store": data["s"], "kind": data["k"], "owner": data["w"]}


def approval_url(token: str) -> str:
    base = os.environ.get("TESTPILOT_PUBLIC_URL", "").strip().rstrip("/") or LOCAL_BASE_URL
    return f"{base}/approve?token={token}"


def _webhook_url() -> str | None:
    url = os.environ.get("TESTPILOT_ZAPIER_WEBHOOK_URL", "").strip()
    return url if url.startswith("https://") else None


def leak_event(result: Investigation, owner: str) -> dict[str, Any]:
    leaks = []
    for leak in result.leaks:
        token = approval_token(result.order_id, result.store, leak.kind, owner)
        leaks.append({
            "kind": leak.kind, "title": leak.title, "amount": leak.amount, "currency": leak.currency,
            "action": leak.recovery_action, "recovery": leak.recovery, "root_cause": leak.root_cause,
            "approve_url": approval_url(token) if token else None,
        })
    summary = (f"PayRecover found {len(leaks)} leak(s) on order {result.order_id}: "
               f"{result.money_at_risk} {result.currency} at risk.")
    return {
        "event": "leak_found", "order_id": result.order_id, "store": result.store,
        "money_at_risk": result.money_at_risk, "currency": result.currency, "leaks": leaks,
        "summary": summary,
        "message": _leak_message(summary, leaks),
        "checked_at": result.checked_at.isoformat(),
    }


def _leak_message(summary: str, leaks: list[dict[str, Any]]) -> str:
    """A ready-to-send plain-text message, so a Zap needs only this one field in its email or chat step."""
    lines = [summary, ""]
    for number, leak in enumerate(leaks, 1):
        lines += [
            f"{number}. {leak['title']} ({leak['amount']} {leak['currency']})",
            f"   Why: {leak['root_cause']}",
            f"   Fix: {leak['recovery']}",
            f"   Approve: {leak['approve_url']}" if leak["approve_url"] else "   Approve it in the PayRecover dashboard.",
            "",
        ]
    lines.append("Nothing moves until you approve. PayRecover then checks PayPal again to confirm it worked.")
    return "\n".join(lines)


def recovery_event(record: RecoveryRecord) -> dict[str, Any]:
    return {
        "event": "recovery_done", "recovery_id": record.recovery_id, "order_id": record.order_id, "store": record.store,
        "kind": record.kind, "action": record.action, "amount": record.amount, "currency": record.currency,
        "status": record.status, "message": record.message, "created_at": record.created_at.isoformat(),
        "summary": f"PayRecover {record.status.replace('_', ' ')}: {record.amount} {record.currency} "
                   f"({record.action}) for order {record.order_id}. {record.message}",
    }


def scan_event(result: ScanResult) -> dict[str, Any]:
    """A store scan's plan, ready to send: what to recover first, in order."""
    lines = [result.plan.summary, ""]
    for number, step in enumerate(result.plan.steps, 1):
        lines.append(f"{number}. Order {step.order_id}: {step.kind.replace('_', ' ')}, {step.amount} {result.currency} "
                     f"({step.action}, {step.urgency}) - {step.why}")
    lines += ["", "Approve each recovery in the PayRecover dashboard."]
    summary = (f"PayRecover scanned {result.orders_checked} paid order(s) in the {result.store} store: "
               f"{result.leaking_orders} leaking, {result.money_at_risk} {result.currency} at risk.")
    return {"event": "scan_done", "store": result.store, "summary": summary, "message": "\n".join(lines),
            "money_at_risk": result.money_at_risk, "currency": result.currency,
            "leaking_orders": result.leaking_orders, "scanned_at": result.scanned_at.isoformat()}



def dispute_event(case: DisputeCase) -> dict[str, Any]:
    """A new PayPal dispute, with PayRecover's evidence and the AI's recommended answer."""
    advice = case.advice
    lines = [f"A buyer opened PayPal dispute {case.dispute_id} for {case.amount} {case.currency} "
             f"(respond by {case.respond_by or 'the date PayPal shows'}).", ""]
    lines += [f"- {fact}" for fact in case.facts]
    if advice:
        lines += ["", f"Recommended: {advice.recommendation.replace('_', ' ')} ({advice.source})."]
        lines += [f"- {reason}" for reason in advice.reasoning]
    lines += ["", "Review the draft answer and approve it in the PayRecover dashboard (Disputes)."]
    return {"event": "dispute_opened", "dispute_id": case.dispute_id, "order_id": case.order_id, "store": case.store,
            "amount": case.amount, "currency": case.currency,
            "recommendation": advice.recommendation if advice else None,
            "summary": f"PayPal dispute {case.dispute_id}: {case.amount} {case.currency} on order {case.order_id}.",
            "message": "\n".join(lines)}


def unrecorded_event(payment: UnrecordedPayment) -> dict[str, Any]:
    """PayPal took a payment for the store, but the store has no paid order for it."""
    summary = (f"PayPal took {payment.amount} {payment.currency} (payment {payment.transaction_id}) for the "
               f"{payment.store} store, but the store never recorded the order.")
    message = (f"{summary}\n\nThe buyer was charged and will not receive anything unless you act. Ship the order "
               "or refund the buyer before they open a dispute.")
    return {"event": "unrecorded_payment", "transaction_id": payment.transaction_id, "store": payment.store,
            "amount": payment.amount, "currency": payment.currency, "summary": summary, "message": message}

async def send(event: dict[str, Any]) -> bool:
    """Post one event to the Zapier webhook. Returns whether Zapier accepted it; never raises."""
    url = _webhook_url()
    if url is None:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(url, json=event) as response:
                return response.status < 300
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


def status() -> dict[str, bool]:
    return {"webhook_configured": _webhook_url() is not None, "approval_links": _secret() is not None}
