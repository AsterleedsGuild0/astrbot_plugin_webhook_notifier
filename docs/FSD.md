# AstrBot Webhook Notifier FSD

## 文档信息

- 文档类型：FSD（Software Functional Specification Document）
- 文档版本：v0.1.0
- 对应 PRD 版本：v0.1.0
- 对应插件版本：v0.1.0
- 状态：Final
- 最后更新：2026-07-09 15:03
- 项目名称：`astrbot_plugin_webhook_notifier`
- 产品名称：Webhook Notifier
- 目标仓库：`AsterleedsGuild0/astrbot_plugin_webhook_notifier`

---

## 目的与范围

本文档定义 Webhook Notifier MVP 的功能规格，作为后续实现、测试和验收的功能契约。

本文档描述：

- 插件功能模块与边界。
- HTTP Webhook 接口行为。
- 鉴权、事件识别和错误响应。
- OMP `session_stop` payload 适配规则。
- 标准化事件对象结构。
- 目标会话路由和消息发送规则。
- 文本渲染、HTML 卡片渲染和降级策略。
- 配置项、状态命令、安全与可观测性要求。

本文档不定义底层具体实现细节，例如具体使用哪个 HTTP 框架、文件拆分路径或内部类名；这些内容应在后续技术设计文档中确定。

---

## 系统边界

### 插件内部职责

Webhook Notifier 负责：

- 提供 Webhook HTTP 入口。
- 校验请求身份。
- 解析并识别外部事件。
- 将 provider 专用 payload 转换为标准化事件对象。
- 按配置选择目标会话。
- 渲染通知内容。
- 调用 AstrBot 消息发送能力投递通知。
- 记录安全、精简、可排障的运行日志。

### 插件外部依赖

Webhook Notifier 依赖：

- AstrBot 插件加载机制。
- AstrBot 配置系统。
- AstrBot 消息发送能力和 UMO 目标格式。
- AstrBot `html_render` / T2I 能力，用于 HTML 卡片图片模式。
- AstrBot OneBot v11 消息平台，用于 QQ 群聊、私聊和图片消息投递。
- 当前验证环境使用 NapCat 作为 OneBot v11 实现。

MVP 支持范围：

- 仅声明支持 AstrBot 集成的 OneBot v11 消息平台。
- 当前验证环境使用 NapCat 作为 OneBot v11 实现。
- 群主/群管理员识别依赖 OneBot v11 / aiocqhttp 路径中的群成员信息，例如 `event.get_group().group_owner` 和 `event.get_group().group_admins`。
- 其他 AstrBot 平台适配器未测试，不在 MVP 支持承诺内。

术语说明：

- 文档中的 QQ 群聊和 QQ 私聊，除非特别说明，均指经由 AstrBot OneBot v11 消息平台发送。
- 文档中的 target UMO 示例使用 `aiocqhttp:GroupMessage:<group_id>` 与 `aiocqhttp:FriendMessage:<user_id>`，代表当前 OneBot v11 平台路径。

### 插件不负责

Webhook Notifier 不负责：

- 管理底层 QQ 登录或 OneBot 连接。
- 适配或验证 OneBot v11 以外的 AstrBot 消息平台。
- 替代 GitHub/GitLab 的完整 App 集成。
- 提供开放公网网关服务的统一认证平台。
- 管理外部系统的任务生命周期。
- 保证 QQ 平台图片上传一定成功。

---

## 功能模块

### Webhook HTTP Server

提供 HTTP POST 入口，接收外部系统事件。

MVP 支持多个 endpoint。每个 endpoint 对应独立 path、provider、token 哈希、申请者和目标白名单。

```text
POST /webhook/omp-session
```

MVP 通用路径：

```text
POST /webhook/u/{owner_hash}/{endpoint_name}
```

`endpoint_name` 在同一个申请用户内唯一；`owner_hash` 用于隔离不同申请用户的同名 endpoint。

功能要求：

- 只接受 POST。
- 只接受 JSON body。
- 限制请求体大小。
- 在 token 未配置时不得启动公网可用 Webhook 服务。
- 默认监听 `127.0.0.1`，公网暴露由反向代理、隧道或用户部署层处理。
- 插件内置 HTTP Server 不直接管理 TLS 证书；公网访问必须通过 HTTPS，由 Nginx、Caddy、Cloudflare Tunnel 或其他部署层组件完成 TLS 终止。
- 不支持也不建议直接把插件 HTTP 端口裸露到公网。
- 处理完成后返回明确 JSON 响应。

### Auth

负责校验请求身份。

MVP 只支持 Bearer Token：

```http
Authorization: Bearer <token>
```

功能要求：

- token 由插件生成，并绑定 endpoint、申请者和目标白名单。
- token 明文格式为 `whn_` 前缀加 32 字节随机值的 URL-safe base64 字符串，由 `secrets.token_urlsafe(32)` 生成。
- 持久化时保存 token 哈希，不保存明文 token。
- token 哈希算法使用 `HMAC-SHA256(server_secret, token)`，其中 `server_secret` 为插件首次初始化时生成并保存在插件数据目录中的本地密钥；日志和状态命令不得展示该密钥。
- token 校验使用 constant-time compare。
- token 明文只在创建或轮换时通过私聊展示一次。
- endpoint 无 token 哈希时不应处理外部请求。
- 不支持 URL query token。
- 不在日志中打印 token 原文。

MVP 不支持单全局 token 作为 Webhook 调用凭据。骨架阶段的 `webhook_token` 配置仅作为早期占位，MVP 实现时应迁移为 endpoint 级 token registry。

### Token Model Decision

MVP 采用 endpoint/token 绑定目标白名单模型：

```text
endpoint path + bearer token → endpoint registry record → allowed targets
```

该模型的功能要求：

- 每个 endpoint 独立 token。
- 每个 token 绑定 owner user id。
- 每个 token 绑定 provider。
- 每个 token 绑定 target whitelist。
- token 可按 endpoint 独立撤销和轮换。
- 请求审计至少能关联 endpoint、owner、provider 和目标。

