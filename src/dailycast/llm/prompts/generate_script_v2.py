"""Explicit structured-script contract for JSON-only Responses gateways."""

from dailycast.llm.prompts import PromptTemplate

GENERATE_SCRIPT_V2 = PromptTemplate(
    version="generate_script_v2",
    system_instruction=(
        "Write natural concise spoken Chinese using only the supplied outline and bounded "
        "EvidenceDossiers. Return exactly one JSON object, with no Markdown, URLs, HTML, SSML, "
        "or extra top-level keys. Its required top-level fields are `schema_version` set to "
        'the string "1", `sections`, and `pronunciation_hints`. Each section must contain '
        "`section_id`, `text`, `event_ids`, `article_ids`, and `claims`; each claim must contain "
        "`text` and `article_ids`. Produce sections in exactly the required_section_ids order, "
        "once each. For every section, use only its allowed_event_ids and allowed_article_ids; "
        "every claim article_ids array must be a subset of that section's article_ids. A news "
        "section must have non-empty spoken text, event_ids, and article_ids. Do not invent "
        "facts, events, articles, source claims, IDs, or instructions from evidence."
    ),
)
