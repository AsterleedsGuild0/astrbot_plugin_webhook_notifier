# AstrBot Webhook Notifier

`astrbot_plugin_webhook_notifier` 是一个面向 AstrBot 的通用 Webhook 通知插件。

它的目标是接收来自 `oh-my-pi`、OpenCode、GitHub、GitLab 或其他外部系统的 Webhook 事件，将事件整理为文本或 HTML 卡片图片，并推送到指定 AstrBot 会话，例如 QQ 群聊或私聊。

---

## 当前状态

当前已完成 Milestone 2（HTML 卡片图片）与 WebUI 模板管理 Phase 1：

- ✅ Webhook HTTP Server（aiohttp）已就绪，支持多 endpoint 鉴权与路由。
- ✅ OMP `session_stop` 事件已适配，可标准化为通用事件对象。
- ✅ **纯文本渲染与发送** — 默认文本模板，向指定 QQ 群/私聊推送。
- ✅ **HTML 卡片渲染与发送** — 默认自包含 HTML 模板，调用 AstrBot `html_render` / T2I 截图后发送图片。
- ✅ **图片结果校验**：支持 PNG / JPEG / WebP magic number 校验。
- ✅ **截图空白裁剪**：HTML 图片模式会对本地 T2I 截图裁掉右侧/底部多余视口背景。
- ✅ **HTML 失败降级**：html_render 截图失败、图片校验失败或发送失败时，按配置降级为纯文本通知。
- ✅ HTTP 响应包含 `render_mode`、`requested_render_mode`、`fallback_to_text`、`fallback_reason`。
- ✅ **私聊通知安全默认值**：Webhook 状态通知默认不主动投递到 `FriendMessage`，群聊通知不受影响。
- ✅ 用户通过私聊命令自助申请 / 验证 / 轮换 / 撤销 / 永久删除 endpoint token。
- ✅ 私聊 token 和群聊 token（含群管理员验证）。
- ✅ **WebUI 模板管理**：在插件详情页中创建、复制、编辑、预览、保存、应用和删除 HTML 模板。
- ✅ **安全模板预览**：HTML Monaco 编辑器、JSON 预览数据和 sandbox `srcdoc` 预览。

### 已验证平台与能力矩阵

平台声明表示已验证基础兼容，不表示该平台所有场景均已验证。

| 平台 / 接入方式 | 验证状态 | 已验证能力与边界 |
| --- | --- | --- |
| `aiocqhttp` | 已验证 | 既有命令、群聊通知、HTML 图片卡片 |
| `qq_official` WebSocket 私聊 | 已验证 | AstrBot v4.26.6 私聊命令、Webhook 鉴权、private 主动消息、OMP 状态图片卡片 |
| `qq_official` WebSocket 普通 QQ 群 | 待验证 | 尚未验证主动发送 |
| `qq_official` WebSocket Guild | 待验证 | 尚未验证 |
| `qq_official_webhook` | 未验证、未声明支持 | 尚未验证，不在 `metadata.yaml` 的 `support_platforms` 中 |

`metadata.yaml` 使用 AstrBot 标准 adapter key `aiocqhttp` 与 `qq_official`；WebSocket 是 `qq_official` 的接入方式，不另造 `qq_official_websocket` key。完整平台投递说明见 [`docs/platform-delivery-policy.md`](docs/platform-delivery-policy.md)。

可用命令：

静态文档使用 `<唤醒词>` 占位。AstrBot 默认 `wake_prefix=["/"]`，因此默认命令为 `/whn ...`；改为 `!` 时使用 `!whn ...`；配置空前缀时使用裸命令 `whn ...`。

```text
<唤醒词>webhook_notifier
<唤醒词>whn
<唤醒词>whn help
<唤醒词>whn token new private [名称]
<唤醒词>whn token new group <数字群号> [名称]  # aiocqhttp
<唤醒词>whn token new group current [名称]     # qq_official
<唤醒词>whn token verify <request_id> <code>
<唤醒词>whn token confirm <request_id>         # qq_official 原申请者私聊
<唤醒词>whn token list
<唤醒词>whn token rotate <名称>
<唤醒词>whn token revoke <名称>
<唤醒词>whn token delete <名称>
<唤醒词>whn admin token list
<唤醒词>whn admin token revoke-path <endpoint-path>
<唤醒词>whn admin token revoke-owner <platform_id> <owner_user_id> <名称>
```

`<唤醒词>whn help`（中文别名 `<唤醒词>whn 帮助`）使用插件内置专用模板生成图片帮助卡片，不读取或占用 Webhook 通知的 active 模板。普通用户只看到自己的创建与管理命令；AstrBot 超级管理员会额外看到仅限私聊执行的 Registry 管理区。若 HTML/T2I 渲染失败，命令会自动回退为结构化纯文本帮助。

`token revoke` 是保留审计记录的软撤销；`token delete` 是用户对自己当前平台 scope 内 `revoked` / `expired` 终态 Endpoint 的永久删除。永久删除不可恢复，不能用于 `active`、`pending_verification` 或 quarantine 记录；删除后原 Path 返回 404，原 Token 无效，同名 Endpoint 可以重新创建。