不采用单全局 token 的原因：

- 单 token 泄露会影响全局。
- 单 token 无法区分调用者身份。
- 单 token 轮换会影响所有用户。
- 若单 token 搭配 payload 指定目标，容易产生任意投递风险。
- 若单 token 再叠加目标白名单，本质上会重新演化为 endpoint/token 模型。

### Simple Mode Evaluation

simple mode 指单人单目标的简化配置，例如一个全局 token 和一组默认目标。

评估结论：

- simple mode 不进入 MVP。
- simple mode 适合单人自用、单目标、内网部署或快速试用。
- simple mode 会引入第二套鉴权和路由语义，增加 MVP 复杂度和安全文档负担。
- 后续如需要支持，应通过独立 issue 规划，并默认关闭。

后续 simple mode 必须满足：

- 明确标注仅建议单人/内网使用。
- 默认不允许 payload 指定任意 UMO。
- 如果支持多个目标，也必须使用 target alias 白名单。
- 支持从 simple mode 迁移到 managed endpoint mode。

### Token Provisioning

负责用户自助申请、验证、发放、轮换和撤销 Webhook Token。

MVP 支持两类申请：

- 私聊目标 token。
- 群聊目标 token。

私聊目标 token：

- 用户必须在私聊中申请。
- token 只能绑定申请者与 Bot 的私聊 UMO。
- 创建成功后，Bot 私聊返回 Webhook URL 和 token。

群聊目标 token：

- 用户在私聊中指定目标群。
- 插件创建待验证申请和一次性验证码。
- 用户到目标群发送验证命令。
- 插件在群消息事件中确认 Bot 在该群、申请者在该群，且申请者是群主或群管理员。
- 验证通过后，Bot 私聊返回 Webhook URL 和 token。

群聊验证成功后的私聊发送约束：

- 插件必须使用平台实例 ID 构造 UMO，例如 `event.get_platform_id():FriendMessage:<user_id>`，不能使用平台类型名 `event.get_platform_name()` 构造 UMO。AstrBot 的主动发送 API 按平台实例 ID 查找 adapter；使用平台类型名会导致 `cannot find platform for session ...` 并丢失私聊 token。
- 调用 `Context.send_message()` 后必须检查返回值；返回 `False` 时应视为私聊发送失败，不能向群内提示“Token 已发送”。
- 如果群聊验证已成功但私聊发送失败，endpoint 已进入 `active` 且 token 明文不可恢复。用户可在私聊中执行 `/whn token rotate <endpoint_name>` 重新生成并获取新 token。

待验证申请格式：

- `request_id` 使用 UUID4 字符串。
- `code` 使用 `secrets.token_hex(3)` 生成 6 位小写十六进制验证码。
- 默认有效期为 10 分钟。
- `request_id + code` 只能使用一次；验证成功、过期或取消后均应失效。
- 验证失败不展示正确验证码。

Token 生命周期：

- 创建：生成 token 明文、保存哈希、私聊返回明文。
- 轮换：生成新 token 并替换 token hash，旧 token 立即失效，无宽限期。
- 撤销：使用软删除，设置 `revoked_at`；默认不硬删除记录，便于审计。
- 列表：普通 `/whn token list` 默认只展示 active 和 pending_verification endpoint 的 name、path、provider、target aliases、render mode、created_at，不展示 token 明文和完整 token hash；revoked/expired 记录保留在持久化数据中用于审计，但不在默认列表中展示。
- 已撤销 endpoint 的请求返回 403 `endpoint_revoked`。

群管理员识别基于目标群的群消息事件：在群消息事件中读取 message/group 对象上的 `group_owner`、`owner`、`owner_id`、`group_admins`、`admins`、`admin_ids` 等字段，并结合 `event.is_admin()`。

不要求 MVP 在私聊上下文中直接调用适配器 API 查询群成员角色；若具体适配器支持，可作为后续优化。

如果当前消息平台事件无法提供群对象、群主字段和群管理员字段，且 `event.is_admin()` 也不能确认管理员身份，则群聊 token 验证必须失败，并向执行者提示“当前平台无法校验群管理员身份”。该场景不创建 endpoint，不返回 token。

#### Token Provisioning 状态机

Endpoint / token 申请记录使用以下状态：

| 状态 | 含义 | 可进入方式 | 可退出到 |
| --- | --- | --- | --- |
| `pending_verification` | 群聊 token 已申请但尚未完成群内验证 | 私聊 `/whn token new group <QQ群号> [name]` | `active`、`expired`、`revoked` |
| `active` | endpoint 可接受 Webhook 请求 | 私聊 token 创建成功，或群聊验证成功 | `revoked` |
| `expired` | 待验证申请过期 | 超过验证有效期 | 终态 |
| `revoked` | endpoint 已撤销 | 用户撤销、管理员撤销或安全策略撤销 | 终态 |

状态规则：

- 私聊 token 不进入 `pending_verification`，创建成功后直接进入 `active`。
- 群聊 token 必须先进入 `pending_verification`，验证通过后进入 `active`。
- `pending_verification` 记录不得接受 Webhook 请求。
- `expired` 和 `revoked` 记录不得接受 Webhook 请求。
- `rotate` 只允许作用于 `active` endpoint，轮换后仍保持 `active`，旧 token 立即失效。
- `revoke` 可作用于 `pending_verification` 或 `active` 记录。

#### 私聊目标申请序列

```text
User -> Bot(private): /whn token new private [name]
Bot -> Bot: 校验命令来自私聊
Bot -> Endpoint Registry: create endpoint(status=active, target=user_private)
Endpoint Registry -> Bot: endpoint path + token 明文
Bot -> User(private): 返回 Webhook URL、Bearer Token、OMP 环境变量示例
```

私聊返回内容必须包含：

