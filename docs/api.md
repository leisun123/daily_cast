# DailyCast V1 管理 API 设计

- 文档状态：接口设计完成，不包含实现
- Base path：`/api/v1`
- 内容类型：管理 API 使用 `application/json`；音频与 RSS 除外

## 1. 通用约定

### 1.1 成功响应

单对象：

```json
{
  "data": {},
  "meta": {
    "request_id": "req_..."
  }
}
```

列表：

```json
{
  "data": [],
  "meta": {
    "request_id": "req_...",
    "page": 1,
    "page_size": 20,
    "total": 42
  }
}
```

异步命令返回 `202 Accepted` 和 TaskRun 摘要：

```json
{
  "data": {
    "task_run_id": "2b7bd1fe-5319-4c01-8c62-08cfb5d66ea1",
    "task_type": "daily_generate",
    "business_key": "daily:2026-07-22:daily:v1",
    "status": "queued",
    "episode_id": 17,
    "created_at": "2026-07-21T23:00:00Z",
    "links": {
      "self": "/api/v1/task-runs/2b7bd1fe-5319-4c01-8c62-08cfb5d66ea1"
    }
  },
  "meta": {"request_id": "req_..."}
}
```

### 1.2 错误响应

```json
{
  "error": {
    "code": "EPISODE_STATE_CONFLICT",
    "message": "Only an approved episode can be published",
    "retryable": false,
    "details": {
      "current_status": "review_required",
      "required_status": "approved"
    }
  },
  "meta": {"request_id": "req_..."}
}
```

统一状态码：

| HTTP | 场景 |
|---:|---|
| 200 | 查询、同步更新或幂等重复命令返回已有结果 |
| 201 | 同步创建 Source |
| 202 | 已接受长任务 |
| 204 | 无响应体的停用/取消成功 |
| 400 | 业务参数组合无效 |
| 404 | 资源不存在 |
| 409 | 状态冲突、乐观锁冲突、幂等键复用但请求不同、活动任务冲突 |
| 412 | `If-Match` 与当前 `lock_version` 不匹配 |
| 422 | JSON/schema/字段校验失败 |
| 429 | 本地任务/预算并发限制；上游 429 不直接透传给管理 API |
| 500 | 应用内部错误或已记录资产损坏 |
| 502 | 来源诊断收到无效上游响应 |
| 503 | readiness 依赖不可用、磁盘/FFmpeg/数据库不可用 |
| 504 | 来源诊断在受限超时内未完成 |

数据库 revision 与代码 head 不一致时，只保留 `/healthz`、`/readyz` 等不依赖业务 schema 的诊断端点；管理页面以及所有业务读取、写入、Feed 和应用托管 media 接口统一返回 `503 DATABASE_REVISION_MISMATCH`，且不启动 scheduler/Task Executor。不能在旧 schema 下执行“只读但仍依赖表结构”的查询。

### 1.3 分页、排序和时间

- `page` 默认 1；`page_size` 默认 20，最大 100。
- 排序使用白名单字段，格式 `sort=-created_at`；不接受任意 SQL 字段。
- 时间为 UTC RFC 3339；`episode_date` 为配置时区中的 `YYYY-MM-DD`。
- 所有 URL 字段返回绝对公开 URL或 API 相对路径，不能返回主机文件路径。

### 1.4 幂等键

`Idempotency-Key` 是最长 128 字符的非空 ASCII 字符串：

- 相同键 + 相同规范请求：返回已有 TaskRun/Publication，不重复执行；
- 相同键 + 不同请求 fingerprint：`409 IDEMPOTENCY_KEY_REUSED`；
- 键的存储和唯一性由 TaskRun/Publication 保证。

只对会创建长任务或外部副作用的命令强制要求该 header。普通 PATCH 使用 `If-Match` 乐观锁；天然状态幂等的 approve 不要求 Idempotency-Key。

### 1.5 认证边界

V1 不实现用户认证。非 Docker 本地开发默认监听 `127.0.0.1:8000`；Compose 中 Uvicorn 在容器内监听 `0.0.0.0:8000`，但端口只映射为宿主机 `127.0.0.1:8000:8000`，因此管理 API 默认仍仅供本机访问。`/feed.xml` 与 `/media/...` 只有通过明确配置的反向代理、静态目录或显式端口暴露才成为公开资源；不得因此匿名公开管理 API。localhost 开发可使用 HTTP，非 loopback 正式公开 Feed/media 必须使用 HTTPS；公网管理访问还必须配置外部认证。

## 2. 公共对象摘要

### 2.1 SourceSummary

```json
{
  "id": "hacker-news-rss",
  "name": "Hacker News",
  "kind": "rss",
  "entry_url": "https://news.ycombinator.com/rss",
  "enabled": true,
  "priority": 60,
  "language": "en",
  "last_success_at": "2026-07-21T23:01:00Z",
  "last_error": null,
  "updated_at": "2026-07-21T12:00:00Z"
}
```

