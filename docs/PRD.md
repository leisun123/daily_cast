# DailyCast 产品需求文档（V1）

- 文档状态：可进入项目骨架设计评审
- 版本：1.0
- 日期：2026-07-22
- 适用范围：DailyCast V1

## 1. 项目背景

个人每天需要从多个新闻源中筛选值得关注的事件，但“收集、阅读、判断、写稿、合成语音、发布”是一条耗时且容易中断的链路。现有 RSS 阅读器只解决聚合问题，通用 AI 工作流产品通常不负责可靠的本地状态、音频文件、审核和长期稳定的播客 URL；一次性脚本又难以重试、审计和长期运行。

DailyCast 是一个个人自托管的 AI 新闻播客生产系统。它每天收集配置范围内的新闻，经确定性清洗、去重和聚类后，使用 LLM 完成编辑判断和中文稿件生成，使用 TTS 与 FFmpeg 产出音频，经过人工审核后发布到稳定的自托管 RSS Feed。

V1 的重点不是追求来源数量或全自动发布，而是打通一条可观察、可恢复、可审核的完整生产链路。

## 2. 目标用户

### 2.1 核心用户

单个具备基本命令行和配置文件使用能力的个人用户，例如：

- 希望每天收听特定技术、商业或产业领域新闻的人；
- 希望把个人信息源整理为私有播客的研究者、开发者或内容编辑；
- 希望用一个完整、可展示的 GitHub 项目学习 AI 应用工程的人。

### 2.2 用户能力假设

- 能安装 Docker Desktop，或在服务器上安装 Docker Compose；
- 能复制 `.env.example`、填写 API Key 和修改 YAML 配置；
- 能通过浏览器打开管理页面并完成审核；
- 不要求用户理解数据库、消息队列或模型提示词实现。

V1 是单用户系统，不提供注册、登录、组织与权限模型。若部署到公网，用户必须在反向代理层增加访问控制。

## 3. 真实使用场景

### 场景 A：每天自动生成待审节目

用户配置每天 07:00 运行。DailyCast 获取 RSS 和普通网页候选新闻，忽略过旧、正文过短或重复内容，将同一事件的多篇报道聚合，生成中文节目稿和 MP3。用户早餐时打开管理页，查看来源和入选理由，试听后批准发布。

### 场景 B：上游部分失败但仍然出刊

某个网页超时或正文提取失败。系统记录失败文章和原因，继续处理其他来源。只要仍满足最低事件数和证据质量要求，就生成带警告的待审节目；失败来源不会让整期无条件失败。

### 场景 C：TTS 中途失败后续跑

节目稿被切分为多个稳定片段。第 7 段因限流失败时，前 6 段已保存并校验。重试任务只生成失败和缺失片段，之后重新合并最终 MP3。

### 场景 D：人工修改稿件

用户发现某段措辞不适合口播，在简单文本编辑区修改稿件。系统提高 `script_revision`，清空已批准的稿件/音频版本，使旧检查结果和当前音频失效，并将 Episode 置为 `draft`。系统重新分段，按“文本 + 音色 + 语速 + Provider 配置”计算缓存键；未变化片段复用，只重建变化片段与最终合成文件。只有当前修订重新检查通过、缺失音频片段生成完成且最终 MP3 合并有效后，Episode 才重新进入 `review_required`。

### 场景 E：稳定订阅历史节目

审核通过后，系统把最终 MP3 复制为不可变公开文件，再原子更新 `feed.xml`。以后重新生成草稿或新增节目，不会改变历史 enclosure URL，也不会让订阅客户端已有节目失效。

## 4. V1 目标

