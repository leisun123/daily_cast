"""Delivery-safe factual prose boundaries for briefing items."""

from dailycast.briefing.schemas import BriefingItem


def test_verbose_summary_compacts_at_a_complete_sentence_boundary() -> None:
    """A WeCom size guard must not visibly cut a factual sentence in half."""
    first_sentence = "中国移动宣布完成重点城区网络升级，并披露了覆盖范围和下一阶段建设安排。"
    item = BriefingItem(
        headline="网络升级进展",
        summary=first_sentence + "后续细节" * 80,
        why_it_matters="网络能力提升会影响重点区域的业务承载和客户体验。",
        source_name="运营商公告",
        source_url="https://publisher.example.test/article",
    )

    assert item.summary == first_sentence
    assert item.summary.endswith("。")
    assert "…" not in item.summary
