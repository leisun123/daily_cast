"""Pipeline checkpoint that publishes only an already approved Episode when explicitly enabled."""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Protocol

from dailycast.db.models import EpisodeStatus, Publication, PublicationTargetStatus
from dailycast.db.repositories import PublicationRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.contracts import JSONValue, StepResult
from dailycast.publishing.contracts import DistributionResult
from dailycast.publishing.service import PublicationPreconditionError


class PublicationDispatcherLike(Protocol):
    """Accept the Sprint 10 async dispatcher and the legacy synchronous RSS service."""

    def publish(
        self, episode_id: int
    ) -> DistributionResult | Publication | Awaitable[DistributionResult]:
        """Publish enabled targets for one Episode."""


@dataclass(frozen=True, slots=True)
class PublishStep:
    """Publish only after explicit human approval or the configured auto-publish handoff."""

    episode_service: EpisodeService
    publication_dispatcher: PublicationDispatcherLike
    auto_publish: bool
    name: str = "publish"

    async def run(self, context: PipelineContext) -> StepResult:
        """Publish an approved Episode or explicitly approve a valid draft in auto mode."""
        _active_task_step_id(context)
        episode_id = context.values.get("episode_id")
        if not isinstance(episode_id, int):
            raise RuntimeError("publish requires an Episode produced by create_episode")
        if not self.auto_publish:
            return _skipped_result(episode_id, "AUTO_PUBLISH_DISABLED")
        episode = self.episode_service.get_episode(episode_id)
        if episode is None:
            raise RuntimeError(f"Episode {episode_id} does not exist")
        auto_approved = False
        if episode.status is EpisodeStatus.REVIEW_REQUIRED:
            self.episode_service.approve(episode_id)
            auto_approved = True
        elif episode.status is not EpisodeStatus.APPROVED:
            return _skipped_result(episode_id, "EPISODE_NOT_REVIEWABLE")
        try:
            published = self.publication_dispatcher.publish(episode_id)
            outcome: DistributionResult | Publication
            if isawaitable(published):
                outcome = await published
            else:
                outcome = published
        except PublicationPreconditionError:
            return _skipped_result(episode_id, "EPISODE_NOT_APPROVED")
        if isinstance(outcome, DistributionResult):
            return _distribution_result(
                context,
                episode_id=episode_id,
                distribution=outcome,
                auto_approved=auto_approved,
            )
        publication = outcome
        response_summary = json.loads(publication.response_summary_json or "{}")
        details: dict[str, JSONValue] = {
            "asset_path": publication.public_asset_path,
            "asset_reused": response_summary.get("asset_reused", False),
            "auto_approved": auto_approved,
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


def _distribution_result(
    context: PipelineContext,
    *,
    episode_id: int,
    distribution: DistributionResult,
    auto_approved: bool,
) -> StepResult:
    """Project isolated target outcomes while retaining existing RSS artifact details."""
    platform_statuses: dict[str, JSONValue] = {
        target.platform.value: target.status.value
        for target in sorted(distribution.targets, key=lambda item: item.platform.value)
    }
    platform_errors: dict[str, JSONValue] = {
        target.platform.value: target.last_error
        for target in sorted(distribution.targets, key=lambda item: item.platform.value)
        if target.last_error
    }
    published_count = sum(
        target.status is PublicationTargetStatus.PUBLISHED for target in distribution.targets
    )
    warning_count = len(distribution.targets) - published_count
    with UnitOfWork(context.session_factory) as unit:
        assert unit.session is not None
        rss_publication = PublicationRepository(unit.session).get_published_for_episode(episode_id)
    response_summary = (
        json.loads(rss_publication.response_summary_json or "{}")
        if rss_publication is not None
        else {}
    )
    details: dict[str, JSONValue] = {
        "asset_path": (rss_publication.public_asset_path if rss_publication is not None else None),
        "asset_reused": response_summary.get("asset_reused", False),
        "auto_approved": auto_approved,
        "episode_id": episode_id,
        "feed_version": response_summary.get("feed_version"),
        "feed_guid": rss_publication.feed_guid if rss_publication is not None else None,
        "publication_id": rss_publication.id if rss_publication is not None else None,
        "platform_errors": platform_errors,
        "platform_statuses": platform_statuses,
    }
    return StepResult(
        input_count=1,
        output_count=published_count,
        warning_count=warning_count,
        checkpoint_json=json.dumps(details, separators=(",", ":"), sort_keys=True),
        details=details,
        artifact_path=(rss_publication.public_asset_path if rss_publication is not None else None),
    )
