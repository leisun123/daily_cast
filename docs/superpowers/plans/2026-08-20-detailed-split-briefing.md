# Detailed Split Daily Briefings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver separate, evidence-backed telecom and AI briefings whose items explain events without requiring readers to open the source.

**Architecture:** Preserve the existing `telecom` and `ai` category loop and two independent WeCom Markdown payloads. Extend the structured editorial contract for a detailed factual summary, then make the Markdown renderer include only complete item blocks that fit the 4,000-byte safety budget.

**Tech Stack:** Python 3.12, Pydantic, pytest, existing DailyCast Markdown renderer and WeCom webhook integration.

---

## File structure

- `src/dailycast/briefing/schemas.py`: editorial field and item-count limits.
- `src/dailycast/briefing/prompt.py`: evidence-only two-sentence factual-summary instructions.
- `src/dailycast/briefing/renderer.py`: complete-item byte-budget enforcement.
- `src/dailycast/core/config.py`: maximum item count per category.
- `tests/test_briefing.py`: schema, prompt, renderer, and byte-budget tests.
- `tests/test_config.py`: default item-cap test.
- `tests/test_briefing_service.py`: independent two-message delivery test.

### Task 1: Define the detailed editorial contract

**Files:**

- Modify: `tests/test_briefing.py:39-85`
- Modify: `src/dailycast/briefing/schemas.py:11-48`
- Modify: `src/dailycast/briefing/prompt.py:30-50`

- [ ] **Step 1: Write failing contract tests**

~~~python
def test_briefing_item_allows_a_detailed_factual_summary() -> None:
    detailed_summary = "甲" * 110
    item = _item("https://news.example.test/a", summary=detailed_summary)
    assert item.summary == detailed_summary


def test_briefing_item_rejects_content_above_delivery_budget() -> None:
    with pytest.raises(ValueError, match="summary"):
        _item("https://news.example.test/a", summary="甲" * 111)
    with pytest.raises(ValueError, match="why_it_matters"):
        _item("https://news.example.test/a", why_it_matters="乙" * 56)


def test_briefing_result_rejects_more_than_five_items() -> None:
    items = [_item(f"https://news.example.test/{index}").model_dump() for index in range(6)]
    with pytest.raises(ValueError, match="items"):
        BriefingResult.model_validate({"overview": "概览。", "items": items})
~~~

Give `_item()` an optional `summary` keyword parameter so it can build the 110-character boundary case.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `.venv/bin/pytest tests/test_briefing.py -k 'detailed_factual or delivery_budget or more_than_five' -q`

Expected: FAIL because the current summary cap is 60 and the result allows 12 items.

- [ ] **Step 3: Implement the bounded schema and prompt**

~~~python
# src/dailycast/briefing/schemas.py
MAX_BRIEFING_ITEMS = 5

class BriefingItem(BaseModel):
    headline: str = Field(min_length=1, max_length=28)
    summary: str = Field(min_length=1, max_length=110)
    why_it_matters: str = Field(min_length=1, max_length=55)

class BriefingResult(BaseModel):
    overview: str = Field(min_length=1, max_length=120)
~~~

Replace the current prompt text for `summary` with:

~~~python
"summary（发生了什么，2句、80-100字为宜、最多110字；必须说明主体、动作、"
"关键数字/范围/阶段中的可用信息，以及当前结果或下一步；只能复述证据中的事实）、"
~~~

Keep `why_it_matters` to one sentence, cap it at 55 characters, and require it not to repeat `summary`.

- [ ] **Step 4: Verify and commit Task 1**

Run: `.venv/bin/pytest tests/test_briefing.py -k 'detailed_factual or delivery_budget or more_than_five or prompt' -q`

Expected: PASS.

~~~bash
git add src/dailycast/briefing/schemas.py src/dailycast/briefing/prompt.py tests/test_briefing.py
git commit -m "feat: expand daily briefing event summaries"
~~~

### Task 2: Keep whole detailed items inside the WeCom budget

**Files:**

- Modify: `tests/test_briefing.py:88-175`
- Modify: `src/dailycast/briefing/renderer.py:17-59`

- [ ] **Step 1: Write failing renderer tests**

~~~python
def test_renderer_keeps_five_detailed_items_inside_the_wecom_budget() -> None:
    evidence = [_evidence(source_url=f"https://news.example.test/{index}") for index in range(5)]
    result = BriefingResult(
        overview="今日多项通信与AI基础设施进展进入实质交付阶段。",
        items=[_item(item.source_url, summary="甲" * 110, why_it_matters="乙" * 55) for item in evidence],
    )
    markdown = render_briefing("通信行业日报", date(2026, 8, 20), result, evidence)
    assert markdown.count("**0") == 5
    assert len(markdown.encode("utf-8")) <= RENDER_BYTE_BUDGET


