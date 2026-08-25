"""Deterministic markdown rendering with evidence-backed source links."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from dailycast.briefing.schemas import BriefingEvidence, BriefingItem, BriefingResult

WECOM_MARKDOWN_MAX_BYTES = 4096
RENDER_BYTE_BUDGET = 4000
_TRUNCATION_SUFFIX = "\n…（内容过长，已截断）"
_RSS_MIRROR_SUFFIX = re.compile(r"[（(]RSSHub(?:\s*镜像)?[）)]", flags=re.IGNORECASE)


def render_briefing(
    category_title: str,
    briefing_date: date,
    result: BriefingResult,
    evidence: Sequence[BriefingEvidence],
) -> str:
    """Render one briefing with links taken only from the collected evidence URLs.

    The LLM writes headlines and summaries, but the rendered link target is
    always validated against the evidence: a URL the LLM invented is replaced
    by the matching source's evidence URL, and an item that matches no evidence
    at all is dropped rather than shipped with a fabricated link.
    """
    urls_in_evidence = {entry.source_url for entry in evidence}
    fallback_url_by_source: dict[str, str] = {}
    for entry in evidence:
        fallback_url_by_source.setdefault(entry.source_name, entry.source_url)
    resolved_items: list[tuple[BriefingItem, str]] = []
    seen_urls: set[str] = set()
    for item in result.items:
        url = _resolve_url(item, urls_in_evidence, fallback_url_by_source)
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        resolved_items.append((item, url))
    accepted_blocks: list[list[str]] = []
    for item, url in resolved_items:
        number = len(accepted_blocks) + 1
        block = _item_block(number, item, url)
        candidate = _render_lines(
            category_title,
            briefing_date,
            result.overview,
            [*accepted_blocks, block],
        )
        if len(candidate.encode("utf-8")) <= RENDER_BYTE_BUDGET:
            accepted_blocks.append(block)
    return _render_lines(category_title, briefing_date, result.overview, accepted_blocks)


def render_merged_briefing(
    briefing_date: date,
    categories: Sequence[tuple[str, str, BriefingResult, Sequence[BriefingEvidence]]],
) -> str:
    """Render the one compact WeCom message from independently selected categories.

    Category-level markdown is kept for audit and retry state, but the group bot
    receives this single title-list message.  It deliberately leaves the verbose
    per-item prose out of the chat surface: each headline links to the already
    verified source page, while the two short category focus phrases form one
    scan-friendly summary line.
    """
    overview_parts: list[str] = []
    sections: list[tuple[str, list[tuple[BriefingItem, str]]]] = []
    for category_name, heading, result, evidence in categories:
        resolved_items = _resolved_items(result, evidence)
        if not resolved_items:
            continue
        overview_parts.append(f"{category_name}：{_compact_focus(result, resolved_items)}")
        sections.append((heading, resolved_items))

    lines = [
        f"# 【行业观察日报】{briefing_date.month}月{briefing_date.day}日",
        "",
        "> **今日关注**",
        f"> {'；'.join(overview_parts)}",
    ]
    number = 0
    for heading, items in sections:
        section_lines = ["", f"## {heading}"]
        if _fits(lines, section_lines):
            lines.extend(section_lines)
        for item, url in items:
            theme = item.theme.strip()
            theme_prefix = f"**{theme}｜** " if theme else ""
            item_lines = [f"{number + 1}. {theme_prefix}[{item.headline.strip()}]({url})"]
            if not _fits(lines, item_lines):
                continue
            lines.extend(item_lines)
            number += 1
    return "\n".join(lines).rstrip("\n") + "\n"


def _compact_focus(
    result: BriefingResult,
    items: Sequence[tuple[BriefingItem, str]],
) -> str:
    """Choose the co-generated focus, with short existing themes as a safe fallback."""
    if result.focus.strip():
        return result.focus.strip()
    themes: list[str] = []
    for item, _ in items:
        theme = item.theme.strip()
        if theme and theme not in themes:
            themes.append(theme)
        if len(themes) == 2:
            break
    return "、".join(themes) if themes else "重点动态"


def truncate_markdown(content: str, max_bytes: int = RENDER_BYTE_BUDGET) -> str:
    """Cap markdown at a UTF-8 byte budget without splitting multibyte characters.

    WeCom group bots reject markdown payloads above 4096 bytes, so the renderer
    keeps a safety margin and marks the cut explicitly instead of silently
    dropping the tail.
    """
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    budget = max_bytes - len(_TRUNCATION_SUFFIX.encode("utf-8"))
    text = encoded[:budget].decode("utf-8", errors="ignore")
    return text.rstrip("\n") + _TRUNCATION_SUFFIX


def _render_lines(
    category_title: str,
    briefing_date: date,
    overview: str,
    item_blocks: Sequence[Sequence[str]],
) -> str:
    """Render the header and complete item blocks with their final item count."""
    lines = [
        f"# {category_title}｜{briefing_date.month}月{briefing_date.day}日",
        f"*今日精选 · {len(item_blocks)} 条*",
        "",
        "> **今日要点**",
        f"> {overview.strip()}",
    ]
    for index, block in enumerate(item_blocks):
        if index:
            lines.extend(["", "---"])
        lines.extend(block)
    return "\n".join(lines).rstrip("\n") + "\n"


def _item_block(number: int, item: BriefingItem, url: str) -> list[str]:
    """Render one evidence-backed item without creating a partial-message risk."""
    source_name = _display_source_name(item.source_name)
    return [
        "",
        f"## {number:02d}｜{item.headline.strip()}",
        "",
        "> **发生了什么**",
        f"> {item.summary.strip()}",
        "",
        "> **为什么值得看**",
        f"> {item.why_it_matters.strip()}",
        "",
        f"[{source_name} · 阅读原文 ↗]({url})",
    ]


def _resolved_items(
    result: BriefingResult, evidence: Sequence[BriefingEvidence]
) -> list[tuple[BriefingItem, str]]:
    """Keep each item on an original, verified reader-facing URL exactly once."""
    urls_in_evidence = {entry.source_url for entry in evidence}
    fallback_url_by_source: dict[str, str] = {}
    for entry in evidence:
        fallback_url_by_source.setdefault(entry.source_name, entry.source_url)
    resolved_items: list[tuple[BriefingItem, str]] = []
    seen_urls: set[str] = set()
    for item in result.items:
        url = _resolve_url(item, urls_in_evidence, fallback_url_by_source)
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        resolved_items.append((item, url))
    return resolved_items


def _fits(existing_lines: Sequence[str], addition: Sequence[str]) -> bool:
    """Accept only complete heading or item blocks inside the WeCom byte budget."""
    candidate = "\n".join([*existing_lines, *addition]).rstrip("\n") + "\n"
    return len(candidate.encode("utf-8")) <= RENDER_BYTE_BUDGET


def _resolve_url(
    item: BriefingItem,
    urls_in_evidence: set[str],
    fallback_url_by_source: dict[str, str],
) -> str | None:
    """Map one LLM item back to an evidence URL, or reject it as unverifiable."""
    if item.source_url in urls_in_evidence:
        return item.source_url
    return fallback_url_by_source.get(item.source_name)


def _display_source_name(source_name: str) -> str:
    """Hide feed-delivery plumbing while keeping the reader-facing publisher name."""
    cleaned = _RSS_MIRROR_SUFFIX.sub("", source_name).strip(" -—·")
    return cleaned or source_name.strip()