1. 在个人电脑或普通 Linux 服务器上长期运行，并能通过一条 Docker Compose 命令启动。
2. 打通“采集—处理—生成—合成—审核—RSS 发布”完整闭环。
3. 任何长任务都能看到步骤、数量、耗时、错误和可重试性。
4. 任务重跑不会重复创建同一天同版本的节目，不会重复生成已命中的音频片段；显式重生成只允许替换未发布的同日 Episode。
5. 新闻源、LLM、TTS 和发布平台均有明确可替换接口。
6. 默认每日模型调用和输入 Token 有硬预算，不能将所有全文直接交给模型。
7. 仓库结构、示例配置、测试边界和文档适合作为公开 GitHub 项目展示。

## 5. 用户使用流程

### 5.1 首次配置

1. 用户复制 `.env.example` 为 `.env`，填入 LLM/TTS 密钥。
2. 用户按示例修改应用、来源、模型、语音、调度和发布配置。
3. 用户执行 `docker compose up -d`。
   Compose 中 Uvicorn 在容器内监听 `0.0.0.0:8000`，默认端口映射为 `127.0.0.1:8000:8000`；不得把容器内监听地址设为 `127.0.0.1`，否则宿主机端口映射无法访问。
4. 用户在宿主机打开 `http://127.0.0.1:8000` 管理页，检查 `/readyz`、来源启用状态和下次运行时间。非 Docker 本地开发同样默认监听 `127.0.0.1:8000`，localhost HTTP 可用于开发。
5. 用户可测试单个来源，或手动触发当天任务。

公网 Feed 和 media 必须通过明确配置的反向代理、只读静态目录或显式端口暴露；发布 RSS 不会自动匿名公开管理页面或管理 API。任何非 loopback 的正式公开 Feed/media 地址必须使用 HTTPS。

### 5.2 日常生产

1. APScheduler 按指定时区触发，或用户手动触发。
2. 系统创建唯一业务键对应的任务运行，依次执行并持久化步骤结果。
3. 系统创建或更新当天草稿节目，最终进入 `review_required`。
4. 用户查看事件、文章来源、入选理由、稿件、检查结果和音频。
5. 用户可修改标题、简介或稿件，也可重生成指定环节或单个音频片段。approved 节目的标题/简介变化只撤销批准并回到 `review_required`；稿件或有效音频发生变化时 Episode 立即回到 `draft`，重新检查与合并成功后再进入 `review_required`。
6. 用户批准节目，随后显式执行发布。
7. RSS Publisher 发布不可变 MP3 并原子更新 Feed，节目进入 `published`。

Alpha 示例可将 `editorial.enforce_quality_gate=false` 与 `publishing.auto_publish=true` 组合使用：仍保存 validation/review finding，但对结构有效的节目自动生成、批准并发布。严格模式保持上述人工审核流程；两种模式都不会绕过 JSON/schema、引用、数据库、TTS、FFmpeg 或文件系统错误。

### 5.3 失败恢复

1. 用户从任务详情看到失败步骤、错误分类、已完成检查点和是否可重试。
2. 对可重试失败，系统可在步骤内有限自动重试；仍失败时任务终止。
3. 用户点击“从失败处重试”，系统创建有父子关系的新 TaskRun。
4. 新任务验证输入版本和已有产物，只重做失败步骤及其失效的下游步骤；需要写入私有 editorial artifact 的下游阶段始终写到子任务目录，模型与音频仅通过已验证缓存复用，绝不覆盖父任务产物。

## 6. 功能需求

### FR-1 新闻源管理

- 支持启用、停用、查看和修改来源。
- V1 提供两类采集器：标准 RSS 与基于 CSS 选择器的普通 HTML 列表页。
- 示例来源为 `https://news.ycombinator.com/rss`（RSS）与 `https://blog.python.org/`（普通网页列表页）；示例用于说明配置，正式启用前应由来源测试功能验证页面结构。
- 来源配置包括名称、类型、入口 URL、优先级、发布时间窗口、请求超时和类型特有参数。
- 单个来源失败必须隔离并记录，不阻塞其他来源。
- V1 不内置大量站点专用适配器，也不默认使用 Playwright 抓新闻。

### FR-2 新闻采集与正文提取