### 2.2 TaskRunSummary

```json
{
  "id": "2b7bd1fe-5319-4c01-8c62-08cfb5d66ea1",
  "task_type": "daily_generate",
  "trigger_type": "scheduled",
  "status": "running",
  "current_step": "synthesizing",
  "episode_id": 17,
  "progress": {"completed_steps": 8, "total_steps": 11},
  "warning_count": 2,
  "started_at": "2026-07-21T23:00:00Z",
  "ended_at": null,
  "retryable": false
}
```

### 2.3 EpisodeSummary

```json
{
  "id": 17,
  "public_id": "55c2448e-49c0-4db4-a2e1-3f7010284c17",
  "episode_date": "2026-07-22",
  "edition": "daily",
  "status": "review_required",
  "title": "DailyCast：今日技术新闻",
  "description": "本期关注……",
  "script_revision": 2,
  "audio_version": 2,
  "duration_ms": 842000,
  "audio_url": "/api/v1/episodes/17/audio",
  "published_at": null,
  "lock_version": 5
}
```

## 3. 健康检查

### 3.1 进程存活

- 方法/路径：`GET /healthz`
- 参数：无。
- 返回：`200`，`{"status":"ok","version":"...","time":"..."}`。
- 常见错误：进程无法响应时由基础设施判断失败；该接口不访问数据库或外部 API。
- 幂等键：不需要。

### 3.2 应用就绪

- 方法/路径：`GET /readyz`
- 参数：无。
- 返回：

```json
{
  "status": "ready",
  "checks": {
    "config": "ok",
    "database": "ok",
    "database_revision": {
      "status": "ok",
      "current": "0001_initial_schema",
      "expected": "0001_initial_schema"
    },
    "private_storage": "ok",
    "public_storage": "ok",
    "ffmpeg": "ok",
    "scheduler": "ok"
  }
}
```

- 常见错误：`503 NOT_READY`，details 中列出失败检查。数据库 revision 缺失、落后或超前时返回 `DATABASE_REVISION_MISMATCH` 及 current/expected；此时除 health/readiness 诊断端点外，scheduler、Task Executor、管理页面以及所有业务读写/Feed/media 接口均不可用。不得在此调用 LLM/TTS 或新闻源。
- 幂等键：不需要。

## 4. 新闻源管理

### 4.1 来源列表

- 方法/路径：`GET /api/v1/sources`
- 查询参数：`enabled:boolean?`、`kind:rss|html_list?`、`page`、`page_size`、`sort`。
- 返回：分页 `SourceSummary[]`。
- 常见错误：`422 INVALID_QUERY`。
- 幂等键：不需要。

### 4.2 创建来源

- 方法/路径：`POST /api/v1/sources`
- 请求：

```json
{
  "id": "python-blog-html",
  "name": "Python Insider",
  "kind": "html_list",
  "entry_url": "https://blog.python.org/",
  "enabled": true,
  "priority": 50,
  "language": "en",
  "request_timeout_seconds": 20,
  "max_items_per_run": 30,
  "config": {
    "item_selector": "article, .date-outer",
    "link_selector": "h3 a, h2 a",
    "title_attribute": "text",
    "url_attribute": "href"
  }
}
```

- 返回：`201` + Source 详情。
- 常见错误：`409 SOURCE_ID_EXISTS`、`409 SOURCE_URL_EXISTS`、`422 SOURCE_CONFIG_INVALID`、`422 URL_NOT_ALLOWED`。
- 幂等键：不需要；`id` 与规范入口 URL 唯一。重复创建明确返回 409，不伪装成功。

### 4.3 来源详情

- 方法/路径：`GET /api/v1/sources/{source_id}`
- 路径参数：`source_id` slug。
- 返回：完整 Source，含类型配置（不含秘密）、最近状态和统计摘要。
- 常见错误：`404 SOURCE_NOT_FOUND`。
- 幂等键：不需要。

### 4.4 修改来源

- 方法/路径：`PATCH /api/v1/sources/{source_id}`
- 请求：创建字段的任意子集；`id` 不可修改。
- 条件请求：建议 `If-Match: "<updated_at-or-version>"`；V1 Source 可使用 `updated_at` ETag。
- 返回：`200` + 修改后 Source。
- 常见错误：`404 SOURCE_NOT_FOUND`、`409 SOURCE_URL_EXISTS`、`412 VERSION_MISMATCH`、`422 SOURCE_CONFIG_INVALID`、`422 URL_NOT_ALLOWED`。
- 幂等键：不需要；PATCH 设置绝对值且由 ETag 防止覆盖。

### 4.5 停用来源

