"""Single-concurrency in-process executor backed by durable TaskRun records."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.time import Clock
from dailycast.db.models import TaskRunStatus
from dailycast.db.repositories import TaskRunRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.pipeline.orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)


class InProcessTaskExecutor:
    """Queue task IDs locally while SQLite remains the sole durable execution source."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        orchestrator: PipelineOrchestrator,
        *,
        queue_maxsize: int = 100,
        heartbeat_interval_seconds: float = 15.0,
        redelivery_interval_seconds: float = 1.0,
        clock: Clock | None = None,
    ) -> None:
        if redelivery_interval_seconds <= 0:
            msg = "redelivery_interval_seconds must be positive"
            raise ValueError(msg)
        self._session_factory = session_factory
        self._orchestrator = orchestrator
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._queued_task_run_ids: set[str] = set()
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._redelivery_interval_seconds = redelivery_interval_seconds
        self._clock = clock or Clock()
        self._accepting = True
        self._shutdown_requested = asyncio.Event()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._last_worker_error: str | None = None

    @property
    def is_healthy(self) -> bool:
        """Expose whether the supervisor and its single worker are alive for readiness."""
        return (
            self._accepting
            and self._supervisor_task is not None
            and not self._supervisor_task.done()
            and self._worker_task is not None
            and not self._worker_task.done()
        )

    @property
    def readiness_detail(self) -> str:
        """Return a safe operator detail without exposing exception traces."""
        if self.is_healthy:
            return "single worker supervisor is running"
        if self._last_worker_error is not None:
            return f"worker supervisor is recovering: {self._last_worker_error}"
        return "single worker supervisor is not running"

    def enqueue(self, task_run_id: str) -> bool:
        """Offer a committed identifier and report whether local delivery was accepted."""
        if not self._accepting:
            logger.info("task_enqueue_skipped_shutdown", extra={"task_run_id": task_run_id})
            return False
        if task_run_id in self._queued_task_run_ids:
            return True
        try:
            self._queue.put_nowait(task_run_id)
        except asyncio.QueueFull:
            # The committed queued row is redelivered by this worker after capacity frees.
            logger.warning("task_queue_full", extra={"task_run_id": task_run_id})
            return False
        self._queued_task_run_ids.add(task_run_id)
        return True

    async def start(self) -> None:
        """Start one restart-supervised worker and immediately reconcile queued durable rows."""
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        self._accepting = True
        self._shutdown_requested.clear()
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(), name="dailycast-task-supervisor"
        )
        self._redeliver_queued()
        # Let the supervisor create its worker before readiness can be observed.
        await asyncio.sleep(0)

    async def shutdown(self, grace_seconds: float) -> None:
        """Stop new work and leave an over-grace running row for startup recovery."""
        self._accepting = False
        self._shutdown_requested.set()
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        supervisor = self._supervisor_task
        if supervisor is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(supervisor), timeout=grace_seconds)
        except TimeoutError:
            logger.warning("task_executor_shutdown_grace_expired")
            # Do not write a synthetic terminal status. The durable running row is explicitly
            # recovered as interrupted when a later process observes its expired heartbeat.
            worker = self._worker_task
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor

    async def _supervisor_loop(self) -> None:
        """Restart an unexpectedly terminated worker without adding another process/service."""
        while not self._shutdown_requested.is_set():
            worker = asyncio.create_task(self._worker_loop(), name="dailycast-task-worker")
            self._worker_task = worker
            try:
                await worker
            except asyncio.CancelledError:
                raise
            except Exception as error:  # Defensive boundary around worker implementation bugs.
                self._last_worker_error = error.__class__.__name__
                logger.exception("task_worker_crashed_restarting")
                if not self._shutdown_requested.is_set():
                    await asyncio.sleep(0)
                    continue
            if not self._shutdown_requested.is_set():
                self._last_worker_error = "worker exited unexpectedly"
                logger.error("task_worker_exited_restarting")
                await asyncio.sleep(0)
                continue
        self._worker_task = None

    async def _worker_loop(self) -> None:
        """Serially execute IDs and isolate one task failure from future queued work."""
        while True:
            try:
                task_run_id = await asyncio.wait_for(
                    self._queue.get(), timeout=self._redelivery_interval_seconds
                )
            except TimeoutError:
                self._redeliver_queued()
                continue
            try:
                if task_run_id is None or self._shutdown_requested.is_set():
                    return
                self._queued_task_run_ids.discard(task_run_id)
                try:
                    await self._execute_one(task_run_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A task cannot kill the only worker. Its durable TaskRun remains inspectable
                    # and recovery can determine its next action after restart.
                    logger.exception(
                        "task_execution_boundary_failed", extra={"task_run_id": task_run_id}
                    )
            finally:
                self._queue.task_done()
                if not self._shutdown_requested.is_set():
                    self._redeliver_queued()

    async def _execute_one(self, task_run_id: str) -> None:
        """Keep a separate heartbeat coroutine alive while the orchestrator does its work."""
        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(task_run_id, stop_heartbeat),
            name=f"dailycast-heartbeat-{task_run_id}",
        )
        try:
            await self._orchestrator.execute(task_run_id, self._shutdown_requested)
        finally:
            stop_heartbeat.set()
            await heartbeat_task

    def _redeliver_queued(self) -> None:
        """Best-effort refill after queue pressure; the database remains authoritative."""
        if not self._accepting:
            return
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            queued_ids = [run.id for run in TaskRunRepository(unit.session).list_queued()]
        for task_run_id in queued_ids:
            if not self.enqueue(task_run_id):
                break

    async def _heartbeat_loop(self, task_run_id: str, stop_heartbeat: asyncio.Event) -> None:
        """Write independent short heartbeat transactions while a TaskRun is running."""
        while True:
            try:
                await asyncio.wait_for(
                    stop_heartbeat.wait(), timeout=self._heartbeat_interval_seconds
                )
                return
            except TimeoutError:
                self._write_heartbeat(task_run_id)

    def _write_heartbeat(self, task_run_id: str) -> None:
        """Commit one heartbeat without sharing any pipeline-step transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            task_run = TaskRunRepository(unit.session).get(task_run_id)
            if task_run is not None and task_run.status == TaskRunStatus.RUNNING:
                TaskRunRepository(unit.session).update_heartbeat(task_run, self._clock.now())
