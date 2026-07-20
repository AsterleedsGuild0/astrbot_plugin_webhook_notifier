# AstrBot Webhook Notifier PRD

## 文档信息

- 文档类型：PRD
- 文档版本：v0.2.0
- 对应插件版本：v0.2.0
- 状态：v0.2.0 Final；Unreleased / 下一版本 Registry v2 与多 Bot 管理能力已实现
- 最后更新：2026-07-20
- 项目名称：`astrbot_plugin_webhook_notifier`
- 产品名称：Webhook Notifier
- 目标仓库：`AsterleedsGuild0/astrbot_plugin_webhook_notifier`
- 当前阶段：HTML 卡片、WebUI 模板管理、Registry v2 与多 Bot 安全管理已交付到 Unreleased
- 首个目标集成：`oh-my-pi` / OMP `session_stop` 任务完成通知
- 目标平台：AstrBot `aiocqhttp` 与 `qq_official`；OneBot 验证环境使用 NapCat，QQ 官方普通群真实主动 Webhook / OMP 图片卡片 smoke 已通过

---

## 背景

当前 `oh-my-pi` 已具备在会话结束后向外部 URL 发送 HTTP Webhook 的能力。Webhook payload 中包含会话、轮次、模型、耗时、输入长度、图片数量、消息增量和最后一条 assistant 元数据等信息。

我们希望在 AstrBot 侧提供一个通用通知插件，使外部工具可以通过 Webhook 把任务完成、构建结果、仓库事件或自定义事件推送到指定聊天会话中。

第一阶段不直接调用平台 HTTP API，而是通过 AstrBot 的消息抽象发送到目标会话。当前命令与 Registry 契约覆盖 `aiocqhttp` 和 `qq_official`；OneBot 验证环境使用 NapCat，QQ 官方 WebSocket 私聊与普通 QQ 群真实主动 Webhook / OMP 图片卡片均已验证。该结论不免除 QQ 官方主动消息规则、额度和 Bot 授权范围限制；其他 AstrBot 平台适配器不进入当前支持承诺。

---

## 产品定位

Webhook Notifier 是一个 AstrBot 通用 Webhook 通知插件。

它接收来自外部系统的 HTTP Webhook 事件，完成鉴权、事件识别、标准化、模板渲染和目标路由，并将通知发送到指定 AstrBot 会话。

核心定位：

- 面向 AstrBot 的通用通知入口。
- 第一阶段服务 `oh-my-pi` / OMP 任务完成通知。
- 后续扩展到 OpenCode、GitHub、GitLab、Gitea、自定义 JSON 和 CloudEvents 风格事件。
- 第一阶段声明支持 AstrBot `aiocqhttp` 与 `qq_official` 的已验证能力边界；QQ 官方普通群主动发送 smoke 已通过，但不外推到 Guild 或 `qq_official_webhook`。
- 支持纯文本通知和 HTML 卡片图片通知。
- HTML 模板由管理员在插件详情页维护，不允许普通聊天用户随意提交 HTML。
- Registry v2 提供独立离线 `platform_id` rebind helper，用于 adapter 实例 ID 变化后的 managed record 运维迁移；不扩展聊天命令或 Plugin Page UI。

Rebind 的产品边界是单写者离线维护：dry-run 为零写入只读操作，execute/rollback 必须在 AstrBot 与插件停止后显式确认执行。该流程保持 endpoint path 与 Token 效力，永久废弃全部 pending，并通过 durable backup、digest guard 和脱敏 audit 控制 rollback。

Registry v2 的产品隔离键固定为 managed `(owner_platform_id, owner_user_id, endpoint_name)` 与 pending `(owner_platform_id, request_id)`。v1 首次加载透明迁移：可安全归属的记录进入 managed，无法唯一归属的 legacy 记录进入 quarantine；迁移先备份、后原子提交，重复加载幂等，非法或不一致数据 fail-closed。quarantine 只保留兼容投递，普通用户不可发现、认领或管理。

---

## 目标用户

### AstrBot 管理者

需要把自动化工具、开发工具和运维任务的结果推送到 QQ 群或私聊。

### 自动化工具使用者

希望在任务结束时收到结构化通知，例如 `oh-my-pi` 会话完成、部署完成、测试失败、代码审查完成等。

### 插件维护者

希望以可扩展的 provider / adapter 方式支持更多 Webhook 来源，而不是为每个来源单独写一个不可复用插件。

---

## 核心场景

### 场景一：oh-my-pi 会话完成通知

`oh-my-pi` 在本轮会话结束时发送 `omp.session_stop` Webhook。

插件收到请求后：

1. 校验 `Authorization: Bearer <token>`。
2. 识别 `X-OMP-Event: session_stop` 或 payload 中的 `event: omp.session_stop`。
3. 将原始 payload 转换为标准化事件对象。
4. 根据配置选择默认目标 QQ 群或私聊。
5. 使用纯文本或 HTML 卡片模板生成通知。
6. 通过 AstrBot 消息发送能力投递。

