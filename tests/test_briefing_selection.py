"""Literal management-policy selection for the daily telecom and AI briefings."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dailycast.briefing.prompt import build_briefing_messages
from dailycast.briefing.schemas import BriefingEvidence
from dailycast.briefing.selection import (
    BriefingSelectionCandidate,
    load_selection_policy,
    select_evidence,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_policy(
    path: Path, *, telecom_tier: str = "P0", fallback_max_items_per_publisher: int | None = None
) -> Path:
    policy_path = path / "selection.yaml"
    fallback_limit = (
        f"\n    fallback_max_items_per_publisher: {fallback_max_items_per_publisher}"
        if fallback_max_items_per_publisher is not None
        else ""
    )
    policy_path.write_text(
        f"""
categories:
  telecom:
    tiers: [P0, P1, P2, P3, P4, P5]
    max_items_per_publisher: 1{fallback_limit}
    rules:
      - id: telecom-ran
        tier: {telecom_tier}
        specificity: 500
        all_groups:
          - [RAN]
        reason: 无线接入网直接动态
    fallback_any_of: [通信业]
    fallback_tier: P5
    global_excludes: [电信诈骗]
  ai:
    tiers: [A0, A1, A2, A3]
    max_items_per_publisher: 1
    rules: []
    fallback_any_of: []
    global_excludes: []
    paper_only_terms: [论文]
""".strip(),
        encoding="utf-8",
    )
    return policy_path


def _candidate(
    *,
    title: str,
    content: str,
    article_id: int = 1,
    source_id: str = "test-source",
    source_priority: int = 80,
    source_url: str | None = None,
) -> BriefingSelectionCandidate:
    evidence = BriefingEvidence(
        title=title,
        source_name=f"来源 {source_id}",
        published_at=datetime(2026, 8, 21, 8, tzinfo=UTC),
        excerpt=content,
        source_url=source_url or f"https://{source_id}.example.test/item-{article_id}",
    )
    return BriefingSelectionCandidate(
        article_id=article_id,
        source_id=source_id,
        source_priority=source_priority,
        discovered_at=datetime(2026, 8, 21, 8, tzinfo=UTC),
        evidence=evidence,
    )


def test_short_latin_terms_do_not_match_inside_a_longer_word(tmp_path: Path) -> None:
    """RAN and AI must respect Latin-token boundaries after normalization."""
    policy = load_selection_policy(_write_policy(tmp_path))

    selected = select_evidence(
        "telecom",
        [_candidate(title="transparent Random system", content="no telecom context")],
        policy,
        limit=5,
    )

    assert selected == ()


def test_unknown_tier_is_rejected(tmp_path: Path) -> None:
    """Malformed YAML must fail fast instead of silently weakening the policy."""
    with pytest.raises(ValueError, match="tier"):
        load_selection_policy(_write_policy(tmp_path, telecom_tier="P9"))


def test_checked_in_management_policy_is_valid() -> None:
    """The production policy remains a standalone, strictly validated config artifact."""
    policy = load_selection_policy(PROJECT_ROOT / "config" / "briefing.selection.yaml")

    assert policy.category("telecom").fallback_tier == "P5"
    assert policy.category("telecom").diversify_first_tiers == ("P0",)
    assert policy.category("telecom").max_items_per_publisher == 1
    assert policy.category("telecom").fallback_max_items_per_publisher == 2
    assert policy.category("ai").editorial_selection is True
    assert policy.category("ai").editorial_candidate_limit == 20
    assert policy.category("ai").editorial_max_candidates_per_source == 10
    assert policy.category("ai").max_items_per_publisher == 2
    assert policy.category("ai").fallback_max_items_per_publisher == 6
    mobile_rule = next(
        rule for rule in policy.category("telecom").rules if rule.id == "telecom-china-mobile"
    )
    assert mobile_rule.max_items_per_briefing == 2
    assert policy.category("ai").fallback_any_of == ()


@pytest.fixture
def policy():
    """Load the management-approved production policy for ranking examples."""
    return load_selection_policy(PROJECT_ROOT / "config" / "briefing.selection.yaml")


def test_direct_mobile_outranks_competitor_and_policy(policy) -> None:
    """Leadership-relevant China Mobile evidence comes before competition and policy fallback."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="policy-source",
                title="工信部发布通信规划",
                content="通信网络专项政策文件",
            ),
            _candidate(
                article_id=2,
                source_id="competitor-source",
                title="中国联通宣布算力网络合作",
                content="运营商业务合作进展",
            ),
            _candidate(
                article_id=3,
                source_id="mobile-source",
                title="中国移动启动基站集采",
                content="无线网建设项目启动",
            ),
        ],
        policy,
        limit=5,
    )

    assert [(item.tier, item.specificity) for item in selected] == [
        ("P0", 500),
        ("P1", 300),
        ("P3", 200),
    ]


