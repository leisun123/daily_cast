# Detailed split daily briefings

## Goal

Readers should understand each selected event without opening the source article.
The existing direct source link remains evidence and optional further reading, not
the place where the basic facts live.

## Delivery shape

DailyCast continues to render and push two independent Markdown messages:

- `通信行业日报`
- `AI 动态日报`

Each message has its own WeCom 4,096-byte limit and contains at most five
items. The categories are never merged merely to conserve message space.

## Per-item content

`发生了什么` becomes a factual two-sentence summary, targeted at roughly
80–100 Chinese characters and capped at 110. It must state the actor, action,
the material detail such as a metric, scope, product stage, or decision, and
the current result or next state when present in evidence.

`为什么值得看` remains a distinct one-sentence interpretation, capped at 55
characters. It explains the reader impact and must not restate the factual
summary. The model may use only the collected evidence; the renderer continues
to resolve every displayed URL against that evidence.

## Size safety

Five detailed entries at the specified caps remain inside the 4,000-byte
rendering budget under normal direct-article URL lengths. Tests will exercise
five full-length entries and assert the result stays below the budget. If the
content still cannot fit, the item count is reduced before rendering rather
than cutting a partially written final item.

## Verification

Update schema and prompt tests for the new limits and two-sentence factual
requirement. Regenerate local telecom and AI previews with verified source
material, check both reports have no more than five items, preserve only
evidence-backed links, and measure each Markdown body against the WeCom limit.