安全默认行为：Webhook 状态通知默认只投递到群聊目标。`FriendMessage` 目标由全局配置 `enable_private_notifications` 控制，默认关闭；关闭时请求被明确标记为 `skipped`，不会触发发送和渲染，也不要求调用方重试。该策略不影响命令回复、Token 发送与验证、endpoint 创建或群聊通知。

### 场景二：HTML 卡片通知

管理员在插件详情页中查看内置模板、创建或复制自定义模板、编辑 HTML、调整画布宽度，并通过 JSON 数据预览后保存或应用。

插件将标准化事件对象传入模板，调用 AstrBot 配置的 T2I 服务渲染为图片，然后发送到 QQ。

如果 T2I 服务失败、返回非图片数据、截图超时或图片发送失败，插件自动降级为纯文本通知。

自定义 active 模板渲染失败时，插件先尝试内置模板；内置模板也失败时才进入纯文本降级链路。

### 场景三：后续接入 OpenCode

OpenCode 或相关工具在任务结束时发送类似 `opencode.session_stop` 的事件。

插件新增 OpenCode provider，把 payload 转换为同一套标准化事件对象，复用路由和模板渲染能力。

### 场景四：用户自助申请 Webhook Token

用户通过私聊 Bot 申请自己的 Webhook Token，并选择通知推送目标。

支持两类目标：

- 私聊：token 只能推送到申请者与 Bot 的私聊会话。
- 群聊：token 只能推送到经过校验的指定群聊。

群聊目标必须满足：

- Bot 能在收到验证命令的目标群内处理事件。
- `aiocqhttp`：原申请者必须在预指定群内，且是该群群主或群管理员。
- `qq_official`：目标群内任一群主或群管理员都可批准，不要求批准者是 C2C 申请者；批准者的 `member_openid` 不与 private owner 比较或建立映射。

由于跨适配器在私聊上下文中查询群成员角色不一定可靠，MVP 推荐使用两步验证：

1. 用户私聊 Bot 发起群聊 Webhook Token 申请。
2. 插件生成一次性验证请求。
3. 群管理员到目标群内发送验证命令。
4. `aiocqhttp` 确认执行者既是原申请者，也是预指定群的群主或群管理员；`qq_official` 仅确认当前目标群执行者是 owner/admin，不要求其是原 C2C 申请者。
5. aiocqhttp 校验后进入 tokenless active、清理 pending，并提示原申请者私聊 rotate；QQ 官方群校验后仍保持 pending，将 phase 转为 waiting-owner，提示原 C2C 申请者在同 platform 私聊 confirm。群内不发送 URL 或 Token。

---

## 用户交互流程

Webhook Notifier 的用户路径分为两个阶段：

本文用 `<唤醒词>` 表示 AstrBot 当前会话的 `wake_prefix`。默认 `wake_prefix=["/"]` 时命令为 `/whn ...`；改为 `!` 时使用 `!whn ...`；空前缀时使用裸命令 `whn ...`。运行时若配置缺失、类型异常、含控制字符或读取失败，帮助输出使用安全占位符 `<AstrBot唤醒词>` 和诊断提示；静态产品文档仍统一使用 `<唤醒词>`。

- 阶段 A：人机交互。用户与 Bot 交互，申请 token、验证权限并绑定推送目标。
- 阶段 B：机机交互。用户从 Plugin Page 复制 Base URL，并与聊天中获得的 Endpoint Path、Token 一起配置外部系统。

### 私聊目标申请流程

适用于用户希望把 OMP 任务完成通知推送到自己与 Bot 的私聊。

```text
用户私聊 Bot：<唤醒词>whn token new private my-omp
  ↓
Bot 创建 endpoint/token
  ↓
Bot 将 endpoint 绑定到申请者私聊会话
  ↓
Bot 先通过正常结果返回安全摘要，再绕过 RespondStage 单次 direct send 仅含 Bearer Token 的 Plain
  ↓
用户从 Plugin Page 复制 Base URL，并结合 Endpoint Path 和 Token 配置 OMP
  ↓
后续 OMP 自动向 Webhook 推送，会通知到该用户私聊
```

产品要求：

- token 明文只在 private create、rotate 或 QQ 官方 confirm 成功时展示一次。
- 第一条创建摘要不得包含 Token 明文或完整 URL；第二条只能包含 `Bearer Token: <token>`。
- 所有聊天输出不得包含 URL scheme、host、domain、`public_base_url` 值或 Webhook URL 环境变量赋值。
- 私聊 token 只能投递到申请者私聊。
- 用户可通过私聊命令查看、轮换、撤销或永久删除自己创建的 endpoint。

### 群聊目标申请流程

适用于用户希望把 OMP 任务完成通知推送到指定 QQ 群。

