# Webhook Notifier 公共契约

## 文档状态

- 本文契约与源码版本：`v2.0.0`
- 状态：2.0 默认 Provider 兼容性变更，保留 1.x 历史契约。
- 2.0 契约定稿日期：2026-10-22
- 正式版本与资产以 [GitHub Releases](https://github.com/AsterleedsGuild0/astrbot_plugin_webhook_notifier/releases) 为准；源码版本号不单独证明远端发布或生产部署已完成。

`v1.3.0` 历史发布范围与证据保留在对应章节，不将 2.0 新默认倒灌为历史 ZIP 的行为。测试包已完成默认 Markdown 私聊路径的线上验证；既有 OMP/OpenCode 线上通知、生产缺字段记录迁移及插件市场安装/更新尚未验证，详见[发布流程](release.md)。

## v2.0.0：默认 Provider 与通用定位

- 插件以三 Provider 的通用 Webhook 通知能力定位；运行时支持集合由 `ProviderRegistry` 决定，当前注册 `markdown`、`omp`、`opencode`。命令校验查询实际注册表，帮助中的 Provider 选项按注册名称排序生成，不以单独的固定白名单为准。
- `token new private`、`token new group <数字群号>` 和 `token new group current` 仅在未提供 `--provider` 时使用默认 `markdown`。成功创建私聊 Endpoint 或群待验证申请后，公开说明提醒默认 `markdown`、`event=markdown.message`，以及 OMP/OpenCode 需显式 `--provider`；独立 Token 消息不追加该提醒，创建错误也不提示成功。
- 显式 flag 的不支持值、缺值、重复指定、`--provider=value` 或 flag 形式的值必须拒绝，不静默回退。OMP 使用 `--provider omp`，OpenCode 使用 `--provider opencode`；显式三种 Provider 均保持原有协议、鉴权、渲染与发送边界。
- 旧持久化 Endpoint 记录仅在 **`provider` key 缺失**时按 `markdown` 读取；显式空字符串或 `null` 仍非法。显式历史 Provider 保留、不自动覆盖、不根据 payload 猜协议。依赖缺失字段的旧 OMP 调用必须创建显式 OMP Endpoint 并更新调用方 URL/Token。
- 加载缺失 `provider` 的旧记录时会记录不含 Endpoint/Token 私密值的 warning，提醒核对调用方协议；该日志不意味着记录已自动迁移到 OMP，也不替代调用方更新。
- Endpoint Provider 不可变，不能通过 rotate 或编辑 Registry 原位切换。`provider=markdown` 决定输入协议；`render_mode=text|html_image` 决定输出展示，Markdown 请求同样可以使用 HTML 图片与文本降级。
- 此变更使用新的主版本号表达兼容性边界，Webhook payload 版本与 Registry 格式版本不变。测试、部署与发布证据分别记录，不将本地回归或测试包投递扩大为全部生产路径通过。

## OMP V2：运行收束与交互观察（未发布）

新 Hook 只发送 `omp.run_finished` 或 `omp.action_required`，`version` 必须是整数 `2`；`X-OMP-Event` 如提供，必须与 body 完全相同。旧服务端不识别这两个事件，不得自动降级为 `omp.session_stop`。仍使用显式 `provider=omp` Endpoint、Bearer 和既有目标别名白名单。

### 信封与隐私

必填：`event`、`version`、`id`、`emittedAt`、`session`、`cycle`、`status`。可选：`instance`、`interaction`、`target_alias`、`subagentTimeline`、`userWaitTimeline`。各层未知字段拒绝，整个 JSON 至多 64 KiB。

- `id` 是每条通知生成一次的随机 32 位小写十六进制标识；重试不得更换。
- `session` 必须有 `ref`（32 位匿名引用）、`scope`（`root|subagent|unknown`），可选 `name`、`cwdName`、`agent`。名称不超过 96 个 Unicode 字符；缺失名称显示 `OMP Session <ref前12位>`。
- `cycle` 必须有随机 `ref`、`partial`（布尔）、`reasons`（去重枚举列表），可选 `startedAt`、`endedAt`、`durationMs`、`model`、`thinking`、`metadataChanged`、`timeBasis`。新 Hook 的 `timeBasis=receipt_monotonic`，耗时来自公开事件接收的单调时钟，非负整数且至多 24 小时；ISO 时间仅作墙钟展示，墙钟偏差超过 10ms 标记 `clock_changed`。省略 timeBasis 的早期 V2 请求仍按 ISO 差值校验。缺开始不提供耗时；`endedAt` 只用于收束事件。`model` 至多 128 字符，实际 `thinking` 至多 32 字符；运行中变化则省略单一模型／档位，缺失则不补造对应字段。
- `instance` 是显式配置的实例别名，至多 96 字符；`target_alias` 至多 128 字符。
- `interaction` 仅用于待处理事件，必填匿名 `ref` 与 `kind=approval|plan_review|ask`。不发送问题、选项、计划、工具名或参数。对应区间是请求观察区间，不是精确人工等待。
- `reasons` 允许 `missing_start`、`session_changed`、`shutdown`、`expired`、`capacity`、`unpaired_interaction`、`incomplete_metadata`、`clock_changed`。恢复不读取私有历史；缺失起点不得补造。

原始会话／代理／请求 ID 只在有界进程内存中关联，匿名会话引用使用每次加载随机密钥的 HMAC；运行／交互引用使用随机数，不依赖正文。不发送 prompt、回答、问题选项、计划、完整路径、原始错误或事件／details 对象。名称清除控制字符并限长，路径／URL 形态的名称舍弃；cwd 只取 basename。可读名称仍可能含业务信息。

模型名称可以是 `provider/model` 或 `provider/org/model`，但绝对路径、用户目录路径及 Windows 路径不会因加上 provider 前缀而变成可公开名称；客户端舍弃，V2 接收端拒绝。

### 运行与状态

`agent_start` 建立观察周期，维护／重试／后台恢复的再次开始沿用尚未收束的周期。`session_stop` 仅保存结构化结果，不投递；最终依据维护路由后的 `agent_end`，`willContinue=true` 时不收束，`awaitingAsyncWork=true` 单独表示后台等待。子代理同样依据 `agent_end`，不依赖缺失的 `session_stop`。工具失败不直接升级为任务失败。

| 状态 | 证据与含义 |
| --- | --- |
| `completed` | 最终 `agent_end`，无重试未决／失败证据，最后 assistant 的结构化 `stopReason=stop`。表示本次运行正常结束，不证明业务目标通过验收。 |
| `failed` | 最终 assistant `stopReason=error`，或重试已报告失败且运行最终收束。 |
| `aborted` | 最终 assistant `stopReason=aborted`；优先于重试失败分类。 |
| `action_required` | 有 UI 的可观察交互请求仍未解决，仅用于 `omp.action_required`。ask 事件不证明 UI 已呈现。 |
| `unknown` | 缺少可靠结果、其他 stopReason、会话切换／退出等未完成观察。不得显示为完成。 |

重试失败事件没有结构化“取消／耗尽”判别字段；不能解析 `finalError` 文本猜测原因。没有最终 `agent_end` 时不凭重试结果补造完成。会话切换只在已发生的 switch／branch／tree 后隔离旧周期，不在可取消的 before 事件中提前终结。

交互用本地 session + agent + 请求 ID 配对去重。150ms 后及每次发送前再次检查；已解决／取消／无 UI 不补发提醒。审批 requested 早于上游 UI 检查，计划 review resolved 表示界面关闭且可能包含处理时间；ask 只看公开 `tool_call/tool_result`，主调用与标准 eval 间接调用沿用同一边界，不扫描 eval 代码。请求无对应结束时保留不完整语义。已发出的提醒不能撤回；解决后不再重试。

### 多代理与交互观察时间线

可选 timeline 只允许出现在 root `omp.run_finished`，并要求 `cycle.timeBasis=receipt_monotonic`。仍标准化到既有 `subagent_timeline`／`user_wait_timeline`，不新增传输事件或第二套状态机。缺失 timeline 表示没有资料，不等于可靠的零。

关联使用同一模块内各已加载工厂的公开 `ctx.agent` 身份和 parent 关系；父实例的公开 `getAsyncJobSnapshot()` 必须能唯一证明 agent／owner 边。相同 Main 身份可能出现在多个根实例，不能只凭 parentId 猜根会话。多候选歧义保持不关联，不因某根先退出而猜剩余根；后到的唯一证据可补关联尚在有界内存中的记录。快照只辅助身份／加载缺口判断，不使用 label、command、startTime 或 endTime 制造生命周期。

- 每个 run 使用随机 `cycleRef`，worker 身份复用会产生新 cycle。每次 `agent_start` → `agent_end` 为独立观察段；续跑段为 `continued`，不是最终成功。等待后台工作的间隙不扩大子代理覆盖。没有 workpool item 调度的公开证据时只记录 worker 运行，不宣称 item 级追踪。
- 常规子代理完成只汇总到根通知；failed／aborted／unknown 和需处理提醒仍独立投递。缺开始不改写明确 error／aborted 证据，但不把无起点的 stop 推为 completed。
- root 保存已关联的同一 Run 引用；child shutdown 不删除已收束观察。每根最多保留 64 个 child run、每 run 最多 64 段和 128 个交互；导出最多 64 段／64 个交互，深度 8；观察计数饱和于 4096。24 小时 TTL 在下一次公开事件到达时清理，不为保留数据启动常驻轮询。重启不恢复历史；根结束后的迟到结果不修改已发通知，也不并入新根周期。

#### 组件字段

两组件均必填整数 `version=1`、`timeBasis`、布尔 `partial`／`truncated`、`partialReasons`。原因按以下固定顺序去重：`missing_parent`、`ambiguous_parent`、`missing_start`、`missing_end`、`loading_gap`、`invalid_parent_graph`、`depth_limit`、`truncated`、`clamped`、`clock_changed`。`partial` 与原因非空一致，`truncated` 与同名原因一致；观察／展示计数是当前有界资料，不是完整调度计数。

`subagentTimeline`：

- `timeBasis=omp_agent_observation`；`observedItemCount` 为 0–4096，`displayedItemCount` 与 `items` 长度相同且不大于观察计数；组件至多 24 KiB。
- 每项必填匿名 `ref`、`cycleRef`、`parentRef`、`depth`（1–8）、`attempt`（1–4096，本 cycle 的观察段序号，**不是重试次数**）、`status`（`running|continued|completed|failed|aborted|unknown`）、`timingQuality`（`observed|partial|unknown`）。第一段 ref 等于 cycleRef，后续段使用新 ref。parentRef 指向父 cycle，根为顶层 cycle.ref。重复段、非法父图／深度和自环拒绝。
- 可选 `name`、`agent`（各 96 字符）、`model`（128）、实际 `thinking`（32）、`metadataChanged`、`startOffsetMs`／`endOffsetMs`／`durationMs`。时间相对根观察开始，严格非负整数且不越可靠根边界。只有完整可靠段允许 duration，等于 end−start；缺端点或裁剪段不补精确时长。name 来自公开显式名称；不从 task 参数、输出或原始 ID 提取可读任务名。

`userWaitTimeline`：

- `timeBasis=omp_interaction_observation`；`observedIntervalCount` 为 0–4096，`displayedIntervalCount` 与 `intervals` 长度相同且不大于观察计数；组件至多 12 KiB。字段名沿用共用结构，**不代表全部都是等待用户**。
- 每项必填匿名 `ref`、所属 cycle 的 `agentRef`、`kind`（`approval|plan_review|ask|background_wait`）、`intervalState`。`complete` 必须有 start／end／duration／result；`right_censored` 只有 start；`left_censored` 只有 end／result；`unknown` 没有时间和 result。result 为 `resolved|cancelled|unknown`，只表达公开调用解决结果，不表示答案正文或业务成功。
- 审批记录 requested→resolved；计划记录 review opened→resolved；ask 记录实际工具边界，不扫描 eval 源码；background_wait 记录 awaitingAsyncWork→下一次 agent_start。含处理时间或无 UI 的区间仍保留其原义。缺端点不参与精确统计。

覆盖率复用共有 renderer 的可靠区间并集算法，分母仅用可靠根窗口。子代理观察与后台／交互不跨语义相加；同类或汇总观察区间重叠按并集去重。根窗口减去覆盖率的剩余时间不得标为“主代理执行”。不完整资料只能展示已观测量／下界，不能声称完整计时。

公开来源：[身份与工厂绑定](https://github.com/can1357/oh-my-pi/blob/v18.12.6/packages/coding-agent/src/sdk.ts#L2785-L2805)、[身份注入](https://github.com/can1357/oh-my-pi/blob/v18.12.6/packages/coding-agent/src/sdk.ts#L3494-L3510)、[owner scoped snapshot](https://github.com/can1357/oh-my-pi/blob/v18.12.6/packages/coding-agent/src/session/agent-session.ts#L3060-L3068)。不使用 `task:subagent:*` 私有通道。跨模块副本、跨进程或未加载实例没有历史补全保证；相同实例在新开始后收到没有 cycle ID 的旧事件也无法凭公开字段恢复其原始归属。

### 共用卡片与降级

- OMP root `omp.run_finished` 直接进入共有主卡／时间线入口，不改写为 OpenCode 事件。简单流程在主卡摘要，复杂或缺失边界的流程使用同一个时间线附图、布局计算和图片投递链路。
- 主卡展示实例、会话、cwd basename、规范结果、公开模型／实际 thinking、当前运行耗时及子代理统计；缺失名称匿名回退，其他缺失元数据自然省略。模型与 thinking 在 `agent_start`、`turn_start`、`before_provider_request`、`agent_end` 边界采样，只读公开元数据，不读取请求 payload。观察到切换后即使恢复原值，也不把该周期显示为单一模型／档位。
- 子代理条目用“周期／段”区分复用和续跑。OMP 标签明确为“观察”，审批、计划审阅、ask、后台等待各有独立轨道；汇总按并集，不称为纯人工等待。不完整组件显示已观测量、下界或不可用；没有可靠区间时不显示精确的零覆盖率。
- 精简规则：没有子代理、交互或后台等待且观察完整时，不显示时间线区域、不生成附图；成功卡不附固定说明语；默认 `main` 代理不显示；字段使用“thinking”“开始时间”“耗时”；页脚只保留事件时间。主卡的时间线只保留一行摘要和交互汇总，详细指标仅在附图中出现。范围说明只在缺口、截断或降级时显示。名称中的私用区图标字符（如 Nerd Font）在展示前移除，全为图标时按缺失处理。
- 文本使用相同规范字段、观察统计和等待分类。图片生成失败沿用文本降级；仅附图失败时仍发送有效主卡。没有从 raw、正文、上下文占用或模型请求参数补充展示数据。
- OpenCode 保留 Question／Permission 与既有时间线语义，Markdown 保留原有内容渲染及目标路由。OMP 的观察语义不覆盖其他 Provider 的契约。

### 资源与投递结果

所有观察 handler 同步返回 `undefined`，不 await 网络，不更改事件或返回控制值。进程内至多 128 个会话、每周期 128 个交互去重项，保留至多 24 小时；到限停止保留并记录固定诊断，不建立持久队列。发送队列至多 64 条、并发 2、每条最多 3 次，重试间隔 250ms／1000ms；429 的 Retry-After 最多延长到 5 秒。超时默认 5 秒，配置限制 100–30000ms；受限响应正文至多 8 KiB。退出只尽力移交，不阻塞退出。

| 客户端结果分类 | 判定及重试 |
| --- | --- |
| `delivered` | HTTP 2xx、`code=0`、`message=ok`、`data.delivered=true`；只代表接收端报告送达。 |
| `policy_skipped` | 有效 `skipped`，非重放；不重试。 |
| `partial_delivery` / `partial_failure` | 受限业务结果；不自动补齐目标，不换 ID 重试。服务端可能已终结幂等记录。 |
| `replayed` | `skip_reason=idempotency_replay`；不证明本次或原请求实际送达，不重试。 |
| `permanent_error` | 401/403 及其他非暂态 4xx；不重试。 |
| `transient_error` / `transport_unknown` | 429、5xx、网络错误或超时，有界重试且保持同一 ID。超时不能推断服务端未发送。 |
| `unknown` | 非 JSON、超限、错误 shape、200 缺少有效业务确认；不视为送达，不盲目重发。 |

URL 与 Token 任一缺失／非法均不发送，日志只含固定分类、HTTP 状态、尝试次数及匿名事件引用，不含 URL、Token、响应正文、异常原文。拒绝重定向，避免凭据跨目标转发。有限内存投递不是 exactly-once 保证。

### V1 接收兼容与纠错

`omp.session_stop`／`session_stop` 的 body／header 兼容形式继续接收；省略 version、`1` 与历史 `"1.0"` 允许，其他 version 拒绝。V1 `lastAssistant.stopReason=error|aborted` 分别修正为 `failed|aborted`；`stop` 及其他缺证据情形为 `unknown`，因为 V1 没有最终 `willContinue` 证据。原始请求的字段继续解析，但不将完整路径、原始 ID 或 raw 元数据带入默认展示。

仅规范 `completed` 参与最短完成时长过滤；V2 可靠短成功任务现在会被默认 15 秒阈值过滤，失败／中止／待处理／未知不受此过滤。V1 不伪造最终成功，因此不因上述纠错新增时长过滤；它没有 V2 的运行／交互／thinking 元数据。

### API 与验收层级

依据 OMP `v18.12.6` 的 [公开事件](https://github.com/can1357/oh-my-pi/blob/v18.12.6/packages/coding-agent/src/extensibility/shared-events.ts)、[收束 producer](https://github.com/can1357/oh-my-pi/blob/v18.12.6/packages/coding-agent/src/session/agent-session.ts#L4698-L4914)、[审批与工具包装](https://github.com/can1357/oh-my-pi/blob/v18.12.6/packages/coding-agent/src/extensibility/extensions/wrapper.ts#L233-L520)、[计划审阅](https://github.com/can1357/oh-my-pi/blob/v18.12.6/packages/coding-agent/src/modes/interactive-mode.ts#L6910-L6973)。本地事件回归、真实 HTTP、原生 OMP、线上 AstrBot 分别记录，源码调查不代替运行验收。先部署支持 V2 的服务端，再独立授权切换客户端；同步暂停不等于真实 Hook 停用。

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
- OpenCode 可选模型档位字段 `modelVariant`：优先来自 Assistant `info.variant`，缺失时来自 `session.model.variant`；仅在同时存在 `model` 时于展示副本中原样追加到模型值，例如 `cpa/gpt-6-sol(max)`，不单独显示。该字段不保证等同于 provider 原始 `reasoning_effort`/`reasoningEffort`，不根据 provider/model 推断。
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

## v1.3.0 新增范围

以下向后兼容能力已纳入 `v1.3.0` 稳定公共契约；正式 tag、GitHub Release 与远端正式 ZIP 已验证，插件市场安装/更新与生产 AstrBot 远端资产 smoke 仍待验证：

- 修复 OpenCode Desktop 会话元数据回退路径中的标题回退行为；该修复不把 anomaly 诊断信号表述为已经确认的 Desktop 内部根因。
- 通用 `markdown` provider 使用现有 Endpoint Path 与 Bearer 鉴权提供信息推送：只接受 `event=markdown.message`，允许字段为 `event`、`id`、`title`、`markdown`、`target_alias`；`markdown` 必须为非空字符串并受 32,768 字符与 64 KiB 限制，可选字段有界校验，未知字段拒绝，内容只由受限 Markdown parser 渲染，不执行 Jinja 或远程资源。
- OpenCode 根会话上下文保持向后兼容：子会话卡片保留当前会话并显示“所属主会话”，完整 root-cycle 时间线显示“所属会话”。出站只允许安全清洗后的 `session.rootName`；父链使用匿名 `parentRef` 且最多解析 16 层，缺失、环路或超深时省略，不传 raw ID、`parentID`、`rootRef`、路径或原始对象；未携带该可选字段的旧 payload 继续接受。
- 兼容部署顺序保持为：先升级并重载 AstrBot 服务端，再部署并完全重启 OpenCode Client；旧服务端 strict allowlist 可能拒绝新增的 `session.rootName`。

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
