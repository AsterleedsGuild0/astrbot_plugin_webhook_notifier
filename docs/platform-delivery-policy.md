# 平台投递策略

## 文档信息

- 适用插件：`astrbot_plugin_webhook_notifier`
- 适用版本：v0.2.0 及后续兼容版本
- 最后更新：2026-07-20
- 主题：Webhook 状态通知在 QQ 平台上的私聊安全默认值与架构边界

工作区通用原则见 [`AstrBot 跨平台主动投递策略`](../../../.opencode/astrbot-platform-delivery-policy.md)；多 Bot 实例下 endpoint、Token、owner 与目标 UMO 的归属规则见 [`AstrBot 跨插件多 Bot 实例数据隔离`](../../../.opencode/astrbot-multi-bot-data-isolation.md)。Registry v2 已实现，本文承载 Webhook Notifier 的具体配置、响应与迁移契约。

---

## 已验证平台与能力矩阵

平台声明表示已验证基础兼容，不表示该平台所有场景均已验证。

| 平台 / 接入方式 | 验证状态 | 已验证能力与边界 |
| --- | --- | --- |
| `aiocqhttp` | 已验证 | 既有命令、群聊通知、HTML 图片卡片 |
| `qq_official` WebSocket 私聊 | 已验证 | AstrBot v4.26.6 私聊命令、Webhook 鉴权、private 主动消息、OMP 状态图片卡片 |
| `qq_official` WebSocket 普通 QQ 群 | 已验证 | 真实主动 Webhook、OMP `session_stop`、HTML/T2I 图片卡片已成功送达；仍受官方主动消息规则、额度与 Bot 授权范围约束 |
| `qq_official` WebSocket QQ 频道（Guild） | 不支持、暂无支持计划 | 当前命令、身份和群验证状态机仅覆盖私聊与普通 QQ 群，不外推到 QQ 频道 |
| `qq_official_webhook` | 不支持、暂无支持计划 | 不在 `metadata.yaml` 的 `support_platforms` 中；插件不适配该接入方式 |

插件元数据仅声明 AstrBot 标准 adapter key `aiocqhttp` 与 `qq_official`。WebSocket 是 `qq_official` 的接入方式，不使用自定义的 `qq_official_websocket` key。普通 QQ 群的验证结果只覆盖上述真实 smoke 环境，不免除官方规则、额度与授权范围限制。QQ 频道（Guild）和 `qq_official_webhook` 明确不在插件支持范围内，也没有支持计划。

---

## 问题背景

Webhook Notifier 会把外部系统的状态事件主动发送到 AstrBot 会话。群聊与私聊虽然都通过 UMO 路由，但平台对主动私聊、主动消息的限制和风险不同，不能把“技术上可调用发送 API”等同于“平台允许无限发送”。

OneBot/NapCat 路径存在账号风控风险；QQ 官方 Bot 路径也受主动消息规则和额度约束。插件因此采用保守默认值：Webhook 状态通知默认不主动投递到 `FriendMessage`，由管理员在确认当前平台规则、额度和风险后显式开启。

该限制只针对外部 Webhook 触发的状态通知，不影响用户已经发起交互后的命令回复。aiocqhttp 群验证后由创建者私聊 rotate；QQ 官方群 verify 后由原 C2C 申请者在同一 Bot 私聊 confirm，均不主动私聊发送凭据。

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

本文中的用户命令示例应使用 `<唤醒词>whn ...`。AstrBot 默认 `wake_prefix=["/"]` 时实际命令为 `/whn ...`；改为 `!` 时使用 `!whn ...`；空前缀时使用裸命令 `whn ...`。

### 全局配置

```yaml
enable_private_notifications: false
```

- 类型为 bool，默认 `false`。
- 仅控制 Webhook 状态通知的 `FriendMessage` 目标。
- 不影响命令回复、private create、rotate、QQ 官方 confirm 的 Token 交付、Token 验证、endpoint 创建与群聊通知。
- 配置开启并 reload 后，已有私聊 endpoint 恢复投递，无需重建 endpoint 或轮换 Token。

### 目标处理