- 方法/路径：`DELETE /api/v1/sources/{source_id}`
- 参数：无；语义是设置 `enabled=false`，不删除历史 Article。
- 返回：`204`。重复停用仍返回 `204`。
- 常见错误：`404 SOURCE_NOT_FOUND`。
- 幂等键：不需要，操作天然幂等。

### 4.6 测试来源

- 方法/路径：`POST /api/v1/sources/{source_id}/test`
- 请求：`{"max_items": 3, "extract_content": true}`。
- 返回：`200`，包含发现条数、最多 3 个标题/规范 URL、提取长度、耗时和脱敏错误；测试结果不创建 Article。
- 常见错误：`404 SOURCE_NOT_FOUND`、`422 SOURCE_CONFIG_INVALID`、`502 SOURCE_FETCH_FAILED`、`504 SOURCE_TIMEOUT`。
- 幂等键：不需要；这是受限诊断，不持久化业务副作用。

## 5. 任务运行

### 5.1 手动触发每日任务

- 方法/路径：`POST /api/v1/task-runs`
- Header：`Idempotency-Key` 必填。
- 请求：

```json
{
  "task_type": "daily_generate",
  "episode_date": "2026-07-22",
  "edition": "daily"
}
```

`episode_date` 可省略并按应用时区取当天；`edition` 默认 `daily`。若要同日第二期，用户必须显式使用不同 edition，而不是 `force` 绕过唯一性。

- 返回：新任务 `202`；相同幂等请求返回已有 TaskRun（运行中为 `202`，终态为 `200`）。若同 business key 已有其他活动任务，`409` 返回其 `task_run_id`。
- 常见错误：`409 TASK_ALREADY_RUNNING`、`409 IDEMPOTENCY_KEY_REUSED`、`422 INVALID_EPISODE_DATE`、`429 PIPELINE_CAPACITY_EXCEEDED`、`503 NOT_READY`。
- 幂等键：必须。

### 5.2 任务历史

- 方法/路径：`GET /api/v1/task-runs`
- 查询参数：`status?`、`task_type?`、`episode_id?`、`created_from?`、`created_to?`、`page`、`page_size`、`sort=-created_at`。
- 返回：分页 `TaskRunSummary[]`。
- 常见错误：`422 INVALID_QUERY`。
- 幂等键：不需要。

### 5.3 任务详情

- 方法/路径：`GET /api/v1/task-runs/{task_run_id}`
- 查询参数：`include_steps:boolean=true`。
- 返回：TaskRun 完整字段和步骤：

```json
{
  "data": {
    "id": "...",
    "task_type": "daily_generate",
    "business_key": "daily:2026-07-22:daily:v1",
    "status": "succeeded_with_warnings",
    "episode_id": 17,
    "parent_task_run_id": null,
    "usage": {
      "llm_calls": 7,
      "llm_input_tokens": 28600,
      "llm_output_tokens": 7200,
      "tts_character_count": 6840
    },
    "steps": [
      {
        "id": 101,
        "name": "extracting",
        "attempt": 1,
        "status": "succeeded_with_warnings",
        "started_at": "...",
        "ended_at": "...",
        "input_count": 42,
        "output_count": 39,
        "warning_count": 3,
        "usage": {
          "llm_call_count": 0,
          "llm_input_tokens": 0,
          "llm_output_tokens": 0,
          "tts_character_count": 0
        },
        "error": null,
        "retryable": false
      }
    ]
  },
  "meta": {"request_id": "req_..."}
}
```

- 常见错误：`404 TASK_RUN_NOT_FOUND`。
- 幂等键：不需要。

### 5.4 获取任务结构化日志

- 方法/路径：`GET /api/v1/task-runs/{task_run_id}/logs`
- 查询参数：`after_sequence:int?`、`limit:int=200`（最大 1,000）、`level?`、`step_name?`。
- 返回：按 sequence 排序的脱敏日志数组及 `next_sequence`；不返回 Prompt/正文全文。
- 常见错误：`404 TASK_RUN_NOT_FOUND`、`410 TASK_LOG_EXPIRED`、`422 INVALID_QUERY`。
- 幂等键：不需要。

### 5.5 从失败处重试

- 方法/路径：`POST /api/v1/task-runs/{task_run_id}/retry`
- Header：`Idempotency-Key` 必填。
- 请求：

```json
{
  "from_step": null,
  "use_current_config": false
}
```

`from_step=null` 表示系统沿 `parent_task_run_id` 读取可验证 checkpoint，并从最早失效步骤恢复。指定步骤只能向前扩大重做范围，不能跳过无效依赖。恢复任务复用已验证的对象 ID、LLMArtifact 和音频缓存，并保留已完成用量审计；产生私有 editorial 文件的步骤会在子任务自己的 artifact 根目录重跑，绝不写入父任务目录。`use_current_config=false` 默认复用父运行脱敏配置快照。

