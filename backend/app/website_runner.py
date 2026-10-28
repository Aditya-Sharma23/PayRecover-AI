"""Bounded Playwright runner with a pinned, same-origin request gateway."""

import asyncio
import ipaddress
import re
import socket
import time
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from playwright.async_api import async_playwright

from app import contract_runner, security_checks
from app.schemas import CheckName, CheckResult, CheckStatus, FlowStep, WebsiteRunRequest
from app.static_a11y import static_accessibility_scan
from app.storage import save_artifact
from app.url_policy import UnsafeTarget, validate_target_url

MAX_REQUESTS = 100
MAX_AXE_VIOLATIONS = 15
STEP_TIMEOUT_MS = 5000
AXE_PATH = Path(__file__).resolve().parent / "vendor" / "axe.min.js"
# Every page request is fulfilled by guarded_route through the pinned fetcher,
# so Chromium itself never needs DNS or a direct connection. These switches make
# that a hard guarantee: unresolvable hosts, a dead proxy, and no WebRTC UDP.
BROWSER_LOCKDOWN_ARGS = (
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--proxy-server=http://127.0.0.1:9",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-component-update",
    "--no-pings",
    "--dns-prefetch-disable",
)
AXE_RUN = """async () => {
  const result = await axe.run(document, {
    resultTypes: ["violations"],
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa", "best-practice"] },
  });
  return {
    passes: result.passes.length,
    incomplete: result.incomplete.length,
    violations: result.violations.map((v) => ({
      id: v.id,
      impact: v.impact,
      help: v.help,
      help_url: v.helpUrl,
      nodes: v.nodes.length,
      targets: v.nodes.slice(0, 3).map((n) => n.target.join(" ")),
    })),
  };
}"""
MAX_RESPONSE_BYTES = 2_000_000
MAX_SCREENSHOT_BYTES = 1_000_000
MAX_CONCURRENT_REQUESTS = 4
SECURITY_HEADERS = (
    "content-security-policy",
    "strict-transport-security",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
    "x-frame-options",
)
# (control, severity, recommendation) for the security header policy check.
HEADER_RULES = (
    ("content-security-policy", "medium", "Add a restrictive Content-Security-Policy appropriate for the site."),
    ("strict-transport-security", "medium", "Enable HSTS after confirming every subdomain is HTTPS-ready."),
    ("x-content-type-options", "low", "Set X-Content-Type-Options: nosniff."),
    ("referrer-policy", "low", "Set a Referrer-Policy that limits referrer data."),
    ("permissions-policy", "info", "Set a Permissions-Policy that disables unused browser features."),
    ("clickjacking-protection", "low", "Set CSP frame-ancestors or X-Frame-Options."),
)
FOCUS_PROBE = """() => {
  const active = document.activeElement;
  const moved = Boolean(active && active !== document.body && active !== document.documentElement);
  return {
    moved,
    target: moved ? active.tagName.toLowerCase() + (active.id ? "#" + active.id : "") : null,
    interactive: document.querySelectorAll('a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"])').length,
  };
}"""
PROBE_CHECKS = (CheckName.COOKIE_FLAGS, CheckName.EXPOSED_RESOURCES, CheckName.ACTIVE_PROBES, CheckName.OPENAPI_CONTRACT)
PROBE_BUDGET_SECONDS = {
    CheckName.COOKIE_FLAGS: 8,
    CheckName.EXPOSED_RESOURCES: 20,
    CheckName.ACTIVE_PROBES: 40,
    CheckName.OPENAPI_CONTRACT: 60,
}


class PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolve one validated hostname only to the public addresses we checked."""

    def __init__(self, hostname: str, addresses: tuple[str, ...]):
        self.hostname = hostname.lower().rstrip(".")
        self.addresses = addresses

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        if host.lower().rstrip(".") != self.hostname:
            raise OSError("request host differs from the validated host")
        resolved = []
        for address in self.addresses:
            ip = ipaddress.ip_address(address)
            resolved.append({
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                "proto": 0,
                "flags": 0,
            })
        return resolved

    async def close(self):
        return None


def _same_origin(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    a_port = a.port or (443 if a.scheme == "https" else 80)
    b_port = b.port or (443 if b.scheme == "https" else 80)
    return (a.scheme.lower(), (a.hostname or "").lower(), a_port) == (
        b.scheme.lower(), (b.hostname or "").lower(), b_port
    )


async def _fetch_pinned(url: str, method: str, timeout_seconds: int):
    addresses = await validate_target_url(url)
    parts = urlsplit(url)
    resolver = PinnedResolver(parts.hostname or "", addresses)
    connector = aiohttp.TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        family=socket.AF_UNSPEC,
        limit=1,
        limit_per_host=1,
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=False,
        auto_decompress=True,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        headers = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.5"}
        async with session.request(
            method,
            url,
            headers=headers,
            allow_redirects=False,
            ssl=True,
        ) as response:
            body = await response.content.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded the 2 MB limit")
            safe_headers = {}
            for name in ("content-type", "location", *SECURITY_HEADERS):
                value = response.headers.get(name)
                if value is not None:
                    safe_headers[name] = value
            return response.status, safe_headers, body


async def _one_check(name: CheckName, status: CheckStatus, message: str, evidence: dict, started: float, limitations=None):
    return CheckResult(
        check_id=name,
        status=status,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        message=message,
        evidence=evidence,
        limitations=limitations or [],
    )


@cache
def _axe_source() -> str:
    return AXE_PATH.read_text(encoding="utf-8")


def _time_budget(request: WebsiteRunRequest) -> int:
    budget = request.timeout_seconds
    if request.flow_steps:
        budget += 2 + len(request.flow_steps) * (STEP_TIMEOUT_MS // 1000 + 1)
    if CheckName.ACCESSIBILITY in request.checks:
        budget += 15
    return budget


def evaluate_security_headers(headers: dict, scheme: str) -> dict:
    """Six controls; HSTS only applies over HTTPS."""
    lower = {name.lower(): value for name, value in headers.items()}
    present, missing, not_applicable, findings = [], [], [], []
    for control, severity, recommendation in HEADER_RULES:
        if control == "strict-transport-security" and scheme != "https":
            not_applicable.append(control)
            continue
        if control == "clickjacking-protection":
            ok = "x-frame-options" in lower or "frame-ancestors" in lower.get("content-security-policy", "").lower()
        else:
            ok = control in lower
        (present if ok else missing).append(control)
        if not ok:
            findings.append(security_checks.finding(
                f"Missing {control}", severity, f"{control} was not observed on the main document.", recommendation,
            ))
    return {"present": present, "missing": missing, "not_applicable": not_applicable, "findings": findings}


async def _probe(name: CheckName, target_url: str) -> dict:
    runner = {
        CheckName.COOKIE_FLAGS: security_checks.cookie_check,
        CheckName.EXPOSED_RESOURCES: security_checks.exposure_check,
        CheckName.ACTIVE_PROBES: security_checks.active_probe_check,
        CheckName.OPENAPI_CONTRACT: contract_runner.run_contract,
    }[name]
    try:
        async with asyncio.timeout(PROBE_BUDGET_SECONDS[name]):
            return await runner(target_url)
    except TimeoutError:
        return {"error": f"the probe exceeded its {PROBE_BUDGET_SECONDS[name]}-second budget"}
    except Exception as exc:
        return {"error": type(exc).__name__}


async def _flow_step(page, step: FlowStep) -> None:
    if step.action == "click":
        await page.locator(step.selector).first.click(timeout=STEP_TIMEOUT_MS)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(300)
    elif step.action == "fill":
        await page.locator(step.selector).first.fill(step.value, timeout=STEP_TIMEOUT_MS)
    elif step.action == "press":
        if step.selector:
            await page.locator(step.selector).first.press(step.value, timeout=STEP_TIMEOUT_MS)
        else:
            await page.keyboard.press(step.value)
        await page.wait_for_timeout(300)
    elif step.action == "wait_for":
        await page.locator(step.selector).first.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    elif step.action == "expect_text":
        await page.wait_for_function(
            "t => document.body && document.body.innerText.includes(t)", arg=step.value, timeout=STEP_TIMEOUT_MS
        )
    elif step.action == "expect_url":
        await page.wait_for_function("v => location.href.includes(v)", arg=step.value, timeout=STEP_TIMEOUT_MS)


async def _run_flow(context, target_url: str, request: WebsiteRunRequest) -> dict:
    """Replay the user's steps in a fresh tab and stop at the first step that does not hold."""
    page = await context.new_page()
    steps: list[dict] = []
    outcome = "passed"
    result: dict = {}
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=request.timeout_seconds * 1000)
        for index, step in enumerate(request.flow_steps or [], start=1):
            started = time.monotonic()
            record: dict = {"step": index, "action": step.action}
            if step.selector:
                record["selector"] = step.selector
            if step.action == "fill":
                # Typed values may be credentials, so only their length is kept.
                record["value_length"] = len(step.value or "")
            elif step.value:
                record["value"] = step.value
            try:
                await _flow_step(page, step)
                record["status"] = "passed"
            except Exception as exc:
                assertion = step.action in {"expect_text", "expect_url"}
                record["status"] = "failed" if assertion else "error"
                record["reason"] = (
                    "The expected condition did not hold within 5 seconds."
                    if assertion else _redact_text(str(exc).splitlines()[0])
                )
            record["duration_ms"] = int((time.monotonic() - started) * 1000)
            steps.append(record)
            if record["status"] != "passed":
                outcome = record["status"]
                break
        for index in range(len(steps) + 1, len(request.flow_steps or []) + 1):
            steps.append({"step": index, "action": request.flow_steps[index - 1].action, "status": "not_run"})
        result["final_url"] = _redact_text(page.url)
        if outcome != "passed" and request.capture_screenshot_on_failure:
            result["screenshot"] = await page.screenshot(type="jpeg", quality=60, full_page=False)
    except Exception as exc:
        outcome = "error"
        result["reason"] = _redact_text(str(exc).splitlines()[0])
    finally:
        await page.close()
    result.update({"outcome": outcome, "steps": steps})
    return result


