"""Daily text briefing schema, prompt, rendering, push, and configuration tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from dailycast.briefing.prompt import build_briefing_messages
from dailycast.briefing.renderer import RENDER_BYTE_BUDGET, render_briefing, truncate_markdown
from dailycast.briefing.schemas import BriefingEvidence, BriefingItem, BriefingResult
from dailycast.briefing.wecom import WeComNotifier, WeComPushError
from dailycast.core.config import BriefingSettings, load_settings

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"


def _evidence(
    source_url: str = "https://news.example.test/a",
    source_name: str = "量子位",
    title: str = "某 AI 公司发布新模型",
) -> BriefingEvidence:
    return BriefingEvidence(
        title=title,
        source_name=source_name,
        published_at=datetime(2026, 8, 19, 10, tzinfo=UTC),
        excerpt="这是正文摘录。",
        source_url=source_url,
    )


def _item(source_url: str, source_name: str = "量子位") -> BriefingItem:
    return BriefingItem(
        headline="头条一句话",
        summary="第一句摘要。第二句摘要。",
        source_name=source_name,
        source_url=source_url,
    )


def test_briefing_result_accepts_a_valid_payload() -> None:
    """The LLM output contract keeps the fields the deterministic renderer needs."""
    result = BriefingResult.model_validate(
        {
            "overview": "今天 AI 行业动态平稳。",
            "items": [_item("https://news.example.test/a").model_dump()],
        }
    )

    assert result.overview == "今天 AI 行业动态平稳。"
    assert len(result.items) == 1


def test_briefing_result_rejects_extra_fields() -> None:
    """Extra keys hint at a schema drift between prompt and provider output."""
    with pytest.raises(ValueError):
        BriefingResult.model_validate({"overview": "概览。", "items": [], "unexpected": 1})


def test_briefing_result_caps_items_at_twelve() -> None:
    """The item cap keeps one briefing inside the WeCom markdown byte budget."""
    items = [_item(f"https://news.example.test/{index}").model_dump() for index in range(13)]
    with pytest.raises(ValueError):
        BriefingResult.model_validate({"overview": "概览。", "items": items})


def test_briefing_item_requires_an_absolute_http_source_url() -> None:
    """A non-web or empty link must never reach a WeCom message."""
    with pytest.raises(ValueError, match="source_url"):
        _item("ftp://news.example.test/a")
    with pytest.raises(ValueError):
        _item("")
    with pytest.raises(ValueError, match="source_url"):
        _item("news.example.test/a")


def test_renderer_uses_the_configured_title_and_numbered_list() -> None:
    """The deterministic layout keeps the daily heading and evidence link format stable."""
    markdown = render_briefing(
        "通信行业日报",
        date(2026, 8, 20),
        BriefingResult(overview="概览一句话。", items=[_item("https://news.example.test/a")]),
        [_evidence()],
    )

    assert markdown.startswith("# 通信行业日报 8月20日\n")
    assert "概览一句话。" in markdown
    expected_item = (
        "1. **头条一句话** — 第一句摘要。第二句摘要。 [量子位](https://news.example.test/a)"
    )
    assert expected_item in markdown


def test_renderer_replaces_a_hallucinated_url_with_the_evidence_url() -> None:
    """A link the LLM invented falls back to the matching source's real evidence URL."""
    markdown = render_briefing(
        "AI 动态日报",
        date(2026, 8, 20),
        BriefingResult(
            overview="概览。",
            items=[_item("https://fabricated.example.test/x", source_name="量子位")],
        ),
        [_evidence(source_url="https://news.example.test/real")],
    )

    assert "https://news.example.test/real" in markdown
    assert "fabricated.example.test" not in markdown


def test_renderer_drops_items_that_match_no_evidence() -> None:
    """An item matching neither URL nor source name is unverifiable and must be dropped."""
    markdown = render_briefing(
        "AI 动态日报",
        date(2026, 8, 20),
        BriefingResult(
            overview="概览。",
            items=[_item("https://fabricated.example.test/x", source_name="不存在的来源")],
        ),
        [_evidence()],
    )

    assert "**头条一句话**" not in markdown


