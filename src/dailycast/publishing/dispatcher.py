"""Independent multi-platform distribution orchestration around the atomic RSS publisher."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import DailyCastError
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
    PublicAsset,
    Publisher,
)
from dailycast.publishing.service import PublicationService


class PlatformNeedsAttentionError(DailyCastError):
    """A platform requires a human action such as login or CAPTCHA completion."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=409, retryable=False)


@dataclass(frozen=True, slots=True)
class RSSDistributionPublisher:
    """Adapt the existing atomic RSS service to the platform-neutral publisher contract."""

    publication_service: PublicationService
    platform_name: PublicationPlatform = PublicationPlatform.RSS

    async def validate(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> None:
        """The RSS service owns its stricter approval and immutable-asset checks."""
        del episode, target, asset

    async def publish(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Publish Feed and immutable asset through the existing crash-safe lifecycle service."""
        del target, asset
        publication = self.publication_service.publish(episode.id)
        public_asset = self.publication_service.public_asset_for_episode(episode.id)
        return PlatformPublishResult(
            status=PublicationTargetStatus.PUBLISHED,
            remote_id=publication.feed_guid,
            remote_url=publication.public_audio_url,
            asset=public_asset,
            rss_publication=publication,
        )

    async def check_status(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Reconcile RSS filesystem state before returning the target's durable status."""
        del target, asset
        self.publication_service.reconcile()
        publication = self.publication_service.rss_publication_for_episode(episode.id)
        if publication is None:
            return PlatformPublishResult(status=PublicationTargetStatus.PENDING)
        if publication.status.value == PublicationTargetStatus.PUBLISHED.value:
            return PlatformPublishResult(
                status=PublicationTargetStatus.PUBLISHED,
                remote_id=publication.feed_guid,
                remote_url=publication.public_audio_url,
                asset=self.publication_service.public_asset_for_episode(episode.id),
                rss_publication=publication,
            )
        return PlatformPublishResult(status=PublicationTargetStatus(publication.status.value))

    async def resume(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """RSS recovery is equivalent to a safe idempotent retry."""
        return await self.publish(episode, target, asset)


class PublicationDispatcher:
    """Publish one Episode to enabled targets while persisting each outcome independently."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publishers: tuple[Publisher, ...],
    ) -> None:
        by_platform = {publisher.platform_name: publisher for publisher in publishers}
        if len(by_platform) != len(publishers):
            raise ValueError("at most one publisher may be configured for each platform")
        self._session_factory = session_factory
        self._publishers = tuple(
            sorted(
                by_platform.values(),
                key=lambda publisher: publisher.platform_name.value != "rss",
            )
        )

    async def publish(self, episode_id: int) -> DistributionResult:
        """Attempt enabled targets; a non-RSS failure becomes a warning, not an Episode failure.

        RSS owns the immutable source-of-truth media: after its FAILED target row is
        persisted, the original exception is re-raised so the pipeline step — and with
        it the Episode lifecycle and retry semantics — still observes the failure.
        """
        episode = self._get_episode(episode_id)
        statuses: dict[str, str] = {}
        warning_count = 0
        immutable_asset: PublicAsset | None = None
        rss_publication = None
        for publisher in self._publishers:
            target = self._ensure_target(episode.id, publisher.platform_name)
            if target.status is PublicationTargetStatus.PUBLISHED:
                result = PlatformPublishResult(
                    status=PublicationTargetStatus.PUBLISHED,
                    remote_id=target.remote_id,
                    remote_url=target.remote_url,
                )
                if publisher.platform_name is PublicationPlatform.RSS:
                    immutable_asset = self._immutable_rss_asset(episode.id)
                    if isinstance(publisher, RSSDistributionPublisher):
                        rss_publication = publisher.publication_service.rss_publication_for_episode(
                            episode.id
                        )
                    result = PlatformPublishResult(
                        status=PublicationTargetStatus.PUBLISHED,
                        remote_id=target.remote_id,
                        remote_url=target.remote_url,
                        asset=immutable_asset,
                        rss_publication=rss_publication,
                    )
            else:
                target = self._begin_attempt(target.id)
                rss_failure: Exception | None = None
                try:
                    await publisher.validate(episode, target, immutable_asset)
                    result = await publisher.publish(episode, target, immutable_asset)
                except PlatformNeedsAttentionError as error:
                    result = PlatformPublishResult(
                        status=PublicationTargetStatus.NEEDS_ATTENTION,
                        last_error=_error_text(error),
                    )
                except Exception as error:
                    result = PlatformPublishResult(
                        status=PublicationTargetStatus.FAILED,
                        last_error=_error_text(error),
                    )
                    if publisher.platform_name is PublicationPlatform.RSS:
                        rss_failure = error
                target = self._record_result(target.id, result)
                if rss_failure is not None:
                    # External targets were skipped deliberately: they upload the
                    # immutable RSS asset that just failed to be produced.
                    raise rss_failure
            statuses[target.platform.value] = target.status.value
            if target.status is not PublicationTargetStatus.PUBLISHED:
                warning_count += 1
            if publisher.platform_name is PublicationPlatform.RSS:
                immutable_asset = result.asset
                rss_publication = result.rss_publication
        return DistributionResult(
            rss_publication=rss_publication,
            target_statuses=statuses,
            warning_count=warning_count,
        )

    async def resume(self, episode_id: int, platform: PublicationPlatform) -> DistributionResult:
        """Resume only the requested target after a human resolved its attention requirement.

        Like ``publish``, an RSS failure is persisted as a FAILED target row and then
        re-raised so callers cannot mistake a broken source-of-truth target for a warning.
        """
        episode = self._get_episode(episode_id)
        publisher = next(
            (candidate for candidate in self._publishers if candidate.platform_name is platform),
            None,
        )
        if publisher is None:
            raise LookupError(f"publisher {platform.value} is not enabled")
        target = self._ensure_target(episode.id, platform)
        if target.status is not PublicationTargetStatus.NEEDS_ATTENTION:
            return DistributionResult(
                rss_publication=None,
                target_statuses={platform.value: target.status.value},
                warning_count=int(target.status is not PublicationTargetStatus.PUBLISHED),
            )
        asset = self._immutable_rss_asset(episode.id)
        target = self._begin_attempt(target.id)
        rss_failure: Exception | None = None
        try:
            await publisher.validate(episode, target, asset)
            result = await publisher.resume(episode, target, asset)
        except PlatformNeedsAttentionError as error:
            result = PlatformPublishResult(
                status=PublicationTargetStatus.NEEDS_ATTENTION,
                last_error=_error_text(error),
            )
        except Exception as error:
            result = PlatformPublishResult(
                status=PublicationTargetStatus.FAILED,
                last_error=_error_text(error),
            )
            if platform is PublicationPlatform.RSS:
                rss_failure = error
        target = self._record_result(target.id, result)
        if rss_failure is not None:
            raise rss_failure
        return DistributionResult(
            rss_publication=result.rss_publication,
            target_statuses={platform.value: target.status.value},
            warning_count=int(target.status is not PublicationTargetStatus.PUBLISHED),
        )

    async def reconcile(self) -> int:
        """Recheck in-progress targets without replaying completed generation or audio work."""
        recovered = 0
        for target in self._targets_to_reconcile():
            publisher = next(
                (
                    candidate
                    for candidate in self._publishers
                    if candidate.platform_name is target.platform
                ),
                None,
            )
            if publisher is None:
                continue
            episode = self._get_episode(target.episode_id)
            asset = self._immutable_rss_asset(episode.id)
            try:
                result = await publisher.check_status(episode, target, asset)
            except PlatformNeedsAttentionError as error:
                result = PlatformPublishResult(
                    status=PublicationTargetStatus.NEEDS_ATTENTION,
                    last_error=_error_text(error),
                )
            except Exception as error:
                result = PlatformPublishResult(
                    status=PublicationTargetStatus.FAILED,
                    last_error=_error_text(error),
                )
            previous_status = target.status
            persisted = self._record_result(target.id, result)
            if persisted.status is not previous_status:
                recovered += 1
        return recovered

    def _get_episode(self, episode_id: int) -> Episode:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).get(episode_id)
            if episode is None:
                raise LookupError(f"Episode {episode_id} does not exist")
            return episode

    def _ensure_target(self, episode_id: int, platform: PublicationPlatform) -> PublicationTarget:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            targets = PublicationTargetRepository(unit.session)
            existing = targets.get_by_platform(episode_id, platform)
            if existing is not None:
                return existing
            return targets.create(
                episode_id=episode_id,
                platform=platform,
                status=PublicationTargetStatus.PENDING,
            )

    def _begin_attempt(self, target_id: int) -> PublicationTarget:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            target = PublicationTargetRepository(unit.session).get(target_id)
            if target is None:
                raise LookupError(f"PublicationTarget {target_id} does not exist")
            return PublicationTargetRepository(unit.session).update(
                target,
                status=PublicationTargetStatus.PUBLISHING,
                attempt_count=target.attempt_count + 1,
                last_error=None,
            )

    def _record_result(self, target_id: int, result: PlatformPublishResult) -> PublicationTarget:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            target = PublicationTargetRepository(unit.session).get(target_id)
            if target is None:
                raise LookupError(f"PublicationTarget {target_id} does not exist")
            return PublicationTargetRepository(unit.session).update(
                target,
                status=result.status,
                remote_id=result.remote_id,
                remote_url=result.remote_url,
                last_error=result.last_error,
            )

    def _targets_to_reconcile(self) -> tuple[PublicationTarget, ...]:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            return tuple(
                PublicationTargetRepository(unit.session).list_by_status(
                    PublicationTargetStatus.PUBLISHING
                )
            )

    def _immutable_rss_asset(self, episode_id: int) -> PublicAsset | None:
        for publisher in self._publishers:
            if isinstance(publisher, RSSDistributionPublisher):
                return publisher.publication_service.public_asset_for_episode(episode_id)
        return None


def _error_text(error: Exception) -> str:
    """Store a stable error identity without browser HTML, credentials, or traces."""
    if isinstance(error, DailyCastError):
        return f"{error.code}: {error.message}"[:1000]
    return f"{type(error).__name__}: {error}"[:1000]
