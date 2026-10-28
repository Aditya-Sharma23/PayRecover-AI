"""Small SQLite store for redacted execution reports, artifacts and job records.

Every row has an owner. With authentication off the owner is always "local";
with per-user tokens each user only ever sees their own runs, jobs and screenshots.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.schemas import ApiRunReport, JobRecord, JobStatus, WebsiteRunReport

Report = WebsiteRunReport | ApiRunReport
LOCAL_OWNER = "local"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_RUNS = 200


def _database_path() -> Path:
    configured = os.environ.get("TESTPILOT_DB_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "data" / "testpilot.db"


def _int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def retention_policy() -> dict[str, int]:
    return {
        "retention_days": _int_setting("TESTPILOT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 1, 3650),
        "max_runs_per_owner": _int_setting("TESTPILOT_MAX_RUNS_PER_OWNER", DEFAULT_MAX_RUNS, 1, 100_000),
    }


def _artifact_path(artifact_id: UUID) -> Path:
    return _database_path().parent / "artifacts" / f"{artifact_id}.jpg"


def save_artifact(artifact_id: UUID, content: bytes) -> None:
    """Store one screenshot artifact next to the local report database."""
    path = _artifact_path(artifact_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def get_artifact(artifact_id: UUID) -> bytes | None:
    path = _artifact_path(artifact_id)
    return path.read_bytes() if path.is_file() else None


def _report_artifacts(report_json: str) -> set[UUID]:
    found = set()
    for check in json.loads(report_json).get("checks", []):
        try:
            found.add(UUID(str(check.get("evidence", {}).get("screenshot_artifact_id"))))
        except ValueError:
            continue
    return found


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            target_url TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            overall_status TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            report_json TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT 'local'
        )
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
    if "owner" not in columns:
        # Databases created before multi-user support belong to the local user.
        connection.execute("ALTER TABLE runs ADD COLUMN owner TEXT NOT NULL DEFAULT 'local'")
    connection.execute("CREATE INDEX IF NOT EXISTS runs_owner_finished ON runs (owner, finished_at)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            created_at TEXT NOT NULL,
            record_json TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _delete_rows(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
    for row in rows:
        for artifact_id in _report_artifacts(row["report_json"]):
            _artifact_path(artifact_id).unlink(missing_ok=True)
        connection.execute("DELETE FROM runs WHERE run_id = ?", (row["run_id"],))
    return len(rows)


def prune_reports(owner: str = LOCAL_OWNER, now: datetime | None = None) -> int:
    """Apply the retention policy: drop runs older than N days or beyond the per-owner cap."""
    policy = retention_policy()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=policy["retention_days"])
    connection = _connect()
    try:
        expired = connection.execute(
            "SELECT run_id, report_json FROM runs WHERE owner = ? AND finished_at < ?",
            (owner, cutoff.isoformat()),
        ).fetchall()
        overflow = connection.execute(
            """
            SELECT run_id, report_json FROM runs WHERE owner = ?
            ORDER BY finished_at DESC LIMIT -1 OFFSET ?
            """,
            (owner, policy["max_runs_per_owner"]),
        ).fetchall()
        unique = {row["run_id"]: row for row in [*expired, *overflow]}
        removed = _delete_rows(connection, list(unique.values()))
        connection.commit()
    finally:
        connection.close()
    return removed


def save_report(report: Report, owner: str = LOCAL_OWNER) -> None:
    payload = report.model_dump(mode="json")
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT OR REPLACE INTO runs
            (run_id, target_url, started_at, finished_at, overall_status, summary_json, report_json, owner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(report.run_id),
                str(report.target_url),
                report.started_at.isoformat(),
                report.finished_at.isoformat(),
                report.overall_status.value,
                json.dumps(payload["summary"]),
                json.dumps(payload),
                owner,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    prune_reports(owner)


def list_reports(limit: int = 50, owner: str = LOCAL_OWNER) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 100))
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT run_id, target_url, started_at, finished_at, overall_status, summary_json, report_json
            FROM runs
            WHERE owner = ?
            ORDER BY finished_at DESC
            LIMIT ?
            """,
            (owner, bounded_limit),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "run_id": UUID(row["run_id"]),
            "target_url": row["target_url"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "overall_status": row["overall_status"],
            "summary": json.loads(row["summary_json"]),
            "checks": [check["check_id"] for check in json.loads(row["report_json"]).get("checks", [])],
        }
        for row in rows
    ]


