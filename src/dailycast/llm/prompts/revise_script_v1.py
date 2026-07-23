"""Versioned prompt identity for one bounded controlled script-revision attempt."""

from dailycast.llm.prompts import PromptTemplate

REVISE_SCRIPT_V1 = PromptTemplate(
    version="revise_script_v1",
    system_instruction=(
        "Return only strict EpisodeScript JSON. Revise only the reported issues using supplied "
        "outline, script, deterministic findings, semantic review, and bounded evidence. Preserve "
        "section IDs, section order, and event/article allowlists. Do not add facts, sources, "
        "Markdown, URLs, unsupported SSML, or unreported changes."
    ),
)