- 返回：`202` + 新 TaskRun，含 `parent_task_run_id`。
- 常见错误：`404 TASK_RUN_NOT_FOUND`、`409 TASK_NOT_RETRYABLE`、`409 TASK_ALREADY_RUNNING`、`409 IDEMPOTENCY_KEY_REUSED`、`422 INVALID_RESUME_STEP`。
- 幂等键：必须。

### 5.6 取消活动任务

- 方法/路径：`POST /api/v1/task-runs/{task_run_id}/cancel`
- 请求：`{"reason":"user_requested"}`。
- 返回：`200` + 更新后 TaskRun；重复取消同一终态返回当前状态。
- 常见错误：`404 TASK_RUN_NOT_FOUND`、`409 TASK_ALREADY_TERMINAL`（对 succeeded/failed 等非 cancelled 终态）。
- 幂等键：不需要；状态转换天然幂等，取消为协作式，可能在当前外部调用超时后生效。

## 6. 新闻候选与事件

### 6.1 文章候选列表

- 方法/路径：`GET /api/v1/articles`
- 查询参数：
  - `task_run_id?`：本次任务涉及的文章；
  - `source_id?`、`status?`、`news_event_id?`；
  - `published_from?`、`published_to?`；
  - `duplicate:boolean?`、`q?`（标题子串，不做全文搜索）；
  - `page`、`page_size`、`sort=-published_at`。
- 返回字段：`id`、source 摘要、title、url、published_at、status、content_length、filter_reason、duplicate_of_article_id、news_event_id、error 摘要。
- 常见错误：`404 SOURCE_NOT_FOUND`（指定无效 source）、`422 INVALID_QUERY`。
- 幂等键：不需要。

### 6.2 文章详情

- 方法/路径：`GET /api/v1/articles/{article_id}`
- 查询参数：`include_content:boolean=false`。为 true 时返回纯文本正文，仍不返回原始 HTML。
- 返回：文章字段、规范化/判重信息、所属 Event 和最近任务错误。
- 常见错误：`404 ARTICLE_NOT_FOUND`。
- 幂等键：不需要。

### 6.3 事件列表

- 方法/路径：`GET /api/v1/news-events`
- 查询参数：`event_date?`、`status?`、`selected_for_episode_id?`、`min_importance?`、分页和排序。
- 返回：Event ID、title、summary、各分数、source/article 数、selection_reason、risk flags 和代表文章。
- 常见错误：`404 EPISODE_NOT_FOUND`、`422 INVALID_QUERY`。
- 幂等键：不需要。

### 6.4 事件详情

- 方法/路径：`GET /api/v1/news-events/{event_id}`
- 参数：无。
- 返回：完整 NewsEvent、聚类算法/版本、评分结果和成员 Article 摘要。
- 常见错误：`404 NEWS_EVENT_NOT_FOUND`。
- 幂等键：不需要。

V1 不提供直接手工改聚类的 API；若实际使用证明需要，再设计可审计的 merge/split 操作。

## 7. 节目管理

### 7.1 节目列表

- 方法/路径：`GET /api/v1/episodes`
- 查询参数：`status?`、`date_from?`、`date_to?`、`edition?`、分页、`sort=-episode_date`。
- 返回：分页 `EpisodeSummary[]`。
- 常见错误：`422 INVALID_QUERY`。
- 幂等键：不需要。

### 7.2 节目详情

- 方法/路径：`GET /api/v1/episodes/{episode_id}`
- 查询参数：`include_script:boolean=true`、`include_items:boolean=true`、`include_segments:boolean=true`。
- 返回：

```json
{
  "data": {
    "id": 17,
    "public_id": "55c2448e-49c0-4db4-a2e1-3f7010284c17",
    "episode_date": "2026-07-22",
    "edition": "daily",
    "status": "review_required",
    "title": "...",
    "description": "...",
    "outline": {"schema_version": "1", "sections": []},
    "script": {
      "revision": 2,
      "text": "...",
      "origin": "generated",
      "sections": []
    },
    "review": {"verdict": "pass", "issues": []},
    "audio": {
      "version": 2,
      "duration_ms": 842000,
      "url": "/api/v1/episodes/17/audio",
      "segments": []
    },
    "items": [
      {
        "position": 1,
        "event_id": 81,
        "title": "...",
        "selection_reason": "...",
        "sources": [{"article_id": 911, "title": "...", "url": "https://..."}]
      }
    ],
    "publications": [],
    "lock_version": 5
  },
  "meta": {"request_id": "req_..."}
}
```

- 常见错误：`404 EPISODE_NOT_FOUND`。
- 幂等键：不需要。

### 7.3 修改节目元数据

