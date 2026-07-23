"""Pipeline checkpoint that extracts text independently for collected Articles."""

from __future__ import annotations

import json
from dataclasses import dataclass

from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import StepResult
from dailycast.sources.extraction import ContentExtractor, FetchPolicy
from dailycast.sources.service import ArticleService


@dataclass(frozen=True, slots=True)
class ExtractingStep:
    """Attempt every missing-body Article and record partial failures as warnings."""

    article_service: ArticleService
    extractor: ContentExtractor
    name: str = "extracting"

    async def run(self, context: PipelineContext) -> StepResult:
        """Extract each candidate separately so one bad page does not terminate the task."""
        article_ids = context.values.get("collected_article_ids", ())
        if not isinstance(article_ids, tuple) or not all(
            isinstance(value, int) for value in article_ids
        ):
            article_ids = ()
        targets = self.article_service.extraction_targets(article_ids)
        succeeded_ids: list[int] = []
        failed_ids: list[int] = []
        for target in targets:
            extracted = await self.extractor.extract(
                target.url,
                FetchPolicy(timeout_seconds=target.timeout_seconds),
            )
            if extracted.error is None:
                self.article_service.apply_extraction(target.article_id, extracted)
                succeeded_ids.append(target.article_id)
            else:
                self.article_service.record_extraction_failure(target.article_id, extracted)
                failed_ids.append(target.article_id)
        return StepResult(
            input_count=len(targets),
            output_count=len(succeeded_ids),
            warning_count=len(failed_ids),
            checkpoint_json=json.dumps(
                {"succeeded_article_ids": succeeded_ids, "failed_article_ids": failed_ids},
                separators=(",", ":"),
            ),
            details={"failed_article_count": len(failed_ids)},
        )
