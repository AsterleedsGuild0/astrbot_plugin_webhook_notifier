# AstrBot Webhook Notifier PRD

## 文档信息

- 文档类型：PRD
- 文档版本：v0.1.0
- 对应插件版本：v0.1.0
- 状态：Final
- 最后更新：2026-07-09 15:03
- 项目名称：`astrbot_plugin_webhook_notifier`
- 产品名称：Webhook Notifier
- 目标仓库：`AsterleedsGuild0/astrbot_plugin_webhook_notifier`
- 当前阶段：MVP 需求定义
- 首个目标集成：`oh-my-pi` / OMP `session_stop` 任务完成通知
- 目标平台：AstrBot OneBot v11 消息平台；当前验证环境使用 NapCat 作为 OneBot v11 实现

---

## 背景

当前 `oh-my-pi` 已具备在会话结束后向外部 URL 发送 HTTP Webhook 的能力。Webhook payload 中包含会话、轮次、模型、耗时、输入长度、图片数量、消息增量和最后一条 assistant 元数据等信息。

我们希望在 AstrBot 侧提供一个通用通知插件，使外部工具可以通过 Webhook 把任务完成、构建结果、仓库事件或自定义事件推送到指定聊天会话中。

第一阶段不直接调用 OneBot HTTP API，而是通过 AstrBot 的消息抽象发送到目标会话。MVP 仅声明支持 AstrBot OneBot v11 消息平台；当前验证环境使用 NapCat 作为 OneBot v11 实现。其他 AstrBot 平台适配器未测试，不进入 MVP 支持承诺。

---

## 产品定位

Webhook Notifier 是一个 AstrBot 通用 Webhook 通知插件。

它接收来自外部系统的 HTTP Webhook 事件，完成鉴权、事件识别、标准化、模板渲染和目标路由，并将通知发送到指定 AstrBot 会话。

核心定位：

- 面向 AstrBot 的通用通知入口。
- 第一阶段服务 `oh-my-pi` / OMP 任务完成通知。
- 后续扩展到 OpenCode、GitHub、GitLab、Gitea、自定义 JSON 和 CloudEvents 风格事件。
- 第一阶段消息投递仅声明支持 AstrBot OneBot v11 消息平台；其他平台保留架构扩展性但不承诺可用。
- 支持纯文本通知和 HTML 卡片图片通知。
- HTML 模板由插件侧配置，不允许普通聊天用户随意提交 HTML。

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

### 场景二：HTML 卡片通知

管理员在插件侧维护 HTML 模板。

插件将标准化事件对象传入模板，调用 AstrBot 配置的 T2I 服务渲染为图片，然后发送到 QQ。

如果 T2I 服务失败、返回非图片数据、截图超时或图片发送失败，插件自动降级为纯文本通知。

### 场景三：后续接入 OpenCode

OpenCode 或相关工具在任务结束时发送类似 `opencode.session_stop` 的事件。

插件新增 OpenCode provider，把 payload 转换为同一套标准化事件对象，复用路由和模板渲染能力。

### 场景四：用户自助申请 Webhook Token

用户通过私聊 Bot 申请自己的 Webhook Token，并选择通知推送目标。

支持两类目标：

- 私聊：token 只能推送到申请者与 Bot 的私聊会话。
- 群聊：token 只能推送到经过校验的指定群聊。

群聊目标必须满足：

- Bot 已在目标群内。
- 申请用户已在目标群内。
- 申请用户是目标群的群主或群管理员。

由于跨适配器在私聊上下文中查询群成员角色不一定可靠，MVP 推荐使用两步验证：

1. 用户私聊 Bot 发起群聊 Webhook Token 申请。
2. 插件生成一次性验证请求。
3. 用户到目标群内发送验证命令。
4. 插件在目标群的群消息事件中读取 AstrBot OneBot v11 提供的群成员角色信息，确认执行验证命令的申请者是该群群主或群管理员。
5. 校验通过后，插件通过私聊把 Webhook URL 和 Token 返回给用户。

---

## 用户交互流程

Webhook Notifier 的用户路径分为两个阶段：

- 阶段 A：人机交互。用户与 Bot 交互，申请 token、验证权限并绑定推送目标。
- 阶段 B：机机交互。外部系统使用阶段 A 获得的 Webhook URL 和 token 自动推送事件。

### 私聊目标申请流程

适用于用户希望把 OMP 任务完成通知推送到自己与 Bot 的私聊。

```text
用户私聊 Bot：/whn token new private my-omp
  ↓
Bot 创建 endpoint/token
  ↓
Bot 将 endpoint 绑定到申请者私聊会话
  ↓
Bot 私聊返回 Webhook URL、Bearer Token 和 OMP 配置示例
  ↓
用户把 URL 和 Token 配置到 OMP 推送端
  ↓
后续 OMP 自动向 Webhook 推送，会通知到该用户私聊
```

产品要求：

- token 明文只在创建或轮换时展示一次。
- 私聊 token 只能投递到申请者私聊。
- 用户可通过私聊命令查看、轮换或撤销自己创建的 endpoint。

### 群聊目标申请流程

适用于用户希望把 OMP 任务完成通知推送到指定 QQ 群。

