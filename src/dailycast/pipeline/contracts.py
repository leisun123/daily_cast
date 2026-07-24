"""Small, typed contracts shared by the Sprint 2 task framework."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from dailycast.db.models import TaskRunStatus, TaskType, TriggerType

if TYPE_CHECKING:
    from dailycast.pipeline.context import PipelineContext

type JSONValue = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]


def canonical_json(value: Mapping[str, JSONValue]) -> str:
    """Encode a mapping into the one stable JSON representation used for task hashes."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class TaskCommand:
    """A request to create one durable TaskRun before it enters the local queue."""

    task_type: TaskType
    request: Mapping[str, JSONValue]
    config_snapshot: Mapping[str, JSONValue] = field(default_factory=dict)
    trigger_type: TriggerType = TriggerType.MANUAL
    idempotency_key: str | None = None
    business_key: str | None = None
    pipeline_version: str = "episode-v1"
    episode_id: int | None = None
    parent_task_run_id: str | None = None
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StepResult:
    """The structured result returned by a completed pipeline checkpoint."""

    input_count: int | None = None
    output_count: int | None = None
    warning_count: int = 0
    input_fingerprint: str | None = None
    output_fingerprint: str | None = None
    checkpoint_json: str | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)
    artifact_path: str | None = None
    retryable: bool = False
    llm_call_count: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_character_count: int = 0
    stop_pipeline: bool = False
    terminal_status: TaskRunStatus | None = None
    completion_code: str | None = None
    completion_summary: str | None = None

    @property
    def details_json(self) -> str:
        """Return valid, canonical JSON suitable for TaskStep.details_json."""
        return canonical_json(self.details)

    def __post_init__(self) -> None:
        """Reject impossible resource counters before they enter a durable audit record."""
        if (
            min(
                self.llm_call_count,
                self.llm_input_tokens,
                self.llm_output_tokens,
                self.tts_character_count,
            )
            < 0
        ):
            msg = "step usage counters must be non-negative"
            raise ValueError(msg)


class PipelineStep(Protocol):
    """One independently persisted, asynchronously executed pipeline checkpoint."""

    name: str

    async def run(self, context: PipelineContext) -> StepResult:
        """Execute the checkpoint without holding an application database transaction open."""