- 保存标题、原始 URL、规范 URL、来源、发布时间、摘要、正文、抓取时间和提取状态。
- RSS 中已有足够正文时可直接使用，否则通过 `httpx + trafilatura` 提取目标页正文。
- HTML 列表源使用配置化选择器发现文章，正文仍通过统一提取器处理。
- 对超时、限流、DNS、HTTP 状态、正文为空和解析异常使用不同错误码。
- 默认禁止访问 loopback、私网、link-local 和非 HTTP(S) 地址，重定向后再次校验。

### FR-3 确定性过滤与基础去重

- 规范化 URL，移除片段、默认端口和已知追踪参数。
- 按规范 URL 哈希、规范标题哈希和正文哈希执行精确去重。
- 在有限时间窗内使用 SimHash 或字符 n-gram Jaccard 做近重复识别。
- 在 LLM 之前按发布时间、正文长度、语言、来源启用状态和来源优先级过滤。
- 被过滤或判重的文章保留记录及原因，便于审计。

### FR-4 事件聚类

- 仅对通过确定性过滤的当期候选做聚类。
- V1 使用标题、摘要和正文前部的 TF-IDF 字符 n-gram 余弦相似度，并施加发布时间窗。
- 同一事件可关联多篇不同来源文章；选择一篇证据最完整、来源优先级最高的代表文章。
- 聚类是普通代码职责，不依赖 LLM、向量数据库或外部嵌入服务。

### FR-5 AI 编辑与生成

- 通过 OpenAI API 兼容 Provider 调用模型，配置 `base_url`、`api_key`、`model`、`timeout`、`temperature`、可选 `top_p`、`max_output_tokens`、response format/structured output mode、其他受支持的模型选项和输入限制。
- LLM 完成事件评分与理由、节目大纲、口播稿、标题简介以及基于证据包的最终语义检查。
- 所有步骤使用版本化提示词和结构化 JSON 输出；响应必须经过 schema 校验。
- schema 校验成功的结构化结果按 `operation + provider + model + prompt_version + schema_version + generation_config_hash + input_hash` 持久化为 LLMArtifact；任务恢复和新任务都可精确复用。
- `generation_config_hash` 是影响模型输出语义的非敏感配置 canonical JSON 的 SHA-256，至少覆盖脱敏规范化后的 Provider endpoint 身份、temperature、可选 top_p、max output tokens、response format/structured output mode 和其他模型语义选项；API Key、Authorization、timeout 和 retry 次数不得参与或被保存。上述任一配置变化都必须 cache miss。
- LLMArtifact 不保存密钥、Authorization、未经限制的完整原始 Prompt 或全部新闻正文；失败和未通过 schema 校验的响应不能进入缓存。
- 模型只接收事件卡片和入选事件的有限证据包，不接收所有原始全文。
- 每期设置最大候选事件数、最大入选事件数、单事件字符数、总输入 Token 和模型调用次数。
- 超过预算时系统先执行确定性裁剪；仍无法满足则以明确错误结束，不静默无限调用。

### FR-6 稿件检查

- 普通代码检查结构、总长度、段落 ID、来源引用、禁止格式、数字是否能在证据中找到、预计时长和空段落。
- LLM 检查表达是否适合中文口播、是否存在证据未支持的事实、重复段落和明显矛盾。
- 检查结果分为 `pass`、`revise`、`human_review`；最多允许一次自动修订，之后必须进入人工审核并展示问题。
- 检查不能声称完成了互联网事实核查；它只核对系统保存的来源证据。

### FR-7 TTS 与音频合成

- V1 默认实现 OpenAI 兼容 TTS Provider；Provider 接口允许以后增加 Edge TTS 或其他服务。
- 按自然段和句子边界确定性分段，每段不超过 Provider 限制。
- 支持音色、语速、模型和输出格式配置。
- 每段独立重试、保存、校验和缓存；支持只重生成一个片段。
- 音频缓存身份必须包含 Provider 实现/endpoint 等非敏感语义配置的 `provider_config_hash`，以及发音词典、金融数字规则和增强断句模式的 `tts_preprocess_hash`；并显式包含 provider、model、voice、speed、format、分段器版本和规范文本；密钥、timeout 与 retry 次数不参与缓存身份。
- FFmpeg 将通过校验的片段按固定编码参数合并为最终 MP3，并使用临时文件加原子重命名。