- 方法/路径：`PATCH /api/v1/episodes/{episode_id}`
- Header：`If-Match: "<lock_version>"` 必填。
- 请求：`{"title":"...","description":"..."}`，至少一个字段。
- 返回：`200` + EpisodeSummary，新 ETag/lock_version。
- 状态语义：draft/review_required/approved 可修改且不使音频失效；approved 修改后清空批准绑定和 `approved_at` 并进入 `review_required`，与 metadata 重生成语义一致。publishing 和 published 均拒绝修改。已发布内容需要修正时显式创建新 edition，避免订阅客户端缓存与服务端元数据不一致。
- 常见错误：`404 EPISODE_NOT_FOUND`、`409 EPISODE_PUBLISHING`、`409 EPISODE_IMMUTABLE`、`412 VERSION_MISMATCH`、`422 METADATA_INVALID`。
- 幂等键：不需要；If-Match 防并发覆盖，相同绝对值重复 PATCH 无额外副作用。

### 7.4 修改播客稿

- 方法/路径：`PATCH /api/v1/episodes/{episode_id}/script`
- Header：`If-Match: "<lock_version>"` 必填。
- 请求：`{"script_text":"...","change_reason":"manual_edit"}`。
- 返回：`200`，包含新 `script_revision`、重新分段 diff：`reused_segment_count`、`stale_segment_count`、`new_segment_count`，以及 Episode 状态。
- 状态语义：仅 draft/review_required/approved 可改。文本 hash 真实变化时必须增加 `script_revision`，清空 `approved_script_revision`、`approved_audio_version`、`approved_at` 和旧检查结果，使 Episode 当前音频引用失效，并进入 `draft`。只有当前修订重新检查通过、缺失片段生成完成且最终 MP3 合并有效后，才进入 `review_required`。published 稿件不可修改。
- 常见错误：`404 EPISODE_NOT_FOUND`、`409 EPISODE_IMMUTABLE`、`409 EPISODE_PUBLISHING`、`412 VERSION_MISMATCH`、`422 SCRIPT_INVALID`。
- 幂等键：不需要；If-Match 必须，内容 hash 相同则不增加修订号。

### 7.5 审核批准

- 方法/路径：`POST /api/v1/episodes/{episode_id}/approve`
- Header：`If-Match: "<lock_version>"` 必填。
- 请求：

```json
{
  "expected_script_revision": 2,
  "expected_audio_version": 2,
  "note": "试听通过"
}
```

- 返回：`200` + status `approved`、批准时间和绑定版本。对同版本重复批准返回当前结果。
- 常见错误：`404 EPISODE_NOT_FOUND`、`409 EPISODE_NOT_REVIEWABLE`、`409 REVIEW_HAS_BLOCKING_ISSUES`、`409 AUDIO_VERSION_STALE`、`412 VERSION_MISMATCH`。
- 幂等键：不需要；操作按期望版本天然幂等，不创建外部副作用。

### 7.6 请求修改/撤销批准

- 方法/路径：`POST /api/v1/episodes/{episode_id}/request-changes`
- Header：`If-Match` 必填。
- 请求：`{"note":"第二段数字需要复核"}`。`note` 只记录审核意见，不修改稿件；实际修改必须随后调用 script PATCH。
- 返回：`200` + status `review_required`。该接口只清空批准绑定和 `approved_at`，不修改稿件、检查结果或音频，因此不会进入 `draft`。对已处于 `review_required` 的相同请求返回当前状态。
- 常见错误：`404 EPISODE_NOT_FOUND`、`409 EPISODE_STATE_CONFLICT`、`409 CURRENT_ARTIFACTS_INVALID`、`412 VERSION_MISMATCH`。若任一当前产物已经失效，调用方应执行相应重生成流程，Episode 保持/进入 `draft`。
- 幂等键：不需要；仅撤销当前批准是天然幂等状态转换。

### 7.7 重生成节目环节

- 方法/路径：`POST /api/v1/episodes/{episode_id}/regenerations`
- Header：`Idempotency-Key` 必填。
- 请求：

```json
{
  "target": "script",
  "force": false,
  "instruction": "保持事实不变，缩短到 12 分钟"
}
```

`target`：

| 值 | 保留 | 失效并重做 | 提交后的 Episode 状态 |
|---|---|---|---|
| `selection` | 原始 Article/Event | 选题、EpisodeItem 及全部下游 | `draft`，完成检查与音频后 `review_required` |
| `outline` | EpisodeItem | 大纲、稿件、检查、音频 | `draft`，完成检查与音频后 `review_required` |
| `script` | EpisodeItem、大纲 | 稿件、检查、音频 | `draft`，新稿增加 revision，完成下游后 `review_required` |
| `metadata` | 稿件、检查和音频 | 标题、简介 | 已批准时清空批准并进入 `review_required`；否则保持现状 |
| `review` | 稿件和音频 | 代码/LLM 检查 | 检查期间为 `draft`，通过后 `review_required` |
| `audio` | 稿件和有效检查 | 目标音频片段与最终 MP3；`force=false` 可复用缓存 | `draft`，合并有效后 `review_required` |

