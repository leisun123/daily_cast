"""Independent multi-platform delivery orchestration for an already generated Episode."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sqlalchemy.orm import Session, sessionmaker

from dailycast.db.models import (
    Episode,
    PublicationPlatform,
    PublicationTarget,
    PublicationTargetStatus,
)
from dailycast.db.repositories import EpisodeRepository, PublicationTargetRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.publishing.contracts import (
    DistributionResult,
    PlatformPublishResult,
    Publisher,
    PublisherNeedsAttentionError,
)
from dailycast.publishing.service import PublicationService


class RSSDistributionPublisher:
    """Expose the existing atomic RSS service through the platform Publisher contract."""

    platform_name = PublicationPlatform.RSS

    def __init__(self, service: PublicationService) -> None:
        self._service = service

    async def validate(self, episode: Episode) -> None:
        """Leave approval, asset, and Feed validation with the existing RSS service."""
        if episode.id <= 0:
            raise ValueError("RSS publication requires a persisted Episode")

    async def publish(self, episode: Episode) -> PlatformPublishResult:
        """Run the synchronous filesystem publisher without blocking the event loop."""
        publication = await asyncio.to_thread(self._service.publish, episode.id)
        return PlatformPublishResult(
            remote_id=publication.feed_guid,
            remote_url=publication.public_audio_url,
        )

    async def check_status(
        self, episode: Episode, target: PublicationTarget
    ) -> PlatformPublishResult:
        """Use the RSS service's idempotent reconcile/publish path as status proof."""
        del target
        return await self.publish(episode)

    async def resume(self, episode: Episode, target: PublicationTarget) -> PlatformPublishResult:
        """Resume only RSS using its existing crash-recovery semantics."""
        del target
        return await self.publish(episode)


class PublicationDispatcher:
    """Run enabled publishers independently and persist every platform outcome."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publishers: Sequence[Publisher],
    ) -> None:
        by_platform = {publisher.platform_name: publisher for publisher in publishers}
        if len(by_platform) != len(publishers):
            raise ValueError("publisher platform names must be unique")
        self._session_factory = session_factory
        self._publishers = by_platform

    async def publish(self, episode_id: int) -> DistributionResult:
        """Attempt every enabled platform without allowing one failure to stop the next."""
        targets: list[PublicationTarget] = []
        for publisher in self._publishers.values():
            target = self._get_or_create_target(episode_id, publisher.platform_name)
            if target.status is PublicationTargetStatus.PUBLISHED:
                targets.append(target)
                continue
            targets.append(
                await self._execute(
                    episode_id,
                    publisher,
                    target,
                    resume=False,
                )
            )
        return DistributionResult(tuple(targets))

    async def resume(self, episode_id: int, platform: PublicationPlatform) -> PublicationTarget:
        """Resume only one target and leave every other platform row untouched."""
        publisher = self._publishers.get(platform)
        if publisher is None:
            raise LookupError(f"publisher {platform.value} is not enabled")
        target = self._get_or_create_target(episode_id, platform)
        return await self._execute(episode_id, publisher, target, resume=True)

    async def check_status(
        self, episode_id: int, platform: PublicationPlatform
    ) -> PublicationTarget:
        """Reconcile one remote platform without running another publisher."""
        publisher = self._publishers.get(platform)
        if publisher is None:
            raise LookupError(f"publisher {platform.value} is not enabled")
        target = self._get_or_create_target(episode_id, platform)
        episode = self._load_episode(episode_id)
        try:
            result = await publisher.check_status(episode, target)
        except PublisherNeedsAttentionError as error:
            return self._finish(
                target.id,
                PublicationTargetStatus.NEEDS_ATTENTION,
                last_error=_safe_error(error),
            )
        except Exception as error:
            return self._finish(
                target.id,
                PublicationTargetStatus.FAILED,
                last_error=_safe_error(error),
            )
        return self._finish_success(target.id, result)

    async def reconcile(self) -> int:
        """Inspect only interrupted publishing rows and never repeat generation work."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            interrupted = PublicationTargetRepository(unit.session).list_by_status(
                PublicationTargetStatus.PUBLISHING
            )
        recovered = 0
        for target in interrupted:
            if target.platform not in self._publishers:
                continue
            current = await self.check_status(target.episode_id, target.platform)
            if current.status is PublicationTargetStatus.PUBLISHED:
                recovered += 1
        return recovered

    async def _execute(
        self,
        episode_id: int,
        publisher: Publisher,
        target: PublicationTarget,
        *,
        resume: bool,
    ) -> PublicationTarget:
        episode = self._load_episode(episode_id)
        active = self._begin(target.id)
        try:
            await publisher.validate(episode)
            result = (
                await publisher.resume(episode, active)
                if resume
                else await publisher.publish(episode)
            )
        except PublisherNeedsAttentionError as error:
            return self._finish(
                active.id,
                PublicationTargetStatus.NEEDS_ATTENTION,
                last_error=_safe_error(error),
            )
        except Exception as error:
            return self._finish(
                active.id,
                PublicationTargetStatus.FAILED,
                last_error=_safe_error(error),
            )
        return self._finish_success(active.id, result)

    def _get_or_create_target(
        self, episode_id: int, platform: PublicationPlatform
    ) -> PublicationTarget:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            if EpisodeRepository(unit.session).get(episode_id) is None:
                raise LookupError(f"Episode {episode_id} does not exist")
            repository = PublicationTargetRepository(unit.session)
            existing = repository.get_by_episode_and_platform(episode_id, platform)
            if existing is not None:
                return existing
            return repository.create(
                episode_id=episode_id,
                platform=platform,
                status=PublicationTargetStatus.PENDING,
            )

    def _load_episode(self, episode_id: int) -> Episode:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            if episode is None:
                raise LookupError(f"Episode {episode_id} does not exist")
            return episode

    def _begin(self, target_id: int) -> PublicationTarget:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = PublicationTargetRepository(unit.session)
            target = repository.get(target_id)
            if target is None:
                raise LookupError(f"PublicationTarget {target_id} does not exist")
            return repository.update(
                target,
                status=PublicationTargetStatus.PUBLISHING,
                attempt_count=target.attempt_count + 1,
                last_error=None,
            )

    def _finish_success(self, target_id: int, result: PlatformPublishResult) -> PublicationTarget:
        return self._finish(
            target_id,
            PublicationTargetStatus.PUBLISHED,
            remote_id=result.remote_id,
            remote_url=result.remote_url,
            last_error=None,
        )

    def _finish(
        self,
        target_id: int,
        status: PublicationTargetStatus,
        **changes: object,
    ) -> PublicationTarget:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            repository = PublicationTargetRepository(unit.session)
            target = repository.get(target_id)
            if target is None:
                raise LookupError(f"PublicationTarget {target_id} does not exist")
            return repository.update(target, status=status, **changes)


def _safe_error(error: Exception) -> str:
    """Persist a bounded operator code/message without traces or browser page contents."""
    message = str(error).strip()
    return (message or error.__class__.__name__)[:1000]
