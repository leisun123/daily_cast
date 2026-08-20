"""Sprint 10 multi-platform distribution lifecycle tests."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient

import dailycast.db.models  # noqa: F401 - import mappings into SQLAlchemy metadata.
import dailycast.db.repositories as repositories
from dailycast.core.config import load_settings
from dailycast.core.lifespan import build_distribution_publishers
from dailycast.core.time import Clock
from dailycast.db.base import Base
from dailycast.db.models import (
    Episode,
    EpisodeStatus,
    PublicationPlatform,
    PublicationTarget,
    PublicationTargetStatus,
)
from dailycast.db.repositories import EpisodeRepository, PublicationTargetRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.episodes.service import EpisodeService
from dailycast.main import create_app
from dailycast.pipeline.context import PipelineContext
from dailycast.pipeline.steps.publish import PublishStep
from dailycast.publishing.contracts import PlatformPublishResult, PublicAsset
from dailycast.publishing.dispatcher import PlatformNeedsAttentionError, PublicationDispatcher
from dailycast.publishing.netease import NetEasePlaywrightPublisher, NetEasePublishingSettings
from dailycast.publishing.service import PublicationOperationError


def test_distribution_metadata_defines_independent_publication_targets() -> None:
    """Every configured platform needs its own durable target lifecycle row."""
    assert "publication_targets" in Base.metadata.tables


def test_distribution_exposes_a_target_repository() -> None:
    """Target lifecycle persistence belongs in an explicit repository, not publisher adapters."""
    assert hasattr(repositories, "PublicationTargetRepository")


def test_enabled_platforms_are_built_with_a_private_persistent_netease_profile(
    app_config_path: Path,
) -> None:
    """The deployment only mounts a private profile when NetEase is explicitly enabled."""
    app_config_path.write_text(
        app_config_path.read_text(encoding="utf-8") + """
publishing:
  rss:
    enabled: true
  netease:
    enabled: true
    profile_dir: netease/profile
    cover_path: config/cover.png
  xiaoyuzhou:
    enabled: false