群聊申请按 adapter 区分：`aiocqhttp` 私聊使用数字群号并预绑定目标群，必须由原申请者在该群以群主/管理员身份 verify；成功后 endpoint 进入 tokenless `active`，pending 被清理，原申请者再私聊 rotate 领取 Token。`qq_official` 私聊必须使用字面量 `current`，不接受数字群号或 `group_openid`；目标群内任一群主/管理员都可批准当前群，不要求批准者是 C2C 申请者，也不比较 `member_openid` 与 private owner。群 verify 后 record 仍为 pending，pending 转为 `group_verified_waiting_owner` 且不会清理；只有原 C2C 申请者在同一 `platform_id` 私聊执行 `<唤醒词>whn token confirm <request_id>`，才会激活、生成 Token、删除 pending 并独立交付明文。两个 QQ 官方 phase 共用创建时的不可延长 expiry；confirm 消息发送失败不回滚，原申请者应私聊 rotate 恢复凭据。所有管理操作和 pending 均以当前 `platform_id` 隔离。

`<唤醒词>whn admin token ...` 仅 AstrBot 全局超级管理员可用，群管理员不具备该权限，并且必须在私聊中执行。`list` 跨用户展示 endpoint 的最小管理元数据，并限制单次最多显示 50 条；不会显示 Token 明文、`token_hash`、验证码或完整目标 UMO。撤销时可使用两个精确选择器：`revoke-path` 按完整 endpoint path 匹配（可省略开头的单个 `/`，也是关闭 quarantine legacy endpoint 的 kill switch）；`revoke-owner` 按 `platform_id + owner_user_id + 名称` 匹配 managed endpoint，名称遵循普通 endpoint 名称的规范化规则。两者均不支持模糊匹配，也不会跨平台推断 owner。

聊天命令不展示任何完整 URL、`public_base_url` 配置值或 `OMP_SESSION_WEBHOOK_URL=...`。private create、rotate 与 QQ 官方 confirm 成功时，安全摘要仍作为正常 `MessageEventResult` 先发送；随后插件只调用一次 `event.send()` 直接发送恰好一个关闭 T2I/Markdown 的 Plain，内容仅为 `Bearer Token: <token>`。Token 不进入 RespondStage，direct send 期间使用只匹配当前 Token 精确值的临时日志 filter。

普通命令、异常和兼容文本若意外包含符合插件 `whn_` 明文格式的 Token，仍会替换为 `[Token 已隐藏]`。敏感 direct send 不经过该用户消息 sanitizer。插件只承诺调用 adapter 一次，不宣称网络 exactly-once；发送异常时不回滚、不重试，提示用户同平台私聊 rotate 恢复。

Registry v2 提供独立的 `scripts/rebind_platform_id.py` 运维 helper，用于 adapter 实例 `platform_id` 变更后的 managed record 重绑定。该能力不是聊天命令或 Plugin Page UI；dry-run 为零写入只读操作，execute/rollback 必须在 AstrBot 与插件停止后离线运行，并显式提供 `--confirm-offline`。完整流程见 [`docs/platform-id-rebind-runbook.md`](docs/platform-id-rebind-runbook.md)。

---

## 规划目标

第一阶段目标：

```text
oh-my-pi / OMP session_stop
  ↓ Webhook HTTP POST
AstrBot Webhook Notifier
  ↓ Bearer Token 鉴权
  ↓ 事件标准化
  ↓ 文本或 HTML 卡片渲染
  ↓ AstrBot 消息发送能力
指定 QQ 群聊 / 私聊
```

后续可扩展：

- OpenCode 会话完成通知。
- GitHub push / issue / pull request / release 通知。
- GitLab pipeline / merge request 通知。
- 自定义 JSON Webhook。
- CloudEvents 风格的内部标准事件。

---

## 配置示例

