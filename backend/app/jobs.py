"""Bounded asynchronous job queue for deterministic test execution.

Each job runs in a fresh spawned process. Job records are written to SQLite so
their status survives a restart; jobs interrupted by a restart are marked failed.
"""

import asyncio
import multiprocessing
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.execution import isolated_worker
from app.schemas import JobRecord, JobStatus, JobType
from app.storage import LOCAL_OWNER, fail_interrupted_jobs, get_job, list_jobs, save_job

TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class JobManager:
    def __init__(self, worker_count: int = 2, queue_limit: int = 20):
        self._queue: asyncio.Queue[tuple[UUID, dict]] = asyncio.Queue(maxsize=queue_limit)
        self._records: dict[UUID, JobRecord] = {}
        self._owners: dict[UUID, str] = {}
        self._done: dict[UUID, asyncio.Event] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._processes: dict[UUID, multiprocessing.Process] = {}
        self._worker_count = worker_count
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(), name=f"testpilot-worker-{index}")
            for index in range(self._worker_count)
        ]

    @staticmethod
    def recover() -> int:
        """Mark jobs left queued or running by a previous server process as failed."""
        return fail_interrupted_jobs()

    async def close(self) -> None:
        active_tasks = list(self._tasks.values())
        workers = list(self._workers)
        for task in active_tasks:
            if not task.done():
                task.cancel()
        for task in workers:
            task.cancel()
        for process in list(self._processes.values()):
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
        await asyncio.gather(*(self._swallow_cancellation(task) for task in active_tasks))
        await asyncio.gather(*(self._swallow_cancellation(task) for task in workers))
        self._workers.clear()
        self._started = False

    @staticmethod
    async def _swallow_cancellation(task: asyncio.Task[None]) -> None:
        try:
            await task
        except BaseException:
            pass

    def _update(self, job_id: UUID, **changes) -> JobRecord:
        record = self._records[job_id].model_copy(update=changes)
        self._records[job_id] = record
        save_job(record, self._owners.get(job_id, LOCAL_OWNER))
        if record.status in TERMINAL_STATUSES and job_id in self._done:
            self._done[job_id].set()
        return record

    async def submit(
        self, job_type: JobType, payload: dict, owner: str = LOCAL_OWNER, target_url: str | None = None
    ) -> JobRecord:
        await self.start()
        job_id = uuid4()
        record = JobRecord(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.QUEUED,
            created_at=datetime.now(timezone.utc),
            target_url=target_url,
        )
        try:
            self._queue.put_nowait((job_id, payload))
        except asyncio.QueueFull:
            raise RuntimeError("job queue is full") from None
        self._records[job_id] = record
        self._owners[job_id] = owner
        self._done[job_id] = asyncio.Event()
        save_job(record, owner)
        return record

    async def run_and_wait(
        self, job_type: JobType, payload: dict, owner: str = LOCAL_OWNER, target_url: str | None = None
    ) -> JobRecord:
        """Queue a job and wait for it; used so public deployments never run checks in the API process."""
        record = await self.submit(job_type, payload, owner, target_url)
        await self._done[record.job_id].wait()
        return self._records[record.job_id]

    def get(self, job_id: UUID, owner: str = LOCAL_OWNER) -> JobRecord | None:
        if job_id in self._records:
            return self._records[job_id] if self._owners.get(job_id) == owner else None
        return get_job(job_id, owner)

    def list(self, owner: str = LOCAL_OWNER, limit: int = 20) -> list[JobRecord]:
        return list_jobs(owner, limit)

    async def cancel(self, job_id: UUID, owner: str = LOCAL_OWNER) -> JobRecord | None:
        record = self.get(job_id, owner)
        if record is None:
            return None
        if job_id in self._records and record.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            task = self._tasks.get(job_id)
            if task is not None:
                task.cancel()
            return self._update(job_id, status=JobStatus.CANCELLED, finished_at=datetime.now(timezone.utc))
        return record

    async def _worker(self) -> None:
        while True:
            job_id, payload = await self._queue.get()
            if self._records.get(job_id, None) and self._records[job_id].status == JobStatus.CANCELLED:
                self._queue.task_done()
                continue
            task = asyncio.create_task(self._execute(job_id, payload))
            self._tasks[job_id] = task
            try:
                # Wait for this job before taking the next one so worker_count
                # bounds concurrent worker processes. asyncio.wait does not
                # raise when the job itself is cancelled.
                await asyncio.wait({task})
            finally:
                self._tasks.pop(job_id, None)
                self._queue.task_done()

    async def _execute(self, job_id: UUID, payload: dict) -> None:
        record = self._update(job_id, status=JobStatus.RUNNING, started_at=datetime.now(timezone.utc))
        try:
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe(duplex=False)
            process = context.Process(
                target=isolated_worker,
                args=(record.job_type.value, payload, self._owners.get(job_id, LOCAL_OWNER), child),
                daemon=True,
            )
            self._processes[job_id] = process
            process.start()
            child.close()
            while process.is_alive() and not parent.poll():
                await asyncio.sleep(0.05)
            result = parent.recv() if parent.poll() else {"ok": False, "error": "WorkerProcessFailure"}
            process.join(timeout=2)
            self._processes.pop(job_id, None)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "WorkerProcessFailure"))
            self._update(
                job_id,
                status=JobStatus.COMPLETED,
                finished_at=datetime.now(timezone.utc),
                report_id=result["report"]["run_id"],
            )
        except asyncio.CancelledError:
            process = self._processes.pop(job_id, None)
            if process is not None and process.is_alive():
                process.terminate()
                process.join(timeout=2)
            raise
        except Exception as exc:
            self._update(
                job_id,
                status=JobStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                error=str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
            )
