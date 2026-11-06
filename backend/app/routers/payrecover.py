"""PayRecover endpoints: PayPal checks, scans, recoveries, disputes, the assistant, alerts, MCP and approvals."""

import html
import logging
import os
from typing import Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from app.schemas import (
    AgentReply,
    StorePing,
    DemoScenario,
    PayPalSetup,
    PriceCheckReport,
    Timeline,
    AutoscanSettings,
    AutoscanStatus,
    Overview,
    AlertItem,
    AlertSettings,
    AlertsView,
    AgentRequest,
    DisputeCase,
    DisputeList,
    DisputeResponseRequest,
    DisputeResponseResult,
    UnrecordedReport,
    Investigation,
    InvestigationRequest,
    PaymentVerification,
    PaymentVerificationRequest,
    RecoveryList,
    RecoveryRecord,
    RecoveryRequest,
    ScanRequest,
    ScanResult,
    WebsiteRunReport,
    WebsiteRunRequest,
    JobType,
)
from app.ai import (
    AIConfigurationError,
    AIProviderError,
)
from app import (agent, alerts, autoscan, demo, disputes, mcp_server, overview, paypal_setup, price_check, store_security,
                 timeline, evidence, investigate, notify, paypal, paypal_toolkit, recovery, unrecorded, webhooks)
from app.security import request_owner
from app.schemas import STORE_KEYS
from app.routers.qa import _run

router = APIRouter()


@router.get("/api/paypal/status", tags=["PayPal"])
def paypal_status() -> dict[str, str | bool]:
    """Return non-secret PayPal Sandbox configuration state."""
    return paypal.configuration_status()


@router.post("/api/paypal/verify", response_model=PaymentVerification, tags=["PayPal"])
async def paypal_verify(request: PaymentVerificationRequest) -> PaymentVerification:
    """Read one order from the PayPal Sandbox Orders API and check it against expectations.

    This is the only source PayRecover trusts for payment state: not the store's
    confirmation page, and not the AI model.
    """
    try:
        return await paypal.verify_order(request)
    except paypal.PayPalConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except paypal.PayPalProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/orders/investigate", response_model=Investigation, tags=["PayRecover"])
async def investigate_order(request: InvestigationRequest, http: Request, background: BackgroundTasks) -> Investigation:
    """Run every check for one order id: the store's claim, PayPal's record, store probes,
    a full website scan of the store, and leaks.

    Leaks and money at risk come from the checks alone; the AI model only explains them. The
    website scan is saved to run history like any other run.
    """
    owner = request_owner(http)

    async def run_website(website: WebsiteRunRequest) -> WebsiteRunReport:
        return await _run(JobType.WEBSITE, website, owner)

    try:
        result = await investigate.investigate(request, run_website)
    except paypal.PayPalConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except paypal.PayPalProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Sent after the response, so a slow or failing alert channel never delays or fails the investigation.
    security = store_security.from_investigation(result)
    if result.leaks:
        background.add_task(alerts.deliver, notify.leak_event(result, owner), owner)
    elif security.findings:
        background.add_task(alerts.deliver, notify.vulnerable_event(
            result.store, security, f"PayRecover checked order {result.order_id}."), owner)
    return result


@router.post("/api/stores/scan", response_model=ScanResult, tags=["PayRecover"])
async def scan_store(request: ScanRequest, http: Request, background: BackgroundTasks) -> ScanResult:
    """Check every recent paid order of a store against PayPal and plan the recovery.

    Leaks are ranked by urgency, then amount; Gemini only words the plan for that order.
    """
    try:
        result, _ = await autoscan.full_scan(request)
    except investigate.StoreUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except paypal.PayPalConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except paypal.PayPalProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # A manual scan always alerts: red when money is leaking, orange when the store has holes, nothing when clear.
    event = autoscan.alert_for(result)
    if event:
        background.add_task(alerts.deliver, event, request_owner(http))
    return result