```text
aiocqhttp 用户私聊 Bot：<唤醒词>whn token new group <数字群号> my-group-omp
或 qq_official 用户私聊 Bot：<唤醒词>whn token new group current my-group-omp
  ↓
Bot 创建待验证申请
  ↓
Bot 私聊返回 request_id、code 和群内验证命令
  ↓
群主或群管理员到目标群发送：<唤醒词>whn token verify <request_id> <code>
  ↓
Bot 在群消息事件中校验：
  - Bot 在该群内
  - aiocqhttp：执行者是原申请者，且是预指定群群主或群管理员
  - qq_official：执行者是当前群任一群主或群管理员，不要求是 C2C 申请者
  ↓
aiocqhttp：激活为 tokenless endpoint 并清理 pending，不生成 Token
qq_official：record 仍为 pending，pending 转 waiting-owner，不生成 Token、不清理 pending
  ↓
aiocqhttp：Bot 提示创建者主动私聊 rotate
qq_official：Bot 提示原申请者同平台私聊 <唤醒词>whn token confirm <request_id>
  ↓
私聊 rotate/confirm 成功后，先收到摘要，凭据由单次 direct send 独立发送
  ↓
后续 OMP 自动向 Webhook 推送，会通知到该 QQ 群
```

产品要求：

- `aiocqhttp` 的 `<数字群号>` 直接填写目标 QQ 群号，并在申请时预绑定。
- `qq_official` 只接受字面量 `current`，不接受数字群号或 `group_openid`；群 verify 只批准并暂存原始 `group_openid`，private confirm 才完成绑定。
- QQ 官方群验证只读取原始 `author.member_openid` 与 `author.member_role`；仅 `owner`、`admin` 通过，缺失或未知角色 fail-closed。`member_openid` 不与 private owner 比较或建立映射。
- `<唤醒词>whn token confirm <request_id>` 必须由原 C2C 申请者在同一 `platform_id` 私聊执行；成功后直接领取一次 Token。
- 验证码默认 10 分钟有效；QQ 官方群 verify 与 private confirm 共用创建时的同一不可延长 expiry。
- 验证失败时不创建 token。
- `aiocqhttp` 非申请者不能代替申请者完成 verify；QQ 官方允许目标群任一 owner/admin 批准，但不能代替原申请者 private confirm。
- 非群主/群管理员不能完成群 verify。
- token 明文不在群聊中展示，验证成功后也不主动私聊发送凭据。
- pending 验证码只以哈希形式持久化；aiocqhttp verify 成功后清理 pending，QQ 官方 verify 只清 challenge 并转 waiting-owner，confirm 成功才删除 pending。
- QQ 官方 confirm 已提交后若 Token 消息发送失败，不回滚、不延长 expiry；原申请者通过私聊 rotate 恢复凭据。

### 后续机机交互流程

用户完成 token 配置后，后续流程不再需要人机交互。

```text
OMP / 外部系统
  ↓ POST /webhook/u/{owner_hash}/{endpoint_name}
Webhook Notifier
  ↓ 鉴权、解析、渲染、发送
AstrBot OneBot v11 消息平台
  ↓
绑定的 QQ 私聊或群聊
```

产品要求：

- 外部系统不能通过 payload 指定任意 UMO。
- payload 最多只能选择 endpoint 白名单内的 target alias。
- token 泄露时，影响范围限制在该 endpoint 绑定的目标白名单内。

---

## 非目标

当前阶段不包含：

- GitHub / GitLab 全量事件适配。
- 多租户和复杂模板权限模型。
- 持久化消息队列、失败重试队列和审计数据库。
- 允许普通聊天用户动态提交 HTML。
- 直接调用 OneBot HTTP API。
- 将插件设计成外部 IME 通用平台网关。
- 任意 JavaScript 执行和远程资源加载能力。
- endpoint 级模板选择；active 模板当前为插件全局设置。
- 单全局 token 的 simple mode。该模式适合单人单目标自用，但不进入 MVP；后续以独立 issue 评估是否补充。
- 对抗、规避或绕过 OneBot、NapCat、QQ 或其他平台的风控与安全机制。
- 为不同 QQ 平台或 adapter 分叉插件仓库；当前保持单仓、UMO 统一路由和 Sender 集中投递策略。
- 根据 UMO 自动识别 NapCat。当前 UMO 只能可靠表达 `platform_id` 与 `FriendMessage` / `GroupMessage`，不能可靠区分不同 OneBot 实现。

---

## 总体链路

```text
外部系统 Webhook
  ↓
HTTP Endpoint
  ↓
鉴权与基础校验
  ↓
Provider Adapter
  ↓
标准化事件对象
  ↓
路由匹配
  ↓
Renderer
  ├─ text
  └─ html_image
       ↓
     AstrBot html_render / T2I
  ↓
AstrBot send_message
  ↓
QQ 群聊 / 私聊
```

---

## MVP 功能范围

### Webhook Endpoint

MVP 需要提供一个 HTTP Webhook 入口。

候选路径：

```text
/webhook/omp-session
```

或更通用：

```text
/webhook/u/{owner_hash}/{endpoint_name}
```

