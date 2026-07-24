"""Execution context passed to each task pipeline checkpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.time import Clock


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """The narrow runtime state a pipeline step may use during one TaskRun."""

    task_run_id: str
    session_factory: sessionmaker[Session]
    shutdown_requested: asyncio.Event
    clock: Clock
    values: dict[str, object] = field(default_factory=dict)

    @property
    def artifact_run_id(self) -> str:
        """Return this attempt's private artifact root; recovery never rewrites ancestor output."""
        return self.task_run_id
