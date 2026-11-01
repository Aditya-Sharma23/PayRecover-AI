"""Alerts: tell the merchant about leaks, recoveries and disputes, in three layers.

  1. The alert feed on the dashboard: always on, no setup.
  2. Email, sent by PayRecover itself through Brevo to the address the merchant saves once
     (TESTPILOT_BREVO_API_KEY and a verified TESTPILOT_ALERT_SENDER on the server).
  3. Zapier, optional: the merchant's own Catch Hook (Slack, SMS, sheets, 9,000+ apps), or the
     operator's TESTPILOT_ZAPIER_WEBHOOK_URL when they have not saved one.

Each owner's settings and feed are kept apart. Delivery never raises: a failing channel is
recorded on the alert and never fails the scan, recovery or webhook that raised it. A saved
Zapier address must be a hooks.zapier.com URL, so a user cannot make the server call other hosts.
"""

import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from app import notify
from app.schemas import AlertItem, AlertSettings, AlertsView

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
EMAIL_TIMEOUT_SECONDS = 10
MAX_ALERTS_PER_OWNER = 50
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
ZAPIER_PATTERN = re.compile(r"^https://hooks\.zapier\.com/hooks/catch/[A-Za-z0-9/_-]{1,200}$")
# Test alerts can send email to any saved address, so each owner gets only a few per hour.
TESTS_PER_HOUR = 5
_tests: dict[str, list[float]] = {}


class InvalidAlertSettings(ValueError):
    """The email or Zapier address is not acceptable."""


class TooManyTests(RuntimeError):
    """The owner sent too many test alerts in the last hour."""


def _folder() -> Path:
    configured = os.environ.get("TESTPILOT_ALERTS_DIR")
    if configured:
        return Path(configured)
    database = os.environ.get("TESTPILOT_DB_PATH")
    return Path(database).parent if database else Path(__file__).resolve().parents[1] / "data"


def _load(name: str) -> dict[str, Any]:
    try:
        data = json.loads((_folder() / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(name: str, data: dict[str, Any]) -> None:
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False, suffix=".tmp") as handle:
        json.dump(data, handle, indent=2, default=str)
    os.replace(handle.name, folder / name)


def _sender() -> str | None:
    sender = os.environ.get("TESTPILOT_ALERT_SENDER", "").strip()
    return sender if EMAIL_PATTERN.match(sender) else None


def _brevo_key() -> str | None:
    return os.environ.get("TESTPILOT_BREVO_API_KEY", "").strip().strip("\"'") or None


def email_available() -> bool:
    return _brevo_key() is not None and _sender() is not None


def settings(owner: str) -> AlertSettings:
    saved = _load("alert-settings.json").get(owner) or {}
    return AlertSettings(email=saved.get("email"), zapier_url=saved.get("zapier_url"))


def save_settings(owner: str, new: AlertSettings) -> AlertSettings:
    email = (new.email or "").strip() or None
    zapier = (new.zapier_url or "").strip() or None
    if email and not EMAIL_PATTERN.match(email):
        raise InvalidAlertSettings("That email address does not look right.")
    if zapier and not ZAPIER_PATTERN.match(zapier):
        raise InvalidAlertSettings("Paste a Zapier Catch Hook address (https://hooks.zapier.com/hooks/catch/...).")
    data = _load("alert-settings.json")
    data[owner] = {"email": email, "zapier_url": zapier}
    _save("alert-settings.json", data)
    return AlertSettings(email=email, zapier_url=zapier)


def _masked(email: str) -> str:
    name, domain = email.split("@", 1)
    return f"{name[:1]}***@{domain}"


async def send_email(to: str, subject: str, text: str) -> bool:
    """One plain-text email through Brevo. Returns whether Brevo accepted it; never raises."""
    key, sender = _brevo_key(), _sender()
    if key is None or sender is None:
        return False
    body = {"sender": {"name": "PayRecover AI", "email": sender}, "to": [{"email": to}],
            "subject": subject[:200], "textContent": text}
    try:
        timeout = aiohttp.ClientTimeout(total=EMAIL_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(BREVO_URL, json=body, headers={"api-key": key, "accept": "application/json"}) as reply:
                return reply.status < 300
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


def _record(owner: str, item: AlertItem) -> None:
    data = _load("alerts.json")
    items = [item.model_dump(mode="json")] + list(data.get(owner) or [])
    data[owner] = items[:MAX_ALERTS_PER_OWNER]
    _save("alerts.json", data)


async def deliver(event: dict[str, Any], owner: str) -> AlertItem:
    """Show the alert on the dashboard, then send it by email and to Zapier when set up."""
    chosen = settings(owner)
    channels: dict[str, str] = {}
    if chosen.email:
        if not email_available():
            channels["email"] = "not set up on the server"
        else:
            sent = await send_email(chosen.email, event.get("summary") or "PayRecover alert",
                                    event.get("message") or event.get("summary") or "")
            channels["email"] = f"sent to {_masked(chosen.email)}" if sent else "failed"
    zapier = chosen.zapier_url or notify._webhook_url()
    if zapier:
        channels["zapier"] = "sent" if await notify.send(event, zapier) else "failed"
    item = AlertItem(
        alert_id=str(uuid.uuid4()), event=str(event.get("event") or "alert"),
        summary=str(event.get("summary") or ""), message=str(event.get("message") or ""),
        order_id=event.get("order_id"), dispute_id=event.get("dispute_id"), store=event.get("store"),
        created_at=datetime.now(timezone.utc), channels=channels,
    )
    _record(owner, item)
    return item


async def deliver_to_store_owners(event: dict[str, Any], fallback_owner: str) -> list[AlertItem]:
    """A store event (from a PayPal webhook) goes to everyone who set up alerts, and always to the operator."""
    owners = set(_load("alert-settings.json")) | {fallback_owner}
    return [await deliver(event, owner) for owner in sorted(owners)]


def view(owner: str) -> AlertsView:
    items = [AlertItem.model_validate(item) for item in _load("alerts.json").get(owner) or []]
    return AlertsView(settings=settings(owner), alerts=items, email_available=email_available(),
                      zapier_default=notify._webhook_url() is not None)


def clear(owner: str) -> int:
    data = _load("alerts.json")
    removed = len(data.pop(owner, None) or [])
    _save("alerts.json", data)
    return removed


def allow_test(owner: str, now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    recent = [stamp for stamp in _tests.get(owner, []) if current - stamp < 3600]
    if len(recent) >= TESTS_PER_HOUR:
        raise TooManyTests(f"Only {TESTS_PER_HOUR} test alerts an hour. Try again later.")
    _tests[owner] = recent + [current]


def test_event() -> dict[str, Any]:
    return {"event": "test", "summary": "PayRecover test alert: your alerts are working.",
            "message": "This is a test from PayRecover AI. Real alerts tell you when a store loses money on "
                       "PayPal, with a link to approve the recovery."}