MVP 应支持多个用户各自拥有独立 endpoint/token，至少需要支持多个 endpoint 记录。`endpoint_name` 在同一个申请用户内唯一；不同用户可以使用相同的 `endpoint_name`。URL 使用 `owner_hash` 做用户命名空间隔离，`owner_hash` 由 `owner_user_id` 计算稳定短 hash，不直接暴露 QQ 号或平台用户 ID。

每个 endpoint/token 必须绑定允许投递的目标白名单，避免 payload 直接指定任意 QQ 私聊或群聊。

### 鉴权

MVP 使用 Bearer Token：

```http
Authorization: Bearer <token>
```

鉴权规则：

- 未配置 token 时默认拒绝启动 Webhook 服务，避免误暴露。
- 请求缺少 `Authorization` 时返回 401。
- token 不匹配时返回 401。
- 不支持从 URL query 中读取 token。

Token 应由插件生成，而不是由用户手动指定。用户通过私聊申请后获得 token，并配置到 `oh-my-pi` / OMP 推送端。

### Token 申请与目标绑定

MVP 需要支持用户自助申请 token。

私聊目标申请：

- 用户在私聊中申请。
- 插件生成 endpoint/token。
- token 的目标白名单只包含申请者私聊会话。

群聊目标申请：

- 用户在私聊中指定目标群。
- 插件生成待验证申请。
- 用户需要在目标群中完成验证命令。
- 插件确认用户和 Bot 都在群内，且用户是群主或群管理员。
- 校验通过后，插件把 endpoint/token 私聊发给用户。

目标绑定原则：

- endpoint/token 决定允许推送的目标。
- payload 不允许直接传入任意 UMO。
- payload 最多只能选择该 endpoint 白名单内的 target alias。

### Token 模型取舍

MVP 选择 endpoint/token 绑定目标白名单，而不是单全局 token。

选择原因：

- 最小权限：token 只能投递到绑定的私聊或群聊目标。
- 泄露隔离：某个用户 token 泄露时，只影响该 endpoint 的目标白名单。
- 独立撤销：可以单独 revoke 或 rotate 某个用户的 endpoint token。
- 名称复用：已撤销或已过期的 endpoint 不应继续占用用户命名空间；用户可以用相同名称重新创建 endpoint。
- 永久删除：用户可在私聊执行 `<唤醒词>whn token delete <名称>`，仅删除当前 platform + owner scope 内的 `revoked` / `expired` managed record。删除不可恢复，删除后 Path 返回 404、旧 Token 永久无效；`active` 必须先 revoke，`pending_verification` 不提供强制删除，quarantine 与其他平台/owner 的同名记录不可见且不受影响。
- 管理员 Registry 操作仅允许 AstrBot 全局超级管理员在私聊执行。`list` 只展示 managed 最小元数据且绝不展示 Token；`revoke-path` 按完整 Path 精确撤销；`revoke-owner` 按 platform、owner 与名称精确撤销。所有结果和审计均脱敏，不允许模糊匹配或跨平台推断。
- 可审计：每次请求都能关联 endpoint、owner、provider 和目标。
- 防越权：payload 即使携带 target alias，也只能选择白名单内目标。

单全局 token 的问题：

- 泄露后影响全局。
- 无法可靠区分调用者。
- 撤销或轮换会影响所有用户。
- 如果允许 payload 指定目标，容易造成任意投递风险。

简单模式评估结论：

- simple mode 可作为后续增强，用于单人、单目标、内网部署或快速试用。
- simple mode 不进入 MVP，避免在第一版同时维护两套鉴权和路由语义。
- 后续如需要支持，应新建 issue 单独设计，要求默认关闭，并明确标注安全边界。

### OMP Provider

MVP 支持 `oh-my-pi` / OMP `session_stop` payload。

事件识别优先级：

1. HTTP Header：`X-OMP-Event: session_stop`
2. Payload 字段：`event: omp.session_stop`

MVP 只处理：

```text
omp.session_stop
```

其他事件返回 202 或 400 需在实现阶段确定。

### 目标会话配置

MVP 支持配置默认推送目标。

目标格式优先使用 AstrBot UMO：

```yaml
targets:
  - name: default_group
    umo: aiocqhttp:GroupMessage:123456789
  - name: owner_private
    umo: aiocqhttp:FriendMessage:10001
```

MVP 可以先只支持一个默认目标，后续支持多目标和规则路由。

### 渲染模式

支持两种模式：

```text
text
html_image
```

`text` 为稳定默认模式。

`html_image` 使用 HTML 模板渲染图片，但必须启用文本兜底。

MVP 阶段渲染模式由插件全局 `render_mode` 统一决定，所有 endpoint/token 跟随全局配置。endpoint 级渲染覆盖能力保留为后续扩展，避免旧 token 持久化值阻止全局切换。

### 失败降级

当 HTML 渲染失败时，如果 `fallback_to_text` 为 true，发送纯文本摘要。

