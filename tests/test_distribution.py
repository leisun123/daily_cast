"""Sprint 10 independent multi-platform publication lifecycle tests."""

from __future__ import annotations

import importlib.util
import tomllib
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from test_episode_service import accepted_artifacts, create_episode
from test_publishing import _ready_episode, _service

import dailycast.db.models as db_models
import dailycast.db.repositories as db_repositories
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.main import create_app
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.steps.publish import PublishStep


def _factory(app_config_path: Path):
    return __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)


def _episode(factory, *, key: str, day: int):
    artifacts = accepted_artifacts(factory, key=key)
    artifacts = replace(artifacts, episode_date=date(2026, 7, day))
    return create_episode(EpisodeService(factory), artifacts)


def test_publication_target_model_and_repository_track_each_platform_independently(
    app_config_path: Path,
) -> None:
    """One Episode owns one durable target row per platform with isolated state."""
    assert hasattr(db_models, "PublicationTarget")
    assert hasattr(db_models, "PublicationPlatform")
    assert hasattr(db_models, "PublicationTargetStatus")
    assert hasattr(db_repositories, "PublicationTargetRepository")
    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="distribution-targets", day=25)
        repository_type = db_repositories.PublicationTargetRepository
        platform = db_models.PublicationPlatform
        status = db_models.PublicationTargetStatus
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            repository = repository_type(unit.session)
            rss = repository.create(
                episode_id=episode.id,
                platform=platform.RSS,
                status=status.PENDING,
            )
            netease = repository.create(
                episode_id=episode.id,
                platform=platform.NETEASE,
                status=status.NEEDS_ATTENTION,
                last_error="NETEASE_LOGIN_REQUIRED",
                attempt_count=1,
            )

            assert repository.get_by_episode_and_platform(episode.id, platform.RSS) is rss
            assert repository.get_by_episode_and_platform(episode.id, platform.NETEASE) is netease
            assert repository.list_by_episode(episode.id) == [rss, netease]
            assert rss.status is status.PENDING
            assert netease.status is status.NEEDS_ATTENTION
            assert netease.last_error == "NETEASE_LOGIN_REQUIRED"
    finally:
        factory.kw["bind"].dispose()


def test_publication_target_rejects_duplicate_episode_platform(app_config_path: Path) -> None:
    """Retries must reuse the target row instead of duplicating platform delivery."""
    assert hasattr(db_models, "PublicationTarget")
    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="distribution-unique", day=26)
        target_type = db_models.PublicationTarget
        platform = db_models.PublicationPlatform
        status = db_models.PublicationTargetStatus
        with pytest.raises(IntegrityError), UnitOfWork(factory) as unit:
            assert unit.session is not None
            unit.session.add_all(
                [
                    target_type(
                        episode_id=episode.id,
                        platform=platform.NETEASE,
                        status=status.PENDING,
                    ),
                    target_type(
                        episode_id=episode.id,
                        platform=platform.NETEASE,
                        status=status.PENDING,
                    ),
                ]
            )
            unit.session.flush()
    finally:
        factory.kw["bind"].dispose()


def test_publication_target_rejects_unknown_episode(app_config_path: Path) -> None:
    """SQLite foreign keys prevent orphan platform delivery state."""
    assert hasattr(db_models, "PublicationTarget")
    factory = _factory(app_config_path)
    try:
        target_type = db_models.PublicationTarget
        platform = db_models.PublicationPlatform
        status = db_models.PublicationTargetStatus
        with pytest.raises(IntegrityError), UnitOfWork(factory) as unit:
            assert unit.session is not None
            unit.session.add(
                target_type(
                    episode_id=999_999,
                    platform=platform.NETEASE,
                    status=status.PENDING,
                )
            )
            unit.session.flush()
    finally:
        factory.kw["bind"].dispose()