def test_domestic_locality_precedes_national_subject_priority(policy) -> None:
    """Changzhou, Jiangsu, other cities, then national is the fixed management order."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="national",
                title="中国移动集团启动全国 6G 网络建设计划",
                content="全国通信网络建设进展。",
            ),
            _candidate(
                article_id=2,
                source_id="jiangsu",
                title="江苏南京中国电信推进 5G-A 网络建设",
                content="省内运营商公布建设进展。",
            ),
            _candidate(
                article_id=3,
                source_id="other-city",
                title="上海中国联通开通 5G-A 网络",
                content="地级市网络建设进展。",
            ),
            _candidate(
                article_id=4,
                source_id="changzhou",
                title="常州中国移动启动基站改造",
                content="常州市无线网建设项目启动。",
            ),
        ],
        policy,
        limit=5,
    )

    assert [item.article_id for item in selected] == [4, 2, 3, 1]


def test_domestic_operator_story_remains_available_as_the_last_fallback(policy) -> None:
    """A verified operator story may fill a quiet-day slot without a narrow action keyword."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                title="中国联通沈阳市分公司织就数字动脉",
                content="公司完成主干光缆扩容，持续提升本地网络承载能力。",
            )
        ],
        policy,
        limit=5,
    )

    assert [item.tier for item in selected] == ["P5"]


@pytest.mark.parametrize(
    "title,content",
    [
        (
            "MobileX confirms CONX investment",
            "The US mobile service provider will expand its wireless distribution.",
        ),
        (
            "Omdia predicts surge in satellite IoT connections",
            "Global satellite connectivity is forecast to expand through 2035.",
        ),
        (
            "Vodafone starts nationwide 5G network deployment",
            "The European operator announced a new RAN build.",
        ),
    ],
)
def test_foreign_telecom_news_cannot_enter_the_domestic_brief(
    title: str, content: str, policy
) -> None:
    """Global telecom relevance alone cannot bypass the domestic-only boundary."""
    assert (
        select_evidence("telecom", [_candidate(title=title, content=content)], policy, limit=5)
        == ()
    )


def test_p0_covers_each_management_subtopic_before_repeating_one_rule(policy) -> None:
    """P0 should show China Mobile, radio, spectrum, and 6G signals before a repeat."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="mobile-primary",
                title="中国移动启动核心网升级",
                content="网络能力建设项目启动",
            ),
            _candidate(
                article_id=2,
                source_id="radio-source",
                title="全国基站建设招标启动",
                content="无线网新建项目推进",
            ),
            _candidate(
                article_id=3,
                source_id="spectrum-source",
                title="工信部频谱许可核发",
                content="无线电频段调整方案公布",
            ),
            _candidate(
                article_id=4,
                source_id="six-g-source",
                title="全国 5G-A 商用开通",
                content="6G 网络建设计划发布",
            ),
            _candidate(
                article_id=5,
                source_id="mobile-secondary",
                title="中国移动发布算力网络规划",
                content="公司公布网络建设安排",
            ),
        ],
        policy,
        limit=5,
    )

    assert [item.article_id for item in selected] == [1, 2, 3, 4, 5]


def test_china_mobile_rule_is_capped_at_two_items_per_briefing(policy) -> None:
    """Several distinct China Mobile stories cannot crowd out the next management priority."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="mobile-primary",
                title="中国移动启动核心网升级",
                content="网络能力建设项目启动",
            ),
            _candidate(
                article_id=2,
                source_id="mobile-secondary",
                title="中国移动发布算力网络规划",
                content="公司公布网络建设安排",
            ),
            _candidate(
                article_id=3,
                source_id="mobile-third",
                title="中国移动推进云网融合项目",
                content="网络能力建设项目进展",
            ),
            _candidate(
                article_id=4,
                source_id="radio-source",
                title="全国基站建设招标启动",
                content="无线网新建项目推进",
            ),
            _candidate(
                article_id=5,
                source_id="spectrum-source",
                title="工信部频谱许可核发",
                content="无线电频段调整方案公布",
            ),
            _candidate(
                article_id=6,
                source_id="six-g-source",
                title="全国 5G-A 商用开通",
                content="6G 网络建设计划发布",
            ),
            _candidate(
                article_id=7,
                source_id="competitor-source",
                title="中国联通启动网络建设合作",
                content="运营商合作项目进入部署阶段",
            ),
        ],
        policy,
        limit=6,
    )

    assert [item.article_id for item in selected] == [1, 4, 5, 6, 2, 7]


