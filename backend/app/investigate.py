"""PayRecover order investigation: one order id in, every check out.

Three sources are checked against each other:
  the store's claim   GET {store}/api/orders/{id}, and its catalog at GET {store}/api/products
  PayPal's record     the Sandbox Orders API, through app.paypal (read-only)
  live store probes   one price-tampering attempt that creates an unapproved Sandbox order
                      (no money moves)
  website checks      TestPilot's full website scan of the store page (Playwright, security,
                      accessibility, contract), run through the normal runners
The store address is set by the operator (TESTPILOT_STORE_URL), never by the request.
Leaks and money at risk come only from these checks. Gemma explains them; it cannot
add, remove or change a leak, and the suggested recovery is never executed here.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from app import gemma, paypal
from app.schemas import (
    CheckStatus,
    GemmaExplanation,
    Investigation,
    InvestigationRequest,
    Leak,
    PaymentCheck,
    PaymentVerificationRequest,
    FULL_SCAN_CHECKS,
    WebsiteRunReport,
    WebsiteRunRequest,
)
from app.url_policy import trusted_origin

WebsiteRunner = Callable[[WebsiteRunRequest], Awaitable[WebsiteRunReport]]

DEFAULT_STORE_URL = "http://127.0.0.1:8100"
STORE_TIMEOUT_SECONDS = 10
TAMPERED_PRICE = "0.01"
CENT = Decimal("0.01")


class StoreUnavailable(RuntimeError):
    """The configured store could not be reached or answered with unusable data."""


def store_url() -> str:
    url = os.environ.get("TESTPILOT_STORE_URL", DEFAULT_STORE_URL).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise StoreUnavailable("TESTPILOT_STORE_URL must start with http:// or https://.")
    return url


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT))


async def _store_json(session: aiohttp.ClientSession, method: str, path: str, body: dict | None = None):
    try:
        async with session.request(method, f"{store_url()}{path}", json=body) as response:
            text = await response.text()
            status = response.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise StoreUnavailable(f"The store at {store_url()} could not be reached.") from exc
    try:
        return status, json.loads(text)
    except json.JSONDecodeError as exc:
        raise StoreUnavailable(f"The store returned non-JSON data for {path}.") from exc


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=STORE_TIMEOUT_SECONDS), trust_env=False)


async def read_store(order_id: str) -> tuple[dict | None, dict[str, Decimal] | None, str | None]:
    """The store's claim about this order and its catalog prices, or why they could not be read."""
    async with _session() as session:
        status, claim = await _store_json(session, "GET", f"/api/orders/{order_id}")
        _, products = await _store_json(session, "GET", "/api/products")
    catalog = None
    if isinstance(products, list):
        catalog = {item["id"]: _decimal(item["price"]) for item in products if isinstance(item, dict) and "id" in item}
    if status == 404:
        return None, catalog, "The store has no record of this order."
    if status >= 400 or not isinstance(claim, dict):
        return None, catalog, f"The store answered HTTP {status} for this order."
    return claim, catalog, None


async def probe_store(catalog: dict[str, Decimal] | None) -> list[PaymentCheck]:
    """Whether the store trusts a price sent by the browser. Creates one unapproved Sandbox order."""
    checks = []
    if not catalog:
        return checks
    product_id, price = next(iter(catalog.items()))
    async with _session() as session:
        status, created = await _store_json(session, "POST", "/api/orders", {
            "items": [{"product_id": product_id, "quantity": 1, "unit_price": TAMPERED_PRICE}],
        })
    charged = _decimal(created.get("total")) if isinstance(created, dict) else None
    if status >= 400 or charged is None:
        checks.append(PaymentCheck(
            name="price_tampering_probe",
            status=CheckStatus.ERROR,
            message=f"The probe order could not be created (HTTP {status}).",
            evidence={"http_status": status},
        ))
        return checks
    trusted = charged != price
    checks.append(PaymentCheck(
        name="price_tampering_probe",
        status=CheckStatus.FAILED if trusted else CheckStatus.PASSED,
        message=(
            f"The store accepted a browser-sent price: {product_id} costs {_money(price)}, the store asked PayPal for {_money(charged)}."
            if trusted else f"The store ignored a tampered price and charged the catalog price {_money(price)}."
        ),
        evidence={
            "product_id": product_id, "catalog_price": _money(price), "sent_price": TAMPERED_PRICE,
            "store_total": _money(charged), "probe_order_id": created.get("id"),
            "note": "The probe order is never approved, so no money moves.",
        },
    ))
    return checks


