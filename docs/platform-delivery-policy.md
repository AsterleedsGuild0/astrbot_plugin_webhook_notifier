# 平台投递策略

## 文档信息

- 适用插件：`astrbot_plugin_webhook_notifier`
- 适用版本：v0.2.0 及后续兼容版本
- 最后更新：2026-07-16
- 主题：Webhook 状态通知在 QQ 平台上的私聊安全默认值与架构边界

工作区通用原则见 [`AstrBot 跨平台主动投递策略`](../../../.opencode/astrbot-platform-delivery-policy.md)；多 Bot 实例下 endpoint、Token、owner 与目标 UMO 的归属规则见 [`AstrBot 跨插件多 Bot 实例数据隔离`](../../../.opencode/astrbot-multi-bot-data-isolation.md)。本文仅保留 Webhook Notifier 的具体配置、响应与迁移契约，不表示尚未实现的 Registry v2 已完成。

---

## 问题背景

Webhook Notifier 会把外部系统的状态事件主动发送到 AstrBot 会话。群聊与私聊虽然都通过 UMO 路由，但平台对主动私聊、主动消息的限制和风险不同，不能把“技术上可调用发送 API”等同于“平台允许无限发送”。

OneBot/NapCat 路径存在账号风控风险；QQ 官方 Bot 路径也受主动消息规则和额度约束。插件因此采用保守默认值：Webhook 状态通知默认不主动投递到 `FriendMessage`，由管理员在确认当前平台规则、额度和风险后显式开启。

该限制只针对外部 Webhook 触发的状态通知，不影响用户已经发起交互后的命令回复，也不阻断 Token 创建、验证与管理流程。

---

## 证据与可信度边界

### 强证据：平台官方规则

