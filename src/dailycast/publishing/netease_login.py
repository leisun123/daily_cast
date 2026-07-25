"""One-time operator command for establishing the official NetEase browser login."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from dailycast.core.config import load_settings
from dailycast.publishing.netease import (
    NetEasePublisherSettings,
    establish_netease_login,
)


def main() -> None:
    """Open a headed official creator session and save portable private login state."""
    settings = load_settings()
    netease = settings.publishing.netease
    private_root = settings.data_dir.resolve()
    profile_dir = _below_private_root(private_root, netease.profile_dir)
    state_path = _below_private_root(private_root, netease.storage_state_path)
    browser_settings = NetEasePublisherSettings(
        profile_dir=profile_dir,
        storage_state_path=state_path,
        creator_url=netease.creator_url,
        headless=False,
        category=netease.category,
        timeout_ms=int(netease.timeout_seconds * 1000),
    )
    print("Opening the official NetEase creator page. Complete login in Chromium.")
    asyncio.run(establish_netease_login(replace(browser_settings, headless=False)))
    print(f"NetEase login state saved privately at {state_path}")


def _below_private_root(private_root: Path, configured: Path) -> Path:
    resolved = (
        configured.resolve() if configured.is_absolute() else (private_root / configured).resolve()
    )
    try:
        resolved.relative_to(private_root)
    except ValueError as error:
        raise ValueError("NetEase login paths must stay below DATA_DIR") from error
    return resolved