```text
用户私聊 Bot：/whn token new group <QQ群号> my-group-omp
  ↓
Bot 创建待验证申请
  ↓
Bot 私聊返回 request_id、code 和群内验证命令
  ↓
用户到目标群发送：/whn token verify <request_id> <code>
  ↓
Bot 在群消息事件中校验：
  - Bot 在该群内
  - 执行验证的用户就是申请者
  - 申请者是该群群主或群管理员
  ↓
校验通过后，Bot 激活 endpoint/token
  ↓
Bot 私聊返回 Webhook URL、Bearer Token 和 OMP 配置示例
  ↓
用户把 URL 和 Token 配置到 OMP 推送端
  ↓
后续 OMP 自动向 Webhook 推送，会通知到该 QQ 群
```

产品要求：

- `<QQ群号>` 直接填写目标 QQ 群号，用户不需要理解内部 UMO 或 OneBot group id 格式。
- 验证码默认 10 分钟有效，且只能使用一次。
- 验证失败时不创建 token。
- 非申请者不能代替申请者完成验证。
- 非群主/群管理员不能为群聊创建 token。
- token 明文不在群聊中展示，只通过私聊返回给申请者。

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

MVP 阶段暂不实现：

- GitHub / GitLab 全量事件适配。
- 多租户、复杂权限模型和 WebUI 模板编辑器。
- 持久化消息队列、失败重试队列和审计数据库。
- 允许普通聊天用户动态提交 HTML。
- 直接调用 OneBot HTTP API。
- 将插件设计成外部 IME 通用平台网关。
- 任意 JavaScript 执行和远程资源加载能力。
- 单全局 token 的 simple mode。该模式适合单人单目标自用，但不进入 MVP；后续以独立 issue 评估是否补充。

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
    {"label": "开始时间", "value": "2026-07-08T11:59:00.000Z"},
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

默认文本通知展示 `session.cwd` 与 `round.startedAt`，便于定位任务上下文与开始时间；`round.endedAt` 默认不展示，仅在 `round.durationMs` 缺失时参与耗时计算。后续 HTML 渲染模板可以自行选择展示或隐藏这些字段。

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
开始时间：2026-07-08T11:59:00.000Z
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
- `templates_dir` 下的本地模板文件。
- 插件配置中的管理员维护模板。

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

### 模板安全

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
  "viewport_width": 900,
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
render_mode: html_image
fallback_to_text: true
server:
  host: 127.0.0.1
  port: 18080
  public_base_url: "https://example.com/webhook"
templates_dir: templates
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
    "viewport_width": 900,
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

---

## 安全要求

### Token 安全

- 使用 `Authorization: Bearer <token>`。
- token 由插件生成，并绑定 endpoint、申请用户和目标白名单。
- 持久化时只保存 token 哈希，不保存明文 token；哈希方案使用基于本地 `server_secret` 的 HMAC-SHA256。
- 不支持 URL query token。
- 日志中不得打印 token 原文。
- README 和示例中不得包含真实 token。
- token 明文只在创建或轮换时通过私聊发送给申请者。

### 目标权限安全

- 私聊 token 只能推送到申请者私聊。
- 群聊 token 只能推送到通过验证的目标群。
- 群聊验证需要确认 Bot 和申请者都在群内，且申请者是群主或群管理员。
- 群聊验证使用一次性 `request_id + code`，默认 10 分钟有效，验证成功、过期或取消后失效。
- 群管理员识别基于目标群的群消息事件：读取 AstrBot OneBot v11 群对象中的群主和管理员字段，并结合 AstrBot 管理员判断。
- payload 不允许指定任意 UMO，只能在 endpoint 白名单范围内选择 target alias。

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
- 目标数量。
- 模板目录。

---

## MVP 验收标准

### 基础验收

- 插件可被 AstrBot 加载。
- `/webhook_notifier` 和 `/whn` 可返回状态摘要。
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
- 能向配置的 QQ 私聊发送纯文本通知。
- HTML 图片模式下能发送卡片图片。
- T2I 失败时能自动降级纯文本。

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

### Milestone 2：HTML 卡片

- 增加默认 HTML 卡片模板。
- 调用 AstrBot `html_render` / T2I 服务。
- 实现图片结果校验。
- 实现渲染失败纯文本降级。

### Milestone 3：可扩展 Provider

- 抽象 provider adapter。
- 增加 OpenCode provider。
- 增加 custom JSON provider。
- 预留 GitHub / GitLab provider。

### Milestone 4：路由与模板增强

- 支持多 endpoint。
- 支持多目标路由。
- 支持按 provider/event/session 条件匹配。
- 支持模板选择和主题扩展。

---

## MVP 产品决策

- Webhook HTTP 服务使用插件内独立 HTTP server，默认监听 `127.0.0.1`；公网暴露由用户部署层处理，公网访问必须使用 HTTPS，TLS 终止不由插件自身负责。
- MVP 使用 endpoint/token 绑定目标白名单模型，不提供单全局 token simple mode。
- MVP 使用 Bearer Token；GitHub/GitLab 等平台签名校验留到对应 provider 阶段。
- token 哈希使用 HMAC-SHA256，token 轮换后旧 token 立即失效。
- 每个 Webhook 请求生成 request id，并贯穿响应、日志、渲染和发送结果。
- MVP 不要求 OMP payload 提供稳定 `status` 字段；`omp.session_stop` 默认视为 `success`，后续如果 payload 提供状态再映射。
- 默认通知展示完整 `session.cwd` 和 `round.startedAt`，不展示完整 `session.file`、`round.prompt` 与 `round.endedAt`；后续 HTML 模板可自行选择展示或隐藏字段。
- HTML 模板默认自包含，不支持任意本地静态资源目录。
- 群聊 token 验证使用群内验证命令；MVP 不依赖适配器原生群成员查询。

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