def get_report(run_id: UUID, owner: str = LOCAL_OWNER) -> dict[str, Any] | None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT report_json FROM runs WHERE run_id = ? AND owner = ?", (str(run_id), owner)
        ).fetchone()
    finally:
        connection.close()
    return json.loads(row["report_json"]) if row else None


def delete_report(run_id: UUID, owner: str = LOCAL_OWNER) -> bool:
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT run_id, report_json FROM runs WHERE run_id = ? AND owner = ?", (str(run_id), owner)
        ).fetchall()
        removed = _delete_rows(connection, rows)
        connection.commit()
    finally:
        connection.close()
    return removed > 0


def delete_all_reports(owner: str = LOCAL_OWNER) -> int:
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT run_id, report_json FROM runs WHERE owner = ?", (owner,)
        ).fetchall()
        removed = _delete_rows(connection, rows)
        connection.commit()
    finally:
        connection.close()
    return removed


SECURITY_CHECK_IDS = {"security_headers", "cookie_flags", "exposed_resources", "active_probes"}
ACCESSIBILITY_CHECK_IDS = {"accessibility", "keyboard_focus"}


def dashboard_stats(owner: str = LOCAL_OWNER) -> dict[str, int]:
    """Totals for the dashboard cards, counted from the owner's stored evidence."""
    connection = _connect()
    try:
        rows = connection.execute("SELECT report_json FROM runs WHERE owner = ?", (owner,)).fetchall()
    finally:
        connection.close()
    stats = {"total_runs": len(rows), "failed_runs": 0, "security_issues": 0,
             "accessibility_issues": 0, "vulnerabilities": 0}
    for row in rows:
        report = json.loads(row["report_json"])
        if report.get("overall_status") in {"failed", "error"}:
            stats["failed_runs"] += 1
        for check in report.get("checks", []):
            evidence = check.get("evidence") or {}
            findings = len(evidence.get("findings", []))
            stats["vulnerabilities"] += findings
            if check.get("check_id") in SECURITY_CHECK_IDS and check.get("status") != "passed":
                stats["security_issues"] += max(findings, 1)
            if check.get("check_id") in ACCESSIBILITY_CHECK_IDS and check.get("status") != "passed":
                stats["accessibility_issues"] += max(int(evidence.get("violation_count", 0)), 1)
    return stats


def artifact_owned_by(artifact_id: UUID, owner: str) -> bool:
    """An artifact is visible only to the owner of a run whose evidence references it."""
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT 1 FROM runs WHERE owner = ? AND report_json LIKE ? LIMIT 1",
            (owner, f'%"screenshot_artifact_id": "{artifact_id}"%'),
        ).fetchone()
    finally:
        connection.close()
    return row is not None


def save_job(record: JobRecord, owner: str = LOCAL_OWNER) -> None:
    connection = _connect()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO jobs (job_id, owner, created_at, record_json) VALUES (?, ?, ?, ?)",
            (str(record.job_id), owner, record.created_at.isoformat(), record.model_dump_json()),
        )
        connection.commit()
    finally:
        connection.close()


def get_job(job_id: UUID, owner: str = LOCAL_OWNER) -> JobRecord | None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT record_json FROM jobs WHERE job_id = ? AND owner = ?", (str(job_id), owner)
        ).fetchone()
    finally:
        connection.close()
    return JobRecord.model_validate_json(row["record_json"]) if row else None


def list_jobs(owner: str = LOCAL_OWNER, limit: int = 20) -> list[JobRecord]:
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT record_json FROM jobs WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
            (owner, max(1, min(limit, 100))),
        ).fetchall()
    finally:
        connection.close()
    return [JobRecord.model_validate_json(row["record_json"]) for row in rows]


def fail_interrupted_jobs() -> int:
    """Jobs that were queued or running when the server stopped can never finish."""
    connection = _connect()
    try:
        rows = connection.execute("SELECT job_id, record_json FROM jobs").fetchall()
        now = datetime.now(timezone.utc)
        changed = 0
        for row in rows:
            record = JobRecord.model_validate_json(row["record_json"])
            if record.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                updated = record.model_copy(update={
                    "status": JobStatus.FAILED, "finished_at": now, "error": "ServerRestarted",
                })
                connection.execute(
                    "UPDATE jobs SET record_json = ? WHERE job_id = ?", (updated.model_dump_json(), row["job_id"])
                )
                changed += 1
        connection.commit()
    finally:
        connection.close()
    return changed