### FR-8 节目审核

- 生成完成后默认进入 `review_required`，禁止自动发布。
- `draft` 表示当前稿件修订、检查结果或草稿音频至少一项无效；`review_required` 表示三者全部有效并等待人工批准。
- 管理页显示事件入选理由、关联文章、来源链接、稿件修订、检查问题和执行日志。
- 支持在线试听、修改标题和简介、用简单文本区修改稿件、批准节目、发布节目。
- 支持重生成大纲、稿件、元数据、全部音频或单个音频片段。
- 人工修改稿件必须增加 `script_revision`，清空批准版本和旧检查结果，使当前音频失效并进入 `draft`。重新检查、生成缺失片段并合并成功后才回到 `review_required`。
- 从 `approved` 仅撤销批准且不修改稿件、检查或音频时回到 `review_required`；修改稿件、音色、语速、TTS model 或有效音频片段时回到 `draft`。
- `published` Episode 不允许修改或重生成。
- V1 无复杂富文本编辑器和应用内权限系统。

### FR-9 自托管 RSS 发布

- 发布前要求节目状态为 `approved` 且音频、元数据和校验均有效。
- 将音频复制到公开静态目录中的不可变路径，生成稳定绝对 URL。
- Feed 符合 RSS 2.0 基本格式，包含稳定 GUID、enclosure URL、MIME、长度、发布时间和节目简介。
- 每次更新先生成临时文件并校验，再原子替换 `feed.xml`。
- 发布时从数据库读取既有 `published` Publications，并把本次已验证、仍为 `publishing` 的 Publication 作为候选 item 显式注入内存 Feed 模型；Feed 原子替换成功后，才在短事务中把 Publication 和 Episode 标记为 `published`。
- 若进程在 Feed 替换后、数据库提交前崩溃，reconcile 必须识别 Feed 中已有的相同 GUID 和正确 enclosure，补写 `published` 状态，不重复添加 item，也不复制或覆盖已存在的不可变音频。
- 保留历史节目；已发布音频不得原地覆盖。

### FR-10 任务调度、执行与日志

- 支持手动和 Cron 定时运行，时区显式配置。
- 记录 collecting、extracting、deduplicating、clustering、ranking、outlining、scripting、checking、synthesizing、assembling、reviewing 和 publishing 等步骤。
- 每步记录开始/结束时间、状态、尝试号、输入/输出数量、错误摘要、错误码、是否可重试和检查点。
- 支持步骤内有限重试，以及从失败步骤创建新的续跑任务。
- 同一业务键同一时刻最多一个活动任务；同一天默认复用同一节目。
- 支持任务总超时、外部调用超时、结构化日志和敏感字段脱敏。
- API/调度器必须先提交 queued TaskRun 到 SQLite，再加入进程内有界队列；启动时恢复 queued，并把心跳过期的 running 任务判定为 interrupted 后续跑。
- SIGTERM 时停止领取和提交新任务，允许当前步骤在关闭期限内提交安全检查点；超时退出后由下一次启动依据心跳恢复。
- V1 不引入分布式任务队列；部署时只运行一个应用进程和一个调度器实例。

### FR-11 配置与密钥

- 普通配置使用 YAML，按应用、新闻源种子、LLM、TTS、调度和发布分区。新闻源种子仅在空库/缺少该 slug 时导入；首次导入后 SQLite Source 是管理页面和 API 的运行时真相源，重启不以 YAML 覆盖人工修改。
- 密钥、Cookie、密码只从环境变量读取，YAML 中使用环境变量名称引用。
- 仓库提供 `.env.example`、不含密钥的示例配置和本地开发说明。
- 启动时校验配置，错误时 fail fast 并输出不含秘密的字段路径。

