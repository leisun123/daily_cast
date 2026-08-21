# DailyCast 网页研究日报 P0 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Preserve the current dirty branch and do not introduce a second service, database, scheduler, or deployment.

**Goal:** 只提升 DailyCast 的通信和 AI 文字日报质量。在现有 DailyCast 内部通过 OpenAI Responses 网页搜索发现当日候选，由服务器逐条验证可访问的原文，写入已有 Article 表，再交给既有 BriefingService、Markdown renderer 与企业微信 webhook。

**Architecture:** 增加一个仅供 briefing 配置使用的 SourceKind.WEB_RESEARCH collector。通信与 AI 各有一个 Source，保证现有 briefing_category 分组仍是确定的。OpenAIResponsesLLMProvider 增加一个窄的原生 web_search 方法，复用当前模型、API key、HTTP 客户端、超时和重试；模型 URL 只是候选，SafeHttpFetcher 与 ContentExtractor 才是唯一证据入口。Podcast 的来源选择、调度、任务快照、新闻处理、脚本与 TTS 均不修改。

**Tech Stack:** 现有 Python、FastAPI、SQLAlchemy/Alembic、HTTPX、Trafilatura、OpenAI Responses-compatible API、Pydantic、pytest、Ruff、Mypy、Zeabur。没有 Anthropic、Agent SDK、独立 worker 或新 webhook。

---

## P0 边界与固定决策

- 只接入 BriefingService。两个网页研究来源只写入 config/briefing.sources.yaml，继承已有 briefing_category，因此 SourceCollectionService 的现有 briefing-only 排除规则会继续让 Podcast 完全看不到它们。
- 使用当前 primary llm.provider: openai_responses。网页搜索请求在同一个 Responses endpoint 发送 tools: [{type: web_search}] 并要求结构化 JSON 候选输出。
- 当前网关若不支持 native web_search，来源返回 WEB_RESEARCH_UNSUPPORTED；不改用 DeepSeek fallback、RSSHub、搜索结果页、浏览器或伪造数据。DeepSeek fallback 继续只服务已有日报写作。
- 网页搜索只用于发现。模型返回的链接必须由 DailyCast 服务器完成安全 DNS/SSRF 检查、逐跳重定向、2xx、HTML、正文和发布日期验证；失败链接不会进入 Article、更不会出现在 WeCom。
- 网页研究文章必须从原文页的 article:published_time、JSON-LD datePublished 或明确 time datetime 提取日期，并落在当前 collection window。没有可验证日期的项标记 MISSING_PUBLICATION_DATE 并丢弃；现有 RSS/HTML-list 的未标日期策略不变。
- 研究开关默认关闭，避免本地开发产生模型调用；Zeabur 测试配置显式开启。关闭时 collector 返回成功空集合，不访问模型或网络，也不造成 warning。
- 日报最终条数仍由现有 max_items_per_category: 5 和 WeCom 4,096-byte renderer 上限决定；单源候选上限只限制请求与验证工作量。
- 不推送、不部署、不提交或改动 Podcast；当前工作区中的既有改动保持原样。

## P0 数据流

    OpenAI Responses + web_search
                 |
                 v
    ResearchCollector（通信或 AI 候选）
                 |
                 v
    SafeHttpFetcher -> ContentExtractor -> 日期/挑战页验证
                 |
                 v
    ArticleService / Article 表
                 |
                 v
    BriefingService -> 现有 Markdown renderer -> WeCom

模型输出不能绕过本地验证；WeCom 链接使用验证后的最终文章 URL。

## Task 1: 增加网页研究 SourceKind 与可迁移数据库约束

**Files:**

- Modify: src/dailycast/db/models.py — SourceKind
- Create: migrations/versions/0008_web_research_source_kind.py
- Modify: tests/test_alembic_revision.py
- Modify: tests/integration/test_database_schema.py

- [ ] 先写迁移测试：从 0007_publication_targets 升级后，插入 kind = web_research 的 Source 成功；已有 rss、html_list Source 和关联 Article 不丢失；回滚时旧约束恢复。
- [ ] 在 SourceKind 添加 WEB_RESEARCH = web_research；不把网页搜索伪装成 RSS 或 HTML list。
- [ ] 新建 0008 Alembic 迁移。SQLite 的 sourcekind 是带 CHECK constraint 的 enum，使用 op.batch_alter_table("sources", recreate="always") 重建 kind enum constraint，同时保留主键、唯一索引、ix_sources_enabled_priority 和 ix_sources_kind。降级时如果仍有 web_research 行，应明确拒绝降级，不能无声删除新闻记录。
- [ ] 更新 Alembic head 断言并做真实插入验证，避免 ORM 枚举已更新但 SQLite 仍拒绝新值。

**验收：**

- alembic upgrade head 在空 SQLite 和已有 0007 数据库上成功。
- 旧文章、旧 Source、现有索引无变化，新 kind 可持久化。