def test_publisher_cap_prevents_one_outlet_filling_a_tier(policy) -> None:
    """After global priority is fixed, one outlet cannot fill the whole P0/450 bucket."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="source-a",
                source_priority=100,
                title="上海基站建设项目启动",
                content="无线网建设进展",
            ),
            _candidate(
                article_id=2,
                source_id="source-b",
                source_priority=80,
                title="北京基站网络商用开通",
                content="无线网络建设进展",
            ),
            _candidate(
                article_id=3,
                source_id="source-a",
                source_priority=100,
                title="上海基站改造计划发布",
                content="无线网络改造进展",
            ),
        ],
        policy,
        limit=5,
    )

    assert [item.source_id for item in selected] == ["source-a", "source-b", "source-a"]
    assert sum(item.source_id == "source-a" for item in selected) == 2


def test_publisher_fallback_can_add_one_second_story_after_source_diversity(
    tmp_path: Path,
) -> None:
    """A quiet day may use a second story from one outlet, but never let it fill the brief."""
    policy = load_selection_policy(_write_policy(tmp_path, fallback_max_items_per_publisher=2))

    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="source-a",
                source_priority=100,
                title="RAN 建设进展一",
                content="无线接入网项目进展",
            ),
            _candidate(
                article_id=2,
                source_id="source-b",
                source_priority=80,
                title="RAN 建设进展二",
                content="无线接入网项目进展",
            ),
            _candidate(
                article_id=3,
                source_id="source-a",
                source_priority=100,
                title="RAN 建设进展三",
                content="无线接入网项目进展",
            ),
        ],
        policy,
        limit=5,
    )

    assert [item.article_id for item in selected] == [1, 2, 3]


def test_publisher_cap_counts_c114_once_across_multiple_feed_ids(tmp_path: Path) -> None:
    """C114's separate feeds still represent one outlet, never five daily slots."""
    policy = load_selection_policy(_write_policy(tmp_path))

    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="c114-operators",
                source_priority=100,
                source_url="https://www.c114.com.cn/news/1.html",
                title="RAN 网络建设进展",
                content="运营商披露无线接入网动态",
            ),
            _candidate(
                article_id=2,
                source_id="c114-equipment",
                source_priority=90,
                source_url="https://www.c114.com.cn/news/2.html",
                title="RAN 设备部署进展",
                content="供应商披露无线接入网动态",
            ),
            _candidate(
                article_id=3,
                source_id="official-miit",
                source_priority=80,
                source_url="https://www.miit.gov.cn/news/3.html",
                title="RAN 试点建设进展",
                content="主管部门披露无线接入网动态",
            ),
        ],
        policy,
        limit=5,
    )

    assert [item.article_id for item in selected] == [1, 3]


@pytest.mark.parametrize(
    "title,content",
    [
        ("老旧小区改造开工", "住宅更新项目"),
        ("电信诈骗案件通报", "反诈宣传"),
        ("华为 Mate 新机发布", "手机产品上市"),
    ],
)
def test_telecom_false_positives_are_excluded(title: str, content: str, policy) -> None:
    """Ambiguous consumer and public-safety stories cannot climb into telecom tiers."""
    assert (
        select_evidence("telecom", [_candidate(title=title, content=content)], policy, limit=5)
        == ()
    )