### FR-12 管理 API 与页面

- FastAPI 提供版本化 JSON 管理接口、健康检查、静态音频和 RSS。
- Jinja2 + HTMX 管理页面仅调用应用服务或管理 API，不把业务逻辑写在 route。
- 长操作返回 `202 Accepted + TaskRun`，页面轮询任务状态。

### FR-13 数据库 schema 管理

- V1 使用 Alembic 管理 SQLite schema revision，仓库包含 `alembic.ini`、`migrations/env.py`、模板和初始 revision。
- 空数据库只通过 `alembic upgrade head` 创建；正常应用启动不得静默调用 `Base.metadata.create_all()`。
- 应用启动时读取数据库当前 revision 并与代码 head 比较。缺失、落后或超前时不启动 scheduler/Task Executor，`/readyz` 返回未就绪及 current/expected revision；除 `/healthz`、`/readyz` 等诊断端点外，所有管理页面、业务读写 API、Feed 和由应用提供的 media 路由均不可用，避免在旧 schema 上执行不安全读取。
- Docker Compose 在启动应用进程前显式执行 `alembic upgrade head`；migration 失败时容器启动失败，不运行应用处理任务。
- migration 测试必须覆盖 `foreign_keys=ON`、活动 TaskRun partial unique index、JSON check、LLMArtifact 的唯一键/外键，以及 Article/NewsEvent 循环外键的插入顺序。

## 7. 非功能需求

### 7.1 可运行性

- 目标环境：2 核 CPU、4 GB 内存、10 GB 可用磁盘的普通设备。
- 一条 `docker compose up -d` 命令启动应用；V1 Compose 只包含 DailyCast 服务，可选反向代理不作为强依赖。
- 使用 Python 3.12、单个 Uvicorn worker；SQLite 使用 WAL、foreign keys 和 busy timeout。

### 7.2 性能与规模边界

- 默认设计规模：最多 20 个来源、每天 300 篇原始文章、100 篇合格候选、30 个候选事件、8 个入选事件、30 分钟音频。
- 对默认规模，除去上游限流等待，日任务目标在 30 分钟内进入待审核状态。
- 管理列表接口必须分页，单页默认 20、最大 100。
- V1 不以高并发为目标；同一时间只运行一个重型流水线。

### 7.3 可靠性与恢复

- 进程重启后，超过心跳阈值的 `running` 任务标记为中断并允许续跑。
- 已完成步骤和音频片段必须持久化后再推进状态。
- SQLite 和公开媒体目录应能独立备份；文档给出一致性备份步骤。
- 不承诺高可用，但单个来源或单个文章失败不应造成系统崩溃。

### 7.4 成本可控

- 默认每期最多 30 个事件进入 LLM 评分、最多 8 个事件入选。
- 默认每期 LLM 调用硬上限 12 次，输入预算 60,000 tokens；具体值可配置但必须有上限。
- 只有 `operation + provider + model + prompt_version + schema_version + generation_config_hash + input_hash` 全部相同，才能复用通过 schema 校验的成功结果；Provider endpoint 身份、模型生成参数或任一其他身份字段变化均视为 cache miss，重试不得重复消耗已命中的成功结果。
- TTS 以包含 `provider_config_hash` 和 `tts_preprocess_hash` 的完整片段缓存键复用；Provider 实现/endpoint、音色、语速、模型、格式、发音词典或其他影响音频的参数变化均 cache miss，避免错误复用和不必要的整期重合成。

### 7.5 安全与隐私

- 不在日志、任务配置快照或 API 响应中输出 API Key、Authorization、Cookie。
- 新闻 URL 访问有 SSRF 防护、响应大小上限和超时。
- 非 Docker 本地开发的管理接口默认绑定 loopback；Compose 中容器监听 `0.0.0.0:8000`，但宿主机仅映射 `127.0.0.1:8000:8000`。公网管理访问必须另行显式配置 TLS 和外部认证代理，公开 Feed/media 不得顺带暴露管理接口。
- HTML 只作为文本提取输入，不在管理页直接渲染不可信源码。