- endpoint name。
- Webhook URL，由 `server.public_base_url + endpoint.path` 拼接；未配置 `public_base_url` 时返回本地 URL 并提示不可公网访问。
- Bearer Token 明文。
- OMP 配置示例：`OMP_SESSION_WEBHOOK_URL` 和 `OMP_SESSION_WEBHOOK_TOKEN`。
- 安全提示：token 只展示一次，泄露后应立即 rotate 或 revoke。

#### 群聊目标申请序列

```text
User -> Bot(private): /whn token new group <QQ群号> [name]
Bot -> Bot: 校验命令来自私聊
Bot -> Endpoint Registry: create pending verification(request_id, code, expires_at)
Bot -> User(private): 返回 request_id、code、过期时间和群内 verify 命令
User -> Bot(group): /whn token verify <request_id> <code>
Bot -> Bot: 校验 request_id/code、未过期、未使用、执行者为申请者
Bot -> OneBot v11 event: 校验当前群为目标群，且执行者是群主或群管理员
Bot -> Endpoint Registry: activate endpoint(status=active, target=group)
Bot -> User(private): 返回 Webhook URL、Bearer Token、OMP 环境变量示例
Bot -> Group(optional): 提示验证已完成，不展示 token
```

群聊验证失败分支：

- `request_id` 不存在、已过期或已使用：验证失败，返回 `verification_expired` 或等价提示。
- `code` 不匹配：验证失败，不展示正确 code。
- 执行者不是申请者：验证失败，不创建 endpoint。
- 当前群不是申请目标群：验证失败，不创建 endpoint。
- 执行者不是群主或群管理员：验证失败，返回 `group_permission_denied` 或等价提示。
- 平台无法提供群权限信息：验证失败，提示当前平台无法校验群管理员身份。

命令参数说明：

- `<QQ群号>` 是用户可见的目标 QQ 群号。
- 插件内部将 `<QQ群号>` 作为 OneBot v11 group id 使用，并在 endpoint registry 中保存为目标群标识。
- 用户不需要手动填写 `aiocqhttp:GroupMessage:<group_id>` 这样的 UMO；插件在验证通过后根据 QQ 群号生成目标 UMO。

#### Token 管理序列

查看 endpoint：

```text
User -> Bot(private): /whn token list
Bot -> Endpoint Registry: list endpoints by owner_user_id
Bot -> User(private): 返回 endpoint 摘要，不展示 token 明文和完整 hash
```

轮换 token：

```text
User -> Bot(private): /whn token rotate <endpoint_name>
Bot -> Endpoint Registry: 校验 owner_user_id 与 endpoint 状态
Bot -> Endpoint Registry: 生成新 token，替换 token hash
Bot -> User(private): 返回新 Bearer Token 和更新后的 OMP 配置示例
```

撤销 endpoint：

```text
User -> Bot(private): /whn token revoke <endpoint_name>
Bot -> Endpoint Registry: 校验 owner_user_id
Bot -> Endpoint Registry: 设置 revoked_at 与 status=revoked
Bot -> User(private): 返回撤销成功摘要
```

### Endpoint Registry

负责保存 endpoint/token/target 绑定关系。

Endpoint Registry 是用户通过 Bot 命令创建出来的运行时安全状态存储。它必须持久化到插件数据目录，例如 `data/webhook_tokens.json`。AstrBot 重启后，已创建且未撤销的 endpoint/token 绑定关系必须继续有效。

Endpoint Registry 的职责不是展示配置，而是作为 Webhook 鉴权和路由的唯一事实来源。用户创建私聊或群聊 token 后，插件必须把 owner、endpoint path、token hash、target whitelist 和状态写入 registry。

配置文件中的 `endpoints` 仅作为可读示例或管理视图，不作为 MVP 的运行时事实来源。MVP 不要求也不建议在用户通过命令创建、轮换、撤销 endpoint 后回写 AstrBot 插件配置。用户查看 endpoint 应使用 `/whn token list`，而不是读取配置文件。

每条记录至少包含：

- endpoint name。
- path。
- provider。
- token hash。
- token hash algorithm。
- owner user id。
- target whitelist。
- render mode。
- template。
- created at。
- revoked at。

持久化绑定要求：

- 私聊 token 创建后，registry 记录必须绑定申请者 `owner_user_id` 和申请者私聊 UMO。
- 群聊 token 验证通过后，registry 记录必须绑定申请者 `owner_user_id`、目标 QQ 群号和由该群号生成的群聊 UMO。
- token 明文不得持久化；registry 只保存 token hash 和 hash algorithm。
- endpoint `status=active` 且 `revoked_at=null` 时才可接受 Webhook 请求。

### Provider Adapter

负责处理不同外部系统的专用 payload。

MVP 仅要求实现 OMP provider。

后续 provider：

- `opencode`
- `github`
- `gitlab`
- `...`
- `custom`

每个 provider 输出统一的标准化事件对象。

### Normalized Event

标准化事件对象是 renderer、router 和 sender 的统一输入。

功能要求：

- provider adapter 必须输出该对象。
- renderer 不应强依赖 provider 原始 payload。
- raw payload 仅作为高级模板或调试用途。

### Router

负责根据配置选择目标会话和模板。

MVP 可只支持默认目标。

后续支持：

- 按 provider 匹配。
- 按 event 匹配。
- 按 endpoint 匹配。
- 按 payload 字段匹配。
- 多目标推送。

### Renderer

负责把标准化事件对象渲染为消息内容。

MVP 支持：

- `text`
- `html_image`

HTML 图片模式必须具备文本降级能力。

### Sender

负责把渲染结果发送到 AstrBot 会话。

功能要求：

- 支持 UMO 目标。
- 发送纯文本消息。
- 发送图片消息。
- 发送失败时返回结构化错误。

### Status Command

提供插件运行状态查看命令。

MVP 命令：

```text
/webhook_notifier
/whn
```

---

## HTTP 接口规范

### 请求方法

```http
POST
```

其他方法应返回 405 或由框架默认处理。

### 请求路径