- [QQ 官方 Bot 发送消息文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html)：QQ 官方平台对主动消息、主动私聊的适用条件和额度有明确规则。具体要求可能调整，运维决策应以该官方页面的当前内容为准。
- [AstrBot aiocqhttp 平台文档](https://docs.astrbot.app/platform/aiocqhttp.html)：说明 AstrBot 对 OneBot v11 / aiocqhttp 平台的接入方式，可用于确认平台配置边界，但不替代 QQ 或具体实现的风控规则。
- [AstrBot QQ 官方 Bot WebSocket 文档](https://docs.astrbot.app/en/platform/qqofficial/websockets.html)：说明 AstrBot 对 QQ 官方 Bot 的接入方式，可用于区分官方 Bot adapter 与 OneBot 路径。

官方文档是判断 QQ 官方 Bot 能否发送主动消息的主要依据。本文件不固化任何无法长期确认的频控数字、时间窗口或额度值，避免文档在平台规则更新后误导运维。

### 中等证据：实现方公开资料

- [NapCatQQ issue #751](https://github.com/NapNeko/NapCatQQ/issues/751)：公开讨论主动私聊等行为与风控现象，证明风险不是纯理论假设。Issue 中的个别环境、触发条件和结果不应被外推为所有账号的固定阈值。
- [NapCat 安全指南](https://napneko.github.io/other/security)：提供实现方的安全与风险说明，应作为 OneBot/NapCat 运维的重要参考。

这些资料足以支持“主动私聊存在真实风险”和“默认应保守”的产品决策，但不足以推导统一、安全且可规避风控的发送频率。插件不提供对抗风控方案。

### 弱证据与禁止外推

- 单个用户反馈、短期测试成功或某个账号未触发风控，只能说明该次环境可用，不能证明长期安全。
- UMO 中出现 `aiocqhttp`、`FriendMessage` 或某个 `platform_id`，不能证明底层实现一定是 NapCat。
- QQ 官方 Bot 使用官方 API，不等于可以无限主动发送；仍必须遵守官方规则和额度。

---

## OneBot 与 QQ 官方 Bot 的差异

| 维度 | OneBot / aiocqhttp 路径 | QQ 官方 Bot 路径 |
| --- | --- | --- |
| AstrBot 接入参考 | [aiocqhttp](https://docs.astrbot.app/platform/aiocqhttp.html) | [QQ 官方 Bot WebSocket](https://docs.astrbot.app/en/platform/qqofficial/websockets.html) |
| 平台身份 | 通常连接个人 QQ 或兼容 OneBot 的实现 | QQ 开放平台官方 Bot |
| 主动私聊风险 | 存在账号风控与实现差异，NapCat 公开资料已记录相关风险 | 受官方主动消息场景、规则和额度限制 |
| 是否无限安全 | 否 | 否 |
| 插件默认策略 | `FriendMessage` Webhook 通知默认关闭 | 同样默认关闭，确认官方规则和额度后再开启 |

当前插件不根据 adapter 名称自动采用不同默认值。原因是 UMO 当前只能可靠提供 `platform_id` 与 `FriendMessage` / `GroupMessage` 等会话类型，不能可靠判断 OneBot 背后的具体实现，也不应仅凭名称推断平台能力。

---

## 当前投递策略

### 全局配置

```yaml
enable_private_notifications: false
```

- 类型为 bool，默认 `false`。
- 仅控制 Webhook 状态通知的 `FriendMessage` 目标。
- 不影响命令回复、Token 创建或轮换后的发送、Token 验证、endpoint 创建与群聊通知。
- 配置开启并 reload 后，已有私聊 endpoint 恢复投递，无需重建 endpoint 或轮换 Token。

### 目标处理

- 仅群聊目标：正常渲染并发送。
- 仅私聊目标且开关关闭：不渲染、不发送，返回 HTTP 200、`message=skipped`、`delivered=false`、`rendered=false`、`retryable=false`。
- 群聊与私聊混合且开关关闭：群聊正常发送，私聊标记为 `skipped`，返回 HTTP 200、`message=partial_delivery`、`retryable=false`。
- 可投递目标在实际调用发送 API 后发生真实发送失败：逐目标标记 `failed`，此时才设置 `retryable=true`。渲染失败不标记为可重试发送失败。

策略性跳过表示服务端已经按配置完成处理，不应触发外部系统重试。否则重试只会重复请求，并不能改变管理员关闭私聊通知的事实。

### 升级兼容

升级不会修改 Endpoint Registry：

- 现有私聊 endpoint 继续存在。
- 现有 Token 继续有效并可完成鉴权。
- 目标白名单不删除 `FriendMessage`。
- 无需重建 endpoint，也无需 rotate Token。

默认关闭期间，Webhook 请求在投递阶段被安全跳过。管理员确认风险后开启配置并 reload，即可使用原 endpoint 与 Token 恢复发送。

---

## 架构决策

### 保持单仓与统一路由

当前保持：

- 单一插件仓库。
- UMO 统一路由。
- Sender 集中执行投递策略与最终兜底。
- Endpoint Registry 继续只负责鉴权、owner、provider 与目标白名单，不保存临时平台投递开关。

Webhook handler 在渲染前执行 preflight，避免全 skipped 请求产生无意义的 HTML/T2I 成本。Sender 在实际发送入口再次检查策略，防止未来调用方绕过 preflight。Renderer 不负责平台判断。

### 为什么不按平台分叉仓库

当前平台差异只影响投递许可，不改变 Webhook 鉴权、provider 解析、标准化事件、模板、registry 或大部分发送契约。此时分叉会带来：

- 重复维护鉴权、模板、路由和安全修复。
- 不同仓库之间行为与响应结构漂移。
- 用户迁移 endpoint 和 Token 的额外成本。
- 在无法可靠识别具体 OneBot 实现时制造虚假的自动平台区分。

因此，当前差异应由集中策略表达，而不是复制整个插件。

分叉只在以下情况之一长期成立时进入评估，不代表自动执行：

- 平台依赖或插件生命周期发生不可兼容冲突。
- 共享的 UMO、事件、发送或响应契约无法表达平台需求。
- 平台专属代码长期显著超过总代码约 30%，并造成持续维护阻塞。

“约 30%”是架构评估信号，不是机械阈值；仍需结合依赖、发布节奏、测试隔离和用户迁移成本判断。

---

## 未来能力层的触发条件

当前不提前创建 `core/delivery_policy.py`。只有出现第二个真实 adapter 差异，并且简单的 `FriendMessage` 全局 bool 无法准确表达时，才提取独立 delivery policy 能力层。

触发信号包括：

- 至少两个已验证 adapter 对主动私聊存在不同且稳定的允许条件。
- 需要依据 adapter 能力、会话类型或官方授权状态返回不同策略结果。
- Sender 中出现重复的平台分支，影响测试与维护。
- 需要统一表达 `allowed`、`skipped`、`failed`、原因与可重试属性。

能力层提取后仍应保持：

- Endpoint Registry 不承担动态平台策略。
- UMO 作为统一路由输入。
- Sender 作为最终执行与兜底边界。
- 未知 adapter 使用保守默认值。

在 UMO 或 AstrBot API 未提供可靠实现标识前，能力层也不得把 `aiocqhttp` 自动等同于 NapCat。

---

## 运维建议

### 开启前

1. 确认当前 AstrBot adapter 及其实际底层平台，不要仅凭 UMO 名称推断为 NapCat。
2. OneBot/NapCat 环境阅读公开 issue 与安全指南，接受主动私聊可能带来的账号风险。
3. QQ 官方 Bot 环境核对官方主动消息规则、适用场景与当前额度，以官方文档为准。
4. 优先选择群聊通知；只有业务确实需要私聊时才开启全局配置。
5. 确认外部 Webhook 调用方能正确理解 HTTP 200 `skipped` 与 `partial_delivery`，不会把策略跳过当作失败重试。

### 开启与回退

```yaml
enable_private_notifications: true
```

修改后 reload 插件，使配置生效。无需重建 endpoint 或 Token。

如观察到平台警告、账号异常、规则变化或不确定风险，应立即将配置恢复为 `false` 并 reload。关闭后群聊通知、命令回复和 Token 管理流程继续工作。

### 监控与排障

- 统计 `delivered`、`skipped` 与 `failed`，不要把三者合并为单一失败率。
- `private_notifications_disabled` 是预期策略结果，不应触发告警风暴或自动重试。
- 只有实际调用发送 API 后的真实发送失败才依据 `retryable=true` 进入重试策略。
- 日志不得记录 Token、Authorization、完整 payload 或无法证实的平台实现推断。
- 平台规则可能变化，应定期复核上述官方与实现方链接，不在本地文档固化未经确认的频控数字。
