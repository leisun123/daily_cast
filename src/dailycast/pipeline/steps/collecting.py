"""Pipeline checkpoint that discovers and persists candidates from enabled Sources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from dailycast.core.time import Clock
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult
from dailycast.sources.contracts import CollectionWindow
from dailycast.sources.service import SourceCollectionService


@dataclass(frozen=True, slots=True)
class CollectingStep:
    """Collect all enabled sources while retaining only Article IDs in the task context."""

    service: SourceCollectionService
    collection_window_hours: int
    clock: Clock
    name: str = "collecting"

    async def run(self, context: PipelineContext) -> StepResult:
        """Persist candidates source-by-source and pass their durable IDs to extraction."""
        end = self.clock.now()
        collected = await self.service.collect_enabled_sources(
            CollectionWindow(start=end - timedelta(hours=self.collection_window_hours), end=end)
        )
        context.values["collected_article_ids"] = collected.article_ids
        return StepResult(
            input_count=collected.source_count,
            output_count=len(collected.article_ids),
            warning_count=collected.warning_count,
            checkpoint_json=json.dumps(
                {"article_ids": list(collected.article_ids)}, separators=(",", ":")
            ),
            details={
                "successful_source_count": collected.successful_source_count,
                "warning_count": collected.warning_count,
            },
        )