降级触发条件包括：

- `html_render` 抛出异常。
- T2I 服务返回非图片内容。
- 返回图片路径不存在或不可读。
- 图片 magic number 校验失败。
- 图片发送构造失败。

---

## 标准化事件对象

插件内部不直接把原始 payload 作为模板主接口，而是转换为标准化事件对象。

示例：

```json
{
  "provider": "omp",
  "event": "omp.session_stop",
  "version": 1,
  "id": "session-id:turn-id",
  "title": "会话完成",
  "status": "success",
  "summary": "",
  "source": {
    "name": "oh-my-pi",
    "url": null
  },
  "actor": {
    "name": null,
    "url": null
  },
  "fields": [
    {"label": "会话", "value": "Add post-conversation HTTP hook"},
    {"label": "cwd", "value": "/home/user/project"},
    {"label": "模型", "value": "openai/gpt-5.5"},
    {"label": "开始时间", "value": "2026-07-08 19:59:00 UTC+08:00"},
    {"label": "耗时", "value": "57.7s"},
    {"label": "输入", "value": "977 字 / 1 张图"},
    {"label": "消息变化", "value": "+2"}
  ],
  "links": [],
  "raw": {}
}
```

设计原则：

- 模板优先使用 `title`、`status`、`summary`、`fields`、`links`。`summary` 可为空；默认通知不应在 `summary` 中重复展示已由 `fields` 表达的会话名和模型名。
- `raw` 仅供高级模板或调试使用。
- 后续 GitHub、GitLab、OpenCode provider 都转换为同一结构。

---

## OMP Payload 映射

根据当前 `oh-my-pi` hook 设计，MVP 关注这些字段：

```text
event
version
emittedAt
session.id
session.file
session.cwd
session.name
session.model
round.turnId
round.startedAt
round.endedAt
round.durationMs
round.promptLength
round.imageCount
round.entryCountDelta
round.messageCountDelta
round.stopHookActive
round.lastAssistant.provider
round.lastAssistant.model
round.lastAssistant.stopReason
round.lastAssistant.durationMs
metadata.version
metadata.eventName
```

`session.model` 可能是字符串，也可能是包含 `provider`、`id`、`name` 的对象。通知展示模型名时，对象优先使用 `provider/name`，其次使用 `provider/id`，缺少 provider 时退化为模型名，避免直接展示完整对象。若 `session.model` 缺失，则回退到 `round.lastAssistant.model`，并可使用 `round.lastAssistant.provider` 拼接 provider。

默认文本通知展示 `session.cwd` 与格式化后的 `round.startedAt`，便于定位任务上下文与开始时间；时间格式为本地时间加 UTC 偏移，例如 `2026-07-08 19:59:00 UTC+08:00`。`round.endedAt` 默认不展示，仅在 `round.durationMs` 缺失时参与耗时计算。后续 HTML 渲染模板可以自行选择展示或隐藏这些字段。

`session.file` 不作为默认会话名兜底，避免在 `session.name` 缺失时把机器生成的 `.jsonl` 文件名展示到群聊通知中；如后续 HTML 模板确实需要，可通过高级模板显式读取 raw 字段。

默认不在群聊通知中展示完整 `round.prompt`，避免泄露敏感输入或造成刷屏。

如需要展示 prompt，应通过配置显式开启，并限制最大长度。

---

## 文本通知模板

MVP 默认文本通知示例：

```text
[oh-my-pi] 会话完成

会话：Add post-conversation HTTP hook
cwd：/home/user/project
模型：openai/gpt-5.5
开始时间：2026-07-08 19:59:00 UTC+08:00
耗时：57.7s
输入：977 字 / 1 张图
消息变化：+2
最后状态：stop
```

文本模板要求：

- 即使字段缺失也能生成可读消息。
- 不输出 token、Authorization、完整 raw payload。
- 长字段需要截断。

---

## HTML 卡片模板

### 模板来源

HTML 模板只允许来自插件侧受信任配置：

- 插件内置默认模板。
- 管理员通过插件详情页创建和维护的自定义模板。

不允许普通聊天用户通过消息动态提交 HTML。

### 默认卡片内容

默认卡片应包含：

- 标题。
- 状态徽标。
- 会话名称。
- 模型。
- 耗时。
- 输入规模。
- 消息变化。
- 文件或工作目录摘要。
- 生成时间。

### 模板运行安全

模板设计原则：

- CSS 尽量内联。
- 不依赖外部 JS。
- 不依赖外部 CSS。
- 不依赖远程字体。
- 不默认加载远程图片。
- 不暴露 token、请求头或敏感环境变量。

### T2I 参数

默认渲染参数：

```json
{
  "full_page": true,
  "type": "png",
  "quality": 90,
  "timeout": 5000,
  "viewport_width": 812,
  "viewport_height": 1200,
  "device_scale_factor_level": "high",
  "wait_until": "domcontentloaded"
}
```

参考现有经验：

