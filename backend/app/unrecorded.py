"""PayPal's side of a store scan: payments PayPal completed for the store that the store never recorded.

The store scan starts from the store's orders; this starts from PayPal's transaction list (read
with PayPal's Agent Toolkit). Each store tags its payments with a custom_id, so a completed
payment carrying the store's tag but missing from the store's paid orders means the buyer was
charged and the store does not know: the order will not ship, and a dispute usually follows.
Refunded payments are left out.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app import investigate, paypal_toolkit, scan
from app.schemas import UnrecordedPayment, UnrecordedReport

STORE_TAGS = {"main": "payrecover-main", "vulnerable": "payrecover-vulnerable", "risky": "payrecover-risky"}
LOOKBACK_DAYS = 30
STORE_ORDERS_READ = 100
TRANSACTION_SEARCH_HELP = (
    "PayPal refused the transaction list. Turn on Transaction search for your Sandbox app: developer.paypal.com > "
    "Apps & Credentials > your app > Features > Transaction search, then save. New payments can take up to "
    "three hours to appear in PayPal's transaction list."
)


def _mask(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    name, domain = email.split("@", 1)
    return f"{name[:1]}***@{domain}"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


async def paypal_payments(store: str, days: int = LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """Completed, unrefunded PayPal payments tagged with this store, newest first."""
    now = datetime.now(timezone.utc)
    listed = await paypal_toolkit.run("list_transactions", {
        "start_date": _stamp(now - timedelta(days=min(days, 31))), "end_date": _stamp(now),
        "transaction_status": "S", "page_size": 100,
    })
    details = listed.get("transaction_details") or []
    refunded = {(d.get("transaction_info") or {}).get("paypal_reference_id") for d in details
                if Decimal(str(((d.get("transaction_info") or {}).get("transaction_amount") or {}).get("value", "0"))) < 0}
    payments = []
    for detail in details:
        info = detail.get("transaction_info") or {}
        amount = info.get("transaction_amount") or {}
        if info.get("custom_field") != STORE_TAGS[store] or Decimal(str(amount.get("value", "0"))) <= 0:
            continue
        if info.get("transaction_id") in refunded:
            continue
        payments.append({
            "transaction_id": info.get("transaction_id"), "amount": str(amount.get("value")),
            "currency": amount.get("currency_code", "USD"), "paid_at": info.get("transaction_initiation_date"),
            "payer_email": (detail.get("payer_info") or {}).get("email_address"),
        })
    return sorted(payments, key=lambda p: p["paid_at"] or "", reverse=True)


async def find(store: str) -> UnrecordedReport:
    """Compare PayPal's completed payments for this store with the captures the store recorded as paid."""
    investigate._active_store.set(store)
    try:
        payments = await paypal_payments(store)
    except paypal_toolkit.ToolkitUnavailable as exc:
        return UnrecordedReport(store=store, checked=False, note=str(exc))
    except paypal_toolkit.ToolkitError as exc:
        return UnrecordedReport(store=store, checked=False,
                                note=TRANSACTION_SEARCH_HELP if exc.status in {401, 403} else str(exc))
    try:
        orders = await scan.recent_orders(STORE_ORDERS_READ)
    except investigate.StoreUnavailable as exc:
        return UnrecordedReport(store=store, checked=False, note=str(exc))
    recorded = {order.get("capture_id") for order in orders if order.get("status") == "PAID"}
    missing = [
        UnrecordedPayment(transaction_id=p["transaction_id"], amount=p["amount"], currency=p["currency"],
                          paid_at=p["paid_at"], payer_email_masked=_mask(p["payer_email"]), store=store)
        for p in payments if p["transaction_id"] not in recorded
    ]
    total = sum((Decimal(p.amount) for p in missing), Decimal(0))
    return UnrecordedReport(
        store=store, checked=True, payments=missing, money=investigate._money(total),
        currency=missing[0].currency if missing else "USD",
        note=None if missing else f"Every PayPal payment for this store in the last {LOOKBACK_DAYS} days is recorded "
                                  "(PayPal's list can lag by up to three hours).",
    )
