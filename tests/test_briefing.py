"""Daily text briefing schema, prompt, rendering, push, and configuration tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from dailycast.briefing.prompt import build_briefing_messages, build_merged_focus_messages
from dailycast.briefing.renderer import (
    RENDER_BYTE_BUDGET,
    render_briefing,
    render_merged_briefing,
    truncate_markdown,
)
from dailycast.briefing.schemas import BriefingEvidence, BriefingItem, BriefingResult
from dailycast.briefing.selection import RankedBriefingEvidence
from dailycast.briefing.service import _interleave_by_source, latest_briefing_date
from dailycast.briefing.webhook import WebhookNotifier, WebhookPushError
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


def _ranked_evidence(evidence: BriefingEvidence) -> RankedBriefingEvidence:
    """Attach the fixed local decision the prompt must expose to the editor model."""
    return RankedBriefingEvidence(
        evidence=evidence,
        tier="P0",
        specificity=500,
        reason="中国移动直接动态",
        rule_id="telecom-china-mobile",
        source_id="mobile-source",
        source_priority=100,
        discovered_at=datetime(2026, 8, 21, 8, tzinfo=UTC),
        article_id=1,
    )


def _item(
    source_url: str,
    source_name: str = "量子位",
    *,
    summary: str = "第一句摘要。第二句摘要。",
    why_it_matters: str = "这会影响行业下一步的产品布局。",
) -> BriefingItem:
    return BriefingItem(
        headline="头条一句话",
        summary=summary,
        why_it_matters=why_it_matters,
        source_name=source_name,
        source_url=source_url,
    )


def test_merged_renderer_keeps_the_model_selected_theme_before_each_headline() -> None:
    """The compact group message retains the preview's scan-friendly topic tags."""
    telecom_url = "https://news.example.test/telecom"
    ai_url = "https://news.example.test/ai"
    telecom_item = _item(telecom_url).model_copy(update={"theme": "5G-A 场景"})
    ai_item = _item(ai_url).model_copy(update={"theme": "国产模型"})

    markdown = render_merged_briefing(
        date(2026, 8, 25),
        [
            (
                "通信",
                "📡 通信",
                BriefingResult(
                    overview="运营商推进5G-A网络建设。",
                    items=[telecom_item],
                ),
                [_evidence(source_url=telecom_url)],
            ),
            (
                "AI",
                "🤖 AI",
                BriefingResult(
                    overview="国产大模型加快商业化落地。",
                    items=[ai_item],
                ),
                [_evidence(source_url=ai_url)],
            ),
        ],
    )

    assert "1. **5G-A 场景｜** [头条一句话](https://news.example.test/telecom)" in markdown
    assert "2. **国产模型｜** [头条一句话](https://news.example.test/ai)" in markdown


def test_merged_renderer_uses_a_natural_yesterday_focus_sentence() -> None:
    """The focus stays readable when WeCom renders quoted Markdown V2 text."""
    telecom_url = "https://news.example.test/telecom"
    ai_url = "https://news.example.test/ai"
    telecom_result = BriefingResult(
        overview="运营商将算力投资、6G战略和网络智能化同步推进，并持续扩大行业场景覆盖。",
        items=[_item(telecom_url)],
    )
    ai_result = BriefingResult(
        overview="国内AI企业持续推进基础设施建设、端侧能力和智能体应用，产品迭代范围不断扩大。",
        items=[_item(ai_url)],
    )

    markdown = render_merged_briefing(
        date(2026, 8, 25),
        [
            ("通信", "📡 通信", telecom_result, [_evidence(source_url=telecom_url)]),
            ("AI", "🤖 AI", ai_result, [_evidence(source_url=ai_url)]),
        ],
        focus="运营商将算力投资与6G演进同步推进；国内 AI 侧，国产模型与智能体应用加速。",
    )

    assert (
        "## 昨日关注\n> 运营商将算力投资与6G演进同步推进；国内 AI 侧，国产模型与智能体应用加速"
        in markdown
    )
    assert "> **昨日关注：**" not in markdown
    assert "通信：" not in markdown
    assert "AI：" not in markdown
    assert "运营商将算力投资、6G战略和网络智能化同步推进" not in markdown
    assert "国内AI企业持续推进基础设施建设" not in markdown