def _redact_text(value: str) -> str:
    return re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*", r"\1?[redacted]", value)[:1000]


async def run_website_checks(request: WebsiteRunRequest) -> list[CheckResult]:
    """Execute selected checks using real observations and bounded requests."""
    target_url = str(request.url)
    await validate_target_url(target_url)
    gate = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    state = {"requests": 0, "blocked_reason": None}
    console_errors: list[str] = []

    async def guarded_route(route):
        req = route.request
        state["requests"] += 1
        if state["requests"] > MAX_REQUESTS:
            state["blocked_reason"] = "request budget exceeded"
            await route.abort("blockedbyclient")
            return
        if req.method not in {"GET", "HEAD"}:
            await route.fulfill(status=405, body="Method blocked by safe runner")
            return
        if not _same_origin(target_url, req.url):
            state["blocked_reason"] = "cross-origin request blocked"
            await route.fulfill(status=403, body="Cross-origin request blocked by safe runner")
            return
        try:
            async with gate:
                status, headers, body = await _fetch_pinned(
                    req.url, req.method, request.timeout_seconds
                )
            await route.fulfill(status=status, headers=headers, body=body)
        except (UnsafeTarget, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            state["blocked_reason"] = str(exc)
            await route.fulfill(status=502, body="Request blocked by safe runner")

    observations: dict[str, object] = {}
    page = None
    browser = None
    playwright = None
    navigation_started = time.monotonic()
    try:
        async with asyncio.timeout(_time_budget(request)):
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=True, args=list(BROWSER_LOCKDOWN_ARGS))
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", guarded_route)
            async def block_websocket(websocket):
                await websocket.close(code=1008, reason="WebSockets are disabled in the safe runner")
            await context.route_web_socket("**/*", block_websocket)
            page = await context.new_page()
            page.on("console", lambda msg: console_errors.append(_redact_text(msg.text)) if msg.type == "error" else None)
            page.on("pageerror", lambda error: console_errors.append(_redact_text(str(error))))
            page.set_default_timeout(request.timeout_seconds * 1000)
            load_started = time.monotonic()
            response = await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=request.timeout_seconds * 1000,
            )
            observations["load_ms"] = int((time.monotonic() - load_started) * 1000)
            observations["status_code"] = response.status if response else None
            observations["headers"] = response.headers if response else {}
            try:
                document_body = await response.body() if response else b""
            except Exception:
                document_body = b""
            observations["body_bytes"] = len(document_body)
            observations["body_has_text"] = bool(document_body.strip())
            observations["document_html"] = document_body.decode("utf-8", errors="replace")
            observations["title"] = await page.title()
            headings = await page.locator("h1").all_text_contents()
            observations["h1_count"] = len(headings)
            observations["h1_text"] = headings[:5]
            observations["console_errors"] = console_errors[:20]
            observations["request_count"] = state["requests"]
            observations["blocked_reason"] = state["blocked_reason"]
            links = await page.locator("a[href]").evaluate_all(
                "(nodes, limit) => nodes.map(a => a.href).slice(0, limit)", request.max_links
            )
            observations["same_origin_link_count"] = sum(_same_origin(target_url, link) for link in links)
            observations["external_link_count"] = sum(not _same_origin(target_url, link) for link in links)
            if CheckName.ACCESSIBILITY in request.checks:
                # page.evaluate is not subject to the page's CSP, and the script
                # is local, so no request leaves the guarded route for it.
                try:
                    await page.evaluate(_axe_source())
                    observations["axe"] = await page.evaluate(AXE_RUN)
                except Exception as exc:
                    observations["axe_error"] = _redact_text(str(exc).splitlines()[0])
            if CheckName.KEYBOARD_FOCUS in request.checks:
                try:
                    await page.keyboard.press("Tab")
                    observations["focus"] = await page.evaluate(FOCUS_PROBE)
                except Exception as exc:
                    observations["focus_error"] = _redact_text(str(exc).splitlines()[0])
            if CheckName.TEXT_PRESENT in request.checks:
                if request.click_selector:
                    # One user-specified click; any resulting non-GET request is
                    # still refused by guarded_route, so forms cannot submit data.
                    try:
                        await page.locator(request.click_selector).first.click(timeout=5000)
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(300)
                        observations["clicked"] = True
                    except Exception as exc:
                        observations["click_error"] = _redact_text(str(exc).splitlines()[0])
                if "click_error" not in observations:
                    # Only the match result is kept; page text is not stored.
                    visible_text = await page.locator("body").inner_text()
                    observations["text_found"] = request.expected_text in visible_text
            if request.capture_screenshot_on_failure:
                observations["screenshot"] = await page.screenshot(
                    type="jpeg", quality=60, full_page=False
                )
            if CheckName.USER_FLOW in request.checks:
                observations["flow"] = await _run_flow(context, target_url, request)
    except Exception as exc:
        observations["runner_error"] = _redact_text(str(exc))
    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
    # Security probes and the contract run use the pinned HTTP client, not the browser.
    probes = {name: await _probe(name, target_url) for name in request.checks if name in PROBE_CHECKS}

    results: list[CheckResult] = []
    for name in request.checks:
        started = navigation_started
        if name in PROBE_CHECKS:
            results.append(_probe_result(name, probes[name], started))
        elif "runner_error" in observations:
            results.append(await _one_check(name, CheckStatus.ERROR, "The browser run did not complete.", {"reason": observations["runner_error"]}, started))
        elif name == CheckName.HTTP_STATUS:
            code = observations.get("status_code")
            passed = isinstance(code, int) and 200 <= code < 400
            results.append(await _one_check(name, CheckStatus.PASSED if passed else CheckStatus.FAILED, f"Observed HTTP status {code}.", {"status_code": code}, started))
        elif name == CheckName.PAGE_TITLE:
            title = str(observations.get("title", ""))
            passed = bool(title.strip())
            results.append(await _one_check(name, CheckStatus.PASSED if passed else CheckStatus.FAILED, "Page title was present." if passed else "Page title was empty.", {"title": title[:500]}, started))
        elif name == CheckName.H1:
            count = observations.get("h1_count", 0)
            text = observations.get("h1_text", [])
            passed = count == 1 and bool(text and str(text[0]).strip())
            results.append(await _one_check(name, CheckStatus.PASSED if passed else CheckStatus.FAILED, f"Observed {count} H1 headings; expected exactly one.", {"count": count, "text": observations.get("h1_text", [])}, started))
        elif name == CheckName.CONSOLE_ERRORS:
            errors = observations.get("console_errors", [])
            passed = not errors
            results.append(await _one_check(name, CheckStatus.PASSED if passed else CheckStatus.FAILED, "No browser console errors were observed." if passed else f"Observed {len(errors)} browser console error(s).", {"errors": errors}, started))
        elif name == CheckName.SAME_ORIGIN_LINKS:
            results.append(await _one_check(name, CheckStatus.WARNING, "Link inventory captured; no expected link count was supplied.", {"same_origin_count": observations.get("same_origin_link_count", 0), "external_count": observations.get("external_link_count", 0)}, started, ["Links were counted but not followed."]))
        elif name == CheckName.SECURITY_HEADERS:
            policy = evaluate_security_headers(observations.get("headers", {}), urlsplit(target_url).scheme)
            applicable = len(policy["present"]) + len(policy["missing"])
            results.append(await _one_check(
                name,
                CheckStatus.WARNING if policy["missing"] else CheckStatus.PASSED,
                f"Observed {len(policy['present'])} of {applicable} recommended security controls.",
                policy,
                started,
                ["This is an inventory, not a complete security audit."]
                + (["HSTS is not applicable over plain HTTP."] if policy["not_applicable"] else []),
            ))
        elif name == CheckName.TEXT_PRESENT and "click_error" in observations:
            results.append(await _one_check(
                name,
                CheckStatus.ERROR,
                "The requested click could not be performed, so the text was not evaluated.",
                {"click_selector": request.click_selector, "reason": observations["click_error"]},
                started,
            ))
        elif name == CheckName.TEXT_PRESENT:
            found = observations.get("text_found") is True
            evidence = {"expected_text": request.expected_text, "found": found}
            if request.click_selector:
                evidence["clicked_selector"] = request.click_selector
            results.append(await _one_check(
                name,
                CheckStatus.PASSED if found else CheckStatus.FAILED,
                "The expected text was visible on the page." if found else "The expected text was not found in the visible page text.",
                evidence,
                started,
                ["Exact, case-sensitive match against visible text after the DOM loaded"
                 + (" and one click." if request.click_selector else ".")],
            ))
        elif name == CheckName.ACCESSIBILITY and "axe_error" in observations:
            fallback = static_accessibility_scan(observations.get("document_html", ""))
            if not fallback["applicable"]:
                results.append(await _one_check(
                    name, CheckStatus.ERROR, "The accessibility engine could not run on this page.",
                    {"reason": observations["axe_error"]}, started,
                ))
            else:
                violations = fallback["violations"]
                results.append(await _one_check(
                    name,
                    CheckStatus.WARNING,
                    f"axe-core could not run; static HTML checks found {len(violations)} issue(s).",
                    {
                        "violation_count": len(violations),
                        "violations": violations,
                        "rules_passed": fallback["rules_passed"],
                        "engine": fallback["engine"],
                        "axe_error": observations["axe_error"],
                    },
                    started,
                    ["Degraded: 5 static rules replace the axe-core run, so this is never reported as a pass."],
                ))
        elif name == CheckName.ACCESSIBILITY:
            axe = observations.get("axe") or {}
            violations = axe.get("violations", [])
            serious = [v for v in violations if v.get("impact") in {"critical", "serious"}]
            status = CheckStatus.FAILED if serious else CheckStatus.WARNING if violations else CheckStatus.PASSED
            by_impact: dict[str, int] = {}
            for violation in violations:
                impact = violation.get("impact") or "unknown"
                by_impact[impact] = by_impact.get(impact, 0) + 1
            results.append(await _one_check(
                name,
                status,
                "No automated accessibility violations were found." if not violations
                else f"Found {len(violations)} accessibility rule violation(s), {len(serious)} critical or serious.",
                {
                    "violation_count": len(violations),
                    "by_impact": by_impact,
                    "violations": violations[:MAX_AXE_VIOLATIONS],
                    "rules_passed": axe.get("passes", 0),
                    "needs_review": axe.get("incomplete", 0),
                    "engine": "axe-core 4.10.3",
                },
                started,
                ["Automated rules find only part of accessibility problems; manual review is still needed.",
                 "Critical or serious violations fail the check; moderate or minor ones are warnings.",
                 "The rules run inside the page under test, so a hostile page could tamper with them."],
            ))
        elif name == CheckName.USER_FLOW:
            flow = observations.get("flow") or {"outcome": "error", "steps": []}
            outcome = flow["outcome"]
            steps = flow["steps"]
            done = sum(step["status"] == "passed" for step in steps)
            stopped = next((step for step in steps if step["status"] in {"failed", "error"}), None)
            if outcome == "passed":
                message = f"All {len(steps)} flow steps completed and every expectation held."
            elif stopped is not None:
                verb = "did not hold" if outcome == "failed" else "could not be performed"
                message = f"Step {stopped['step']} ({stopped['action']}) {verb}; {done} step(s) passed before it."
            else:
                message = "The flow could not start."
            evidence = {"steps": steps, "final_url": flow.get("final_url")}
            if flow.get("reason"):
                evidence["reason"] = flow["reason"]
            results.append(await _one_check(
                name,
                {"passed": CheckStatus.PASSED, "failed": CheckStatus.FAILED}.get(outcome, CheckStatus.ERROR),
                message,
                evidence,
                started,
                ["Steps ran in a fresh tab; form submissions and other non-GET requests are blocked by the runner.",
                 "Typed values are not stored; only their length is recorded."],
            ))
        else:
            results.append(await _browser_extra_result(name, observations, request, started))
    _attach_screenshot(results, observations.get("screenshot"), (observations.get("flow") or {}).get("screenshot"))
    return results


