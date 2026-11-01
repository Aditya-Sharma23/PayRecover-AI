"""PayPal webhooks: check each payment and dispute the moment PayPal reports it.

Every event is verified with PayPal (verify-webhook-signature, against TESTPILOT_PAYPAL_WEBHOOK_ID)
before anything is read from it; an unverified event is refused. Events only start checks and
alerts. They never move money, so recoveries and dispute answers still wait for the merchant.

  PAYMENT.CAPTURE.COMPLETED  re-check the order in its store (tagged by custom_id); alert on a leak,
                             or when the store never recorded the payment.
  CUSTOMER.DISPUTE.CREATED   gather the evidence, get the AI's recommended answer, and alert.
"""

import asyncio
import os
from typing import Any

from app import disputes, investigate, notify, paypal, unrecorded
from app.schemas import InvestigationRequest, UnrecordedPayment

STORE_BY_TAG = {tag: store for store, tag in unrecorded.STORE_TAGS.items()}
# The store saves the order right after PayPal confirms the capture; give it a moment first.
STORE_SETTLE_SECONDS = 5
SIGNATURE_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}
HANDLED_EVENTS = ("PAYMENT.CAPTURE.COMPLETED", "CUSTOMER.DISPUTE.CREATED")


class WebhookNotConfigured(RuntimeError):
    """TESTPILOT_PAYPAL_WEBHOOK_ID is not set, so events cannot be verified."""


class WebhookRejected(ValueError):
    """PayPal did not confirm that this event came from PayPal."""


def webhook_id() -> str | None:
    return os.environ.get("TESTPILOT_PAYPAL_WEBHOOK_ID", "").strip() or None


def status() -> dict[str, Any]:
    return {"configured": webhook_id() is not None, "events": list(HANDLED_EVENTS)}


async def verify(headers: dict[str, str], event: dict[str, Any]) -> None:
    """Ask PayPal whether it really sent this event. Raises WebhookRejected if not."""
    configured = webhook_id()
    if configured is None:
        raise WebhookNotConfigured("PayPal webhooks are not set up. Set TESTPILOT_PAYPAL_WEBHOOK_ID.")
    lowered = {key.lower(): value for key, value in headers.items()}
    fields = {name: lowered.get(header) for name, header in SIGNATURE_HEADERS.items()}
    if not all(fields.values()):
        raise WebhookRejected("The request does not carry PayPal's signature headers.")
    status_code, body = await paypal.paypal_call("POST", "/v1/notifications/verify-webhook-signature",
                                                 {**fields, "webhook_id": configured, "webhook_event": event})
    if status_code >= 400 or not isinstance(body, dict) or body.get("verification_status") != "SUCCESS":
        raise WebhookRejected("PayPal did not confirm this event's signature.")


async def on_capture(resource: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    """Check the captured order in its store; return the alerts to send."""
    store = STORE_BY_TAG.get(str(resource.get("custom_id") or ""))
    order_id = ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
    if store is None or not order_id:
        return []  # not a payment from a PayRecover-watched store
    await asyncio.sleep(STORE_SETTLE_SECONDS)
    result = await investigate.investigate(InvestigationRequest(
        order_id=order_id, store=store, probe_store=False, website_checks=False, xss_probe=False, explain=False,
    ))
    claim = result.store_claim or {}
    if claim.get("status") != "PAID" or claim.get("capture_id") != resource.get("id"):
        amount = resource.get("amount") or {}
        return [notify.unrecorded_event(UnrecordedPayment(
            transaction_id=str(resource.get("id")), amount=str(amount.get("value")),
            currency=amount.get("currency_code", "USD"), paid_at=resource.get("create_time"), store=store,
        ))]
    return [notify.leak_event(result, owner)] if result.leaks else []


async def on_dispute(resource: dict[str, Any]) -> list[dict[str, Any]]:
    dispute_id = resource.get("dispute_id")
    if not dispute_id:
        return []
    return [notify.dispute_event(await disputes.get_case(dispute_id))]


async def handle(event: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    """Run the checks for one verified event and send its alerts. Returns the alerts sent."""
    kind = event.get("event_type")
    resource = event.get("resource") or {}
    if kind == "PAYMENT.CAPTURE.COMPLETED":
        alerts = await on_capture(resource, owner)
    elif kind == "CUSTOMER.DISPUTE.CREATED":
        alerts = await on_dispute(resource)
    else:
        return []
    for alert in alerts:
        await notify.send(alert)
    return alerts
