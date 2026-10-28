"""Deterministic report execution shared by API and isolated workers."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from app.api_runner import run_api_checks
from app.schemas import (
    ApiRunReport,
    ApiRunRequest,
    CheckResult,
    CheckStatus,
    JobType,
    OverallStatus,
    SummaryCounts,
    WebsiteRunReport,
    WebsiteRunRequest,
)
from app.storage import LOCAL_OWNER, save_report
from app.url_policy import UnsafeTarget
from app.website_runner import run_website_checks


def summary(checks: list[CheckResult]) -> SummaryCounts:
    return SummaryCounts(
        passed=sum(check.status == CheckStatus.PASSED for check in checks),
        failed=sum(check.status == CheckStatus.FAILED for check in checks),
        warnings=sum(check.status == CheckStatus.WARNING for check in checks),
        errors=sum(check.status == CheckStatus.ERROR for check in checks),
        skipped=sum(check.status == CheckStatus.SKIPPED for check in checks),
    )


def overall_status(checks: list[CheckResult]) -> OverallStatus:
    statuses = [check.status for check in checks]
    if not statuses or all(status == CheckStatus.SKIPPED for status in statuses):
        return OverallStatus.BLOCKED
    if CheckStatus.FAILED in statuses:
        return OverallStatus.FAILED
    if CheckStatus.ERROR in statuses:
        return OverallStatus.ERROR
    if CheckStatus.WARNING in statuses or CheckStatus.SKIPPED in statuses:
        return OverallStatus.WARNING
    return OverallStatus.PASSED


def redact_target_url(url: str) -> str:
    parsed = urlsplit(url)
    sensitive = {
        "token", "access_token", "api_key", "apikey", "key", "password",
        "secret", "authorization", "session", "cookie",
    }
    query = [
        (name, "[redacted]" if name.lower() in sensitive else value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _blocked_checks(request, reason: str) -> list[CheckResult]:
    return [
        CheckResult(
            check_id=name,
            status=CheckStatus.SKIPPED,
            message="Execution was blocked by the destination safety policy.",
            evidence={"reason": reason},
        )
        for name in request.checks
    ]


def _error_checks(request, exc: Exception) -> list[CheckResult]:
    return [
        CheckResult(
            check_id=name,
            status=CheckStatus.ERROR,
            message="The runner could not complete this check.",
            evidence={"error_type": type(exc).__name__},
        )
        for name in request.checks
    ]


async def run_report(
    job_type: JobType, request: WebsiteRunRequest | ApiRunRequest
) -> WebsiteRunReport | ApiRunReport:
    """Run the deterministic checks and build a report only from their records."""
    started_at = datetime.now(timezone.utc)
    try:
        checks = (
            await run_website_checks(request)
            if job_type == JobType.WEBSITE
            else await run_api_checks(request)
        )
    except UnsafeTarget as exc:
        checks = _blocked_checks(request, str(exc))
    except Exception as exc:
        checks = _error_checks(request, exc)
    overall = overall_status(checks)
    warnings = []
    if overall not in {OverallStatus.BLOCKED, OverallStatus.ERROR}:
        warnings.append(
            "Cross-origin resources and non-GET requests are blocked by the safe runner."
            if job_type == JobType.WEBSITE
            else "Only one bounded GET request is executed; redirects and request bodies are blocked."
        )
    report_type = WebsiteRunReport if job_type == JobType.WEBSITE else ApiRunReport
    return report_type(
        run_id=uuid4(),
        target_url=redact_target_url(str(request.url)),
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        overall_status=overall,
        summary=summary(checks),
        checks=checks,
        warnings=warnings,
    )


async def execute_job(
    job_type: JobType, payload: dict, owner: str = LOCAL_OWNER
) -> WebsiteRunReport | ApiRunReport:
    request = (
        WebsiteRunRequest.model_validate(payload)
        if job_type == JobType.WEBSITE
        else ApiRunRequest.model_validate(payload)
    )
    report = await run_report(job_type, request)
    save_report(report, owner)
    return report


def isolated_worker(job_type: str, payload: dict, owner: str, connection) -> None:
    """Run one job in a fresh spawned process and return only serialized output."""
    try:
        report = asyncio.run(execute_job(JobType(job_type), payload, owner))
        connection.send({"ok": True, "report": report.model_dump(mode="json")})
    except (OSError, sqlite3.Error, Exception) as exc:
        connection.send({"ok": False, "error": type(exc).__name__})
    finally:
        connection.close()
