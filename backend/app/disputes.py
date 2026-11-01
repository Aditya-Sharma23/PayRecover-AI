"""PayRecover's dispute defender: answer PayPal disputes with evidence, after the merchant approves.

For each open dispute, PayPal's Agent Toolkit reads the dispute; PayRecover follows the disputed
capture to its order and store, re-runs the order's checks, and Gemini recommends whether to
send evidence or accept the claim, with a draft note. The recommendation is advice only:
  * the merchant edits the note and presses Approve (approved: true);
  * only the actions PayPal itself offers for that dispute (its HATEOAS links) are allowed;
  * the dispute is re-read from PayPal right before acting, and again afterwards to confirm.
"""

from typing import Any

from app import ai, investigate, paypal, paypal_toolkit
from app.schemas import (
    DisputeAdvice,
    DisputeCase,
    DisputeList,
    DisputeResponseRequest,
    DisputeResponseResult,
    InvestigationRequest,
)

MAX_DISPUTES = 10
STORE_TAGS = {"payrecover-main": "main", "payrecover-vulnerable": "vulnerable"}
# PayPal's link names for each action PayRecover can take.
ACTION_LINKS = {"provide_evidence": "provide_evidence", "accept_claim": "accept_claim"}
DISPUTES_HELP = (
    "PayPal refused to list disputes. Turn on Disputes for your Sandbox app: developer.paypal.com > "
    "Apps & Credentials > your app > Features > Disputes, then save."
)
REASON_WORDS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "the buyer says the item never arrived",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "the buyer says the item was not as described",
    "UNAUTHORISED": "the buyer says they did not make this payment",
    "CREDIT_NOT_PROCESSED": "the buyer says a promised refund never came",
    "DUPLICATE_TRANSACTION": "the buyer says they were charged twice",
    "INCORRECT_AMOUNT": "the buyer says they were charged the wrong amount",
    "PAYMENT_BY_OTHER_MEANS": "the buyer says they paid another way",
    "CANCELED_RECURRING_BILLING": "the buyer says a cancelled subscription was charged",
}


class DisputeNotActionable(RuntimeError):
    """PayPal does not offer this action for the dispute now (already answered, closed, or the wrong stage)."""


def _allowed_actions(dispute: dict[str, Any]) -> list[str]:
    rels = {link.get("rel") for link in dispute.get("links") or [] if isinstance(link, dict)}
    return [action for action, rel in ACTION_LINKS.items() if rel in rels]


async def _capture_details(capture_id: str) -> dict[str, Any]:
    status, body = await paypal.paypal_call("GET", f"/v2/payments/captures/{capture_id}")
    return body if status < 400 and isinstance(body, dict) else {}


def _facts(case: DisputeCase, claim: dict | None) -> list[str]:
    facts = [f"PayPal dispute {case.dispute_id}: {REASON_WORDS.get(case.reason or '', case.reason or 'no reason given')}; "
             f"{case.amount} {case.currency} disputed; status {case.status}, stage {case.stage}."]
    if case.paypal_capture_status:
        facts.append(f"PayPal shows the disputed capture {case.capture_id} as {case.paypal_capture_status}.")
    if case.order_id is None:
        facts.append("PayPal does not link the disputed payment to an order PayRecover can read.")
    elif claim is None:
        facts.append(f"The {case.store} store has no record of order {case.order_id}, although PayPal took the payment.")
    else:
        facts.append(f"The {case.store} store records order {case.order_id} as {claim.get('status')}, "
                     f"total {claim.get('total')} {claim.get('currency')}.")
    for leak in case.leaks:
        facts.append(f"PayRecover found a leak on this order: {leak.title} ({leak.amount} {leak.currency}).")
    return facts


def builtin_advice(case: DisputeCase, claim: dict | None, reason: str | None = None) -> DisputeAdvice:
    """A rule-based recommendation, used when the AI model is unavailable."""
    duplicate = any(leak.kind == "duplicate_charge" and any(t.capture_id == case.capture_id for t in leak.targets)
                    for leak in case.leaks)
    store_paid = bool(claim) and claim.get("status") == "PAID"
    if duplicate or (case.order_id and claim is None):
        recommendation = "accept_claim"
        why = ("PayPal charged this buyer twice for one cart, so the buyer is right." if duplicate else
               "The store never recorded this order, so it was almost certainly never shipped.")
        note = ("We have reviewed this payment and agree with the buyer. Please refund the disputed amount. "
                "We apologise for the inconvenience.")
    elif store_paid and not case.leaks:
        recommendation = "provide_evidence"
        why = "The store records a paid order that matches PayPal's capture, with no leak found."
        note = (f"Order {case.order_id} was placed and paid through PayPal (capture {case.capture_id}, "
                f"{case.amount} {case.currency}) and is recorded as paid and fulfilled by the store. "
                "The payment is genuine and matches the order.")
    else:
        recommendation = "review"
        why = "The evidence is mixed; check the order and shipping records before answering."
        note = f"We are reviewing order {case.order_id} and will respond with the order and delivery records."
    reasoning = [why] + ([f"AI unavailable: {reason}"] if reason else [])
    return DisputeAdvice(recommendation=recommendation, reasoning=reasoning, note=note, source="PayRecover rules")


def _advice_prompt(case: DisputeCase) -> str:
    return (
        "You help a merchant answer a PayPal dispute. Use only the facts below; never invent tracking numbers, "
        "dates, or documents. Choose one recommendation from: "
        + ", ".join(case.allowed_actions + ["review"])
        + ". 'accept_claim' refunds the buyer; 'provide_evidence' argues the payment was valid; 'review' means "
        "the facts are not enough to decide. Return JSON only: "
        '{"recommendation": "...", "reasoning": ["short reason", ...], '
        '"note": "the message to PayPal, polite, factual, under 1200 characters"}\n\nFacts:\n- '
        + "\n- ".join(case.facts)
    )