- 仅群聊目标：正常渲染并发送。
- 仅私聊目标且开关关闭：不渲染、不发送，返回 HTTP 200、`message=skipped`、`delivered=false`、`rendered=false`、`retryable=false`。
- 群聊与私聊混合且开关关闭：群聊正常发送，私聊标记为 `skipped`，返回 HTTP 200、`message=partial_delivery`、`retryable=false`。
- 可投递目标在实际调用发送 API 后发生真实发送失败：逐目标标记 `failed`，此时才设置 `retryable=true`。渲染失败不标记为可重试发送失败。

当一次请求的全部目标都因私聊通知关闭而在渲染前跳过时，服务端会写入一条请求级 INFO 日志，包含 `request_id`、provider、event、`result=skipped`、`reason=private_notifications_disabled`、跳过目标数量与 `rendered=false`。该日志只写一次，不包含 endpoint、owner、目标名称、UMO、Token、Authorization、请求 body 或真实 URL，可通过 `request_id` 和 `reason` 关联调用方响应与服务端策略判定。

策略性跳过表示服务端已经按配置完成处理，不应触发外部系统重试。否则重试只会重复请求，并不能改变管理员关闭私聊通知的事实。

### 命令凭据投递边界

- private create、rotate 与 QQ 官方 confirm 先 yield 无凭据摘要，再调用当前事件的 `event.send()` 一次，直接发送恰好一个关闭 T2I/Markdown、仅含 Bearer Token 的 Plain。敏感消息不得进入 RespondStage。
- direct send await 期间仅按当前 Token 精确值过滤 root/AstrBot/botpy/aiocqhttp 日志；finally 移除。失败不回滚或重试，提示同平台私聊 rotate 恢复；不承诺网络 exactly-once。
- aiocqhttp 必须由原申请者在预指定群以 owner/admin 身份 verify；成功后进入 tokenless `active`、清理 pending，并提示原申请者私聊 rotate。
- QQ 官方允许目标群内任一 owner/admin 批准，不要求批准者是 C2C 申请者；群 verify 后 record 仍为 pending，pending 转为 `group_verified_waiting_owner` 且不清理。原申请者随后在同一 `platform_id` 私聊 confirm，才激活 endpoint、生成 Token、删除 pending。
- group verify 只在当前群事件中返回无凭据摘要，不主动私聊发送 URL 或 Token。QQ 官方 confirm 与 private create、rotate 一样，通过当前私聊回复独立交付一次 Token。
- QQ 官方两个 phase 共用创建时的不可延长 expiry。confirm 的 Token 消息发送失败不回滚、不延长或恢复 pending；原申请者通过私聊 rotate 恢复可用凭据。
- 所有聊天回复不展示完整 URL、configured domain 或 URL 环境变量赋值；Base URL 只通过认证后的 Plugin Page bridge 展示。
- 普通聊天结果中的 `whn_` Token 明文由 sanitizer 隐藏，只有 private create、rotate、QQ 官方 confirm 的专用第二条凭据消息允许原样返回；aiocqhttp 群 endpoint 在 rotate 前返回 `token_unclaimed`，不会触发主动私聊补发。

### 升级兼容

针对 `enable_private_notifications` 安全默认值的 v0.2.0 升级不会改变 Endpoint Registry 中既有凭据和目标语义：

- 现有私聊 endpoint 继续存在。
- 现有 Token 继续有效并可完成鉴权。
- 目标白名单不删除 `FriendMessage`。
- 无需重建 endpoint，也无需 rotate Token。

Registry v1 到 v2 的首次加载会另行执行透明、幂等迁移：可安全归属的记录进入 managed，无法唯一归属的 legacy 记录进入 quarantine；迁移使用私有备份与原子提交，失败时 fail-closed。默认关闭期间，Webhook 请求在投递阶段被安全跳过；管理员确认风险后开启配置并 reload，即可使用迁移后仍有效的原 endpoint 与 Token 恢复发送。

---

## 架构决策

### 保持单仓与统一路由

当前保持：

- 单一插件仓库。
- UMO 统一路由。
- Sender 集中执行投递策略与最终兜底。
- Endpoint Registry 继续只负责鉴权、owner、provider 与目标白名单，不保存临时平台投递开关。
- 用户永久删除 Endpoint 时严格按当前 `platform_id + owner_user_id + name` 隔离，仅允许 `revoked` / `expired` managed record；不会跨平台清理 pending，不会删除 quarantine。删除后该 path 的投递鉴权统一返回 404 `not_found`，同名重建的新 Token 不会恢复旧 Token 的权限。

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