若 Episode 原为 approved，任何导致稿件、检查或音频失效的 target 都必须在创建 TaskRun 的同一事务中清空 `approved_script_revision`、`approved_audio_version` 和 `approved_at`。`metadata` 不使三项审核产物失效，但新元数据仍需人工确认，因此只撤销批准并进入 `review_required`。

`instruction` 最长 500 字符，只作为本次受控编辑要求，不写入系统 Prompt。published Episode 不允许重生成；应创建新 edition/修订节目。

- 返回：`202` + `regenerate_episode` TaskRun。
- 常见错误：`404 EPISODE_NOT_FOUND`、`409 EPISODE_IMMUTABLE`、`409 TASK_ALREADY_RUNNING`、`409 IDEMPOTENCY_KEY_REUSED`、`422 INVALID_REGENERATION_TARGET`、`422 INSTRUCTION_TOO_LONG`。
- 幂等键：必须。

### 7.8 重生成单个音频片段

- 方法/路径：`POST /api/v1/episodes/{episode_id}/audio-segments/{segment_id}/regenerate`
- Header：`Idempotency-Key` 必填。
- 请求：`{"force":true}`。默认 force 为 true，表示即使相同缓存键也实际请求一次 TTS。
- 返回：`202` + `regenerate_segment` TaskRun。提交后清空批准绑定并使当前音频失效，Episode 进入 `draft`；成功后仅替换当前稿件修订中的该段、复用其他段并重新合并整期，合并有效后进入 `review_required`。
- 常见错误：`404 EPISODE_NOT_FOUND`、`404 AUDIO_SEGMENT_NOT_FOUND`、`409 SEGMENT_NOT_CURRENT`、`409 EPISODE_IMMUTABLE`、`409 TASK_ALREADY_RUNNING`、`409 IDEMPOTENCY_KEY_REUSED`。
- 幂等键：必须。

### 7.9 获取节目音频

- 方法/路径：`GET /api/v1/episodes/{episode_id}/audio`
- Header：支持标准 `Range`、`If-None-Match`。
- 查询参数：`download:boolean=false`。
- 返回：草稿/已发布音频流，`Content-Type: audio/mpeg`，支持 `200/206/304`；ETag 使用文件 sha256。
- 访问选择：published 返回/重定向到公开不可变 URL；其他状态返回当前私有草稿，仅管理端可访问。
- 常见错误：`404 EPISODE_NOT_FOUND`、`404 AUDIO_NOT_READY`、`416 RANGE_NOT_SATISFIABLE`、`500 AUDIO_ASSET_MISSING`。
- 幂等键：不需要。

## 8. 发布

### 8.1 发布已批准节目

- 方法/路径：`POST /api/v1/episodes/{episode_id}/publications`
- Header：`Idempotency-Key` 必填。
- 请求：

```json
{
  "target_key": "rss-local"
}
```

RSS target 始终由配置确定；启用网易云时，发布 dispatcher 会在 RSS 原子发布成功后自动尝试已配置的 NetEase target。请求不允许传入任意类名、URL、账号、Cookie 或浏览器 profile。

- 返回：`202` + publish TaskRun；重复相同请求返回已有 TaskRun/Publication。
- 执行语义：RSSPublisher 创建或复用 `Publication(publishing)`，提升并校验不可变 MP3，读取既有 `published` Publications，再把当前已验证的 `publishing` Publication 作为候选 item 显式注入内存 Feed；校验并原子替换 `feed.xml` 后，最后在短事务中同时标记 Publication/Episode=`published`。Feed 的稳定状态只包含成功发布节目，过渡 candidate 不能依赖 published 查询获得。
- 崩溃恢复：若 Feed 已含当前 GUID 且 enclosure URL、MIME、长度和公开文件 checksum 正确，而数据库仍为 `publishing`，reconcile 补写两个 published 状态，不重复 item、不重复复制或覆盖不可变音频；若 Feed 尚未含该 GUID，则以相同 candidate 重新构建。
- 常见错误：`404 EPISODE_NOT_FOUND`、`404 PUBLISH_TARGET_NOT_FOUND`、`409 EPISODE_NOT_APPROVED`、`409 APPROVAL_STALE`、`409 PUBLICATION_ALREADY_RUNNING`、`409 IDEMPOTENCY_KEY_REUSED`、`503 PUBLIC_STORAGE_UNAVAILABLE`。
- 幂等键：必须。

### 8.2 Publication 详情