MVP 推荐：

```text
/webhook/omp-session
```

如果实现通用 endpoint，则路径为：

```text
/webhook/u/{owner_hash}/{endpoint_name}
```

其中 `endpoint_name` 用于匹配申请者名下的 endpoint 名称；`owner_hash` 是由 `owner_user_id` 计算得到的稳定短 hash，用于 URL 命名空间隔离，不直接暴露 QQ 号或平台用户 ID。

命名规则：

- `endpoint_name` 在同一个 `owner_user_id` 下唯一。
- 不同 `owner_user_id` 可以创建相同的 `endpoint_name`。
- Registry 查询、轮换和撤销必须按 `owner_user_id + endpoint_name` 定位记录，不能只按全局 name 查询。
- Webhook 入站请求仍以完整 path 匹配 endpoint；真正鉴权仍依赖 `Authorization: Bearer <token>`。

### 请求头

必需：

```http
Content-Type: application/json
Authorization: Bearer <token>
```

OMP 推荐：

```http
X-OMP-Event: session_stop
User-Agent: omp-session-webhook
```

### 请求体

请求体必须是 JSON object。

请求体大小限制：

- 默认上限为 256 KiB。
- 若存在 `Content-Length` 且超过上限，应在读取 body 前返回 413 `payload_too_large`。
- 若缺少 `Content-Length`，服务端读取 body 时必须累计字节数；超过上限立即停止读取并返回 413 `payload_too_large`。
- 解析 JSON 前必须先完成大小限制检查。

MVP 不接受：

- 空 body。
- JSON array 作为顶层结构。
- 表单提交。
- `text/plain`。

### 成功响应

同步处理成功：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "request_id": "b8b7b3e2-1f3a-4b7e-8d92-6b7b61c2c001",
    "provider": "omp",
    "event": "omp.session_stop",
    "delivered": true,
    "targets": ["default_group"],
    "render_mode": "text"
  }
}
```

若后续实现异步队列，可使用 202：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "request_id": "..."
  }
}
```

### 错误响应

统一格式：

```json
{
  "code": 1,
  "message": "unauthorized",
  "data": {
    "error": "invalid_token"
  }
}
```

错误码：

| HTTP 状态码 | error | 说明 |
| --- | --- | --- |
| 400 | `invalid_json` | 请求体不是合法 JSON |
| 400 | `invalid_payload` | payload 顶层结构或必要字段无效 |
| 400 | `unsupported_event` | 事件类型不支持 |
| 401 | `missing_authorization` | 缺少 Authorization |
| 401 | `invalid_token` | Bearer Token 不匹配 |
| 403 | `endpoint_revoked` | endpoint 已撤销 |
| 403 | `group_permission_denied` | 群聊 token 验证时申请者不是群主或群管理员 |
| 409 | `verification_expired` | 群聊 token 验证请求已过期或已失效 |
| 413 | `payload_too_large` | 请求体超过限制 |
| 415 | `unsupported_media_type` | Content-Type 不支持 |
| 500 | `invalid_config` | 服务端配置无效，例如 endpoint 未绑定任何有效 target |
| 500 | `render_failed` | 渲染失败且未能降级 |
| 500 | `send_failed` | 消息发送失败 |
| 503 | `webhook_disabled` | 插件或 Webhook 服务未启用 |

---

## Webhook 机机交互序列

用户完成 token 申请与外部系统配置后，后续 Webhook 调用不再需要人机交互。

同步处理序列：

```text
External System(OMP)
  -> Webhook HTTP Server: POST /webhook/u/{owner_hash}/{endpoint_name}
     headers: Authorization: Bearer <token>, Content-Type: application/json
     body: OMP session_stop payload

Webhook HTTP Server
  -> Request Guard: 生成 request_id，校验 method/content-type/body size/json object

Request Guard
  -> Endpoint Registry: 按 endpoint path 查找 endpoint 记录

Endpoint Registry
  -> Auth: 校验 endpoint 状态、token hash、revoked_at

Auth
  -> Provider Adapter: 使用 endpoint.provider 解析 payload

Provider Adapter
  -> Router: 输出 Normalized Event，解析 target alias

Router
  -> Renderer: 根据 endpoint/plugin render_mode 选择 text 或 html_image

Renderer
  -> Sender: 构造文本或图片消息链

Sender
  -> AstrBot OneBot v11 Message Platform: 发送到 endpoint target whitelist 内的 UMO

Webhook HTTP Server
  -> External System(OMP): 返回 JSON 处理结果，包含 request_id
```

失败短路规则：

- method、content-type、body size、JSON 解析失败时，不进入 endpoint registry。
- endpoint 不存在、未激活或已撤销时，不进入 provider adapter。
- token 校验失败时，不记录 payload 详情，不进入 provider adapter。
- provider 解析失败时，不进入 renderer/sender。
- target alias 不在白名单内时，不进入 sender。
- html_image 渲染失败且 `fallback_to_text=true` 时，继续走 text renderer 和 sender。
- sender 多目标发送时，MVP 串行执行并记录每个目标结果；部分失败时响应中必须体现失败目标。
- sender 调用 `Context.send_message()` 后必须检查返回值；返回 `False` 时不得记录为发送成功。

审计与日志关联：

- 每次 Webhook 请求必须生成 request id。
- request id 应贯穿 HTTP 响应、日志、渲染错误、发送结果和后续排障摘要。
- 日志不得记录 token 明文、Authorization header、完整 raw payload 或完整 prompt。

---

## OMP Provider 规格

### 事件识别

事件识别规则：

- 仅存在 Header `X-OMP-Event: session_stop` 时，识别为 `omp.session_stop`。
- 仅存在 Body `event: omp.session_stop` 时，识别为 `omp.session_stop`。
- Header 与 Body 同时存在且语义一致时，识别为 `omp.session_stop`。
- Header 与 Body 同时存在但不一致时，拒绝请求并返回 400 `invalid_payload`。