@router.get("/api/orders/{order_id}/evidence", response_class=HTMLResponse, tags=["PayRecover"])
async def evidence_pack(
    http: Request,
    order_id: str = PathParam(pattern=r"^[A-Z0-9]{8,40}$"),
    store: Literal[STORE_KEYS] = "main",
) -> HTMLResponse:
    """A printable, dispute-ready evidence pack for one order, from a fresh read of the store and PayPal."""
    try:
        page, digest = await evidence.build(order_id, store, request_owner(http))
    except paypal.PayPalConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except paypal.PayPalProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return HTMLResponse(page, headers={"X-Evidence-SHA256": digest, "Cache-Control": "no-store"})


@router.post("/api/recoveries", response_model=RecoveryRecord, tags=["PayRecover"])
async def approve_recovery(request: RecoveryRequest, http: Request, background: BackgroundTasks) -> RecoveryRecord:
    """Recover one leak after the merchant approves: capture, refund or invoice in PayPal Sandbox.

    The leak and its amount are recomputed from the store and PayPal; the result is decided
    by a fresh read of PayPal, and a recovery already done is returned instead of repeated.
    """
    try:
        owner = request_owner(http)
        record = await recovery.recover(request, owner)
    except recovery.RecoveryNotPossible as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except paypal.PayPalConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except paypal.PayPalProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    background.add_task(alerts.deliver, notify.recovery_event(record), owner)
    if record.status in recovery.DONE_STATUSES:
        background.add_task(overview.refresh_after_recovery, record.store)
    return record


@router.get("/api/recoveries", response_model=RecoveryList, tags=["PayRecover"])
def list_recoveries(http: Request) -> dict:
    """The caller's recoveries, newest first, with totals collected, invoiced and refunded."""
    return recovery.list_recoveries(request_owner(http))


@router.delete("/api/recoveries", tags=["PayRecover"])
def clear_recoveries(http: Request) -> dict[str, int]:
    """Delete the caller's recovery log, e.g. test runs before a demo. Nothing at PayPal is undone."""
    return {"deleted": recovery.clear_recoveries(request_owner(http))}


@router.get("/api/stores/{store}/unrecorded", response_model=UnrecordedReport, tags=["PayRecover"])
async def unrecorded_payments(store: Literal[STORE_KEYS]) -> UnrecordedReport:
    """Payments PayPal completed for this store that the store never recorded (read with PayPal's Agent Toolkit)."""
    try:
        return await unrecorded.find(store)
    except paypal.PayPalConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _toolkit_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, (paypal.PayPalConfigurationError, paypal_toolkit.ToolkitUnavailable)):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, paypal_toolkit.ToolkitError) and exc.status == 404:
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


TOOLKIT_ERRORS = (paypal.PayPalConfigurationError, paypal.PayPalProviderError, paypal_toolkit.ToolkitUnavailable,
                  paypal_toolkit.ToolkitError)


@router.get("/api/disputes", response_model=DisputeList, tags=["PayRecover"])
async def list_disputes() -> DisputeList:
    """PayPal disputes, each linked to its store order, with PayRecover's evidence and the AI's recommended answer."""
    try:
        return await disputes.list_cases()
    except TOOLKIT_ERRORS as exc:
        raise _toolkit_errors(exc) from exc


@router.get("/api/disputes/{dispute_id}", response_model=DisputeCase, tags=["PayRecover"])
async def get_dispute(dispute_id: str = PathParam(pattern=r"^[A-Za-z0-9_-]{1,255}$")) -> DisputeCase:
    """One PayPal dispute with its evidence and recommended answer."""
    try:
        return await disputes.get_case(dispute_id)
    except TOOLKIT_ERRORS as exc:
        raise _toolkit_errors(exc) from exc