def test_dispatcher_isolates_platform_failure_and_keeps_successful_target(
    app_config_path: Path,
) -> None:
    """A NetEase attention state cannot roll back an independently successful RSS target."""
    assert importlib.util.find_spec("dailycast.publishing.dispatcher") is not None
    from dailycast.publishing.contracts import (
        PlatformPublishResult,
        PublisherNeedsAttentionError,
    )
    from dailycast.publishing.dispatcher import PublicationDispatcher

    platform = db_models.PublicationPlatform
    target_status = db_models.PublicationTargetStatus

    class SuccessfulRSSPublisher:
        platform_name = platform.RSS

        async def validate(self, episode) -> None:
            assert episode.id > 0

        async def publish(self, episode) -> PlatformPublishResult:
            return PlatformPublishResult(
                remote_id=episode.public_id,
                remote_url="https://podcast.example.test/feed.xml",
            )

        async def check_status(self, episode, target) -> PlatformPublishResult:
            return await self.publish(episode)

        async def resume(self, episode, target) -> PlatformPublishResult:
            return await self.publish(episode)

    class LoginExpiredNetEasePublisher:
        platform_name = platform.NETEASE

        async def validate(self, episode) -> None:
            assert episode.id > 0

        async def publish(self, episode) -> PlatformPublishResult:
            raise PublisherNeedsAttentionError("NETEASE_LOGIN_REQUIRED")

        async def check_status(self, episode, target) -> PlatformPublishResult:
            raise PublisherNeedsAttentionError("NETEASE_LOGIN_REQUIRED")

        async def resume(self, episode, target) -> PlatformPublishResult:
            raise PublisherNeedsAttentionError("NETEASE_LOGIN_REQUIRED")

    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="distribution-isolation", day=27)
        result = __import__("asyncio").run(
            PublicationDispatcher(
                factory,
                (SuccessfulRSSPublisher(), LoginExpiredNetEasePublisher()),
            ).publish(episode.id)
        )

        assert result.published_platforms == (platform.RSS,)
        assert result.needs_attention_platforms == (platform.NETEASE,)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            repository = db_repositories.PublicationTargetRepository(unit.session)
            rss = repository.get_by_episode_and_platform(episode.id, platform.RSS)
            netease = repository.get_by_episode_and_platform(episode.id, platform.NETEASE)
            assert rss is not None and rss.status is target_status.PUBLISHED
            assert netease is not None and netease.status is target_status.NEEDS_ATTENTION
            assert netease.last_error == "NETEASE_LOGIN_REQUIRED"
            assert rss.attempt_count == 1
            assert netease.attempt_count == 1
    finally:
        factory.kw["bind"].dispose()


def test_dispatcher_resume_retries_only_requested_platform(app_config_path: Path) -> None:
    """Manual recovery resumes NetEase without regenerating or republishing RSS."""
    assert importlib.util.find_spec("dailycast.publishing.dispatcher") is not None
    from dailycast.publishing.contracts import PlatformPublishResult
    from dailycast.publishing.dispatcher import PublicationDispatcher

    platform = db_models.PublicationPlatform

    class RecordingPublisher:
        def __init__(self, platform_name) -> None:
            self.platform_name = platform_name
            self.publish_calls = 0
            self.resume_calls = 0

        async def validate(self, episode) -> None:
            assert episode.id > 0

        async def publish(self, episode) -> PlatformPublishResult:
            self.publish_calls += 1
            return PlatformPublishResult(remote_id=f"{self.platform_name.value}-first")

        async def check_status(self, episode, target) -> PlatformPublishResult:
            return PlatformPublishResult(remote_id=target.remote_id)

        async def resume(self, episode, target) -> PlatformPublishResult:
            self.resume_calls += 1
            return PlatformPublishResult(
                remote_id="netease-resumed",
                remote_url="https://music.163.com/podcast/netease-resumed",
            )

    rss = RecordingPublisher(platform.RSS)
    netease = RecordingPublisher(platform.NETEASE)
    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="distribution-resume", day=28)
        dispatcher = PublicationDispatcher(factory, (rss, netease))
        __import__("asyncio").run(dispatcher.publish(episode.id))

        resumed = __import__("asyncio").run(dispatcher.resume(episode.id, platform.NETEASE))

        assert resumed.remote_id == "netease-resumed"
        assert rss.publish_calls == 1
        assert rss.resume_calls == 0
        assert netease.publish_calls == 1
        assert netease.resume_calls == 1
    finally:
        factory.kw["bind"].dispose()


