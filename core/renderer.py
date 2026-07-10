from __future__ import annotations

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
