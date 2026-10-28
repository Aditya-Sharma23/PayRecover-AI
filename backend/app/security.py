"""Token authentication with per-user ownership, request limiting and response headers."""

import hashlib
import hmac
import os
import re
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from app.audit import write_event
from app.storage import LOCAL_OWNER

OWNER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
PUBLIC_PATHS = {"/api/health", "/api/deployment/readiness"}
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
APP_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def configured_tokens() -> dict[str, str]:
    """Map each configured token to its owner name.

    TESTPILOT_API_TOKENS="alice:<token>,bob:<token>" gives every user their own
    history. The single TESTPILOT_API_TOKEN belongs to the owner "default".
    """
    tokens: dict[str, str] = {}
    legacy = os.environ.get("TESTPILOT_API_TOKEN", "").strip()
    if legacy:
        tokens[legacy] = "default"
    for entry in os.environ.get("TESTPILOT_API_TOKENS", "").split(","):
        owner, _, token = entry.strip().partition(":")
        if OWNER_NAME.fullmatch(owner) and token.strip():
            tokens[token.strip()] = owner
    return tokens


def _supplied_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-testpilot-api-key", "").strip()


def authenticate(request: Request) -> str | None:
    """Return the owner for a valid token, comparing digests in constant time."""
    supplied = _supplied_token(request)
    if not supplied:
        return None
    supplied_digest = hashlib.sha256(supplied.encode()).digest()
    owner = None
    for token, name in configured_tokens().items():
        if hmac.compare_digest(supplied_digest, hashlib.sha256(token.encode()).digest()):
            owner = name
    return owner


def _valid_token(request: Request) -> bool:
    return authenticate(request) is not None


def request_owner(request: Request) -> str:
    return getattr(request.state, "owner", None) or LOCAL_OWNER


class RequestLimiter:
    def __init__(self, limit: int = 60, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        timestamps = self._requests[key]
        cutoff = current - self.window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= self.limit:
            return False
        timestamps.append(current)
        return True


_limiter = RequestLimiter()


def _apply_headers(path: str, response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if not path.startswith(DOCS_PATHS):
        response.headers.setdefault("Content-Security-Policy", APP_CSP)
    if os.environ.get("TESTPILOT_PUBLIC_URL", "").startswith("https://"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


async def security_middleware(request: Request, call_next):
    path = request.url.path
    owner = LOCAL_OWNER
    if _enabled("TESTPILOT_AUTH_ENABLED") and path.startswith("/api/") and path not in PUBLIC_PATHS:
        if request.method == "OPTIONS":
            return await call_next(request)
        authenticated = authenticate(request)
        if authenticated is None:
            response = JSONResponse({"detail": "authentication required"}, status_code=401)
            _apply_headers(path, response)
            return response
        owner = authenticated
    request.state.owner = owner

    if _enabled("TESTPILOT_RATE_LIMITING") and path.startswith("/api/") and path != "/api/health":
        client = request.client.host if request.client else "unknown"
        key = f"{owner}:{path}" if owner != LOCAL_OWNER else f"{client}:{path}"
        if not _limiter.allow(key):
            response = JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(_limiter.window_seconds)},
            )
            _apply_headers(path, response)
            return response
    response = await call_next(request)
    _apply_headers(path, response)
    if _enabled("TESTPILOT_AUDIT_LOGGING") and path.startswith("/api/"):
        write_event({
            "method": request.method,
            "path": path,
            "status_code": response.status_code,
            "owner": owner,
            "client": request.client.host if request.client else "unknown",
        })
    return response