Header `session_stop` 与 Body `omp.session_stop` 视为语义一致。

### 支持事件

MVP 仅支持：

```text
omp.session_stop
```

### 输入字段

MVP 支持读取：

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
round.prompt
round.promptLength
round.imageCount
round.entryCountBefore
round.entryCountAfter
round.entryCountDelta
round.messageCountBefore
round.messageCountAfter
round.messageCountDelta
round.stopHookActive
round.lastAssistant.provider
round.lastAssistant.model
round.lastAssistant.stopReason
round.lastAssistant.timestamp
round.lastAssistant.durationMs
metadata.version
metadata.eventName
```

`session.model` 支持两种形态：

- 字符串，例如 `"gpt-5.5"`。
- 对象，例如 `{ "provider": "openai", "id": "gpt-5.5", "name": "GPT-5.5" }`。

标准化为通知展示值时，对象形态优先使用 `name`，其次使用 `id`；若 `session.model` 无法得到展示值，再回退到 `round.lastAssistant.model`。

### 字段缺失处理

- `session.name` 缺失时使用 `session.file` basename。
- `session.model` 缺失时使用 `round.lastAssistant.model`。
- `round.turnId` 允许为数字 `0`，实现不得用 truthy 判断将其视为缺失；生成事件 ID 时应保留为字符串 `"0"`。
- `round.durationMs` 缺失时尝试由 `startedAt` 和 `endedAt` 计算。
- `promptLength` 缺失但 `prompt` 存在时可用字符串长度计算。
- `imageCount` 缺失时显示为 `0` 或 `未知`，实现阶段按模板策略确定。
- 非必要字段缺失不得导致插件崩溃。

### Prompt 处理

默认策略：

- 不在通知中展示完整 `round.prompt`。
- 允许配置是否包含 prompt 摘要。
- 若展示，必须按配置截断。

推荐配置：

```yaml
providers:
  omp:
    include_prompt: false
    max_prompt_length: 500
```

---

## 标准化事件对象规格

### 字段定义

```json
{
  "provider": "omp",
  "event": "omp.session_stop",
  "version": 1,
  "id": "session-id:turn-id",
  "emitted_at": "2026-07-08T12:00:00.000Z",
  "title": "oh-my-pi 会话完成",
  "status": "success",
  "summary": "会话已完成",
  "source": {
    "name": "oh-my-pi",
    "url": null
  },
  "actor": {
    "name": null,
    "url": null
  },
  "fields": [],
  "links": [],
  "raw": {}
}
```

### 必需字段

- `provider`
- `event`
- `id`
- `emitted_at`
- `title`
- `status`
- `summary`
- `fields`

若 provider payload 缺少时间字段，`emitted_at` 使用插件接收请求时的 UTC ISO-8601 时间。

### Raw 字段保留策略

MVP 中支持读取但未映射为 `fields` 的 OMP 字段，默认保留在 `raw` 中，不直接展示。

以下字段默认仅进入 `raw`：

- `metadata.version`
- `metadata.eventName`
- `round.stopHookActive`
- `round.entryCountBefore`
- `round.entryCountAfter`
- `round.lastAssistant.timestamp`

以下字段默认映射为通知字段：

- `session.name` 或 `session.file` basename → 会话。
- `session.model` 或 `round.lastAssistant.model` → 模型。
- `round.durationMs` → 耗时。
- `round.promptLength` 与 `round.imageCount` → 输入规模。
- `round.messageCountDelta` → 消息变化。
- `round.lastAssistant.stopReason` → 最后状态。

### Status 取值

MVP 支持：

```text
success
warning
failed
info
unknown
```

OMP `session_stop` 默认使用 `success`。如果后续 payload 明确包含失败状态，则映射为 `failed`。

### Field 结构

```json
{
  "label": "模型",
  "value": "gpt-5.5",
  "short": true
}
```

字段要求：

- `label` 必须是短文本。
- `value` 必须可转换为字符串。
- 长值需要在 renderer 层截断或换行。

---

## 路由规格

### MVP Endpoint 路由

MVP 路由以 endpoint 为核心。每个 endpoint 绑定 provider 和 target whitelist。

配置示例：

```yaml
targets:
  - name: default_group
    umo: aiocqhttp:GroupMessage:123456789

endpoints:
  - name: alice_omp
    path: u/0a1b2c3d4e5f/alice_omp
    provider: omp
    token_hash: "..."
    owner_user_id: "10001"
    targets:
      - default_group
    render_mode: html_image
    template: omp_session_stop.html
```

默认行为：

- 请求 path 先匹配 endpoint。
- Bearer Token 必须匹配 endpoint 的 token hash。
- provider adapter 由 endpoint 指定。
- 默认发送到 endpoint 绑定的 targets。
- 若 payload 包含 target alias，只能选择 endpoint targets 白名单内的目标。
- 若未配置目标，请求处理失败并返回 `send_failed` 或 `invalid_config`。

安全约束：

- payload 不允许直接传入任意 UMO。
- endpoint 之外的 target alias 不可被选择。
- token 泄露时，攻击面被限制在该 endpoint 的目标白名单内。

### 后续规则路由

预留结构：

```yaml
routes:
  - name: omp_default
    match:
      provider: omp
      event: omp.session_stop
    targets:
      - default_group
    render_mode: html_image
    template: omp_session_stop.html
```

匹配顺序：

1. endpoint 精确匹配。
2. provider + event 匹配。
3. 默认路由。

MVP 可以不实现该规则路由，但配置设计需兼容未来扩展。

---

## 渲染规格

### Text Renderer

输入：标准化事件对象。

输出：纯文本字符串。

模板引擎：

- Text Renderer 与 HTML Image Renderer 均使用 Jinja2 模板语法。
- 模板渲染应使用 sandboxed environment。
- 模板上下文根变量名统一为 `event`，其值为标准化事件对象。
- 文本模板不提供顶层 `title`、`summary`、`fields` 等裸变量；必须通过 `event.title`、`event.summary`、`event.fields` 访问。

默认模板：

```text
[{{ event.source.name }}] {{ event.title }}