@router.post("/api/disputes/{dispute_id}/respond", response_model=DisputeResponseResult, tags=["PayRecover"])
async def respond_to_dispute(
    request: DisputeResponseRequest, dispute_id: str = PathParam(pattern=r"^[A-Za-z0-9_-]{1,255}$"),
) -> DisputeResponseResult:
    """Send the merchant's approved answer to a dispute (evidence, or accept the claim), then confirm it with PayPal."""
    try:
        return await disputes.respond(dispute_id, request)
    except disputes.DisputeNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TOOLKIT_ERRORS as exc:
        raise _toolkit_errors(exc) from exc


@router.post("/api/agent", response_model=AgentReply, tags=["PayRecover"])
async def ask_payrecover(request: AgentRequest, http: Request) -> AgentReply:
    """Ask PayRecover a question. Gemini calls PayPal's Agent Toolkit and PayRecover's checks to answer.

    It can only propose money movements; each proposal needs the merchant's approval.
    """
    try:
        return await agent.answer(request, request_owner(http))
    except AIConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TOOLKIT_ERRORS as exc:
        raise _toolkit_errors(exc) from exc


async def _handle_webhook(event: dict, owner: str) -> None:
    try:
        await webhooks.handle(event, owner)
    except Exception:  # a background check must never crash the server; the event is logged instead
        logging.getLogger("payrecover.webhooks").exception("PayPal webhook %s failed", event.get("id"))


