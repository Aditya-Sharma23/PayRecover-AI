import asyncio
from uuid import uuid4
import unittest

from app.jobs import JobManager
from app.schemas import ApiRunRequest, CheckName, JobStatus, JobType
from isolation import restore_database, use_temporary_database

setUpModule = use_temporary_database
tearDownModule = restore_database


class JobTests(unittest.TestCase):
    def test_job_completes_and_records_result_id(self):
        async def scenario():
            manager = JobManager(worker_count=1)
            request = ApiRunRequest(
                url="https://example.com/",
                authorization_confirmed=True,
                checks=[CheckName.API_STATUS],
            )
            record = await manager.submit(JobType.API, request.model_dump(mode="json"))
            await asyncio.sleep(2)
            finished = manager.get(record.job_id)
            await manager.close()
            return finished

        finished = asyncio.run(scenario())
        self.assertIsNotNone(finished)
        self.assertEqual(finished.status, JobStatus.COMPLETED)
        self.assertIsNotNone(finished.report_id)

    def test_cancelled_job_is_not_reported_as_completed(self):
        async def scenario():
            manager = JobManager(worker_count=1)
            started = asyncio.Event()

            async def blocked():
                started.set()
                await asyncio.sleep(60)

            request = {"url": "https://example.com/", "authorization_confirmed": True, "checks": ["http_status"]}
            record = await manager.submit(JobType.WEBSITE, request)
            cancelled = await manager.cancel(record.job_id)
            await manager.close()
            return cancelled

        cancelled = asyncio.run(scenario())
        self.assertEqual(cancelled.status, JobStatus.CANCELLED)

    def test_worker_count_bounds_concurrent_jobs_and_queue_limit_applies(self):
        async def scenario():
            manager = JobManager(worker_count=1, queue_limit=2)
            running = 0
            peak = 0

            async def slow_execute(job_id, _payload):
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.2)
                running -= 1

            manager._execute = slow_execute
            accepted = 0
            rejected = 0
            for _ in range(5):
                try:
                    await manager.submit(JobType.API, {})
                    accepted += 1
                except RuntimeError:
                    rejected += 1
            await asyncio.sleep(1)
            await manager.close()
            return peak, accepted, rejected

        peak, accepted, rejected = asyncio.run(scenario())
        self.assertEqual(peak, 1)
        self.assertGreater(rejected, 0)
        self.assertLessEqual(accepted, 3)

    def test_run_and_wait_returns_the_finished_record_for_its_owner_only(self):
        async def scenario():
            manager = JobManager(worker_count=1)

            async def finish(job_id, _payload):
                await asyncio.sleep(0.05)
                manager._update(job_id, status=JobStatus.COMPLETED, report_id=uuid4())

            manager._execute = finish
            record = await manager.run_and_wait(JobType.API, {}, "alice", "https://example.com/")
            other = manager.get(record.job_id, "bob")
            listed = manager.list("alice")
            await manager.close()
            return record, other, listed

        record, other, listed = asyncio.run(scenario())
        self.assertEqual(record.status, JobStatus.COMPLETED)
        self.assertIsNone(other)
        self.assertEqual(listed[0].job_id, record.job_id)
        self.assertEqual(listed[0].target_url, "https://example.com/")


if __name__ == "__main__":
    unittest.main()
