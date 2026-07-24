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
| `event.emitted_at` | string | `2026-07-15T12:00:00Z` | Provider canonical 事件时间；保持原始带时区 ISO 值，缺失时由 UTC 接收时间补齐 |
| `event.title` | string | `会话完成` | 卡片标题 |
| `event.status` | string | `success` | 常见值为 `success`、`warning`、`failed`、`info`、`unknown` |
| `event.status_display` | string | `已完成` | 展示层中文状态；不改变 `event.status` 原值 |
| `event.summary` | string | `任务已完成` | 可为空 |
| `event.source` | string | `OpenCode Desktop` | HTML 兼容字段；OpenCode 来源是实例名，见下文 |
| `event.actor` | object | `{"name": null, "url": null}` | 事件执行者信息 |
| `event.model_variant` | string，可选 | `medium` | OpenCode runtime 的 variant；仅在存在模型时于展示副本中追加到模型值，不单独显示 |
| `event.fields` | array of object | `[{"label": "模型", "value": "openai/gpt-5.5"}]` | 展示字段列表 |
| `event.links` | array of object | `[]` | 链接列表；当前 OMP 通常为空 |
| `event.raw` | object | `{}` | Provider 原始兼容数据，不建议默认展示 |
| `event.display_timezone` | string | `Asia/Shanghai` | 当前全局展示时区；首版不支持 Endpoint override |
| `event.generated_at` | string | `2026-07-15 20:00:01 CST (UTC+08:00)` | 本次 HTML 渲染生成时间，已转换到展示时区 |
| `event.event_time` | string | `2026-07-15 20:00:00 CST (UTC+08:00)` | `event.emitted_at` 的展示值，已转换到展示时区 |

`fields` 在 HTML/默认文本渲染前会复制为展示字段：已知 OpenCode 键映射为中文，未知键保留原键名；展示副本会隐藏 `sessionRef` 和重复的 `durationMs`，在已有完整问题/权限明细时隐藏重复计数，并对重复问题摘要去重。Permission 明细使用 `permission[1].category`、`.summary`、`.title`、`.description`、`.action`、`.target`、`.patterns`，中文分别显示为“权限 1 类型”等；`permissionCount` 显示为“权限请求数”。Question 继续使用连续编号的 `question[1]`，同一聚合中的 `questionCount`/`optionCount` 不重复展示。OpenCode 的 `projectName` 映射为“项目”，仅在 envelope 有自动推导的最后一级项目名时出现；`sessionName` 只要存在就始终作为“会话名称”独立展示，即使它与 `event.title` 相同。耗时、时间和问题选项会转换为适合阅读的格式，其中选项描述独立缩进换行。`modelVariant` 保持安全短值原文，仅在同时存在 `model` 时以半角括号追加到模型值，括号前无空格，例如 `cpa/gpt-5.6-sol(max)`；仅有 variant 时不显示。时间字段的展示标签为：`startedAt` →“会话开始时间”、`taskStartedAt` →“当前任务开始时间”、`endedAt` →“当前任务结束时间”、`duration`/`durationMs` →“当前任务耗时”。仅有 provider 的 `model` 仍显示为“模型提供方”，有 variant 时同样追加括号；`provider/model` 仍显示为“模型”。`NormalizedEvent` 仅在有值时保留可选的 `model_variant`，Webhook envelope 仅在有值时保留可选的 `modelVariant`。

当且仅当 `event.provider` 为 `opencode`、`startedAt` 和 `event.emitted_at` 都是可解析且带时区的时间，并且事件时间不早于会话开始时间时，展示层会额外派生“会话已持续”。它使用 `event.emitted_at - startedAt` 计算，并复用中文耗时格式；不会写回 envelope、`NormalizedEvent` 或 Provider adapter，也不会增加原始毫秒字段。“会话已持续”描述的是事件产生时的已流逝时间，不暗示 Session 已永久结束。它与来自 `duration`/`durationMs` 的“当前任务耗时”相互独立，因此 Question/Permission 等未结束事件也可以只有“会话已持续”。OMP、未知 Provider、非法时间、naive 时间或负时长均不会自动派生。

OpenCode 展示字段会稳定排列任务与会话信息：执行代理、模型、当前任务耗时（若有）、会话已持续、会话开始时间、当前任务开始时间、当前任务结束时间，之后再展示其他字段及问题/权限内容。默认 text 与 HTML renderer 使用同一展示副本，因此标签、值和顺序一致。

默认 HTML 卡片会把摘要和所有 field value 中成对的单反引号（例如 `` `pytest tests` ``）渲染为轻量行内代码。普通文本与代码内容都会先做 HTML escape；不会启用完整 Markdown，也不会解析原始 HTML、链接、图片、代码块或任意标签。空反引号、未闭合反引号和双反引号保持为安全普通文本。默认纯文本 renderer 保留反引号，不输出 `<code>` 标签。自定义模板如需同样行为，可显式使用 `inline_code` filter：`{{ field.value|inline_code }}`。