{{ event.summary }}
{% for field in event.fields %}
{{ field.label }}：{{ field.value }}
{% endfor %}
```

OMP 示例：

```text
[oh-my-pi] 会话完成

会话：Add post-conversation HTTP hook
模型：gpt-5.5
耗时：57.7s
输入：977 字 / 1 张图
消息变化：+2
最后状态：stop
```

要求：

- 字段缺失时不输出 `None`。
- 长字段按配置截断。
- 不输出 token、请求头和完整 raw payload。

### HTML Image Renderer

输入：标准化事件对象和模板名称。

输出：图片消息构造所需对象。

渲染步骤：

1. 选择模板。
2. 将标准化事件对象传入模板。
3. 调用 AstrBot `html_render` / T2I 服务。
4. 校验渲染结果是否为图片。
5. 构造图片消息链。

模板引擎：

- MVP 使用 Jinja2 模板语法，与 Text Renderer 保持一致。
- 模板渲染应使用 sandboxed environment。
- 默认模板必须自包含，不依赖外部 JS、CSS、远程字体或 CDN 图片。
- 模板上下文根变量名为 `event`，其值为标准化事件对象。

### 模板变量

Text 与 HTML 模板均应优先使用：

```text
event.provider
event.event
event.title
event.status
event.summary
event.fields
event.links
event.source
event.actor
```

可选高级变量：

```text
event.raw
```

模板中不提供顶层 `provider`、`title` 等裸变量；必须通过 `event.provider`、`event.title` 等命名空间访问。

### 渲染参数

默认：

```json
{
  "full_page": true,
  "type": "png",
  "quality": 90,
  "timeout": 5000,
  "viewport_width": 900,
  "device_scale_factor_level": "high",
  "wait_until": "domcontentloaded"
}
```

`timeout` 单位为毫秒，并直接传递给 AstrBot `html_render` / T2I 服务对应截图参数。

### 图片结果校验

渲染结果可能是：

- bytes
- URL
- `base64://...`
- 本地文件路径

校验要求：

- bytes 需要检查图片 magic number。
- 本地路径需要确认文件存在、可读且是图片。
- `base64://` 需要能成功解码。
- URL 模式可交由 AstrBot 图片组件处理，但日志中不得记录敏感 query。

### 降级规则

如果 HTML 图片渲染失败，且 `fallback_to_text` 为 true：

1. 记录渲染失败安全摘要。
2. 使用 Text Renderer 生成纯文本。
3. 发送纯文本。
4. HTTP 响应中标记 `fallback_used: true`。

如果 `fallback_to_text` 为 false，则返回渲染失败。

---

## 发送规格

插件应通过 AstrBot 插件 API 发送消息，优先使用 `context.send_message(umo, message_chain)` 或当前 AstrBot 版本提供的等价 UMO 发送接口。

实现要求：

- 发送入口必须接受 UMO 字符串作为目标。
- 发送入口必须能发送文本消息链和图片消息链。
- 若 AstrBot API 版本差异导致方法名不同，插件应在 sender 模块内封装兼容层，FSD 其他模块不得直接依赖具体方法名。

### 目标格式

MVP 使用 UMO 字符串：

```text
platform_id:MessageType:session_id
```

示例：

```text
aiocqhttp:GroupMessage:123456789
aiocqhttp:FriendMessage:10001
```

### 文本发送

发送纯文本通知到目标会话。

要求：

- 单条文本不应超过平台可接受长度。
- 超长消息需要截断或后续拆分。

### 图片发送

发送 HTML 渲染得到的图片。

要求：

- 支持 bytes、base64、本地文件和 URL 结果。
- 图片结果无效时触发文本降级。

### 多目标发送

MVP 可以串行发送。

多目标结果应记录每个目标的发送结果。

示例：

```json
{
  "targets": [
    {"name": "default_group", "ok": true},
    {"name": "owner_private", "ok": false, "error": "send_failed"}
  ]
}
```

---

## 配置规格

### 当前配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 是否启用插件 |
| `render_mode` | string | `text` | `text` 或 `html_image` |
| `fallback_to_text` | bool | `true` | HTML 渲染失败是否降级文本 |
| `auth_mode` | string | `managed` | 预留字段；MVP 只支持 `managed`，不支持 `simple` |
| `targets` | yaml text | 示例注释 | 推送目标列表 |
| `endpoints` | yaml text | `[]` | endpoint registry 的可读配置视图；运行时以插件数据目录中的 registry 为准 |
| `templates_dir` | string | `templates` | 自定义模板目录 |
| `render_options` | json text | 默认截图参数 | T2I 渲染参数 |

### MVP 新增建议配置

后续实现时建议补充：

```yaml
server:
  host: 127.0.0.1
  port: 18080
  base_path: /webhook
  public_base_url: https://example.com/webhook
  body_limit_bytes: 262144

storage:
  token_store: data/webhook_tokens.json
  server_secret_file: data/server_secret

targets:
  - name: alice_private
    umo: aiocqhttp:FriendMessage:10001
  - name: bot_dev_group
    umo: aiocqhttp:GroupMessage:123456789

endpoints:
  - name: alice_omp
    path: u/0a1b2c3d4e5f/alice_omp
    provider: omp
    token_hash: "..."
    owner_user_id: "10001"
    targets:
      - alice_private
    render_mode: html_image

providers:
  omp:
    enabled: true
    include_prompt: false
    max_prompt_length: 500
```

`public_base_url` 用于 token 创建或轮换成功后拼接完整 Webhook URL，并通过私聊返回给申请者。若未配置，则状态命令和 token 创建结果应提示管理员补充公网访问地址；插件仍可返回本地监听地址用于内网测试。

配置优先级：

