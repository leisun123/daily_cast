"""Startup recovery that re-offers queued work and resumes stale running work safely."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.time import Clock
from dailycast.db.repositories import TaskRunRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.pipeline.submission import TaskSubmissionService


class RecoveryService:
    """Reconcile SQLite task state with the empty process-local queue on application startup."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        submission_service: TaskSubmissionService,
        *,
        stale_after_seconds: float = 60.0,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._submission_service = submission_service
        self._stale_after_seconds = stale_after_seconds
        self._clock = clock or Clock()

    async def recover(self) -> None:
        """Enqueue durable queued work, then atomically replace each stale running task."""
        for task_run_id in self._queued_task_ids():
            self._submission_service.enqueue_existing(task_run_id)

        stale_before = self._clock.now() - timedelta(seconds=self._stale_after_seconds)
        for task_run_id in self._stale_task_ids(stale_before):
            self._submission_service.recover_stale(task_run_id, stale_before=stale_before)

    def _queued_task_ids(self) -> list[str]:
        """Read queued IDs in a short, non-mutating transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return [task_run.id for task_run in TaskRunRepository(unit.session).list_queued()]

    def _stale_task_ids(self, stale_before: datetime) -> list[str]:
        """Read stale running IDs before each is recovered in its own atomic transaction."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return [
                task_run.id
                for task_run in TaskRunRepository(unit.session).list_stale_running(stale_before)
            ]
