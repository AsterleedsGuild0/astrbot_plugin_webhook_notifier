# Webhook Notifier 公共契约

## 文档状态

- 稳定版本：`v1.2.0`
- 状态：Final / 1.x 稳定公共契约
- 定稿日期：2026-08-10
- 远端发布状态：以 GitHub Releases 页面为准；本文档定义源码公共契约，不作为 tag、Release 或正式 ZIP 是否已创建的动态状态证明
- 当前源码版本：`v1.2.0`

`v1.2.0` 是当前稳定源码契约版本与正式发布目标。正式资产可用后，OpenCode 集成、Provider Registry/DI、Subagent Timeline 覆盖统计、用户等待时间线与相关 smoke 应使用 `v1.2.0` 稳定资产，不应回溯描述为 `v1.1.0` 已发布能力。AstrBot WebUI 安装、Bot Endpoint 和 Desktop 端到端 smoke 的正式版包验证仍须按实际执行结果单独留证。

---

## 公共契约范围

以下行为属于 v1.0 公共契约：

- `_conf_schema.json` 暴露的配置字段、类型、默认值及默认安全语义，特别是 `render_mode=text`、`enable_private_notifications=false`、Endpoint 级 Bearer 鉴权与本地监听默认值。
- `<唤醒词>whn` 命令族的行为、owner scope、私聊限制、群验证流程及全局超级管理员权限边界。
- Registry v2 的持久化格式版本、managed/pending scope、v1 透明迁移、quarantine、原子提交、fail-closed 和离线 rebind 语义。
- Webhook HTTP JSON 请求、Bearer Token、OMP version 1 兼容解析，以及 `ok`、`skipped`、`partial_delivery`、`partial_failure`、`targets`、可选 `send_results`、`delivered`、`rendered`、`retryable` 与 `skip_reason` 响应语义。
- `aiocqhttp`、`qq_official` WebSocket 私聊与普通 QQ 群的支持边界，以及 QQ 频道和 `qq_official_webhook` 不受支持的声明。
- GitHub Release ZIP 的可安装结构：顶层插件目录包含运行所需源码、配置、静态资源、模板和随包文档，可由 AstrBot WebUI 上传安装。

---

## v1.1.0 新增范围

以下能力经 `v1.1.0-rc.1` 候选阶段验证后纳入 `v1.1.0` 稳定公共契约：

- #18：Provider Adapter / Registry 与依赖注入边界，`omp` / `opencode` provider 选择及 Endpoint provider 不可变。
- #19：OpenCode Server Adapter 与四类 V1 envelope。
- #20：OpenCode V1 Client Plugin、正确 `plugin` tuple、env/file 凭据、状态机、timeout/retry 和 at-least-once 语义。
- #21：严格白名单、匿名 session ref/name fallback、Bun/Python/CLI smoke 和集成文档。
- OpenCode 丰富通知字段：实例标识 `instanceDisplayName`、客户端自动推导的 `projectName`、会话名、agent、`provider/model`、会话开始时间、busy→idle 任务时间（无可靠周期时间时 fallback 到 Assistant 元数据）与可读耗时/低敏计数，以及默认 strict、显式 opt-in 的 `actionContentMode`；项目名只在详细字段中显示。
- OpenCode 可选模型档位字段 `modelVariant`：优先来自 Assistant `info.variant`，缺失时来自 `session.model.variant`；仅在同时存在 `model` 时于展示副本中原样追加到模型值，例如 `cpa/gpt-5.6-sol(max)`，不单独显示。该字段不保证等同于 provider 原始 `reasoning_effort`/`reasoningEffort`，不根据 provider/model 推断。
- OpenCode V1 `session.scope`（`root|subagent|auxiliary|unknown`）与 Client scope 判断；`parentID` 不是公共字段，绝不发送。默认精确识别 `smartfetch-secondary`，非空 `parentID` 始终优先为 `subagent`。
- OpenCode root `session_idle` 的可选 `subagentTimeline`。它只允许出现在 `event=opencode.session_idle` 且 `session.scope=root` 的 envelope；其他 event 或 scope 携带该字段会被 Python strict adapter 拒绝。该能力无新增配置项，仍是 root completion 的可选数据。
- OpenCode Permission/Question 瞬时聚合：同一 Session、同一类型固定 150ms debounce；Permission 与 Question 不合并，不同 Session 不合并；回复在 flush 前撤销。Permission envelope 使用 `permission: {count, items[]}`，Question 保持 `question: {count, optionCount, summary?, items?}`。
- 全局 `notification_mode` 仅允许 `focused`（默认）和 `all`；`focused` 只抑制 `subagent` 与 `auxiliary` 的 `completed`，unknown scope/status fail-open。策略过滤返回 HTTP 200、`message=skipped`、`scope`、`reason=notification_mode_filtered`、`skip_reason=notification_mode_filtered`、`rendered=false`、`delivered=false`、`retryable=false`，且不进入 renderer、T2I 或 sender。
- Webhook retry 幂等：Envelope 顶层 `id` 参与幂等键；相同 endpoint/provider、event、id、session scope 与 target selector 在进程内 10 分钟、最多 2048 项 single-flight。重复请求返回 HTTP 200、`message=skipped`、`skip_reason=idempotency_replay`、`deduplicated=true`，过滤和全私聊策略 skip 不占用幂等 cache。

