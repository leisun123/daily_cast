"""Versioned prompt identity for one bounded controlled script-revision attempt."""

from dailycast.llm.prompts import PromptTemplate

REVISE_SCRIPT_V1 = PromptTemplate(
    version="revise_script_v3",
    system_instruction=(
        "Return only strict EpisodeScript JSON. Revise only the reported issues using supplied "
        "outline, script, deterministic findings, semantic review, and bounded evidence. Preserve "
        "section IDs, section order, and event/article allowlists. When deterministic findings "
        "flag duration, use duration_requirement.target_character_count as a mandatory total "
        "spoken-character target. Do not return the script until it reaches that target while "
        "matching each section's seconds; expand with evidence-bounded explanation, listener "
        "context, and natural transitions rather than unsupported facts. "
        "Do not add facts, sources, "
        "Markdown, URLs, unsupported SSML, or unreported changes."
    ),
)
