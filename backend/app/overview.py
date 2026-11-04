"""The dashboard overview: headline numbers and each store's latest scan result.

Each store's latest scan is kept in last-scans.json beside the alert feed, so the overview
survives a restart. The open-disputes count comes from PayPal's Agent Toolkit and is cached
for a minute, so refreshing the dashboard does not call PayPal every time.
"""

import asyncio
import time
from decimal import Decimal

import aiohttp

from app import alerts, investigate, paypal, paypal_toolkit, recovery, scan
from app.schemas import STORE_KEYS, Overview, ScanRequest, ScanResult, StorePing, StoreStatus

DISPUTES_CACHE_SECONDS = 60
OPEN_DISPUTE_STATES = ("REQUIRED_ACTION", "REQUIRED_OTHER_PARTY_ACTION", "UNDER_PAYPAL_REVIEW")
# "at" is None until the first fetch: monotonic time starts near boot, so 0 can look like "just fetched".
_disputes_cache: dict[str, float | int | None] = {"at": None, "count": None}


def remember_scan(result: ScanResult) -> None:
    """Keep the store's latest scan for the overview."""
    data = alerts._load("last-scans.json")
    data[result.store] = StoreStatus(
        store=result.store, verdict=result.verdict, orders_checked=result.orders_checked,
        money_at_risk=result.money_at_risk, currency=result.currency,
        security_issues=len(result.security.findings) if result.security else 0, scanned_at=result.scanned_at,
    ).model_dump(mode="json")
    alerts._save("last-scans.json", data)


async def _open_disputes() -> int | None:
    fetched = _disputes_cache["at"]
    if fetched is not None and time.monotonic() - float(fetched) < DISPUTES_CACHE_SECONDS:
        return _disputes_cache["count"]  # type: ignore[return-value]
    count = 0
    try:
        for state in OPEN_DISPUTE_STATES:
            listed = await paypal_toolkit.run("list_disputes", {"dispute_state": state, "page_size": 50})
            count += len(listed.get("items") or [])
    except (paypal_toolkit.ToolkitError, paypal_toolkit.ToolkitUnavailable, paypal.PayPalConfigurationError):
        count = None
    _disputes_cache.update(at=time.monotonic(), count=count)
    return count


async def build(owner: str) -> Overview:
    saved = alerts._load("last-scans.json")
    stores = [StoreStatus.model_validate(saved[key]) if key in saved else StoreStatus(store=key) for key in STORE_KEYS]
    totals = recovery.list_recoveries(owner)["totals"]
    recovered = Decimal(totals["collected"]) + Decimal(totals["invoiced"]) + Decimal(totals["refunded"])
    at_risk = sum((Decimal(s.money_at_risk) for s in stores), Decimal(0))
    newest_alert = next(iter(alerts.view(owner).alerts), None)
    moments = [s.scanned_at for s in stores if s.scanned_at] + ([newest_alert.created_at] if newest_alert else [])
    return Overview(
        orders_scanned=sum(s.orders_checked for s in stores),
        money_at_risk=str(at_risk.quantize(Decimal("0.01"))),
        currency=next((s.currency for s in stores if s.scanned_at), "USD"),
        open_disputes=await _open_disputes(),
        money_recovered=str(recovered.quantize(Decimal("0.01"))),
        stores=stores,
        last_activity=max(moments) if moments else None,
    )


async def refresh_after_recovery(store: str) -> None:
    """Re-check the store's payments after a recovery, so money at risk drops right away.

    Only payments are re-read (no security probe, no AI plan, no alerts); the store's last
    security result is kept, and the verdict is worked out again from both.
    """
    data = alerts._load("last-scans.json")
    previous = data.get(store)
    if previous is None:
        return
    try:
        result = await scan.scan_store(ScanRequest(store=store, explain=False, check_paypal_side=False,
                                                   check_security=False))
    except (investigate.StoreUnavailable, paypal.PayPalConfigurationError, paypal.PayPalProviderError):
        return
    issues = int(previous.get("security_issues") or 0)
    previous.update(
        verdict="leaking" if result.leaking_orders else ("vulnerable" if issues else "clear"),
        orders_checked=result.orders_checked, money_at_risk=result.money_at_risk, currency=result.currency,
        scanned_at=result.scanned_at.isoformat(),
    )
    data[store] = previous
    alerts._save("last-scans.json", data)


# A free Render store sleeps when idle and takes up to about a minute to start again.
STORE_START_SECONDS = 75


async def _store_up(store: str) -> StorePing:
    """Ask one store for its product list, which also starts it if its host put it to sleep."""
    investigate._active_store.set(store)
    started = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=STORE_START_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(f"{investigate.store_url()}/api/products") as response:
                up = response.status < 500
    except (aiohttp.ClientError, asyncio.TimeoutError):
        up = False
    return StorePing(store=store, up=up, seconds=round(time.monotonic() - started, 1))


async def start_stores() -> list[StorePing]:
    """Start every demo store at once (each in its own task), and say which ones answered."""
    return list(await asyncio.gather(*(_store_up(store) for store in STORE_KEYS)))