def _captures(order: dict[str, Any]) -> list[dict[str, Any]]:
    captures = []
    for unit in order.get("purchase_units") or []:
        captures.extend((unit.get("payments") or {}).get("captures") or [])
    return captures


def expected_total(claim: dict | None, catalog: dict[str, Decimal] | None) -> Decimal | None:
    """What the order should cost: catalog price x quantity, or the store's own total if the catalog is unknown."""
    if claim is None:
        return None
    items = claim.get("items") or []
    if catalog and items and all(item.get("product_id") in catalog for item in items):
        return sum((catalog[item["product_id"]] * int(item.get("quantity", 0)) for item in items), Decimal(0))
    return _decimal(claim.get("total"))


def _price_check(claim: dict, catalog: dict[str, Decimal] | None, expected: Decimal | None) -> PaymentCheck:
    recorded = _decimal(claim.get("total"))
    if not catalog or expected is None:
        return PaymentCheck(
            name="price_integrity", status=CheckStatus.SKIPPED,
            message="The store catalog was not available, so prices could not be checked.", evidence={},
        )
    off = [
        {"product_id": item.get("product_id"), "recorded": item.get("unit_price"),
         "catalog": _money(catalog[item["product_id"]])}
        for item in claim.get("items") or []
        if item.get("product_id") in catalog and _decimal(item.get("unit_price")) != catalog[item["product_id"]]
    ]
    ok = not off and recorded == expected
    return PaymentCheck(
        name="price_integrity",
        status=CheckStatus.PASSED if ok else CheckStatus.FAILED,
        message=(
            f"Order total {_money(recorded)} matches the catalog." if ok
            else f"The store charged {claim.get('total')} but the catalog price is {_money(expected)}."
        ),
        evidence={"recorded_total": claim.get("total"), "catalog_total": _money(expected), "mismatched_lines": off},
    )


def _claim_check(claim: dict, summary: dict | None) -> PaymentCheck:
    store_paid = claim.get("status") == "PAID"
    paypal_paid = bool(summary) and summary["capture_status"] == "COMPLETED"
    ok = store_paid == paypal_paid
    paypal_state = "no order" if summary is None else f"{summary['status']}, capture {summary['capture_status'] or 'none'}"
    return PaymentCheck(
        name="store_claim_matches",
        status=CheckStatus.PASSED if ok else CheckStatus.FAILED,
        message=f"The store says {claim.get('status')}; PayPal says {paypal_state}.",
        evidence={"store_status": claim.get("status"), "paypal_status": summary and summary["status"],
                  "paypal_capture_status": summary and summary["capture_status"]},
    )


def find_leaks(
    claim: dict | None, order: dict | None, expected: Decimal | None, checks: list[PaymentCheck]
) -> list[Leak]:
    """Turn failed checks into leaks with an amount and a suggested recovery. Deterministic."""
    if claim is None or claim.get("status") != "PAID" or expected is None:
        return []
    currency = claim.get("currency") or "USD"
    statuses = {check.name: check.status for check in checks}
    price_hole = CheckStatus.FAILED in {statuses.get("price_integrity"), statuses.get("price_tampering_probe")}
    capture_hole = "The store marks an order PAID without a completed PayPal capture."
    if price_hole:
        capture_hole += " It also trusts prices sent by the browser."
    if order is None or order.get("status") not in {"APPROVED", "COMPLETED"}:
        return [Leak(
            kind="fake_paid", title="Marked paid, but PayPal never received a payment",
            amount=_money(expected), currency=currency, root_cause=capture_hole, recovery_action="invoice",
            recovery=f"Send the buyer a PayPal invoice for {_money(expected)} {currency}, or hold the shipment.",
        )]
    captures = [c for c in _captures(order) if c.get("status") == "COMPLETED"]
    if not captures:
        return [Leak(
            kind="never_captured", title="Buyer approved, but the money was never captured",
            amount=_money(expected), currency=currency, root_cause=capture_hole, recovery_action="capture",
            recovery=f"Capture order {order.get('id')} at PayPal now to collect {_money(expected)} {currency}.",
        )]
    leaks = []
    amounts = [_decimal((c.get("amount") or {}).get("value")) or Decimal(0) for c in captures]
    if len(captures) > 1:
        extra = sum(amounts[1:], Decimal(0))
        leaks.append(Leak(
            kind="duplicate_charge", title=f"Buyer was charged {len(captures)} times",
            amount=_money(extra), currency=currency,
            root_cause="Capture is not idempotent: a retry created a second charge.", recovery_action="refund",
            recovery=f"Refund the extra {_money(extra)} {currency} before the buyer files a chargeback.",
        ))
    shortfall = expected - amounts[0]
    if shortfall > 0:
        leaks.append(Leak(
            kind="wrong_amount", title=f"Charged {_money(amounts[0])} instead of {_money(expected)}",
            amount=_money(shortfall), currency=currency,
            root_cause=(
                "The store's server uses the price the browser sends, so the buyer can change it."
                if price_hole else "The amount sent to PayPal does not match the catalog price."
            ),
            recovery_action="invoice",
            recovery=f"Send the buyer a PayPal invoice for the missing {_money(shortfall)} {currency}, and price carts on the server.",
        ))
    return leaks


