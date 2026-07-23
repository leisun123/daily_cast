"""Pipeline checkpoint that converts eligible primary Articles into NewsEvents."""

from __future__ import annotations

import json
from dataclasses import dataclass

from dailycast.news.service import NewsProcessor
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult


@dataclass(frozen=True, slots=True)
class ClusteringStep:
    """Persist TF-IDF character-n-gram event clusters for deduplicated Article IDs."""

    processor: NewsProcessor
    name: str = "clustering"

    async def run(self, context: PipelineContext) -> StepResult:
        """Create or update deterministic NewsEvents without invoking an LLM."""
        article_ids = _article_ids(context.values.get("deduplicated_article_ids"))
        result = self.processor.cluster(article_ids)
        context.values["news_event_ids"] = result.event_ids
        return StepResult(
            input_count=len(article_ids),
            output_count=len(result.event_ids),
            checkpoint_json=json.dumps(
                {
                    "news_event_ids": list(result.event_ids),
                    "clustered_article_ids": list(result.clustered_article_ids),
                },
                separators=(",", ":"),
            ),
            details={"clustered_article_count": len(result.clustered_article_ids)},
        )


def _article_ids(value: object) -> tuple[int, ...]:
    """Reject malformed in-memory context values instead of issuing an unbounded query."""
    if isinstance(value, tuple) and all(isinstance(article_id, int) for article_id in value):
        return value
    return ()