def test_rss_distribution_adapter_preserves_existing_atomic_publication(
    app_config_path: Path, tmp_path: Path
) -> None:
    """The dispatcher wraps, rather than replaces, the crash-safe RSS Publication service."""
    from dailycast.publishing.dispatcher import (
        PublicationDispatcher,
        RSSDistributionPublisher,
    )

    platform = db_models.PublicationPlatform
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="distribution-rss", day=29)
        result = __import__("asyncio").run(
            PublicationDispatcher(
                factory,
                (RSSDistributionPublisher(_service(factory, tmp_path)),),
            ).publish(episode.id)
        )

        assert result.published_platforms == (platform.RSS,)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            publication = db_repositories.PublicationRepository(
                unit.session
            ).get_published_for_episode(episode.id)
            assert publication is not None
            assert publication.public_audio_url is not None
            assert publication.feed_guid == episode.public_id
    finally:
        factory.kw["bind"].dispose()


class FakeNetEaseBrowser:
    """Deterministic browser boundary used without internet or a real account."""

    def __init__(
        self,
        *,
        authenticated: bool = True,
        captcha: bool = False,
        existing: object | None = None,
        upload_error: Exception | None = None,
        submitted_result: object | None = None,
    ) -> None:
        self.authenticated = authenticated
        self.captcha = captcha
        self.existing = existing
        self.upload_error = upload_error
        self.submitted_result = submitted_result
        self.opened_urls: list[str] = []
        self.uploaded_paths: list[Path] = []
        self.metadata: list[tuple[str, str, str, Path | None]] = []
        self.submit_calls = 0
        self.wait_for_login_calls = 0
        self.storage_state_saved = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def open(self, url: str) -> None:
        self.opened_urls.append(url)

    async def is_authenticated(self) -> bool:
        return self.authenticated

    async def has_human_verification(self) -> bool:
        return self.captcha

    async def find_existing(self, title: str):
        assert title
        return self.existing

    async def upload_audio(self, path: Path) -> None:
        if self.upload_error is not None:
            raise self.upload_error
        self.uploaded_paths.append(path)

    async def fill_metadata(
        self,
        *,
        title: str,
        description: str,
        category: str,
        cover_path: Path | None,
    ) -> None:
        self.metadata.append((title, description, category, cover_path))

    async def submit(self):
        self.submit_calls += 1
        return self.submitted_result or SimpleNamespace(
            remote_id="netease-episode-1",
            remote_url="https://music.163.com/program?id=netease-episode-1",
        )

    async def wait_until_authenticated(self, timeout_ms: int) -> None:
        assert timeout_ms > 0
        self.wait_for_login_calls += 1
        self.authenticated = True

    async def save_storage_state(self) -> None:
        self.storage_state_saved = True


def _netease_publisher(factory, tmp_path: Path, browser: FakeNetEaseBrowser):
    assert importlib.util.find_spec("dailycast.publishing.netease") is not None
    from dailycast.publishing.netease import (
        NetEasePlaywrightPublisher,
        NetEasePublisherSettings,
    )

    return NetEasePlaywrightPublisher(
        factory,
        public_dir=tmp_path / "public",
        settings=NetEasePublisherSettings(
            profile_dir=tmp_path / "data" / "netease" / "profile",
            storage_state_path=(tmp_path / "data" / "netease" / "storage-state.json"),
            creator_url="https://musicupload.netease.com/",
            headless=True,
            category="科技",
        ),
        browser_factory=lambda _: browser,
    )


def test_netease_publisher_uploads_only_immutable_rss_asset(
    app_config_path: Path, tmp_path: Path
) -> None:
    """NetEase receives the verified public MP3, never the mutable Episode draft."""
    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="netease-upload", day=30)
        publication = _service(factory, tmp_path).publish(episode.id)
        browser = FakeNetEaseBrowser()
        publisher = _netease_publisher(factory, tmp_path, browser)

        result = __import__("asyncio").run(publisher.publish(episode))

        assert result.remote_id == "netease-episode-1"
        assert browser.submit_calls == 1
        assert browser.uploaded_paths == [tmp_path / "public" / str(publication.public_asset_path)]
        assert browser.metadata == [(str(episode.title), str(episode.description), "科技", None)]
        assert browser.uploaded_paths[0] != tmp_path / "data" / str(episode.draft_audio_path)
    finally:
        factory.kw["bind"].dispose()