def test_renderer_deduplicates_items_sharing_one_evidence_url() -> None:
    """The same article must not appear twice even if the LLM repeats it."""
    items = [_item("https://news.example.test/a"), _item("https://news.example.test/a")]
    markdown = render_briefing(
        "AI 动态日报",
        date(2026, 8, 20),
        BriefingResult(overview="概览。", items=items),
        [_evidence()],
    )

    assert markdown.count("https://news.example.test/a") == 1


def test_truncate_markdown_keeps_short_content_unchanged() -> None:
    """Content already inside the byte budget ships without any marker."""
    assert truncate_markdown("# 标题\n正文\n") == "# 标题\n正文\n"


def test_truncate_markdown_respects_the_utf8_byte_budget() -> None:
    """The cap never splits a multibyte character and always stays below the budget."""
    content = "# 日报\n" + "这是一条很长的新闻摘要。" * 400

    truncated = truncate_markdown(content)

    assert len(truncated.encode("utf-8")) <= RENDER_BYTE_BUDGET
    assert truncated.endswith("…（内容过长，已截断）")


def test_prompt_carries_bounded_evidence_and_forbids_invented_urls() -> None:
    """The prompt is the only evidence channel, so it must pin links to the evidence."""
    messages = build_briefing_messages("通信行业日报", [_evidence()])

    assert messages[0].role == "system"
    user_content = messages[-1].content
    assert "https://news.example.test/a" in user_content
    assert "这是正文摘录。" in user_content
    assert "不得修改、拼接或编造" in user_content


def test_wecom_notifier_posts_the_markdown_payload_shape() -> None:
    """WeCom group robots accept only the documented markdown message envelope."""
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WeComNotifier(WEBHOOK_URL, client=client)

    asyncio.run(notifier.push("# 日报"))

    assert requests == [{"msgtype": "markdown", "markdown": {"content": "# 日报"}}]


def test_wecom_notifier_raises_on_a_non_zero_errcode() -> None:
    """A rejected webhook message must surface as a push failure, not silent loss."""
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WeComNotifier(WEBHOOK_URL, client=client)

    with pytest.raises(WeComPushError, match="errcode=93000"):
        asyncio.run(notifier.push("# 日报"))
    assert attempts == 2


def test_wecom_notifier_retries_once_and_then_succeeds() -> None:
    """One transient failure must not lose the whole briefing push."""
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"errcode": 500, "errmsg": "server error"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WeComNotifier(WEBHOOK_URL, client=client)

    asyncio.run(notifier.push("# 日报"))

    assert attempts == 2


def test_briefing_settings_default_to_disabled() -> None:
    """The briefing flow is strictly opt-in per deployment."""
    settings = BriefingSettings()

    assert settings.enabled is False
    assert settings.wecom_enabled is False
    assert settings.window_hours == 24
    assert settings.cron_expression == "30 7 * * *"


def test_wecom_enabled_requires_a_webhook_url() -> None:
    """A WeCom target without a webhook URL must fail at configuration load."""
    with pytest.raises(ValueError, match="wecom_webhook_url"):
        BriefingSettings(wecom_enabled=True)
    with pytest.raises(ValueError, match="wecom_webhook_url"):
        BriefingSettings(wecom_enabled=True, wecom_webhook_url="")


def test_briefing_settings_load_from_yaml(app_config_path: Path, tmp_path: Path) -> None:
    """The nested briefing block flows through the existing YAML settings source."""
    app_config_path.write_text(
        app_config_path.read_text(encoding="utf-8")
        + "briefing:\n"
        + "  enabled: true\n"
        + "  window_hours: 12\n"
        + "  wecom_enabled: true\n"
        + f"  wecom_webhook_url: {WEBHOOK_URL}\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=app_config_path, env_file=tmp_path / "absent.env")

    assert settings.briefing.enabled is True
    assert settings.briefing.window_hours == 12
    assert settings.briefing.wecom_webhook_url == WEBHOOK_URL