async def _browser_extra_result(name: CheckName, observations: dict, request: WebsiteRunRequest, started: float) -> CheckResult:
    """Checks merged in from the single-URL scanner that read the same browser observations."""
    if name == CheckName.RESPONSE_TIME:
        load_ms = observations.get("load_ms")
        passed = isinstance(load_ms, int) and load_ms <= request.max_load_time_ms
        return await _one_check(
            name, CheckStatus.PASSED if passed else CheckStatus.FAILED,
            f"The main document loaded in {load_ms} ms; limit {request.max_load_time_ms} ms.",
            {"load_ms": load_ms, "max_load_time_ms": request.max_load_time_ms}, started,
            ["Measured from navigation start to DOMContentLoaded through the safe runner."],
        )
    if name == CheckName.RESPONSE_HEADERS:
        count = len(observations.get("headers") or {})
        return await _one_check(
            name, CheckStatus.PASSED if count else CheckStatus.FAILED,
            f"The main document returned {count} response header(s) the runner passes through.",
            {"header_count": count, "header_names": sorted(observations.get("headers") or {})}, started,
            ["The safe runner passes through content type, location and security headers only."],
        )
    if name == CheckName.BODY_NOT_EMPTY:
        has_text = observations.get("body_has_text") is True
        return await _one_check(
            name, CheckStatus.PASSED if has_text else CheckStatus.FAILED,
            "The main document body contains text." if has_text else "The main document body is empty or whitespace-only.",
            {"body_bytes": observations.get("body_bytes", 0)}, started,
        )
    if name == CheckName.KEYBOARD_FOCUS:
        if "focus_error" in observations:
            return await _one_check(name, CheckStatus.ERROR, "The keyboard check could not run.",
                                    {"reason": observations["focus_error"]}, started)
        focus = observations.get("focus") or {}
        passed = focus.get("moved") or not focus.get("interactive")
        message = (
            "Pressing Tab moved focus to an interactive element." if focus.get("moved")
            else "The page has no interactive elements to reach." if not focus.get("interactive")
            else "Pressing Tab did not move focus off the document body."
        )
        return await _one_check(name, CheckStatus.PASSED if passed else CheckStatus.FAILED, message, focus, started,
                                ["Only the first Tab press is checked; a full keyboard review is still needed."])
    raise ValueError(f"unknown website check {name}")


