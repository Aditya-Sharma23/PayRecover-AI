"""AI adapter: Gemini through Google's Generative Language API, or a local Ollama server.

The model can structure a user goal and explain stored evidence. It cannot execute
checks, create evidence, or change runner classifications. Settings are TESTPILOT_AI_*.
"""

import asyncio
import json
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv

from app.schemas import (
    API_CHECK_NAMES,
    WEBSITE_CHECK_NAMES,
    AIExplanation,
    AIPlan,
    AIPlanRequest,
)

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_OLLAMA_MODEL = "llama3.2"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SECONDS = 90
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# The model that produced the latest answer in this request, which may be a fallback model.
_answered_by: ContextVar[str | None] = ContextVar("answered_by", default=None)


def _env(name: str, default: str = "") -> str:
    """TESTPILOT_AI_<name>, else the default."""
    return os.environ.get(f"TESTPILOT_AI_{name}", "").strip() or default


def ai_name(model: str) -> str:
    """The family name shown to people: Gemini, or plain AI for anything else."""
    return "Gemini" if model.lower().startswith("gemini") else "AI"


def answer_source() -> str:
    """How an explanation's source is labelled, e.g. "Gemini (gemini-3.7-flash)"."""
    model = _answered_by.get() or str(configuration_status()["model"])
    return f"{ai_name(model)} ({model})"


class AIConfigurationError(RuntimeError):
    """The AI model is not configured for this process."""


class AIProviderError(RuntimeError):
    """The configured AI provider failed or returned invalid output."""


def _settings() -> tuple[str, str]:
    raw_key = os.environ.get("TESTPILOT_GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    api_key = raw_key.strip().strip("\"'") if raw_key else ""
    model = _env("MODEL", DEFAULT_MODEL)
    if not api_key:
        raise AIConfigurationError("No AI API key is configured. Set TESTPILOT_GOOGLE_API_KEY.")
    return api_key, model


def _provider() -> str:
    return _env("PROVIDER", "google").lower()


def _ollama_settings() -> tuple[str, str]:
    """Local inference: the Ollama server is chosen by the operator, never by a user request."""
    url = os.environ.get("TESTPILOT_OLLAMA_URL", DEFAULT_OLLAMA_URL).strip().rstrip("/")
    model = _env("MODEL", DEFAULT_OLLAMA_MODEL)
    return url, model


def configuration_status() -> dict[str, str | bool]:
    if _provider() == "ollama":
        _, model = _ollama_settings()
        return {"configured": True, "model": model, "name": ai_name(model), "provider": "ollama_local"}
    raw_key = os.environ.get("TESTPILOT_GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    return {
        "configured": bool(raw_key and raw_key.strip().strip("\"'")),
        "model": _env("MODEL", DEFAULT_MODEL),
        "name": ai_name(_env("MODEL", DEFAULT_MODEL)),
        "provider": "google_generative_language_api",
    }


