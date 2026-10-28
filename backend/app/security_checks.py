"""Bounded security probes: cookie flags, exposed files and opt-in active probes.

Adapted from the single-URL scanner build. Every request goes through the same
pinned resolver as the browser runner, so DNS rebinding cannot redirect a probe.
Probes are read-only: GET or HEAD, no redirects, no cookies kept, small bodies.
"""

import asyncio
import secrets
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from app.url_policy import UnsafeTarget, validate_target_url

PROBE_TIMEOUT_SECONDS = 6
MAX_PROBE_BODY_BYTES = 128_000
USER_AGENT = "TestPilot-AI-Scanner/1.0"
EXPOSURE_PATHS = ((".env file", "/.env"), ("Git metadata", "/.git/HEAD"), ("Backup archive", "/backup.zip"))
ACCESS_CONTROL_PATHS = ("/admin", "/manage", "/settings", "/account", "/profile")
LOGIN_WORDS = ("login", "log in", "sign in", "password")


@dataclass
class ProbeResponse:
    status: int
    headers: dict[str, str]
    set_cookies: list[str] = field(default_factory=list)
    body: bytes = b""


async def fetch_probe(url: str, method: str = "GET", body_limit: int = 0) -> ProbeResponse:
    """One pinned, redirect-free request. Tests replace this seam."""
    from app.website_runner import PinnedResolver

    addresses = await validate_target_url(url)
    connector = aiohttp.TCPConnector(
        resolver=PinnedResolver(urlsplit(url).hostname or "", addresses),
        use_dns_cache=False,
        family=socket.AF_UNSPEC,
        limit=1,
    )
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS),
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={"User-Agent": USER_AGENT},
    ) as session:
        async with session.request(method, url, allow_redirects=False, ssl=True) as response:
            body = await response.content.read(body_limit) if body_limit else b""
            return ProbeResponse(
                status=response.status,
                headers={name.lower(): value for name, value in response.headers.items()},
                set_cookies=response.headers.getall("set-cookie", []),
                body=body,
            )


def _origin_url(target: str, path: str) -> str:
    parts = urlsplit(target)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def finding(title: str, severity: str, detail: str, recommendation: str) -> dict[str, str]:
    return {"title": title, "severity": severity, "detail": detail[:320], "recommendation": recommendation[:320]}


def cookie_flag_results(set_cookies: list[str]) -> tuple[list[dict], list[dict]]:
    """Check Secure, HttpOnly and SameSite on each cookie; values are never kept."""
    cookies, findings = [], []
    for cookie in set_cookies[:20]:
        parts = [part.strip().lower() for part in cookie.split(";")]
        name = cookie.split("=", 1)[0].strip()[:80] or "unnamed cookie"
        missing = [
            flag for flag in ("secure", "httponly", "samesite")
            if not any(part == flag or part.startswith(flag + "=") for part in parts[1:])
        ]
        cookies.append({"name": name, "missing": missing})
        if missing:
            findings.append(finding(
                f"Cookie {name} is missing {', '.join(missing)}",
                "medium" if "httponly" in missing else "low",
                f"{name} was set without {', '.join(missing)}. The cookie value was not retained.",
                "Set Secure, HttpOnly and an appropriate SameSite attribute where applicable.",
            ))
    return cookies, findings


async def _safe(coro):
    try:
        return await coro, None
    except UnsafeTarget as exc:
        return None, f"blocked: {exc}"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
        return None, type(exc).__name__


async def cookie_check(target: str) -> dict:
    response, error = await _safe(fetch_probe(target, "GET"))
    if error:
        return {"error": error}
    cookies, findings = cookie_flag_results(response.set_cookies)
    return {"status_code": response.status, "cookies": cookies, "findings": findings}


async def exposure_check(target: str) -> dict:
    probes, findings = [], []
    for label, path in EXPOSURE_PATHS:
        url = _origin_url(target, path)
        response, error = await _safe(fetch_probe(url, "HEAD"))
        if error:
            probes.append({"path": path, "error": error})
            continue
        exposed = response.status == 200
        probes.append({"path": path, "status_code": response.status, "exposed": exposed})
        if exposed:
            findings.append(finding(
                f"Potentially exposed {label}",
                "medium",
                f"HEAD {path} returned HTTP 200; confirm whether this is a real file or a catch-all route. No body was read.",
                "Remove public access to secrets, repository metadata and backup files.",
            ))
    return {"probes": probes, "findings": findings}


async def active_probe_check(target: str) -> dict:
    """An inert HTML canary and unauthenticated GETs of common admin routes."""
    findings: list[dict] = []
    marker = "<testpilot-probe-" + secrets.token_hex(8) + ">"
    parts = urlsplit(target)
    query = (parts.query + "&" if parts.query else "") + "testpilot_probe=" + marker
    canary_url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))
    response, error = await _safe(fetch_probe(canary_url, "GET", MAX_PROBE_BODY_BYTES))
    canary = {"error": error} if error else {
        "status_code": response.status, "reflected": marker.encode("ascii") in response.body,
    }
    if canary.get("reflected"):
        findings.append(finding(
            "Potential reflected HTML injection",
            "medium",
            "An inert HTML canary in a query parameter came back unescaped. No script was run, so this is not proof of XSS.",
            "Encode output for its HTML or JavaScript context and validate the affected input.",
        ))
    routes = []
    for path in ACCESS_CONTROL_PATHS:
        response, error = await _safe(fetch_probe(_origin_url(target, path), "GET", 4096))
        if error:
            routes.append({"path": path, "error": error})
            continue
        text = response.body.decode("utf-8", errors="replace").lower()
        open_route = response.status == 200 and bool(response.body.strip()) and not any(word in text for word in LOGIN_WORDS)
        routes.append({"path": path, "status_code": response.status, "needs_review": open_route})
        if open_route:
            findings.append(finding(
                f"Review access control for {path}",
                "medium",
                f"An unauthenticated GET of {path} returned HTTP 200 without login text. This is a lead, not proof.",
                "Confirm the route requires authorization, using a browser that is not signed in.",
            ))
    return {"canary": canary, "routes": routes, "findings": findings}