- #24：全局 `min_completion_duration_seconds`（最短完成通知时长）：成功完成的 Webhook 事件耗时低于阈值时跳过通知（默认 15 秒），减少短任务噪音。0 关闭过滤恢复旧行为。仅 `canonical completed` 状态参与；notification_mode 过滤在 duration 之前判断。task_duration_ms 仅来自 Provider 的可靠任务耗时（OMP round.durationMs / startedAt→endedAt 差值，OpenCode payload durationMs），不对外暴露。通过 `admin config min-duration` 命令查询/设置/reset。保存失败时回滚内存值，Server 保持旧值。缺失配置默认 15；非法历史配置归一化为 0（fail-open）。skip 响应返回 HTTP 200、`message=skipped`、`skip_reason=completion_below_duration_threshold`、`rendered=false`、`delivered=false`、`retryable=false`。

`actionContentMode` 与 `notification_mode` 正交：前者只控制 Question/Permission 内容隐私，后者只控制通知是否发送。

聚合 bucket 只存在客户端进程内存，raw session ID 仅作本地 key；request ID 仅用于去重/撤销，不出站。`strict` 的 Permission item 仅允许 `category`，不会发送正文；`summary`/`full` 仍按 allowlist 和上限处理。

这些能力的 RC 与正式版验证证据应按实际执行结果分别记录；不得将源码契约、RC 验收、正式资产验证或插件市场安装/更新验证相互替代。

---

## v1.2.0 新增范围

以下向后兼容能力纳入 `v1.2.0` 稳定公共契约：

- anomaly 元数据诊断：`metadataDiagnostics=anomaly` 只观测既有 `session.get` 与 outgoing envelope 链路中的 root/unknown fallback 候选，不增加额外 API 或 HTTP 调用。输出必须有界、匿名、去重并 fail-closed；它只提供取证信号，不表示已经定位 fallback 根因。
- #25：Subagent Timeline 覆盖统计。总任务时长来自可靠 root busy→idle 周期；覆盖时长只合并 `observed`/`fallback` 且起止完整、已裁剪到 root 周期的 subagent 区间，重叠区间按并集计算。数据不完整时必须标记观测受限，不得把未覆盖时间归因给 root 或其他执行。
- #26：root `opencode.session_idle` 可携带独立可选的 `userWaitTimeline`；旧客户端可以完全省略该字段，且它不修改 `subagentTimeline.version=1`。其他 event 或非 root scope 携带该字段会被 strict adapter 拒绝。
- `userWaitTimeline` 只汇总 root session 自身 Question/Permission asked→resolved 的插件接收时间。`complete` 区间包含可靠起止与 duration 并参与等待并集；`left_censored`/`right_censored` 只保留已知边界，不伪造 duration、不参与并集。
- Renderer 在同一张完整 root-cycle 甘特图中展示固定“等待用户”轨道与 subagent 区间。等待区间不计入子任务数、峰值并发或 subagent 覆盖率；“未分类时间 / 占比”使用总任务时长减去可靠 subagent 与等待区间并集，不把剩余时间归因给主 agent。
- raw session/request ID、Question/Permission 正文、答案、pattern/target、URL 与 Token 不得进入 `userWaitTimeline`、诊断日志或渲染输出；request ID 只在 Client 内存中用于去重/撤销。
- 部署必须先升级并重载 AstrBot 服务端，再部署并完全重启 OpenCode Client；旧服务端严格 allowlist 不接受新增 `userWaitTimeline`。
- 新增 `markdown` provider：不新增公开路由，仍使用现有 Endpoint Path、Bearer Token、目标别名白名单、幂等、Sender、`text`/`html_image`/fallback 链路。创建后 provider 仍不可变。
- `markdown` provider 只接受 `event=markdown.message`。`markdown` 为必填非空字符串，最多 32768 个 Unicode 字符且 UTF-8 不超过 64 KiB；可选 `title` 最多 200 字符、`id` 最多 128 字符、`target_alias` 最多 128 字符。未知字段、错误类型、空值、错误事件或超限输入按既有 JSON 错误结构返回 4xx。
- 受限 Markdown 子集包括标题、段落、无序/有序列表、粗体/斜体、inline code、fenced code 和普通 `http(s)` 链接。Raw HTML、图片、Jinja/模板执行与远程资源加载不受支持；相关输入按文本显示。HTML 图片继续复用内置卡片、sandbox、CSP、T2I 与文本 fallback。

最小请求示例：

```json
{
  "event": "markdown.message",
  "id": "cpa-update-stable-id",
  "title": "CPA 自动更新",
  "markdown": "## 更新完成\n\n- CPA：`x → y`\n- 状态：成功",
  "target_alias": "default"
}
```

---

## `subagentTimeline` 精简契约

