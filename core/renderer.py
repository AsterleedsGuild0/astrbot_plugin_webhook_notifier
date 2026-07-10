from __future__ import annotations

import struct
from datetime import datetime, timezone
from typing import Any

from jinja2 import BaseLoader, Environment
from jinja2.sandbox import SandboxedEnvironment

from .models import NormalizedEvent

# 默认文本模板（与 FSD 一致）
DEFAULT_TEXT_TEMPLATE = """\
[{{ event.source.name }}] {{ event.title }}

{% if event.summary %}{{ event.summary }}
{% endif %}{% for field in event.fields %}
{{ field.label }}：{{ field.value }}{% endfor %}
"""

# 默认 HTML 卡片模板，由 designer 设计。
# 自包含、无外部资源，使用 Jinja2 模板语法。
# 上下文根变量为 event，其值由 render_html_data() 生成的 dict 提供。
DEFAULT_HTML_TEMPLATE = """\
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      padding: 0;
      width: 900px;
      min-height: 100%;
      color: #edf3ff;
      background: #0a0f1c;
      font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    }

    body {
      padding: 34px;
    }

    .card {
      position: relative;
      width: 832px;
      overflow: hidden;
      border: 1px solid rgba(156, 177, 255, 0.22);
      border-radius: 34px;
      background:
        radial-gradient(circle at 12% 0%, rgba(108, 140, 255, 0.34), transparent 34%),
        radial-gradient(circle at 88% 14%, rgba(37, 208, 166, 0.20), transparent 32%),
        linear-gradient(145deg, #121a2d 0%, #0c1324 52%, #101826 100%);
      box-shadow: 0 28px 72px rgba(0, 0, 0, 0.48);
    }

    .card-inner {
      position: relative;
      padding: 42px 46px 36px;
    }

    .ambient-line {
      position: absolute;
      left: 46px;
      right: 46px;
      top: 0;
      height: 4px;
      border-radius: 0 0 999px 999px;
      background: linear-gradient(90deg, #8ea2ff, #32d9ae 52%, rgba(255, 255, 255, 0));
      opacity: 0.95;
    }

    .topbar {
      display: table;
      width: 100%;
      margin-bottom: 30px;
    }

    .source-wrap,
    .status-wrap {
      display: table-cell;
      vertical-align: top;
    }

    .status-wrap {
      text-align: right;
    }

    .eyebrow {
      display: inline-block;
      max-width: 520px;
      padding: 9px 14px;
      border: 1px solid rgba(255, 255, 255, 0.10);
      border-radius: 999px;
      color: #b9c8e8;
      background: rgba(255, 255, 255, 0.055);
      font-size: 22px;
      letter-spacing: 0.04em;
      line-height: 1.25;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .status-badge {
      display: inline-block;
      min-width: 112px;
      padding: 10px 18px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 999px;
      color: #dbe6ff;
      background: rgba(142, 162, 255, 0.18);
      font-size: 23px;
      font-weight: 700;
      line-height: 1.2;
      text-align: center;
      overflow-wrap: anywhere;
    }

    .status-success,
    .status-ok,
    .status-succeeded {
      color: #dffcf4;
      background: rgba(43, 214, 161, 0.20);
      border-color: rgba(43, 214, 161, 0.36);
    }

    .status-error,
    .status-failed,
    .status-fail {
      color: #ffe7e7;
      background: rgba(255, 93, 111, 0.20);
      border-color: rgba(255, 93, 111, 0.40);
    }

    .status-warning,
    .status-warn {
      color: #fff1cf;
      background: rgba(255, 190, 92, 0.20);
      border-color: rgba(255, 190, 92, 0.38);
    }

    h1 {
      margin: 0;
      color: #f8fbff;
      font-size: 52px;
      font-weight: 800;
      line-height: 1.12;
      letter-spacing: -0.04em;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .summary {
      margin-top: 22px;
      padding: 22px 24px;
      border: 1px solid rgba(133, 157, 255, 0.18);
      border-radius: 24px;
      color: #d6e2fa;
      background: rgba(255, 255, 255, 0.055);
      font-size: 27px;
      line-height: 1.52;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .section-label {
      margin: 32px 0 14px;
      color: #8fa4c9;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: 0.16em;
    }

    .fields {
      margin: 0;
      padding: 0;
      list-style: none;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 26px;
      background: rgba(6, 11, 22, 0.30);
      overflow: hidden;
    }

    .field {
      display: table;
      width: 100%;
      border-top: 1px solid rgba(255, 255, 255, 0.075);
    }

    .field:first-child {
      border-top: 0;
    }

    .field-name,
    .field-value {
      display: table-cell;
      vertical-align: top;
      padding: 18px 22px;
      font-size: 24px;
      line-height: 1.42;
    }

    .field-name {
      width: 220px;
      color: #96a8ca;
      font-weight: 700;
      background: rgba(255, 255, 255, 0.032);
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .field-value {
      color: #eef4ff;
      overflow-wrap: anywhere;
      word-break: break-word;
      white-space: pre-wrap;
    }

    .empty-fields {
      padding: 20px 22px;
      color: #8fa4c9;
      font-size: 23px;
      line-height: 1.45;
    }

    .meta {
      display: table;
      width: 100%;
      margin-top: 28px;
      padding-top: 22px;
      border-top: 1px solid rgba(255, 255, 255, 0.09);
      color: #94a7c9;
      font-size: 20px;
      line-height: 1.45;
    }

    .meta-item {
      display: table-cell;
      width: 50%;
      vertical-align: top;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .meta-item + .meta-item {
      text-align: right;
      padding-left: 24px;
    }

    .meta-label {
      color: #63779d;
    }
  </style>
</head>
<body>
  {% set title = event.title|default('Webhook 通知', true) %}
  {% set source = event.source|default('AstrBot', true) %}
  {% set status = event.status|default('info', true)|string %}
  {% set status_text = status|lower %}
  {% set status_tone = 'default' %}
  {% if status_text in ['success', 'ok', 'succeeded', '成功', '完成', '已完成'] %}
    {% set status_tone = 'success' %}
  {% elif status_text in ['error', 'failed', 'fail', '错误', '失败', '异常'] %}
    {% set status_tone = 'error' %}
  {% elif status_text in ['warning', 'warn', '警告', '告警'] %}
    {% set status_tone = 'warning' %}
  {% endif %}
  {% set summary = event.summary|default('', true) %}
  {% set generated_time = event.generated_at|default('', true) %}
  {% set event_time = event.event_time|default(event.emitted_at|default('', true), true) %}

  <main class="card">
    <div class="ambient-line"></div>
    <div class="card-inner">
      <div class="topbar">
        <div class="source-wrap">
          <span class="eyebrow">来源：{{ source|e }}</span>
        </div>
        <div class="status-wrap">
          <span class="status-badge status-{{ status_tone }}">{{ status|e }}</span>
        </div>
      </div>

      <h1>{{ title|e }}</h1>

      {% if summary|string|trim %}
      <div class="summary">{{ summary|e }}</div>
      {% endif %}

      <div class="section-label">字段</div>
      <ul class="fields">
        {% set visible_count = namespace(value=0) %}
        {% if event.fields %}
          {% if event.fields is mapping %}
            {% for field_name, field_value in event.fields.items() %}
              {% set safe_name = field_name|string %}
              {% set safe_key = safe_name|lower %}
              {% set safe_value = field_value if field_value is not none else '' %}
              {% if 'token' not in safe_key and 'raw' not in safe_key and 'prompt' not in safe_key %}
                {% set visible_count.value = visible_count.value + 1 %}
        <li class="field">
          <div class="field-name">{{ safe_name|e }}</div>
          <div class="field-value">{{ safe_value|e }}</div>
        </li>
              {% endif %}
            {% endfor %}
          {% else %}
            {% for field in event.fields %}
              {% set safe_name = field.label|default(field.name|default(field.key|default('字段', true), true), true)|string %}
              {% set safe_value = field.value|default('') %}
              {% if safe_value is none %}
                {% set safe_value = '' %}
              {% endif %}
              {% set safe_key = safe_name|lower %}
              {% if 'token' not in safe_key and 'raw' not in safe_key and 'prompt' not in safe_key %}
                {% set visible_count.value = visible_count.value + 1 %}
        <li class="field">
          <div class="field-name">{{ safe_name|e }}</div>
          <div class="field-value">{{ safe_value|e }}</div>
        </li>
              {% endif %}
            {% endfor %}
          {% endif %}
        {% endif %}
        {% if visible_count.value == 0 %}
        <li class="empty-fields">暂无可展示字段</li>
        {% endif %}
      </ul>

      <div class="meta">
        <div class="meta-item"><span class="meta-label">生成时间：</span>{{ generated_time|default('未提供', true)|e }}</div>
        <div class="meta-item"><span class="meta-label">事件时间：</span>{{ event_time|default('未提供', true)|e }}</div>
      </div>
    </div>
  </main>
</body>
</html>"""


