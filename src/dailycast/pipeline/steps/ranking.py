"""Pipeline checkpoint that scores clustered NewsEvents and persists deterministic selection."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult


@dataclass(frozen=True, slots=True)
class RankingStep:
    """Use the LLM only to score bounded EventCards; code owns the final top-N selection."""

    editorial_service: AIEditorialService
    budget_factory: Callable[[], BudgetController]
    name: str = "ranking"

    async def run(self, context: PipelineContext) -> StepResult:
        """Score clustering output using the TaskStep provenance supplied by the orchestrator."""
        event_ids = _event_ids(context.values.get("news_event_ids"))
        task_step_id = context.values.get("active_task_step_id")
        if not isinstance(task_step_id, int):
            msg = "ranking requires an active persisted TaskStep ID"
            raise RuntimeError(msg)
        if not event_ids:
            return StepResult(
                input_count=0,
                output_count=0,
                warning_count=1,
                checkpoint_json=json.dumps(
                    {"scored_event_ids": [], "selected_event_ids": []}, separators=(",", ":")
                ),
                details={"skip_reason": "NO_PUBLISHABLE_EVENTS"},
                stop_pipeline=True,
                completion_code="NO_PUBLISHABLE_EVENTS",
                completion_summary=(
                    "no eligible NewsEvents were available after deterministic processing"
                ),
            )
        budget = _task_budget(context, self.budget_factory)
        result = await self.editorial_service.score_events(
            event_ids,
            task_run_id=context.task_run_id,
            task_step_id=task_step_id,
            budget=budget,
        )
        context.values["selected_news_event_ids"] = result.selected_event_ids
        return StepResult(
            input_count=len(event_ids),
            output_count=len(result.selected_event_ids),
            checkpoint_json=json.dumps(
                {
                    "artifact_id": result.artifact_id,
                    "scored_event_ids": list(result.scored_event_ids),
                    "selected_event_ids": list(result.selected_event_ids),
                },
                separators=(",", ":"),
            ),
            details={
                "artifact_id": result.artifact_id,
                "cache_hit": result.cache_hit,
                "llm_input_tokens": result.usage.input_tokens,
                "llm_output_tokens": result.usage.output_tokens,
            },
            llm_call_count=result.provider_call_count,
            llm_input_tokens=result.usage.input_tokens if not result.cache_hit else 0,
            llm_output_tokens=result.usage.output_tokens if not result.cache_hit else 0,
            cache_hit_count=int(result.cache_hit),
        )


def _event_ids(value: object) -> tuple[int, ...]:
    """Reject malformed clustering context values instead of scoring an unbounded query."""
    if isinstance(value, tuple) and all(isinstance(event_id, int) for event_id in value):
        return value
    return ()


def _task_budget(
    context: PipelineContext, budget_factory: Callable[[], BudgetController]
) -> BudgetController:
    """Create one lazily shared budget controller for all future LLM checkpoints in this TaskRun."""
    existing = context.values.get("llm_budget")
    if isinstance(existing, BudgetController):
        return existing
    budget = budget_factory()
    context.values["llm_budget"] = budget
    return budget