def test_renderer_omits_an_oversized_item_without_cutting_the_next_item() -> None:
    oversized_url = "https://news.example.test/" + "a" * 3900
    evidence = [_evidence(source_url=oversized_url), _evidence(source_url="https://news.example.test/kept")]
    result = BriefingResult(overview="概览。", items=[_item(oversized_url), _item("https://news.example.test/kept")])
    markdown = render_briefing("通信行业日报", date(2026, 8, 20), result, evidence)
    assert oversized_url not in markdown
    assert "https://news.example.test/kept" in markdown
    assert not markdown.endswith("…（内容过长，已截断）")
~~~

- [ ] **Step 2: Run the renderer tests and verify they fail**

Run: `.venv/bin/pytest tests/test_briefing.py -k 'five_detailed or oversized_item' -q`

Expected: FAIL because the current renderer emits all blocks and a later function may slice the tail.

- [ ] **Step 3: Implement item-aware rendering**

Refactor `render_briefing()` to build item blocks first. Add an internal helper that takes the title, date, overview, and accepted blocks, then renders the heading with `今日精选 · {len(blocks)} 条`. Before accepting a block, render the candidate complete message and check:

~~~python
if len(candidate_markdown.encode("utf-8")) <= RENDER_BYTE_BUDGET:
    accepted_blocks.append(block)
~~~

For every rejected over-budget block, continue to later items. Use `len(accepted_blocks) + 1` for displayed numbering. Return the complete helper result directly, retaining existing labels and the evidence-resolved link, rather than applying `truncate_markdown()` to normal generated content.

- [ ] **Step 4: Verify and commit Task 2**

Run: `.venv/bin/pytest tests/test_briefing.py -q`

Expected: PASS, including existing fabricated-link fallback and duplicate-link tests.

~~~bash
git add src/dailycast/briefing/renderer.py tests/test_briefing.py
git commit -m "fix: keep detailed briefing items within WeCom budget"
~~~

### Task 3: Cap configuration and prove two independent messages

**Files:**

- Modify: `tests/test_config.py`
- Modify: `tests/test_briefing_service.py:137-240`
- Modify: `src/dailycast/core/config.py:96-104`

- [ ] **Step 1: Write failing configuration and service tests**

~~~python
def test_briefing_defaults_to_five_items_per_category() -> None:
    assert BriefingSettings().max_items_per_category == 5


def test_briefing_rejects_more_than_five_items_per_category() -> None:
    with pytest.raises(ValueError, match="max_items_per_category"):
        BriefingSettings(max_items_per_category=6)
~~~

Extend `test_briefing_run_generates_pushes_and_persists_every_category` with five evidence-backed articles and five structured items for each category. Assert `len(notifier.pushed) == 2`; one message starts with `# 通信行业日报`, the other with `# AI 动态日报`; and both have encoded size at most `RENDER_BYTE_BUDGET`.

- [ ] **Step 2: Run and verify the new tests fail**

Run: `.venv/bin/pytest tests/test_config.py tests/test_briefing_service.py -k 'five_items_per_category or generates_pushes' -q`

Expected: FAIL because `BriefingSettings` currently defaults to 10 and allows 6.

- [ ] **Step 3: Implement the configuration ceiling**

~~~python
# src/dailycast/core/config.py
class BriefingSettings(BaseModel):
    max_items_per_category: int = Field(default=5, ge=1, le=5)
~~~

Leave `config/app.example.yaml` at its existing value of 5. Do not merge the category loop or independent webhook calls.

- [ ] **Step 4: Verify and commit Task 3**

Run: `.venv/bin/pytest tests/test_config.py tests/test_briefing_service.py -k 'five_items_per_category or generates_pushes' -q`

Expected: PASS.

~~~bash
git add src/dailycast/core/config.py tests/test_config.py tests/test_briefing_service.py
git commit -m "feat: cap each detailed briefing at five items"
~~~

### Task 4: Final regression check and local two-message preview

**Files:**

- Modify: `/private/tmp/dailycast-briefing-preview.html` (generated local preview only)

- [ ] **Step 1: Run final automated checks**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY .venv/bin/pytest tests/test_briefing.py tests/test_briefing_service.py tests/test_config.py -q`

Expected: all checks pass.

- [ ] **Step 2: Generate and audit both previews**

Use GLM with `thinking: {"type": "disabled"}` only as the editor of verified evidence. Generate one `通信行业日报` and one `AI 动态日报`, each with no more than five items. Before updating the HTML, assert:

~~~python
assert len(markdown.encode("utf-8")) <= RENDER_BYTE_BUDGET
assert markdown.count('<font color="comment">发生了什么</font>') <= 5
assert "https://" in markdown
~~~

Verify each included source page and collection-window date. Do not call the WeCom webhook. Update the local HTML with two `.message` articles, then parse it with `html.parser` to assert each article has no more than five `.story` sections and every visible source link is absolute HTTPS.

- [ ] **Step 3: Report the preview**

Give the two item counts and byte counts, link the local preview, and state explicitly that no enterprise-WeChat message was sent.