### 7.6 可维护性与可测试性

- 外部 HTTP、LLM、TTS、时钟、文件和 Publisher 均可替换为测试实现。
- 纯规则逻辑与 FastAPI、SQLAlchemy Session、APScheduler 解耦。
- 单元测试不访问网络；集成测试使用临时 SQLite 和临时目录；端到端测试使用 Fake LLM/TTS。
- 关键 schema、状态迁移、幂等键、Feed 和音频缓存行为必须有自动化测试。

## 8. 非目标

V1 明确不做：

- 多用户、注册登录、应用内权限、计费和 SaaS；
- 移动端 App、实时新闻推送、视频播客和社交媒体内容生成；
- 多 Agent 协作、向量知识库、向量数据库和复杂数据分析；
- Redis、Celery、Kafka、Elasticsearch、Kubernetes 或微服务拆分；
- 大量站点专用抓取器、绕过反爬或非官方逆向接口；
- 验证码自动绕过、非官方逆向发布接口，以及除受控网易云 Playwright adapter 以外的自动化平台发布；
- 把 Dify 作为 V1 运行时、调度器、数据库或音频处理器；
- 在同一实例并行生成多期重型任务，或提供商业级高可用。

## 9. V1 验收标准

### AC-1 安装与启动

- 新用户按文档准备 `.env` 和 YAML 后，可以用一条 Compose 命令启动。
- Compose 启动日志能够证明先成功执行 `alembic upgrade head`，再启动应用；空数据库由初始 migration 创建，应用本身不调用 `create_all()`。
- `/healthz` 返回进程存活；配置、SQLite revision 与代码 head 一致、媒体目录和 FFmpeg 可用时 `/readyz` 返回就绪。revision 不匹配时 `/readyz` 必须返回 503，且所有非诊断业务端点返回 `503 DATABASE_REVISION_MISMATCH`。
- Compose 容器内 Uvicorn 监听 `0.0.0.0:8000`，默认使用 `127.0.0.1:8000:8000` 端口映射；宿主机可访问管理页，但局域网/公网不能默认访问。非 Docker 开发默认监听 `127.0.0.1:8000`。

### AC-2 完整流水线

- 使用至少一个 RSS 示例源和一个 HTML 示例源，能够产生文章记录、事件、待审节目、分段音频和最终 MP3。
- 自动任务结束于 `review_required`，未经批准无法发布。
- 在 Alpha 放宽质量门槛配置下，质量 finding 与 review verdict 必须保留在 artifact 中，但结构有效节目可以自动生成、批准并发布；严格模式仍必须在 review_required 等待人工批准。
- 所有 LLM 输出均经结构化 schema 校验，所有最终稿段落可追溯到事件和文章。

### AC-3 去重与成本

- 同一规范 URL、同一正文或近重复文章不会作为多个独立事件进入评分。
- 测试能证明评分输入是限长事件卡片而非全部全文。
- 达到调用或 Token 硬预算时任务以明确状态停止，不继续调用模型。
- 自动化测试证明 LLMArtifact 只保存通过 schema 校验的成功输出，完整缓存身份相同可跨 TaskRun 复用；provider、model、Prompt、schema、`generation_config_hash` 或输入任一变化均不能误命中，且 generation config 不包含秘密、timeout 或 retry 配置。

### AC-4 故障隔离与恢复

- 一个来源或少量页面提取失败时，满足最低候选阈值仍可生成节目，并展示警告。
- TTS 在中间片段失败后重试只请求失败/缺失片段。
- 进程中断后能够从持久化检查点续跑，不重复创建 Episode。
- QueueFull 后已提交的 queued TaskRun 会由 SQLite 扫描重新投递；单个任务或 worker loop 异常不应杀死唯一 worker，`/readyz` 必须在 supervisor/worker 不健康时返回未就绪。
- deadline 到期任务写入 `timed_out/TASK_DEADLINE_EXCEEDED`，保留已完成 checkpoint；TaskRun/TaskStep 保存实际 LLM 调用/Token 与 TTS 请求字符，并保留稳定错误码和 retryable 分类。

