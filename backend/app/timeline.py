"""The timeline: what happened to each store, and what is due, for the dashboard's Bryntum Scheduler.

Rows are the three demo stores plus PayPal disputes. Blocks are:
  * each scan, coloured by its result (red money leaking, orange vulnerable, green all clear);
  * each recovery (captured, invoiced or refunded), from the recovery log;
  * each open dispute, from the day it opened to PayPal's response deadline, so a deadline
    about to pass is plain to see. Disputes come from PayPal's Agent Toolkit, cached a minute.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app import alerts, paypal, paypal_toolkit, recovery
from app.schemas import STORE_KEYS, ScanResult, Timeline, TimelineEvent, TimelineResource

HISTORY_FILE = "scan-history.json"
MAX_HISTORY = 300
SCAN_BLOCK = timedelta(minutes=20)
RECOVERY_BLOCK = timedelta(minutes=20)
DISPUTES_CACHE_SECONDS = 60
MAX_DISPUTES = 10
STORE_NAMES = {"main": "Main store", "vulnerable": "Vulnerable store", "risky": "Risky store"}
VERDICT_LOOK = {"leaking": ("Money leaking", "red"), "vulnerable": ("Vulnerable", "orange"), "clear": ("All clear", "green")}
# "at" is None until the first fetch: monotonic time starts near boot, so 0 can look like "just fetched".
_disputes_cache: dict[str, Any] = {"at": None, "items": None}


def record_scan(result: ScanResult) -> None:
    """Add a scan to the history the timeline shows (newest last, the oldest dropped past MAX_HISTORY)."""
    data = alerts._load(HISTORY_FILE)
    entries = list(data.get("scans") or [])
    entries.append({"store": result.store, "verdict": result.verdict, "at": result.scanned_at.isoformat(),
                    "money_at_risk": result.money_at_risk, "currency": result.currency,
                    "security_issues": len(result.security.findings) if result.security else 0,
                    "orders_checked": result.orders_checked})
    data["scans"] = entries[-MAX_HISTORY:]
    alerts._save(HISTORY_FILE, data)


def _scan_detail(entry: dict[str, Any]) -> str:
    if entry.get("verdict") == "leaking":
        return f"{entry.get('money_at_risk')} {entry.get('currency')} at risk"
    if entry.get("verdict") == "vulnerable":
        return f"{entry.get('security_issues')} security issue(s)"
    return f"{entry.get('orders_checked')} paid order(s) checked"


def _when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _disputes() -> list[dict[str, Any]] | None:
    """Open disputes with their deadlines, or None when PayPal could not be asked."""
    if _disputes_cache["at"] is not None and time.monotonic() - _disputes_cache["at"] < DISPUTES_CACHE_SECONDS:
        return _disputes_cache["items"]
    try:
        listed = await paypal_toolkit.run("list_disputes", {"page_size": MAX_DISPUTES})
        items = [await paypal_toolkit.run("get_dispute", {"dispute_id": item["dispute_id"]})
                 for item in (listed.get("items") or [])[:MAX_DISPUTES]]
    except (paypal_toolkit.ToolkitError, paypal_toolkit.ToolkitUnavailable, paypal.PayPalConfigurationError):
        items = None
    _disputes_cache.update(at=time.monotonic(), items=items)
    return items


async def build(owner: str, days_back: int = 3, days_ahead: int = 10, now: datetime | None = None) -> Timeline:
    current = now or datetime.now(timezone.utc)
    start, end = current - timedelta(days=days_back), current + timedelta(days=days_ahead)
    resources = [TimelineResource(id=key, name=STORE_NAMES.get(key, key)) for key in STORE_KEYS]
    resources.append(TimelineResource(id="disputes", name="PayPal disputes"))
    events: list[TimelineEvent] = []

    for number, entry in enumerate(alerts._load(HISTORY_FILE).get("scans") or []):
        at = _when(entry.get("at"))
        if at is None or at < start or entry.get("store") not in STORE_KEYS:
            continue
        label, tone = VERDICT_LOOK.get(entry.get("verdict"), ("Scanned", "grey"))
        events.append(TimelineEvent(id=f"scan-{number}", resource_id=entry["store"], name=label, kind="scan",
                                    tone=tone, start=at, end=at + SCAN_BLOCK, detail=_scan_detail(entry)))

    for record in recovery.list_recoveries(owner)["records"]:
        if record.created_at < start or record.store not in STORE_KEYS:
            continue
        done = record.status in recovery.DONE_STATUSES
        events.append(TimelineEvent(
            id=f"recovery-{record.recovery_id}", resource_id=record.store, kind="recovery",
            name=f"{record.action.capitalize()} {record.amount} {record.currency}", tone="blue" if done else "grey",
            start=record.created_at, end=record.created_at + RECOVERY_BLOCK, detail=record.message,
        ))

    disputes = await _disputes()
    for dispute in disputes or []:
        opened = _when(dispute.get("create_time"))
        due = _when(dispute.get("seller_response_due_date")) or (opened + timedelta(days=10) if opened else None)
        if opened is None or dispute.get("status") == "RESOLVED":
            continue
        amount = dispute.get("dispute_amount") or {}
        overdue_soon = due is not None and due - current < timedelta(days=3)
        events.append(TimelineEvent(
            id=f"dispute-{dispute['dispute_id']}", resource_id="disputes", kind="dispute",
            name=f"Dispute {amount.get('value')} {amount.get('currency_code')} · respond by {due:%d %b}",
            tone="red" if overdue_soon else "orange", start=opened, end=max(due, opened + timedelta(hours=1)),
            detail=f"{dispute.get('reason', '').replace('_', ' ').lower()} · status {dispute.get('status')}",
        ))
    note = None if disputes is not None else "PayPal disputes could not be read right now."
    return Timeline(resources=resources, events=events, start=start, end=end, note=note)
