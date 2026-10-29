"""Reflected XSS probe: many inert payloads, each in many injection points.

Every payload carries a unique random token (``tpx`` + hex) and calls nothing that
exists, so even a page that reflected it raw would only raise a ReferenceError. The
probe never renders pages: it sends GET requests through the pinned probe client and
reads the response text. For each payload it reports one of:

  unescaped      the dangerous part came back raw in an HTML response (a finding)
  non_html       it came back raw, but in JSON or plain text (lower risk; a warning)
  encoded        the token came back, but with its special characters escaped (safe)
  not_reflected  the token did not come back at all

Stored and DOM-based XSS, POST forms and headers are out of scope and listed as limitations.
"""

import asyncio
import re
import secrets
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from app.security_checks import MAX_PROBE_BODY_BYTES, _safe, finding

# (id, label, context, severity, payload, signature). "{t}" is the unique token; the
# signature is the part that proves the payload was not escaped. A signature starting
# with "re:" is a regular expression, for payloads that are only dangerous in one place.
PAYLOADS = (
    ("html_tag", "Raw HTML tag", "HTML body", "high", "<{t}>", "<{t}>"),
    ("script_tag", "Script element", "HTML body", "high", "<script>{t}</script>", "<script>{t}"),
    ("img_onerror", "Image error handler", "HTML body", "high", "<img src=x onerror={t}>", "onerror={t}>"),
    ("svg_onload", "SVG load handler", "HTML body", "high", "<svg/onload={t}>", "<svg/onload={t}"),
    ("iframe_js", "Iframe with javascript: URL", "HTML body", "high", "<iframe src=javascript:{t}>", "<iframe src=javascript:{t}"),
    ("details_ontoggle", "Details toggle handler", "HTML body", "high", "<details open ontoggle={t}>", "ontoggle={t}>"),
    ("attr_double", "Break out of a \"double-quoted\" attribute", "HTML attribute", "high", "\"><{t}>", "\"><{t}>"),
    ("attr_single", "Break out of a 'single-quoted' attribute", "HTML attribute", "high", "'><{t}>", "'><{t}>"),
    ("attr_event", "New event attribute, no angle brackets", "HTML attribute", "medium",
     "\" autofocus onfocus={t} x=\"", "\" autofocus onfocus={t}"),
    ("js_string_double", "Break out of a \"double-quoted\" JavaScript string", "JavaScript", "medium", "\";{t}//", "\";{t}"),
    ("js_string_single", "Break out of a 'single-quoted' JavaScript string", "JavaScript", "medium", "';{t}//", "';{t}"),
    ("script_close", "Close the page's own script block", "JavaScript", "high", "</script><{t}>", "</script><{t}>"),
    ("javascript_url", "javascript: URL in a link or source", "URL attribute", "medium", "javascript:{t}",
     r"re:\b(?:href|src|action|formaction|data)\s*=\s*[\"']?\s*javascript:{t}"),
    ("textarea_breakout", "Break out of a textarea or title", "RCDATA element", "high",
     "</textarea></title><{t}>", "</textarea></title><{t}>"),
    ("polyglot", "Mixed-case polyglot", "Several contexts", "high",
     "jaVasCript:/*-/*`/*'/*\"/**/(/* */oNcliCk={t} )//</stYle/</titLe/</teXtarEa/</scRipt/--!><sVg/oNloAd={t}//>",
     "<sVg/oNloAd={t}"),
)
# Parameter names pages commonly echo back: search boxes, messages, redirects, callbacks.
PARAM_NAMES = (
    "q", "search", "query", "s", "keyword", "id", "name", "message", "error",
    "redirect", "next", "url", "callback", "page", "lang",
)
# Payloads also tried as a path segment, where 404 pages often echo the path.
PATH_PAYLOADS = ("html_tag", "img_onerror", "attr_double")
MAX_CONCURRENT = 4
HTML_TYPES = ("text/html", "application/xhtml+xml")


def _url(target: str, path: str | None = None, extra: list[tuple[str, str]] | None = None) -> str:
    parts = urlsplit(target)
    query = parse_qsl(parts.query, keep_blank_values=True) + (extra or [])
    return urlunsplit((parts.scheme, parts.netloc, path if path is not None else parts.path or "/", urlencode(query), ""))


def classify(body: str, content_type: str, token: str, signature: str) -> str:
    """unescaped, non_html, encoded or not_reflected for one token in one response."""
    if token not in body:
        return "not_reflected"
    if signature.startswith("re:"):
        raw = re.search(signature[3:], body, re.IGNORECASE) is not None
    else:
        raw = signature.lower() in body.lower()
    if raw:
        is_html = any(kind in content_type.lower() for kind in HTML_TYPES) or not content_type
        return "unescaped" if is_html else "non_html"
    return "encoded"