- 使用 AstrBot 配置的 `html_render` / T2I 服务。
- 借鉴 GPT Image2 的图片结果校验和纯文本降级策略。
- 借鉴 T2I 服务对 `domcontentloaded`、跳过字体等待、context 重建和截图错误诊断的处理。

---

## 配置设计

MVP 初始配置：

```yaml
enabled: true
enable_private_notifications: false
render_mode: html_image
fallback_to_text: true
server:
  host: 127.0.0.1
  port: 18080
  public_base_url: "https://example.com/webhook"
targets: |
  - name: default_group
    umo: aiocqhttp:GroupMessage:123456789
endpoints: |
  - name: user_private_omp
    path: u/0a1b2c3d4e5f/user_private_omp
    provider: omp
    token_hash: "由插件生成并保存哈希"
    owner_user_id: "10001"
    targets:
      - user_private
render_options: |
  {
    "full_page": true,
    "type": "png",
    "quality": 90,
    "timeout": 5000,
    "viewport_width": 812,
    "viewport_height": 1200,
    "device_scale_factor_level": "high",
    "wait_until": "domcontentloaded"
  }
```

后续扩展配置：

```yaml
routes:
  - name: omp_default
    match:
      provider: omp
      event: omp.session_stop
    targets:
      - default_group
    template: omp_session_stop.html

providers:
  omp:
    enabled: true
    include_prompt: false
    max_prompt_length: 500
```

MVP 中 `webhook_token` 不应作为全局共享 token 长期使用。更推荐保存 endpoint 级 token 哈希，并通过私聊申请流程生成和展示一次性明文 token。

`enable_private_notifications` 是 Webhook 状态通知的全局安全开关：

- 默认 `false`，所有 `FriendMessage` 通知目标在发送前被标记为 `skipped`。
- 不影响命令回复、private create、rotate、QQ 官方 confirm 的 Token 交付、Token 验证、endpoint 创建与群聊通知。
- 升级时保留现有 endpoint registry 与 Token；开启配置并 reload 后即可恢复私聊通知，无需重建 endpoint。
- 混合目标继续投递群聊，私聊目标跳过，整体结果为 `partial_delivery`。

---

## 安全要求

### Token 安全

- 使用 `Authorization: Bearer <token>`。
- token 由插件生成，并绑定 endpoint、申请用户和目标白名单。
- 持久化时只保存 token 哈希，不保存明文 token；哈希方案使用基于本地 `server_secret` 的 HMAC-SHA256。
- 不支持 URL query token。
- 日志中不得打印 token 原文。
- README 和示例中不得包含真实 token。
- token 明文只在 private create、rotate 或 QQ 官方 confirm 成功时通过私聊发送给申请者。
- private create、rotate 与 QQ 官方 confirm 的摘要可进入 `MessageEventResult`；凭据不得进入该结果或 RespondStage，只能由一次 `event.send()` 发送恰好一个关闭 T2I/Markdown 的 Plain，内容仅为 `Bearer Token: <token>`。
- direct send 期间日志只按当前 Token 精确值脱敏；失败不回滚、不重试，用户通过同平台私聊 rotate 恢复。
- 群聊 verify 不生成、暂存或主动发送 token 明文；aiocqhttp 通过私聊 rotate 领取，QQ 官方通过原申请者私聊 confirm 领取。
- 聊天输出允许展示不含 scheme、host、domain 的 `Endpoint Path`，但不得展示任何完整 URL 或 configured domain。
- 普通聊天文本意外包含符合 `whn_` 明文格式的 Token 时必须替换为 `[Token 已隐藏]`；只有 private create、rotate、QQ 官方 confirm 的专用第二条凭据结果允许原样交付。
- aiocqhttp 验证后的 tokenless active endpoint 在 rotate 前返回稳定的 `403 token_unclaimed`；QQ 官方 confirm 前保持 pending，不进入 active。

### 目标权限安全

- 私聊 token 只能推送到申请者私聊。
- 群聊 token 只能推送到通过验证的目标群。
- 群聊验证按 adapter 分流：aiocqhttp 要求原申请者在预指定群为 owner/admin；QQ 官方允许目标群任一 owner/admin 批准，原申请者控制权由后续同平台 private confirm 单独校验。
- 群聊验证使用一次性 `request_id + code`，默认 10 分钟有效，验证成功、过期或取消后失效。
- 群管理员识别基于目标群的群消息事件：读取 AstrBot OneBot v11 群对象中的群主和管理员字段，并结合 AstrBot 管理员判断。
- payload 不允许指定任意 UMO，只能在 endpoint 白名单范围内选择 target alias。

### 平台投递安全默认值

