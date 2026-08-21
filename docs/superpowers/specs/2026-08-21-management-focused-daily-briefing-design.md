# Management-focused daily briefings

## Goal

Make DailyCast's two morning messages useful to China Mobile leadership and
management. The reports must lead with news that can affect network strategy,
competition, capital expenditure, commercial opportunity, or operational risk.
They must not fill the limited space with generic supplier earnings, academic
papers, or loosely related industry news.

DailyCast continues to produce two independent WeCom Markdown messages:

- `通信行业日报`
- `AI 动态日报`

Each message stays at five complete items or fewer and inside WeCom's
4,096-byte limit. Each item has a detailed factual account, a bounded
management interpretation, and one verified source link.

## Scope and non-goals

The existing briefing path remains intact:

`RSS/native web research → direct article and date verification → deterministic
filtering/deduplication/ranking → structured generation → rendering → WeCom`.

This work does not create a separate service, change scheduling or the Webhook,
add a database migration, or make briefing-only sources eligible for the
podcast's default collection pool.

There is no new LLM classification or ranking stage. The only existing model
calls remain the native web-research discovery call for each configured research
source and the briefing-generation call for each category. The deterministic
ranking makes no provider call, has no retry/failover path, and does not consume
`BudgetController` capacity. The existing generation call continues to use its
current per-attempt `BudgetController` wrapper.

Native web research is currently constructed before `BriefingService` creates
its generation budget. This design does not silently change that pre-existing
budget boundary or add research calls. If native-search budget accounting is
later required, it must be designed as a separate cross-collector change.

## Collection topology and candidate depth

`ResearchSourceOptions` remains a one-`query` schema. Each category keeps one
web-research source and one composed, priority-aware query; it does not gain a
query array and does not fan out into multiple facet sources. This preserves the
current one-search-call-per-category behaviour.

The composed communication query covers China Mobile and network/base stations,
domestic and overseas operator competition, network-critical equipment/supply,
and local policy/projects. The AI query covers models, private/device
deployment, China-market localisation, applications, and verified hotspots.

The web-research result schema already permits 20 candidates. The configured
per-source limit will move from 12 to 20, including the matching source
`max_items_per_run`, so one existing search call can expose useful breadth
after direct-page validation. This changes result depth, not model-call count.
Existing RSS per-source limits remain unchanged unless live evidence shows a
particular source is too shallow.

