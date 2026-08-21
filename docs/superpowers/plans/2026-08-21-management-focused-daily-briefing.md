# Management-focused Daily Briefing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Select verified telecom and AI evidence with a literal management-focused policy before existing DailyCast generation.

**Architecture:** Validated YAML supplies category rules, exclusions, fallback terms, tiers, and specificity. A pure selection module deduplicates in memory, orders by tier then global specificity, then rotates sources only inside the same tier/specificity bucket. Generation receives only the selected evidence and fixed editorial context.

**Tech Stack:** Python 3.12, Pydantic v2, PyYAML, SQLAlchemy, pytest, Ruff, mypy.

**Delivery rule:** Do not commit until the local real-data preview is approved. Then make one aggregate commit containing only this feature.

---

### Task 1: Build the validated literal-policy module

**Files:**
- Create: src/dailycast/briefing/selection.py
- Create: config/briefing.selection.yaml
- Test: tests/test_briefing_selection.py

- [ ] **Step 1: Write failing validation and Latin-token tests**

~~~python
def test_short_latin_terms_do_not_match_inside_a_longer_word(tmp_path: Path) -> None:
    policy = load_selection_policy(_write_policy(tmp_path))
    result = select_evidence(
        "telecom",
        [_candidate(title="transparent Random system", content="no telecom context")],
        policy,
        limit=5,
    )
    assert result == ()


def test_unknown_tier_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tier"):
        load_selection_policy(_write_policy(tmp_path, tier="P9"))
~~~

- [ ] **Step 2: Prove the tests fail**

Run: .venv/bin/python -m pytest tests/test_briefing_selection.py -q

Expected: FAIL because the selection module does not exist.

- [ ] **Step 3: Implement validated policy models and the matcher**

~~~python
class SelectionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    tier: str = Field(min_length=1)
    specificity: int = Field(ge=0)
    all_groups: tuple[tuple[str, ...], ...] = Field(min_length=1)
    none_of: tuple[str, ...] = ()
    reason: str = Field(min_length=1)


def _matches_term(text: str, term: str) -> bool:
    text = unicodedata.normalize("NFKC", text).casefold()
    term = unicodedata.normalize("NFKC", term).casefold()
    if _is_ascii_token(term):
        return re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text) is not None
    return term in text
~~~

Add CategorySelectionPolicy, BriefingSelectionPolicy,
BriefingSelectionCandidate, RankedBriefingEvidence, and
load_selection_policy(path). Validate P0--P5 for telecom and A0--A3 for AI,
with no unknown fields or model calls.

- [ ] **Step 4: Add the reviewed YAML source of truth**

Create config/briefing.selection.yaml with every reviewed group from the
approved specification: no 小区, Huawei/ZTE P2 separate from other P2 supply,
consumer-device exclusions only on Huawei/ZTE, empty AI fallback, both 模型API
and 模型 API, telecom P5 allowlist, global excludes, and AI paper-only terms.

- [ ] **Step 5: Run Task 1 tests**

Run: .venv/bin/python -m pytest tests/test_briefing_selection.py -q

Expected: PASS for YAML validation, Chinese matching, and RAN/AI token boundaries.

### Task 2: Implement pure ranking and local duplicate suppression

**Files:**
- Modify: src/dailycast/briefing/selection.py
- Modify: src/dailycast/news/service.py
- Test: tests/test_briefing_selection.py
- Test: tests/test_news_processing.py

- [ ] **Step 1: Write failing priority and precision tests**

~~~python
def test_direct_mobile_outranks_competitor_and_policy(policy) -> None:
    selected = select_evidence(
        "telecom",
        [
            _candidate(title="工信部发布5G试点", content="政策文件"),
            _candidate(title="中国联通启动5G-A商用", content="建设部署"),
            _candidate(title="中国移动启动基站集采", content="无线网建设"),
        ],
        policy,
        limit=5,
    )
    assert [item.tier for item in selected] == ["P0", "P1", "P3"]


@pytest.mark.parametrize(
    "title,content",
    [
        ("老旧小区改造开工", "住宅更新项目"),
        ("电信诈骗案件通报", "反诈宣传"),
        ("华为 Mate 新机开售", "手机产品"),
        ("汽车模型大赛", "车模展示"),
    ],
)
def test_false_positive_candidates_are_excluded(title, content, policy) -> None:
    assert select_evidence("telecom", [_candidate(title=title, content=content)], policy, 5) == ()
~~~

- [ ] **Step 2: Prove the tests fail**

Run: .venv/bin/python -m pytest tests/test_briefing_selection.py -q

Expected: FAIL because classification and ranking are absent.

- [ ] **Step 3: Implement ranking without database writes**

