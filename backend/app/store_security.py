"""The store-wide security check: holes that could let money leak, even when no money has leaked yet.

Runs once per store with a scan (not once per order), using the same probes as an order
investigation:
  * price tampering: does the store charge a price the browser sends? (one unapproved
    Sandbox order is created; no money moves)
  * reflected XSS: does the store page echo input back unescaped?
  * security headers: Content-Security-Policy, clickjacking protection, MIME sniffing protection.
Findings decide the scan's middle verdict, "no leak, but the store is vulnerable".
"""

import aiohttp

from app import investigate
from app.schemas import CheckStatus, Investigation, SecurityFinding, StoreSecurity

PRICE_FIX = "Price every cart on the server from the catalog, and ignore any price the browser sends."
XSS_FIX = "Escape everything a visitor types before putting it in a page, and send a Content-Security-Policy."
HEADERS_FIX = ("Send Content-Security-Policy, X-Frame-Options (or CSP frame-ancestors) and "
               "X-Content-Type-Options: nosniff on every page.")
HEADER_NAMES = {
    "content-security-policy": "Content-Security-Policy",
    "x-frame-options": "X-Frame-Options",
    "x-content-type-options": "X-Content-Type-Options",
}


def price_finding(message: str) -> SecurityFinding:
    return SecurityFinding(name="price_tampering", severity="high", title="Buyers can change prices",
                           detail=message, fix=PRICE_FIX)


def xss_finding(summary: str) -> SecurityFinding:
    return SecurityFinding(name="xss", severity="high", title="The store page runs code a visitor sends (XSS)",
                           detail=summary, fix=XSS_FIX)


def missing_headers(headers: dict[str, str]) -> list[str]:
    lowered = {key.lower(): value for key, value in headers.items()}
    missing = [label for key, label in HEADER_NAMES.items() if key not in lowered]
    # A CSP frame-ancestors rule protects against clickjacking as well as X-Frame-Options does.
    if "X-Frame-Options" in missing and "frame-ancestors" in lowered.get("content-security-policy", ""):
        missing.remove("X-Frame-Options")
    return missing


def headers_finding(missing: list[str]) -> SecurityFinding:
    return SecurityFinding(name="security_headers", severity="medium", title="Security headers are missing",
                           detail=f"The store page does not send {', '.join(missing)}.", fix=HEADERS_FIX)


async def _page_headers() -> dict[str, str]:
    url = f"{investigate.store_url()}/"
    try:
        async with investigate._session() as session:
            async with session.get(url, allow_redirects=False) as response:
                return dict(response.headers)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise investigate.StoreUnavailable(f"The store at {investigate.store_url()} could not be reached.") from exc


async def _catalog():
    async with investigate._session() as session:
        _, products = await investigate._store_json(session, "GET", "/api/products")
    if not isinstance(products, list):
        return None
    return {item["id"]: investigate._decimal(item["price"]) for item in products if isinstance(item, dict) and "id" in item}


async def check(store: str) -> StoreSecurity:
    """Probe the store once for the holes behind leaks. Never raises for an unreachable store."""
    investigate._active_store.set(store)
    findings: list[SecurityFinding] = []
    try:
        for probe in await investigate.probe_store(await _catalog()):
            if probe.status == CheckStatus.FAILED:
                findings.append(price_finding(probe.message))
        xss = await investigate.scan_xss()
        if xss.get("status") == "failed":
            findings.append(xss_finding(xss.get("summary", "")))
        missing = missing_headers(await _page_headers())
        if missing:
            findings.append(headers_finding(missing))
    except investigate.StoreUnavailable as exc:
        return StoreSecurity(checked=False, findings=findings, note=str(exc))
    return StoreSecurity(checked=True, findings=findings)


def from_investigation(result: Investigation) -> StoreSecurity:
    """The same findings, read from an order investigation's own probes."""
    findings = [price_finding(c.message) for c in result.checks
                if c.name == "price_tampering_probe" and c.status == CheckStatus.FAILED]
    if result.xss_scan and result.xss_scan.get("status") == "failed":
        findings.append(xss_finding(result.xss_scan.get("summary", "")))
    report = result.website_report
    headers = next((c for c in report.checks if c.check_id == "security_headers"), None) if report else None
    if headers is not None and headers.status == "failed":
        findings.append(SecurityFinding(name="security_headers", severity="medium", title="Security headers are missing",
                                        detail=headers.message, fix=HEADERS_FIX))
    return StoreSecurity(checked=bool(result.xss_scan or report or any(
        c.name == "price_tampering_probe" for c in result.checks)), findings=findings)


def verdict(has_leaks: bool, security: StoreSecurity | None) -> str:
    if has_leaks:
        return "leaking"
    return "vulnerable" if security and security.findings else "clear"
