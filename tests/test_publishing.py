"""Sprint 6 RSS publication, immutable public media, recovery, and endpoint tests."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from test_episode_service import accepted_artifacts, create_episode

from dailycast.core.hashes import sha256_bytes
from dailycast.db.models import EpisodeStatus, PublicationStatus
from dailycast.db.repositories import EpisodeRepository, PublicationRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.main import create_app
from dailycast.publishing.rss import RSSPublisher, RSSSettings
from dailycast.publishing.service import PublicationOperationError, PublicationService
from dailycast.tts.contracts import MergedAudio
from dailycast.tts.providers.fake import FakeTTSProvider
from dailycast.tts.service import AudioGenerationService, TTSGenerationSettings


class AtomicFakeMerger:
    """A local merger that gives publishing tests a deterministic draft audio file."""

    def merge(self, input_paths: tuple[Path, ...], output_path: Path) -> MergedAudio:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.part")
        payload = b"published-draft:" + b"|".join(path.read_bytes() for path in input_paths)
        temporary.write_bytes(payload)
        os.replace(temporary, output_path)
        return MergedAudio(
            duration_ms=len(input_paths) * 1000,
            sample_rate=24_000,
            byte_size=len(payload),
            sha256=sha256_bytes(payload),
        )


def _factory(app_config_path: Path) -> sessionmaker[Session]:
    """Use the Alembic-created SQLite schema shared by the existing editorial fixtures."""
    return __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)


def _ready_episode(factory: sessionmaker[Session], tmp_path: Path, *, key: str, day: int) -> object:
    """Create a fully reviewed, approved Episode with a checksum-valid draft MP3."""
    artifacts = replace(
        accepted_artifacts(factory, key=key),
        episode_date=date(2026, 7, day),
    )
    episode = create_episode(EpisodeService(factory), artifacts)
    audio_service = AudioGenerationService(
        factory,
        FakeTTSProvider(),
        data_dir=tmp_path / "data",
        merger=AtomicFakeMerger(),
        settings=TTSGenerationSettings(voice="zh-CN-XiaoxiaoNeural"),
    )
    asyncio.run(audio_service.generate_episode_draft(episode.id))
    EpisodeService(factory).approve(episode.id)
    ready = EpisodeService(factory).get_episode(episode.id)
    assert ready is not None
    assert ready.status is EpisodeStatus.APPROVED
    return ready


def _publisher(tmp_path: Path) -> RSSPublisher:
    """Create a public RSS writer with an explicit HTTPS enclosure origin."""
    return RSSPublisher(
        data_dir=tmp_path / "data",
        public_dir=tmp_path / "public",
        settings=RSSSettings(
            public_base_url="https://podcast.example.test",
            feed_title="DailyCast Test Feed",
            feed_description="Testable personal news podcast.",
            language="zh-CN",
            author="DailyCast Tester",
        ),
    )


def _service(factory: sessionmaker[Session], tmp_path: Path) -> PublicationService:
    """Create the application service under test without any external publishing platform."""
    return PublicationService(factory, _publisher(tmp_path))


def test_create_publication_is_idempotent_pending_record(
    app_config_path: Path, tmp_path: Path
) -> None:
    """One approved Episode maps to one reusable pending local-rss Publication row."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-create", day=22)
        service = _service(factory, tmp_path)

        first = service.create_publication(episode.id)
        second = service.create_publication(episode.id)

        assert first.id == second.id
        assert first.status is PublicationStatus.PENDING
        assert first.target_key == "local-rss"
        assert first.idempotency_key == f"rss:{episode.public_id}"
    finally:
        factory.kw["bind"].dispose()


def test_publish_copies_checksum_verified_audio_to_immutable_public_asset(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Publishing copies, never exposes, the mutable draft into a public content-addressed path."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-asset", day=22)

        publication = _service(factory, tmp_path).publish(episode.id)

        expected_path = (
            tmp_path
            / "public"
            / "media"
            / "episodes"
            / episode.public_id
            / f"{publication.asset_sha256}.mp3"
        )
        assert publication.status is PublicationStatus.PUBLISHED
        assert (
            publication.public_asset_path
            == expected_path.relative_to(tmp_path / "public").as_posix()
        )
        assert publication.asset_byte_size == expected_path.stat().st_size
        assert publication.asset_sha256 == sha256_bytes(expected_path.read_bytes())
        assert publication.public_audio_url == (
            f"https://podcast.example.test/{publication.public_asset_path}"
        )
        assert publication.public_asset_path != episode.draft_audio_path
    finally:
        factory.kw["bind"].dispose()


def test_publish_rejects_a_draft_audio_file_whose_checksum_no_longer_matches(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A modified draft must not be copied into a public immutable media path."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-checksum", day=22)
        assert episode.draft_audio_path is not None
        (tmp_path / "data" / episode.draft_audio_path).write_bytes(b"corrupted draft audio")

        with pytest.raises(PublicationOperationError):
            _service(factory, tmp_path).publish(episode.id)

        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            publication = PublicationRepository(unit.session).get_by_target(
                episode.id, "rss", "local-rss"
            )
            assert publication is not None
            assert publication.status is PublicationStatus.FAILED
            assert publication.public_asset_path is None
    finally:
        factory.kw["bind"].dispose()


