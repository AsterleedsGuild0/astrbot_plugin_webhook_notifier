# AstrBot Webhook Notifier FSD

## 文档信息

- 文档类型：FSD（Software Functional Specification Document）
- 文档版本：v0.1.0
- 对应 PRD 版本：v0.1.0
- 对应插件版本：v0.1.0
- 状态：Draft
- 最后更新：2026-07-08 20:25
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
- QQ 平台适配器，例如 aiocqhttp、NapCat、Lagrange 或 QQ 官方适配器。

### 插件不负责

Webhook Notifier 不负责：

- 管理底层 QQ 登录或 OneBot 连接。
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

通用路径：

```text
POST /webhook/{endpoint}
```

功能要求：

- 只接受 POST。
- 只接受 JSON body。
- 限制请求体大小。
- 在 token 未配置时不得启动公网可用 Webhook 服务。
- 默认监听 `127.0.0.1`，公网暴露由反向代理、隧道或用户部署层处理。
- 处理完成后返回明确 JSON 响应。

### Auth

负责校验请求身份。

MVP 只支持 Bearer Token：

```http
Authorization: Bearer <token>
```

功能要求：

- token 由插件生成，并绑定 endpoint、申请者和目标白名单。
- 持久化时保存 token 哈希，不保存明文 token。
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

群管理员识别优先参考 `astrbot_plugin_gpt_image2`：在群消息事件中读取 message/group 对象上的 `group_owner`、`owner`、`owner_id`、`group_admins`、`admins`、`admin_ids` 等字段，并结合 `event.is_admin()`。

不要求 MVP 在私聊上下文中直接调用适配器 API 查询群成员角色；若具体适配器支持，可作为后续优化。

### Endpoint Registry

负责保存 endpoint/token/target 绑定关系。

每条记录至少包含：

- endpoint name。
- path。
- provider。
- token hash。
- owner user id。
- target whitelist。
- render mode。
- template。
- created at。
- revoked at。

Endpoint Registry 是 Webhook 鉴权和路由的事实来源。

### Provider Adapter

负责处理不同外部系统的专用 payload。

MVP 仅要求实现 OMP provider。

后续 provider：

- `opencode`
- `github`
- `gitlab`
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
/webhook/{endpoint}
```

其中 `endpoint` 用于匹配配置中的 endpoint 名称。

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
| 413 | `payload_too_large` | 请求体超过限制 |
| 415 | `unsupported_media_type` | Content-Type 不支持 |
| 500 | `render_failed` | 渲染失败且未能降级 |
| 500 | `send_failed` | 消息发送失败 |
| 503 | `webhook_disabled` | 插件或 Webhook 服务未启用 |

---

## OMP Provider 规格

### 事件识别

事件识别优先级：

1. Header `X-OMP-Event: session_stop`
2. Body `event: omp.session_stop`

当 header 与 body 不一致时，MVP 应拒绝请求并返回 `invalid_payload`，避免错误路由。

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

### 字段缺失处理

- `session.name` 缺失时使用 `session.file` basename。
- `session.model` 缺失时使用 `round.lastAssistant.model`。
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
- `title`
- `status`
- `summary`
- `fields`

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
    path: /omp/alice
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

默认模板：

```text
[{{ source.name }}] {{ title }}

{{ summary }}
{{ fields }}
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

### 模板变量

HTML 模板应优先使用：

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
| `webhook_token` | string | `""` | 骨架阶段遗留的全局 Bearer Token；MVP 实现时应迁移为 endpoint 级 token |
| `auth_mode` | string | `managed` | 预留字段；MVP 只支持 `managed`，不支持 `simple` |
| `targets` | yaml text | 示例注释 | 推送目标列表 |
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

targets:
  - name: alice_private
    umo: aiocqhttp:FriendMessage:10001
  - name: bot_dev_group
    umo: aiocqhttp:GroupMessage:123456789

endpoints:
  - name: alice_omp
    path: /omp/alice
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

### 配置校验

启动时需要校验：

- `render_mode` 必须是 `text` 或 `html_image`。
- `auth_mode` 在 MVP 中必须是 `managed`。
- `server.host` 默认必须是 `127.0.0.1`，除非管理员显式改为公网监听。
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
- token 是否已配置。
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
/whn token new group <group_id> [name]
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
| 已撤销 Token | 使用 revoked endpoint token | 401 `invalid_token` 或 403 `endpoint_revoked` |

### Token 申请与权限

| 用例 | 输入 | 预期 |
| --- | --- | --- |
| 申请私聊 token | 私聊 `/whn token new private` | 创建 endpoint，私聊返回 URL 和 token |
| 私聊 token 列表 | 私聊 `/whn token list` | 只展示自己的 endpoint 摘要，不展示 token 明文 |
| 申请群聊 token | 私聊 `/whn token new group <group_id>` | 创建待验证申请 |
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

## 开放问题

- Webhook HTTP server 是否使用插件内独立 aiohttp server，还是接入 AstrBot 统一 HTTP 能力？MVP 倾向独立 server 并默认监听 `127.0.0.1`。
- endpoint registry 使用 JSON 文件、SQLite，还是复用 AstrBot 插件配置持久化？
- 目标 UMO 的有效性是否能在启动时校验，还是只能发送时发现？
- HTML 模板是否需要支持本地静态资源目录？
- `html_image` 模式是否默认开启，还是必须由管理员显式开启？
- 是否需要为请求生成 request id 并在响应、日志和发送结果中串联？
- 是否需要适配器原生群成员查询作为可选优化，从而减少群内验证步骤？
- simple mode 是否需要作为后续增强？若需要，应新建 issue 独立定义适用范围、迁移路径和安全限制。

---

## 版本变更记录

### v0.1.0

- 初始 FSD Draft。
- 定义 MVP 功能模块、HTTP 接口、OMP provider、标准化事件、渲染、发送、配置、安全和验收用例。
- 补充用户自助申请 Webhook Token、多 endpoint、目标白名单和群管理员验证规格。
- 补充 endpoint/token 模型取舍，并明确 simple mode 不进入 MVP。
