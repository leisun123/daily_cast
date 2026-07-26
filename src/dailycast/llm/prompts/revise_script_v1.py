"""Versioned prompt identity for one bounded controlled script-revision attempt."""

from dailycast.llm.prompts import PromptTemplate

REVISE_SCRIPT_V1 = PromptTemplate(
    version="revise_script_v2",
    system_instruction=(
        "Return only strict EpisodeScript JSON. Revise only the reported issues using supplied "
        "outline, script, deterministic findings, semantic review, and bounded evidence. Preserve "
        "section IDs, section order, and event/article allowlists. When deterministic findings "
        "flag duration, expand or condense the narration to match outline.target_seconds and each "
        "section's seconds without padding with unsupported facts. Do not add facts, sources, "
        "Markdown, URLs, unsupported SSML, or unreported changes."
    ),
)
