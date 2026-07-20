# HTML 模板变量

## 上下文入口

HTML 模板只提供一个根变量：`event`。不要使用顶层 `title`、`fields` 等裸变量。

```jinja2
<h1>{{ event.title }}</h1>
```

生产渲染先调用 `NormalizedEvent.to_dict()`，再由 `render_html_data()` 补充 HTML 辅助字段并处理兼容格式。模板实际收到的 `event` 字段如下。

`templates/help_card.html` 是独立的内置命令帮助模板，不读取、不替换也不占用 Webhook Template Registry 的 active 模板。帮助卡片的命令前缀由运行时解析当前会话 `wake_prefix` 后注入或安全格式化；静态文档统一写作 `<唤醒词>`。当配置异常时使用安全占位符与诊断提示，不把 Token、owner、UMO、Base URL 或其他敏感字段作为帮助模板变量。

---

## 字段列表

| 字段 | 生产类型 | 示例 | 说明 |
| --- | --- | --- | --- |
| `event.provider` | string | `omp` | Provider 标识 |
| `event.event` | string | `omp.session_stop` | 标准事件名 |
| `event.version` | integer | `1` | 标准化事件版本 |
| `event.id` | string | `session-id:turn-id` | 事件 ID |
| `event.emitted_at` | string | `2026-07-15T12:00:00Z` | Provider 事件时间；缺失时由接收时间补齐 |
| `event.title` | string | `会话完成` | 卡片标题 |
| `event.status` | string | `success` | 常见值为 `success`、`warning`、`failed`、`info`、`unknown` |
| `event.summary` | string | `任务已完成` | 可为空 |
| `event.source` | string | `oh-my-pi` | HTML 兼容字段，见下文 |
| `event.actor` | object | `{"name": null, "url": null}` | 事件执行者信息 |
| `event.fields` | array of object | `[{"label": "模型", "value": "openai/gpt-5.5"}]` | 展示字段列表 |
| `event.links` | array of object | `[]` | 链接列表；当前 OMP 通常为空 |
| `event.raw` | object | `{}` | Provider 原始兼容数据，不建议默认展示 |
| `event.generated_at` | string | `2026-07-15T12:00:01.123456+00:00` | 本次 HTML 渲染生成时间，UTC ISO-8601 |
| `event.event_time` | string | `2026-07-15T12:00:00Z` | `event.emitted_at` 的 HTML 辅助别名 |

`fields` 中的标准条目由 provider 生成，通常至少包含：

```json
{
  "label": "模型",
  "value": "openai/gpt-5.5",
  "short": true
}
```

`short` 并非所有条目都存在，模板应按可选字段处理。`label` 和 `value` 是当前默认模板实际使用的字段。

---

## 完整生产示例

下面示例对应 HTML 模板收到的 `event`，不是原始 Webhook payload：

```json
{
  "provider": "omp",
  "event": "omp.session_stop",
  "version": 1,
  "id": "session-id:turn-id",
  "emitted_at": "2026-07-15T12:00:00Z",
  "title": "会话完成",
  "status": "success",
  "summary": "",
  "source": "oh-my-pi",
  "actor": {
    "name": null,
    "url": null
  },
  "fields": [
    {
      "label": "会话",
      "value": "Add post-conversation HTTP hook"
    },
    {
      "label": "模型",
      "value": "openai/gpt-5.5"
    }
  ],
  "links": [],
  "raw": {},
  "generated_at": "2026-07-15T12:00:01.123456+00:00",
  "event_time": "2026-07-15T12:00:00Z"
}
```

---

## 兼容行为

### `source`

`NormalizedEvent.to_dict()` 中的 `source` 原始类型是 object：

```json
{
  "name": "oh-my-pi",
  "url": null
}
```

HTML 生产渲染的 `render_html_data()` 会把它展平为 `source.name` 对应的 string，因此 HTML 模板应使用 `event.source`，不要使用 `event.source.name`。名称为空时回退为 `AstrBot`。

