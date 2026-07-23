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
    """Queue task IDs locally while keeping SQLite as the execution source of truth."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        orchestrator: PipelineOrchestrator,
        *,
        queue_maxsize: int = 100,
        heartbeat_interval_seconds: float = 15.0,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._orchestrator = orchestrator
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._clock = clock or Clock()
        self._accepting = True
        self._shutdown_requested = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

    def enqueue(self, task_run_id: str) -> None:
        """Offer a committed task identifier without ever treating memory as persistence."""
        if not self._accepting:
            logger.info("task_enqueue_skipped_shutdown", extra={"task_run_id": task_run_id})
            return
        try:
            self._queue.put_nowait(task_run_id)
        except asyncio.QueueFull:
            # The committed queued row remains recoverable through the database scan on restart.
            logger.warning("task_queue_full", extra={"task_run_id": task_run_id})

    async def start(self) -> None:
        """Start exactly one worker coroutine, preserving single heavy-task concurrency."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._accepting = True
        self._shutdown_requested.clear()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="dailycast-task-worker")

    async def shutdown(self, grace_seconds: float) -> None:
        """Stop accepting work and allow the active checkpoint to finish within its grace period."""
        self._accepting = False
        self._shutdown_requested.set()
        worker = self._worker_task
        if worker is None:
            return
        # The worker will consume its existing work before observing shutdown at its next boundary.
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=grace_seconds)
        except TimeoutError:
            logger.warning("task_executor_shutdown_grace_expired")

    async def _worker_loop(self) -> None:
        """Execute task IDs serially; the orchestrator ignores duplicate queue entries."""
        while True:
            task_run_id = await self._queue.get()
            try:
                if task_run_id is None or self._shutdown_requested.is_set():
                    return
                await self._execute_one(task_run_id)
            finally:
                self._queue.task_done()

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