""",
        encoding="utf-8",
    )
    settings = load_settings(config_path=app_config_path)

    publishers = build_distribution_publishers(settings, cast(object, None))

    assert [publisher.platform_name for publisher in publishers] == [
        PublicationPlatform.RSS,
        PublicationPlatform.NETEASE,
    ]
    netease = publishers[1]
    assert isinstance(netease, NetEasePlaywrightPublisher)
    assert netease.profile_dir == settings.data_dir / "netease" / "profile"


def test_netease_distribution_uses_a_dedicated_playwright_adapter_module() -> None:
    """The NetEase target must be isolated from the RSS writer and generation pipeline."""
    assert (Path(__file__).parents[1] / "src" / "dailycast" / "publishing" / "netease.py").is_file()


def test_xiaoyuzhou_preparation_is_isolated_in_its_own_disabled_adapter_module() -> None:
    """The future RSS consumer has a contract home without gaining browser automation early."""
    assert (
        Path(__file__).parents[1] / "src" / "dailycast" / "publishing" / "xiaoyuzhou.py"
    ).is_file()


def test_disabled_xiaoyuzhou_never_creates_a_distribution_target(app_config_path: Path) -> None:
    """The default adapter list is RSS-only, so a future platform has no pipeline side effects."""
    settings = load_settings(config_path=app_config_path)

    publishers = build_distribution_publishers(settings, cast(object, None))

    assert [publisher.platform_name for publisher in publishers] == [PublicationPlatform.RSS]


def test_publish_step_keeps_an_episode_valid_when_one_external_target_needs_attention() -> None:
    """A NetEase login action records a warning without undoing a successful RSS publication."""

    class ApprovedEpisodeService:
        def get_episode(self, episode_id: int) -> object:
            assert episode_id == 42
            return SimpleNamespace(status=EpisodeStatus.APPROVED)

        def approve(self, episode_id: int) -> None:
            raise AssertionError(f"unexpected approval transition for {episode_id}")

    class RecordingDispatcher:
        def __init__(self) -> None:
            self.episode_ids: list[int] = []

        async def publish(self, episode_id: int) -> object:
            self.episode_ids.append(episode_id)
            return SimpleNamespace(
                rss_publication=SimpleNamespace(
                    id=9,
                    public_asset_path="media/episodes/episode-42/immutable.mp3",
                    feed_guid="episode-42",
                    status=SimpleNamespace(value="published"),
                    response_summary_json='{"feed_version":"feed-v1","asset_reused":false}',
                ),
                target_statuses={"rss": "published", "netease": "needs_attention"},
                warning_count=1,
            )

    dispatcher = RecordingDispatcher()
    result = asyncio.run(
        PublishStep(
            cast(EpisodeService, ApprovedEpisodeService()),
            cast(object, dispatcher),
            auto_publish=True,
        ).run(
            PipelineContext(
                task_run_id="distribution-task",
                session_factory=cast(object, None),
                shutdown_requested=asyncio.Event(),
                clock=Clock(),
                values={"active_task_step_id": 1, "episode_id": 42},
            )
        )
    )

    assert dispatcher.episode_ids == [42]
    assert result.output_count == 1
    assert result.warning_count == 1
    assert result.details["target_statuses"] == {
        "rss": "published",
        "netease": "needs_attention",
    }


class _FakeNetEasePage:
    """Small deterministic browser fake that mirrors the page operations the publisher consumes."""

    def __init__(self, *, login_visible: bool = False, fail_upload: bool = False) -> None:
        self.login_visible = login_visible
        self.fail_upload = fail_upload
        self.actions: list[tuple[str, str]] = []

    async def goto(self, url: str) -> None:
        self.actions.append(("goto", url))

    async def is_visible(self, selector: str) -> bool:
        if selector == "text=登录":
            return self.login_visible
        if selector == "text=/验证码|安全验证|滑块/":
            return False
        if selector == "text=发布声音":
            return not self.login_visible
        return False

    async def click(self, selector: str) -> None:
        self.actions.append(("click", selector))

    async def set_input_files(self, selector: str, path: Path) -> None:
        self.actions.append(("upload", f"{selector}:{path.name}"))
        if self.fail_upload:
            raise OSError("upload transport failed")

    async def fill(self, selector: str, value: str) -> None:
        self.actions.append(("fill", f"{selector}:{value}"))

    async def wait_for_visible(self, selector: str, timeout_ms: int) -> None:
        self.actions.append(("wait", f"{selector}:{timeout_ms}"))

    async def current_url(self) -> str:
        return "https://music.163.com/creatorcenter/voice/remote-42"

    async def attribute(self, selector: str, name: str) -> str | None:
        if name == "data-id":
            return "remote-42"
        return None


class _FakeNetEaseSession:
    """Async context manager preserving the real persistent-session lifecycle shape."""

    def __init__(self, page: _FakeNetEasePage) -> None:
        self._page = page

    async def __aenter__(self) -> _FakeNetEasePage:
        return self._page

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        del exc_type, exc_value, traceback
        return None


def _netease_episode() -> Episode:
    timestamp = datetime.now(UTC)
    return Episode(
        id=42,
        public_id="00000000-0000-0000-0000-000000000042",
        episode_date=timestamp.date(),
        status=EpisodeStatus.APPROVED,
        title="今日 DailyCast",
        description="一份已验证的新闻节目。",
        created_at=timestamp,
        updated_at=timestamp,
    )


def _netease_target() -> PublicationTarget:
    timestamp = datetime.now(UTC)
    return PublicationTarget(
        id=7,
        episode_id=42,
        platform=PublicationPlatform.NETEASE,
        status=PublicationTargetStatus.PUBLISHING,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _netease_publisher(tmp_path: Path, page: _FakeNetEasePage) -> NetEasePlaywrightPublisher:
    audio_path = tmp_path / "immutable.mp3"
    audio_path.write_bytes(b"valid immutable audio")
    cover_path = tmp_path / "cover.png"
    cover_path.write_bytes(b"cover")
    return NetEasePlaywrightPublisher(
        NetEasePublishingSettings(
            creator_url="https://music.163.com/creatorcenter",
            profile_dir=tmp_path / "netease-profile",
            headless=True,
            cover_path=cover_path,
            category="资讯",
        ),
        browser_session_factory=lambda profile_dir, headless: _FakeNetEaseSession(page),
    )


def _immutable_asset(tmp_path: Path) -> PublicAsset:
    audio_path = tmp_path / "immutable.mp3"
    return PublicAsset(
        relative_path="media/episodes/episode/immutable.mp3",
        absolute_path=audio_path,
        public_url="https://podcast.example.test/media/episodes/episode/immutable.mp3",
        sha256="a" * 64,
        byte_size=audio_path.stat().st_size,
    )


def test_netease_playwright_publisher_uploads_immutable_audio_and_metadata(tmp_path: Path) -> None:
    """A normal creator session uploads the final MP3 and records the remote identity."""
    page = _FakeNetEasePage()
    publisher = _netease_publisher(tmp_path, page)

    result = asyncio.run(
        publisher.publish(_netease_episode(), _netease_target(), _immutable_asset(tmp_path))
    )

    assert result.status is PublicationTargetStatus.PUBLISHED
    assert result.remote_id == "remote-42"
    assert result.remote_url == "https://music.163.com/creatorcenter/voice/remote-42"
    assert ("upload", "input[type='file'][accept*='audio']:immutable.mp3") in page.actions
    assert ("fill", "input[placeholder*='标题']:今日 DailyCast") in page.actions


def test_netease_login_expiry_requires_human_action_before_upload(tmp_path: Path) -> None:
    """The adapter never attempts credentials or upload when a persistent login is absent."""
    page = _FakeNetEasePage(login_visible=True)
    publisher = _netease_publisher(tmp_path, page)

    with pytest.raises(PlatformNeedsAttentionError) as raised:
        asyncio.run(
            publisher.publish(_netease_episode(), _netease_target(), _immutable_asset(tmp_path))
        )

    assert raised.value.code == "NETEASE_LOGIN_EXPIRED"
    assert not [action for action in page.actions if action[0] == "upload"]


def test_netease_upload_failure_is_saved_for_manual_recovery(tmp_path: Path) -> None:
    """An RPA upload error never falls through as a fake success or a credential workaround."""
    page = _FakeNetEasePage(fail_upload=True)
    publisher = _netease_publisher(tmp_path, page)

    with pytest.raises(PlatformNeedsAttentionError) as raised:
        asyncio.run(
            publisher.publish(_netease_episode(), _netease_target(), _immutable_asset(tmp_path))
        )

    assert raised.value.code == "NETEASE_UPLOAD_FAILED"
    assert [action for action in page.actions if action[0] == "upload"]


def test_netease_resume_retries_the_same_target_after_login_is_restored(tmp_path: Path) -> None:
    """Resuming does not regenerate audio; it only replays the target upload with the same row."""
    page = _FakeNetEasePage()
    publisher = _netease_publisher(tmp_path, page)
    target = _netease_target()

    result = asyncio.run(publisher.resume(_netease_episode(), target, _immutable_asset(tmp_path)))

    assert result.status is PublicationTargetStatus.PUBLISHED
    assert target.id == 7


def test_dispatcher_persists_rss_success_and_netease_attention_independently(
    app_config_path: Path, tmp_path: Path
) -> None:
    """One target's human action cannot invalidate an already published RSS result or Episode."""
    factory = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)

    class FakeRSSPublisher:
        platform_name = PublicationPlatform.RSS

        async def validate(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> None:
            del episode, target, asset

        async def publish(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            mp3 = tmp_path / "immutable.mp3"
            mp3.write_bytes(b"rss")
            return PlatformPublishResult(
                status=PublicationTargetStatus.PUBLISHED,
                asset=PublicAsset(
                    relative_path="media/episodes/test/immutable.mp3",
                    absolute_path=mp3,
                    public_url="https://podcast.example.test/media/episodes/test/immutable.mp3",
                    sha256="b" * 64,
                    byte_size=3,
                ),
            )

        async def check_status(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

        async def resume(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

    class LoginRequiredNetEasePublisher:
        platform_name = PublicationPlatform.NETEASE

        async def validate(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> None:
            del episode, target, asset

        async def publish(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            raise PlatformNeedsAttentionError("NETEASE_LOGIN_EXPIRED", "login required")

        async def check_status(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

        async def resume(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).create(
                public_id="aaaaaaaa-0000-0000-0000-000000000010",
                episode_date=datetime.now(UTC).date(),
                status=EpisodeStatus.APPROVED,
            )

        result = asyncio.run(
            PublicationDispatcher(
                factory,
                (cast(object, FakeRSSPublisher()), cast(object, LoginRequiredNetEasePublisher())),
            ).publish(episode.id)
        )

        assert result.target_statuses == {"rss": "published", "netease": "needs_attention"}
        assert result.warning_count == 1
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            targets = PublicationTargetRepository(unit.session)
            assert (
                targets.get_by_platform(episode.id, PublicationPlatform.RSS).status
                is PublicationTargetStatus.PUBLISHED
            )
            netease = targets.get_by_platform(episode.id, PublicationPlatform.NETEASE)
            assert netease is not None
            assert netease.status is PublicationTargetStatus.NEEDS_ATTENTION
            assert netease.last_error == "NETEASE_LOGIN_EXPIRED: login required"
            current_episode = EpisodeRepository(unit.session).get(episode.id)
            assert current_episode is not None
            assert current_episode.status is EpisodeStatus.APPROVED
    finally:
        factory.kw["bind"].dispose()


def test_dispatcher_does_not_republish_or_recheck_a_durable_published_target(
    app_config_path: Path, tmp_path: Path
) -> None:
    """A retry preserves an already published target without another remote platform call."""
    factory = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)

    class CountingRSSPublisher:
        platform_name = PublicationPlatform.RSS

        def __init__(self) -> None:
            self.publish_calls = 0
            self.check_calls = 0

        async def validate(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> None:
            del episode, target, asset

        async def publish(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            self.publish_calls += 1
            audio = tmp_path / "immutable.mp3"
            audio.write_bytes(b"rss")
            return PlatformPublishResult(
                status=PublicationTargetStatus.PUBLISHED,
                remote_id="episode-guid",
                remote_url="https://podcast.example.test/feed.xml",
                asset=PublicAsset(
                    relative_path="media/episodes/test/immutable.mp3",
                    absolute_path=audio,
                    public_url="https://podcast.example.test/media/episodes/test/immutable.mp3",
                    sha256="b" * 64,
                    byte_size=3,
                ),
            )

        async def check_status(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            self.check_calls += 1
            raise AssertionError("a published target must not be remotely rechecked during retry")

        async def resume(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).create(
                public_id="aaaaaaaa-0000-0000-0000-000000000011",
                episode_date=datetime.now(UTC).date(),
                status=EpisodeStatus.APPROVED,
            )

        rss = CountingRSSPublisher()
        dispatcher = PublicationDispatcher(factory, (cast(object, rss),))
        asyncio.run(dispatcher.publish(episode.id))
        retry = asyncio.run(dispatcher.publish(episode.id))

        assert rss.publish_calls == 1
        assert rss.check_calls == 0
        assert retry.target_statuses == {"rss": "published"}
    finally:
        factory.kw["bind"].dispose()


def test_dispatcher_resume_retries_only_the_attention_target(app_config_path: Path) -> None:
    """Manual login recovery invokes only the requested platform, not episode generation."""
    factory = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)

    class ResumableNetEasePublisher:
        platform_name = PublicationPlatform.NETEASE

        def __init__(self) -> None:
            self.resume_calls = 0

        async def validate(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> None:
            del episode, target, asset

        async def publish(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            raise AssertionError("resume must not restart an initial publish")

        async def check_status(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            raise AssertionError("resume must not reconcile unrelated target state")

        async def resume(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            self.resume_calls += 1
            return PlatformPublishResult(
                status=PublicationTargetStatus.PUBLISHED,
                remote_id="netease-episode-1",
                remote_url="https://music.163.com/creatorcenter/voice/netease-episode-1",
            )

    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).create(
                public_id="aaaaaaaa-0000-0000-0000-000000000012",
                episode_date=datetime.now(UTC).date(),
                status=EpisodeStatus.PUBLISHED,
            )
            PublicationTargetRepository(unit.session).create(
                episode_id=episode.id,
                platform=PublicationPlatform.NETEASE,
                status=PublicationTargetStatus.NEEDS_ATTENTION,
                last_error="NETEASE_LOGIN_EXPIRED: login required",
            )

        netease = ResumableNetEasePublisher()
        result = asyncio.run(
            PublicationDispatcher(factory, (cast(object, netease),)).resume(
                episode.id, PublicationPlatform.NETEASE
            )
        )

        assert result.target_statuses == {"netease": "published"}
        assert netease.resume_calls == 1
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            target = PublicationTargetRepository(unit.session).get_by_platform(
                episode.id, PublicationPlatform.NETEASE
            )
            assert target is not None
            assert target.status is PublicationTargetStatus.PUBLISHED
            assert target.attempt_count == 1
            assert target.remote_id == "netease-episode-1"
    finally:
        factory.kw["bind"].dispose()


def test_dispatcher_persists_rss_failure_and_propagates_the_episode_error(
    app_config_path: Path,
) -> None:
    """An RSS failure stays an Episode-level error after its FAILED target row is persisted."""
    factory = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)

    class FailingRSSPublisher:
        platform_name = PublicationPlatform.RSS

        async def validate(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> None:
            del episode, target, asset

        async def publish(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            raise PublicationOperationError("RSS asset promotion or Feed publication failed")

        async def check_status(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            raise AssertionError("a failed publish must not be reconciled during the same call")

        async def resume(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

    class RecordingNetEasePublisher:
        platform_name = PublicationPlatform.NETEASE

        def __init__(self) -> None:
            self.publish_calls = 0

        async def validate(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> None:
            del episode, target, asset

        async def publish(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            del episode, target, asset
            self.publish_calls += 1
            return PlatformPublishResult(status=PublicationTargetStatus.PUBLISHED)

        async def check_status(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

        async def resume(
            self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
        ) -> PlatformPublishResult:
            return await self.publish(episode, target, asset)

    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).create(
                public_id="aaaaaaaa-0000-0000-0000-000000000013",
                episode_date=datetime.now(UTC).date(),
                status=EpisodeStatus.APPROVED,
            )

        netease = RecordingNetEasePublisher()
        with pytest.raises(PublicationOperationError):
            asyncio.run(
                PublicationDispatcher(
                    factory,
                    (cast(object, FailingRSSPublisher()), cast(object, netease)),
                ).publish(episode.id)
            )

        # External targets upload the immutable RSS asset, so they are skipped once
        # the source of truth itself failed instead of publishing stale state.
        assert netease.publish_calls == 0
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            targets = PublicationTargetRepository(unit.session)
            target = targets.get_by_platform(episode.id, PublicationPlatform.RSS)
            assert target is not None
            assert target.status is PublicationTargetStatus.FAILED
            assert target.attempt_count == 1
            assert target.last_error is not None
            assert target.last_error.startswith("PUBLICATION_FAILED")
            assert targets.get_by_platform(episode.id, PublicationPlatform.NETEASE) is None
            current_episode = EpisodeRepository(unit.session).get(episode.id)
            assert current_episode is not None
            assert current_episode.status is EpisodeStatus.APPROVED
    finally:
        factory.kw["bind"].dispose()


def test_distribution_resume_endpoint_is_wired_and_reports_unknown_targets(
    app_config_path: Path,
) -> None:
    """The manual resume route distinguishes disabled publishers from unknown platforms."""
    factory = __import__(
        "editorial_test_support", fromlist=["upgraded_session_factory"]
    ).upgraded_session_factory(app_config_path)

    try:
        with UnitOfWork(factory) as unit:
            assert unit.session is not None
            episode = EpisodeRepository(unit.session).create(
                public_id="aaaaaaaa-0000-0000-0000-000000000014",
                episode_date=datetime.now(UTC).date(),
                status=EpisodeStatus.PUBLISHED,
            )

        with TestClient(create_app(config_path=app_config_path)) as client:
            disabled = client.post(f"/distribution/episodes/{episode.id}/targets/netease/resume")
            unknown = client.post(f"/distribution/episodes/{episode.id}/targets/fediverse/resume")
            missing = client.post("/distribution/episodes/999999/targets/rss/resume")

        assert disabled.status_code == 404
        assert disabled.json()["detail"] == "publisher netease is not enabled"
        assert unknown.status_code == 422
        assert missing.status_code == 404
    finally:
        factory.kw["bind"].dispose()
