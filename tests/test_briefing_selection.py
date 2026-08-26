"""Candidate-pool selection for the daily telecom and AI briefings."""

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


def _candidate(
    *,
    title: str,
    content: str,
    article_id: int = 1,
    source_id: str = "test-source",
    source_priority: int = 80,
    source_url: str | None = None,
    published_at: datetime | None = None,
) -> BriefingSelectionCandidate:
    published = published_at or datetime(2026, 8, 21, 8, tzinfo=UTC)
    evidence = BriefingEvidence(
        title=title,
        source_name=f"来源 {source_id}",
        published_at=published,
        excerpt=content,
        source_url=source_url or f"https://{source_id}.example.test/item-{article_id}",
    )
    return BriefingSelectionCandidate(
        article_id=article_id,
        source_id=source_id,
        source_priority=source_priority,
        discovered_at=published,
        evidence=evidence,
    )


@pytest.fixture
def policy():
    """Load the production policy used by the daily generation path."""
    return load_selection_policy(PROJECT_ROOT / "config" / "briefing.selection.yaml")


def test_checked_in_management_policy_is_editorial_for_both_categories() -> None:
    """No production category may use literal rules to decide management relevance."""
    policy = load_selection_policy(PROJECT_ROOT / "config" / "briefing.selection.yaml")

    telecom = policy.category("telecom")
    ai = policy.category("ai")
    assert telecom.editorial_selection is True
    assert telecom.tiers == ("LLM",)
    assert telecom.editorial_candidate_limit == 20
    assert telecom.editorial_max_candidates_per_publisher == 10
    assert not hasattr(telecom, "rules")
    assert not hasattr(telecom, "required_any_of")
    assert not hasattr(telecom, "fallback_any_of")
    assert not hasattr(telecom, "global_excludes")
    assert ai.editorial_selection is True
    assert ai.tiers == ("LLM",)
    assert ai.editorial_candidate_limit == 20
    assert ai.editorial_max_candidates_per_publisher == 10
    assert not hasattr(ai, "rules")


@pytest.mark.parametrize("category", ["telecom", "ai"])
def test_editorial_pool_keeps_verified_candidate_without_literal_keyword_match(
    category, policy
) -> None:
    """Content semantics are deliberately deferred to the LLM for both categories."""
    selected = select_evidence(
        category,
        [
            _candidate(
                title="项目完成关键验收",
                content="实施单位已完成关键节点验收，并公布下一阶段资源安排。",
            )
        ],
        policy,
        limit=5,
    )

    assert [(item.article_id, item.tier, item.rule_id) for item in selected] == [
        (1, "LLM", "editorial-llm")
    ]


def test_telecom_editorial_pool_round_robins_publishers_before_repeating(policy) -> None:
    """Code bounds context by domain without ranking topics or publisher credibility."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="source-a",
                source_priority=100,
                title="候选一",
                content="经核验的候选正文。",
            ),
            _candidate(
                article_id=2,
                source_id="source-a",
                source_priority=100,
                title="候选二",
                content="经核验的候选正文。",
            ),
            _candidate(
                article_id=3,
                source_id="source-b",
                source_priority=80,
                title="候选三",
                content="经核验的候选正文。",
            ),
        ],
        policy,
        limit=5,
    )

    assert [item.article_id for item in selected] == [1, 3, 2]


def test_editorial_pool_caps_one_publisher_before_llm_review(policy) -> None:
    """A prolific publisher cannot consume all evidence slots before editorial judgment."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=index,
                source_id="prolific-source",
                source_priority=100,
                title=f"候选文章 {index}",
                content="经过时间、正文和链接核验的候选。",
            )
            for index in range(1, 14)
        ]
        + [
            _candidate(
                article_id=14,
                source_id="other-source",
                source_priority=80,
                title="另一来源候选",
                content="经过时间、正文和链接核验的候选。",
            )
        ],
        policy,
        limit=20,
    )

    assert [item.article_id for item in selected] == [1, 14, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_editorial_pool_balances_real_publishers_inside_one_search_channel(policy) -> None:
    """Search discoveries from different publisher domains must not share one source cap."""
    candidates = [
        _candidate(
            article_id=index,
            source_id="openai-web-research-ai-management",
            title=f"甲媒体候选 {index}",
            content="经过时间、正文和链接核验的候选。",
            source_url=f"https://media-a.example/article-{index}",
        )
        for index in range(1, 12)
    ] + [
        _candidate(
            article_id=20,
            source_id="openai-web-research-ai-management",
            title="乙媒体候选",
            content="经过时间、正文和链接核验的候选。",
            source_url="https://media-b.example/article-20",
        )
    ]

    selected = select_evidence("ai", candidates, policy, limit=20)

    assert len(selected) == 11
    assert selected[0].evidence.source_url.startswith("https://media-a.example/")
    assert selected[1].evidence.source_url == "https://media-b.example/article-20"


def test_editorial_pool_does_not_use_source_priority_as_a_content_proxy(policy) -> None:
    """A manually configured source score must not hide a fresher candidate from the LLM."""
    selected = select_evidence(
        "telecom",
        [
            _candidate(
                article_id=1,
                source_id="high-priority-source",
                source_priority=100,
                title="较早候选",
                content="经过时间、正文和链接核验的候选。",
                published_at=datetime(2026, 8, 20, 8, tzinfo=UTC),
            ),
            _candidate(
                article_id=2,
                source_id="low-priority-source",
                source_priority=10,
                title="较新候选",
                content="经过时间、正文和链接核验的候选。",
                published_at=datetime(2026, 8, 21, 8, tzinfo=UTC),
            ),
        ],
        policy,
        limit=1,
    )

    assert [item.article_id for item in selected] == [2]


def test_telecom_editorial_prompt_delegates_relevance_and_source_suitability_to_llm(policy) -> None:
    """The LLM, not source names or keyword matches, judges content and reporting quality."""
    evidence = select_evidence(
        "telecom",
        [_candidate(title="候选新闻", content="有待编辑判断的正文。")],
        policy,
        limit=5,
    )

    prompt = build_briefing_messages(
        "通信行业日报", evidence, category="telecom", editorial_selection=True
    )[-1].content

    assert "中国移动管理层" in prompt
    assert "常州" in prompt
    assert "江苏" in prompt
    assert "中国电信" in prompt
    assert "中国联通" in prompt
    assert "基站" in prompt
    assert "频谱" in prompt
    assert "来源配置或关键词命中" in prompt
    assert "来源名称本身不能证明" in prompt
    assert "不属于国内通信管理议题" in prompt
    assert "只能从给定候选中选择" in prompt


def test_ai_editorial_prompt_keeps_global_ai_scope_but_judges_source_suitability(policy) -> None:
    """AI selection remains independent from telecom while applying the same source judgment."""
    evidence = select_evidence(
        "ai",
        [_candidate(title="豆包工作正式发布", content="字节跳动推出企业办公智能体。")],
        policy,
        limit=5,
    )

    prompt = build_briefing_messages(
        "AI 动态日报", evidence, category="ai", editorial_selection=True
    )[-1].content

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
    assert "来源名称本身不能证明" in prompt
    assert "排除纯论文、预印本、榜单" in prompt


def test_policy_rejects_a_non_editorial_category(tmp_path: Path) -> None:
    """A local semantic selector cannot be reintroduced through a future YAML edit."""
    policy_path = tmp_path / "selection.yaml"
    policy_path.write_text(
        """
categories:
  telecom:
    tiers: [LLM]
    editorial_selection: true
  ai:
    tiers: [LLM]
    editorial_selection: false
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="editorial_selection"):
        load_selection_policy(policy_path)