- endpoint 级 `render_mode` 优先于插件级 `render_mode`。
- endpoint 级 `template` 优先于 provider 默认模板。
- endpoint 未配置 `render_mode` 时使用插件级 `render_mode`。
- endpoint 未配置 `template` 时使用 provider/event 默认模板。

骨架迁移：

- 若升级时检测到骨架阶段早期占位的 `webhook_token`，MVP 不自动把它作为可用 endpoint token。
- 插件应在状态命令中提示管理员使用 `/whn token new ...` 重新创建 managed endpoint。
- `webhook_token` 不在 MVP 配置界面展示，也不参与 Webhook 鉴权，避免误启用单全局 token 模式。

### 配置校验

启动时需要校验：

- `render_mode` 必须是 `text` 或 `html_image`。
- `auth_mode` 在 MVP 中必须是 `managed`。
- `server.host` 默认必须是 `127.0.0.1`，除非管理员显式改为公网监听。
- `server.public_base_url` 如为空，不阻止本地服务启动，但 token 发放结果必须提示 URL 可能不可公网访问。
- `targets` 必须能解析为列表。
- 每个 target 必须包含 `name` 和 `umo`。
- 每个 endpoint 必须包含 `name`、`path`、`provider`、`token_hash`、`owner_user_id` 和 `targets`。
- endpoint 的每个 target alias 必须存在于 `targets` 中。
- `render_options` 必须能解析为 JSON object。

---

## 状态命令规格

### `/webhook_notifier`

返回完整状态摘要。

内容包括：

- 插件启用状态。
- Webhook 服务运行状态。
- active endpoint 数量。
- 默认渲染模式。
- fallback 是否开启。
- 目标数量。
- 模板目录。
- 最近错误摘要，后续可选。

### `/whn`

短命令，行为与 `/webhook_notifier` 一致。

### Token 管理命令

MVP 推荐命令形态：

```text
/whn token new private [name]
/whn token new group <QQ群号> [name]
/whn token verify <request_id> <code>
/whn token list
/whn token revoke <endpoint_name>
/whn token rotate <endpoint_name>
```

私聊命令要求：

- `new private` 必须在私聊中执行。
- `new group` 必须在私聊中执行，并创建待验证申请。
- `list` 只展示申请者自己的 endpoint 摘要，不展示 token 明文。
- `revoke` 和 `rotate` 只能操作申请者拥有的 endpoint。

群聊验证命令要求：

- `verify` 必须在目标群中执行。
- 执行者必须与申请者 user id 一致。
- 插件必须在群消息事件中确认执行者是群主或群管理员。
- 验证通过后，token 明文只通过私聊发送给申请者，不在群内展示。

---

## 日志与脱敏

日志允许记录：

- provider。
- event。
- endpoint。
- 目标名称。
- 渲染模式。
- 错误类型和阶段。

日志禁止记录：

- Bearer Token 原文。
- Authorization header 原文。
- token 哈希完整值。
- 完整 raw payload。
- 完整 prompt。
- 带 query 和 fragment 的 URL。

URL 日志应去除 query 和 fragment。

---

## 安全规格

### Body Size 限制

MVP 推荐默认最大 body：

```text
256 KiB
```

超过限制返回 413。

### Content-Type 限制

只接受：

```text
application/json
application/*+json
```

### Token 与目标授权限制

- token 必须由插件生成，使用足够随机的不可预测值。
- token 持久化时必须保存哈希，不保存明文。
- token 明文只在创建或轮换时私聊发送给申请者。
- endpoint 必须绑定 owner user id。
- 私聊 endpoint 只能投递给申请者私聊。
- 群聊 endpoint 必须经过群内验证后创建。
- 群聊验证需要确认 Bot 和申请者都在群内，且申请者是群主或群管理员。
- payload 不允许直接指定任意 UMO。
- payload 中的目标选择只能是 endpoint target whitelist 内的 alias。

### 群管理员校验限制

MVP 默认使用群消息事件进行校验，而不是依赖私聊上下文查询群角色。

校验字段兼容策略：

- 优先读取消息对象或事件对象上的 group 信息。
- owner 字段候选：`group_owner`、`owner`、`owner_id`。
- admin 字段候选：`group_admins`、`admins`、`admin_ids`。
- 同时兼容 `event.is_admin()`。
- 如果当前适配器无法提供群角色信息，则群聊 token 申请应失败并提示无法校验。

### HTML 模板限制

- 模板只能由插件管理员配置。
- 默认模板必须自包含。
- 默认不依赖外部 JS、CSS、远程字体和 CDN 图片。
- 不提供聊天命令上传模板。

### Raw Payload 使用限制

- renderer 默认不展示 raw payload。
- README 和日志中不得输出真实 payload 中的敏感信息。
- 后续如果支持调试模式，需要显式开启。

---

## 错误处理规格

### 鉴权错误

鉴权错误不应泄露正确 token 是否存在。

响应：

```json
{
  "code": 1,
  "message": "unauthorized",
  "data": {
    "error": "invalid_token"
  }
}
```

### Provider 解析错误

解析错误应返回 `invalid_payload` 或 `unsupported_event`。

日志只记录缺失字段路径和事件摘要。

### 渲染错误

渲染错误分阶段记录：

- `template`
- `html_render`
- `image_validate`
- `message_build`

### 发送错误

发送错误记录：

- target name。
- UMO 类型摘要。
- error type。

不记录完整 payload。

---

## 验收用例

### 状态命令

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 查看完整状态 | `/webhook_notifier` | 返回插件状态摘要 |
| 查看短状态 | `/whn` | 返回插件状态摘要 |

### HTTP 鉴权

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 缺少 Authorization | POST JSON | 401 `missing_authorization` |
| Token 错误 | `Authorization: Bearer wrong` | 401 `invalid_token` |
| Token 正确 | `Authorization: Bearer <token>` | 继续处理 |
| 已撤销 Token | 使用 revoked endpoint token | 403 `endpoint_revoked` |