def _risk(leaks: list[Leak], checks: list[PaymentCheck]) -> str:
    if leaks or any(c.status == CheckStatus.FAILED for c in checks):
        return "HIGH"
    if any(c.status != CheckStatus.PASSED for c in checks):
        return "REVIEW"
    return "LOW"


def builtin_explanation(result: dict[str, Any], reason: str | None = None) -> GemmaExplanation:
    leaks = result["leaks"]
    if leaks:
        summary = (
            f"Order {result['order_id']} is leaking money: {result['money_at_risk']} {result['currency']} at risk. "
            + " ".join(leak["title"] + "." for leak in leaks)
        )
    elif result["verdict"] == "healthy":
        summary = f"Order {result['order_id']} is healthy: the store's claim matches PayPal's record."
    else:
        summary = f"Order {result['order_id']} needs review: some checks could not be completed."
    problems = [c for c in result["checks"] if c["status"] in {"failed", "error", "warning"}]
    website = (result.get("website_scan") or {}).get("checks", [])
    problems += [
        {"name": f"website {c['check']}", "message": c["message"]} for c in website if c["status"] in {"failed", "error"}
    ]
    limitations = ["Gemma was not used; this summary was built from the check records only."]
    if reason:
        limitations.append(reason[:300])
    return GemmaExplanation(
        summary=summary,
        findings=[f"{c['name']}: {c['message']}" for c in problems][:10],
        limitations=limitations,
        next_steps=[leak["recovery"] for leak in leaks][:5] + [leak["root_cause"] for leak in leaks][:5],
        risk_level=_risk([Leak(**leak) for leak in leaks], [PaymentCheck(**c) for c in result["checks"]]),
        source="Built-in summary",
    )


async def explain(result: dict[str, Any]) -> GemmaExplanation:
    """Gemma explains for a merchant in plain words; the deterministic risk is a floor it cannot lower."""
    floor = builtin_explanation(result).risk_level
    prompt = f"""Return only JSON matching this schema:
{{"summary": "string", "findings": ["string"], "limitations": ["string"], "next_steps": ["string"], "risk_level": "HIGH|REVIEW|LOW"}}
You are helping a small online merchant. Explain this payment investigation in plain, simple words: what went wrong with the order, how much money is at risk, why it happened (the root cause), and what the merchant should do. Use only the checks, leaks and evidence below. Never add a leak, change an amount, or say a failed check passed. Recovery actions need the merchant's approval; do not say they were done.
Investigation JSON:
{json.dumps(result, separators=(',', ':'), default=str)}"""
    try:
        value = gemma._extract_json(await gemma._generate(prompt))
        risk = str(value.get("risk_level", "")).upper()
        value["risk_level"] = risk if risk in {"HIGH", "REVIEW", "LOW"} else "UNKNOWN"
        value["source"] = "Gemma"
        explanation = GemmaExplanation.model_validate(value)
    except (gemma.GemmaConfigurationError, gemma.GemmaProviderError) as exc:
        return builtin_explanation(result, str(exc))
    except Exception:
        return builtin_explanation(result, "Gemma's explanation did not match the required schema.")
    order = ["UNKNOWN", "LOW", "REVIEW", "HIGH"]
    if order.index(explanation.risk_level) < order.index(floor):
        explanation.risk_level = floor
    return explanation


