"""Playwright-only publisher for the NetEase Cloud Music creator backend."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, cast

from dailycast.core.errors import DailyCastError
from dailycast.db.models import (
    Episode,
    PublicationPlatform,
    PublicationTarget,
    PublicationTargetStatus,
)
from dailycast.publishing.contracts import PlatformPublishResult, PublicAsset
from dailycast.publishing.dispatcher import PlatformNeedsAttentionError


class NetEasePublicationError(DailyCastError):
    """A non-interactive NetEase upload or creator-backend operation failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=502, retryable=True)


class NetEasePage(Protocol):
    """The small Playwright page surface required by the publisher and deterministic tests."""

    async def goto(self, url: str) -> None:
        """Navigate to a creator-center URL."""

    async def is_visible(self, selector: str) -> bool:
        """Return whether a selector is currently visible."""

    async def click(self, selector: str) -> None:
        """Click a visible selector."""

    async def set_input_files(self, selector: str, path: Path) -> None:
        """Set one local file input without using a shell or desktop automation."""

    async def fill(self, selector: str, value: str) -> None:
        """Fill one text field."""

    async def wait_for_visible(self, selector: str, timeout_ms: int) -> None:
        """Wait for a publish-state selector."""

    async def current_url(self) -> str:
        """Return the final browser URL after a creator action."""

    async def attribute(self, selector: str, name: str) -> str | None:
        """Read a non-secret DOM attribute from a verified publication element."""


class NetEaseBrowserSession(Protocol):
    """An async persistent-browser session; production uses Playwright and tests inject a fake."""

    async def __aenter__(self) -> NetEasePage:
        """Open a persistent browser context and return its page adapter."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Close browser resources without suppressing publisher failures."""


@dataclass(frozen=True, slots=True)
class NetEaseSelectors:
    """One explicit selector set makes page-contract drift surface as a safe human action."""

    login: str = "text=登录"
    captcha: str = "text=/验证码|安全验证|滑块/"
    publish_entry: str = "text=发布声音"
    audio_upload: str = "input[type='file'][accept*='audio']"
    cover_upload: str = "input[type='file'][accept*='image']"
    title: str = "input[placeholder*='标题']"
    description: str = "textarea[placeholder*='介绍']"
    category: str = "text=选择分类"
    submit: str = "text=发布"
    success: str = "text=/发布成功|提交成功|审核中/"


@dataclass(frozen=True, slots=True)
class NetEasePublishingSettings:
    """Non-secret creator backend settings supplied by the DailyCast application configuration."""

    creator_url: str
    profile_dir: Path
    headless: bool
    cover_path: Path | None
    category: str


class _PlaywrightPage:
    """Concrete page adapter kept private so platform logic never sends direct HTTP requests."""

    def __init__(self, page: Any) -> None:
        self._page = page

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def is_visible(self, selector: str) -> bool:
        return bool(await self._page.locator(selector).is_visible())

    async def click(self, selector: str) -> None:
        await self._page.locator(selector).click()

    async def set_input_files(self, selector: str, path: Path) -> None:
        await self._page.locator(selector).set_input_files(str(path))

    async def fill(self, selector: str, value: str) -> None:
        await self._page.locator(selector).fill(value)

    async def wait_for_visible(self, selector: str, timeout_ms: int) -> None:
        await self._page.locator(selector).wait_for(state="visible", timeout=timeout_ms)

    async def current_url(self) -> str:
        return str(self._page.url)

    async def attribute(self, selector: str, name: str) -> str | None:
        value = await self._page.locator(selector).get_attribute(name)
        return cast(str | None, value)


class PlaywrightNetEaseBrowserSession:
    """Launch Chromium with a persistent profile that is rooted below private DATA_DIR."""

    def __init__(self, *, profile_dir: Path, headless: bool) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._playwright: Any | None = None
        self._context: Any | None = None

    async def __aenter__(self) -> NetEasePage:
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:  # pragma: no cover - packaging verifies the real dependency.
            raise NetEasePublicationError(
                "NETEASE_PLAYWRIGHT_UNAVAILABLE",
                "Playwright Chromium support is not installed in this deployment",
            ) from error
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=self._headless,
        )
        page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        return _PlaywrightPage(page)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exc_type, exc_value, traceback
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        return None