@pytest.mark.parametrize(
    ("authenticated", "captcha", "expected_code"),
    [
        pytest.param(False, False, "NETEASE_LOGIN_REQUIRED", id="login-expired"),
        pytest.param(True, True, "NETEASE_HUMAN_VERIFICATION_REQUIRED", id="captcha"),
    ],
)
def test_netease_publisher_requires_human_attention_for_authentication_or_captcha(
    app_config_path: Path,
    tmp_path: Path,
    authenticated: bool,
    captcha: bool,
    expected_code: str,
) -> None:
    """The RPA stops for platform security controls instead of bypassing them."""
    from dailycast.publishing.contracts import PublisherNeedsAttentionError

    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(
            factory,
            tmp_path,
            key=f"netease-attention-{expected_code}",
            day=31 if authenticated else 30,
        )
        _service(factory, tmp_path).publish(episode.id)
        publisher = _netease_publisher(
            factory,
            tmp_path,
            FakeNetEaseBrowser(authenticated=authenticated, captcha=captcha),
        )

        with pytest.raises(PublisherNeedsAttentionError, match=expected_code):
            __import__("asyncio").run(publisher.publish(episode))
    finally:
        factory.kw["bind"].dispose()


def test_netease_upload_failure_is_platform_failure(app_config_path: Path, tmp_path: Path) -> None:
    """A failed upload is retryable platform work, not a successful remote publication."""
    from dailycast.publishing.contracts import PublisherError

    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="netease-upload-failure", day=30)
        _service(factory, tmp_path).publish(episode.id)
        publisher = _netease_publisher(
            factory,
            tmp_path,
            FakeNetEaseBrowser(upload_error=TimeoutError("upload timed out")),
        )

        with pytest.raises(PublisherError, match="NETEASE_UPLOAD_FAILED"):
            __import__("asyncio").run(publisher.publish(episode))
    finally:
        factory.kw["bind"].dispose()


def test_netease_unknown_submit_result_requires_attention(
    app_config_path: Path, tmp_path: Path
) -> None:
    """An accepted click without a remote identity must never be marked published."""
    from dailycast.publishing.contracts import PublisherNeedsAttentionError

    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="netease-unknown-submit", day=30)
        _service(factory, tmp_path).publish(episode.id)
        publisher = _netease_publisher(
            factory,
            tmp_path,
            FakeNetEaseBrowser(
                submitted_result=SimpleNamespace(
                    remote_id=None,
                    remote_url="https://musicupload.netease.com/",
                )
            ),
        )

        with pytest.raises(
            PublisherNeedsAttentionError,
            match="NETEASE_REMOTE_STATUS_UNKNOWN",
        ):
            __import__("asyncio").run(publisher.publish(episode))
    finally:
        factory.kw["bind"].dispose()


