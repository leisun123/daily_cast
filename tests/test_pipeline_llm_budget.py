"""All editorial checkpoints share one TaskRun LLM budget controller."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from editorial_test_support import (
    FakeLLMProvider,
    build_outline,
    create_selected_event,
    create_task_provenance,
    upgraded_session_factory,
    valid_script_payload,
)
from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import AIBudgetExceededError
from dailycast.core.time import Clock
from dailycast.db.models import LLMOperation
from dailycast.llm.budget import BudgetController
from dailycast.llm.editorial_service import AIEditorialService
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.steps.checking import CheckingStep
from dailycast.pipeline.steps.outlining import OutliningStep
from dailycast.pipeline.steps.ranking import RankingStep
from dailycast.pipeline.steps.scripting import ScriptingStep


def test_pipeline_reuses_one_budget_across_ranking_through_metadata(
    app_config_path: Path, tmp_path: Path
) -> None:
    """The fifth cache-miss call fails when all four preceding editorial calls share one budget."""
    factory: sessionmaker[Session] = upgraded_session_factory(app_config_path)
    try:
        fixture = create_selected_event(factory, key="shared-budget", content="可信新闻证据。")
        outline = build_outline(fixture.event_id)
        provider = FakeLLMProvider(
            {
                LLMOperation.SCORE_EVENTS: [
                    {
                        "scores": [
                            {
                                "event_id": fixture.event_id,
                                "importance": 90,
                                "relevance": 80,
                                "confidence": 70,
                                "recommend": True,
                                "reason": "重要且相关。",
                                "risks": [],
                            }
                        ]
                    }
                ],
                LLMOperation.GENERATE_OUTLINE: [outline.model_dump(mode="json")],
                LLMOperation.GENERATE_SCRIPT: [valid_script_payload(outline, fixture)],
                LLMOperation.REVIEW_SCRIPT: [
                    {
                        "schema_version": "1",
                        "verdict": "pass",
                        "issues": [],
                        "suggested_changes": [],
                    }
                ],
            }
        )
        task_run_id, task_step_id = create_task_provenance(
            factory, step_name="ranking", step_order=6
        )
        budget = BudgetController(max_calls=4, max_input_tokens=20_000, max_output_tokens=2_000)
        context = PipelineContext(
            task_run_id=task_run_id,
            session_factory=factory,
            shutdown_requested=asyncio.Event(),
            clock=Clock(),
            values={"active_task_step_id": task_step_id, "news_event_ids": (fixture.event_id,)},
        )
        service = AIEditorialService(
            factory,
            provider,
            target_duration_seconds=120,
            duration_tolerance_seconds=0,
        )

        asyncio.run(RankingStep(service, lambda: budget).run(context))
        asyncio.run(OutliningStep(service, tmp_path, lambda: budget).run(context))
        asyncio.run(ScriptingStep(service, tmp_path, lambda: budget).run(context))
        with pytest.raises(AIBudgetExceededError):
            asyncio.run(CheckingStep(service, tmp_path, lambda: budget).run(context))

        assert budget.call_count == 4
        assert provider.calls_by_operation[LLMOperation.SCORE_EVENTS] == 1
        assert provider.calls_by_operation[LLMOperation.GENERATE_OUTLINE] == 1
        assert provider.calls_by_operation[LLMOperation.GENERATE_SCRIPT] == 1
        assert provider.calls_by_operation[LLMOperation.REVIEW_SCRIPT] == 1
        assert LLMOperation.GENERATE_METADATA not in provider.calls_by_operation
    finally:
        factory.kw["bind"].dispose()
