"""PayPal setup check: which Sandbox app features PayRecover needs are on, and how to turn on the rest.

Each feature is tested with the smallest read PayPal offers for it (one invoice, one dispute, one
day of transactions), so the answer comes from PayPal itself rather than from a setting PayRecover
guesses at. A feature PayPal refuses (HTTP 401 or 403) is reported as off, with the exact place in
developer.paypal.com to switch it on. Results are cached for two minutes, so the dashboard can
show them on every refresh without calling PayPal each time.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app import paypal, paypal_toolkit, webhooks
from app.schemas import PayPalSetup, PayPalSetupItem

CACHE_SECONDS = 120
APP_FEATURES = "developer.paypal.com > Apps & Credentials > your Sandbox app > Features"
_cache: dict[str, Any] = {"at": None, "value": None}


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _credentials() -> PayPalSetupItem:
    name = "Sandbox keys"
    try:
        # Any answer means PayPal issued a token for these keys; the read itself may be refused.
        await paypal.paypal_call("GET", "/v1/identity/oauth2/userinfo?schema=paypalv1.1")
    except paypal.PayPalConfigurationError as exc:
        return PayPalSetupItem(id="credentials", name=name, ok=False, detail=str(exc),
                               fix="Copy the Client ID and Secret of your Sandbox app into backend/.env, then restart.")
    except paypal.PayPalProviderError as exc:
        return PayPalSetupItem(id="credentials", name=name, ok=None, detail=str(exc), fix="Try again in a minute.")
    return PayPalSetupItem(id="credentials", name=name, ok=True, detail="PayPal accepted the Client ID and Secret.")


async def _invoicing() -> PayPalSetupItem:
    name = "Invoicing"
    path = "/v2/invoicing/invoices?page=1&page_size=1"
    status, _ = await paypal.paypal_call("GET", path)
    if status in {401, 403}:
        # A cached token keeps the permissions it was issued with; ask for a fresh one once.
        paypal.forget_token()
        status, _ = await paypal.paypal_call("GET", path)
    if status in {401, 403}:
        return PayPalSetupItem(id="invoicing", name=name, ok=False,
                               detail=f"PayPal refused to list invoices (HTTP {status}). Invoicing short payments will fail.",
                               fix=f"{APP_FEATURES} > Invoicing, then save.")
    if status >= 400:
        return PayPalSetupItem(id="invoicing", name=name, ok=None, detail=f"PayPal answered HTTP {status}.",
                               fix="Try again in a minute.")
    return PayPalSetupItem(id="invoicing", name=name, ok=True, detail="PayRecover can send invoices for short payments.")


async def _toolkit_feature(feature_id: str, name: str, method: str, params: dict[str, Any], works: str,
                           switch: str, lag: str = "") -> PayPalSetupItem:
    try:
        await paypal_toolkit.run(method, params)
    except paypal_toolkit.ToolkitUnavailable as exc:
        return PayPalSetupItem(id=feature_id, name=name, ok=False, detail=str(exc),
                               fix="Install the Agent Toolkit: pip install --no-deps -r requirements-toolkit.txt")
    except paypal_toolkit.ToolkitError as exc:
        if exc.status in {401, 403}:
            return PayPalSetupItem(id=feature_id, name=name, ok=False,
                                   detail=f"PayPal refused it{paypal_toolkit.said(exc)}",
                                   fix=f"{APP_FEATURES} > {switch}, then save.{lag}")
        return PayPalSetupItem(id=feature_id, name=name, ok=None, detail=str(exc), fix="Try again in a minute.")
    return PayPalSetupItem(id=feature_id, name=name, ok=True, detail=works)


def _webhooks() -> PayPalSetupItem:
    configured = webhooks.status()["configured"]
    return PayPalSetupItem(
        id="webhooks", name="Webhooks (optional)", ok=True if configured else None, required=False,
        detail="PayPal events are verified and checked as they arrive." if configured
        else "Not set up. Scans and auto-scan still work; webhooks add real-time checks once deployed.",
        fix=None if configured else "Add a webhook to your app pointing at <public url>/api/paypal/webhook, then set "
                                    "TESTPILOT_PAYPAL_WEBHOOK_ID.",
    )


async def check(refresh: bool = False, now: datetime | None = None) -> PayPalSetup:
    """Ask PayPal which features work for these Sandbox keys."""
    fetched = _cache["at"]
    if not refresh and fetched is not None and time.monotonic() - float(fetched) < CACHE_SECONDS:
        return _cache["value"]
    current = now or datetime.now(timezone.utc)
    items = [await _credentials()]
    if items[0].ok is False:
        result = PayPalSetup(ready=False, items=items, checked_at=current)
        _cache.update(at=time.monotonic(), value=result)
        return result
    items.append(await _toolkit_feature(
        "transaction_search", "Transaction search", "list_transactions",
        {"start_date": _stamp(current - timedelta(days=1)), "end_date": _stamp(current), "page_size": 1},
        "PayRecover can find payments the store never recorded.", "Transaction search",
        " New payments can take up to three hours to appear after it is on.",
    ))
    items.append(await _toolkit_feature(
        "disputes", "Disputes", "list_disputes", {"page_size": 1},
        "The dispute defender can read and answer disputes.", "Disputes",
    ))
    items.append(await _invoicing())
    items.append(_webhooks())
    ready = all(item.ok for item in items if item.required)
    result = PayPalSetup(ready=ready, items=items, checked_at=current)
    _cache.update(at=time.monotonic(), value=result)
    return result
