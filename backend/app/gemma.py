"""Gemma adapter: Google's Generative Language API by default, or a local Ollama server.

Gemma can structure a user goal and explain stored evidence. It cannot execute
checks, create evidence, or change runner classifications.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv

from app.schemas import (
    API_CHECK_NAMES,
    WEBSITE_CHECK_NAMES,
    GemmaExplanation,
    GemmaPlan,
    GemmaPlanRequest,
)

DEFAULT_MODEL = "gemma-4-26b-a4b-it"
DEFAULT_OLLAMA_MODEL = "gemma4"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SECONDS = 90
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class GemmaConfigurationError(RuntimeError):
    """Gemma is not configured for this process."""


class GemmaProviderError(RuntimeError):
    """The configured Gemma provider failed or returned invalid output."""


def _settings() -> tuple[str, str]:
    raw_key = os.environ.get("TESTPILOT_GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    api_key = raw_key.strip().strip("\"'") if raw_key else ""
    model = os.environ.get("TESTPILOT_GEMMA_MODEL", DEFAULT_MODEL).strip()
    if not api_key:
        raise GemmaConfigurationError(
            "Gemma is required but no API key is configured. Set TESTPILOT_GOOGLE_API_KEY."
        )
    return api_key, model


def _provider() -> str:
    return os.environ.get("TESTPILOT_GEMMA_PROVIDER", "google").strip().lower() or "google"


def _ollama_settings() -> tuple[str, str]:
    """Local inference: the Ollama server is chosen by the operator, never by a user request."""
    url = os.environ.get("TESTPILOT_OLLAMA_URL", DEFAULT_OLLAMA_URL).strip().rstrip("/")
    model = os.environ.get("TESTPILOT_GEMMA_MODEL", DEFAULT_OLLAMA_MODEL).strip()
    return url, model


def configuration_status() -> dict[str, str | bool]:
    if _provider() == "ollama":
        _, model = _ollama_settings()
        return {"configured": True, "model": model, "provider": "ollama_local"}
    raw_key = os.environ.get("TESTPILOT_GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    return {
        "configured": bool(raw_key and raw_key.strip().strip("\"'")),
        "model": os.environ.get("TESTPILOT_GEMMA_MODEL", DEFAULT_MODEL),
        "provider": "google_generative_language_api",
    }


def _timeout_seconds() -> int:
    try:
        configured = int(os.environ.get("TESTPILOT_GEMMA_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(5, min(configured, 300))


def _answer_text(data: dict[str, Any]) -> str:
    """Join the answer parts, skipping Gemma 4 reasoning ("thought") parts."""
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    if not text.strip():
        raise KeyError("no answer text")
    return text


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if not cleaned.startswith("{") and "{" in cleaned and "}" in cleaned:
        cleaned = cleaned[cleaned.index("{"):cleaned.rindex("}") + 1]
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GemmaProviderError("Gemma returned non-JSON output.") from exc
    if not isinstance(value, dict):
        raise GemmaProviderError("Gemma returned a JSON value that was not an object.")
    return value


async def _generate_ollama(prompt: str) -> str:
    url, model = _ollama_settings()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
    }
    timeout = aiohttp.ClientTimeout(total=_timeout_seconds())
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(f"{url}/api/chat", json=payload) as response:
                body = await response.text()
                if response.status >= 400:
                    raise GemmaProviderError(f"Local Gemma (Ollama) returned HTTP {response.status}.")
    except GemmaProviderError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GemmaProviderError("Local Gemma (Ollama) could not be reached.") from exc
    try:
        text = json.loads(body)["message"]["content"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GemmaProviderError("Local Gemma (Ollama) returned an incomplete response.") from exc
    if not str(text).strip():
        raise GemmaProviderError("Local Gemma (Ollama) returned an empty answer.")
    return str(text)


async def _generate(prompt: str) -> str:
    if _provider() == "ollama":
        return await _generate_ollama(prompt)
    api_key, model = _settings()
    url = f"{API_ROOT}/{quote(model, safe='')}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    timeout = aiohttp.ClientTimeout(total=_timeout_seconds())
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(url, headers={"x-goog-api-key": api_key}, json=payload) as response:
                body = await response.text()
                if response.status >= 400:
                    try:
                        provider_error = json.loads(body).get("error", {})
                        message = provider_error.get("message", "unknown provider error")
                        reason = provider_error.get("status")
                    except (TypeError, json.JSONDecodeError):
                        message = "unknown provider error"
                        reason = None
                    suffix = f" ({reason})" if reason else ""
                    raise GemmaProviderError(
                        f"Gemma provider returned HTTP {response.status}{suffix}: {message}"
                    )
    except GemmaProviderError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GemmaProviderError("Gemma provider could not be reached.") from exc
    try:
        data = json.loads(body)
        return _answer_text(data)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise GemmaProviderError("Gemma provider returned an incomplete response.") from exc


CHECK_GUIDE = {
    "http_status": "page loads with a 2xx/3xx status",
    "page_title": "page has a non-empty <title>",
    "h1": "page has exactly one main heading",
    "console_errors": "no browser console errors",
    "same_origin_links": "inventory of links (always a warning, never a pass)",
    "security_headers": "six security controls: CSP, HSTS, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, clickjacking",
    "text_present": "exact visible text appears, optionally after one click (needs suggested_expected_text)",
    "accessibility": "automated axe-core accessibility rules",
    "user_flow": "multi-step journey of click/fill/press/wait_for/expect_text/expect_url steps (needs suggested_flow_steps)",
    "api_status": "expected HTTP status code",
    "api_response_time": "response time under a limit",
    "api_content_type": "expected Content-Type",
    "api_redirects": "no redirect",
    "api_response_size": "response size under a limit",
    "api_json_assertions": "conditions on JSON fields such as data.items.0.id (needs suggested_json_assertions)",
    "api_json_schema": "response matches a JSON Schema the user supplies",
    "api_json_shape": "response JSON has the same keys and types as a sample the user supplies",
    "response_time": "main document loads within a time limit (default 5 seconds)",
    "response_headers": "main document returns response headers",
    "body_not_empty": "main document body contains text",
    "keyboard_focus": "pressing Tab reaches an interactive element",
    "cookie_flags": "cookies set Secure, HttpOnly and SameSite",
    "exposed_resources": "HEAD probes for /.env, /.git/HEAD and /backup.zip",
    "active_probes": "opt-in inert reflection canary and unauthenticated admin-route checks",
    "openapi_contract": "discover a same-origin OpenAPI schema and run GET-only Schemathesis checks",
}
SECURITY_CHECKS = {"security_headers", "cookie_flags", "exposed_resources", "active_probes"}


def _plan_prompt(request: GemmaPlanRequest) -> str:
    target = str(request.target_url) if request.target_url else "not supplied"
    allowed = WEBSITE_CHECK_NAMES if request.test_type == "website" else API_CHECK_NAMES
    guide = "\n".join(f"- {name}: {CHECK_GUIDE[name]}" for name in allowed)
    return f"""Return only JSON matching this schema:
{{"goal": "string", "test_type": "{request.test_type}", "checks": ["check name"], "expectations": ["explicit user-grounded condition"], "limitations": ["string"], "rationale": "string",
 "suggested_expected_text": "string or null",
 "suggested_flow_steps": [{{"action": "click|fill|press|wait_for|expect_text|expect_url", "selector": "CSS selector or null", "value": "string or null"}}] or null,
 "suggested_json_assertions": [{{"path": "dot.path", "operator": "exists|not_exists|equals|not_equals|contains|type|length", "expected": "value or null"}}] or null}}
