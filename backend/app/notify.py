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

from app.schemas import Investigation, RecoveryRecord

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
    return {
        "event": "leak_found", "order_id": result.order_id, "store": result.store,
        "money_at_risk": result.money_at_risk, "currency": result.currency, "leaks": leaks,
        "summary": f"PayRecover found {len(leaks)} leak(s) on order {result.order_id}: "
                   f"{result.money_at_risk} {result.currency} at risk.",
        "checked_at": result.checked_at.isoformat(),
    }


def recovery_event(record: RecoveryRecord) -> dict[str, Any]:
    return {
        "event": "recovery_done", "recovery_id": record.recovery_id, "order_id": record.order_id, "store": record.store,
        "kind": record.kind, "action": record.action, "amount": record.amount, "currency": record.currency,
        "status": record.status, "message": record.message, "created_at": record.created_at.isoformat(),
    }


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