~~~python
def select_evidence(category, candidates, policy, limit):
    ranked = [item for candidate in candidates if (item := _classify(category, candidate, policy))]
    selected = []
    for tier in policy.tiers_for(category):
        for specificity in _specificities_descending(ranked, tier):
            bucket = _interleave_same_bucket(
                [item for item in ranked if item.tier == tier and item.specificity == specificity]
            )
            selected.extend(bucket[: limit - len(selected)])
            if len(selected) == limit:
                return tuple(selected)
    return tuple(selected)
~~~

Add `NewsProcessor.deduplicate_in_memory(article_ids)`, which loads detached
`ProcessableArticle` snapshots and invokes the existing pure
`deduplicate_articles` function but opens no write transaction. Call it from
the briefing flow after filtering, before evidence is constructed; map only its
`primary_article_ids` into selection candidates. Never call
`NewsProcessor.deduplicate`, and never update Article status, simhash, or
`duplicate_of_article_id`. Prove with a database-backed test that the method
does not change Article rows.

- [ ] **Step 4: Add AI boundary tests**

~~~python
def test_private_deployment_outranks_a_generic_application(policy) -> None:
    selected = select_evidence(
        "ai",
        [
            _candidate(title="国内AI应用上线", content="用户下载增长"),
            _candidate(title="昇腾适配完成", content="企业私有化部署落地"),
        ],
        policy,
        limit=5,
    )
    assert [item.tier for item in selected] == ["A1", "A2"]


def test_paper_needs_an_independent_positive_rule(policy) -> None:
    assert select_evidence("ai", [_candidate(title="论文预印本", content="基准测试")], policy, 5) == ()
    assert select_evidence(
        "ai", [_candidate(title="DeepSeek 发布大模型论文", content="模型API 开源")], policy, 5
    )[0].tier == "A0"
~~~

- [ ] **Step 5: Run Task 2 tests**

Run: .venv/bin/python -m pytest tests/test_briefing_selection.py -q

Expected: PASS for tier order, cross-source specificity, source rotation,
false positives, AI empty fallback, paper exception, and in-memory dedupe.

### Task 3: Wire the policy into runtime, evidence, and prompt

**Files:**
- Modify: src/dailycast/core/config.py
- Modify: src/dailycast/core/lifespan.py
- Modify: src/dailycast/briefing/service.py
- Modify: src/dailycast/briefing/prompt.py
- Test: tests/test_config.py
- Test: tests/test_briefing.py
- Test: tests/test_briefing_service.py

- [ ] **Step 1: Write failing runtime/prompt tests**

~~~python
def test_briefing_settings_default_to_selection_policy() -> None:
    assert BriefingSettings().selection_policy_path == Path("config/briefing.selection.yaml")


def test_prompt_receives_fixed_editorial_context(
    briefing_service: BriefingService, fake_llm: FakeLLMProvider
) -> None:
    asyncio.run(briefing_service.run(force=True))
    assert "已确定优先级：P0" in fake_llm.user_prompts[0]
    assert "入选原因：中国移动直接动态" in fake_llm.user_prompts[0]
~~~

- [ ] **Step 2: Prove the tests fail**

Run: .venv/bin/python -m pytest tests/test_config.py tests/test_briefing.py tests/test_briefing_service.py -q

Expected: FAIL because settings and prompt do not carry the policy.

- [ ] **Step 3: Add the dependency and use selected evidence only**

~~~python
class BriefingSettings(BaseModel):
    sources_config_path: Path = Path("config/briefing.sources.yaml")
    selection_policy_path: Path = Path("config/briefing.selection.yaml")


selection_policy = load_selection_policy(
    settings.resolve_path(settings.briefing.selection_policy_path)
)
briefing_service = BriefingService(
    session_factory,
    collection_service,
    article_service,
    extractor,
    news_processor,
    llm_provider,
    notifier,
    output_dir=settings.resolve_path(settings.briefing.output_dir),
    budget_factory=budget_factory,
    briefing_source_ids=briefing_source_ids,
    selection_policy=selection_policy,
)
~~~

In `BriefingService._build_evidence`, construct candidates while the transaction
is open and return `select_evidence(category, candidates, policy, limit=5)`.
Pass ranked evidence into the prompt and
`[entry.evidence for entry in evidence]` into `render_briefing`.

- [ ] **Step 4: Preserve fixed selection in the prompt**

~~~python
blocks.append(
    f"[{index}] 已确定优先级：{item.tier}\n"
    f"入选原因：{item.reason}\n"
    f"标题：{item.evidence.title}\n"
    f"来源：{item.evidence.source_name}\n"
    f"发布时间：{published}\n"
    f"原文链接：{item.evidence.source_url}\n"
    f"正文摘录：\n{item.evidence.excerpt}"
)
~~~