User goal: {request.goal}
Target URL: {target}
Test type: {request.test_type}
Checks you may use:
{guide}
Rules: do not invent expected behavior; use only the goal. Use only the checks listed above. Fill a suggested_* field only when its check is selected and the goal states the value; selectors you cannot know must be marked as guesses in limitations. A user flow needs at least one expect_text or expect_url step. Never put passwords or secrets in suggestions. Do not claim execution or evidence."""


async def create_plan(request: GemmaPlanRequest) -> GemmaPlan:
    value = _extract_json(await _generate(_plan_prompt(request)))
    try:
        plan = GemmaPlan.model_validate(value)
    except Exception:
        # Malformed optional drafts should not discard an otherwise valid plan.
        core = {key: item for key, item in value.items() if not key.startswith("suggested_")}
        try:
            plan = GemmaPlan.model_validate(core)
        except Exception as exc:
            raise GemmaProviderError("Gemma plan did not match the required schema.") from exc
        plan.limitations.append("Gemma's suggested inputs were malformed and were dropped.")
    allowed = WEBSITE_CHECK_NAMES if request.test_type == "website" else API_CHECK_NAMES
    if plan.test_type != request.test_type or any(check not in allowed for check in plan.checks):
        raise GemmaProviderError("Gemma plan used checks outside the requested test type.")
    return plan


def risk_level(report: dict[str, Any]) -> str:
    """Deterministic risk from check statuses; Gemma may raise it, never lower it."""
    checks = report.get("checks", [])
    if not checks:
        return "UNKNOWN"
    if any(c["status"] == "failed" and (c["check_id"] in SECURITY_CHECKS or c["check_id"] == "accessibility") for c in checks):
        return "HIGH"
    if any(c["status"] in {"failed", "warning", "error", "skipped"} for c in checks):
        return "REVIEW"
    return "LOW"


def builtin_explanation(report: dict[str, Any], reason: str | None = None) -> GemmaExplanation:
    """Summary built only from the report, used when Gemma is not available."""
    checks = report.get("checks", [])
    problems = [c for c in checks if c["status"] in {"failed", "error"}]
    findings = []
    for check in checks:
        for item in (check.get("evidence") or {}).get("findings", [])[:3]:
            findings.append(f"{item['title']} ({item['severity']}): {item['recommendation']}")
    for check in problems:
        findings.append(f"{check['check_id']} {check['status']}: {check['message']}")
    summary = report.get("summary", {})
    limitations = ["Gemma was not used; this summary was built from the observed check records only."]
    if reason:
        limitations.append(reason[:300])
    limitations.extend(
        f"{c['check_id']} was skipped: {c['message']}" for c in checks if c["status"] == "skipped"
    )
    next_steps = [f"Fix and re-run {c['check_id']}." for c in problems[:5]] or [
        "No failed checks; continue with manual security and accessibility review."
    ]
    return GemmaExplanation(
        summary=(
            f"{report.get('overall_status', 'unknown').capitalize()} run on {report.get('target_url', 'the target')}: "
            f"{summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed, {summary.get('warnings', 0)} warnings, "
            f"{summary.get('errors', 0)} errors, {summary.get('skipped', 0)} skipped."
        ),
        findings=findings[:10],
        limitations=limitations[:10],
        next_steps=next_steps[:10],
        risk_level=risk_level(report),
        source="Built-in summary",
    )


async def explain_with_fallback(report: dict[str, Any]) -> GemmaExplanation:
    try:
        explanation = await explain_report(report)
    except (GemmaConfigurationError, GemmaProviderError) as exc:
        return builtin_explanation(report, str(exc))
    floor = risk_level(report)
    order = ["UNKNOWN", "LOW", "REVIEW", "HIGH"]
    if order.index(explanation.risk_level) < order.index(floor):
        explanation.risk_level = floor
    return explanation


async def explain_report(report: dict[str, Any]) -> GemmaExplanation:
    prompt = f"""Return only JSON matching this schema:
{{"summary": "string", "findings": ["string"], "limitations": ["string"], "next_steps": ["string"], "risk_level": "HIGH|REVIEW|LOW"}}
Explain this execution report. Use only its observed checks, evidence, statuses, and limitations. Never change its overall_status, claim unobserved facts, or imply that a failed/skipped/error check passed.
Report JSON:
{json.dumps(report, separators=(',', ':'))}"""
    value = _extract_json(await _generate(prompt))
    risk = str(value.get("risk_level", "")).upper()
    value["risk_level"] = risk if risk in {"HIGH", "REVIEW", "LOW"} else "UNKNOWN"
    value["source"] = "Gemma"
    try:
        return GemmaExplanation.model_validate(value)
    except Exception as exc:
        raise GemmaProviderError("Gemma explanation did not match the required schema.") from exc