def test_merged_focus_prompt_keeps_global_ai_scope_with_chinese_sources() -> None:
    """The lead must not relabel global AI stories as domestic AI events."""
    result = BriefingResult(
        overview="全球模型和基础设施继续演进。",
        items=[_item("https://news.example.test/global-ai")],
    )

    prompt = build_merged_focus_messages([("AI", result)])[-1].content

    assert "全球AI动态" in prompt
    assert "中文来源" in prompt
    assert "国内AI侧" not in prompt


def test_telecom_editorial_prompt_orders_local_management_events_before_national_news() -> None:
    """Regional priority must be explicit to the LLM, not inferred from candidate order."""
    prompt = build_briefing_messages(
        "通信行业日报",
        [_ranked_evidence(_evidence())],
        category="telecom",
        editorial_selection=True,
    )[-1].content

    assert "选择和输出都必须按地域落地价值排序" in prompt
    assert "先常州，再江苏省级，再其他地级市，最后才是全国性动态" in prompt
    assert "不得排在更具体的地方动态之前" in prompt


def test_merged_renderer_keeps_six_items_per_category_in_one_message() -> None:
    """The final WeCom artifact carries the agreed 6+6 stories without splitting."""
    telecom_urls = [f"https://telecom.example.test/{index}" for index in range(1, 7)]
    ai_urls = [f"https://ai.example.test/{index}" for index in range(1, 7)]
    telecom_items = [
        _item(url).model_copy(update={"headline": f"通信新闻第{index}条"})
        for index, url in enumerate(telecom_urls, start=1)
    ]
    ai_items = [
        _item(url).model_copy(update={"headline": f"AI新闻第{index}条"})
        for index, url in enumerate(ai_urls, start=1)
    ]

    markdown = render_merged_briefing(
        date(2026, 8, 25),
        [
            (
                "通信",
                "📡 通信",
                BriefingResult(overview="通信动态。", items=telecom_items),
                [_evidence(source_url=url) for url in telecom_urls],
            ),
            (
                "AI",
                "🤖 AI",
                BriefingResult(overview="AI动态。", items=ai_items),
                [_evidence(source_url=url) for url in ai_urls],
            ),
        ],
        focus="通信网络与人工智能产业均出现新的产品和建设进展。",
    )

    assert markdown.count("https://telecom.example.test/") == 6
    assert markdown.count("https://ai.example.test/") == 6
    assert "12. " in markdown
    assert len(markdown.encode("utf-8")) <= RENDER_BYTE_BUDGET


def test_briefing_result_accepts_a_valid_payload() -> None:
    """The LLM output contract keeps the fields the deterministic renderer needs."""
    result = BriefingResult.model_validate(
        {
            "overview": "今天 AI 行业动态平稳。",
            "items": [
                {
                    **_item("https://news.example.test/a").model_dump(),
                    "theme": "国产模型",
                }
            ],
        }
    )

    assert result.overview == "今天 AI 行业动态平稳。"
    assert result.items[0].theme == "国产模型"
    assert len(result.items) == 1


def test_briefing_result_rejects_extra_fields() -> None:
    """Extra keys hint at a schema drift between prompt and provider output."""
    with pytest.raises(ValueError):
        BriefingResult.model_validate({"overview": "概览。", "items": [], "unexpected": 1})


def test_briefing_result_rejects_more_than_six_items() -> None:
    """A category may contain six stories but may not exceed the agreed section cap."""
    items = [_item(f"https://news.example.test/{index}").model_dump() for index in range(7)]
    with pytest.raises(ValueError):
        BriefingResult.model_validate({"overview": "概览。", "items": items})


def test_briefing_item_allows_a_detailed_factual_summary() -> None:
    """A factual event summary can carry enough detail to stand on its own."""
    detailed_summary = "甲" * 160

    item = _item("https://news.example.test/a", summary=detailed_summary)

    assert item.summary == detailed_summary


def test_briefing_item_allows_a_two_sentence_management_impact() -> None:
    """The impact field has room to explain the concrete business consequence."""
    detailed_impact = "甲" * 80

    item = _item("https://news.example.test/a", why_it_matters=detailed_impact)

    assert item.why_it_matters == detailed_impact


def test_briefing_item_never_makes_an_unfinished_sentence_look_complete() -> None:
    """A size guard may omit an item later, but it must not publish a fabricated ending."""
    item = _item(
        "https://news.example.test/a",
        summary="甲" * 161,
        why_it_matters="乙" * 81,
    )

    assert item.summary == "甲" * 161
    assert item.why_it_matters == "乙" * 81