class NetEasePlaywrightPublisher:
    """Use a persistent Playwright browser to publish one immutable RSS asset to NetEase."""

    platform_name = PublicationPlatform.NETEASE

    def __init__(
        self,
        settings: NetEasePublishingSettings,
        *,
        selectors: NetEaseSelectors | None = None,
        browser_session_factory: Callable[[Path, bool], NetEaseBrowserSession] | None = None,
    ) -> None:
        self._settings = settings
        self._selectors = selectors or NetEaseSelectors()
        self._browser_session_factory = browser_session_factory or (
            lambda profile_dir, headless: PlaywrightNetEaseBrowserSession(
                profile_dir=profile_dir, headless=headless
            )
        )

    @property
    def profile_dir(self) -> Path:
        """Expose the resolved private profile root for deployment verification only."""
        return self._settings.profile_dir

    async def validate(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> None:
        """Require an immutable MP3, durable metadata, and a local non-secret cover asset."""
        del target
        if asset is None or not asset.absolute_path.is_file():
            raise NetEasePublicationError(
                "NETEASE_IMMUTABLE_ASSET_MISSING",
                "NetEase publishing requires the immutable RSS audio asset",
            )
        if not episode.title or not episode.description:
            raise NetEasePublicationError(
                "NETEASE_METADATA_MISSING",
                "NetEase publishing requires Episode title and description",
            )
        if self._settings.cover_path is None or not self._settings.cover_path.is_file():
            raise PlatformNeedsAttentionError(
                "NETEASE_COVER_REQUIRED",
                "configure a readable podcast cover image before NetEase publication",
            )

    async def publish(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Upload immutable audio and submit the creator form without bypassing account security."""
        await self.validate(episode, target, asset)
        assert asset is not None
        assert self._settings.cover_path is not None
        try:
            async with self._browser_session_factory(
                self._settings.profile_dir, self._settings.headless
            ) as page:
                await self._open_and_require_login(page)
                await page.click(self._selectors.publish_entry)
                await page.set_input_files(self._selectors.audio_upload, asset.absolute_path)
                await page.set_input_files(self._selectors.cover_upload, self._settings.cover_path)
                await page.fill(self._selectors.title, episode.title or "")
                await page.fill(self._selectors.description, episode.description or "")
                await page.click(self._selectors.category)
                await page.click(f"text={self._settings.category}")
                await self._raise_if_human_action(page)
                await page.click(self._selectors.submit)
                await page.wait_for_visible(self._selectors.success, timeout_ms=30_000)
                await self._raise_if_human_action(page)
                return PlatformPublishResult(
                    status=PublicationTargetStatus.PUBLISHED,
                    remote_id=await page.attribute(self._selectors.success, "data-id"),
                    remote_url=await page.current_url(),
                )
        except PlatformNeedsAttentionError:
            raise
        except NetEasePublicationError:
            raise
        except OSError as error:
            raise PlatformNeedsAttentionError(
                "NETEASE_UPLOAD_FAILED",
                "NetEase audio or cover upload failed; inspect the creator backend before resuming",
            ) from error
        except Exception as error:
            raise PlatformNeedsAttentionError(
                "NETEASE_PAGE_CHANGED",
                "NetEase creator page did not match the configured publish workflow",
            ) from error

    async def check_status(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Reconcile a target using its remote identity or its visible creator-history title."""
        del asset
        async with self._browser_session_factory(
            self._settings.profile_dir, self._settings.headless
        ) as page:
            await page.goto(self._settings.creator_url)
            await self._raise_if_human_action(page)
            if await page.is_visible(self._selectors.login):
                raise PlatformNeedsAttentionError(
                    "NETEASE_LOGIN_EXPIRED", "NetEase creator login must be completed manually"
                )
            if target.remote_id or target.remote_url:
                return PlatformPublishResult(
                    status=PublicationTargetStatus.PUBLISHED,
                    remote_id=target.remote_id,
                    remote_url=target.remote_url,
                )
            if episode.title and await page.is_visible(f"text={episode.title}"):
                return PlatformPublishResult(
                    status=PublicationTargetStatus.PUBLISHED,
                    remote_id=await page.attribute(f"text={episode.title}", "data-id"),
                    remote_url=await page.current_url(),
                )
            return PlatformPublishResult(status=PublicationTargetStatus.PENDING)

    async def resume(
        self, episode: Episode, target: PublicationTarget, asset: PublicAsset | None
    ) -> PlatformPublishResult:
        """Retry exactly the same target after a human restored login, cover, or page readiness."""
        return await self.publish(episode, target, asset)

    async def _open_and_require_login(self, page: NetEasePage) -> None:
        await page.goto(self._settings.creator_url)
        await self._raise_if_human_action(page)
        if await page.is_visible(self._selectors.login):
            raise PlatformNeedsAttentionError(
                "NETEASE_LOGIN_EXPIRED", "NetEase creator login must be completed manually"
            )
        if not await page.is_visible(self._selectors.publish_entry):
            raise PlatformNeedsAttentionError(
                "NETEASE_PAGE_CHANGED",
                "NetEase creator publish entry was not found; inspect selectors manually",
            )

    async def _raise_if_human_action(self, page: NetEasePage) -> None:
        if await page.is_visible(self._selectors.captcha):
            raise PlatformNeedsAttentionError(
                "NETEASE_CHALLENGE", "NetEase requested a CAPTCHA or safety verification"
            )