def _timeout_seconds() -> int:
    try:
        configured = int(_env("TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(5, min(configured, 300))


def _answer_text(data: dict[str, Any]) -> str:
    """Join the answer parts, skipping reasoning ("thought") parts."""
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    if not text.strip():
        raise KeyError("no answer text")
    return text


MAX_LIST_ITEMS = 10
MAX_SUMMARY_CHARS = 2000


def fit_to_limits(value: dict[str, Any], list_fields: tuple[str, ...], text_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """Shape an AI answer to the schema limits instead of rejecting it.

    Models often return an eleventh note or one long paragraph; dropping the whole answer for
    that would throw away a correct explanation. Lists keep their first ten non-empty strings
    (a lone string becomes a one-item list), and text fields are cut at the length limit.
    Content is only shortened, never added to.
    """
    shaped = dict(value)
    for name in list_fields:
        items = shaped.get(name)
        if isinstance(items, str):
            items = [items]
        if isinstance(items, list):
            shaped[name] = [str(item).strip() for item in items if str(item).strip()][:MAX_LIST_ITEMS]
    for name in text_fields:
        if isinstance(shaped.get(name), str):
            shaped[name] = shaped[name].strip()[:MAX_SUMMARY_CHARS]
    return shaped


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if not cleaned.startswith("{") and "{" in cleaned and "}" in cleaned:
        cleaned = cleaned[cleaned.index("{"):cleaned.rindex("}") + 1]
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIProviderError("The AI model returned non-JSON output.") from exc
    if not isinstance(value, dict):
        raise AIProviderError("The AI model returned a JSON value that was not an object.")
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
                    raise AIProviderError(f"The local model (Ollama) returned HTTP {response.status}.")
    except AIProviderError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise AIProviderError("The local model (Ollama) could not be reached.") from exc
    try:
        text = json.loads(body)["message"]["content"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AIProviderError("The local model (Ollama) returned an incomplete response.") from exc
    if not str(text).strip():
        raise AIProviderError("The local model (Ollama) returned an empty answer.")
    return str(text)


# Google answers these when a model is busy or briefly failing; another try, or
# another model, usually succeeds. Everything else (bad key, bad request) is final.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAY_SECONDS = 2
# One stalled model must not use the whole budget and starve the fallbacks.
ATTEMPT_TIMEOUT_SECONDS = 20
# A model that stalled is skipped for a while, so a multi-step answer does not wait on it at every step.
STALL_COOLDOWN_SECONDS = 120
_stalled_until: dict[str, float] = {}


class _RetryableProviderError(AIProviderError):
    """A busy, overloaded or unreachable provider; worth trying again."""


class _StalledProviderError(_RetryableProviderError):
    """The model did not answer in time; move on to the next model instead of waiting on it again."""


def _fallback_models(primary: str) -> list[str]:
    """Models to try after the primary one, from TESTPILOT_AI_FALLBACK_MODELS (comma-separated)."""
    raw = _env("FALLBACK_MODELS")
    models = [name.strip() for name in raw.split(",") if name.strip()]
    return [name for name in dict.fromkeys(models) if name != primary]


async def _post_google(payload: dict[str, Any], api_key: str, model: str, timeout_seconds: float) -> dict[str, Any]:
    url = f"{API_ROOT}/{quote(model, safe='')}:generateContent"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
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
                    error = _RetryableProviderError if response.status in RETRYABLE_STATUSES else AIProviderError
                    raise error(f"{model} returned HTTP {response.status}{suffix}: {message}")
    except AIProviderError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise _StalledProviderError(f"{model} could not be reached or did not answer in time.") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise AIProviderError(f"{model} returned an incomplete response.") from exc


async def _generate_google(prompt: str, api_key: str, model: str, timeout_seconds: float) -> str:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    data = await _post_google(payload, api_key, model, timeout_seconds)
    try:
        return _answer_text(data)
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError(f"{model} returned an incomplete response.") from exc


async def _with_fallbacks(attempt):
    """Ask the primary model twice, then each fallback model once, within one overall time budget.

    attempt(api_key, model, timeout) makes one call. A model that stalls is not asked again;
    a model that answers "busy" gets its second try.
    """
    api_key, model = _settings()
    attempts = [model, model, *_fallback_models(model)]
    now = time.monotonic()
    ready = [name for name in attempts if _stalled_until.get(name, 0) <= now]
    attempts = ready or attempts
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _timeout_seconds()
    failures: list[str] = []
    stalled: set[str] = set()
    for index, name in enumerate(attempts):
        remaining = deadline - loop.time()
        if remaining < 1:
            break
        if name in stalled:
            continue
        try:
            result = await attempt(api_key, name, min(remaining, ATTEMPT_TIMEOUT_SECONDS))
            _answered_by.set(name)
            return result
        except _RetryableProviderError as exc:
            failures.append(str(exc))
            if isinstance(exc, _StalledProviderError):
                stalled.add(name)
                _stalled_until[name] = time.monotonic() + STALL_COOLDOWN_SECONDS
                continue
            if index + 1 < len(attempts) and deadline - loop.time() > RETRY_DELAY_SECONDS + 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
    detail = failures[-1] if failures else "the time budget ran out"
    raise AIProviderError(f"The AI provider is busy or unreachable after {len(failures)} attempt(s); last: {detail}")


async def _generate(prompt: str) -> str:
    if _provider() == "ollama":
        _answered_by.set(_ollama_settings()[1])
        return await _generate_ollama(prompt)
    return await _with_fallbacks(lambda key, name, timeout: _generate_google(prompt, key, name, timeout))


async def ask_json(prompt: str) -> dict[str, Any]:
    """One JSON answer from the configured model."""
    return _extract_json(await _generate(prompt))


async def generate_turn(contents: list[dict[str, Any]], tools: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    """One step of a tool-using conversation with Gemini: returns the model's parts (text and function calls).

    The caller runs any function calls and sends the results back in the next turn. The parts are
    returned unchanged, so their thought signatures go back to the model as Gemini requires.
    """
    if _provider() == "ollama":
        raise AIConfigurationError("The PayRecover assistant needs Gemini; the local model cannot call tools.")
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "tools": [{"functionDeclarations": tools}],
        "generationConfig": {"temperature": 0},
    }

    async def attempt(key: str, name: str, timeout: float) -> list[dict[str, Any]]:
        data = await _post_google(payload, key, name, timeout)
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError(f"{name} returned an incomplete response.") from exc
        return [part for part in parts if isinstance(part, dict)]

    return await _with_fallbacks(attempt)


CHECK_GUIDE = {
    "http_status": "page loads with a 2xx/3xx status",
    "page_title": "page has a non-empty <title>",
    "h1": "page has exactly one main heading",
    "console_errors": "no browser console errors",
    "same_origin_links": "opens the page's same-site links (external ones are never opened) and fails on broken ones",
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
    "active_probes": "opt-in reflected-XSS probe (15 inert payloads in 15 parameters and the URL path) and unauthenticated admin-route checks",
    "openapi_contract": "discover a same-origin OpenAPI schema and run GET-only Schemathesis checks",
}
SECURITY_CHECKS = {"security_headers", "cookie_flags", "exposed_resources", "active_probes"}


def _plan_prompt(request: AIPlanRequest) -> str:
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


async def create_plan(request: AIPlanRequest) -> AIPlan:
    value = fit_to_limits(
        _extract_json(await _generate(_plan_prompt(request))), ("expectations", "limitations"), ("rationale",)
    )
    try:
        plan = AIPlan.model_validate(value)
    except Exception:
        # Malformed optional drafts should not discard an otherwise valid plan.
        core = {key: item for key, item in value.items() if not key.startswith("suggested_")}
        try:
            plan = AIPlan.model_validate(core)
        except Exception as exc:
            raise AIProviderError("The AI plan did not match the required schema.") from exc
        plan.limitations.append("The AI model's suggested inputs were malformed and were dropped.")
    allowed = WEBSITE_CHECK_NAMES if request.test_type == "website" else API_CHECK_NAMES
    if plan.test_type != request.test_type or any(check not in allowed for check in plan.checks):
        raise AIProviderError("The AI plan used checks outside the requested test type.")
    return plan


def risk_level(report: dict[str, Any]) -> str:
    """Deterministic risk from check statuses; the AI model may raise it, never lower it."""
    checks = report.get("checks", [])
    if not checks:
        return "UNKNOWN"
    if any(c["status"] == "failed" and (c["check_id"] in SECURITY_CHECKS or c["check_id"] == "accessibility") for c in checks):
        return "HIGH"
    if any(c["status"] in {"failed", "warning", "error", "skipped"} for c in checks):
        return "REVIEW"
    return "LOW"


def builtin_explanation(report: dict[str, Any], reason: str | None = None) -> AIExplanation:
    """Summary built only from the report, used when the AI model is not available."""
    checks = report.get("checks", [])
    problems = [c for c in checks if c["status"] in {"failed", "error"}]
    findings = []
    for check in checks:
        for item in (check.get("evidence") or {}).get("findings", [])[:3]:
            findings.append(f"{item['title']} ({item['severity']}): {item['recommendation']}")
    for check in problems:
        findings.append(f"{check['check_id']} {check['status']}: {check['message']}")
    summary = report.get("summary", {})
    limitations = ["The AI model was not used; this summary was built from the observed check records only."]
    if reason:
        limitations.append(reason[:300])
    limitations.extend(
        f"{c['check_id']} was skipped: {c['message']}" for c in checks if c["status"] == "skipped"
    )
    next_steps = [f"Fix and re-run {c['check_id']}." for c in problems[:5]] or [
        "No failed checks; continue with manual security and accessibility review."
    ]
    return AIExplanation(
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


async def explain_with_fallback(report: dict[str, Any]) -> AIExplanation:
    try:
        explanation = await explain_report(report)
    except (AIConfigurationError, AIProviderError) as exc:
        return builtin_explanation(report, str(exc))
    floor = risk_level(report)
    order = ["UNKNOWN", "LOW", "REVIEW", "HIGH"]
    if order.index(explanation.risk_level) < order.index(floor):
        explanation.risk_level = floor
    return explanation


EXPLANATION_LISTS = ("findings", "limitations", "next_steps")


async def explain_report(report: dict[str, Any]) -> AIExplanation:
    prompt = f"""Return only JSON matching this schema:
{{"summary": "string", "findings": ["string"], "limitations": ["string"], "next_steps": ["string"], "risk_level": "HIGH|REVIEW|LOW"}}
Explain this execution report. Use only its observed checks, evidence, statuses, and limitations. Never change its overall_status, claim unobserved facts, or imply that a failed/skipped/error check passed.
Report JSON:
{json.dumps(report, separators=(',', ':'))}"""
    value = fit_to_limits(_extract_json(await _generate(prompt)), EXPLANATION_LISTS, ("summary",))
    risk = str(value.get("risk_level", "")).upper()
    value["risk_level"] = risk if risk in {"HIGH", "REVIEW", "LOW"} else "UNKNOWN"
    value["source"] = answer_source()
    try:
        return AIExplanation.model_validate(value)
    except Exception as exc:
        raise AIProviderError("The AI explanation did not match the required schema.") from exc
