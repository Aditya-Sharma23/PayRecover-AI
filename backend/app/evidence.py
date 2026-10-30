"""Dispute-ready evidence pack: one printable page per order for a PayPal dispute or chargeback.

The pack is generated fresh: PayRecover re-reads the store and PayPal when it is opened, adds
every recovery recorded for the order, and ends with a SHA-256 fingerprint of that evidence so
a later change to it can be detected. Every value is HTML-escaped, and no buyer details appear.
"""

import hashlib
import html
import json
from datetime import datetime, timezone
from typing import Any

from app import investigate, recovery
from app.schemas import InvestigationRequest

STATUS_STYLE = {"passed": "ok", "failed": "bad", "error": "bad", "warning": "warn", "skipped": "muted"}
RECOVERY_WORDS = {"recovered": "Recovered", "invoice_sent": "Invoice sent", "refunded": "Refunded",
                  "unverified": "Not confirmed", "failed": "Failed"}
CLAIM_FIELDS = (("status", "Status"), ("total", "Total"), ("currency", "Currency"), ("capture_id", "Capture"),
                ("cart_id", "Cart"), ("created_at", "Created"))
PAYPAL_FIELDS = (("status", "Status"), ("amount", "Order amount"), ("capture_status", "Capture status"),
                 ("capture_amount", "Captured"), ("capture_id", "Capture"), ("capture_count", "Captures"),
                 ("create_time", "Created"), ("update_time", "Last updated"))


def _e(value: Any) -> str:
    return html.escape("—" if value is None or value == "" else str(value))


def _table(record: dict | None, fields: tuple, empty: str) -> str:
    if not record:
        return f"<p class='muted'>{_e(empty)}</p>"
    rows = "".join(f"<tr><th>{_e(label)}</th><td>{_e(record.get(key))}</td></tr>" for key, label in fields)
    return f"<table class='kv'>{rows}</table>"


def fingerprint(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


async def build(order_id: str, store: str, owner: str) -> tuple[str, str]:
    """Return (html, fingerprint) for one order, from a fresh read of the store and PayPal."""
    result = await investigate.investigate(InvestigationRequest(
        order_id=order_id, store=store, probe_store=False, website_checks=False, xss_probe=False, explain=False,
    ))
    recoveries = [r.model_dump(mode="json") for r in recovery.list_recoveries(owner)["records"] if r.order_id == order_id]
    evidence = {"investigation": result.model_dump(mode="json"), "recoveries": recoveries}
    digest = fingerprint(evidence)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    verdict = {"healthy": ("Healthy", "ok"), "leaking": ("Money leak found", "bad"),
               "review": ("Needs review", "warn")}[result.verdict]

    checks = "".join(
        f"<tr><td>{_e(c.name.replace('_', ' '))}</td><td class='{STATUS_STYLE.get(c.status, '')}'>{_e(c.status)}</td>"
        f"<td>{_e(c.message)}</td></tr>"
        for c in result.checks
    )
    leaks = "".join(
        f"<div class='box'><h3>{_e(leak.title)} <span class='bad'>{_e(leak.amount)} {_e(leak.currency)}</span></h3>"
        f"<p><b>Cause:</b> {_e(leak.root_cause)}</p><p><b>Recovery:</b> {_e(leak.recovery)}</p>"
        f"<p class='muted'>Urgency: {_e(leak.urgency)}"
        + (f" · waiting since {_e(leak.waiting_since.isoformat())}" if leak.waiting_since else "")
        + "".join(f" · target order {_e(t.order_id)}" + (f", capture {_e(t.capture_id)}" if t.capture_id else "")
                  for t in leak.targets)
        + "</p></div>"
        for leak in result.leaks
    ) or "<p class='ok'>No money leak: the store's claim matches PayPal's record.</p>"
    timeline = "".join(
        f"<div class='box'><h3>{_e(RECOVERY_WORDS.get(r['status'], r['status']))}: {_e(r['action'])} "
        f"{_e(r['amount'])} {_e(r['currency'])}</h3><p class='muted'>{_e(r['created_at'])} · approved by the merchant · "
        f"PayPal reference {_e(json.dumps(r['paypal']))}</p><p>{_e(r['message'])}</p><ul>"
        + "".join(f"<li class='{STATUS_STYLE.get(c['status'], '')}'>{_e(c['message'])}</li>" for c in r["verification"])
        + "</ul></div>"
        for r in recoveries
    ) or "<p class='muted'>No recovery has been approved for this order.</p>"

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PayRecover evidence pack · {_e(order_id)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:860px;margin:32px auto;padding:0 18px;color:#1d2433;line-height:1.45}}
h1{{margin:0 0 4px;font-size:24px}}h2{{margin:28px 0 8px;font-size:17px;border-bottom:1px solid #dde1ea;padding-bottom:4px}}
h3{{margin:0 0 6px;font-size:15px}}table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid #eef0f5;vertical-align:top}}.kv th{{width:38%;color:#4a5068}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.box{{border:1px solid #dde1ea;border-radius:8px;padding:10px 14px;margin:8px 0}}
.ok{{color:#1d6b37}}.bad{{color:#a2262e}}.warn{{color:#8a5a00}}.muted{{color:#5b6276;font-size:13px}}
.verdict{{font-size:20px;font-weight:700}}code,.fp{{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}}
@media print{{body{{margin:0}}.noprint{{display:none}}}}@media(max-width:640px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<p class="muted">PayRecover AI · evidence pack · PayPal Sandbox</p>
<h1>Order {_e(order_id)}</h1>
<p class="muted">Store: {_e(store)} ({_e(result.store_url)}) · generated {generated} from a fresh read of the store and PayPal</p>
<p class="verdict {verdict[1]}">{_e(verdict[0])} · money at risk {_e(result.money_at_risk)} {_e(result.currency)}</p>
<p class="noprint muted">Use your browser's Print → Save as PDF to attach this pack to a PayPal dispute or chargeback.</p>
<h2>What each side recorded</h2>
<div class="grid"><div><h3>The store's claim</h3>{_table(result.store_claim, CLAIM_FIELDS, "The store has no record of this order.")}</div>
<div><h3>PayPal's record</h3>{_table(result.paypal_order, PAYPAL_FIELDS, "PayPal has no record of this order.")}</div></div>
<h2>Checks</h2><table><tr><th>Check</th><th>Result</th><th>Evidence</th></tr>{checks}</table>
<h2>Leaks</h2>{leaks}
<h2>Recoveries</h2>{timeline}
<h2>Integrity</h2>
<p class="muted">SHA-256 of this evidence (investigation and recoveries, as JSON with sorted keys):</p>
<p class="fp">{digest}</p>
<p class="muted">Payment states come from PayPal's Orders and Payments APIs; the AI never decided any result.
Buyer details are not included. Sandbox data only.</p>
</body></html>"""
    return page, digest