### AC-5 审核与局部再生

- 用户能查看来源、入选理由、稿件、音频和步骤日志，并修改标题/简介。
- 修改稿件后 Episode 立即进入 `draft`，批准字段、旧检查和当前音频引用失效；只有重新检查、片段生成和最终合并完成后才进入 `review_required`。
- 只有完整缓存键改变的片段重新生成；未变片段校验后复用。测试必须证明 `provider_config_hash` 和 `tts_preprocess_hash` 参与身份，Provider 实现/endpoint、voice、开场/结尾速度、model、format、发音词典或其他音频语义参数变化时不能命中旧片段。
- 仅撤销批准但不改变内容和音频时，Episode 从 `approved` 回到 `review_required`，不进入 `draft`。
- 用户可单独重生成指定音频片段。

### AC-6 RSS 发布

- 只有 `approved` 节目能发布；发布后可通过稳定 URL 获取 MP3 和 `feed.xml`。
- Feed 通过项目内 RSS 结构校验，并保留历史节目。
- 重跑发布不会产生重复 item，历史 enclosure URL 和 GUID 不改变；测试覆盖当前 `publishing` candidate 被写入新 Feed，以及 Feed 已替换但数据库尚未提交时由 reconcile 补写 `published` 状态。
- 启用网易云目标后，RSS 先完成不可变资产与 Feed，再由独立 `PublicationTarget` 尝试 Playwright 上传；登录、验证码、上传或页面失配写为 `needs_attention`，不回滚 Episode/RSS，也不重新生成新闻、稿件或音频。

### AC-7 审计与安全

- TaskRun/TaskStep 能展示每步时间、数量、状态、错误和可重试性。
- API、结构化日志和配置快照中不存在真实密钥、Cookie 或 Authorization 值。
- 自动化测试不依赖真实外部 API Key。
- migration 集成测试覆盖 foreign keys、partial unique index、JSON check、Article/NewsEvent 循环外键以及包含 `generation_config_hash` 的 LLMArtifact 完整唯一约束。
- migration 与运行时测试还覆盖 TaskStep 非负 TTS 用量、RSS 同一 external_id URL 变化冲突、Responses JSON-object fallback 的二次预算预留，以及草稿 MP3 不位于 `PUBLIC_DIR`。

## 10. 后续路线图

### V1.1：运维与来源扩展

- 增加来源测试报告、失败率统计、备份/恢复命令和可配置保留策略。
- 增加一个动态网页采集器，但仍要求每个适配器有隔离的契约测试。
- 增加兼容 S3 的 `MediaStore`，保持公开 URL 与 Publisher 接口不变。

### V1.2：AI/TTS Provider 扩展

- 增加 Edge TTS Provider 和第二个 OpenAI 兼容模型配置。
- 增加 `DifyWorkflowProvider`，仅替换评分/大纲/稿件等 AI 操作，不接管本地调度、数据、审核和音频。
- 建立固定新闻样本的离线评估，比较提示词版本的选题一致性、事实支持率和口播质量。

### V2.0：API 平台发布

- 实现 `PodbeanAPIPublisher` 或其他有官方 API 的 Publisher。
- Publication 保存远端节目 ID、发布 URL、请求幂等键和脱敏响应摘要。

### V2.1：更多受控平台发布

- 保持 RSS 为不可变媒体的源头；为具备稳定官方 API 的平台优先增加 API Publisher。
- 网易云 adapter 已使用独立持久化浏览器配置目录和人工登录；登录失效、验证码、风控或页面选择器失配时进入 `needs_attention`，不尝试绕过安全机制。
- RPA 仍只消费已批准的 Episode 和最终音频，不参与新闻理解或内容生成。
