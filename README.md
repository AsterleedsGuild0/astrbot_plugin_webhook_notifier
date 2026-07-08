# AstrBot Webhook Notifier

`astrbot_plugin_webhook_notifier` 是一个面向 AstrBot 的通用 Webhook 通知插件。

它的目标是接收来自 `oh-my-pi`、OpenCode、GitHub、GitLab 或其他外部系统的 Webhook 事件，将事件整理为文本或 HTML 卡片图片，并推送到指定 AstrBot 会话，例如 QQ 群聊或私聊。

---

## 当前状态

当前仓库处于初始化骨架阶段：

- 已提供 AstrBot 插件元信息、配置 Schema 和状态命令。
- 已预留 Webhook Token、目标会话、HTML 模板目录和 T2I 渲染参数配置。
- 尚未启动 HTTP Webhook 服务。
- 尚未实现事件解析、目标路由和消息发送。

可用命令：

```text
/webhook_notifier
/whn
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
render_mode: html_image
fallback_to_text: true
webhook_token: "请替换为高强度随机 Token"
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
    "viewport_width": 900,
    "device_scale_factor_level": "high",
    "wait_until": "domcontentloaded"
  }
```

---

## Webhook 鉴权建议

推荐外部系统使用 `Authorization: Bearer <token>` 发送请求：

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