Wire JSON 使用 camelCase。以下是精简 shape，不代表可省略其中的必填字段：

```json
{
  "event": "opencode.session_idle",
  "session": {"scope": "root"},
  "subagentTimeline": {
    "version": 1,
    "timeBasis": "root_cycle",
    "partial": false,
    "partialReasons": [],
    "observedItemCount": 1,
    "displayedItemCount": 1,
    "truncated": false,
    "items": [
      {
        "ref": "<匿名 hash 引用>",
        "parentRef": "<匿名 hash 引用>",
        "status": "completed",
        "timingQuality": "observed",
        "depth": 1,
        "attempt": 1
      }
    ]
  }
}
```

- `subagentTimeline` 的字段为 `version`、`timeBasis`、`partial`、`partialReasons`、`observedItemCount`、`displayedItemCount`、`truncated` 与 `items`。item 必须有 `ref`、`parentRef`、`status`、`timingQuality`、`depth`、`attempt`，可选 `name`、`agent`、`model`、`modelVariant`、`startOffsetMs`、`endOffsetMs`、`durationMs`。
- `model` 与 `modelVariant` 是经过清洗和 128 字符限制的可选安全文本；展示统一使用 `agent · model(variant)`，仅非 `default` variant 追加括号，缺失字段时自然降级。`modelVariant` 只表示 OpenCode runtime variant，不命名或解释为 `reasoning_effort`。它们不改变时间、状态、排序或截断语义。
- `partialReasons` 使用受限值：`missing_parent`、`missing_start`、`missing_end`、`invalid_parent_graph`、`truncated`、`clamped`；`timingQuality` 表示 `observed`、`fallback`、`partial` 或 `unknown` 的时间可信度。
- `ref`/`parentRef` 是匿名 hash 图引用，仅用于建立父子关系，不是 raw Session ID；默认用户输出不展示它们。
- offset 是相对 root busy→idle cycle 的毫秒偏移；它描述观测到的时间关系，不声明调度依赖。区间重叠只表示任务同时运行。
- `partial` 必须反映缺失、校正或其他不完整原因；`truncated` 表示记录受上限截断。Python adapter 对未知字段、错误 scope/event、错误 shape 和超限 payload fail-closed。
- 限制为 `items` 不超过 64 项、timeline JSON 不超过 24 KiB、整个 request body 不超过 64 KiB、`depth` 不超过 8；新增字段计入已有序列化大小边界。超限、缺失或截断时应保留 `partial`/`truncated` 语义，不把不完整数据表述为完整执行记录。

该字段不进入 `session_error`、`permission_asked`、`question_asked` 或非 root session 通知。`auxiliary`（包括 `smartfetch-secondary`）不进入 timeline；`focused` 仍独立过滤成功完成的 `subagent`/`auxiliary` 通知，但 root completion 可以汇总 timeline。

---

## Webhook retry 幂等

- OpenCode Envelope 顶层 `id` 是 HTTP retry 幂等键的组成部分；同一 key 的并发请求只允许一个 owner 进入渲染/发送，其他请求等待 owner 终态。
- 幂等 store 仅为当前进程内存中的 10 分钟 TTL、2048 项 LRU；TTL 从 owner 完成后开始，重放不会续期。
- 服务重启、进程崩溃或未来多实例部署之间不保证共享幂等状态。幂等不是远端 exactly-once 交付保证。
- notification policy filtered 与 all-private preflight 的 skip 在 claim 之前返回，因此不占用 cache。
- duplicate replay 使用当前 HTTP request_id 返回兼容的 `skipped` 200，不复用原请求的 aiohttp response，也不在响应或幂等日志中暴露 event id、endpoint path 或 payload。

---

## 非公共实现细节

以下内容不构成兼容承诺，可在不改变公共行为的前提下调整：

- 中文提示的具体措辞、标点和排版。
- Plugin Page 页面布局、视觉层级和内部交互实现。
- `core/` 下的模块路径、函数拆分和内部类。
- Plugin Page Bridge API 及其他仅供当前页面实现使用的内部接口。
- JSON 对象字段顺序。
- 未在公共文档中声明的内部类、辅助函数、日志实现和测试夹具。

---

## 1.x 兼容政策

- 1.x 可以向后兼容地新增配置、命令选项、响应字段、平台适配或修复错误。
- 调用方必须忽略未知 JSON 字段，不依赖字段顺序或中文提示全文匹配。
- 破坏现有公共契约的变更进入 2.0；若安全或平台变化必须在 1.x 调整，应提供明确迁移说明、兼容层或合理弃用周期。
- 安全修复可以收紧未承诺的内部行为，但不得无说明地恢复已废弃凭据、扩大 Token 权限或绕过默认安全策略。

---

## 契约依据

- [PRD](PRD.md)
- [FSD](FSD.md)
- [命令参考](command-reference.md)
- [OMP 客户端接入](client-integration.md)
- [OpenCode 集成](opencode-integration.md)
- [安全与运维](security-and-operations.md)
- [发布流程](release.md)