def _probe_result(name: CheckName, probe: dict, started: float) -> CheckResult:
    def result(status: CheckStatus, message: str, evidence: dict, limitations: list[str]) -> CheckResult:
        return CheckResult(
            check_id=name, status=status, message=message, evidence=evidence, limitations=limitations,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    if probe.get("error"):
        return result(CheckStatus.ERROR, "The probe could not complete.", {"reason": probe["error"]}, [])
    findings = probe.get("findings", [])
    if name == CheckName.COOKIE_FLAGS:
        cookies = probe.get("cookies", [])
        if not cookies:
            return result(CheckStatus.PASSED, "No Set-Cookie headers were observed, so no cookie flags were missing.",
                          probe, ["Only cookies set by the first page response are checked."])
        return result(
            CheckStatus.WARNING if findings else CheckStatus.PASSED,
            f"{len(findings)} of {len(cookies)} cookie(s) lack Secure, HttpOnly or SameSite.",
            probe, ["Cookie values are never stored; only names and flags are kept."],
        )
    if name == CheckName.EXPOSED_RESOURCES:
        errored = [item for item in probe.get("probes", []) if "error" in item]
        status = CheckStatus.FAILED if findings else CheckStatus.ERROR if errored and len(errored) == len(probe.get("probes", [])) else CheckStatus.PASSED
        return result(
            status,
            f"{len(findings)} of {len(security_checks.EXPOSURE_PATHS)} sensitive paths answered HTTP 200.",
            probe, ["HEAD requests only; no file contents were read. A 200 may be a catch-all route."],
        )
    if name == CheckName.ACTIVE_PROBES:
        reflected = probe.get("canary", {}).get("reflected")
        status = CheckStatus.FAILED if reflected else CheckStatus.WARNING if findings else CheckStatus.PASSED
        return result(
            status,
            f"Active probes raised {len(findings)} lead(s) for manual review.",
            probe, ["Inert canary and unauthenticated GETs only; these are indicators, not proof of a vulnerability."],
        )
    outcome = probe.get("outcome", "error")
    evidence = {key: value for key, value in probe.items() if key not in {"outcome", "message"}}
    return result(
        {"passed": CheckStatus.PASSED, "failed": CheckStatus.FAILED, "skipped": CheckStatus.SKIPPED}.get(outcome, CheckStatus.ERROR),
        probe.get("message", "The contract run did not report a result."),
        evidence,
        ["GET operations only, 1 generated example each, 2 requests per second.",
         "Schemathesis opens its own connections to the same origin after the URL policy check."],
    )


def _attach_screenshot(
    results: list[CheckResult], screenshot: bytes | None, flow_screenshot: bytes | None = None
) -> None:
    """Store opt-in viewport screenshots only for checks that failed or errored.

    The user flow runs in its own tab, so it gets the screenshot of that tab.
    """
    failing = [r for r in results if r.status in {CheckStatus.FAILED, CheckStatus.ERROR}]
    _store_screenshot([r for r in failing if r.check_id == CheckName.USER_FLOW], flow_screenshot)
    _store_screenshot([r for r in failing if r.check_id != CheckName.USER_FLOW], screenshot)


def _store_screenshot(failing: list[CheckResult], screenshot: bytes | None) -> None:
    if not screenshot or not failing:
        return
    if len(screenshot) > MAX_SCREENSHOT_BYTES:
        for result in failing:
            result.limitations.append("A failure screenshot was captured but exceeded the 1 MB limit and was discarded.")
        return
    artifact_id = uuid4()
    save_artifact(artifact_id, screenshot)
    for result in failing:
        result.evidence["screenshot_artifact_id"] = str(artifact_id)