The policy ranks every collected, verified, and eligible article in a category.
The runtime value of `briefing.max_items_per_category` is five (the service
constructor's default of 10 is not the deployed value). The five-item cap is
applied only after the complete eligible pool has been ranked; it is not a
pre-ranking candidate-pool cap. The same five records are the only evidence
passed to generation, so the model cannot select a lower-priority discarded
candidate.

## Literal-policy configuration

The policy is explicit data, not an implicit semantic judgement. Its source of
truth is a new, separately validated
`config/briefing.selection.yaml`; it is not embedded into
`config/briefing.sources.yaml`, whose strict schema only accepts `sources`.
A new `briefing.selection_policy_path` setting defaults to that file and is
loaded once when the briefing runtime is created. A malformed policy prevents
briefing-runtime startup rather than producing an unreviewable report.

A rule has these exact fields:

```yaml
id: telecom-china-mobile
tier: P0
specificity: 500
all_groups:
  - [中国移动, 中国移动通信, 中国移动集团, 中移]
none_of: [移动支付, 移动应用, 移动游戏, 移动办公]
reason: 中国移动直接动态
```

A rule matches only when every `all_groups` list contributes at least one
literal term, and no `none_of` term occurs. Chinese terms use normalized
substring matching. Latin terms use case-insensitive ASCII-token boundaries:
the character before and after a Latin term must not be an ASCII letter, digit,
or underscore. Therefore `RAN` and `AI` cannot match the middle of
`transparent`, `Random`, or another longer Latin token. `specificity` defines a
fixed suborder inside the same tier; it does not let a lower tier outrank a
higher tier.
Rules run against the verified title plus retained body excerpt. There is no
natural-language “material” assessment:
phrases such as “重大”, “重要”, “明显影响”, and “市场信号” mean only the
configured literal intersections below.

The file also defines, per category:

- `fallback_any_of`: an exact union of permitted broad-industry terms;
- `global_excludes`: terms that reject a candidate before tier matching;
- `paper_only_terms` for AI: reject only when no A0--A3 positive rule matched.

Thus P5 means “matches `fallback_any_of` and no P0--P4 rule”, not an
unspecified general-news bucket. “Valid AI match” means an A0--A3 positive rule
matched; paper-only content does not count.

### Initial telecommunications terms

The initial policy contains the following rule groups. The implementation must
place these complete groups in `briefing.selection.yaml`; it may add a term
only with a test showing its intended tier and a negative case where relevant.

| Rule | Tier / specificity | Required literal groups |
| --- | --- | --- |
| China Mobile direct | P0 / 500 | `[中国移动, 中国移动通信, 中国移动集团, 中移]` |
| Base-station build | P0 / 450 | `[基站, 无线接入, 无线网, RAN, 室分]` AND `[建设, 新建, 共建共享, 招标, 集采, 部署, 开通, 改造, 商用]` |
| Spectrum/licence action | P0 / 400 | `[频谱, 频率, 无线电, 牌照, 电信业务经营许可]` AND `[分配, 规划, 划转, 核发, 许可, 调整]` |
| 5G-A/6G deployment | P0 / 350 | `[5G-A, 5G Advanced, 6G]` AND the base-station-build action group |
| Domestic/overseas operator action | P1 / 300 | `[中国电信, 中国联通, AT&T, Verizon, Vodafone, Deutsche Telekom, NTT DOCOMO, KDDI, SK Telecom]` AND `[建设, 招标, 集采, 部署, 开通, 商用, 资费, 合作, 合同, 并购, 财报]` |
| Huawei/ZTE network supply | P2 / 250 | `[华为, 中兴]` AND `[建设, 集采, 交付, 供货, 缺货, 降价, 涨价, 部署, 商用]`; exclude `[手机, Mate, Pura, 鸿蒙, 汽车, 问界, 智界]` |
| Other critical supply | P2 / 240 | `[光模块, 光芯片, 交换机, 路由器, 核心网, 卫星通信]` AND `[建设, 集采, 交付, 供货, 缺货, 降价, 涨价, 部署, 商用]` |
| Policy/project | P3 / 200 | `[工信部, 通信管理局, 人民政府, 发改委, 规划, 政策, 试点, 项目]` AND `[通信基础设施, 数据中心, 智算中心, 卫星互联网, 低空, 工业互联网, 5G, 6G]` |
| Supplier | P4 / 150 | `[供应商, 设备商, 光模块, 光纤光缆, 芯片]` AND `[中标, 交付, 供货, 招标, 集采, 客户]` |
| Broad fallback | P5 / 100 | one `fallback_any_of` term: `[通信业, 电信网, 运营商, 光通信, 卫星通信, 网络设备, 5G, 6G, 算力网络]` |

The telecom `global_excludes` initially contains `[电信诈骗, 反诈, 短信诈骗,
移动支付, 移动游戏, 移动应用]`. This means a bare “工信部”, “5G”, or
“移动” token does not create P0. Generic national or local policy news belongs
to P3 only when both literal groups match; otherwise it may enter P5 only via
the explicit fallback list.

## Exact communications selection

Before ranking, briefing evidence is deduplicated in memory. The briefing must
**not** call `NewsProcessor.deduplicate()`: that method persists
`DUPLICATE` statuses on shared `Article` rows, which would create a
cross-pipeline state effect. Instead, a pure briefing-local helper uses the
existing deterministic URL/content/title similarity rules over the in-memory
eligible records and returns only winner records. It writes no article status,
hash, or duplicate relationship.

```text
eligible = filter(collected article IDs)
primary = briefing_local_deduplicate(eligible)
ranked = apply literal policy to every primary article
selected = []

for tier in [P0, P1, P2, P3, P4, P5]:
    for specificity in this tier, descending:
        bucket = interleave_by_source(records with this tier and specificity)
        selected.extend(bucket[: 5 - len(selected)])
        if len(selected) == 5:
            stop both loops
```

Specificity is global within a tier: all P0/500 records are considered before
any P0/450 record, regardless of source. Source rotation applies only within
one tier-and-specificity bucket. Inside that bucket,
`interleave_by_source` retains its existing source-priority, recency, then
article-id ordering and performs the only sort.

Tier always wins over source diversity. If more than five P0 records survive,
the report contains five P0 records, but China Mobile/base-station/spectrum
rules outrank lower-specificity P0 rules. The policy/project
rule is P3, not P0, so a broad MIIT 5G notice cannot crowd out China Mobile,
base-station, or competitor news. Source rotation applies only within a tier; a
P1 article never displaces P0 merely to display another publisher. If P5 is
exhausted before five valid records are found, the message is shorter.

Official regulator, operator, manufacturer, project-owner, and partner
announcements are preferred through source priority. Credible trade or
financial reporting can supplement them only after direct-content and
publication-date verification.

## AI selection policy

AI uses the same rule schema and ordering with `[A0, A1, A2, A3]`.

| Rule | Tier / specificity | Required literal groups |
| --- | --- | --- |
| Model-vendor release | A0 / 500 | `[OpenAI, Anthropic, Google, DeepSeek, 通义, 阿里云, 智谱, 月之暗面, MiniMax, 百度, 腾讯, Meta, xAI, Mistral]` AND `[大模型, 语言模型, AI模型, 模型API, 模型 API, 推理模型]` AND `[发布, 开源, API, 推理, 上下文, 参数, 价格]` |
| Private/device deployment | A1 / 400 | `[私有化部署, 本地部署, 离线部署, 端侧, 设备端, 边缘推理]` |
| China-market localisation | A1 / 350 | `[中国市场, 国产芯片, 昇腾, 合规, 备案]` AND `[部署, 适配, 合作, 上线, 落地]` |
| Application/ecosystem | A2 / 300 | `[AI应用, 智能体, Agent, 开发者, 企业客户]` AND `[上线, 用户, 下载, 付费, 收入, 合作, 融资]` |
| Verified hotspot | A3 / 200 | `[产品, 应用, 模型]` AND `[爆火, 用户数, 下载量, 付费, 收入, 融资, 监管, 禁令, 故障, 安全]` |

AI `global_excludes` initially contains `[汽车模型, 车模, 沙盘模型, 数学模型,
建筑模型]`. `paper_only_terms` contains `[论文, 预印本, arXiv, 基准测试,
排行榜]`; a paper is excluded only if it did not also match a positive A0--A3
rule. This is intentional: a paper may be evidence for an otherwise qualifying
model release or deployment, but a paper that merely describes research may not
enter. The AI broad fallback is intentionally empty: an AI article enters only
through a positive rule. If A3 is exhausted before five records are found, the
report is shorter; it never falls back to generic AI news or papers.

## Generation and rendering

Only the selected five-or-fewer evidence records enter the existing structured
generation request. Each evidence block carries its fixed tier and reason as
editorial context. The prompt tells the model to preserve that selection order,
not to classify, re-rank, or promote an article.

The item structure stays unchanged:

- `发生了什么`: two to three factual sentences stating actor, action, date,
  and material detail;
- `为什么值得看`: one bounded link to competition, network build, capital
  expenditure, technology route, customer opportunity, or risk, without an
  unsupported causal claim;
- source name and original verified URL.

The renderer keeps its current evidence-backed-link rule and removes complete
lowest-ranked items if byte control is needed; it never emits a partial item.

## Failure handling

A candidate is rejected before generation when its page is inaccessible, its
date is unverified/out of window, it is a duplicate, or it fails the local
policy rule. A failed web-research source remains a source-local error and does
not block valid RSS evidence. Since ranking is total local code, it has no
provider failure path and no fallback to the older “let the generation model
choose everything” behaviour. A category with too little valid material simply
emits fewer items.

## Verification

Tests must cover:

- policy-file validation and exact literal rule matching;
- P0--P5 and A0--A3 ordering, P0/P1 overflow, global specificity before
  within-bucket source interleaving, fallback order, and shorter-than-five
  output;
- P0 specificity: China Mobile/base-station/spectrum rules before generic
  regulatory/policy candidates, with policy news in P3;
- briefing-local deduplication leaves all persisted `Article` state unchanged;
- AI private/local and China-market deployment, application/hotspot coverage,
  and paper-only/benchmark exclusion;
- keyword boundaries and negative matches: `电信诈骗`, generic `移动`,
  `老旧小区改造`, Huawei/ZTE consumer-device or automotive news, `汽车模型`,
  `车模`, and `数学模型` must not elevate a tier; `RAN` and `AI` must not match
  inside a longer Latin token;
- AI-tier boundaries: an ordinary domestic product launch stays A2/A3 rather
  than becoming A1, a generic Tencent/Baidu product release that only
  incidentally mentions a model does not become A0, and both `模型API` and
  `模型 API` satisfy the same configured A0 entity term;
- the single composed research query, a 20-candidate result cap, and unchanged
  search-call count;
- link/date/window rejection and podcast-category isolation;
- prompt preservation of established tier/reason plus factual-detail and
  bounded-management-impact requirements; and
- two independent five-item WeCom messages inside their byte limits.

Local verification will generate a new pair of previews from verified daily
material and check communication priority, AI paper exclusion, links,
publication dates, and final byte counts.