`fields` 中的标准条目通常至少包含：

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
      "label": "会话名称",
      "value": "Add post-conversation HTTP hook"
    },
    {
      "label": "模型",
      "value": "openai/gpt-5.5"
    }
  ],
  "links": [],
  "raw": {},
  "display_timezone": "Asia/Shanghai",
  "generated_at": "2026-07-15 20:00:01 CST (UTC+08:00)",
  "event_time": "2026-07-15 20:00:00 CST (UTC+08:00)"
}
```

---

## 来源字段

### `source`

`NormalizedEvent.to_dict()` 中的 `source` 原始类型是 object。对 OpenCode，值按 `instanceDisplayName` > `OpenCode` 选择；`projectName` 不参与来源名称：

```json
{
  "name": "oh-my-pi",
  "url": null
}
```

HTML 生产渲染的 `render_html_data()` 会把它展平为 `source.name` 对应的 string，因此 HTML 模板应使用 `event.source`，不要使用 `event.source.name`。名称为空时回退为 `AstrBot`。

纯文本 renderer 使用同一份展示层字段副本，仍可访问 `event.source.name`；默认文本模板会显示中文状态和分行选项。本文其余内容以 WebUI 管理的 HTML 模板为准。

### `fields`

`NormalizedEvent.to_dict()` 原样保留 `fields`，展示模板收到的是安全复制后的展示字段。生产 OMP 事件使用 array of object，每项主要包含 `label` 与 `value`，可选包含 `short`。内置模板为兼容预览数据，也接受 object/mapping 形式，但自定义模板不应依赖该兼容分支作为生产数据契约。

### `links`

`links` 由 `NormalizedEvent.to_dict()` 原样传入，不做展平或补字段。当前模型只约束它为 array of object；模板在读取某个 link 子字段前应先判断是否存在，不应假定固定的 `label`、`title` 或 `url` schema。

### `generated_at` 与 `event_time`

- `display_timezone` 来自 AstrBot 插件全局配置，默认 `Asia/Shanghai`，接受可由 Python `zoneinfo.ZoneInfo` 加载的 IANA timezone，例如 `UTC`、`Asia/Tokyo`。首版不提供 Endpoint override。
- `generated_at` 是每次生产 HTML 渲染时生成的当前时刻，不是 Webhook 到达时间，也不是模板保存时间；展示前会转换到 `display_timezone`。
- `event_time` 是 canonical `emitted_at` 的可读展示值，也会转换到 `display_timezone`。输出同时包含时区缩写和 UTC offset，例如 `2026-07-24 09:44:35 CST (UTC+08:00)`。
- `fields` 中的 `startedAt`、`taskStartedAt`、`endedAt`、`开始时间`、`结束时间` 使用同一转换规则。naive 或无法解析的时间戳不会猜测时区，会安全保留原值。
- 非法 `display_timezone` 会记录不包含配置原值的固定 warning，并回退 `Asia/Shanghai`；若系统缺少该 zoneinfo 数据，则继续回退 UTC。
- WebUI preview 直接使用预览 JSON，不会自动调用 `render_html_data()`。需要预览这两个字段时，应在预览 JSON 中显式提供。

---

## Jinja 示例

### 标题、状态和摘要

```jinja2
<header>
  <span>{{ event.source|default('AstrBot', true) }}</span>
  <strong>{{ event.status_display|default(event.status|default('unknown', true), true) }}</strong>
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
- `inline_code` filter 只识别成对单反引号，并使用 `Markup.escape` 分段组合普通文本与 `<code>` 标签；不得用未转义原文配合 `safe` 绕过 autoescape。
- 禁止 `script`、`iframe`、`object`、`embed`、`base`、`form`、HTML 事件属性、`javascript:`、`meta refresh` 和模板自定义 CSP。
- CSS 禁止 `url(...)`、`@import`、`expression(...)`；HTML 属性禁止 `http:`、`https:` 和 `file:` 外部资源。
- 渲染结果自动注入 CSP：`default-src 'none'; style-src 'unsafe-inline'; img-src data:`。
- 模板最大 512 KiB，渲染后 HTML 最大 2 MiB，`canvas_width` 必须是 `320..2048` 的整数。
- preview event 必须是 JSON object，canonical JSON 最大 64 KiB，并受深度、节点数、容器长度和字符串长度限制。
- preview event key 不得包含 token、password、secret、authorization、cookie、apikey 或 accesstoken 等敏感标记。
- 不要在模板中展示 `event.raw`、完整 prompt、token、请求头或其他敏感数据；管理员模板仍需遵循最小披露原则。
