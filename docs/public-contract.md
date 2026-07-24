# Webhook Notifier 公共契约

## 文档状态

- 稳定版本：`v1.0.0`
- 状态：Final / 1.x 稳定公共契约
- 定稿日期：2026-07-21
- 远端发布状态：`v1.0.0` Git tag、GitHub Release 与正式 ZIP 已发布
- 当前源码候选：`v1.1.0-rc.1`，尚未发布

`v1.0.0` 是现有稳定版。当前源码包含尚未发布的 `v1.1.0-rc.1` 候选功能；OpenCode 集成、Provider Registry/DI 与相关 smoke 应使用该候选或后续版本，不应回溯描述为 `v1.0.0` 已发布能力。AstrBot WebUI 安装、Bot Endpoint 和 Desktop 端到端 smoke 的 RC 包验证仍须按实际执行结果单独留证。

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

## v1.1.0-rc.1 候选新增范围

以下是当前源码候选新增、尚未进入已发布 `v1.0.0` 资产的范围：

- #18：Provider Adapter / Registry 与依赖注入边界，`omp` / `opencode` provider 选择及 Endpoint provider 不可变。
- #19：OpenCode Server Adapter 与四类 V1 envelope。
- #20：OpenCode V1 Client Plugin、正确 `plugin` tuple、env/file 凭据、状态机、timeout/retry 和 at-least-once 语义。
- #21：严格白名单、匿名 session ref/name fallback、Bun/Python/CLI smoke 和集成文档。
- OpenCode 丰富通知字段：实例标识 `instanceDisplayName`、客户端自动推导的 `projectName`、会话名、agent、`provider/model`、会话开始时间、busy→idle 任务时间（无可靠周期时间时 fallback 到 Assistant 元数据）与可读耗时/低敏计数，以及默认 strict、显式 opt-in 的 `actionContentMode`；项目名只在详细字段中显示。
- OpenCode 可选模型档位字段 `modelVariant`：优先来自 Assistant `info.variant`，缺失时来自 `session.model.variant`；仅在同时存在 `model` 时于展示副本中原样追加到模型值，例如 `cpa/gpt-5.6-sol(max)`，不单独显示。该字段不保证等同于 provider 原始 `reasoning_effort`/`reasoningEffort`，不根据 provider/model 推断。
- OpenCode V1 `session.scope`（`root|subagent|auxiliary|unknown`）与 Client scope 判断；`parentID` 不是公共字段，绝不发送。默认精确识别 `smartfetch-secondary`，非空 `parentID` 始终优先为 `subagent`。
- OpenCode Permission/Question 瞬时聚合：同一 Session、同一类型固定 150ms debounce；Permission 与 Question 不合并，不同 Session 不合并；回复在 flush 前撤销。Permission envelope 使用 `permission: {count, items[]}`，Question 保持 `question: {count, optionCount, summary?, items?}`。
- 全局 `notification_mode` 仅允许 `focused`（默认）和 `all`；`focused` 只抑制 `subagent` 与 `auxiliary` 的 `completed`，unknown scope/status fail-open。策略过滤返回 HTTP 200、`message=skipped`、`scope`、`reason=notification_mode_filtered`、`skip_reason=notification_mode_filtered`、`rendered=false`、`delivered=false`、`retryable=false`，且不进入 renderer、T2I 或 sender。

`actionContentMode` 与 `notification_mode` 正交：前者只控制 Question/Permission 内容隐私，后者只控制通知是否发送。

聚合 bucket 只存在客户端进程内存，raw session ID 仅作本地 key；request ID 仅用于去重/撤销，不出站。`strict` 的 Permission item 仅允许 `category`，不会发送正文；`summary`/`full` 仍按 allowlist 和上限处理。

这些候选能力在 RC 包完成 AstrBot WebUI 手动安装、Bot Endpoint 和 Desktop 端到端 smoke 前，不得写成已验证发布能力。

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
