"""Pipeline checkpoint that applies deterministic eligibility rules to collected Articles."""

from __future__ import annotations

import json
from dataclasses import dataclass

from dailycast.news.service import NewsProcessor
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult


@dataclass(frozen=True, slots=True)
class FilteringStep:
    """Persist filtering decisions and pass only eligible Article IDs to deduplication."""

    processor: NewsProcessor
    name: str = "filtering"

    async def run(self, context: PipelineContext) -> StepResult:
        """Filter this TaskRun's collected IDs and retain durable Article reasons."""
        article_ids = _article_ids(context.values.get("collected_article_ids"))
        decision = self.processor.filter(article_ids)
        context.values["eligible_article_ids"] = decision.eligible_article_ids
        return StepResult(
            input_count=len(article_ids),
            output_count=len(decision.eligible_article_ids),
            warning_count=len(decision.filtered_reasons),
            checkpoint_json=json.dumps(
                {
                    "eligible_article_ids": list(decision.eligible_article_ids),
                    "filtered_article_ids": sorted(decision.filtered_reasons),
                },
                separators=(",", ":"),
            ),
            details={"filtered_article_count": len(decision.filtered_reasons)},
        )


def _article_ids(value: object) -> tuple[int, ...]:
    """Reject malformed in-memory context values instead of issuing an unbounded query."""
    if isinstance(value, tuple) and all(isinstance(article_id, int) for article_id in value):
        return value
    return ()