## Task 2: 配置仅供日报使用的网页研究源

**Files:**

- Modify: src/dailycast/core/config.py
- Modify: config/briefing.sources.yaml
- Modify: config/app.example.yaml, config/app.yaml, config/zeabur.yaml
- Modify: src/dailycast/sources/bootstrap.py
- Modify: tests/test_config.py, tests/test_sources.py

- [ ] 先写 settings 测试：max_candidates_per_source 限制在 1–20；search_context_size 只接受 low、medium、high；enabled=false 时 collector 零模型调用、零 HTTP 调用。
- [ ] 添加 Settings.web_research，字段为 enabled=false、max_candidates_per_source=12、max_search_calls_per_source=1、search_context_size=medium、max_article_chars=12000。web_research 不进入 Podcast 的配置快照，因为 P0 不改播客。
- [ ] 只向 config/briefing.sources.yaml 添加两个种子：
  - openai-web-research-telecom：briefing_category=telecom，第一方监管/运营商/设备商优先的通信 query；
  - openai-web-research-ai：briefing_category=ai，产品、模型、芯片、具名公司公告优先的 AI query。
  两项均使用 kind: web_research、entry_url: research://telecom 或 research://ai、priority 95、require_verified_publication_date=true。不得写入 config/sources.example.yaml。
- [ ] 默认 app 配置保持 web_research.enabled=false；现有 Zeabur 测试配置显式 true。密钥仍只来自 DAILYCAST_LLM__API_KEY，YAML 不增加任何 secret。
- [ ] 修改 source bootstrap：_normalized_entry_url 接受 SourceKind。仅 WEB_RESEARCH 可以使用严格 research://topic 内部 URI（无账号、端口、路径、query、fragment）；其他种类保持 HTTP(S) 或已有 rsshub:// 规则。该 URI 只用于 Source identity，ResearchCollector 永不抓取它。

**验收：**

- Briefing runtime seed 后两个源进入已有 briefing source allowlist；基础 Podcast source 配置没有新来源。
- 无效 research URI、在 RSS 源使用 research URI、或重复 internal URI 都在启动前拒绝。

## Task 3: 在当前 OpenAI Responses provider 上增加网页搜索能力

**Files:**

- Modify: src/dailycast/llm/contracts.py
- Modify: src/dailycast/llm/providers/openai_responses.py
- Modify: src/dailycast/core/errors.py
- Modify: src/dailycast/core/lifespan.py
- Modify: tests/test_llm.py

- [ ] 先写 MockTransport wire test。断言 web research 请求复用当前 /responses endpoint、Authorization、模型、temperature、timeout、retry client，并在结构化 output format 外增加 tools: [{type: web_search}] 与 tool_choice: auto。
- [ ] 定义窄的 WebResearchProvider protocol，输入 LLMMessage、Pydantic schema 与非秘密 options，输出 StructuredResult。不要把 LLMProvider、LLMArtifactService、缓存或所有 fallback provider 改为必须支持搜索。
- [ ] 给 OpenAIResponsesLLMProvider 实现 generate_web_research。复用 _request_payload、_post、_parse_response 和 JSON-object fallback contract；只额外附加 search tool、search context。Responses output 中的 tool-call 项被忽略，最终 output_text JSON 仍由现有 parser 提取。
- [ ] 搜索 tool 返回 400/422/未实现时映射到 LLMWebSearchUnsupportedError；认证、超时、429、5xx 沿用现有失败语义。禁止将该错误转发到 DeepSeek fallback。
- [ ] 调整 lifespan 构造顺序：共享 httpx client 与 primary direct provider 先建立；编辑使用的 failover provider 后建立；仅 primary 为 OpenAIResponsesLLMProvider 时将它注入 ResearchCollector，否则注入明确 unavailable adapter。其他生命周期资源及 Podcast pipeline 代码不改行为。

**验收：**

- 原有结构化写作/failover 测试不变。
- 不支持 web_search 时仅产生可诊断的来源错误，量子位、36Kr、工信部、C114 和现有简报写作仍工作。

## Task 4: 实现本地验证优先的 ResearchCollector

**Files:**

- Create: src/dailycast/sources/research.py
- Modify: src/dailycast/sources/contracts.py
- Modify: src/dailycast/sources/extraction.py
- Modify: src/dailycast/sources/service.py
- Modify: src/dailycast/core/lifespan.py
- Modify: tests/test_research_source.py（或 tests/test_sources.py）

- [ ] 先写失败测试：禁用时零调用；超过候选上限拒绝；非 HTTP URL、搜索页、社媒页不抓取；每个合格候选都经安全抓取；最终 redirect URL 与本地正文才会入库；一条失败仅产生 source-local warning；全部失败不阻断另一类别日报。
- [ ] 在 research.py 定义严格 DTO：
  - WebResearchCandidate(title, url, publisher, finding, published_at_hint)
  - WebResearchCandidateSet(candidates)，数目不超过 source/settings 的较小值
  - ResearchSourceOptions(query、topic、first-party preference、require_verified_publication_date)，extra=forbid
