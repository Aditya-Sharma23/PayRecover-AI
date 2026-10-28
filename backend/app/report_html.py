"""Safe HTML rendering for stored evidence reports."""

import base64
import html
import json
from typing import Any
from uuid import UUID

from app.storage import get_artifact


def _screenshot(evidence: dict[str, Any]) -> str:
    """Embed the screenshot so the report is self-contained and works when auth is on."""
    try:
        artifact_id = UUID(str(evidence.get("screenshot_artifact_id")))
    except ValueError:
        return ""
    content = get_artifact(artifact_id)
    if content is None:
        return "<p><em>The failure screenshot is no longer stored.</em></p>"
    encoded = base64.b64encode(content).decode("ascii")
    return (
        f'<img alt="Failure screenshot" src="data:image/jpeg;base64,{encoded}" '
        'style="max-width:100%;width:480px;margin-top:.5rem;border:1px solid #ddd">'
    )


def render_report_html(report: dict[str, Any]) -> str:
    checks = report.get("checks", [])
    check_rows = "".join(
        f"<tr><td>{html.escape(str(check.get('check_id', '')))}</td>"
        f"<td>{html.escape(str(check.get('status', '')))}</td>"
        f"<td>{html.escape(str(check.get('message', '')))}</td>"
        f"<td><pre>{html.escape(json.dumps(check.get('evidence', {}), indent=2))}</pre>"
        f"{_screenshot(check.get('evidence', {}))}</td></tr>"
        for check in checks
    )
    status = html.escape(str(report.get("overall_status", "unknown")))
    target = html.escape(str(report.get("target_url", "")))
    summary = html.escape(json.dumps(report.get("summary", {}), indent=2))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>TestPilot report</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20263a}}
.status{{font-weight:700}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:.6rem;text-align:left;vertical-align:top}}
pre{{white-space:pre-wrap;margin:0}}</style></head><body>
<h1>TestPilot AI evidence report</h1><p class="status">Overall status: {status}</p>
<p>Target: <code>{target}</code></p><h2>Summary</h2><pre>{summary}</pre>
<h2>Checks</h2><table><thead><tr><th>Check</th><th>Status</th><th>Message</th><th>Evidence</th></tr></thead>
<tbody>{check_rows}</tbody></table></body></html>"""