def test_briefing_item_rejects_model_supplied_ellipsis() -> None:
    """A model must retry through the factual fallback instead of publishing a half-sentence."""
    with pytest.raises(ValueError, match="ellipsis"):
        _item("https://news.example.test/a", summary="公司发布新产品，后续细节仍待确认…")


def test_briefing_item_preserves_a_complete_long_source_title_for_degraded_delivery() -> None:
    """A degraded title list preserves the verified source title instead of truncating it."""
    headline = "半年3轮10亿，他们都投了这家已经把机器人卖到500个家庭的公司并计划进入更多城市"

    item = BriefingItem(
        headline=headline,
        summary="原文已完成事实核验。",
        why_it_matters="它反映机器人行业融资进展。",
        source_name="量子位",
        source_url="https://news.example.test/a",
    )

    assert item.headline == headline


def test_briefing_item_allows_a_complete_sentence_headline_longer_than_28_chars() -> None:
    """A compact linked headline still has room to state the subject, action, and result."""
    headline = "中国移动完成常州防汛专线升级并持续提升极端天气通信保障能力"

    item = BriefingItem(
        headline=headline,
        summary="常州移动完成防汛专线升级并加强极端天气保障。",
        why_it_matters="这会提升重点通信业务的连续性。",
        source_name="江苏新闻",
        source_url="https://news.example.test/a",
    )

    assert item.headline == headline


def test_briefing_item_requires_an_absolute_http_source_url() -> None:
    """A non-web or empty link must never reach a WeCom message."""
    with pytest.raises(ValueError, match="source_url"):
        _item("ftp://news.example.test/a")
    with pytest.raises(ValueError):
        _item("")
    with pytest.raises(ValueError, match="source_url"):
        _item("news.example.test/a")


def test_renderer_uses_spaced_quote_blocks_and_reader_friendly_source() -> None:
    """A reader can scan one visual block at a time without losing the original source."""
    markdown = render_briefing(
        "通信行业日报",
        date(2026, 8, 20),
        BriefingResult(
            overview="概览一句话。",
            items=[
                _item(
                    "https://news.example.test/a",
                    source_name="36氪快讯（RSSHub 镜像）",
                    why_it_matters="企业部署模型时可少一道数据顾虑。",
                )
            ],
        ),
        [_evidence()],
    )

    assert markdown.startswith("# 通信行业日报｜8月20日\n")
    assert "*今日精选 · 1 条*" in markdown
    assert (
        "> **今日要点**\n> 概览一句话。\n\n"
        "## 01｜头条一句话\n\n"
        "> **发生了什么**\n> 第一句摘要。第二句摘要。\n\n"
        "> **为什么值得看**\n> 企业部署模型时可少一道数据顾虑。\n\n"
    ) in markdown
    assert "[36氪快讯 · 阅读原文 ↗](https://news.example.test/a)" in markdown
    assert "RSSHub" not in markdown
    assert "<font" not in markdown


def test_renderer_places_a_markdown_v2_rule_between_briefing_items() -> None:
    """Each selected item has a visible separator in WeCom's richer Markdown view."""
    markdown = render_briefing(
        "通信行业日报",
        date(2026, 8, 20),
        BriefingResult(
            overview="概览一句话。",
            items=[
                _item("https://news.example.test/a"),
                _item("https://news.example.test/b", source_name="C114"),
            ],
        ),
        [_evidence(), _evidence("https://news.example.test/b", source_name="C114")],
    )

    assert "[量子位 · 阅读原文 ↗](https://news.example.test/a)\n\n---\n\n## 02｜" in markdown


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


def test_renderer_keeps_five_detailed_items_inside_the_wecom_budget() -> None:
    """Five fully detailed entries must remain a complete single WeCom message."""
    evidence = [_evidence(source_url=f"https://news.example.test/{index}") for index in range(5)]
    result = BriefingResult(
        overview="今日多项通信与AI基础设施进展进入实质交付阶段。",
        items=[
            _item(item.source_url, summary="甲" * 110, why_it_matters="乙" * 80)
            for item in evidence
        ],
    )

    markdown = render_briefing("通信行业日报", date(2026, 8, 20), result, evidence)

    assert markdown.count("## 0") == 5
    assert len(markdown.encode("utf-8")) <= RENDER_BYTE_BUDGET