- [ ] Prompt 只搜索当前 collection window 中与本主题直接相关的重要事件。第一方原文优先；媒体可用于发现但应返回原始公告/文章；禁止搜索结果页、聚合页、社媒、登录页、营销页和无日期旧文。模型只返回 JSON 候选，不能写日报结论。
- [ ] ResearchCollector.collect 的固定顺序是：检查开关 -> 解析 source config -> 仅调用一次 WebResearchProvider -> 验证候选 URL 基础形态 -> ContentExtractor 实际抓取 -> 验证原文发布日期及 window -> 创建 ArticleCandidate。所有候选单独隔离失败。
- [ ] ArticleCandidate 增加可选 fetched_at 与 http_status；ArticleService.upsert_candidate 在新增/合并时保存真正的服务器验证时间与状态。metadata_json 保存 discovery_method、模型名、request id、候选原 URL、publisher、验证时间和 final URL；不保存网页搜索 snippet、完整模型响应、密钥或 webhook。
- [ ] 扩展 ExtractedArticle/ContentExtractor 的日期识别：按 article:published_time、JSON-LD datePublished、time datetime 的固定优先级解析为 UTC；对验证码/反爬挑战、空正文、无日期、窗口外日期返回稳定 SourceError。每跳 URL 安全、2xx、大小、超时仍只由现有 SafeHttpFetcher 执行。
- [ ] 在 SourceCollectionService collector map 注册 SourceKind.WEB_RESEARCH。不要改 _enabled_sources 或 Podcast 的 briefing-only 排除逻辑；P0 研究源只存在于 briefing config，故 Podcast 不会选择它。

**验收：**

- 任何展示或持久化的 research article 都有 DailyCast 服务器验证过的正文、日期和最终 URL。
- 同一类别一条链接失败不会阻止该类别剩余链接或另一类别的日报。
- 现有 source collection tests 证明 Podcast 默认来源集合未变化。

## Task 5: 复用既有日报写作、渲染和推送，并验证 P0

**Files:**

- Modify: src/dailycast/briefing/service.py（只在需要暴露来源 warning/计数时）
- Modify: README.md, docs/api.md
- Modify: tests/test_briefing_service.py, tests/test_briefing.py

- [ ] 写 briefing service integration test：两个 research source 各自返回一篇本地验证文章；BriefingService 通过现有 briefing_category 分别构建通信/AI evidence；使用 final URL 和正文而非模型 finding/snippet。
- [ ] 保持 BriefingService 的现有双护栏：configured source IDs 加 briefing_category。研究源只由 config/briefing.sources.yaml 进入；任意默认 Podcast 源不会进入企业微信日报。
- [ ] 不改 BriefingResult schema、生成 prompt、renderer、WebhookNotifier、企业微信消息格式或 4,096-byte 截断策略。只确认成功文章能与既有 RSS 证据一起参与五条以内的选择。
- [ ] 文档明确：需要官方 OpenAI Responses web-search 权限；开关为 DAILYCAST_WEB_RESEARCH__ENABLED；它不是 RSSHub/Google/Bing 抓取，也无需 Anthropic key。解释 WEB_RESEARCH_UNSUPPORTED、MISSING_PUBLICATION_DATE、ACCESS_CHALLENGE 的可观察含义。
- [ ] 增加演练测试：研究候选经过 final URL 验证后才渲染 Markdown；断言失败链接不出现、链接是 final URL、WeCom 内容不超过 4,096 bytes。

**验收命令：**

    alembic -c alembic.ini upgrade head
    env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY -u all_proxy -u https_proxy -u http_proxy .venv/bin/ruff check src tests
    env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY -u all_proxy -u https_proxy -u http_proxy .venv/bin/mypy src
    env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY -u all_proxy -u https_proxy -u http_proxy .venv/bin/pytest -q

## P0 完成定义

在不改 webhook 地址的现有测试环境中，应用 0008、开启 DAILYCAST_WEB_RESEARCH__ENABLED 并让当前 OpenAI key 允许 web_search 后，调用已有日报生成入口一次。通过条件是：

1. 通信、AI 各自可输出 0–5 条，但任意展示条目都由 DailyCast 从服务器抓到最终正文页；
2. 链接可重新抓取为正文，不能是搜索页、登录页或验证码页；
3. WeCom Markdown 在 4,096 bytes 内；
4. 网页搜索不可用或单个链接被拒绝时，现有稳定 RSS 依然可构成日报；
5. Podcast 的来源、调度与音频流程没有代码或行为变化。

不在 P0：任何 Podcast 搜索复用、音频调度改动、独立 Zeabur 服务、Claude/Anthropic、headless browser、A/B webhook 或取消 RSS 来源。日报稳定后再单独评估。
