# AstrBot Webhook Notifier

`astrbot_plugin_webhook_notifier` 是一个面向 AstrBot 的通用 Webhook 通知插件。

它的目标是接收来自 `oh-my-pi`、OpenCode、GitHub、GitLab 或其他外部系统的 Webhook 事件，将事件整理为文本或 HTML 卡片图片，并推送到指定 AstrBot 会话，例如 QQ 群聊或私聊。

---

## 当前状态

当前处于 Milestone 2（HTML 卡片图片）：

- ✅ Webhook HTTP Server（aiohttp）已就绪，支持多 endpoint 鉴权与路由。
- ✅ OMP `session_stop` 事件已适配，可标准化为通用事件对象。
- ✅ **纯文本渲染与发送** — 默认文本模板，向指定 QQ 群/私聊推送。
- ✅ **HTML 卡片渲染与发送** — 默认自包含 HTML 模板，调用 AstrBot `html_render` / T2I 截图后发送图片。
- ✅ **图片结果校验**：支持 PNG / JPEG / WebP magic number 校验。
- ✅ **截图空白裁剪**：HTML 图片模式会对本地 T2I 截图裁掉右侧/底部多余视口背景。
- ✅ **HTML 失败降级**：html_render 截图失败、图片校验失败或发送失败时，按配置降级为纯文本通知。
- ✅ HTTP 响应包含 `render_mode`、`requested_render_mode`、`fallback_to_text`、`fallback_reason`。
- ✅ 用户通过私聊命令自助申请 / 验证 / 轮换 / 撤销 endpoint token。
- ✅ 私聊 token 和群聊 token（含群管理员验证）。
- 🔲 自定义模板文件加载（后续版本）。

可用命令：

```text
/webhook_notifier
/whn
/whn token new private [名称]
/whn token new group <群号> [名称]
/whn token verify <request_id> <code>
/whn token list
/whn token rotate <名称>
/whn token revoke <名称>
```

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
render_mode: html_image   # text | html_image
fallback_to_text: true     # html_image 渲染失败时降级为纯文本
templates_dir: templates
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

T2I 截图空白裁剪与排障经验见 [`docs/t2i-rendering-notes.md`](docs/t2i-rendering-notes.md)。

---

## Webhook 鉴权建议

Token 由 Bot 命令创建，不通过插件配置项手动填写。先在私聊或群聊中使用 `/whn token new ...` 创建 managed endpoint，再将 Bot 返回的 Token 配置到外部系统。外部系统应使用 `Authorization: Bearer <token>` 发送请求：

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

## HTML 卡片与 T2I 方向

插件计划支持用户在插件侧配置受信任的 HTML 模板，而不是允许聊天用户随意提交 HTML。

推荐渲染链路：

```text
Webhook payload
  ↓
标准化事件对象
  ↓
插件内置或用户配置的 HTML 模板
  ↓
AstrBot html_render / 已配置的 T2I 服务
  ↓
图片消息
```

设计原则：

- 默认模板尽量自包含，避免依赖外部 JS、CSS、远程字体和 CDN 图片。
- HTML 图片渲染失败时自动降级为纯文本通知。
- 模板主要使用标准化事件字段，原始 payload 仅作为高级调试数据。
- 渲染参数沿用 AstrBot T2I 服务支持的截图选项。

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