- 方法/路径：`GET /api/v1/publications/{publication_id}`
- 参数：无。
- 返回：RSS `Publication` 的 status、target、attempt_count、公开 URL、asset sha256/字节数、last_verified_at 和脱敏错误。RSS `Publication.status` 仍只可能是 `pending`、`publishing`、`published`、`failed`；独立 `PublicationTarget` 记录外部 target 的 `needs_attention`，供后续显式 resume 用例读取，不能通过该接口写入登录凭证。
- 常见错误：`404 PUBLICATION_NOT_FOUND`。
- 幂等键：不需要。

## 9. RSS 与公开静态音频

### 9.1 获取 RSS Feed

- 方法/路径：`GET /feed.xml`
- Header：支持 `If-None-Match`、`If-Modified-Since`。
- 查询参数：无。
- 返回：`200 application/rss+xml; charset=utf-8` 或 `304`。对外可见的稳定 Feed item 只包含成功 published Episode，GUID 稳定；发布原子替换期间，RSSPublisher 在内存模型中显式加入已验证的当前 candidate，并在替换成功后立即补写数据库状态。
- 常见错误：首次尚未生成 Feed 时返回有效的空 channel，而不是 404；存储损坏时 `500 FEED_UNAVAILABLE` 并保留上一个已验证文件。
- 幂等键：不需要。

### 9.2 获取公开音频

- 方法/路径：`GET /media/episodes/{episode_public_id}/{audio_asset_id}.mp3`
- Header：支持 `Range`、`If-None-Match`。
- 返回：不可变音频，`Cache-Control: public, max-age=31536000, immutable`，`200/206/304`。
- 常见错误：`404 PUBLIC_AUDIO_NOT_FOUND`、`416 RANGE_NOT_SATISFIABLE`。
- 幂等键：不需要。

公开路径只由 RSSPublisher 生成，不提供上传 API。

## 10. 接口与幂等总览

| 接口 | 是否需要 Idempotency-Key | 幂等依据 |
|---|---:|---|
| 所有 GET | 否 | 只读 |
| POST Source | 否 | source id / normalized URL 唯一，重复返回 409 |
| PATCH/DELETE Source | 否 | 绝对值 + ETag / 软停用 |
| POST TaskRun | **是** | TaskRun.idempotency_key + business key |
| POST Task retry | **是** | 新 TaskRun.idempotency_key |
| POST cancel | 否 | 状态转换 |
| PATCH Episode metadata/script | 否 | `If-Match` + 内容 hash |
| POST approve/request-changes | 否 | 期望修订 + 状态转换 |
| POST regeneration | **是** | TaskRun.idempotency_key |
| POST segment regenerate | **是** | TaskRun.idempotency_key |
| POST publication | **是** | TaskRun + Publication idempotency key |

## 11. 管理页面与 API 的关系

管理页面位于 `/admin`，由服务端 Jinja2 渲染，HTMX 请求可复用上述用例。页面 route 不应绕过用例直接操作数据库。建议页面：

- `/admin`：最近任务、待审节目、下次调度；
- `/admin/sources`：来源列表和测试结果；
- `/admin/tasks/{id}`：步骤时间线、数量、日志轮询；
- `/admin/articles`：候选/过滤/失败文章；
- `/admin/episodes/{id}`：来源、理由、稿件、检查、音频、审核和重生成；
- `/admin/publications/{id}`：发布状态与错误。

HTMX 表单遇到业务错误时可返回相同错误码并渲染局部错误片段；JSON API 契约仍是唯一业务接口语义来源。

## 12. 明确不提供的 V1 API

- 用户、登录、角色和 Token 管理；
- 任意 SQL/日志全文搜索；
- 上传原始音频、删除公开音频、批量物理删除文章；
- 任意 URL 代理或网页截图；
- 直接调用供应商、编辑 Prompt、修改数据库状态；
- 直接查询、编辑或删除 LLMArtifact；该表是内部只读缓存，只能由通过 schema 校验的 LLM 服务写入，并按保留策略清理；
- 直接传入网易云账号、Cookie、验证码、任意 Playwright selector，或使用非官方逆向接口的运行接口；
- WebSocket 实时推送。管理页使用有界轮询，任务终态后停止。

## 13. 每日文字简报（Briefing，已实现）

本节接口已随简报功能落地，独立于上述 `/api/v1` 设计（不带前缀、不走 TaskRun）。简报流程从带 `briefing_category` 标签的来源采集近 24 小时新闻，按类目（`telecom` 通信行业日报、`ai` AI 动态日报）生成中文 markdown 并推送 webhook 目标（默认企业微信群机器人，也可对接任意接受 JSON 的 webhook，如 Slack 风格 `{"text": ...}`）；与播客流水线互不共享任务与来源池。

启用方式：

