from contextlib import asynccontextmanager
import os
import sqlite3
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.schemas import (
    ApiRunReport,
    ApiRunRequest,
    GemmaExplanation,
    GemmaExplanationRequest,
    GemmaPlan,
    GemmaPlanRequest,
    OpenApiImportRequest,
    OpenApiImportResult,
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
from app.gemma import (
    GemmaConfigurationError,
    GemmaProviderError,
    configuration_status,
    create_plan,
    explain_with_fallback,
)
from app.compare import compare_reports
from app.openapi_import import OpenApiError, import_openapi
from app.report_html import render_report_html
from app.deployment import deployment_readiness, public_mode
from app.security import request_owner, security_middleware
from app.jobs import JobManager
from app.execution import overall_status, redact_target_url, run_report, summary
from app.schemas import API_CHECK_NAMES, FULL_SCAN_CHECKS, WEBSITE_CHECK_NAMES

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


app = FastAPI(
    title="TestPilot AI API",
    version="2.0.0",
    description="Evidence-first software testing API with bounded website and API checks.",
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


@app.get("/api/ai/status", tags=["Gemma"])
def gemma_status() -> dict[str, str | bool]:
    """Return non-secret Gemma configuration state."""
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
    record = job_manager.get(job_id, request_owner(http))
    if record is None:
        raise HTTPException(status_code=404, detail="Job was not found.")
    return record


@app.post("/api/jobs/{job_id}/cancel", response_model=JobRecord, tags=["jobs"])
async def cancel_job(job_id: UUID, http: Request) -> JobRecord:
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


@app.post("/api/ai/plan", response_model=GemmaPlan, tags=["Gemma"])
async def gemma_plan(request: GemmaPlanRequest) -> GemmaPlan:
    """Use Gemma to structure a goal without executing or inventing evidence."""
    try:
        return await create_plan(request)
    except GemmaConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GemmaProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/ai/explain", response_model=GemmaExplanation, tags=["Gemma"])
async def gemma_explain(request: GemmaExplanationRequest) -> GemmaExplanation:
    """Explain a report with Gemma, or with a built-in summary when Gemma is unavailable.

    Neither can change the report's result, and the risk level never drops below
    what the check statuses imply.
    """
    return await explain_with_fallback(request.report.model_dump(mode="json"))


if FRONTEND_DIST.joinpath("index.html").is_file():
    # Serve the built dashboard from the same origin, so no CORS is needed in production.
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
