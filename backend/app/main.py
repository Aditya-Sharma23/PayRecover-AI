import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.storage import (
    dashboard_stats,
    retention_policy,
)
from app.ai import (
    configuration_status,
)
from app import (autoscan)
from app.deployment import deployment_readiness, public_mode
from app.security import request_owner, security_middleware
from app.jobs import JobManager
from app.routers import payrecover, qa
from app.schemas import API_CHECK_NAMES, FULL_SCAN_CHECKS, WEBSITE_CHECK_NAMES

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
    scanner = asyncio.create_task(autoscan.loop())
    yield
    scanner.cancel()
    await qa.job_manager.close()


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
def ai_status() -> dict[str, str | bool | int]:
    """Return non-secret AI configuration state: model, family name and provider."""
    return configuration_status()




app.include_router(qa.router)
app.include_router(payrecover.router)


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

