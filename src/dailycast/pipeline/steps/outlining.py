"""Pipeline checkpoint that turns bounded selected-event evidence into a validated outline."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult


@dataclass(frozen=True, slots=True)
class OutliningStep:
    """Build bounded evidence, cache a validated outline, and persist a private checkpoint file."""

    editorial_service: AIEditorialService
    data_dir: Path
    budget_factory: Callable[[], BudgetController] = BudgetController
    name: str = "outlining"

    async def run(self, context: PipelineContext) -> StepResult:
        """Write only schema-valid canonical outline JSON under the task's controlled data root."""
        selected_event_ids = _event_ids(context.values.get("selected_news_event_ids"))
        task_step_id = context.values.get("active_task_step_id")
        if not isinstance(task_step_id, int):
            msg = "outlining requires an active persisted TaskStep ID"
            raise RuntimeError(msg)
        built = self.editorial_service.build_evidence_dossiers(selected_event_ids)
        budget = _task_budget(context, self.budget_factory)
        result = await self.editorial_service.generate_outline(
            built.event_ids,
            built.dossiers,
            task_run_id=context.task_run_id,
            task_step_id=task_step_id,
            budget=budget,
        )
        artifact_path = _write_outline(
            self.data_dir, context.artifact_run_id, result.outline.model_dump()
        )
        context.values["outlined_news_event_ids"] = built.event_ids
        context.values["episode_outline"] = result.outline
        return StepResult(
            input_count=len(selected_event_ids),
            output_count=len(result.outline.sections),
            checkpoint_json=_canonical_json(
                {
                    "artifact_id": result.artifact_id,
                    "event_ids": list(built.event_ids),
                    "outline_schema_version": result.outline.schema_version,
                }
            ),
            details={
                "artifact_id": result.artifact_id,
                "artifact_path": artifact_path,
                "cache_hit": result.cache_hit,
                "source_article_count": built.source_article_count,
                "total_evidence_chars": built.total_evidence_chars,
                "llm_input_tokens": result.usage.input_tokens,
                "llm_output_tokens": result.usage.output_tokens,
            },
            artifact_path=artifact_path,
            llm_call_count=result.provider_call_count,
            llm_input_tokens=result.usage.input_tokens if not result.cache_hit else 0,
            llm_output_tokens=result.usage.output_tokens if not result.cache_hit else 0,
        )


def _event_ids(value: object) -> tuple[int, ...]:
    """Accept only the durable selected-event IDs written by the ranking checkpoint."""
    if isinstance(value, tuple) and all(isinstance(event_id, int) for event_id in value):
        return value
    return ()


def _task_budget(
    context: PipelineContext, budget_factory: Callable[[], BudgetController]
) -> BudgetController:
    """Share the same per-TaskRun LLM budget that ranking already reserved against."""
    existing = context.values.get("llm_budget")
    if isinstance(existing, BudgetController):
        return existing
    budget = budget_factory()
    context.values["llm_budget"] = budget
    return budget


def _write_outline(data_dir: Path, task_run_id: str, outline: object) -> str:
    """Atomically persist one canonical validated outline below a path not controlled by input."""
    relative_path = Path("work") / task_run_id / "editorial" / "outline.json"
    root = data_dir.resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        msg = "outline artifact path escaped the configured data directory"
        raise RuntimeError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(_canonical_json(outline), encoding="utf-8")
    os.replace(temporary, target)
    return relative_path.as_posix()


def _canonical_json(value: object) -> str:
    """Use one stable JSON form for TaskStep checkpoints and outline artifact files."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