- Webhook 状态通知默认禁止主动投递到 `FriendMessage`，管理员必须在确认平台规则、额度和风险后显式开启。
- OneBot/NapCat 主动私聊存在真实风控风险；产品不承诺规避风控，也不提供对抗方案。
- QQ 官方 Bot 的主动私聊与主动消息同样受官方规则和额度约束，不视为无限安全替代方案。
- 平台策略保持在统一 Sender 中集中执行，不分叉平台仓库。只有出现第二个真实 adapter 差异后，才评估提取独立 delivery policy 能力层。
- 当前 UMO 信息不足以可靠识别 NapCat，不得基于 `aiocqhttp` 或 `FriendMessage` 宣称自动识别具体 OneBot 实现。

### 请求安全

- 限制 body 大小。
- 只接受 JSON 请求。
- 对未知事件做明确处理。
- 记录必要错误摘要，但不记录完整敏感 payload。
- 插件内置 Webhook HTTP Server 默认只监听 `127.0.0.1`，不直接承担公网 TLS 证书管理。
- 公网暴露 Webhook 时必须通过 HTTPS；HTTPS 终止由 Nginx、Caddy、Cloudflare Tunnel 或其他部署层组件负责。
- 不建议直接把插件 HTTP 端口裸露到公网。

### 模板安全

- 普通聊天用户不能提交模板。
- 模板渲染使用标准化数据对象。
- 文本模板和 HTML 模板均使用 Jinja2 语法，模板上下文根变量统一为 `event`。
- 自定义模板视为管理员受信任输入，但仍应避免外部资源依赖。

#### HTML 转义决策

HTML renderer 使用 Jinja2 `SandboxedEnvironment` 并启用 `autoescape`。上下文只提供 `event` 根变量；模板内部可使用受控的 `namespace` helper。保存和渲染阶段均拒绝危险 HTML/CSS、事件属性、脚本、外部资源和手工 CSP，渲染结果会注入限制性 CSP。

### 输出安全

- 默认不发送完整 prompt。
- 默认不发送完整 raw payload。
- 文件路径和工作目录可通过配置控制是否展示。

---

## 错误处理

### HTTP 层

- 401：鉴权失败。
- 400：JSON 无效、必要字段缺失或事件类型不支持。
- 413：请求体过大。
- 202：事件已接收但未投递，适用于未来异步模式。
- 200：事件已处理。

### 渲染层

HTML 渲染失败时：

1. 记录渲染错误类型、阶段和安全摘要。
2. 尝试纯文本降级。
3. 若文本发送也失败，记录错误并返回失败状态。

### 发送层

目标不可达或 UMO 格式错误时：

- 记录目标名称和错误类型。
- 不在日志中输出 token 或完整 payload。
- MVP 可直接失败，后续再加入重试。

安全策略跳过不是发送失败：全为私聊且开关关闭时返回 HTTP 200、`message=skipped`、`retryable=false`、`rendered=false`；混合目标中群聊成功而私聊跳过时返回 `message=partial_delivery`。只有实际调用发送 API 后发生真实发送失败时才标记 `retryable=true`。

---

## 可观测性

MVP 需要有基础日志：

- 插件初始化状态。
- Webhook 服务启动状态。
- 请求鉴权失败计数摘要。
- 接收到的 provider 和 event。
- 渲染模式。
- 渲染失败原因。
- 发送目标名称和发送结果。
- 每个 Webhook 请求的 request id。

状态命令应展示：

- 插件是否启用。
- Webhook 服务是否运行。
- active endpoint 数量。
- 默认渲染模式。
- fallback 是否开启。
- 监听 IP、监听端口、基础路径三个独立字段。
- 固定提示 `Base URL：请在 Plugin Page 中复制`，不得展示 configured `public_base_url`。
- 当前 active 模板及有效模板状态。

---

## MVP 验收标准

### 基础验收

- 插件可被 AstrBot 加载。
- `<唤醒词>webhook_notifier` 和 `<唤醒词>whn` 可返回状态摘要。
- 未配置 token 时不会误启动公网 Webhook 服务。
- 配置 schema 可在 AstrBot 插件配置中展示。
- 用户可通过私聊申请私聊目标 token。
- 用户可通过私聊发起群聊目标 token 申请，并在目标群完成管理员验证。

### Webhook 验收

- 使用正确 Bearer Token 发送 OMP `session_stop` payload 后，插件能识别事件。
- 使用错误 Token 时返回 401。
- 非 JSON 请求被拒绝。
- 未知事件不会导致插件崩溃。
- 不同 endpoint/token 能路由到不同的私聊或群聊目标。
- payload 不能绕过 endpoint 白名单指定任意 UMO。

### 消息验收

- 能向配置的 QQ 群发送纯文本通知。
- 默认不向配置的 QQ 私聊发送 Webhook 状态通知，并返回不可重试的 `skipped` 结果。
- 显式开启 `enable_private_notifications` 并 reload 后，现有私聊 endpoint 可恢复发送，无需重建 endpoint 或 Token。
- HTML 图片模式下能发送卡片图片。
- T2I 失败时能自动降级纯文本。
- 混合群聊与私聊目标在默认配置下继续发送群聊，私聊标记为 `skipped`，整体为 `partial_delivery`。

### 安全验收