async def advise(case: DisputeCase, claim: dict | None) -> DisputeAdvice:
    try:
        value = ai.fit_to_limits(await ai.ask_json(_advice_prompt(case)), ("reasoning",), ("note",))
        recommendation = value.get("recommendation")
        if recommendation not in {*case.allowed_actions, "review"} or not str(value.get("note", "")).strip():
            raise ai.AIProviderError("The AI model chose an action PayPal does not offer for this dispute.")
        return DisputeAdvice(recommendation=recommendation, reasoning=value.get("reasoning") or [],
                             note=str(value["note"]).strip(), source=ai.answer_source())
    except (ai.AIConfigurationError, ai.AIProviderError) as exc:
        return builtin_advice(case, claim, str(exc)[:300])


async def build_case(dispute: dict[str, Any], with_advice: bool = True) -> DisputeCase:
    """Link one PayPal dispute to its order and store, and gather the evidence."""
    transaction = (dispute.get("disputed_transactions") or [{}])[0]
    amount = dispute.get("dispute_amount") or {}
    case = DisputeCase(
        dispute_id=dispute["dispute_id"], reason=dispute.get("reason"), status=dispute.get("status"),
        stage=dispute.get("dispute_life_cycle_stage"), amount=amount.get("value"), currency=amount.get("currency_code"),
        created_at=dispute.get("create_time"), respond_by=dispute.get("seller_response_due_date"),
        capture_id=transaction.get("seller_transaction_id"), allowed_actions=_allowed_actions(dispute),
    )
    claim = None
    if case.capture_id:
        capture = await _capture_details(case.capture_id)
        case.paypal_capture_status = capture.get("status")
        case.order_id = ((capture.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
        case.store = STORE_TAGS.get(str(capture.get("custom_id") or ""), "main")
    if case.order_id:
        try:
            found = await investigate.investigate(InvestigationRequest(
                order_id=case.order_id, store=case.store, probe_store=False, website_checks=False,
                xss_probe=False, explain=False,
            ))
            claim = found.store_claim
            case.leaks = found.leaks
        except (paypal.PayPalProviderError, paypal.PayPalConfigurationError):
            pass
        case.store_status = (claim or {}).get("status")
    case.facts = _facts(case, claim)
    if with_advice:
        case.advice = await advise(case, claim)
    return case


async def _dispute(dispute_id: str) -> dict[str, Any]:
    return await paypal_toolkit.run("get_dispute", {"dispute_id": dispute_id})


async def list_cases(limit: int = MAX_DISPUTES) -> DisputeList:
    try:
        listed = await paypal_toolkit.run("list_disputes", {"page_size": limit})
    except paypal_toolkit.ToolkitError as exc:
        if exc.status in {401, 403}:
            return DisputeList(disputes=[], note=DISPUTES_HELP)
        raise
    cases = []
    for item in (listed.get("items") or [])[:limit]:
        detail = await _dispute(item["dispute_id"])
        open_case = detail.get("status") not in {"RESOLVED", "OTHER"}
        cases.append(await build_case(detail, with_advice=open_case))
    note = None if cases else ("No disputes yet. To try this in the Sandbox, sign in to sandbox.paypal.com as the "
                               "buyer, open a payment, and report a problem with it.")
    return DisputeList(disputes=cases, note=note)


async def get_case(dispute_id: str) -> DisputeCase:
    return await build_case(await _dispute(dispute_id))


async def respond(dispute_id: str, request: DisputeResponseRequest) -> DisputeResponseResult:
    """Send the merchant's approved answer, then confirm it with a fresh read of the dispute."""
    before = await _dispute(dispute_id)
    if request.action not in _allowed_actions(before):
        raise DisputeNotActionable(
            f"PayPal does not offer '{request.action.replace('_', ' ')}' for dispute {dispute_id} now "
            f"(status {before.get('status')}). It may already be answered or closed."
        )
    if request.action == "accept_claim":
        await paypal_toolkit.run("accept_dispute_claim", {"dispute_id": dispute_id, "note": request.note})
    else:
        # The Sandbox accepts the OTHER evidence type with notes; live disputes add documents.
        evidence = {"evidences": [{"evidence_type": "OTHER", "notes": request.note}]}
        status, body = await paypal.paypal_multipart(f"/v1/customer/disputes/{dispute_id}/provide-evidence", evidence)
        if status >= 400:
            issue = ((body or {}).get("details") or [{}])[0].get("issue") if isinstance(body, dict) else None
            return DisputeResponseResult(
                dispute_id=dispute_id, action=request.action, status="failed",
                message=f"PayPal refused the evidence (HTTP {status}{', ' + issue if issue else ''}).",
                dispute_status_before=before.get("status"),
            )
    after = await _dispute(dispute_id)
    changed = after.get("status") != before.get("status") or request.action not in _allowed_actions(after)
    done = {"accept_claim": "PayPal accepted the claim and refunds the buyer.",
            "provide_evidence": "PayPal received the evidence and the dispute moved on."}[request.action]
    return DisputeResponseResult(
        dispute_id=dispute_id, action=request.action, status="sent" if changed else "unverified",
        message=done if changed else "PayPal accepted the request, but a fresh read does not show the change yet.",
        dispute_status_before=before.get("status"), dispute_status_after=after.get("status"),
    )