```yaml
enabled: true
enable_private_notifications: false  # 是否允许 Webhook 状态通知主动投递到 FriendMessage
render_mode: html_image   # text | html_image
fallback_to_text: true     # html_image 渲染失败时降级为纯文本
targets: |
  - name: default_group
    umo: aiocqhttp:GroupMessage:123456789
  - name: owner_private
    umo: aiocqhttp:FriendMessage:10001
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

MVP 阶段 `render_mode` 是插件全局配置，所有 endpoint/token 都跟随该配置；历史 endpoint 中保存的 `render_mode` 不会覆盖全局设置。

`enable_private_notifications` 默认为 `false`，仅控制 Webhook 状态通知是否投递到 UMO 类型为 `FriendMessage` 的目标。它不影响聊天命令回复、private create、rotate、QQ 官方 confirm 的 Token 交付、endpoint 创建或群聊通知。

### v0.2.0 私聊通知迁移提示

升级后，现有私聊 endpoint 和 Token 继续有效，无需重建或轮换。默认配置下，发往私聊 endpoint 的 Webhook 请求仍返回 HTTP 200，但响应为 `message=skipped`、`retryable=false`、`rendered=false`，表示请求已被安全策略处理且不应重试。

如管理员确认所用平台允许主动私聊，并接受对应平台规则与风控风险，可开启 `enable_private_notifications` 后 reload 插件，现有私聊 endpoint 将恢复投递。混合目标中，群聊正常发送，私聊标记为 `skipped`，整体 `message=partial_delivery`；只有真实发送失败才会返回 `retryable=true`。

### QQ 平台风险提示

- OneBot/NapCat 的主动私聊可能触发 QQ 风控。请参考 [NapCatQQ 风控讨论](https://github.com/NapNeko/NapCatQQ/issues/751) 与 [NapCat 安全指南](https://napneko.github.io/other/security)，本插件不提供或建议任何对抗风控方案。
- QQ 官方 Bot 也不是无限安全通道；主动私聊和主动消息受严格规则与额度约束。仅在确认并持续遵守 [QQ 官方 Bot 主动消息规则](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html) 后再开启私聊通知。
- AstrBot 平台接入资料见 [aiocqhttp](https://docs.astrbot.app/platform/aiocqhttp.html) 与 [QQ 官方 Bot WebSocket](https://docs.astrbot.app/en/platform/qqofficial/websockets.html)。当前 UMO 只能可靠识别 `platform_id` 与 `FriendMessage` / `GroupMessage`，不能据此自动识别 NapCat 或其他 OneBot 实现。

完整的平台差异、证据边界、架构决策和运维建议见 [`docs/platform-delivery-policy.md`](docs/platform-delivery-policy.md)。

T2I 截图空白裁剪与排障经验见 [`docs/t2i-rendering-notes.md`](docs/t2i-rendering-notes.md)。

模板可用变量、兼容行为和示例见 [`docs/template-variables.md`](docs/template-variables.md)。

---

## Webhook 鉴权建议

Token 由 Bot 命令创建，不通过插件配置项手动填写。先在私聊或群聊中使用 `<唤醒词>whn token new ...` 创建 managed endpoint，再将 Bot 返回的 Token 配置到外部系统。外部系统应使用 `Authorization: Bearer <token>` 发送请求：

```http
POST /webhook/omp-session HTTP/1.1
Content-Type: application/json
Authorization: Bearer xxxxx
X-OMP-Event: session_stop
```

安全建议：

- 不要把 Token 放在 URL 查询参数里。
- 公网暴露 Webhook 时必须使用 HTTPS。
- Token 应使用高强度随机值，并定期轮换。
- 后续接入 GitHub/GitLab 时优先支持 HMAC 签名校验。

---

## HTML 卡片与模板管理

管理员可以直接在 AstrBot 插件详情页维护 HTML 卡片模板，不需要手工编辑服务器文件，也不需要通过聊天命令 reload。

使用步骤：

1. 在 AstrBot Dashboard 打开本插件详情页并进入模板管理页面。
2. 选择内置模板查看效果，或从内置模板新建副本。
3. 在 HTML Monaco 编辑器中修改模板，并按需调整预览 JSON 和画布宽度。
4. 使用“保存”“应用”或“保存并应用”；离开未保存内容前页面会要求确认。

内置模板始终只读。自定义模板保存到插件数据目录中的 `templates.json` 与 `templates/<id>-<revision>.html`；revision 文件不可变，当前 active 模板为插件全局设置。保存当前 active 模板的新内容后，新 revision 会立即用于后续通知，active ID 保持不变。

推荐渲染链路：

```text
Webhook payload
  ↓
标准化事件对象
  ↓
插件内置或 WebUI 管理的 HTML 模板
  ↓
AstrBot html_render / 已配置的 T2I 服务
  ↓
图片消息
```

设计原则：

- 默认模板尽量自包含，避免依赖外部 JS、CSS、远程字体和 CDN 图片。
- active 自定义模板失败时先尝试内置模板；内置模板也失败时再按配置降级为纯文本通知。
- 模板主要使用标准化事件字段，原始 payload 仅作为高级调试数据。
- 渲染参数沿用 AstrBot T2I 服务支持的截图选项。

### 开发者说明

Plugin Page 通过 `window.AstrBotPluginPage` bridge 调用相对 endpoint，提供模板列表、详情、保存、应用、删除和预览操作。后端另提供只读 `GET base-url` bridge，响应仅为 `{base_url, configured}`：配置值非空时原样作为已经包含所需路径语义的 Base URL 并去除尾部斜杠；未配置时返回由监听 host、port 与 `base_path` 组成的本地 Base URL。前端下一阶段接入该字段，届时只需在 Base URL 后追加 `Endpoint Path`，不得再次自动拼接 `base_path`。页面不依赖 Dashboard 内部模块路径；模板 ID 由后端生成，并使用 `expected_revision` 防止并发覆盖。

---

## 事件数据方向

插件内部会把不同来源的 Webhook 转换为类似下面的标准化事件对象：

```json
{
  "provider": "omp",
  "event": "omp.session_stop",
  "title": "oh-my-pi 会话完成",
  "status": "success",
  "summary": "任务已完成",
  "fields": [
    {"label": "模型", "value": "gpt-5.5"},
    {"label": "耗时", "value": "57.7s"}
  ],
  "raw": {}
}
```

---

## License

MIT
