"""Auto-scan: PayRecover scans every store on a timer, in the background, while the server runs.

The merchant turns it on or off and picks how often (5, 10, 30 or 60 minutes) on the
dashboard; the choice is saved in autoscan.json, so it survives a restart. Each run is the
same full scan as the Scan button (payments, then the store-wide security check), without
the AI plan, so it costs no AI calls.

An auto-scan alerts only when a store's result changes (a new leak, a different amount at
risk, a new security issue), so the same red or orange alert is not sent every few minutes.
A manual scan always alerts.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app import alerts, investigate, notify, overview, paypal, scan, store_security, timeline, unrecorded
from app.schemas import STORE_KEYS, AutoscanSettings, AutoscanStatus, ScanRequest, ScanResult
from app.security import LOCAL_OWNER

SETTINGS_FILE = "autoscan.json"
TICK_SECONDS = 30
_lock = asyncio.Lock()


async def full_scan(request: ScanRequest) -> tuple[ScanResult, dict[str, Any] | None]:
    """Scan one store end to end and remember the result. Returns it with the store's previous result."""
    result = await scan.scan_store(request)
    if request.check_paypal_side:
        result.unrecorded = await unrecorded.find(request.store)
    if request.check_security:
        result.security = await store_security.check(request.store)
    result.verdict = store_security.verdict(bool(result.leaking_orders), result.security)
    previous = alerts._load("last-scans.json").get(request.store)
    overview.remember_scan(result)
    timeline.record_scan(result)
    return result, previous


def alert_for(result: ScanResult) -> dict[str, Any] | None:
    """Red when money is leaking, orange when it is not but the store has holes; nothing when all clear."""
    if result.verdict == "leaking":
        return notify.scan_event(result)
    if result.verdict == "vulnerable":
        return notify.vulnerable_event(result.store, result.security,
                                       f"PayRecover checked {result.orders_checked} paid order(s).")
    return None


def changed(previous: dict[str, Any] | None, result: ScanResult) -> bool:
    """Whether this result is news compared with the store's previous one."""
    if not previous:
        return True
    issues = len(result.security.findings) if result.security else 0
    return (previous.get("verdict") != result.verdict or previous.get("money_at_risk") != result.money_at_risk
            or int(previous.get("security_issues") or 0) != issues)


def _saved() -> dict[str, Any]:
    return alerts._load(SETTINGS_FILE) or {"enabled": False, "interval_minutes": 10}


def _when(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def status() -> AutoscanStatus:
    saved = _saved()
    return AutoscanStatus(
        enabled=bool(saved.get("enabled")), interval_minutes=int(saved.get("interval_minutes") or 10),
        running=_lock.locked(), last_run=_when(saved.get("last_run")), next_run=_when(saved.get("next_run")),
        last_error=saved.get("last_error"),
    )


def configure(settings: AutoscanSettings, now: datetime | None = None) -> AutoscanStatus:
    """Turn auto-scan on or off. Turning it on schedules the first run right away."""
    saved = _saved()
    current = now or datetime.now(timezone.utc)
    was_on = bool(saved.get("enabled"))
    saved.update(enabled=settings.enabled, interval_minutes=settings.interval_minutes)
    if settings.enabled and not was_on:
        saved["next_run"] = current.isoformat()
    elif settings.enabled and saved.get("last_run"):
        saved["next_run"] = (_when(saved["last_run"]) + timedelta(minutes=settings.interval_minutes)).isoformat()
    elif not settings.enabled:
        saved["next_run"] = None
    alerts._save(SETTINGS_FILE, saved)
    return status()


async def run_once() -> list[str]:
    """Scan every store once; alert on changes. Returns the alert events sent."""
    sent: list[str] = []
    problems: list[str] = []
    async with _lock:
        for store in STORE_KEYS:
            request = ScanRequest(store=store, explain=False, check_paypal_side=False)
            try:
                result, previous = await full_scan(request)
            except (investigate.StoreUnavailable, paypal.PayPalConfigurationError, paypal.PayPalProviderError) as exc:
                problems.append(f"{store}: {exc}")
                continue
            event = alert_for(result)
            if event and changed(previous, result):
                await alerts.deliver_to_store_owners(event, LOCAL_OWNER)
                sent.append(f"{store}:{event['event']}")
        saved = _saved()
        finished = datetime.now(timezone.utc)
        saved.update(last_run=finished.isoformat(), last_error="; ".join(problems)[:500] or None)
        if saved.get("enabled"):
            saved["next_run"] = (finished + timedelta(minutes=int(saved.get("interval_minutes") or 10))).isoformat()
        alerts._save(SETTINGS_FILE, saved)
    return sent


def due(now: datetime | None = None) -> bool:
    saved = _saved()
    next_run = _when(saved.get("next_run"))
    return bool(saved.get("enabled")) and next_run is not None and (now or datetime.now(timezone.utc)) >= next_run


async def loop() -> None:
    """Runs for the life of the server: every half minute, start a scan if one is due."""
    while True:
        await asyncio.sleep(TICK_SECONDS)
        if due() and not _lock.locked():
            try:
                await run_once()
            except Exception:  # one failed run must not stop auto-scan for the life of the server
                logging.getLogger("payrecover.autoscan").exception("Auto-scan run failed")