- 日志不包含 Bearer Token 原文。
- 持久化数据不保存 token 明文。
- 默认通知不包含完整 prompt。
- 模板只能由插件侧配置。
- 非群主/群管理员不能为群聊目标创建 token。

---

## 迭代计划

### Milestone 0：项目骨架

- 初始化插件仓库。
- 增加 README、metadata、配置 schema 和状态命令。
- 编写 PRD。

### Milestone 1：文本链路 MVP

- 启动 Webhook HTTP 入口。
- 实现 endpoint 级 Bearer Token 鉴权。
- 实现私聊自助申请 token。
- 实现群聊 token 申请与群管理员验证。
- 实现 OMP `session_stop` parser。
- 实现默认目标 UMO 发送。
- 实现纯文本模板。

### Milestone 2：HTML 卡片 ✅

- 增加默认 HTML 卡片模板。
- 调用 AstrBot `html_render` / T2I 服务。
- 实现图片结果校验（PNG/JPEG/WebP）。
- 实现渲染失败纯文本降级。
- 响应标记实际 render_mode、fallback_to_text、fallback_reason。

### WebUI 模板管理 Phase 0 ✅

- 在 Plugin Page 中本地打包 Monaco Editor 0.52.2 与 Vite 6.4.3。
- 使用 4 个 inline workers，完成 `asset_token` 与 sandbox 环境验证。
- 发布包约 1.335 MB。

### WebUI 模板管理 Phase 1 ✅

- 提供模板列表、内置模板只读、新建、复制、删除、保存、应用和保存并应用。
- 提供 dirty 离开确认、HTML Monaco 编辑器、JSON 预览数据与 sandbox `srcdoc` 预览。
- 模板持久化采用 version 1 registry 与不可变 revision 文件；active 模板全局生效。
- 管理员无需手工维护模板文件或通过聊天命令 reload。

### Milestone 3：可扩展 Provider

- 抽象 provider adapter。
- 增加 OpenCode provider。
- 增加 custom JSON provider。
- 预留 GitHub / GitLab provider。

### Milestone 4：路由与模板增强

- 支持多 endpoint。
- 支持多目标路由。
- 支持按 provider/event/session 条件匹配。
- 支持 endpoint/路由级模板选择和主题扩展。

---

## MVP 产品决策

- Webhook HTTP 服务使用插件内独立 HTTP server，默认监听 `127.0.0.1`；公网暴露由用户部署层处理，公网访问必须使用 HTTPS，TLS 终止不由插件自身负责。
- MVP 使用 endpoint/token 绑定目标白名单模型，不提供单全局 token simple mode。
- MVP 使用 Bearer Token；GitHub/GitLab 等平台签名校验留到对应 provider 阶段。
- token 哈希使用 HMAC-SHA256，token 轮换后旧 token 立即失效。
- MVP 渲染模式全局生效；endpoint/token 不单独决定 `render_mode`。
- 每个 Webhook 请求生成 request id，并贯穿响应、日志、渲染和发送结果。
- MVP 不要求 OMP payload 提供稳定 `status` 字段；`omp.session_stop` 默认视为 `success`，后续如果 payload 提供状态再映射。
- 默认通知展示完整 `session.cwd` 和格式化后的 `round.startedAt`，不展示完整 `session.file`、`round.prompt` 与 `round.endedAt`；后续 HTML 模板可自行选择展示或隐藏字段。
- `session.name` 缺失时默认不输出会话字段，不使用 `session.file` basename 兜底。
- HTML 模板默认自包含，不支持任意本地静态资源目录。
- 群聊 token 验证使用群内验证命令；MVP 不依赖适配器原生群成员查询。
- Webhook 私聊通知采用默认关闭的全局安全开关；该开关不改变 endpoint registry，也不影响命令与 Token 交互。
- 平台投递策略保持单仓、UMO 统一路由和 Sender 集中治理；出现第二个真实 adapter 差异后再评估独立能力层。

---

## 后续评估项

- 是否提供 UMO 目标格式 WebUI 辅助生成。
- 是否提供 simple mode 作为单人单目标快速试用模式。
- 是否为 GitHub/GitLab provider 增加 HMAC 签名校验。
- 是否支持受限本地静态资源目录。
- 是否支持适配器原生群成员查询以减少群内验证步骤。

---

## 相关参考

- `astrbot_plugin_gpt_image2`：文本转 HTML 卡片、`html_render` 调用、图片结果校验、纯文本兜底。
- `astrbot-t2i-service`：HTML/T2I 服务、截图参数、Playwright 稳定性处理。
- `astrbot_plugin_github_webhook`：GitHub Webhook 接收、签名校验和目标投递思路。
- AstrBot UMO 和消息发送能力：MVP 用于对接当前 OneBot v11 消息平台，并避免插件直接调用 OneBot HTTP API。
- [平台投递策略](platform-delivery-policy.md)：QQ 平台证据、默认私聊策略、架构边界与运维建议。
