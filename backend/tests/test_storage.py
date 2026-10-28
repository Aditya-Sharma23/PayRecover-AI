import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from app.schemas import (
    CheckName,
    CheckResult,
    CheckStatus,
    OverallStatus,
    SummaryCounts,
    WebsiteRunReport,
)
from app.schemas import JobRecord, JobStatus, JobType
from app.storage import (
    _connect,
    fail_interrupted_jobs,
    get_artifact,
    get_job,
    get_report,
    list_reports,
    save_artifact,
    save_job,
    save_report,
)
from isolation import restore_database, use_temporary_database


def report_at(finished: datetime, artifact: str | None = None) -> WebsiteRunReport:
    evidence = {"count": 0}
    if artifact:
        evidence["screenshot_artifact_id"] = artifact
    return WebsiteRunReport(
        run_id=uuid4(),
        target_url="https://example.com/",
        started_at=finished,
        finished_at=finished,
        overall_status=OverallStatus.FAILED,
        summary=SummaryCounts(failed=1),
        checks=[CheckResult(check_id=CheckName.H1, status=CheckStatus.FAILED, message="No H1.", evidence=evidence)],
    )


class RetentionAndJobTests(unittest.TestCase):
    def setUp(self):
        use_temporary_database()

    def tearDown(self):
        restore_database()

    def test_old_runs_and_runs_beyond_the_cap_are_pruned_with_their_screenshots(self):
        now = datetime.now(timezone.utc)
        artifact = uuid4()
        save_artifact(artifact, b"jpeg")
        old = report_at(now - timedelta(days=40), str(artifact))
        save_report(old)
        self.assertIsNone(get_report(old.run_id))
        self.assertIsNone(get_artifact(artifact))

        with patch.dict(os.environ, {"TESTPILOT_MAX_RUNS_PER_OWNER": "2"}):
            reports = [report_at(now - timedelta(minutes=offset)) for offset in (3, 2, 1)]
            for report in reports:
                save_report(report)
            kept = {row["run_id"] for row in list_reports(10)}
        self.assertEqual(kept, {reports[1].run_id, reports[2].run_id})

    def test_runs_are_scoped_to_their_owner(self):
        report = report_at(datetime.now(timezone.utc))
        save_report(report, "alice")
        self.assertIsNotNone(get_report(report.run_id, "alice"))
        self.assertIsNone(get_report(report.run_id, "bob"))
        self.assertEqual(list_reports(10, "bob"), [])

    def test_job_records_survive_and_interrupted_jobs_are_failed(self):
        record = JobRecord(job_id=uuid4(), job_type=JobType.API, status=JobStatus.RUNNING,
                           created_at=datetime.now(timezone.utc))
        save_job(record, "alice")
        self.assertIsNone(get_job(record.job_id, "bob"))
        self.assertEqual(fail_interrupted_jobs(), 1)
        restored = get_job(record.job_id, "alice")
        self.assertEqual(restored.status, JobStatus.FAILED)
        self.assertEqual(restored.error, "ServerRestarted")

    def test_databases_from_before_ownership_are_migrated(self):
        path = os.environ["TESTPILOT_DB_PATH"]
        legacy = sqlite3.connect(path)
        legacy.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, target_url TEXT NOT NULL, started_at TEXT NOT NULL, "
                       "finished_at TEXT NOT NULL, overall_status TEXT NOT NULL, summary_json TEXT NOT NULL, "
                       "report_json TEXT NOT NULL)")
        legacy.commit()
        legacy.close()
        connection = _connect()
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
        connection.close()
        self.assertIn("owner", columns)


class StorageTests(unittest.TestCase):
    def test_reports_round_trip_through_local_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("TESTPILOT_DB_PATH")
            os.environ["TESTPILOT_DB_PATH"] = os.path.join(directory, "runs.db")
            try:
                run_id = uuid4()
                now = datetime.now(timezone.utc)
                report = WebsiteRunReport(
                    run_id=run_id,
                    target_url="https://example.com/?token=%5Bredacted%5D",
                    started_at=now,
                    finished_at=now,
                    overall_status=OverallStatus.PASSED,
                    summary=SummaryCounts(passed=1),
                    checks=[CheckResult(
                        check_id=CheckName.HTTP_STATUS,
                        status=CheckStatus.PASSED,
                        message="Observed HTTP status 200.",
                        evidence={"status_code": 200},
                    )],
                )
                save_report(report)
                self.assertEqual(get_report(run_id)["run_id"], str(run_id))
                self.assertEqual(list_reports(1)[0]["run_id"], run_id)
            finally:
                if previous is None:
                    os.environ.pop("TESTPILOT_DB_PATH", None)
                else:
                    os.environ["TESTPILOT_DB_PATH"] = previous


if __name__ == "__main__":
    unittest.main()
