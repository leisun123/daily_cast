"""Official-page Playwright automation for NetEase Cloud Music podcast publishing."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session, sessionmaker

from dailycast.core.hashes import sha256_bytes
from dailycast.db.models import Episode, PublicationPlatform, PublicationTarget
from dailycast.db.repositories import PublicationRepository
from dailycast.db.transactions import UnitOfWork
from dailycast.publishing.contracts import (
    PlatformPublishResult,
    PublisherError,
    PublisherNeedsAttentionError,
)


@dataclass(frozen=True, slots=True)
class NetEasePublisherSettings:
    """Non-secret browser and episode metadata settings for one NetEase account."""

    profile_dir: Path
    storage_state_path: Path | None = None
    creator_url: str = "https://musicupload.netease.com/"
    headless: bool = True
    category: str = "科技"
    cover_path: Path | None = None
    timeout_ms: int = 120_000


class NetEaseBrowser(Protocol):
    """Small browser boundary that keeps platform behavior unit-testable."""

    async def open(self, url: str) -> None: ...

    async def is_authenticated(self) -> bool: ...

    async def has_human_verification(self) -> bool: ...

    async def find_existing(self, title: str) -> PlatformPublishResult | None: ...

    async def upload_audio(self, path: Path) -> None: ...

    async def fill_metadata(
        self,
        *,
        title: str,
        description: str,
        category: str,
        cover_path: Path | None,
    ) -> None: ...

    async def submit(self) -> PlatformPublishResult: ...


class NetEaseLoginBrowser(NetEaseBrowser, Protocol):
    """Operator-only capabilities for establishing the initial browser session."""

    async def wait_until_authenticated(self, timeout_ms: int) -> None: ...

    async def save_storage_state(self) -> None: ...


BrowserFactory = Callable[
    [NetEasePublisherSettings],
    AbstractAsyncContextManager[NetEaseBrowser],
]
LoginBrowserFactory = Callable[
    [NetEasePublisherSettings],
    AbstractAsyncContextManager[NetEaseLoginBrowser],
]


class NetEasePageChangedError(RuntimeError):
    """Expected creator controls no longer match the official page."""


class NetEasePlaywrightPublisher:
    """Upload immutable Episode assets through the official creator web interface only."""

    platform_name = PublicationPlatform.NETEASE

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        public_dir: Path,
        settings: NetEasePublisherSettings,
        browser_factory: BrowserFactory | None = None,
    ) -> None:
        if settings.timeout_ms <= 0:
            raise ValueError("NetEase browser timeout must be positive")
        if not settings.creator_url.startswith("https://"):
            raise ValueError("NetEase creator URL must use HTTPS")
        self._session_factory = session_factory
        self._public_dir = public_dir.resolve()
        self._settings = settings
        self._browser_factory = browser_factory or _default_browser_factory

    async def validate(self, episode: Episode) -> None:
        """Require complete metadata and the already published immutable RSS asset."""
        if not episode.title or not episode.description:
            raise PublisherError("NETEASE_METADATA_MISSING")
        self._immutable_asset(episode.id)
        cover_path = self._settings.cover_path
        if cover_path is not None and not cover_path.is_file():
            raise PublisherError("NETEASE_COVER_MISSING")

    async def publish(self, episode: Episode) -> PlatformPublishResult:
        """Publish once, or return the existing remote program after a crash."""
        return await self._publish_or_recover(episode)

    async def check_status(
        self, episode: Episode, target: PublicationTarget
    ) -> PlatformPublishResult:
        """Trust a persisted remote identity or inspect the creator page by exact title."""
        if target.remote_id is not None:
            return PlatformPublishResult(
                remote_id=target.remote_id,
                remote_url=target.remote_url,
            )
        return await self._find_or_require_attention(episode)

    async def resume(self, episode: Episode, target: PublicationTarget) -> PlatformPublishResult:
        """Resume only NetEase and search before upload to prevent duplicate programs."""
        del target
        return await self._publish_or_recover(episode)

    async def _publish_or_recover(self, episode: Episode) -> PlatformPublishResult:
        await self.validate(episode)
        asset_path = self._immutable_asset(episode.id)
        self._settings.profile_dir.mkdir(parents=True, exist_ok=True)
        async with self._browser_factory(self._settings) as browser:
            await browser.open(self._settings.creator_url)
            await _require_human_safe_session(browser)
            try:
                existing = await browser.find_existing(str(episode.title))
            except NetEasePageChangedError as error:
                raise PublisherNeedsAttentionError("NETEASE_PAGE_CHANGED") from error
            if existing is not None:
                return existing
            try:
                await browser.upload_audio(asset_path)
            except NetEasePageChangedError as error:
                raise PublisherNeedsAttentionError("NETEASE_PAGE_CHANGED") from error
            except Exception as error:
                raise PublisherError("NETEASE_UPLOAD_FAILED") from error
            try:
                await browser.fill_metadata(
                    title=str(episode.title),
                    description=str(episode.description),
                    category=self._settings.category,
                    cover_path=self._settings.cover_path,
                )
                if await browser.has_human_verification():
                    raise PublisherNeedsAttentionError("NETEASE_HUMAN_VERIFICATION_REQUIRED")
                result = await browser.submit()
                if not result.remote_id:
                    raise PublisherNeedsAttentionError("NETEASE_REMOTE_STATUS_UNKNOWN")
            except PublisherNeedsAttentionError:
                raise
            except NetEasePageChangedError as error:
                raise PublisherNeedsAttentionError("NETEASE_PAGE_CHANGED") from error
            except Exception as error:
                raise PublisherError("NETEASE_SUBMIT_FAILED") from error
            return PlatformPublishResult(
                remote_id=result.remote_id,
                remote_url=result.remote_url,
            )

    async def _find_or_require_attention(self, episode: Episode) -> PlatformPublishResult:
        self._settings.profile_dir.mkdir(parents=True, exist_ok=True)
        async with self._browser_factory(self._settings) as browser:
            await browser.open(self._settings.creator_url)
            await _require_human_safe_session(browser)
            existing = await browser.find_existing(str(episode.title))
            if existing is None:
                raise PublisherNeedsAttentionError("NETEASE_REMOTE_STATUS_UNKNOWN")
            return existing

    def _immutable_asset(self, episode_id: int) -> Path:
        with UnitOfWork(self._session_factory) as unit:
            assert unit.session is not None
            publication = PublicationRepository(unit.session).get_published_for_episode(episode_id)
            if (
                publication is None
                or publication.public_asset_path is None
                or publication.asset_sha256 is None
                or publication.asset_byte_size is None
            ):
                raise PublisherError("NETEASE_IMMUTABLE_ASSET_MISSING")
            relative_path = Path(publication.public_asset_path)
            if relative_path.is_absolute():
                raise PublisherError("NETEASE_IMMUTABLE_ASSET_INVALID")
            asset_path = (self._public_dir / relative_path).resolve()
            try:
                asset_path.relative_to(self._public_dir)
            except ValueError as error:
                raise PublisherError("NETEASE_IMMUTABLE_ASSET_INVALID") from error
            if (
                not asset_path.is_file()
                or asset_path.stat().st_size != publication.asset_byte_size
                or sha256_bytes(asset_path.read_bytes()) != publication.asset_sha256
            ):
                raise PublisherError("NETEASE_IMMUTABLE_ASSET_INVALID")
            return asset_path


async def _require_human_safe_session(browser: NetEaseBrowser) -> None:
    if await browser.has_human_verification():
        raise PublisherNeedsAttentionError("NETEASE_HUMAN_VERIFICATION_REQUIRED")
    if not await browser.is_authenticated():
        raise PublisherNeedsAttentionError("NETEASE_LOGIN_REQUIRED")


def _default_browser_factory(
    settings: NetEasePublisherSettings,
) -> AbstractAsyncContextManager[NetEaseBrowser]:
    return PlaywrightNetEaseBrowser(settings)


async def establish_netease_login(
    settings: NetEasePublisherSettings,
    *,
    browser_factory: LoginBrowserFactory | None = None,
) -> None:
    """Keep the official creator page open for one manual login and save private state."""
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    factory = browser_factory or _default_login_browser_factory
    async with factory(settings) as browser:
        await browser.open(settings.creator_url)
        if not await browser.is_authenticated():
            await browser.wait_until_authenticated(settings.timeout_ms)
        if await browser.has_human_verification():
            raise PublisherNeedsAttentionError("NETEASE_HUMAN_VERIFICATION_REQUIRED")
        await browser.save_storage_state()


def _default_login_browser_factory(
    settings: NetEasePublisherSettings,
) -> AbstractAsyncContextManager[NetEaseLoginBrowser]:
    return PlaywrightNetEaseBrowser(settings)


class PlaywrightNetEaseBrowser:
    """Persistent Chromium session using only the supported Playwright browser API."""

    def __init__(self, settings: NetEasePublisherSettings) -> None:
        self._settings = settings
        self._manager: Any = None
        self._context: Any = None
        self._page: Any = None

    async def __aenter__(self) -> PlaywrightNetEaseBrowser:
        from playwright.async_api import async_playwright

        self._manager = await async_playwright().start()
        self._context = await self._manager.chromium.launch_persistent_context(
            user_data_dir=str(self._settings.profile_dir),
            headless=self._settings.headless,
            timeout=self._settings.timeout_ms,
        )
        await self._restore_storage_state()
        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )
        self._page.set_default_timeout(self._settings.timeout_ms)
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._context is not None:
            await self._context.close()
        if self._manager is not None:
            await self._manager.stop()

    async def open(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def is_authenticated(self) -> bool:
        cookies = await self._context.cookies()
        return any(
            cookie.get("name") == "MUSIC_U" and bool(cookie.get("value")) for cookie in cookies
        )

    async def wait_until_authenticated(self, timeout_ms: int) -> None:
        """Wait for an operator to complete the official login without automating it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while loop.time() < deadline:
            if await self.is_authenticated():
                return
            await asyncio.sleep(1)
        raise PublisherNeedsAttentionError("NETEASE_LOGIN_TIMEOUT")

    async def save_storage_state(self) -> None:
        """Export portable authentication state beside the private persistent profile."""
        state_path = self._settings.storage_state_path
        if state_path is None:
            return
        state_path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(state_path))
        state_path.chmod(0o600)

    async def has_human_verification(self) -> bool:
        return await self._any_visible(
            (
                "text=请完成验证",
                "text=安全验证",
                "text=请输入验证码",
                "iframe[src*='captcha']",
            )
        )

    async def find_existing(self, title: str) -> PlatformPublishResult | None:
        locator = self._page.get_by_text(title, exact=True)
        if await locator.count() == 0:
            return None
        candidate = locator.first
        if not await candidate.is_visible():
            return None
        link = candidate.locator("xpath=ancestor-or-self::a[1]")
        href = await link.get_attribute("href") if await link.count() else None
        if href is None:
            return PlatformPublishResult(remote_id=title)
        remote_url = self._absolute_url(href)
        return PlatformPublishResult(
            remote_id=_remote_id(remote_url) or title,
            remote_url=remote_url,
        )

    async def upload_audio(self, path: Path) -> None:
        await self._click_first(
            (
                self._page.get_by_role("button", name=re.compile("发布播客|发布节目|上传作品")),
                self._page.get_by_text(re.compile("发布播客|发布节目|上传作品"), exact=True),
            )
        )
        file_input = self._page.locator(
            "input[type='file'][accept*='audio'], input[type='file']"
        ).first
        if await file_input.count() == 0:
            raise NetEasePageChangedError("audio file input was not found")
        await file_input.set_input_files(str(path))

    async def fill_metadata(
        self,
        *,
        title: str,
        description: str,
        category: str,
        cover_path: Path | None,
    ) -> None:
        title_input = self._page.locator(
            "input[name='title'], input[placeholder*='标题'], input[placeholder*='节目名称']"
        ).first
        description_input = self._page.locator(
            "textarea[name='description'], textarea[placeholder*='简介'], "
            "textarea[placeholder*='描述']"
        ).first
        if await title_input.count() == 0 or await description_input.count() == 0:
            raise NetEasePageChangedError("metadata controls were not found")
        await title_input.fill(title)
        await description_input.fill(description)
        category_control = self._page.get_by_text(category, exact=True)
        if await category_control.count() and await category_control.first.is_visible():
            await category_control.first.click()
        if cover_path is not None:
            cover_input = self._page.locator("input[type='file'][accept*='image']").first
            if await cover_input.count() == 0:
                raise NetEasePageChangedError("cover file input was not found")
            await cover_input.set_input_files(str(cover_path))

    async def submit(self) -> PlatformPublishResult:
        await self._click_first(
            (
                self._page.get_by_role("button", name=re.compile("提交发布|确认发布|发布")),
                self._page.get_by_text(re.compile("提交发布|确认发布"), exact=True),
            )
        )
        await self._page.wait_for_load_state("domcontentloaded")
        remote_url = str(self._page.url)
        return PlatformPublishResult(
            remote_id=_remote_id(remote_url),
            remote_url=remote_url,
        )

    async def _click_first(self, locators: tuple[Any, ...]) -> None:
        for locator in locators:
            if await locator.count() and await locator.first.is_visible():
                await locator.first.click()
                return
        raise NetEasePageChangedError("expected creator action was not found")

    async def _any_visible(self, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            locator = self._page.locator(selector)
            if await locator.count() and await locator.first.is_visible():
                return True
        return False

    async def _restore_storage_state(self) -> None:
        """Load only official NetEase cookies from the private portable state file."""
        state_path = self._settings.storage_state_path
        if state_path is None or not state_path.is_file():
            return
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            cookies = raw.get("cookies") if isinstance(raw, dict) else None
        except (OSError, json.JSONDecodeError) as error:
            raise PublisherNeedsAttentionError("NETEASE_STORAGE_STATE_INVALID") from error
        if not isinstance(cookies, list):
            raise PublisherNeedsAttentionError("NETEASE_STORAGE_STATE_INVALID")
        official_cookies: list[dict[str, Any]] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise PublisherNeedsAttentionError("NETEASE_STORAGE_STATE_INVALID")
            domain = cookie.get("domain")
            if not isinstance(domain, str) or not _is_official_cookie_domain(domain):
                continue
            official_cookies.append(cookie)
        if official_cookies:
            await self._context.add_cookies(official_cookies)

    def _absolute_url(self, href: str) -> str:
        if href.startswith(("http://", "https://")):
            return href
        parsed = urlparse(str(self._page.url))
        return f"{parsed.scheme}://{parsed.netloc}/{href.lstrip('/')}"


def _remote_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("id", "programId", "program_id"):
        values = query.get(key)
        if values:
            return values[0]
    path_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return path_id or None


def _is_official_cookie_domain(domain: str) -> bool:
    normalized = domain.lower().lstrip(".")
    return (
        normalized == "163.com"
        or normalized.endswith(".163.com")
        or normalized == "netease.com"
        or normalized.endswith(".netease.com")
    )
