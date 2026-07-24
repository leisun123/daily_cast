"""Publication lifecycle service that makes RSS assets and Feed writes crash-recoverable."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.errors import DailyCastError
from dailycast.core.hashes import sha256_text
from dailycast.db.models import (
    Episode,
    EpisodeStatus,
    Publication,
    PublicationStatus,
    PublisherType,
    utc_now,
)
from dailycast.db.repositories import EpisodeRepository, PublicationRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.publishing.contracts import PublicAsset, RSSFeedItem, RSSPublicationTarget
from dailycast.publishing.rss import RSSPublicationError


class PublicationPreconditionError(DailyCastError):
    """Raised when an Episode has not passed the current human-approval publication gate."""

    def __init__(self, message: str) -> None:
        super().__init__(code="PUBLICATION_PRECONDITION_FAILED", message=message, status_code=409)


class PublicationOperationError(DailyCastError):
    """Raised when local immutable asset or RSS Feed side effects cannot be safely completed."""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="PUBLICATION_FAILED", message=message, status_code=502, retryable=True
        )


@dataclass(frozen=True, slots=True)
class _PublicationSnapshot:
    """Detached durable values for filesystem and Feed work outside SQLite transactions."""

    publication: Publication
    episode: Episode


class PublicationService:
    """Own one target-idempotent RSS Publication from approval gate through Feed reconciliation."""

    def __init__(
        self, session_factory: sessionmaker[Session], publisher: RSSPublicationTarget
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher

    def create_publication(self, episode_id: int) -> Publication:
        """Create or reuse the pending local-rss Publication after validating approval."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            episodes = EpisodeRepository(unit.session)
            episode = episodes.get(episode_id)
            if episode is None:
                raise LookupError(f"Episode {episode_id} does not exist")
            publications = PublicationRepository(unit.session)
            existing = publications.get_by_target(
                episode.id, PublisherType.RSS.value, self._publisher.target_key
            )
            if existing is not None:
                return existing
            self._require_approved_episode(episode)
            fingerprint = _request_fingerprint(episode)
            return publications.create(
                episode_id=episode.id,
                publisher_type=PublisherType.RSS,
                target_key=self._publisher.target_key,
                status=PublicationStatus.PENDING,
                idempotency_key=f"rss:{episode.public_id}",
                request_fingerprint=fingerprint,
                attempt_count=0,
            )

    def publish(self, episode_id: int) -> Publication:
        """Promote immutable media, update the Feed atomically, then commit state last."""
        publication = self.create_publication(episode_id)
        snapshot = self._load_snapshot(publication.id)
        if snapshot.publication.status is PublicationStatus.PUBLISHED:
            return snapshot.publication
        is_reconciled = snapshot.publication.status is PublicationStatus.PUBLISHING and (
            self._publisher.reconcile(snapshot.publication, snapshot.episode)
        )
        if is_reconciled:
            return self._mark_published(snapshot.publication.id)
        snapshot = self._begin_publish(snapshot.publication.id)
        try:
            asset = self._publisher.promote_asset(snapshot.episode)
            self._publisher.validate(snapshot.episode, asset)
            persisted = self._persist_candidate_asset(snapshot.publication.id, asset)
            candidate = self._publisher.feed_item(persisted.episode, persisted.publication, asset)
            feed_result = self._publisher.publish(self._feed_items(candidate))
            return self._mark_published(
                persisted.publication.id,
                feed_version=feed_result.feed_version,
                asset_reused=asset.reused,
            )
        except RSSPublicationError as error:
            self._mark_publish_failed(snapshot.publication.id, str(error))
            raise PublicationOperationError(
                "RSS asset promotion or Feed publication failed"
            ) from error
        except OSError as error:
            self._mark_publish_failed(snapshot.publication.id, str(error))
            raise PublicationOperationError("local RSS publication storage failed") from error

    def reconcile(self) -> int:
        """Recover interrupted publishing rows and rebuild Feed items that are missing."""
        recovered = 0
        for snapshot in self._snapshots_with_status(PublicationStatus.PUBLISHING):
            if self._publisher.reconcile(snapshot.publication, snapshot.episode):
                self._mark_published(snapshot.publication.id)
                recovered += 1
                continue
            asset = self._asset_if_valid(snapshot.publication)
            if asset is None:
                self._return_to_pending(snapshot.publication.id)
                continue
            try:
                candidate = self._publisher.feed_item(snapshot.episode, snapshot.publication, asset)
                feed_result = self._publisher.publish(self._feed_items(candidate))
            except (OSError, RSSPublicationError) as error:
                self._mark_publish_failed(snapshot.publication.id, str(error))
                continue
            self._mark_published(
                snapshot.publication.id,
                feed_version=feed_result.feed_version,
                asset_reused=asset.reused,
            )
            recovered += 1

        published = self._snapshots_with_status(PublicationStatus.PUBLISHED)
        if published and any(
            not self._publisher.reconcile(snapshot.publication, snapshot.episode)
            for snapshot in published
        ):
            try:
                self._publisher.publish(self._feed_items())
                recovered += 1
            except (OSError, RSSPublicationError):
                # Published database state remains authoritative; a later reconciliation retries.
                pass
        return recovered

    def _begin_publish(self, publication_id: int) -> _PublicationSnapshot:
        """Persist publishing before file/Feed effects and increment the retry attempt."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publications = PublicationRepository(unit.session)
            publication = publications.get(publication_id)
            if publication is None:
                raise LookupError(f"Publication {publication_id} does not exist")
            episode = EpisodeRepository(unit.session).get(publication.episode_id)
            if episode is None:
                raise LookupError(f"Episode {publication.episode_id} does not exist")
            self._require_current_approval(episode, allow_publishing=True)
            if publication.status is PublicationStatus.PUBLISHED:
                return _PublicationSnapshot(publication=publication, episode=episode)
            publication = publications.update(
                publication,
                status=PublicationStatus.PUBLISHING,
                attempt_count=publication.attempt_count + 1,
                request_fingerprint=_request_fingerprint(episode),
                error_code=None,
                error_summary=None,
            )
            episode.status = EpisodeStatus.PUBLISHING
            episode.error_code = None
            episode.error_summary = None
            unit.session.flush()
            return _PublicationSnapshot(publication=publication, episode=episode)

    def _persist_candidate_asset(
        self, publication_id: int, asset: PublicAsset
    ) -> _PublicationSnapshot:
        """Commit candidate asset identity before Feed mutation so a crash is fully reconcilable."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publications = PublicationRepository(unit.session)
            publication = publications.get(publication_id)
            if publication is None:
                raise LookupError(f"Publication {publication_id} does not exist")
            episode = EpisodeRepository(unit.session).get(publication.episode_id)
            if episode is None:
                raise LookupError(f"Episode {publication.episode_id} does not exist")
            publication = publications.update(
                publication,
                public_asset_path=asset.relative_path,
                public_audio_url=asset.public_url,
                asset_sha256=asset.sha256,
                asset_byte_size=asset.byte_size,
                feed_guid=episode.public_id,
            )
            return _PublicationSnapshot(publication=publication, episode=episode)

    def _mark_published(
        self,
        publication_id: int,
        *,
        feed_version: str | None = None,
        asset_reused: bool | None = None,
    ) -> Publication:
        """Commit Publication/Episode states only after Feed and enclosure verification."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publications = PublicationRepository(unit.session)
            publication = publications.get(publication_id)
            if publication is None:
                raise LookupError(f"Publication {publication_id} does not exist")
            episode = EpisodeRepository(unit.session).get(publication.episode_id)
            if episode is None:
                raise LookupError(f"Episode {publication.episode_id} does not exist")
            if publication.status is PublicationStatus.PUBLISHED:
                return publication
            if not self._publisher.reconcile(publication, episode):
                raise PublicationOperationError(
                    "RSS Feed does not yet verify the candidate publication"
                )
            now = utc_now()
            summary: dict[str, object] = {
                key: value
                for key, value in {
                    "feed_version": feed_version,
                    "asset_reused": asset_reused,
                }.items()
                if value is not None
            }
            publication = publications.update(
                publication,
                status=PublicationStatus.PUBLISHED,
                published_at=now,
                last_verified_at=now,
                response_summary_json=_canonical_json(summary),
                error_code=None,
                error_summary=None,
            )
            episode.status = EpisodeStatus.PUBLISHED
            episode.published_at = now
            episode.error_code = None
            episode.error_summary = None
            unit.session.flush()
            return publication

    def _mark_publish_failed(self, publication_id: int, summary: str) -> None:
        """Make a failed Feed effect retryable and return the Episode to approved state."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publication = PublicationRepository(unit.session).get(publication_id)
            if publication is None:
                return
            episode = EpisodeRepository(unit.session).get(publication.episode_id)
            PublicationRepository(unit.session).update(
                publication,
                status=PublicationStatus.FAILED,
                error_code="RSS_PUBLICATION_FAILED",
                error_summary=summary[:1000],
            )
            if episode is not None and episode.status is EpisodeStatus.PUBLISHING:
                episode.status = EpisodeStatus.APPROVED
                episode.error_code = "RSS_PUBLICATION_FAILED"
                episode.error_summary = summary[:1000]
                unit.session.flush()

    def _return_to_pending(self, publication_id: int) -> None:
        """Recover a crash before asset promotion without writing or overwriting public media."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publication = PublicationRepository(unit.session).get(publication_id)
            if publication is None:
                return
            episode = EpisodeRepository(unit.session).get(publication.episode_id)
            PublicationRepository(unit.session).update(
                publication,
                status=PublicationStatus.PENDING,
                error_code=None,
                error_summary=None,
            )
            if episode is not None and episode.status is EpisodeStatus.PUBLISHING:
                episode.status = EpisodeStatus.APPROVED
                unit.session.flush()

    def _load_snapshot(self, publication_id: int) -> _PublicationSnapshot:
        """Load a detached Publication/Episode pair for filesystem work outside SQLite."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publication = PublicationRepository(unit.session).get(publication_id)
            if publication is None:
                raise LookupError(f"Publication {publication_id} does not exist")
            episode = EpisodeRepository(unit.session).get(publication.episode_id)
            if episode is None:
                raise LookupError(f"Episode {publication.episode_id} does not exist")
            return _PublicationSnapshot(publication=publication, episode=episode)

    def _snapshots_with_status(self, status: PublicationStatus) -> tuple[_PublicationSnapshot, ...]:
        """Read durable reconciliation candidates before performing side effects."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publications = PublicationRepository(unit.session).list_by_status(status)
            episodes = EpisodeRepository(unit.session)
            snapshots: list[_PublicationSnapshot] = []
            for publication in publications:
                episode = episodes.get(publication.episode_id)
                if episode is not None:
                    snapshots.append(_PublicationSnapshot(publication=publication, episode=episode))
            return tuple(snapshots)

    def _feed_items(self, candidate: RSSFeedItem | None = None) -> tuple[RSSFeedItem, ...]:
        """Build Feed state from published rows and inject one publishing candidate."""
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publications = PublicationRepository(unit.session).list_published(
                target_key=self._publisher.target_key
            )
            episodes = EpisodeRepository(unit.session)
            items: list[RSSFeedItem] = []
            for publication in publications:
                episode = episodes.get(publication.episode_id)
                asset = self._asset_if_valid(publication)
                if episode is not None and asset is not None:
                    items.append(self._publisher.feed_item(episode, publication, asset))
        if candidate is not None:
            items = [item for item in items if item.guid != candidate.guid]
            items.append(candidate)
        return tuple(items)

    def _asset_if_valid(self, publication: Publication) -> PublicAsset | None:
        """Return an asset only when the RSS adapter confirms durable immutable fields."""
        if (
            publication.public_asset_path is None
            or publication.public_audio_url is None
            or publication.asset_sha256 is None
            or publication.asset_byte_size is None
        ):
            return None
        try:
            return self._publisher.asset_from_publication(publication)
        except OSError:
            return None

    @staticmethod
    def _require_approved_episode(episode: Episode) -> None:
        """Enforce approval plus current script/audio bindings before any new public side effect."""
        PublicationService._require_current_approval(episode, allow_publishing=False)

    @staticmethod
    def _require_current_approval(episode: Episode, *, allow_publishing: bool) -> None:
        """Accept a publishing retry only when original approval bindings still hold."""
        allowed_statuses = {EpisodeStatus.APPROVED}
        if allow_publishing:
            allowed_statuses.add(EpisodeStatus.PUBLISHING)
        if episode.status not in allowed_statuses:
            raise PublicationPreconditionError("Episode must be approved before publishing")
        if episode.approved_script_revision != episode.script_revision:
            raise PublicationPreconditionError("Episode approved script revision is not current")
        if episode.approved_audio_version != episode.audio_version:
            raise PublicationPreconditionError("Episode approved audio version is not current")
        if (
            episode.title is None
            or episode.description is None
            or episode.review_json is None
            or episode.script_text is None
            or episode.draft_audio_path is None
            or episode.draft_audio_sha256 is None
        ):
            raise PublicationPreconditionError(
                "Episode lacks validated metadata, script, or draft audio"
            )


def _request_fingerprint(episode: Episode) -> str:
    """Fingerprint publication inputs so a retry cannot silently publish a changed Episode."""
    return sha256_text(
        _canonical_json(
            {
                "audio_version": episode.audio_version,
                "draft_audio_sha256": episode.draft_audio_sha256,
                "episode_public_id": episode.public_id,
                "script_revision": episode.script_revision,
                "title": episode.title,
            }
        )
    )


def _canonical_json(value: dict[str, object]) -> str:
    """Store a compact non-secret response/request fingerprint projection in SQLite JSON columns."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
