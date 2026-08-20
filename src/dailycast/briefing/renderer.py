"""Deterministic markdown rendering with evidence-backed source links."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from dailycast.briefing.schemas import BriefingEvidence, BriefingItem, BriefingResult

WECOM_MARKDOWN_MAX_BYTES = 4096
RENDER_BYTE_BUDGET = 4000
_TRUNCATION_SUFFIX = "\n…（内容过长，已截断）"


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
    lines = [
        f"# {category_title} {briefing_date.month}月{briefing_date.day}日",
        "",
        result.overview.strip(),
        "",
    ]
    seen_urls: set[str] = set()
    number = 0
    for item in result.items:
        url = _resolve_url(item, urls_in_evidence, fallback_url_by_source)
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        number += 1
        # One item per block: WeCom renders each line separately, so a single
        # long "headline — summary — link" line becomes an unreadable wall.
        lines.append(f"**{number}. {item.headline.strip()}**")
        lines.append(item.summary.strip())
        lines.append(f"[{item.source_name.strip()}]({url})")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


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


def _resolve_url(
    item: BriefingItem,
    urls_in_evidence: set[str],
    fallback_url_by_source: dict[str, str],
) -> str | None:
    """Map one LLM item back to an evidence URL, or reject it as unverifiable."""
    if item.source_url in urls_in_evidence:
        return item.source_url
    return fallback_url_by_source.get(item.source_name)
