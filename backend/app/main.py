from contextlib import asynccontextmanager
import html
import logging
import os
import sqlite3
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.schemas import (
    AgentReply,
    AlertItem,
    AlertSettings,
    AlertsView,
    AgentRequest,
    ApiRunReport,
    DisputeCase,
    DisputeList,
    DisputeResponseRequest,
    DisputeResponseResult,
    UnrecordedReport,
    ApiRunRequest,
    AIExplanation,
    AIExplanationRequest,
    AIPlan,
    AIPlanRequest,
    Investigation,
    InvestigationRequest,
    OpenApiImportRequest,
    OpenApiImportResult,
    PaymentVerification,
    PaymentVerificationRequest,
    RecoveryList,
    RecoveryRecord,
    RecoveryRequest,
    ScanRequest,
    ScanResult,
    RunComparison,
    TargetPreflightRequest,
    WebsiteRunReport,
    WebsiteRunRequest,
    JobRequest,
    JobRecord,
    JobStatus,
    JobType,
)
from app.url_policy import UnsafeTarget, validate_target_url
from app.storage import (
    artifact_owned_by,
    dashboard_stats,
    delete_all_reports,
    delete_report,
    get_artifact,
    get_report,
    list_reports,
    retention_policy,
    save_report,
)
from app.ai import (
    AIConfigurationError,
    AIProviderError,
    configuration_status,
    create_plan,
    explain_with_fallback,
)
from app import (agent, alerts, disputes, store_security, evidence, investigate, notify, paypal, paypal_toolkit,
                 recovery, scan, unrecorded, webhooks)
from app.compare import compare_reports
from app.openapi_import import OpenApiError, import_openapi
from app.report_html import render_report_html
from app.deployment import deployment_readiness, public_mode
from app.security import request_owner, security_middleware
from app.jobs import JobManager
from app.execution import overall_status, redact_target_url, run_report, summary
from app.schemas import API_CHECK_NAMES, FULL_SCAN_CHECKS, STORE_KEYS, WEBSITE_CHECK_NAMES

job_manager = JobManager()
FRONTEND_DIST = Path(
    os.environ.get("TESTPILOT_FRONTEND_DIST")
    or Path(__file__).resolve().parents[2] / "frontend" / "dist"
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    readiness = deployment_readiness()
    if not readiness["ready"]:
        # Fail closed: a public deployment with a missing safeguard never starts.
        raise RuntimeError("Public deployment blocked: " + "; ".join(readiness["blockers"]))
    JobManager.recover()
    yield
    await job_manager.close()


OPENAPI_TAGS = [
    {"name": "PayRecover", "description": "Find money a store has lost on PayPal, rank it, recover it after "
                                          "approval, and export dispute-ready evidence."},
    {"name": "PayPal", "description": "Read one PayPal Sandbox order and check it against expectations."},
    {"name": "AI", "description": "Gemini plans checks and explains results; it never decides a result."},
    {"name": "website tests", "description": "Bounded, read-only website checks."},
    {"name": "API tests", "description": "Bounded API checks and OpenAPI import."},
    {"name": "jobs", "description": "Background runs."},
    {"name": "run history", "description": "Stored runs, comparisons and reports."},
    {"name": "system", "description": "Health, readiness and caller details."},
]


def _operation_id(route) -> str:
    """Operation ids are the Python function names, so generated SDK methods read like scan_store()."""
    return route.name


app = FastAPI(
    title="PayRecover AI API",
    version="2.0.0",
    description=(
        "PayRecover finds money a store has lost on PayPal (payments never captured, marked paid without "
        "payment, charged too little, or charged twice), proves each leak against PayPal's own record, and "
        "recovers it through PayPal after the merchant approves. PayPal Sandbox only."
    ),
    openapi_tags=OPENAPI_TAGS,
    generate_unique_id_function=_operation_id,
    lifespan=lifespan,
)


def _cors_origins() -> list[str]:
    configured = os.environ.get("TESTPILOT_CORS_ORIGINS", "").strip()
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    if public_mode():
        return []
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]

app.add_middleware(BaseHTTPMiddleware, dispatch=security_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-TestPilot-API-Key"],
)


