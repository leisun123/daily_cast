"""Prompt construction for the daily text briefing generation."""

from __future__ import annotations

from collections.abc import Sequence

from dailycast.briefing.schemas import MAX_BRIEFING_ITEMS, BriefingResult
from dailycast.briefing.selection import RankedBriefingEvidence
from dailycast.llm.contracts import LLMMessage

_SYSTEM_PROMPT = (
    "你是一名中文科技资讯编辑，为读者编写每日文字简报。"
    "你只能依据用户提供的证据材料写作，不得使用任何外部知识，"
    "不得编造事实、数字、日期或链接。"
    "写作风格：通俗易懂的简体中文，面向非专业读者，避免堆砌术语。"
)


def build_briefing_messages(
    category_title: str,
    evidence: Sequence[RankedBriefingEvidence],
    *,
    editorial_selection: bool = False,
) -> tuple[LLMMessage, ...]:
    """Build evidence instructions for deterministic or LLM-led item selection."""
    blocks: list[str] = []
    for index, item in enumerate(evidence, start=1):
        source = item.evidence
        published = (
            source.published_at.strftime("%Y-%m-%d %H:%M") if source.published_at else "未知"
        )
        selection_context = (
            f"[{index}] 候选文章（已通过时间、正文与原文链接核验）\n"
            if editorial_selection
            else f"[{index}] 已确定优先级：{item.tier}\n入选原因：{item.reason}\n"
        )
        blocks.append(
            selection_context + f"标题：{source.title}\n"
            f"来源：{source.source_name}\n"
            f"发布时间：{published}\n"
            f"原文链接：{source.source_url}\n"
            f"正文摘录：\n{source.excerpt}"
        )
    selection_instruction = (
        "这些候选均已通过时间、正文、中文来源与原文链接核验。请由你自行挑选最值得中国移动管理层"
        f"阅读的 {MAX_BRIEFING_ITEMS} 条全球 AI 发展新闻，不得因为一条新闻与通信行业相关就提高其"
        "优先级。事件范围不限"
        "国内：既覆盖字节、腾讯、华为、小米、阿里、百度、DeepSeek、智谱、月之暗面、MiniMax，"
        "也覆盖 OpenAI GPT、Anthropic Claude、Google Gemini、Meta Llama、xAI Grok 等全球重要"
        "大模型的发布、升级、开源、本地化或私有化部署；同时覆盖 AI 基础设施、"
        "算力芯片、开源生态，以及已有明确产品、用户或市场热度的热门应用与热点、智能体和具身智能。"
        "候选充足时，模型与部署优先 2 条，应用与热点 1-2 条，基础设施、生态或商业化 1-2 条。"
        "以运营商经营或网络建设为主体的新闻属于通信板块，不得选入 AI 板块。不得选通用办公噱头、"
        "法律模型、泛安全应用或没有明确业务进展的展会展示。排除纯论文、预印本、榜单。"
        "避免同一事件重复，并优先覆盖不同来源。\n"
        if editorial_selection
        else (
            f"请按以上固定顺序逐条生成今日「{category_title}」文字简报，不得重新挑选、"
            f"排序或补充其他新闻（最多 {MAX_BRIEFING_ITEMS} 条）。\n"
        )
    )
    output_requirements = (
        "- overview：用 1-2 句话概括当天该类目的整体动态，不超过 120 字。\n"
        "- 每条 item 包含：headline（写成主体明确、结论在前的完整新闻句，建议 20-36 字；"
        "必须交代谁做了什么或产生了什么结果，不能只写成名词短语，不加句号且不超过 36 字）、"
        "theme（放在标题前的 2-6 字主题标签，如“5G-A 场景”“国产模型”“算力投资”；"
        "必须直接对应这条证据，不得泛化或编造，不含“｜”符号）、"
        "summary（只说明发生了什么，1-2 句、110-150 字为宜、不超过 160 字；必须说明主体、"
        "动作、关键数字/范围/阶段中的可用信息，以及当前结果或下一步；只能复述证据中的事实）、"
        "why_it_matters（只说明对行业、用户或业务的实际影响，1-2 句、60-75 字为宜、"
        "不超过 80 字；"
        "不能重复 summary，也不能写“值得关注”等空话）。summary、why_it_matters 和 overview "
        "都必须以完整句结束；不要使用省略号或截断句。"
        "source_name（照抄对应证据的「来源」）、"
        "source_url（必须原样照抄对应证据的「原文链接」，不得修改、拼接或编造）。\n"
        + (
            "- 每条已给定证据都要覆盖一次；不得重复报道同一事件。\n"
            if not editorial_selection
            else "- 只能从给定候选中选择，不得补充其他新闻；不得重复报道同一事件。\n"
        )
        + "- 证据不足时不要补充其他新闻，也不要硬凑条数。\n"
    )
    user_content = (
        f"以下是「{category_title}」新闻证据（共 {len(evidence)} 条）：\n\n"
        + "\n\n".join(blocks)
        + f"\n\n{selection_instruction}要求：\n"
        + output_requirements
    )
    return (
        LLMMessage(role="system", content=_SYSTEM_PROMPT),
        LLMMessage(role="user", content=user_content),
    )


def build_merged_focus_messages(
    categories: Sequence[tuple[str, BriefingResult]],
) -> tuple[LLMMessage, ...]:
    """Ask the editor to write one natural lead from already selected factual items."""
    blocks: list[str] = []
    for category_name, result in categories:
        items = "\n".join(
            f"- 标题：{item.headline}\n  事实：{item.summary}" for item in result.items
        )
        blocks.append(f"【{category_name}】\n{items}")
    user_content = (
        "请为最终「昨日关注」写一条自然的中文综合导语。它会直接接在“昨日关注：”之后。\n"
        "只能依据下列已经入选且核验过的新闻事实；不要重新选题、不要补充外部信息、"
        "不要逐条罗列。优先抽取通信行业与全球AI动态中最值得管理层快速理解的共同趋势；"
        "AI事件国别不限，依据的是已核验中文来源，不得仅因来源语言而把事件归为国内事件。"
        "写成 35-85 字的自然一句话，可用分号连接两个相关判断。不要使用“通信：”“AI：”"
        "等字段标签，不要 Markdown，不要省略号。\n\n" + "\n\n".join(blocks)
    )
    return (
        LLMMessage(
            role="system",
            content="你是一名中文管理简报编辑，只能依据给定事实写简洁、自然的导语。",
        ),
        LLMMessage(role="user", content=user_content),
    )