- YAML 增加 `briefing:` 段：`enabled: true`、`sources_config_path`（简报源种子，默认 `config/briefing.sources.yaml`）、`cron_expression`（默认 `30 7 * * *`，应用时区）、`window_hours`、`webhook_enabled`、`webhook_format`（`wecom_markdown`（默认）或 `generic_json`）。
- webhook 凭据只从环境变量注入：`DAILYCAST_BRIEFING__WEBHOOK_URL`；`webhook_enabled=true` 时必填，缺失则配置加载失败。
- `config/app.yaml` 为启用示例（配合 `.env` 的 `DAILYCAST_CONFIG_PATH=config/app.yaml`）；默认 `config/app.example.yaml` 中 `briefing.enabled=false`，功能完全关闭。

### 13.1 手动触发简报生成

- 方法/路径：`POST /briefing/generate`
- 查询参数：`force:boolean=false`。`force=true` 忽略当日各类目完成标记，重新生成并推送。
- 返回：`202`，`{"status":"accepted"}`；实际生成在后台任务中执行。
- 幂等语义：同一应用时区日期内，每个类目完成（生成成功且推送已发送或未启用）后写入 `data/work/briefings/YYYY-MM-DD-{category}.done` 标记；非 force 的重复触发对已完成类目直接跳过（report 中 `status=skipped`、`reason=already_completed`），不重复调用 LLM、不重复推送。推送失败不写标记，下次运行自动补推。全进程同时只允许一个简报 run。
- 常见错误：`409 {"detail":"briefing is not enabled"}`（功能未启用）；`409 {"detail":"briefing run already in progress"}`（已有运行中的简报任务）。
- 幂等键：不需要；按日按类目完成标记 + 单运行互斥提供等效保护。

### 13.2 手动测试推送通道

- 方法/路径：`POST /briefing/test-push`
- 参数：无。
- 返回：`200`，`{"status":"sent"}`。接口**同步**发送一条固定的测试 markdown（标题「DailyCast 简报推送测试」，含触发时间与已配置类目）到当前配置的 webhook，用于在不触发完整简报运行（不采集、不调 LLM）的情况下调试推送通道；在目标群里看到这条消息即代表通道可用。
- 常见错误：`409 {"detail":"briefing is not enabled"}`（简报功能未启用）；`409 {"detail":"briefing webhook is not enabled"}`（简报启用但未配置 `webhook_enabled=true`）；`502 {"detail":"webhook push failed: ..."}`（webhook 不可达或拒绝消息，错误信息含 HTTP 状态码 / `errcode` 等，重试一次后仍失败才返回）。
- 幂等键：不需要；仅用于调试，无落盘副作用。

### 13.3 读取最近一期简报

- 方法/路径：`GET /briefing/latest`
- 参数：无。
- 返回：`200`：

```json
{
  "date": "2026-08-20",
  "briefings": {
    "telecom": "# 通信行业日报 8月20日\n...",
    "ai": "# AI 动态日报 8月20日\n..."
  }
}
```

- `date` 为最近一个已落盘简报的业务日期；`briefings` 只包含该日已落盘的类目 markdown。当日简报尚未生成时（例如午夜后、`cron_expression` 触发前），返回最近一个有简报的日期，而不是 404。
- 常见错误：`404`（从未生成过任何简报）。
- 幂等键：不需要。

## 14. 分发目标手动恢复（Distribution resume，已实现）

多平台分发（RSS / NetEase / Xiaoyuzhou）为每个 Episode × platform 维护独立的 `publication_targets` 状态行。外部目标进入 `needs_attention`（如 NetEase 登录过期、验证码、缺少封面）后，需要人工完成对应操作，再通过本接口恢复该目标；恢复只重放该平台的上传，不重新生成节目。

### 14.1 恢复一个 needs_attention 目标

- 方法/路径：`POST /distribution/episodes/{episode_id}/targets/{platform}/resume`
- 路径参数：`episode_id:integer`；`platform:string`（`rss` | `netease` | `xiaoyuzhou`）。
- 返回：`200`：

```json
{
  "episode_id": 42,
  "target_statuses": {"netease": "published"},
  "warning_count": 0
}
```

- 语义：仅当目标处于 `needs_attention` 时重放上传；其他状态直接返回当前状态（`pending`/`publishing`/`published`/`failed`）。RSS 是不可变媒体的 source of truth：其恢复失败在持久化 FAILED 目标行后以原始错误抛出（映射为对应 HTTP 错误码），不会降级为 warning。
- 常见错误：`404 {"detail":"publisher netease is not enabled"}`（该平台未启用）；`404 {"detail":"Episode 999 does not exist"}`；`422`（未知 platform）；`409 {"detail":"distribution is not ready"}`（数据库 revision 不安全）。
- 幂等键：不需要；`(episode_id, platform)` 唯一目标行 + 目标状态机提供等效保护。