@app.get("/api/health", tags=["system"])
def health() -> dict[str, str]:
    """Return a stable, non-sensitive service health response."""
    return {"status": "ok"}


@app.get("/api/deployment/readiness", tags=["system"])
def deployment_status() -> dict[str, object]:
    """Report whether the public-deployment gates are satisfied."""
    return deployment_readiness()


@app.get("/api/me", tags=["system"])
def me(request: Request) -> dict[str, object]:
    """Return who the caller is and the policies that apply to their data."""
    return {
        "owner": request_owner(request),
        "auth_enabled": os.environ.get("TESTPILOT_AUTH_ENABLED", "").lower() == "true",
        "public_deployment": public_mode(),
        **retention_policy(),
    }


@app.get("/api/checks", tags=["system"])
def check_catalog() -> dict[str, list[str]]:
    """List every check name per test type, and the one-URL full-scan preset."""
    return {
        "website": list(WEBSITE_CHECK_NAMES),
        "api": list(API_CHECK_NAMES),
        "full_scan": list(FULL_SCAN_CHECKS),
    }


@app.get("/api/dashboard", tags=["run history"])
def dashboard(http: Request) -> dict[str, int]:
    """Totals for the dashboard cards, from the caller's stored runs."""
    return dashboard_stats(request_owner(http))


@app.get("/api/ai/status", tags=["AI"])
def ai_status() -> dict[str, str | bool]:
    """Return non-secret AI configuration state: model, family name and provider."""
    return configuration_status()


def _job_payload(request: JobRequest) -> tuple[dict, str]:
    inner = request.website if request.website is not None else request.api
    return inner.model_dump(mode="json"), redact_target_url(str(inner.url))


@app.post("/api/jobs", response_model=JobRecord, status_code=202, tags=["jobs"])
async def submit_job(request: JobRequest, http: Request) -> JobRecord:
    """Queue a bounded deterministic test without blocking the API request."""
    payload, target = _job_payload(request)
    try:
        return await job_manager.submit(request.job_type, payload, request_owner(http), target)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/jobs", response_model=list[JobRecord], tags=["jobs"])
def jobs(http: Request, limit: int = 20) -> list[JobRecord]:
    """List the caller's recent background jobs."""
    return job_manager.list(request_owner(http), limit)


@app.get("/api/jobs/{job_id}", response_model=JobRecord, tags=["jobs"])
def job_status(job_id: UUID, http: Request) -> JobRecord:
    """Return one background run's status and, once finished, its report."""
    record = job_manager.get(job_id, request_owner(http))
    if record is None:
        raise HTTPException(status_code=404, detail="Job was not found.")
    return record


@app.post("/api/jobs/{job_id}/cancel", response_model=JobRecord, tags=["jobs"])
async def cancel_job(job_id: UUID, http: Request) -> JobRecord:
    """Cancel a background run that has not finished yet."""
    record = await job_manager.cancel(job_id, request_owner(http))
    if record is None:
        raise HTTPException(status_code=404, detail="Job was not found.")
    if record.status not in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLED}:
        raise HTTPException(status_code=409, detail="Only queued or running jobs can be cancelled.")
    return record


@app.post("/api/targets/preflight", tags=["website tests"])
async def target_preflight(request: TargetPreflightRequest) -> dict[str, str | bool]:
    """Check URL policy and public DNS addresses without connecting to target."""
    try:
        await validate_target_url(str(request.url))
    except UnsafeTarget as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "eligible": True,
        "message": "URL and DNS preflight passed. No website checks were run.",
    }


_summary = summary
_overall_status = overall_status
_redact_target_url = redact_target_url


def _store(report: WebsiteRunReport | ApiRunReport, owner: str) -> None:
    try:
        save_report(report, owner)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="Run completed but could not be stored.") from exc


