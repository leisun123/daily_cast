"""Explicit outline contract for JSON-only Responses gateways."""

from dailycast.llm.prompts import PromptTemplate

GENERATE_OUTLINE_V2 = PromptTemplate(
    version="generate_outline_v2",
    system_instruction=(
        "Create an outline using only the supplied selected-event EvidenceDossiers. Return exactly "
        "one JSON object, with no Markdown or extra top-level keys. Its required top-level fields "
        'are `schema_version` set to the string "1", `title_angle`, `target_seconds`, and '
        "`sections`. Every section must contain `section_id`, `type`, `event_ids`, `goal`, "
        "`key_facts`, and `seconds`. Use only the configured section types. Keep section_id "
        "values unique. target_seconds and the sum of all section seconds must both be within "
        "the supplied duration_tolerance_seconds of target_duration_seconds. Use only supplied "
        "event IDs, and "
        "cover every supplied event ID in a news section. Every news section needs at least one "
        "event_id and one key_fact. Do not invent facts, sources, articles, or event IDs. Give "
        "higher-ranked events more time, and keep intro and outro short. Organize this as a daily "
        "podcast in this order: opening, today's overview, main stories, brief updates, then "
        "closing. Use only the supplied section types: put the opening and overview in intro or "
        "transition sections, use concise news sections for brief updates, and use an outro for "
        "the closing."
    ),
)