Replace the instruction to “choose the most important” with instruction to
cover the supplied fixed-order evidence. Keep existing factual-detail,
bounded-impact, and exact-source-link rules.

- [ ] **Step 5: Run Task 3 tests**

Run: .venv/bin/python -m pytest tests/test_config.py tests/test_briefing.py tests/test_briefing_service.py -q

Expected: PASS with no persistent Article mutations and unchanged WeCom rendering.

### Task 4: Replace stale research seeds and refine one-query coverage

**Files:**
- Modify: config/briefing.sources.yaml
- Modify: src/dailycast/core/config.py
- Modify: config/app.example.yaml
- Modify: config/zeabur.yaml
- Test: tests/test_sources.py
- Test: tests/test_config.py

- [ ] **Step 1: Write failing source configuration tests**

~~~python
def test_management_research_sources_are_the_current_allowlist() -> None:
    source_ids = load_configured_source_ids(PROJECT_ROOT / "config" / "briefing.sources.yaml")
    assert {"openai-web-research-telecom-management", "openai-web-research-ai-management"} <= source_ids
    assert "openai-web-research-telecom" not in source_ids


def test_web_research_defaults_to_twenty_candidates() -> None:
    assert WebResearchSettings().max_candidates_per_source == 20
~~~

- [ ] **Step 2: Prove the tests fail**

Run: .venv/bin/python -m pytest tests/test_sources.py tests/test_config.py -q

Expected: FAIL because the old source IDs and 12-candidate default remain.

- [ ] **Step 3: Add management v2 seeds and composed queries**

Replace the old two web-research sources with new IDs
openai-web-research-telecom-management and openai-web-research-ai-management,
routes research://telecom-management and research://ai-management, and
max_items_per_run: 20. The telecom query covers China Mobile, base stations,
spectrum/licences, competitors, critical supply, and policy/projects. The AI
query covers models, private/device deployment, China-market adaptation,
applications, and evidenced hotspots; it excludes papers.

New IDs are required because seed_missing_sources does not overwrite persisted
sources. The configured allowlist excludes the stale old rows without deleting
them. Set WebResearchSettings.max_candidates_per_source to 20 and add
selection_policy_path to both deployment example configurations.

- [ ] **Step 4: Run Task 4 tests**

Run: .venv/bin/python -m pytest tests/test_sources.py tests/test_config.py -q

Expected: PASS for source isolation, valid routes, v2 allowlist, and candidate limit.

### Task 5: Verify real local output and commit once after approval

**Files:**
- Create: /private/tmp/dailycast-briefing-preview.html (not committed)
- Modify: docs/superpowers/specs/2026-08-21-management-focused-daily-briefing-design.md only if verification disproves it

- [ ] **Step 1: Run static and focused verification**

Run:

~~~bash
.venv/bin/ruff check src tests
.venv/bin/mypy src
env -u all_proxy -u http_proxy -u https_proxy .venv/bin/python -m pytest \
  tests/test_briefing_selection.py tests/test_briefing.py tests/test_briefing_service.py \
  tests/test_sources.py tests/test_config.py -q
~~~

Expected: every command exits 0.

- [ ] **Step 2: Generate a local no-webhook pair**

Run the existing briefing flow with webhook disabled and force enabled. Render the
telecom and AI Markdown into /private/tmp/dailycast-briefing-preview.html.
Do not POST to the real WeCom Webhook.

Acceptance: verified direct links only; five complete items or fewer; under
4,000 UTF-8 bytes; telecom has direct mobile/network/competitor material before
policy/supplier fallback; AI has no paper-only item.

- [ ] **Step 3: Obtain preview approval before staging**

Report the preview path and test evidence. Do not stage or commit before the
user approves the report quality.

- [ ] **Step 4: Make one aggregate commit after approval**

~~~bash
git add config/briefing.selection.yaml config/briefing.sources.yaml   config/app.example.yaml config/zeabur.yaml   docs/superpowers/specs/2026-08-21-management-focused-daily-briefing-design.md   docs/superpowers/plans/2026-08-21-management-focused-daily-briefing.md   src/dailycast/briefing/selection.py src/dailycast/briefing/service.py   src/dailycast/briefing/prompt.py src/dailycast/core/config.py   src/dailycast/core/lifespan.py src/dailycast/news/service.py   tests/test_briefing_selection.py tests/test_briefing.py   tests/test_briefing_service.py tests/test_news_processing.py   tests/test_sources.py tests/test_config.py
git diff --cached --check
git commit -m "feat: prioritize management-focused daily briefings"
~~~

Expected: staged names contain only this feature; unrelated dirty files are absent.