async def _run(job_type: JobType, request: WebsiteRunRequest | ApiRunRequest, owner: str):
    if not public_mode():
        report = await run_report(job_type, request)
        _store(report, owner)
        return report
    # Public deployments never run a browser or target request inside the API process.
    try:
        record = await job_manager.run_and_wait(
            job_type, request.model_dump(mode="json"), owner, redact_target_url(str(request.url))
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    stored = get_report(record.report_id, owner) if record.report_id else None
    if record.status != JobStatus.COMPLETED or stored is None:
        raise HTTPException(status_code=503, detail=f"The isolated worker did not complete ({record.error}).")
    report_type = WebsiteRunReport if job_type == JobType.WEBSITE else ApiRunReport
    return report_type.model_validate(stored)


@app.post("/api/tests/website", response_model=WebsiteRunReport, tags=["website tests"])
async def run_website(request: WebsiteRunRequest, http: Request) -> WebsiteRunReport:
    """Run bounded deterministic website checks and return their evidence."""
    return await _run(JobType.WEBSITE, request, request_owner(http))


@app.post("/api/tests/api", response_model=ApiRunReport, tags=["API tests"])
async def run_api(request: ApiRunRequest, http: Request) -> ApiRunReport:
    """Run one bounded authorized API GET and return observed evidence."""
    return await _run(JobType.API, request, request_owner(http))


@app.post("/api/openapi/import", response_model=OpenApiImportResult, tags=["API tests"])
def openapi_import(request: OpenApiImportRequest) -> OpenApiImportResult:
    """Turn an OpenAPI document into reviewable GET check suggestions; nothing is executed."""
    try:
        return import_openapi(request)
    except OpenApiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/artifacts/{artifact_id}", tags=["run history"])
def artifact(artifact_id: UUID, http: Request) -> Response:
    """Return one opt-in failure screenshot referenced by one of the caller's reports."""
    content = get_artifact(artifact_id) if artifact_owned_by(artifact_id, request_owner(http)) else None
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact was not found.")
    return Response(content=content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/runs", tags=["run history"])
def runs(http: Request, limit: int = 20) -> list[dict]:
    """List the caller's recent redacted run summaries."""
    return list_reports(limit, request_owner(http))


@app.delete("/api/runs", tags=["run history"])
def clear_runs(http: Request) -> dict[str, int]:
    """Delete all of the caller's stored runs and their screenshots."""
    return {"deleted": delete_all_reports(request_owner(http))}


def _owned_report(run_id: UUID, http: Request) -> dict:
    report = get_report(run_id, request_owner(http))
    if report is None:
        raise HTTPException(status_code=404, detail="Run was not found.")
    return report


@app.get("/api/runs/compare", response_model=RunComparison, tags=["run history"])
def compare_runs(base: UUID, head: UUID, http: Request) -> RunComparison:
    """Compare two stored runs check by check; nothing is re-run."""
    return compare_reports(_owned_report(base, http), _owned_report(head, http))


@app.get("/api/runs/{run_id}", tags=["run history"])
def run_detail(run_id: UUID, http: Request):
    """Return one previously stored canonical report."""
    return _owned_report(run_id, http)


@app.delete("/api/runs/{run_id}", tags=["run history"])
def remove_run(run_id: UUID, http: Request) -> dict[str, bool]:
    """Delete one stored run and its screenshot."""
    if not delete_report(run_id, request_owner(http)):
        raise HTTPException(status_code=404, detail="Run was not found.")
    return {"deleted": True}


@app.get("/api/runs/{run_id}/html", response_class=HTMLResponse, tags=["run history"])
def run_html(run_id: UUID, http: Request) -> str:
    """Render one stored report as escaped, evidence-only HTML."""
    return render_report_html(_owned_report(run_id, http))


@app.post("/api/ai/plan", response_model=AIPlan, tags=["AI"])
async def ai_plan(request: AIPlanRequest) -> AIPlan:
    """Use the AI model to structure a goal without executing or inventing evidence."""
    try:
        return await create_plan(request)
    except AIConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/ai/explain", response_model=AIExplanation, tags=["AI"])
async def ai_explain(request: AIExplanationRequest) -> AIExplanation:
    """Explain a report with the AI model, or with a built-in summary when it is unavailable.

    Neither can change the report's result, and the risk level never drops below
    what the check statuses imply.
    """
    return await explain_with_fallback(request.report.model_dump(mode="json"))


@app.get("/api/paypal/status", tags=["PayPal"])
def paypal_status() -> dict[str, str | bool]:
    """Return non-secret PayPal Sandbox configuration state."""
    return paypal.configuration_status()


@app.post("/api/paypal/verify", response_model=PaymentVerification, tags=["PayPal"])
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


@app.post("/api/orders/investigate", response_model=Investigation, tags=["PayRecover"])
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


@app.post("/api/stores/scan", response_model=ScanResult, tags=["PayRecover"])
async def scan_store(request: ScanRequest, http: Request, background: BackgroundTasks) -> ScanResult:
    """Check every recent paid order of a store against PayPal and plan the recovery.

    Leaks are ranked by urgency, then amount; Gemini only words the plan for that order.
    """
    try:
        result = await scan.scan_store(request)
    except investigate.StoreUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except paypal.PayPalConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except paypal.PayPalProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if request.check_paypal_side:
        result.unrecorded = await unrecorded.find(request.store)
    if request.check_security:
        result.security = await store_security.check(request.store)
    result.verdict = store_security.verdict(bool(result.leaking_orders), result.security)
    # Red when money is leaking, orange when it is not but the store has holes; an all-clear scan sends nothing.
    if result.verdict == "leaking":
        background.add_task(alerts.deliver, notify.scan_event(result), request_owner(http))
    elif result.verdict == "vulnerable":
        background.add_task(alerts.deliver, notify.vulnerable_event(
            result.store, result.security, f"PayRecover checked {result.orders_checked} paid order(s)."),
            request_owner(http))
    return result


@app.get("/api/orders/{order_id}/evidence", response_class=HTMLResponse, tags=["PayRecover"])
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


@app.post("/api/recoveries", response_model=RecoveryRecord, tags=["PayRecover"])
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
    return record


@app.get("/api/recoveries", response_model=RecoveryList, tags=["PayRecover"])
def list_recoveries(http: Request) -> dict:
    """The caller's recoveries, newest first, with totals collected, invoiced and refunded."""
    return recovery.list_recoveries(request_owner(http))


@app.delete("/api/recoveries", tags=["PayRecover"])
def clear_recoveries(http: Request) -> dict[str, int]:
    """Delete the caller's recovery log, e.g. test runs before a demo. Nothing at PayPal is undone."""
    return {"deleted": recovery.clear_recoveries(request_owner(http))}


@app.get("/api/stores/{store}/unrecorded", response_model=UnrecordedReport, tags=["PayRecover"])
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


@app.get("/api/disputes", response_model=DisputeList, tags=["PayRecover"])
async def list_disputes() -> DisputeList:
    """PayPal disputes, each linked to its store order, with PayRecover's evidence and the AI's recommended answer."""
    try:
        return await disputes.list_cases()
    except TOOLKIT_ERRORS as exc:
        raise _toolkit_errors(exc) from exc


@app.get("/api/disputes/{dispute_id}", response_model=DisputeCase, tags=["PayRecover"])
async def get_dispute(dispute_id: str = PathParam(pattern=r"^[A-Za-z0-9_-]{1,255}$")) -> DisputeCase:
    """One PayPal dispute with its evidence and recommended answer."""
    try:
        return await disputes.get_case(dispute_id)
    except TOOLKIT_ERRORS as exc:
        raise _toolkit_errors(exc) from exc


@app.post("/api/disputes/{dispute_id}/respond", response_model=DisputeResponseResult, tags=["PayRecover"])
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


@app.post("/api/agent", response_model=AgentReply, tags=["PayRecover"])
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


@app.post("/api/paypal/webhook", tags=["PayRecover"])
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


@app.get("/api/paypal/status", tags=["PayRecover"])
def paypal_features() -> dict:
    """Which PayPal features PayRecover can use here: the Agent Toolkit and verified webhooks."""
    return {"agent_toolkit": paypal_toolkit.available(), "webhooks": webhooks.status()}


@app.get("/api/alerts", response_model=AlertsView, tags=["PayRecover"])
def list_alerts(http: Request) -> AlertsView:
    """The caller's recent alerts and where they are sent (dashboard, email, Zapier)."""
    return alerts.view(request_owner(http))


@app.put("/api/alerts/settings", response_model=AlertSettings, tags=["PayRecover"])
def save_alert_settings(settings: AlertSettings, http: Request) -> AlertSettings:
    """Save where the caller's alerts go: an email address and, optionally, their own Zapier Catch Hook."""
    try:
        return alerts.save_settings(request_owner(http), settings)
    except alerts.InvalidAlertSettings as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/alerts/test", response_model=AlertItem, tags=["PayRecover"])
async def send_test_alert(http: Request) -> AlertItem:
    """Send a test alert to the caller's dashboard, email and Zapier, to check the set-up."""
    owner = request_owner(http)
    try:
        alerts.allow_test(owner)
    except alerts.TooManyTests as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return await alerts.deliver(alerts.test_event(), owner)


@app.delete("/api/alerts", tags=["PayRecover"])
def clear_alerts(http: Request) -> dict[str, int]:
    """Clear the caller's alert feed (settings are kept)."""
    return {"deleted": alerts.clear(request_owner(http))}


@app.get("/api/notify/status", tags=["PayRecover"])
def notify_status() -> dict[str, bool]:
    """Whether the Zapier webhook and signed approval links are configured (never their values)."""
    return notify.status()


LEAK_WORDS = {
    "never_captured": "capture a payment the store never collected",
    "fake_paid": "invoice an order the store marked paid without payment",
    "wrong_amount": "invoice the buyer for the amount they underpaid",
    "duplicate_charge": "refund a duplicate charge",
}


def _approval_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:10vh auto;padding:0 16px;color:#20263a;background:#f7f8fb}}
main{{padding:24px;border:1px solid #e1e4ec;border-radius:12px;background:#fff}}button{{padding:12px 20px;border:0;border-radius:999px;
background:#ed2938;color:#fff;font-weight:700;font-size:15px;cursor:pointer}}.ok{{color:#1d6b37}}.bad{{color:#a24045}}small{{color:#4a5068}}</style>
</head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
    return HTMLResponse(page, status_code=status_code, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/approve", response_class=HTMLResponse, include_in_schema=False)
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


@app.post("/approve", response_class=HTMLResponse, include_in_schema=False)
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
    tone = "ok" if record.status in recovery.DONE_STATUSES else "bad"
    checks = "".join(f"<li>{'✓' if c.status == 'passed' else '✗'} {html.escape(c.message)}</li>" for c in record.verification)
    return _approval_page("Recovery done" if tone == "ok" else "Recovery not completed",
                          f"<p class='{tone}'><strong>{html.escape(record.message)}</strong></p><ul>{checks}</ul>")


def _drop_field_titles(node, top: bool = False) -> None:
    """Remove the per-field titles pydantic adds; SDK generators read them as clashing type names."""
    if isinstance(node, dict):
        if not top and isinstance(node.get("title"), str):
            del node["title"]
        for value in node.values():
            _drop_field_titles(value)
    elif isinstance(node, list):
        for value in node:
            _drop_field_titles(value)


def _openapi() -> dict:
    """The OpenAPI description, with a server address and the optional token login, for SDK generators."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title=app.title, version=app.version, description=app.description,
                         routes=app.routes, tags=OPENAPI_TAGS)
    schema["servers"] = [{"url": os.environ.get("TESTPILOT_PUBLIC_URL", "").strip().rstrip("/") or "http://127.0.0.1:8000",
                          "description": "PayRecover backend"}]
    schema.setdefault("components", {})["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer",
                       "description": "API token; required only when TESTPILOT_AUTH_ENABLED=true."},
    }
    # An empty requirement first: a local server without authentication accepts calls with no token.
    schema["security"] = [{}, {"bearerAuth": []}]
    for model in schema["components"].get("schemas", {}).values():
        _drop_field_titles(model, top=True)
    _drop_field_titles(schema["paths"])
    app.openapi_schema = schema
    return schema


app.openapi = _openapi


if FRONTEND_DIST.joinpath("index.html").is_file():
    # Serve the built dashboard from the same origin, so no CORS is needed in production.
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