def test_renderer_omits_an_oversized_item_without_cutting_the_next_item() -> None:
    """An unusably long direct link drops its block rather than truncating the message tail."""
    oversized_url = "https://news.example.test/" + "a" * 3900
    evidence = [
        _evidence(source_url=oversized_url),
        _evidence(source_url="https://news.example.test/kept"),
    ]
    result = BriefingResult(
        overview="概览。",
        items=[_item(oversized_url), _item("https://news.example.test/kept")],
    )

    markdown = render_briefing("通信行业日报", date(2026, 8, 20), result, evidence)

    assert oversized_url not in markdown
    assert "https://news.example.test/kept" in markdown
    assert not markdown.endswith("…（内容过长，已截断）")


def test_truncate_markdown_keeps_short_content_unchanged() -> None:
    """Content already inside the byte budget ships without any marker."""
    assert truncate_markdown("# 标题\n正文\n") == "# 标题\n正文\n"


def test_truncate_markdown_respects_the_utf8_byte_budget() -> None:
    """The cap never splits a multibyte character and always stays below the budget."""
    content = "# 日报\n" + "这是一条很长的新闻摘要。" * 400

    truncated = truncate_markdown(content)

    assert len(truncated.encode("utf-8")) <= RENDER_BYTE_BUDGET
    assert truncated.endswith("…（内容过长，已截断）")


def test_latest_briefing_date_falls_back_to_the_most_recent_persisted_day(
    tmp_path: Path,
) -> None:
    """The latest read survives the window after midnight before today's run happens."""
    output_dir = tmp_path / "briefings"
    output_dir.mkdir()
    (output_dir / "2026-08-19-telecom.md").write_text("# 通信行业日报", encoding="utf-8")
    (output_dir / "2026-08-19-telecom.done").write_text("done\n", encoding="utf-8")
    # Completion markers and partial temporary files must never count as a briefing day.
    (output_dir / ".2026-08-20-ai.md.tmp").write_text("partial", encoding="utf-8")

    assert latest_briefing_date(output_dir) == date(2026, 8, 19)
    assert latest_briefing_date(tmp_path / "empty") is None


def test_prompt_carries_bounded_evidence_and_forbids_invented_urls() -> None:
    """The prompt is the only evidence channel, so it must pin links to the evidence."""
    messages = build_briefing_messages("通信行业日报", [_ranked_evidence(_evidence())])

    assert messages[0].role == "system"
    user_content = messages[-1].content
    assert "https://news.example.test/a" in user_content
    assert "这是正文摘录。" in user_content
    assert "不得修改、拼接或编造" in user_content


def test_prompt_receives_fixed_management_priority_and_reason() -> None:
    """The generation model explains fixed evidence; it does not reselect or reorder it."""
    evidence = _evidence(title="中国移动启动基站集采")
    ranked = _ranked_evidence(evidence)

    messages = build_briefing_messages("通信行业日报", [ranked])

    assert "已确定优先级：P0" in messages[-1].content
    assert "入选原因：中国移动直接动态" in messages[-1].content
    assert "按以上固定顺序逐条生成" in messages[-1].content
    assert "挑选最重要" not in messages[-1].content


def test_prompt_requests_a_two_sentence_management_impact() -> None:
    """The model must explain the business consequence instead of adding a slogan."""
    messages = build_briefing_messages("通信行业日报", [_ranked_evidence(_evidence())])

    assert "1-2 句、60-75 字为宜、不超过 80 字" in messages[-1].content


def test_webhook_notifier_posts_the_wecom_markdown_envelope() -> None:
    """WeCom group robots accept only the documented markdown message envelope."""
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WEBHOOK_URL, client=client)

    asyncio.run(notifier.push("# 日报"))

    assert requests == [{"msgtype": "markdown", "markdown": {"content": "# 日报"}}]


def test_webhook_notifier_posts_the_wecom_markdown_v2_envelope() -> None:
    """The richer group-bot format must select the matching v2 JSON keys."""
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WEBHOOK_URL, payload_format="wecom_markdown_v2", client=client)

    asyncio.run(notifier.push("## 日报\n\n---\n\n正文"))

    assert requests == [
        {
            "msgtype": "markdown_v2",
            "markdown_v2": {"content": "## 日报\n\n---\n\n正文"},
        }
    ]