async def scan(target: str) -> dict:
    """Send every payload through every parameter and the path; return per-payload outcomes."""
    from app import security_checks

    run = secrets.token_hex(4)
    existing = [name for name, _ in parse_qsl(urlsplit(target).query, keep_blank_values=True)]
    params = list(dict.fromkeys([*existing, *PARAM_NAMES]))
    gate = asyncio.Semaphore(MAX_CONCURRENT)
    csp_seen = False

    async def fetch(url: str):
        async with gate:
            return await _safe(security_checks.fetch_probe(url, "GET", MAX_PROBE_BODY_BYTES))

    async def query_probe(index: int, payload: tuple) -> dict:
        nonlocal csp_seen
        pid, label, context, severity, template, signature = payload
        # One request per payload: every parameter gets its own token, so a hit names its parameter.
        tokens = {name: f"tpx{run}{index:02d}{slot:02d}" for slot, name in enumerate(params)}
        extra = [(name, template.format(t=token)) for name, token in tokens.items()]
        response, error = await fetch(_url(target, extra=extra))
        if error:
            return {"id": pid, "outcome": "error", "error": error}
        text = response.body.decode("utf-8", errors="replace")
        content_type = response.headers.get("content-type", "")
        csp_seen = csp_seen or "content-security-policy" in response.headers
        hits = {name: classify(text, content_type, token, signature.format(t=token)) for name, token in tokens.items()}
        return {"id": pid, "status_code": response.status, "content_type": content_type.split(";")[0], "hits": hits}

    async def path_probe(index: int, payload: tuple) -> dict:
        pid, _, _, _, template, signature = payload
        token = f"tpx{run}{index:02d}pp"
        response, error = await fetch(_url(target, path="/" + quote(template.format(t=token), safe="")))
        if error:
            return {"id": pid, "outcome": "error", "error": error}
        text = response.body.decode("utf-8", errors="replace")
        outcome = classify(text, response.headers.get("content-type", ""), token, signature.format(t=token))
        return {"id": pid, "status_code": response.status, "hits": {"(URL path)": outcome}}

    by_id = {payload[0]: (index, payload) for index, payload in enumerate(PAYLOADS)}
    results = await asyncio.gather(
        *(query_probe(index, payload) for index, payload in enumerate(PAYLOADS)),
        *(path_probe(by_id[pid][0], by_id[pid][1]) for pid in PATH_PAYLOADS),
    )

    rank = {"unescaped": 4, "non_html": 3, "encoded": 2, "not_reflected": 1, "error": 0}
    merged: dict[str, dict] = {}
    for item in results:
        pid = item["id"]
        _, (_, label, context, severity, template, _) = by_id[pid]
        entry = merged.setdefault(pid, {
            "id": pid, "label": label, "context": context, "severity": severity,
            "payload": template.format(t="tpx…"), "outcome": "error", "reflected_in": [], "encoded_in": [],
        })
        if "error" in item:
            entry.setdefault("errors", []).append(item["error"])
            continue
        for where, outcome in item["hits"].items():
            if outcome in {"unescaped", "non_html"}:
                entry["reflected_in"].append(where)
            elif outcome == "encoded":
                entry["encoded_in"].append(where)
            if rank[outcome] > rank[entry["outcome"]]:
                entry["outcome"] = outcome
    payloads = list(merged.values())

    findings = [
        finding(
            f"Reflected XSS: {entry['label']}",
            entry["severity"],
            f"The {entry['context']} payload came back unescaped from: {', '.join(entry['reflected_in'][:6])}. "
            "The payload is inert and no script was run, so confirm it in a browser.",
            "Encode output for its exact context (HTML, attribute, JavaScript or URL), and add a strict Content-Security-Policy.",
        )
        for entry in payloads if entry["outcome"] == "unescaped"
    ]
    unescaped = [entry for entry in payloads if entry["outcome"] == "unescaped"]
    non_html = [entry for entry in payloads if entry["outcome"] == "non_html"]
    errors = [entry for entry in payloads if entry["outcome"] == "error"]
    if unescaped:
        status = "failed"
        summary = f"XSS found: {len(unescaped)} of {len(payloads)} payloads came back unescaped in HTML."
    elif len(errors) == len(payloads):
        status = "error"
        summary = "The XSS probe could not reach the page."
    elif non_html:
        status = "warning"
        summary = f"No XSS in HTML, but {len(non_html)} payload(s) came back raw in a non-HTML response."
    else:
        status = "passed"
        encoded = sum(1 for entry in payloads if entry["outcome"] == "encoded")
        summary = (
            f"No XSS found: {len(payloads)} payloads in {len(params)} parameters and the URL path were "
            + (f"safely encoded ({encoded}) or not reflected." if encoded else "not reflected.")
        )
    return {
        "status": status,
        "summary": summary,
        "payload_count": len(PAYLOADS),
        "parameters": params,
        "requests": len(results),
        "content_security_policy": csp_seen,
        "payloads": payloads,
        "findings": findings,
        "limitations": [
            "Reflected XSS only: stored and DOM-based XSS need a browser and test data.",
            "GET query parameters and the URL path only; forms, POST bodies and headers are not tried.",
            "Payloads are inert and pages are not rendered, so a hit is strong evidence but not a working exploit.",
        ],
    }
