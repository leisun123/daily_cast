"""Pipeline checkpoint that publishes only an already approved Episode when explicitly enabled."""

from __future__ import annotations

import json
from dataclasses import dataclass

from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import JSONValue, StepResult
from dailycast.publishing.service import PublicationPreconditionError, PublicationService


@dataclass(frozen=True, slots=True)
class PublishStep:
    """Keep generation review-gated while supporting an explicitly enabled publish checkpoint."""

    publication_service: PublicationService
    auto_publish: bool
    name: str = "publish"

    async def run(self, context: PipelineContext) -> StepResult:
        """Publish an approved Episode or record a normal review-gate no-op."""
        _active_task_step_id(context)
        episode_id = context.values.get("episode_id")
        if not isinstance(episode_id, int):
            raise RuntimeError("publish requires an Episode produced by create_episode")
        if not self.auto_publish:
            return _skipped_result(episode_id, "AUTO_PUBLISH_DISABLED")
        try:
            publication = self.publication_service.publish(episode_id)
        except PublicationPreconditionError:
            return _skipped_result(episode_id, "EPISODE_NOT_APPROVED")
        response_summary = json.loads(publication.response_summary_json or "{}")
        details: dict[str, JSONValue] = {
            "asset_path": publication.public_asset_path,
            "asset_reused": response_summary.get("asset_reused", False),
            "episode_id": episode_id,
            "feed_version": response_summary.get("feed_version"),
            "feed_guid": publication.feed_guid,
            "publication_id": publication.id,
            "publication_status": publication.status.value,
        }
        return StepResult(
            input_count=1,
            output_count=1,
            checkpoint_json=json.dumps(details, separators=(",", ":"), sort_keys=True),
            details=details,
            artifact_path=publication.public_asset_path,
        )


def _skipped_result(episode_id: int, reason: str) -> StepResult:
    """Record a non-error review-gate or explicit auto-publish configuration no-op."""
    details: dict[str, JSONValue] = {
        "episode_id": episode_id,
        "publication_created": False,
        "skip_reason": reason,
    }
    return StepResult(
        input_count=1,
        output_count=0,
        checkpoint_json=json.dumps(details, separators=(",", ":"), sort_keys=True),
        details=details,
    )


def _active_task_step_id(context: PipelineContext) -> int:
    """Require the orchestrator-persisted TaskStep before publishing side effects."""
    task_step_id = context.values.get("active_task_step_id")
    if not isinstance(task_step_id, int):
        raise RuntimeError("publish requires an active persisted TaskStep ID")
    return task_step_id