def _create_sandbox() -> SandboxedEnvironment:
    """创建 Jinja2 sandbox 环境。

    SandboxedEnvironment 默认已限制危险操作。
    模板上下文只注入 event 对象，不暴露 Python 内置函数。
    """
    return SandboxedEnvironment(
        loader=BaseLoader(),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_text(
    event: NormalizedEvent,
    template_str: str | None = None,
) -> str:
    """使用 Jinja2 sandbox 将标准化事件渲染为纯文本。

    Args:
        event: 标准化事件对象。
        template_str: Jinja2 模板字符串，None 使用默认模板。

    Returns:
        渲染后的纯文本字符串。

    Raises:
        jinja2.exceptions.TemplateError: 模板渲染失败。
    """
    if template_str is None:
        template_str = DEFAULT_TEXT_TEMPLATE

    env = _create_sandbox()
    template = env.from_string(template_str)
    result = template.render(event=event.to_dict())
    return result


def render_text_default(event: NormalizedEvent) -> str:
    """使用默认模板渲染 OMP session_stop 事件为纯文本。

    与 FSD 中 OMP 示例格式保持一致。
    """
    data = event.to_dict()
    lines: list[str] = []

    source_name = _get(data, ["source", "name"], "unknown")
    title = _get(data, ["title"], "事件")
    lines.append(f"[{source_name}] {title}")
    lines.append("")

    summary = _get(data, ["summary"], "")
    if summary:
        lines.append(summary)

    for field in _get(data, ["fields"], []):
        label = _get(field, ["label"], "")
        value = _get(field, ["value"], "")
        if label or value:
            lines.append(f"{label}：{value}" if label else value)

    return "\n".join(lines)


def render_html_data(event: NormalizedEvent) -> dict[str, Any]:
    """为 HTML 模板准备数据 dict。

    基于 event.to_dict()，添加 HTML 模板所需的辅助字段
    （generated_at、event_time），并将 source 展平为字符串
    （兼容设计师模板对 event.source 的字符串预期）。

    Args:
        event: 标准化事件对象。

    Returns:
        包含 event 上下文的 dict：{"event": {...}}。
    """
    data = event.to_dict()

    # 将 source 展平为字符串（设计师模板预期 event.source 为字符串）
    if isinstance(data.get("source"), dict):
        source_name = data["source"].get("name") or "AstrBot"
        data["source"] = source_name

    # 添加辅助时间字段
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["event_time"] = data.get("emitted_at", "")

    # NormalizedEvent 已用 list[dict] 存储 fields，模板 list 分支可用
    return {"event": data}


def render_html(
    event: NormalizedEvent,
    template_str: str | None = None,
) -> str:
    """使用 Jinja2 sandbox 将标准化事件渲染为 HTML。

    生成的 HTML 可直接用于 AstrBot html_render / T2I 截图。

    Args:
        event: 标准化事件对象。
        template_str: Jinja2 HTML 模板字符串，None 使用 DEFAULT_HTML_TEMPLATE。

    Returns:
        渲染后的 HTML 字符串。

    Raises:
        jinja2.exceptions.TemplateError: 模板渲染失败。
    """
    if template_str is None:
        template_str = DEFAULT_HTML_TEMPLATE

    context = render_html_data(event)
    env = _create_sandbox()
    template = env.from_string(template_str)
    result = template.render(**context)
    return result


def render_html_default(event: NormalizedEvent) -> str:
    """使用默认 HTML 模板将标准化事件渲染为 HTML 卡片。"""
    return render_html(event, DEFAULT_HTML_TEMPLATE)


def validate_image_result(result: Any) -> bool:
    """校验图片渲染结果是否有效。

    支持的类型：
    - str URL（不下载校验，信任 AstrBot 图片组件）
    - str ``base64://...``
    - str ``data:image/...;base64,...``
    - str 本地文件路径
    - bytes（检查 PNG/JPEG/WebP magic number）

    Args:
        result: 图片渲染结果。

    Returns:
        True 表示校验通过。

    Raises:
        ValueError: 结果无效或无法识别。
        TypeError: 结果类型不支持。
    """
    if result is None:
        raise ValueError("image result is None")

    if isinstance(result, bytes):
        return _validate_image_bytes(result)

    if isinstance(result, str):
        result_str = result.strip()

        # base64:// 前缀 — 解码后校验
        if result_str.startswith("base64://"):
            import base64

            b64_data = result_str[len("base64://") :].strip()
            try:
                decoded = base64.b64decode(b64_data)
            except Exception as e:
                raise ValueError(f"base64 解码失败: {e}") from e
            return _validate_image_bytes(decoded)

        # data:image/...;base64,... — 解码后校验
        if result_str.startswith("data:"):
            if ";base64," in result_str:
                _, b64_part = result_str.split(";base64,", 1)
                import base64

                try:
                    decoded = base64.b64decode(b64_part.strip())
                except Exception as e:
                    raise ValueError(f"data URL base64 解码失败: {e}") from e
                return _validate_image_bytes(decoded)
            # data URL without base64 — 非标准，跳过校验
            return True

        # 本地文件路径 — 检查存在性
        import os

        if os.path.exists(result_str) and os.path.isfile(result_str):
            with open(result_str, "rb") as f:
                header = f.read(16)
            return _validate_image_bytes(header, is_header=True)
        elif result_str.startswith("http://") or result_str.startswith("https://"):
            # URL — 不下载校验
            return True
        elif os.path.isfile(result_str):
            # 其他路径（含相对路径）
            with open(result_str, "rb") as f:
                header = f.read(16)
            return _validate_image_bytes(header, is_header=True)
        else:
            # 非 URL 且非本地路径 — 尝试作为 base64 解码
            import base64

            try:
                decoded = base64.b64decode(result_str)
            except Exception:
                raise ValueError(f"无法识别的图片结果: 不是 URL、路径或 base64 编码")
            return _validate_image_bytes(decoded)

    raise TypeError(f"不支持的图片结果类型: {type(result).__name__}")


def _validate_image_bytes(data: bytes, is_header: bool = False) -> bool:
    """校验 bytes 是否为受支持的图片格式。

    检查 PNG（\\x89PNG）、JPEG（\\xff\\xd8\\xff）、WebP（RIFF....WEBP）magic number。

    Args:
        data: 图片 bytes 或文件头部 bytes。
        is_header: 如果 True，data 仅为文件头部（前 16 字节），
                    仍可进行 magic number 检查。

    Returns:
        True 表示匹配已知格式。

    Raises:
        ValueError: 格式不匹配或数据过短。
    """
    if not data:
        raise ValueError("图片数据为空")

    min_len = 3  # JPEG magic 最小长度
    if len(data) < min_len:
        raise ValueError(f"图片数据过短 ({len(data)} bytes)，无法校验 magic number")

    # PNG: \x89PNG\r\n\x1a\n (8 bytes)
    if data[:4] == b"\x89PNG":
        if len(data) >= 8:
            expected = b"\x89PNG\r\n\x1a\n"
            if data[:8] == expected:
                return True
        return True  # 前 4 字节匹配即视为 PNG

    # JPEG: \xff\xd8\xff (3 bytes)
    if data[:3] == b"\xff\xd8\xff":
        return True

    # WebP: RIFF....WEBP (12 bytes)
    if len(data) >= 4 and data[:4] == b"RIFF":
        if len(data) >= 12:
            if data[8:12] == b"WEBP":
                return True
            raise ValueError("RIFF 文件头但非 WEBP 格式")
        # 头部不够 12 字节，仅匹配 RIFF 则视为可能 WebP
        return True

    raise ValueError(f"不支持的图片格式: magic={data[:8].hex()} (支持 PNG/JPEG/WebP)")


def _get(obj: Any, keys: list[str], default: Any = None) -> Any:
    """安全地从嵌套字典中获取值。"""
    current = obj
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
    if current is None or (isinstance(current, str) and not current):
        return default
    return current
