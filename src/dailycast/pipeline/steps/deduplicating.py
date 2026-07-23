"""Pipeline checkpoint that marks exact and near duplicate Articles."""

from __future__ import annotations

import json
from dataclasses import dataclass

from dailycast.news.service import NewsProcessor
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult


@dataclass(frozen=True, slots=True)
class DeduplicatingStep:
    """Persist duplicate mappings and pass only primary Articles to event clustering."""

    processor: NewsProcessor
    name: str = "deduplicating"

    async def run(self, context: PipelineContext) -> StepResult:
        """Run deterministic deduplication for the eligible checkpoint output only."""
        article_ids = _article_ids(context.values.get("eligible_article_ids"))
        decision = self.processor.deduplicate(article_ids)
        context.values["deduplicated_article_ids"] = decision.primary_article_ids
        return StepResult(
            input_count=len(article_ids),
            output_count=len(decision.primary_article_ids),
            warning_count=len(decision.duplicate_of_article_ids),
            checkpoint_json=json.dumps(
                {
                    "primary_article_ids": list(decision.primary_article_ids),
                    "duplicate_article_ids": sorted(decision.duplicate_of_article_ids),
                },
                separators=(",", ":"),
            ),
            details={"duplicate_article_count": len(decision.duplicate_of_article_ids)},
        )


def _article_ids(value: object) -> tuple[int, ...]:
    """Reject malformed in-memory context values instead of issuing an unbounded query."""
    if isinstance(value, tuple) and all(isinstance(article_id, int) for article_id in value):
        return value
    return ()
