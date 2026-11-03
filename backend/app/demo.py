"""Demo scenarios: made-up orders run through PayRecover's real rules, so every result can be shown on demand.

A judge or a first-time visitor sees each leak without a PayPal account, a store or a working
Sandbox. Nothing here calls PayPal or a store: each scenario is a fixed store claim, catalog and
PayPal order record, passed to investigate.judge(), the same function a real investigation uses.
Results are marked sample_data, and the dashboard turns off Approve and the evidence pack for them,
because there is no real payment behind them to recover.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app import disputes, investigate
from app.schemas import CheckStatus, DemoScenario, DisputeCase, Investigation, PaymentCheck

ORDER_ID = "DEMO0RDER0000001"
OTHER_ORDER_ID = "DEMO0RDER0000002"
CATALOG = {"tee": Decimal("18.00"), "mug": Decimal("12.50"), "stickers": Decimal("4.99")}
SCENARIOS = {
    "never_captured": ("Never captured", "The buyer approved the payment and the store says paid, but nobody "
                                         "collected the money at PayPal."),
    "fake_paid": ("Fake paid", "The store says paid, but PayPal never received a payment."),
    "wrong_amount": ("Wrong amount", "The store charged the price the browser sent: 1.80 instead of 18.00."),
    "duplicate_charge": ("Duplicate charge", "A double click paid the same cart twice."),
    "hold_expiring": ("Held payment expiring", "PayPal only holds the money; the hold expires in two days unless "
                                               "the store captures it."),
    "waiting": ("Waiting for PayPal", "A buyer opened a dispute, so PayPal is holding the payment."),
    "healthy": ("Healthy", "Everything matches: the store's claim and PayPal's record agree."),
}


class UnknownScenario(KeyError):
    """There is no demo scenario with this name."""


def scenarios() -> list[DemoScenario]:
    return [DemoScenario(id=key, title=title, description=text) for key, (title, text) in SCENARIOS.items()]


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim(total: str = "18.00", unit_price: str = "18.00", cart_id: str | None = "demo-cart-0001") -> dict[str, Any]:
    return {
        "order_id": ORDER_ID, "status": "PAID", "total": total, "currency": "USD", "cart_id": cart_id,
        "capture_id": None, "created_at": None,
        "items": [{"product_id": "tee", "name": "PayRecover T-shirt", "quantity": 1, "unit_price": unit_price}],
    }


def _order(now: datetime, status: str = "COMPLETED", amount: str = "18.00", intent: str = "CAPTURE",
           payments: dict | None = None) -> dict[str, Any]:
    unit: dict[str, Any] = {"amount": {"currency_code": "USD", "value": amount}, "custom_id": "payrecover-demo"}
    if payments:
        unit["payments"] = payments
    return {"id": ORDER_ID, "status": status, "intent": intent, "purchase_units": [unit],
            "create_time": _stamp(now - timedelta(hours=3)), "update_time": _stamp(now - timedelta(hours=2)),
            "payer": {"email_address": "buyer@example.com"}}


def _capture(capture_id: str, amount: str = "18.00", status: str = "COMPLETED") -> dict[str, Any]:
    return {"id": capture_id, "status": status, "amount": {"currency_code": "USD", "value": amount}}


def _inputs(name: str, now: datetime) -> dict[str, Any]:
    """The store's claim, PayPal's record and the cart's other orders for one scenario."""
    no_others: dict[str, Any] = {"siblings": [], "sibling_note": None}
    if name == "never_captured":
        return {"claim": _claim(), "order": _order(now, status="APPROVED"), **no_others}
    if name == "fake_paid":
        return {"claim": _claim(), "order": None, "order_note": f"PayPal Sandbox has no order {ORDER_ID}.", **no_others}
    if name == "wrong_amount":
        return {"claim": _claim(total="1.80", unit_price="1.80"),
                "order": _order(now, amount="1.80", payments={"captures": [_capture("DEMOCAPTURE01", "1.80")]}),
                **no_others}
    if name == "duplicate_charge":
        other = {"order_id": OTHER_ORDER_ID, "store_status": "PAID", "total": "18.00", "paypal_status": "COMPLETED",
                 "captured": "18.00", "capture_ids": ["DEMOCAPTURE02"],
                 "captures": [{"id": "DEMOCAPTURE02", "amount": "18.00"}]}
        return {"claim": _claim(), "order": _order(now, payments={"captures": [_capture("DEMOCAPTURE01")]}),
                "siblings": [other], "sibling_note": None}
    if name == "hold_expiring":
        hold = {"id": "DEMOHOLD000001", "status": "CREATED", "amount": {"currency_code": "USD", "value": "18.00"},
                "create_time": _stamp(now - timedelta(days=27)), "expiration_time": _stamp(now + timedelta(days=2))}
        return {"claim": _claim(), "order": _order(now, intent="AUTHORIZE", payments={"authorizations": [hold]}),
                **no_others}
    if name == "waiting":
        return {"claim": _claim(),
                "order": _order(now, payments={"captures": [_capture("DEMOCAPTURE01", status="PENDING")]}),
                **no_others}
    if name == "healthy":
        return {"claim": _claim(), "order": _order(now, payments={"captures": [_capture("DEMOCAPTURE01")]}),
                **no_others}
    raise UnknownScenario(name)


def investigation(name: str, now: datetime | None = None) -> Investigation:
    """One scenario's full investigation, from the real rules. No network calls."""
    current = now or datetime.now(timezone.utc)
    data = _inputs(name, current)
    judged = investigate.judge(data["claim"], CATALOG, data["order"], data.get("order_note"), [],
                               data["siblings"], data["sibling_note"], current)
    store_check = PaymentCheck(
        name="store_claim_found", status=CheckStatus.PASSED,
        message=f"The store says this order is PAID, total {data['claim']['total']} USD.",
        evidence={"store_url": "demo", "status": "PAID"},
    )
    return Investigation(
        order_id=ORDER_ID, verdict=judged["verdict"], waiting_reason=judged["waiting_reason"], checked_at=current,
        store="demo", store_url="demo", store_claim=data["claim"],
        expected_total=investigate._money(judged["expected"]) if judged["expected"] is not None else None,
        paypal_order=judged["summary"], checks=[store_check, *judged["checks"]], leaks=judged["leaks"],
        money_at_risk=judged["money_at_risk"], currency=judged["currency"], sample_data=True,
    )


def dispute(now: datetime | None = None) -> DisputeCase:
    """A sample PayPal dispute about a duplicate charge, with the rule-based recommendation (no AI call)."""
    current = now or datetime.now(timezone.utc)
    found = investigation("duplicate_charge", current)
    case = DisputeCase(
        dispute_id="PP-D-DEMO-0001", reason="DUPLICATE_TRANSACTION", status="WAITING_FOR_SELLER_RESPONSE",
        stage="CHARGEBACK", amount="18.00", currency="USD", created_at=_stamp(current - timedelta(days=1)),
        respond_by=_stamp(current + timedelta(days=9)), capture_id="DEMOCAPTURE02", order_id=ORDER_ID, store="demo",
        store_status="PAID", paypal_capture_status="COMPLETED", leaks=found.leaks,
        allowed_actions=["provide_evidence", "accept_claim"],
    )
    case.facts = disputes._facts(case, found.store_claim)
    case.advice = disputes.builtin_advice(case, found.store_claim)
    return case
