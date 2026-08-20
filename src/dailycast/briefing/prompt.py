"""Prompt construction for the daily text briefing generation."""

from __future__ import annotations

from collections.abc import Sequence

from dailycast.briefing.schemas import MAX_BRIEFING_ITEMS, BriefingEvidence
from dailycast.llm.contracts import LLMMessage

_SYSTEM_PROMPT = (
    "你是一名中文科技资讯编辑，为读者编写每日文字简报。"
    "你只能依据用户提供的证据材料写作，不得使用任何外部知识，"
    "不得编造事实、数字、日期或链接。"
    "写作风格：通俗易懂的简体中文，面向非专业读者，避免堆砌术语。"
)


def build_briefing_messages(
    category_title: str, evidence: Sequence[BriefingEvidence]
) -> tuple[LLMMessage, ...]:
    """Build the bounded system and user messages for one category briefing."""
    blocks: list[str] = []
    for index, item in enumerate(evidence, start=1):
        published = item.published_at.strftime("%Y-%m-%d %H:%M") if item.published_at else "未知"
        blocks.append(
            f"[{index}] 标题：{item.title}\n"
            f"来源：{item.source_name}\n"
            f"发布时间：{published}\n"
            f"原文链接：{item.source_url}\n"
            f"正文摘录：\n{item.excerpt}"
        )
    user_content = (
        f"以下是过去24小时收集到的「{category_title}」相关新闻证据（共 {len(evidence)} 条）：\n\n"
        + "\n\n".join(blocks)
        + f"\n\n请从以上证据中挑选最重要、最有信息量的新闻（最多 {MAX_BRIEFING_ITEMS} 条），"
        f"生成今日「{category_title}」文字简报。要求：\n"
        "- overview：用 2-3 句话概括当天该类目的整体动态。\n"
        "- 每条 item 包含：headline（一句话标题）、summary（2-3 句通俗摘要）、"
        "source_name（照抄对应证据的「来源」）、source_url（必须原样照抄对应证据的"
        "「原文链接」，不得修改、拼接或编造）。\n"
        "- 同一事件只报道一次；证据不足时不要硬凑条数。\n"
    )
    return (
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_content),
    )