@router.post("/api/paypal/webhook", tags=["PayRecover"])
async def paypal_webhook(http: Request, background: BackgroundTasks) -> dict[str, str]:
    """PayPal's webhook: verified with PayPal, then checks the payment or dispute it reports and alerts on problems."""
    try:
        event = await http.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="The webhook body is not JSON.") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="The webhook body is not a PayPal event.")
    try:
        await webhooks.verify(dict(http.headers), event)
    except webhooks.WebhookNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except webhooks.WebhookRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (paypal.PayPalConfigurationError, paypal.PayPalProviderError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # PayPal expects a quick 200; the checks run after the response.
    background.add_task(_handle_webhook, event, request_owner(http))
    return {"status": "accepted"}


@router.get("/api/paypal/status", tags=["PayRecover"])
def paypal_features() -> dict:
    """Which PayPal features PayRecover can use here: the Agent Toolkit and verified webhooks."""
    return {"agent_toolkit": paypal_toolkit.available(), "webhooks": webhooks.status()}


@router.post("/api/stores/start", response_model=list[StorePing], tags=["PayRecover"])
async def start_stores() -> list[StorePing]:
    """Start every demo store. A free host sleeps idle stores; this wakes them all at once before a scan."""
    return await overview.start_stores()


@router.get("/api/overview", response_model=Overview, tags=["PayRecover"])
async def dashboard_overview(http: Request) -> Overview:
    """Headline numbers (orders scanned, money at risk, open disputes, money recovered) and each store's latest result."""
    result = await overview.build(request_owner(http))
    result.autoscan = autoscan.status()
    return result


@router.get("/api/paypal/setup", response_model=PayPalSetup, tags=["PayRecover"])
async def paypal_setup_check(refresh: bool = False) -> PayPalSetup:
    """Which PayPal Sandbox app features work for these keys (Transaction search, Disputes, Invoicing), and how to
    switch on the rest. Asked of PayPal itself, cached two minutes."""
    return await paypal_setup.check(refresh)


@router.get("/api/demo/scenarios", response_model=list[DemoScenario], tags=["PayRecover"])
def demo_scenarios() -> list[DemoScenario]:
    """Made-up orders that show each result. They run through the real rules; no store or PayPal is involved."""
    return demo.scenarios()


@router.get("/api/demo/scenarios/{scenario}", response_model=Investigation, tags=["PayRecover"])
def demo_scenario(scenario: str = PathParam(pattern=r"^[a-z_]{1,40}$")) -> Investigation:
    """One demo scenario's investigation, marked sample_data. Nothing at PayPal is read or changed."""
    try:
        return demo.investigation(scenario)
    except demo.UnknownScenario as exc:
        raise HTTPException(status_code=404, detail=f"There is no demo scenario named {scenario}.") from exc


@router.get("/api/demo/dispute", response_model=DisputeCase, tags=["PayRecover"])
def demo_dispute() -> DisputeCase:
    """A sample PayPal dispute about a duplicate charge, with PayRecover's rule-based recommendation."""
    return demo.dispute()


@router.post("/api/mcp", tags=["PayRecover"], response_model=None)
async def mcp_endpoint(http: Request) -> Response:
    """PayRecover's MCP server (Streamable HTTP, JSON replies): AI assistants call PayRecover's checks as tools.
    It can never move money; propose_recovery only says where the merchant approves."""
    origin = http.headers.get("origin")
    if origin and origin.rstrip("/") not in _allowed_origins(http):
        # Browsers send Origin; refusing other sites stops a web page from driving a local server (DNS rebinding).
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Origin not allowed."}},
                            status_code=403)
    try:
        message = await http.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}},
                            status_code=400)
    owner = request_owner(http)
    if isinstance(message, list):
        replies = [reply for reply in [await mcp_server.handle(item, owner) for item in message] if reply is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await mcp_server.handle(message, owner)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/api/mcp", tags=["PayRecover"], response_model=None, include_in_schema=False)
def mcp_stream() -> Response:
    """PayRecover's MCP server answers every request directly, so it offers no event stream."""
    return Response(status_code=405, headers={"Allow": "POST"})


def _allowed_origins(http: Request) -> set[str]:
    own = f"{http.url.scheme}://{http.headers.get('host', '')}"
    public = os.environ.get("TESTPILOT_PUBLIC_URL", "").strip().rstrip("/")
    return {own, public} - {""}


@router.get("/api/stores/{store}/prices", response_model=PriceCheckReport, tags=["PayRecover"])
async def market_prices(store: Literal[STORE_KEYS]) -> PriceCheckReport:
    """Compare each product's price with what comparable products sell for across retailers (Channel3)."""
    return await price_check.check(store)


@router.get("/api/timeline", response_model=Timeline, tags=["PayRecover"])
async def store_timeline(http: Request) -> Timeline:
    """Scans, recoveries and dispute deadlines per store, from three days back to ten days ahead."""
    return await timeline.build(request_owner(http))


@router.get("/api/autoscan", response_model=AutoscanStatus, tags=["PayRecover"])
def autoscan_status() -> AutoscanStatus:
    """Whether auto-scan is on, how often it runs, and when it last ran and runs next."""
    return autoscan.status()


@router.put("/api/autoscan", response_model=AutoscanStatus, tags=["PayRecover"])
def set_autoscan(settings: AutoscanSettings) -> AutoscanStatus:
    """Turn auto-scan on or off and choose how often it scans every store (5, 10, 30 or 60 minutes)."""
    return autoscan.configure(settings)


@router.get("/api/alerts", response_model=AlertsView, tags=["PayRecover"])
def list_alerts(http: Request) -> AlertsView:
    """The caller's recent alerts and where they are sent (dashboard, email, Zapier)."""
    return alerts.view(request_owner(http))


@router.put("/api/alerts/settings", response_model=AlertSettings, tags=["PayRecover"])
def save_alert_settings(settings: AlertSettings, http: Request) -> AlertSettings:
    """Save where the caller's alerts go: an email address and, optionally, their own Zapier Catch Hook."""
    try:
        return alerts.save_settings(request_owner(http), settings)
    except alerts.InvalidAlertSettings as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/alerts/test", response_model=AlertItem, tags=["PayRecover"])
async def send_test_alert(http: Request) -> AlertItem:
    """Send a test alert to the caller's dashboard, email and Zapier, to check the set-up."""
    owner = request_owner(http)
    try:
        alerts.allow_test(owner)
    except alerts.TooManyTests as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return await alerts.deliver(alerts.test_event(), owner)


@router.delete("/api/alerts", tags=["PayRecover"])
def clear_alerts(http: Request) -> dict[str, int]:
    """Clear the caller's alert feed (settings are kept)."""
    return {"deleted": alerts.clear(request_owner(http))}


@router.get("/api/notify/status", tags=["PayRecover"])
def notify_status() -> dict[str, bool]:
    """Whether the Zapier webhook and signed approval links are configured (never their values)."""
    return notify.status()


LEAK_WORDS = {
    "never_captured": "capture a payment the store never collected",
    "fake_paid": "invoice an order the store marked paid without payment",
    "wrong_amount": "invoice the buyer for the amount they underpaid",
    "duplicate_charge": "refund a duplicate charge",
    "hold_expiring": "capture a payment PayPal is holding before the hold expires",
    "hold_expired": "invoice the buyer for a payment whose hold expired",
}


def _approval_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:10vh auto;padding:0 16px;color:#20263a;background:#f7f8fb}}
main{{padding:24px;border:1px solid #e1e4ec;border-radius:12px;background:#fff}}button{{padding:12px 20px;border:0;border-radius:999px;
background:#ed2938;color:#fff;font-weight:700;font-size:15px;cursor:pointer}}.ok{{color:#1d6b37}}.bad{{color:#a24045}}small{{color:#4a5068}}</style>
</head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
    return HTMLResponse(page, status_code=status_code, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/approve", response_class=HTMLResponse, include_in_schema=False)
def approval_form(token: str = "") -> HTMLResponse:
    """The page a Zapier approval link opens. It only asks; nothing happens until the button is pressed."""
    try:
        approval = notify.read_approval(token)
    except notify.InvalidApproval as exc:
        return _approval_page("Approval link not valid", f"<p class='bad'>{html.escape(str(exc))}</p>", 400)
    what = LEAK_WORDS.get(approval["kind"], approval["kind"])
    return _approval_page("Approve this recovery?", f"""
<p>PayRecover will <strong>{html.escape(what)}</strong> for order <code>{html.escape(approval['order_id'])}</code>
({html.escape(approval['store'])} store) in PayPal <strong>Sandbox</strong>.</p>
<p><small>The amount is recalculated from the store and PayPal when you approve, and PayRecover checks PayPal again
to confirm it worked. If the leak is already fixed, nothing happens.</small></p>
<form method="post" action="/approve"><input type="hidden" name="token" value="{html.escape(token)}">
<button type="submit">Approve recovery</button></form>""")


@router.post("/approve", response_class=HTMLResponse, include_in_schema=False)
async def approval_submit(http: Request, background: BackgroundTasks) -> HTMLResponse:
    """Run the approved recovery for the owner named in the signed token."""
    form = parse_qs((await http.body()).decode("utf-8", errors="replace"))
    try:
        approval = notify.read_approval((form.get("token") or [""])[0])
        record = await recovery.recover(RecoveryRequest(
            order_id=approval["order_id"], store=approval["store"], kind=approval["kind"], approved=True,
        ), approval["owner"])
    except notify.InvalidApproval as exc:
        return _approval_page("Approval link not valid", f"<p class='bad'>{html.escape(str(exc))}</p>", 400)
    except recovery.RecoveryNotPossible as exc:
        return _approval_page("Nothing to recover", f"<p>{html.escape(str(exc))}</p>", 409)
    except (paypal.PayPalConfigurationError, paypal.PayPalProviderError) as exc:
        return _approval_page("PayPal could not be reached", f"<p class='bad'>{html.escape(str(exc))}</p>", 502)
    background.add_task(alerts.deliver, notify.recovery_event(record), approval["owner"])
    if record.status in recovery.DONE_STATUSES:
        background.add_task(overview.refresh_after_recovery, record.store)
    tone = "ok" if record.status in recovery.DONE_STATUSES else "bad"
    checks = "".join(f"<li>{'✓' if c.status == 'passed' else '✗'} {html.escape(c.message)}</li>" for c in record.verification)
    return _approval_page("Recovery done" if tone == "ok" else "Recovery not completed",
                          f"<p class='{tone}'><strong>{html.escape(record.message)}</strong></p><ul>{checks}</ul>")


