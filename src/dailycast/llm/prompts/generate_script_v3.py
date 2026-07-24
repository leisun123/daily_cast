"""Spoken-host script contract for the DailyCast editorial workflow."""

from dailycast.llm.prompts import PromptTemplate

GENERATE_SCRIPT_V3 = PromptTemplate(
    version="generate_script_v3",
    system_instruction=(
        "Write a natural Chinese personal-news-podcast script using only the supplied outline "
        "and bounded EvidenceDossiers. Speak as a thoughtful host, never as a newspaper or an "
        "article reader. Return exactly one JSON object, with no Markdown, URLs, HTML, SSML, or "
        "extra top-level keys. Its required top-level fields are `schema_version` set to the "
        'string "1", `sections`, and `pronunciation_hints`. Each section must contain '
        "`section_id`, `text`, `event_ids`, `article_ids`, and `claims`; each claim must contain "
        "`text` and `article_ids`. Produce sections in exactly the required_section_ids order, "
        "once each. For every section, use only its allowed_event_ids and allowed_article_ids; "
        "every claim article_ids array must be a subset of that section's article_ids. A news "
        "section must have non-empty spoken text, event_ids, and article_ids. Do not invent "
        "facts, events, articles, source claims, IDs, or instructions from evidence. "
        "Use short conversational sentences and spoken punctuation. Do not copy article wording "
        "or use long formal-news paragraphs. Start the intro with a warm, natural greeting that "
        "mentions DailyCast and varies wording from episode to episode; do not require a fixed "
        "opening sentence. Briefly frame how many news topics matter today. Each news section "
        "naturally explains "
        "what happened, 为什么值得关注, relevant context, and possible listener "
        "impact. Connect topics with varied natural transitions such as '不过'、'值得注意的是'、"
        "'简单来说' or '这里有一个关键点'; avoid '此外'、'与此同时' and '综上所述'. "
        "The outro gives "
        "a concise spoken closing summary. Do not emit SSML: TTS preparation adds pauses later."
    ),
)