def test_rss_feed_contains_stable_guid_and_valid_enclosure(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Feed items use immutable Episode public IDs, never titles or business dates."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-feed", day=22)

        publication = _service(factory, tmp_path).publish(episode.id)

        root = ElementTree.fromstring((tmp_path / "public" / "feed.xml").read_bytes())
        channel = root.find("channel")
        assert channel is not None
        assert channel.findtext("title") == "DailyCast Test Feed"
        item = channel.find("item")
        assert item is not None
        assert item.findtext("guid") == episode.public_id
        enclosure = item.find("enclosure")
        assert enclosure is not None
        assert enclosure.attrib == {
            "length": str(publication.asset_byte_size),
            "type": "audio/mpeg",
            "url": publication.public_audio_url,
        }
    finally:
        factory.kw["bind"].dispose()


def test_feed_write_is_atomic_and_repeated_publish_never_duplicates_guid(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A repeated idempotent publish leaves one GUID and no partially written feed artifact."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-repeat", day=22)
        service = _service(factory, tmp_path)

        first = service.publish(episode.id)
        second = service.publish(episode.id)

        feed_path = tmp_path / "public" / "feed.xml"
        assert first.id == second.id
        assert not (tmp_path / "public" / "feed.xml.tmp").exists()
        root = ElementTree.fromstring(feed_path.read_bytes())
        assert [item.findtext("guid") for item in root.findall("./channel/item")] == [
            episode.public_id
        ]
    finally:
        factory.kw["bind"].dispose()


def test_reconcile_marks_publishing_row_after_feed_replaced_before_database_commit(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Recovery observes a written candidate GUID and finalizes database state once."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-reconcile", day=22)
        publisher = _publisher(tmp_path)
        service = PublicationService(factory, publisher)
        publication = service.create_publication(episode.id)
        asset = publisher.promote_asset(episode)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            current = PublicationRepository(unit.session).get(publication.id)
            persisted_episode = EpisodeRepository(unit.session).get(episode.id)
            assert current is not None and persisted_episode is not None
            current.status = PublicationStatus.PUBLISHING
            current.attempt_count = 1
            current.public_asset_path = asset.relative_path
            current.public_audio_url = asset.public_url
            current.asset_sha256 = asset.sha256
            current.asset_byte_size = asset.byte_size
            current.feed_guid = episode.public_id
            persisted_episode.status = EpisodeStatus.PUBLISHING
            unit.session.flush()
        publisher.publish((publisher.feed_item(episode, publication, asset),))

        reconciled = service.reconcile()

        assert reconciled == 1
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            current = PublicationRepository(unit.session).get(publication.id)
            assert current is not None
            assert current.status is PublicationStatus.PUBLISHED
            persisted_episode = EpisodeRepository(unit.session).get(episode.id)
            assert persisted_episode is not None
            assert persisted_episode.status is EpisodeStatus.PUBLISHED
    finally:
        factory.kw["bind"].dispose()


def test_reconcile_keeps_pending_publication_retryable_until_an_asset_exists(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A crash before promotion leaves the pending row intact and creates no Feed item."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-pending", day=22)
        service = _service(factory, tmp_path)
        publication = service.create_publication(episode.id)

        assert service.reconcile() == 0
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            current = PublicationRepository(unit.session).get(publication.id)
            assert current is not None
            assert current.status is PublicationStatus.PENDING
        assert not (tmp_path / "public" / "feed.xml").exists()
    finally:
        factory.kw["bind"].dispose()


def test_reconcile_rebuilds_feed_when_a_published_item_is_missing(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Stable published rows rebuild a missing Feed without copying or duplicating audio assets."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-rebuild", day=22)
        service = _service(factory, tmp_path)
        publication = service.publish(episode.id)
        feed_path = tmp_path / "public" / "feed.xml"
        feed_path.unlink()

        assert service.reconcile() == 1
        root = ElementTree.fromstring(feed_path.read_bytes())
        assert [item.findtext("guid") for item in root.findall("./channel/item")] == [
            episode.public_id
        ]
        assert publication.public_asset_path is not None
        assert (tmp_path / "public" / publication.public_asset_path).is_file()
    finally:
        factory.kw["bind"].dispose()


def test_public_feed_and_audio_endpoints_support_etag_and_byte_ranges(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Only published immutable media is served, with standard ETag and Range behavior."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="publication-endpoints", day=22)
        publication = _service(factory, tmp_path).publish(episode.id)

        with TestClient(create_app(config_path=app_config_path)) as client:
            feed = client.get("/feed.xml")
            media = client.get(
                f"/media/episodes/{episode.public_id}/{publication.asset_sha256}.mp3"
            )
            ranged = client.get(
                f"/media/episodes/{episode.public_id}/{publication.asset_sha256}.mp3",
                headers={"Range": "bytes=0-9"},
            )

        assert feed.status_code == 200
        assert episode.public_id in feed.text
        assert media.status_code == 200
        assert media.headers["etag"] == f'"{publication.asset_sha256}"'
        assert media.headers["accept-ranges"] == "bytes"
        assert ranged.status_code == 206
        assert ranged.headers["content-range"].startswith("bytes 0-9/")
        assert len(ranged.content) == 10
    finally:
        factory.kw["bind"].dispose()