async def scan_store(run_website: WebsiteRunner) -> WebsiteRunReport:
    """TestPilot's full website scan of the store page, allowed past the private-address rule for this origin only."""
    url = f"{store_url()}/"
    with trusted_origin(url):
        return await run_website(WebsiteRunRequest(
            url=url, authorization_confirmed=True, checks=list(FULL_SCAN_CHECKS),
        ))


def _website_digest(report: WebsiteRunReport | None) -> dict[str, Any] | None:
    """What the explanation needs from the website scan: statuses and messages, no raw evidence."""
    if report is None:
        return None
    return {
        "overall_status": report.overall_status,
        "checks": [{"check": c.check_id, "status": c.status, "message": c.message} for c in report.checks],
    }


async def investigate(request: InvestigationRequest, run_website: WebsiteRunner | None = None) -> Investigation:
    """Run every check for one order. PayPal configuration and provider errors propagate to the caller."""
    checks: list[PaymentCheck] = []
    try:
        base = store_url()
        claim, catalog, store_note = await read_store(request.order_id)
    except StoreUnavailable as exc:
        base = os.environ.get("TESTPILOT_STORE_URL", DEFAULT_STORE_URL)
        claim, catalog, store_note = None, None, str(exc)
    checks.append(PaymentCheck(
        name="store_claim_found",
        status=CheckStatus.PASSED if claim else CheckStatus.ERROR,
        message=(f"The store says this order is {claim.get('status')}, total {claim.get('total')} {claim.get('currency')}."
                 if claim else store_note),
        evidence={"store_url": base, "status": claim and claim.get("status")},
    ))

    try:
        order = await paypal.fetch_order(request.order_id)
    except paypal.PayPalOrderNotFound as exc:
        order = None
        checks.append(PaymentCheck(
            name="order_found", status=CheckStatus.FAILED, message=str(exc), evidence={"order_id": request.order_id}
        ))
    summary = paypal.order_summary(order) if order else None
    if summary:
        summary["capture_count"] = len(_captures(order))

    expected = expected_total(claim, catalog)
    currency = (claim or {}).get("currency") or (summary or {}).get("currency") or "USD"
    if order:
        claims_paid = claim is None or claim.get("status") == "PAID"
        checks.extend(paypal.evaluate_order(order, PaymentVerificationRequest(
            order_id=request.order_id,
            expected_status="COMPLETED" if claims_paid else order.get("status"),
            require_capture=claims_paid,
            expected_amount=_money(expected) if expected is not None and claims_paid else None,
            expected_currency=currency,
        )))
        checks.append(PaymentCheck(
            name="single_capture",
            status=CheckStatus.PASSED if summary["capture_count"] <= 1 else CheckStatus.FAILED,
            message=f"PayPal shows {summary['capture_count']} capture(s) for this order.",
            evidence={"capture_count": summary["capture_count"]},
        ))
    if claim:
        checks.append(_claim_check(claim, summary))
        checks.append(_price_check(claim, catalog, expected))
    if request.probe_store and claim is not None:
        try:
            checks.extend(await probe_store(catalog))
        except StoreUnavailable as exc:
            checks.append(PaymentCheck(
                name="price_tampering_probe", status=CheckStatus.ERROR, message=str(exc), evidence={}
            ))

    website_report = None
    if request.website_checks and run_website is not None and claim is not None:
        website_report = await scan_store(run_website)

    leaks = find_leaks(claim, order, expected, checks)
    if leaks:
        verdict = "leaking"
    elif claim and order and all(c.status in {CheckStatus.PASSED, CheckStatus.WARNING} for c in checks):
        verdict = "healthy"
    else:
        verdict = "review"
    at_risk = sum((Decimal(leak.amount) for leak in leaks), Decimal(0))
    result = Investigation(
        order_id=request.order_id,
        verdict=verdict,
        checked_at=datetime.now(timezone.utc),
        store_url=base,
        store_claim=claim,
        expected_total=_money(expected) if expected is not None else None,
        paypal_order=summary,
        checks=checks,
        website_report=website_report,
        leaks=leaks,
        money_at_risk=_money(at_risk),
        currency=currency,
    )
    if request.explain:
        digest = result.model_dump(mode="json", exclude={"explanation", "website_report"})
        digest["website_scan"] = _website_digest(website_report)
        result.explanation = await explain(digest)
    return result