纯文本 renderer 直接使用 `NormalizedEvent.to_dict()`，仍可访问 `event.source.name`。本文其余内容以 WebUI 管理的 HTML 模板为准。

### `fields`

`NormalizedEvent.to_dict()` 原样保留 `fields`。生产 OMP 事件使用 array of object，每项主要包含 `label` 与 `value`，可选包含 `short`。内置模板为兼容预览数据，也接受 object/mapping 形式，但自定义模板不应依赖该兼容分支作为生产数据契约。

### `links`

`links` 由 `NormalizedEvent.to_dict()` 原样传入，不做展平或补字段。当前模型只约束它为 array of object；模板在读取某个 link 子字段前应先判断是否存在，不应假定固定的 `label`、`title` 或 `url` schema。

### `generated_at` 与 `event_time`

- `generated_at` 是每次生产 HTML 渲染时生成的当前 UTC 时间，不是 Webhook 到达时间，也不是模板保存时间。
- `event_time` 直接复制 `emitted_at`；如果 `emitted_at` 为空，则为空字符串。
- WebUI preview 直接使用预览 JSON，不会自动调用 `render_html_data()`。需要预览这两个字段时，应在预览 JSON 中显式提供。

---

## Jinja 示例

### 标题、状态和摘要

```jinja2
<header>
  <span>{{ event.source|default('AstrBot', true) }}</span>
  <strong>{{ event.status|default('unknown', true) }}</strong>
</header>
<h1>{{ event.title|default('Webhook 通知', true) }}</h1>
{% if event.summary %}
<p>{{ event.summary }}</p>
{% endif %}
```

### 遍历 `fields`

```jinja2
<dl>
{% for field in event.fields|default([]) %}
  <dt>{{ field.label|default('字段', true) }}</dt>
  <dd>{{ field.value|default('') }}</dd>
{% endfor %}
</dl>
```

### 可选字段计数

模板环境允许内部使用 `namespace` helper：

```jinja2
{% set visible = namespace(count=0) %}
{% for field in event.fields|default([]) %}
  {% if field.value %}
    {% set visible.count = visible.count + 1 %}
  {% endif %}
{% endfor %}
<p>非空字段：{{ visible.count }}</p>
```

### 时间

```jinja2
<footer>
  <span>事件时间：{{ event.event_time|default('未提供', true) }}</span>
  <span>生成时间：{{ event.generated_at|default('未提供', true) }}</span>
</footer>
```

---

## WebUI preview 示例

模板管理页面初始提供的最小 preview event 为：

```json
{
  "title": "Webhook 通知",
  "source": "AstrBot",
  "status": "success",
  "summary": "模板预览示例",
  "fields": [
    {
      "label": "事件",
      "value": "preview"
    }
  ]
}
```

如需更贴近生产事件，可在 JSON Monaco 中使用“完整生产示例”，并自行更新 `generated_at` 与 `event_time`。preview 只返回本次渲染结果，不保存模板、事件数据或 active 状态。

---

## 安全限制

- Jinja 使用 `SandboxedEnvironment` 与 autoescape；只注入 `event` 根变量和模板内部可用的 `namespace` helper。
- 禁止 `script`、`iframe`、`object`、`embed`、`base`、`form`、HTML 事件属性、`javascript:`、`meta refresh` 和模板自定义 CSP。
- CSS 禁止 `url(...)`、`@import`、`expression(...)`；HTML 属性禁止 `http:`、`https:` 和 `file:` 外部资源。
- 渲染结果自动注入 CSP：`default-src 'none'; style-src 'unsafe-inline'; img-src data:`。
- 模板最大 512 KiB，渲染后 HTML 最大 2 MiB，`canvas_width` 必须是 `320..2048` 的整数。
- preview event 必须是 JSON object，canonical JSON 最大 64 KiB，并受深度、节点数、容器长度和字符串长度限制。
- preview event key 不得包含 token、password、secret、authorization、cookie、apikey 或 accesstoken 等敏感标记。
- 不要在模板中展示 `event.raw`、完整 prompt、token、请求头或其他敏感数据；管理员模板仍需遵循最小披露原则。