def test_netease_resume_reuses_remote_episode_without_upload(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Crash recovery searches by title before upload so resume cannot duplicate a program."""
    from dailycast.publishing.contracts import PlatformPublishResult

    factory = _factory(app_config_path)
    try:
        episode = _ready_episode(factory, tmp_path, key="netease-resume", day=30)
        _service(factory, tmp_path).publish(episode.id)
        existing = PlatformPublishResult(
            remote_id="already-there",
            remote_url="https://music.163.com/program?id=already-there",
        )
        browser = FakeNetEaseBrowser(existing=existing)
        publisher = _netease_publisher(factory, tmp_path, browser)
        target = SimpleNamespace(remote_id=None, remote_url=None)

        result = __import__("asyncio").run(publisher.resume(episode, target))

        assert result == existing
        assert browser.uploaded_paths == []
        assert browser.submit_calls == 0
    finally:
        factory.kw["bind"].dispose()


def test_netease_login_bootstrap_waits_for_scan_and_saves_private_state(
    tmp_path: Path,
) -> None:
    """The one-time operator flow keeps the official page open until login succeeds."""
    from dailycast.publishing.netease import (
        NetEasePublisherSettings,
        establish_netease_login,
    )

    browser = FakeNetEaseBrowser(authenticated=False)
    settings = NetEasePublisherSettings(
        profile_dir=tmp_path / "data" / "netease" / "profile",
        storage_state_path=(tmp_path / "data" / "netease" / "storage-state.json"),
        headless=False,
    )

    __import__("asyncio").run(establish_netease_login(settings, browser_factory=lambda _: browser))

    assert browser.opened_urls == ["https://musicupload.netease.com/"]
    assert browser.wait_for_login_calls == 1
    assert browser.storage_state_saved is True


def test_xiaoyuzhou_adapter_uses_rss_claim_state_without_browser_upload(
    app_config_path: Path,
) -> None:
    """A configured Xiaoyuzhou program URL records distribution without coupling upload logic."""
    assert importlib.util.find_spec("dailycast.publishing.xiaoyuzhou") is not None
    from dailycast.publishing.xiaoyuzhou import XiaoyuzhouPublisher

    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="xiaoyuzhou-rss", day=29)
        publisher = XiaoyuzhouPublisher(
            program_url="https://www.xiaoyuzhoufm.com/podcast/dailycast"
        )

        result = __import__("asyncio").run(publisher.publish(episode))

        assert result.remote_id == "dailycast"
        assert result.remote_url == "https://www.xiaoyuzhoufm.com/podcast/dailycast"
    finally:
        factory.kw["bind"].dispose()


def test_xiaoyuzhou_unclaimed_rss_requires_attention_not_failure(
    app_config_path: Path,
) -> None:
    """Enabling an unclaimed RSS adapter honestly asks for manual platform import."""
    assert importlib.util.find_spec("dailycast.publishing.xiaoyuzhou") is not None
    from dailycast.publishing.contracts import PublisherNeedsAttentionError
    from dailycast.publishing.xiaoyuzhou import XiaoyuzhouPublisher

    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="xiaoyuzhou-unclaimed", day=30)

        with pytest.raises(
            PublisherNeedsAttentionError,
            match="XIAOYUZHOU_RSS_IMPORT_REQUIRED",
        ):
            __import__("asyncio").run(XiaoyuzhouPublisher(program_url=None).publish(episode))
    finally:
        factory.kw["bind"].dispose()


def test_publish_step_records_multi_platform_outcomes_without_failing_episode(
    app_config_path: Path,
) -> None:
    """One needs-attention target produces warnings while the generation pipeline completes."""
    from dailycast.publishing.contracts import DistributionResult

    platform = db_models.PublicationPlatform
    target_status = db_models.PublicationTargetStatus

    class RecordingDispatcher:
        async def publish(self, episode_id: int) -> DistributionResult:
            assert episode_id > 0
            return DistributionResult(
                (
                    SimpleNamespace(
                        id=1,
                        platform=platform.RSS,
                        status=target_status.PUBLISHED,
                        remote_id="feed-guid",
                        remote_url="https://podcast.example.test/feed.xml",
                        last_error=None,
                    ),
                    SimpleNamespace(
                        id=2,
                        platform=platform.NETEASE,
                        status=target_status.NEEDS_ATTENTION,
                        remote_id=None,
                        remote_url=None,
                        last_error="NETEASE_LOGIN_REQUIRED",
                    ),
                )
            )

    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="distribution-pipeline", day=28)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            task_run = db_repositories.TaskRunRepository(unit.session).create(
                id="distribution-pipeline-task",
                task_type="daily_generate",
                business_key="daily:2026-07-28:daily:rss-v1",
                idempotency_key="distribution-pipeline-task",
                trigger_type="manual",
                status="running",
                pipeline_version="rss-v1",
                config_fingerprint="a" * 64,
                config_snapshot_json="{}",
                request_json="{}",
            )
            step = db_repositories.TaskStepRepository(unit.session).create(
                task_run_id=task_run.id,
                step_name="publish",
                step_order=12,
                attempt=1,
                status="running",
                details_json="{}",
            )
        result = __import__("asyncio").run(
            PublishStep(
                EpisodeService(factory),
                RecordingDispatcher(),
                auto_publish=True,
            ).run(
                PipelineContext(
                    task_run_id="distribution-pipeline-task",
                    session_factory=factory,
                    shutdown_requested=__import__("asyncio").Event(),
                    clock=__import__("dailycast.core.time", fromlist=["Clock"]).Clock(),
                    values={
                        "episode_id": episode.id,
                        "active_task_step_id": step.id,
                    },
                )
            )
        )

        assert result.output_count == 1
        assert result.warning_count == 1
        assert result.details["platform_statuses"] == {
            "netease": "needs_attention",
            "rss": "published",
        }
        assert result.details["platform_errors"] == {"netease": "NETEASE_LOGIN_REQUIRED"}
    finally:
        factory.kw["bind"].dispose()


def test_runtime_builds_only_enabled_distribution_publishers(
    app_config_path: Path, tmp_path: Path
) -> None:
    """Configuration selects adapters while NetEase profile state stays under DATA_DIR."""
    from dailycast.core.config import (
        NetEasePublishingSettings,
        PublishingSettings,
        RSSPublishingSettings,
        XiaoyuzhouPublishingSettings,
    )
    from dailycast.core.lifespan import build_distribution_publishers

    factory = _factory(app_config_path)
    try:
        settings = PublishingSettings(
            rss=RSSPublishingSettings(enabled=True),
            netease=NetEasePublishingSettings(
                enabled=True,
                profile_dir=Path("netease/profile"),
            ),
            xiaoyuzhou=XiaoyuzhouPublishingSettings(enabled=False),
        )

        publishers = build_distribution_publishers(
            settings,
            session_factory=factory,
            data_dir=tmp_path / "data",
            public_dir=tmp_path / "public",
            rss_service=_service(factory, tmp_path),
        )

        assert [publisher.platform_name.value for publisher in publishers] == [
            "rss",
            "netease",
        ]
        netease = publishers[1]
        assert netease._settings.profile_dir == tmp_path / "data" / "netease" / "profile"
        assert netease._settings.storage_state_path == (
            tmp_path / "data" / "netease" / "storage-state.json"
        )
    finally:
        factory.kw["bind"].dispose()


def test_runtime_rejects_netease_profile_outside_private_data_root(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A malicious absolute path cannot move browser credentials outside DATA_DIR."""
    from dailycast.core.config import (
        NetEasePublishingSettings,
        PublishingSettings,
        RSSPublishingSettings,
    )
    from dailycast.core.lifespan import build_distribution_publishers

    factory = _factory(app_config_path)
    try:
        settings = PublishingSettings(
            rss=RSSPublishingSettings(enabled=True),
            netease=NetEasePublishingSettings(
                enabled=True,
                profile_dir=tmp_path / "outside-profile",
            ),
        )

        with pytest.raises(ValueError, match="must stay below DATA_DIR"):
            build_distribution_publishers(
                settings,
                session_factory=factory,
                data_dir=tmp_path / "data",
                public_dir=tmp_path / "public",
                rss_service=_service(factory, tmp_path),
            )
    finally:
        factory.kw["bind"].dispose()


def test_dispatcher_reconciles_interrupted_publishing_target_on_startup(
    app_config_path: Path,
) -> None:
    """A crash after remote submit is finalized by status inspection, not duplicate upload."""
    from dailycast.publishing.contracts import PlatformPublishResult
    from dailycast.publishing.dispatcher import PublicationDispatcher

    platform = db_models.PublicationPlatform
    target_status = db_models.PublicationTargetStatus

    class ReconciledPublisher:
        platform_name = platform.NETEASE

        async def validate(self, episode) -> None:
            assert episode.id > 0

        async def publish(self, episode) -> PlatformPublishResult:
            raise AssertionError("startup reconcile must not publish")

        async def check_status(self, episode, target) -> PlatformPublishResult:
            assert target.status is target_status.PUBLISHING
            return PlatformPublishResult(
                remote_id="reconciled",
                remote_url="https://music.163.com/program?id=reconciled",
            )

        async def resume(self, episode, target) -> PlatformPublishResult:
            raise AssertionError("startup reconcile must not resume")

    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="distribution-reconcile", day=27)
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            db_repositories.PublicationTargetRepository(unit.session).create(
                episode_id=episode.id,
                platform=platform.NETEASE,
                status=target_status.PUBLISHING,
                attempt_count=1,
            )
        dispatcher = PublicationDispatcher(factory, (ReconciledPublisher(),))

        recovered = __import__("asyncio").run(dispatcher.reconcile())

        assert recovered == 1
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            target = db_repositories.PublicationTargetRepository(
                unit.session
            ).get_by_episode_and_platform(episode.id, platform.NETEASE)
            assert target is not None
            assert target.status is target_status.PUBLISHED
            assert target.remote_id == "reconciled"
    finally:
        factory.kw["bind"].dispose()