def test_ai_editorial_pool_keeps_a_candidate_without_keyword_match(policy) -> None:
    """AI candidates reach the editor even when no local keyword rule matches them."""
    selected = select_evidence(
        "ai",
        [
            _candidate(
                title="自动化客服接入新服务",
                content="机构推出多轮对话服务，已接入软件平台并覆盖三个城市。",
            )
        ],
        policy,
        limit=5,
    )

    assert [(item.article_id, item.tier) for item in selected] == [(1, "LLM")]


def test_ai_editorial_pool_defers_priority_to_the_llm(policy) -> None:
    """The AI pool keeps verified candidates rather than encoding topical priority locally."""
    selected = select_evidence(
        "ai",
        [
            _candidate(
                article_id=1,
                source_id="application-source",
                title="AI应用上线",
                content="企业客户开始采用",
            ),
            _candidate(
                article_id=2,
                source_id="deployment-source",
                title="昇腾适配完成",
                content="企业私有化部署落地",
            ),
        ],
        policy,
        limit=5,
    )

    assert [item.article_id for item in selected] == [1, 2]
    assert [item.tier for item in selected] == ["LLM", "LLM"]


def test_global_industry_body_is_not_a_domestic_telecom_signal(policy) -> None:
    """A verified GSMA network report cannot fill a domestic management briefing."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                source_id="gsma-newsroom",
                title="80% of Malawians Remain Offline Despite Coverage",
                content="GSMA report details mobile network coverage and connectivity policy.",
            )
        ],
        policy,
        limit=5,
    )

    assert selected == ()


def test_ai_editorial_pool_keeps_deeper_valid_items_from_a_prolific_source(policy) -> None:
    """One prolific feed cannot consume the complete context window before editorial review."""
    selected = select_evidence(
        "ai",
        [
            _candidate(
                article_id=index,
                source_id="prolific-source",
                title=f"候选文章 {index}",
                content="经过核验的 AI 行业候选。",
            )
            for index in range(1, 12)
        ]
        + [
            _candidate(
                article_id=12,
                source_id="other-source",
                title="另一来源候选",
                content="经过核验的 AI 行业候选。",
            )
        ],
        policy,
        limit=20,
    )

    assert [item.article_id for item in selected] == [1, 12, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_ai_editorial_prompt_excludes_papers_after_candidates_are_collected(policy) -> None:
    """Paper exclusion is an editorial instruction, not a brittle pre-selection keyword gate."""
    evidence = select_evidence(
        "ai", [_candidate(title="论文预印本", content="基准测试结果")], policy, limit=5
    )

    messages = build_briefing_messages("AI 动态日报", evidence, editorial_selection=True)

    assert "排除纯论文、预印本、榜单" in messages[-1].content


def test_ai_editorial_prompt_selects_global_ai_events_from_chinese_sources(policy) -> None:
    """The AI editor balances global events without reintroducing operator bias."""
    evidence = select_evidence(
        "ai",
        [_candidate(title="豆包工作正式发布", content="字节跳动推出企业办公智能体。")],
        policy,
        limit=5,
    )

    messages = build_briefing_messages("AI 动态日报", evidence, editorial_selection=True)
    prompt = messages[-1].content

    assert "通信行业直接相关" not in prompt
    assert "运营商经营或网络建设" in prompt
    assert "中文来源" in prompt
    assert "字节" in prompt
    assert "腾讯" in prompt
    assert "华为" in prompt
    assert "小米" in prompt
    assert "OpenAI" in prompt
    assert "Anthropic" in prompt
    assert "Google" in prompt
    assert "GPT" in prompt
    assert "Claude" in prompt
    assert "Gemini" in prompt
    assert "阅读的 6 条" in prompt
    assert "大模型" in prompt
    assert "本地化" in prompt
    assert "热门应用" in prompt
    assert "模型与部署" in prompt
    assert "应用与热点" in prompt