def test_webhook_notifier_treats_markdown_v2_rejection_as_a_push_failure() -> None:
    """A v2 payload must not mistake an errcode response for successful delivery."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WEBHOOK_URL, payload_format="wecom_markdown_v2", client=client)

    with pytest.raises(WebhookPushError, match="errcode=93000"):
        asyncio.run(notifier.push("# 日报"))


def test_webhook_notifier_generic_json_posts_text_and_owns_the_response_body() -> None:
    """generic_json targets get a plain text payload; any HTTP 200 means delivered."""
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WEBHOOK_URL, payload_format="generic_json", client=client)

    asyncio.run(notifier.push("# 日报"))

    assert requests == [{"text": "# 日报"}]


def test_webhook_notifier_raises_on_a_non_zero_errcode() -> None:
    """A rejected webhook message must surface as a push failure, not silent loss."""
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WEBHOOK_URL, client=client)

    with pytest.raises(WebhookPushError, match="errcode=93000"):
        asyncio.run(notifier.push("# 日报"))
    assert attempts == 2


def test_webhook_notifier_retries_once_and_then_succeeds() -> None:
    """One transient failure must not lose the whole briefing push."""
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"errcode": 500, "errmsg": "server error"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(WEBHOOK_URL, client=client)

    asyncio.run(notifier.push("# 日报"))

    assert attempts == 2


def test_briefing_settings_default_to_disabled() -> None:
    """The briefing flow is strictly opt-in per deployment."""
    settings = BriefingSettings()

    assert settings.enabled is False
    assert settings.webhook_enabled is False
    assert settings.webhook_format == "wecom_markdown"
    assert settings.window_hours == 24
    assert settings.cron_expression == "30 8 * * mon-fri"
    assert settings.rsshub_base_url is None


def test_webhook_enabled_requires_a_webhook_url() -> None:
    """A webhook target without a URL must fail at configuration load."""
    with pytest.raises(ValueError, match="webhook_url"):
        BriefingSettings(webhook_enabled=True)
    with pytest.raises(ValueError, match="webhook_url"):
        BriefingSettings(webhook_enabled=True, webhook_url="")


def test_monitoring_settings_uses_an_independent_alert_webhook() -> None:
    """An alert robot is configured separately from the briefing delivery robot."""
    from dailycast.core.config import MonitoringSettings

    settings = MonitoringSettings(
        webhook_url="https://qyapi.example.test/alert-hook",
        webhook_format="wecom_markdown_v2",
    )

    assert settings.webhook_url == "https://qyapi.example.test/alert-hook"
    assert settings.webhook_format == "wecom_markdown_v2"


def test_briefing_settings_load_from_yaml(app_config_path: Path, tmp_path: Path) -> None:
    """The nested briefing block flows through the existing YAML settings source."""
    app_config_path.write_text(
        app_config_path.read_text(encoding="utf-8")
        + "briefing:\n"
        + "  enabled: true\n"
        + "  window_hours: 12\n"
        + "  rsshub_base_url: https://rsshub.example.test/private-instance\n"
        + "  webhook_enabled: true\n"
        + "  webhook_format: generic_json\n"
        + f"  webhook_url: {WEBHOOK_URL}\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=app_config_path, env_file=tmp_path / "absent.env")

    assert settings.briefing.enabled is True
    assert settings.briefing.window_hours == 12
    assert settings.briefing.webhook_enabled is True
    assert settings.briefing.webhook_format == "generic_json"
    assert settings.briefing.webhook_url == WEBHOOK_URL
    assert settings.briefing.rsshub_base_url == "https://rsshub.example.test/private-instance"


def test_interleave_by_source_rotates_feeds_instead_of_ranking_one_to_the_top() -> None:
    """A high-priority feed must share the briefing with lower-priority sources."""
    entries = [
        ((-90, -10.0, 1), _evidence(source_url="https://a.example.test/1", source_name="甲源")),
        ((-90, -9.0, 2), _evidence(source_url="https://a.example.test/2", source_name="甲源")),
        ((-90, -8.0, 3), _evidence(source_url="https://a.example.test/3", source_name="甲源")),
        ((-50, -7.0, 4), _evidence(source_url="https://b.example.test/1", source_name="乙源")),
        ((-50, -6.0, 5), _evidence(source_url="https://c.example.test/1", source_name="丙源")),
    ]

    picked = _interleave_by_source(entries)

    assert [entry.source_name for entry in picked] == ["甲源", "乙源", "丙源", "甲源", "甲源"]
    # Within one source the ranked order is preserved.
    assert [entry.source_url for entry in picked if entry.source_name == "甲源"] == [
        "https://a.example.test/1",
        "https://a.example.test/2",
        "https://a.example.test/3",
    ]