### Token 申请与权限

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 申请私聊 token | 私聊 `/whn token new private` | 创建 endpoint，私聊返回 URL 和 token |
| 私聊 token 列表 | 私聊 `/whn token list` | 只展示自己的 endpoint 摘要，不展示 token 明文 |
| 申请群聊 token | 私聊 `/whn token new group <QQ群号>` | 创建待验证申请 |
| 群管理员验证 | 目标群 `/whn token verify <request_id> <code>` | 验证通过，私聊返回 URL 和 token |
| 普通群成员验证 | 普通成员执行 verify | 验证失败，不创建 token |
| 非申请者验证 | 其他用户执行 verify | 验证失败 |
| Bot 不在目标群 | 申请群聊 token | 申请失败或无法验证 |
| 轮换 token | 私聊 `/whn token rotate <endpoint_name>` | 旧 token 失效，新 token 私聊返回 |
| 撤销 token | 私聊 `/whn token revoke <endpoint_name>` | endpoint 被撤销，后续请求不可用 |

### OMP 事件

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 标准 `session_stop` | OMP payload | 生成标准化事件 |
| Header 与 body 不一致 | `X-OMP-Event: session_stop` + body 其他 event | 400 `invalid_payload` |
| 未知事件 | `event: omp.unknown` | 400 `unsupported_event` |
| 缺少可选字段 | 缺少 `round.lastAssistant` | 仍可生成通知 |

### 渲染与发送

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| text 模式 | 标准事件 | 发送纯文本 |
| html_image 模式成功 | 标准事件 + T2I 正常 | 发送图片 |
| html_image 模式失败且 fallback 开启 | T2I 异常 | 发送纯文本并标记 fallback |
| 目标 UMO 错误 | 配置错误 UMO | 返回或记录 `send_failed` |
| payload 指定白名单内 target alias | endpoint 绑定多个 target | 只发送到该 alias 对应目标 |
| payload 指定白名单外 target alias | endpoint 未绑定该 target | 拒绝请求或忽略该 target，不发送到未授权目标 |

---

## 与 PRD 的对应关系

| PRD 能力 | FSD 对应章节 |
| --- | --- |
| Webhook 接收 | HTTP 接口规范 |
| Bearer Token 鉴权 | Auth、HTTP 接口规范、安全规格 |
| Token 模型取舍 | Token Model Decision、Simple Mode Evaluation |
| 用户自助申请 token | Token Provisioning、状态命令规格、安全规格 |
| 多用户多 endpoint | Endpoint Registry、路由规格、配置规格 |
| OMP 首个集成 | OMP Provider 规格 |
| 标准化事件 | 标准化事件对象规格 |
| QQ 群聊 / 私聊投递 | 路由规格、发送规格 |
| HTML 卡片图片 | 渲染规格 |
| 文本兜底 | 渲染规格、错误处理规格 |
| 状态可观测 | 状态命令规格、日志与脱敏 |

---

## 已收敛决策

FSD v0.1.0 对 MVP 功能契约作出以下决策：

| 议题 | MVP 决策 | 后续方向 |
| --- | --- | --- |
| Webhook HTTP server | 使用插件内独立 HTTP server，默认监听 `127.0.0.1`；公网暴露交给 Nginx、Caddy、Cloudflare Tunnel 或部署层，公网访问必须使用 HTTPS，TLS 终止不由插件负责 | 若 AstrBot 后续提供稳定统一 HTTP 插件入口，可评估迁移 |
| Endpoint Registry 存储 | 使用插件数据目录下的 JSON 文件，例如 `data/webhook_tokens.json`；token 只存哈希 | endpoint 数量、审计或并发写入需求上升后评估 SQLite |
| UMO 校验 | 启动或配置加载时校验 UMO 字符串格式；实际可达性在发送时确认 | 后续如 AstrBot 提供会话探测 API，可增加主动校验 |
| HTML 静态资源 | MVP 默认模板自包含，不支持任意本地静态资源目录 | 后续可设计受限资源目录和路径白名单 |
| 默认渲染模式 | 默认 `text`；`html_image` 必须由管理员显式开启 | 后续可按 endpoint/template 配置默认模式 |
| Request ID | 每个 Webhook 请求生成 request id，并贯穿响应、日志、渲染和发送结果 | 后续可接入持久化审计日志 |
| Token 哈希 | 使用 `HMAC-SHA256(server_secret, token)`，token 明文只展示一次 | 后续如需要更强抗暴力破解能力，可评估 Argon2/bcrypt，但需考虑依赖和性能 |
| 群聊验证码 | `request_id` 使用 UUID4，`code` 使用 6 位小写十六进制，默认 10 分钟有效且一次性使用 | 后续可按风险增加速率限制和验证码长度 |
| 群管理员校验 | MVP 使用群内验证命令，不依赖私聊上下文查询群角色 | 适配器原生群成员查询可作为可选优化 |
| Simple mode | 不进入 MVP；MVP 只支持 managed endpoint/token 模型 | 如需要，后续新建 issue 独立定义安全边界和迁移路径 |

---

## 后续待评估项

以下事项不阻塞 MVP 功能契约：

- 是否提供 UMO WebUI 辅助生成器。
- 是否支持本地静态资源目录及资源打包。
- 是否提供 SQLite backend。
- 是否提供 simple mode。
- 是否支持适配器原生群成员查询以减少群内验证步骤。

---

## 版本变更记录

### v0.1.0

- 初始 FSD Draft。
- 定义 MVP 功能模块、HTTP 接口、OMP provider、标准化事件、渲染、发送、配置、安全和验收用例。
- 补充用户自助申请 Webhook Token、多 endpoint、目标白名单和群管理员验证规格。
- 补充 endpoint/token 模型取舍，并明确 simple mode 不进入 MVP。
- 收敛 MVP 开放问题，补充 HTTP server、endpoint registry、UMO 校验、默认渲染模式和 request id 等决策。
- 补充 token 哈希算法、群聊验证码格式、模板变量命名空间、错误码、配置优先级、body size 检查和 token 生命周期细节。