def test_management_endpoint_resumes_only_requested_publication_target(
    app_config_path: Path,
) -> None:
    """An operator can resume NetEase after login without regenerating the Episode."""
    platform = db_models.PublicationPlatform
    target_status = db_models.PublicationTargetStatus

    class RecordingDispatcher:
        def __init__(self) -> None:
            self.calls: list[tuple[int, object]] = []

        async def resume(self, episode_id: int, requested_platform: object):
            self.calls.append((episode_id, requested_platform))
            return SimpleNamespace(
                id=91,
                episode_id=episode_id,
                platform=platform.NETEASE,
                status=target_status.PUBLISHED,
                remote_id="netease-resumed",
                remote_url="https://music.163.com/program?id=netease-resumed",
                last_error=None,
                attempt_count=2,
            )

    factory = _factory(app_config_path)
    try:
        episode = _episode(factory, key="distribution-resume-endpoint", day=26)
        dispatcher = RecordingDispatcher()
        app = create_app(config_path=app_config_path)
        with TestClient(app) as client:
            app.state.runtime = replace(
                app.state.runtime,
                publication_dispatcher=dispatcher,
            )
            response = client.post(f"/episodes/{episode.id}/publications/netease/resume")

        assert response.status_code == 200
        assert response.json() == {
            "id": 91,
            "episode_id": episode.id,
            "platform": "netease",
            "status": "published",
            "remote_id": "netease-resumed",
            "remote_url": "https://music.163.com/program?id=netease-resumed",
            "last_error": None,
            "attempt_count": 2,
        }
        assert dispatcher.calls == [(episode.id, platform.NETEASE)]
    finally:
        factory.kw["bind"].dispose()


