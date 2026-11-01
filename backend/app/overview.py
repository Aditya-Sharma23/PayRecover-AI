"""The dashboard overview: headline numbers and each store's latest scan result.

Each store's latest scan is kept in last-scans.json beside the alert feed, so the overview
survives a restart. The open-disputes count comes from PayPal's Agent Toolkit and is cached
for a minute, so refreshing the dashboard does not call PayPal every time.
"""

import time
from decimal import Decimal

from app import alerts, paypal, paypal_toolkit, recovery
from app.schemas import STORE_KEYS, Overview, ScanResult, StoreStatus

DISPUTES_CACHE_SECONDS = 60
OPEN_DISPUTE_STATES = ("REQUIRED_ACTION", "REQUIRED_OTHER_PARTY_ACTION", "UNDER_PAYPAL_REVIEW")
_disputes_cache: dict[str, float | int | None] = {"at": 0.0, "count": None}


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
    if time.monotonic() - float(_disputes_cache["at"] or 0) < DISPUTES_CACHE_SECONDS:
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
