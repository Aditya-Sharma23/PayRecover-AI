"""Fail-closed checks for public deployment.

Each gate is checked against real configuration rather than a self-declared
flag. When TESTPILOT_PUBLIC_DEPLOYMENT=true and any gate fails, the server
refuses to start (see app.main.lifespan).
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

from app.security import configured_tokens

MIN_TOKEN_LENGTH = 32
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def public_mode() -> bool:
    return os.environ.get("TESTPILOT_PUBLIC_DEPLOYMENT", "").strip().lower() == "true"


def _true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def deployment_readiness() -> dict[str, object]:
    blockers: list[str] = []
    public = public_mode()
    if public:
        if not _true("TESTPILOT_AUTH_ENABLED"):
            blockers.append("TESTPILOT_AUTH_ENABLED must be true for public deployment")
        tokens = configured_tokens()
        if not tokens:
            blockers.append("TESTPILOT_API_TOKENS (or TESTPILOT_API_TOKEN) must configure at least one token")
        elif any(len(token) < MIN_TOKEN_LENGTH for token in tokens):
            blockers.append(f"every API token must be at least {MIN_TOKEN_LENGTH} characters long")
        if not _true("TESTPILOT_RATE_LIMITING"):
            blockers.append("TESTPILOT_RATE_LIMITING must be true for public deployment")
        if not _true("TESTPILOT_AUDIT_LOGGING"):
            blockers.append("TESTPILOT_AUDIT_LOGGING must be true for public deployment")
        public_url = os.environ.get("TESTPILOT_PUBLIC_URL", "").strip()
        if urlsplit(public_url).scheme != "https" or not urlsplit(public_url).hostname:
            blockers.append("TESTPILOT_PUBLIC_URL must be the https:// address users open (TLS is required)")
        origins = [o.strip() for o in os.environ.get("TESTPILOT_CORS_ORIGINS", "").split(",") if o.strip()]
        if any(origin == "*" or not origin.startswith("https://") for origin in origins):
            blockers.append("TESTPILOT_CORS_ORIGINS may only list explicit https:// origins")
        if ENV_FILE.exists():
            blockers.append("backend/.env must not exist in a public deployment; use the platform's secret store")
    return {
        "public_deployment_requested": public,
        "ready": not blockers,
        "blockers": blockers,
        "controls": {
            "dns_validation_and_pinning": True,
            "browser_network_lockdown": True,
            "same_origin_get_only_browser": True,
            "worker_process_isolation": public,
            "per_user_ownership": _true("TESTPILOT_AUTH_ENABLED"),
            "retention_policy": True,
        },
    }