def test_container_installs_playwright_chromium_for_single_service_rpa() -> None:
    """The shipped container includes the browser runtime used by the enabled adapter."""
    project_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")

    assert "playwright" in pyproject["tool"]["poetry"]["dependencies"]
    assert (
        pyproject["tool"]["poetry"]["scripts"]["dailycast-netease-login"]
        == "dailycast.publishing.netease_login:main"
    )
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile


def test_zeabur_template_persists_netease_profile_and_documents_target_settings() -> None:
    """GitHub deployments keep login state inside the existing private /app/data volume."""
    project_root = Path(__file__).resolve().parents[1]
    template = (project_root / "zeabur.yaml").read_text(encoding="utf-8")
    env_example = (project_root / ".env.example").read_text(encoding="utf-8")

    assert "dir: /app/data" in template
    assert "DAILYCAST_PUBLISHING__NETEASE__ENABLED:" in template
    assert "DAILYCAST_PUBLISHING__NETEASE__PROFILE_DIR:" in template
    assert "DAILYCAST_PUBLISHING__NETEASE__STORAGE_STATE_PATH:" in template
    assert "DAILYCAST_PUBLISHING__RSS__ENABLED:" in template
    assert "DAILYCAST_PUBLISHING__NETEASE__ENABLED=false" in env_example
    assert "DAILYCAST_PUBLISHING__NETEASE__PROFILE_DIR=netease/profile" in env_example
    assert (
        "DAILYCAST_PUBLISHING__NETEASE__STORAGE_STATE_PATH=" "netease/storage-state.json"
    ) in env_example
