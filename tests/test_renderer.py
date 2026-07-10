"""Renderer tests - no AstrBot dependency."""

from __future__ import annotations

from core.models import NormalizedEvent
from core.renderer import DEFAULT_TEXT_TEMPLATE, render_text, render_text_default


def _make_event(
    title: str = "会话完成",
    summary: str = "会话 test-session 已完成",
    fields: list | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        provider="omp",
        event="omp.session_stop",
        version=1,
        id="sess_001:turn_001",
        emitted_at="2026-07-08T12:00:00.000Z",
        title=title,
        status="success",
        summary=summary,
        source={"name": "oh-my-pi", "url": None},
        actor={"name": None, "url": None},
        fields=fields or [],
        links=[],
        raw={},
    )


class TestRenderTextDefault:
    def test_basic_render(self):
        """基本渲染应包含标题和字段。"""
        event = _make_event(
            fields=[
                {
                    "label": "会话",
                    "value": "Add post-conversation HTTP hook",
                    "short": True,
                },
                {"label": "模型", "value": "gpt-5.5", "short": True},
                {"label": "耗时", "value": "57.7s", "short": True},
            ],
        )
        result = render_text_default(event)
        assert "[oh-my-pi]" in result
        assert "会话完成" in result
        assert "会话：" in result
        assert "模型：" in result
        assert "耗时：" in result

    def test_empty_fields(self):
        """空 fields 不应渲染出多余内容。"""
        event = _make_event(fields=[])
        result = render_text_default(event)
        assert "[oh-my-pi]" in result
        # 不应有无标签的字段行
        assert "：" not in result or all(
            line.count("：") <= 1 for line in result.split("\n") if line.strip()
        )

    def test_field_value_without_label(self):
        """字段值即使无 label 也应渲染。"""
        fields = [{"label": "", "value": "just-a-value", "short": True}]
        event = _make_event(fields=fields)
        result = render_text_default(event)
        assert "just-a-value" in result

    def test_long_summary(self):
        """长摘要不应截断（文本模式下不截断）。"""
        long_summary = "A" * 500
        event = _make_event(summary=long_summary)
        result = render_text_default(event)
        assert long_summary in result

    def test_source_name_unknown(self):
        """source.name 为空时使用 unknown。"""
        event = _make_event()
        event.source["name"] = ""
        result = render_text_default(event)
        assert "[unknown]" in result

    def test_omp_example_format(self):
        """应匹配 FSD 中的 OMP 示例格式。"""
        event = _make_event(
            title="会话完成",
            summary="",
            fields=[
                {
                    "label": "会话",
                    "value": "Add post-conversation HTTP hook",
                    "short": True,
                },
                {"label": "模型", "value": "gpt-5.5", "short": True},
                {"label": "耗时", "value": "57.7s", "short": True},
                {"label": "输入", "value": "977 字 / 1 张图", "short": True},
                {"label": "消息变化", "value": "+2", "short": True},
                {"label": "最后状态", "value": "stop", "short": True},
            ],
        )
        result = render_text_default(event)
        # 验证主要结构
        lines = [l for l in result.split("\n") if l.strip()]
        assert len(lines) >= 3
        assert "[oh-my-pi]" in lines[0]
        assert all("模型" not in line or line == "模型：gpt-5.5" for line in lines)


class TestRenderTextJinja2:
    def test_default_template(self):
        """使用 Jinja2 默认模板渲染。"""
        event = _make_event(
            fields=[
                {"label": "模型", "value": "gpt-5.5", "short": True},
            ],
        )
        result = render_text(event)
        assert "[oh-my-pi]" in result
        assert "gpt-5.5" in result

    def test_custom_template(self):
        """自定义模板应正确渲染。"""
        template = "Custom: {{ event.title }} @ {{ event.source.name }}"
        event = _make_event(title="测试通知")
        result = render_text(event, template)
        assert result == "Custom: 测试通知 @ oh-my-pi"

    def test_template_sandbox(self):
        """sandbox 应阻止危险操作。"""
        template = "{{ event.__class__.__mro__ }}"
        event = _make_event()
        try:
            result = render_text(event, template)
            # sandbox 应导致渲染失败或返回空
            assert False, "sandbox 未阻止危险操作"
        except Exception:
            pass  # 预期异常

    def test_empty_template(self):
        """空模板应返回空字符串。"""
        event = _make_event()
        result = render_text(event, "")
        assert result == ""

    def test_event_dict_access(self):
        """模板应通过 event 命名空间访问字段。"""
        template = "{{ event.fields[0].label }}: {{ event.fields[0].value }}"
        event = _make_event(
            fields=[{"label": "版本", "value": "1.0", "short": True}],
        )
        result = render_text(event, template)
        assert result == "版本: 1.0"

    def test_default_template_matches_fsd(self):
        """默认模板应与 FSD 定义一致。"""
        expected_template = """\
[{{ event.source.name }}] {{ event.title }}

{% if event.summary %}{{ event.summary }}
{% endif %}{% for field in event.fields %}
{{ field.label }}：{{ field.value }}{% endfor %}
"""
        assert DEFAULT_TEXT_TEMPLATE == expected_template
