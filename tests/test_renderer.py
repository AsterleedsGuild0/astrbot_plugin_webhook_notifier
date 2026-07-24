"""Renderer tests - no AstrBot dependency."""

from __future__ import annotations

import pytest

from core.models import NormalizedEvent
from core.renderer import (
    DEFAULT_HTML_TEMPLATE,
    DEFAULT_TEXT_TEMPLATE,
    _expected_canvas_right,
    _scaled_right_crop_padding,
    render_html,
    render_html_data,
    render_html_default,
    render_preview,
    render_text,
    render_text_default,
    trim_viewport_whitespace,
    validate_html_template,
    validate_image_result,
)
from core.display import (
    INVALID_DISPLAY_TIMEZONE_WARNING,
    MISSING_DEFAULT_TIMEZONE_WARNING,
    create_display_context,
    format_duration_ms,
    format_timestamp,
    prepare_display_fields,
    status_label,
)


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
        assert "会话名称：" in result
        assert "模型：" in result
        assert "耗时：" in result

    def test_permission_aggregate_labels_match_in_text_and_html(self):
        event = _make_event(
            fields=[
                {"label": "permissionCount", "value": "2"},
                {"label": "permission[1].category", "value": "read"},
                {"label": "permission[2].summary", "value": "Write <file>"},
            ],
        )
        text = render_text_default(event)
        html = render_html_default(event)
        assert "权限请求数：2" not in text
        assert "权限 1 类型：read" in text
        assert "权限 2 摘要：Write <file>" in text
        assert "权限 1 类型" in html
        assert "Write &lt;file&gt;" in html

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
                {"label": "cwd", "value": "/home/user/project", "short": False},
                {"label": "模型", "value": "openai/gpt-5.5", "short": True},
                {
                    "label": "开始时间",
                    "value": "2026-07-08T11:59:00.000Z",
                    "short": True,
                },
                {"label": "耗时", "value": "57.7s", "short": True},
                {"label": "输入", "value": "977 字 / 1 张图", "short": True},
                {"label": "消息变化", "value": "+2", "short": True},
                {"label": "最后状态", "value": "stop", "short": True},
            ],
        )
        result = render_text_default(event)
        # 验证主要结构
        lines = [line for line in result.split("\n") if line.strip()]
        assert len(lines) >= 3
        assert "[oh-my-pi]" in lines[0]
        assert all(
            "模型" not in line or line == "模型：openai/gpt-5.5" for line in lines
        )
        assert "cwd：/home/user/project" in lines
        assert "开始时间：2026-07-08 19:59:00 CST (UTC+08:00)" in lines


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
            render_text(event, template)
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

状态：{{ event.status_display }}

{% if event.summary %}{{ event.summary }}
{% endif %}{% for field in event.fields %}
{{ field.label }}：{{ field.value }}{% endfor %}
"""
        assert DEFAULT_TEXT_TEMPLATE == expected_template


# ─── HTML 渲染测试 ─────────────────────────────────────────


class TestRenderHtmlData:
    def test_basic_structure(self):
        """render_html_data 返回的 dict 应包含 event 键。"""
        event = _make_event()
        context = render_html_data(event)
        assert "event" in context
        assert "generated_at" in context["event"]
        assert "event_time" in context["event"]

    def test_source_flattened(self):
        """source dict 应展平为字符串。"""
        event = _make_event()
        context = render_html_data(event)
        assert isinstance(context["event"]["source"], str)
        assert context["event"]["source"] == "oh-my-pi"

    def test_source_empty_fallback(self):
        """source.name 为空时使用 'AstrBot'。"""
        event = _make_event()
        event.source["name"] = ""
        context = render_html_data(event)
        assert context["event"]["source"] == "AstrBot"

    def test_status_and_opencode_labels_are_localized_in_display_copy(self):
        event = _make_event(title="Session One")
        event.status = "action_required"
        event.fields = [
            {"label": "projectName", "value": "Demo"},
            {"label": "sessionName", "value": "Session One"},
            {"label": "question[12].header", "value": "Environment"},
            {"label": "unknownField", "value": "keep"},
        ]
        display = render_html_data(event)["event"]
        labels = [field["label"] for field in display["fields"]]
        assert display["status_display"] == "待处理"
        assert labels == ["项目", "会话名称", "问题 12 标题", "unknownField"]

    def test_html_status_badge_is_localized_without_changing_event_status(self):
        event = _make_event()
        event.status = "action_required"
        html = render_html_default(event)
        assert "待处理" in html
        assert "action_required" not in html


class TestDisplayLocalization:
    def test_status_mapping_keeps_visual_semantics_values(self):
        assert status_label("completed") == "已完成"
        assert status_label("failed") == "失败"
        assert status_label("action_required") == "待处理"

    def test_dynamic_labels_unknown_fallback_and_duration_dedup(self):
        fields = prepare_display_fields(
            [
                {"label": "durationMs", "value": 1000},
                {"label": "duration", "value": "58h 37m"},
                {"label": "question[2]", "value": "Choose"},
                {"label": "question[2].options", "value": "A | B"},
                {"label": "futureField", "value": "raw"},
            ]
        )
        assert [field["label"] for field in fields] == [
            "当前任务耗时",
            "问题 2",
            "问题 2 选项",
            "futureField",
        ]
        assert fields[0]["value"] == "2 天 10 小时 37 分钟"
        assert fields[2]["value"] == "1. A\n2. B"

    def test_model_provider_display_and_session_name_is_always_independent(self):
        fields = prepare_display_fields(
            [
                {"label": "model", "value": "cpa/gpt-5.6-sol"},
                {"label": "model", "value": "cpa"},
                {"label": "sessionRef", "value": "anonymous-ref"},
                {"label": "sessionName", "value": "Session One"},
            ],
            title="Session One",
        )
        assert [(field["label"], field["value"]) for field in fields] == [
            ("模型", "cpa/gpt-5.6-sol"),
            ("模型提供方", "cpa"),
            ("会话名称", "Session One"),
        ]
        no_title = prepare_display_fields(
            [{"label": "sessionName", "value": "Session One"}]
        )
        assert no_title[0]["label"] == "会话名称"

    @pytest.mark.parametrize(
        ("fields", "model_variant", "expected"),
        [
            (
                [
                    {"label": "model", "value": "cpa/gpt-5.6-sol"},
                    {"label": "modelVariant", "value": "max"},
                ],
                "max",
                [("模型", "cpa/gpt-5.6-sol(max)")],
            ),
            (
                [{"label": "model", "value": "cpa/gpt-5.6-sol"}],
                None,
                [("模型", "cpa/gpt-5.6-sol")],
            ),
            (
                [{"label": "modelVariant", "value": "medium"}],
                "medium",
                [],
            ),
            (
                [
                    {"label": "model", "value": "cpa"},
                    {"label": "modelVariant", "value": "default"},
                ],
                "default",
                [("模型提供方", "cpa(default)")],
            ),
            (
                [
                    {"label": "model", "value": "cpa/gpt-5.6-sol"},
                    {"label": "modelVariant", "value": "experimental-v2"},
                ],
                "experimental-v2",
                [("模型", "cpa/gpt-5.6-sol(experimental-v2)")],
            ),
        ],
    )
    def test_model_variant_display_contract(self, fields, model_variant, expected):
        event = _make_event(fields=fields)
        event.model_variant = model_variant

        display_fields = render_html_data(event)["event"]["fields"]
        actual = [(field["label"], field["value"]) for field in display_fields]
        text = render_text_default(event)
        html = render_html_default(event)

        assert actual == expected
        assert "思考深度" not in text
        assert "思考深度" not in html
        for label, value in expected:
            assert f"{label}：{value}" in text
            assert label in html
            assert value in html

    def test_model_variant_uses_normalized_event_fallback_without_raw_field(self):
        event = _make_event(fields=[{"label": "model", "value": "cpa/gpt-5.6-sol"}])
        event.model_variant = "medium"

        fields = render_html_data(event)["event"]["fields"]
        assert [(field["label"], field["value"]) for field in fields] == [
            ("模型", "cpa/gpt-5.6-sol(medium)")
        ]

    def test_model_variant_text_and_html_share_value_and_escape_html(self):
        event = _make_event(
            fields=[
                {"label": "model", "value": "cpa/gpt-5.6-sol"},
                {"label": "modelVariant", "value": "max<&>"},
            ]
        )
        event.model_variant = "max<&>"

        display_value = render_html_data(event)["event"]["fields"][0]["value"]
        text = render_text_default(event)
        html = render_html_default(event)

        assert display_value == "cpa/gpt-5.6-sol(max<&>)"
        assert f"模型：{display_value}" in text
        assert "cpa/gpt-5.6-sol(max&lt;&amp;&gt;)" in html
        assert display_value not in html
        assert "思考深度" not in text
        assert "思考深度" not in html

    def test_question_counts_and_summary_are_context_aware(self):
        strict = prepare_display_fields(
            [
                {"label": "questionCount", "value": "1"},
                {"label": "optionCount", "value": "2"},
                {"label": "question.summary", "value": "Choose"},
            ]
        )
        assert [field["label"] for field in strict] == [
            "问题数量",
            "选项数量",
            "问题摘要",
        ]

        detailed = prepare_display_fields(
            [
                {"label": "questionCount", "value": "1"},
                {"label": "optionCount", "value": "2"},
                {"label": "question.summary", "value": "Choose"},
                {"label": "question[1]", "value": "Choose"},
            ]
        )
        assert [field["label"] for field in detailed] == ["问题 1"]

        multiple = prepare_display_fields(
            [
                {"label": "question.summary", "value": "Choose two"},
                {"label": "question[1]", "value": "Choose"},
                {"label": "question[2]", "value": "Choose another"},
            ]
        )
        assert "问题摘要" in [field["label"] for field in multiple]

    def test_permission_aggregate_labels_are_localized_and_count_deduplicated(self):
        fields = prepare_display_fields(
            [
                {"label": "permissionCount", "value": "2"},
                {"label": "permission[1].category", "value": "read"},
                {"label": "permission[2].summary", "value": "Write summary"},
            ]
        )
        assert [(field["label"], field["value"]) for field in fields] == [
            ("权限 1 类型", "read"),
            ("权限 2 摘要", "Write summary"),
        ]

    def test_duration_ms_alone_is_readable_chinese(self):
        fields = prepare_display_fields([{"label": "durationMs", "value": 65000}])
        assert fields[0]["label"] == "当前任务耗时"
        assert fields[0]["value"] == "1 分钟 5 秒"
        assert format_duration_ms(211_020_000) == "2 天 10 小时 37 分钟"

    @pytest.mark.parametrize(
        "value",
        [
            "2026-07-24T01:44:35Z",
            "2026-07-24T09:44:35+08:00",
            "2026-07-23T20:44:35-05:00",
        ],
    )
    def test_timestamp_inputs_convert_to_default_asia_shanghai(self, value):
        assert format_timestamp(value) == "2026-07-24 09:44:35 CST (UTC+08:00)"

    def test_configured_utc_and_tokyo_timezones(self):
        value = "2026-07-24T01:44:35Z"
        utc_context = create_display_context("UTC")
        tokyo_context = create_display_context("Asia/Tokyo")
        assert format_timestamp(value, utc_context) == (
            "2026-07-24 01:44:35 UTC (UTC+00:00)"
        )
        assert format_timestamp(value, tokyo_context) == (
            "2026-07-24 10:44:35 JST (UTC+09:00)"
        )

    @pytest.mark.parametrize("value", ["2026-07-24 01:44:35", "not-a-time"])
    def test_naive_and_invalid_timestamp_are_preserved(self, value):
        assert format_timestamp(value) == value

    def test_invalid_timezone_falls_back_without_leaking_value(self):
        warnings: list[str] = []
        context = create_display_context(
            "Sensitive/Private-Config", warn=warnings.append
        )
        assert context.timezone_name == "Asia/Shanghai"
        assert warnings == [INVALID_DISPLAY_TIMEZONE_WARNING]
        assert "Sensitive" not in warnings[0]

    def test_missing_default_zoneinfo_data_falls_back_to_utc(self, monkeypatch):
        import core.display as display
        from zoneinfo import ZoneInfoNotFoundError

        def missing_timezone(_name: str):
            raise ZoneInfoNotFoundError

        warnings: list[str] = []
        monkeypatch.setattr(display, "_load_timezone", missing_timezone)
        context = display.create_display_context("Asia/Shanghai", warn=warnings.append)
        assert context.timezone_name == "UTC"
        assert warnings == [MISSING_DEFAULT_TIMEZONE_WARNING]

    def test_text_html_footer_and_fields_share_display_timezone(self):
        event = _make_event(
            title="Session One",
            fields=[
                {"label": "sessionName", "value": "Session One"},
                {"label": "startedAt", "value": "2026-07-24T01:44:35Z"},
                {"label": "endedAt", "value": "2026-07-24T09:44:35+08:00"},
            ],
        )
        event.emitted_at = "2026-07-23T20:44:35-05:00"
        context = create_display_context("Asia/Shanghai")
        expected = "2026-07-24 09:44:35 CST (UTC+08:00)"

        text = render_text_default(event, context)
        html_data = render_html_data(event, context)["event"]
        html = render_html_default(event, context)

        assert "会话名称：Session One" in text
        assert text.count(expected) == 2
        assert [field["value"] for field in html_data["fields"][1:]] == [
            expected,
            expected,
        ]
        assert html_data["event_time"] == expected
        assert html_data["generated_at"].endswith("CST (UTC+08:00)")
        assert "会话名称" in html
        assert html.count(expected) == 3

    def test_task_time_labels_and_display_timezone(self):
        context = create_display_context("Asia/Tokyo")
        fields = prepare_display_fields(
            [
                {"label": "startedAt", "value": "2026-07-24T01:00:00Z"},
                {"label": "taskStartedAt", "value": "2026-07-24T01:15:00Z"},
                {"label": "endedAt", "value": "2026-07-24T01:45:00Z"},
                {"label": "durationMs", "value": 1_800_000},
            ],
            display_context=context,
        )
        assert [(field["label"], field["value"]) for field in fields] == [
            ("会话开始时间", "2026-07-24 10:00:00 JST (UTC+09:00)"),
            ("当前任务开始时间", "2026-07-24 10:15:00 JST (UTC+09:00)"),
            ("当前任务结束时间", "2026-07-24 10:45:00 JST (UTC+09:00)"),
            ("当前任务耗时", "30 分钟"),
        ]

    def test_action_required_does_not_synthesize_end_or_duration_fields(self):
        event = _make_event(
            fields=[
                {"label": "startedAt", "value": "2026-07-24T01:00:00Z"},
                {"label": "taskStartedAt", "value": "2026-07-24T01:15:00Z"},
            ]
        )
        event.status = "action_required"
        display = render_html_data(event)["event"]
        labels = [field["label"] for field in display["fields"]]
        assert labels == ["会话开始时间", "当前任务开始时间"]
        assert "当前任务结束时间" not in labels
        assert "当前任务耗时" not in labels

    @pytest.mark.parametrize("status", ["completed", "action_required"])
    def test_opencode_session_elapsed_is_derived_for_active_and_completed_events(
        self, status
    ):
        event = _make_event(
            fields=[
                {"label": "startedAt", "value": "2026-07-21T07:13:00Z"},
                {"label": "question[1]", "value": "Run `pytest`?"},
            ]
        )
        event.provider = "opencode"
        event.status = status
        event.emitted_at = "2026-07-24T01:00:00Z"

        display = render_html_data(event)["event"]
        assert ("会话已持续", "2 天 17 小时 47 分钟") in [
            (field["label"], field["value"]) for field in display["fields"]
        ]
        assert "当前任务耗时" not in [field["label"] for field in display["fields"]]

    @pytest.mark.parametrize(
        ("started_at", "emitted_at"),
        [
            ("invalid", "2026-07-24T01:00:00Z"),
            ("2026-07-24T00:00:00", "2026-07-24T01:00:00Z"),
            ("2026-07-24T00:00:00Z", "invalid"),
            ("2026-07-24T00:00:00Z", "2026-07-24T01:00:00"),
            ("2026-07-24T02:00:00Z", "2026-07-24T01:00:00Z"),
        ],
    )
    def test_invalid_or_negative_opencode_session_elapsed_is_omitted(
        self, started_at, emitted_at
    ):
        event = _make_event(fields=[{"label": "startedAt", "value": started_at}])
        event.provider = "opencode"
        event.emitted_at = emitted_at
        labels = [
            field["label"] for field in render_html_data(event)["event"]["fields"]
        ]
        assert "会话已持续" not in labels

    @pytest.mark.parametrize(
        "provider", ["omp", "unknown", "OpenCode", "OpenCode-compatible"]
    )
    def test_non_opencode_provider_does_not_derive_session_elapsed(self, provider):
        event = _make_event(
            fields=[{"label": "startedAt", "value": "2026-07-24T00:00:00Z"}]
        )
        event.provider = provider
        event.emitted_at = "2026-07-24T01:00:00Z"
        labels = [
            field["label"] for field in render_html_data(event)["event"]["fields"]
        ]
        assert "会话已持续" not in labels

    def test_session_elapsed_text_html_order_and_task_duration_are_independent(self):
        event = _make_event(
            fields=[
                {"label": "permission.description", "value": "Allow `bash`"},
                {"label": "endedAt", "value": "2026-07-24T00:59:00Z"},
                {"label": "question[1]", "value": "Run `pytest`?"},
                {"label": "taskStartedAt", "value": "2026-07-24T00:58:30Z"},
                {"label": "model", "value": "cpa/gpt-5.6-sol"},
                {"label": "durationMs", "value": 30_000},
                {"label": "startedAt", "value": "2026-07-23T23:00:00Z"},
                {"label": "agent", "value": "Designer"},
            ]
        )
        event.provider = "opencode"
        event.emitted_at = "2026-07-24T01:00:00Z"

        expected_labels = [
            "执行代理",
            "模型",
            "当前任务耗时",
            "会话已持续",
            "会话开始时间",
            "当前任务开始时间",
            "当前任务结束时间",
            "权限说明",
            "问题 1",
        ]
        display = render_html_data(event)["event"]
        assert [field["label"] for field in display["fields"]] == expected_labels
        assert [field["value"] for field in display["fields"]][2:4] == [
            "30 秒",
            "2 小时",
        ]

        text = render_text_default(event)
        html = render_html_default(event)
        assert "当前任务耗时：30 秒" in text
        assert "会话已持续：2 小时" in text
        assert "当前任务耗时" in html and "30 秒" in html
        assert "会话已持续" in html and "2 小时" in html
        assert text.index("当前任务耗时") < text.index("会话已持续")
        assert html.index("当前任务耗时") < html.index("会话已持续")

    def test_display_timezone_changes_timestamps_but_not_session_elapsed(self):
        event = _make_event(
            fields=[{"label": "startedAt", "value": "2026-07-24T01:00:00Z"}]
        )
        event.provider = "opencode"
        event.emitted_at = "2026-07-24T10:30:00+09:00"

        utc_fields = render_html_data(event, create_display_context("UTC"))["event"][
            "fields"
        ]
        tokyo_fields = render_html_data(event, create_display_context("Asia/Tokyo"))[
            "event"
        ]["fields"]
        utc_values = {field["label"]: field["value"] for field in utc_fields}
        tokyo_values = {field["label"]: field["value"] for field in tokyo_fields}
        assert utc_values["会话已持续"] == "30 分钟"
        assert tokyo_values["会话已持续"] == "30 分钟"
        assert utc_values["会话开始时间"].endswith("UTC (UTC+00:00)")
        assert tokyo_values["会话开始时间"].endswith("JST (UTC+09:00)")

    def test_omp_and_unknown_field_labels_are_preserved(self):
        fields = prepare_display_fields(
            [
                {"label": "开始时间", "value": "2026-07-24T01:00:00Z"},
                {"label": "耗时", "value": "2m"},
                {"label": "futureField", "value": "keep"},
            ]
        )
        assert [(field["label"], field["value"]) for field in fields] == [
            ("开始时间", "2026-07-24 09:00:00 CST (UTC+08:00)"),
            ("耗时", "2 分钟"),
            ("futureField", "keep"),
        ]

    def test_question_options_text_and_html_are_multiline_and_escaped(self):
        values = [
            [
                {"label": "Allow", "description": "<b>safe</b>", "recommended": True},
                {"label": "Deny", "description": "No", "recommended": False},
            ],
            '[{"label":"Allow","description":"<b>safe</b>","recommended":true},{"label":"Deny"}]',
            "Allow: <b>safe</b> (recommended=true) | Deny",
        ]
        for value in values:
            event = _make_event(
                fields=[{"label": "question[1].options", "value": value}]
            )
            text = render_text_default(event)
            html = render_html_default(event)
            assert "1. Allow（推荐）" in text
            assert "   <b>safe</b>" in text
            assert "2. Deny" in text
            assert "1. Allow（推荐）" in html
            assert "   &lt;b&gt;safe&lt;/b&gt;" in html
            assert " | " not in html


class TestRenderHtml:
    def test_default_template_renders(self):
        """使用默认 HTML 模板应正常渲染。"""
        event = _make_event(
            fields=[
                {"label": "模型", "value": "gpt-5.5", "short": True},
                {"label": "耗时", "value": "57.7s", "short": True},
            ],
        )
        html = render_html_default(event)
        assert "<!doctype html>" in html.lower() or "<html" in html.lower()
        assert "oh-my-pi" in html
        assert "gpt-5.5" in html
        assert "57.7 秒" in html
        assert "会话完成" in html

    def test_empty_summary(self):
        """空 summary 不应输出 summary 区域。"""
        event = _make_event(summary="")
        html = render_html_default(event)
        assert 'class="summary"' not in html

    def test_summary_with_content(self):
        """非空 summary 应渲染到页面。"""
        event = _make_event(summary="任务已完成")
        html = render_html_default(event)
        assert "任务已完成" in html
        assert 'class="summary"' in html

    def test_multiple_fields(self):
        """多字段应全部渲染。"""
        fields = [
            {"label": "会话", "value": "test-session"},
            {"label": "模型", "value": "gpt-5.5"},
            {"label": "耗时", "value": "1m 30s"},
            {"label": "输入", "value": "500 字"},
            {"label": "消息变化", "value": "+3"},
        ]
        event = _make_event(fields=fields)
        html = render_html_default(event)
        for f in fields:
            assert f["label"] in html
            expected_value = "1 分钟 30 秒" if f["label"] == "耗时" else f["value"]
            assert expected_value in html

    def test_no_fields(self):
        """无字段时应显示占位文本。"""
        event = _make_event(fields=[])
        html = render_html_default(event)
        assert "暂无可展示字段" in html

    def test_field_token_filtered(self):
        """包含 token/raw/prompt 的字段应被过滤。"""
        fields = [
            {"label": "会话", "value": "visible"},
            {"label": "access_token", "value": "secret"},
            {"label": "raw_payload", "value": "should-be-hidden"},
            {"label": "prompt_text", "value": "should-be-hidden"},
        ]
        event = _make_event(fields=fields)
        html = render_html_default(event)
        assert "visible" in html
        assert "secret" not in html
        assert "should-be-hidden" not in html

    def test_html_template_escapes_content(self):
        """HTML 卡片内容应转义，避免字段值破坏卡片结构。"""
        event = _make_event(
            title="<b>标题</b>",
            summary="<script>alert(1)</script>",
            fields=[{"label": "路径 <cwd>", "value": "/tmp/<project>"}],
        )
        html = render_html_default(event)
        assert "&lt;b&gt;标题&lt;/b&gt;" in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "路径 &lt;cwd&gt;" in html
        assert "/tmp/&lt;project&gt;" in html
        assert "<script>alert(1)</script>" not in html

    def test_inline_code_is_rendered_and_all_content_is_escaped(self):
        event = _make_event(
            summary="执行 `pytest tests`，不要解析 <em>HTML</em>",
            fields=[
                {
                    "label": "问题",
                    "value": "编辑 `core/renderer.py` 后保留 <img src=x onerror=alert(1)>",
                },
                {
                    "label": "权限",
                    "value": "允许 `<script>alert(1)</script>` 吗？",
                },
            ],
        )
        html = render_html_default(event)
        assert "执行 <code>pytest tests</code>" in html
        assert "&lt;em&gt;HTML&lt;/em&gt;" in html
        assert "编辑 <code>core/renderer.py</code>" in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "<code>&lt;script&gt;alert(1)&lt;/script&gt;</code>" in html
        assert "<img src=x" not in html
        assert "<script>alert(1)</script>" not in html

    def test_inline_code_supports_multiple_spans_and_preserves_invalid_ticks(self):
        event = _make_event(
            fields=[
                {
                    "label": "内容",
                    "value": "运行 `python -m pytest`，再看 `中文 文件.py`；未闭合 `tail；空 ``；双 ``code``",
                }
            ]
        )
        html = render_html_default(event)
        assert "<code>python -m pytest</code>" in html
        assert "<code>中文 文件.py</code>" in html
        assert "未闭合 `tail" in html
        assert "空 ``" in html
        assert "双 ``code``" in html
        assert html.count("<code>") == 2

    def test_inline_code_in_multiline_options_keeps_numbering_and_description(self):
        event = _make_event(
            fields=[
                {
                    "label": "question[1].options",
                    "value": [
                        {
                            "label": "运行 `pytest`",
                            "description": "检查 `tests/test_renderer.py`",
                            "recommended": True,
                        },
                        {
                            "label": "跳过",
                            "description": "保留 `--no-run`",
                        },
                    ],
                }
            ]
        )
        html = render_html_default(event)
        assert "1. 运行 <code>pytest</code>（推荐）" in html
        assert "   检查 <code>tests/test_renderer.py</code>" in html
        assert "2. 跳过" in html
        assert "   保留 <code>--no-run</code>" in html

    def test_text_mode_keeps_backticks_without_html_tags(self):
        event = _make_event(
            summary="运行 `pytest`",
            fields=[{"label": "命令", "value": "`python -m compileall core`"}],
        )
        text = render_text_default(event)
        assert "运行 `pytest`" in text
        assert "命令：`python -m compileall core`" in text
        assert "<code>" not in text

    def test_long_inline_code_has_wrapping_css(self):
        event = _make_event(fields=[{"label": "命令", "value": f"`{'x' * 500}`"}])
        html = render_html_default(event)
        assert f"<code>{'x' * 500}</code>" in html
        assert ".field-value code" in html
        assert "overflow-wrap: anywhere" in html
        assert "word-break: break-all" in html

    def test_html_template_keeps_falsey_field_values(self):
        """0 和 False 等字段值也应展示，不能被默认值吞掉。"""
        event = _make_event(
            fields=[
                {"label": "退出码", "value": 0},
                {"label": "是否跳过", "value": False},
            ],
        )
        html = render_html_default(event)
        assert "退出码" in html
        assert ">0</div>" in html
        assert "是否跳过" in html
        assert "False" in html

    def test_sandbox_blocks_dangerous(self):
        """sandbox 应阻断危险操作。"""
        dangerous_template = "<html><body>{{ event.__class__.__mro__ }}</body></html>"
        event = _make_event()
        try:
            render_html(event, dangerous_template)
            assert False, "sandbox 未阻止危险操作"
        except Exception:
            pass

    def test_custom_template(self):
        """自定义模板应正确渲染。"""
        template = (
            "<html><body>Custom: {{ event.title }} @ {{ event.source }}</body></html>"
        )
        event = _make_event(title="测试通知")
        html = render_html(event, template)
        assert "Custom: 测试通知 @ oh-my-pi" in html

    def test_event_time_from_emitted_at(self):
        """未传入 event_time 时应回退到 emitted_at。"""
        event = _make_event()
        context = render_html_data(event)
        assert context["event"]["event_time"] == ("2026-07-08 20:00:00 CST (UTC+08:00)")

    def test_default_template_contains_styles(self):
        """默认 HTML 模板应包含 macOS 浅色卡片样式及 shrinkwrap CSS。"""
        assert "box-sizing" in DEFAULT_HTML_TEMPLATE
        assert "-apple-system" in DEFAULT_HTML_TEMPLATE
        assert "PingFang SC" in DEFAULT_HTML_TEMPLATE
        assert ".status-badge" in DEFAULT_HTML_TEMPLATE
        assert "background: #ffffff" in DEFAULT_HTML_TEMPLATE
        assert "width: fit-content" in DEFAULT_HTML_TEMPLATE
        assert "min-width: 0" in DEFAULT_HTML_TEMPLATE
        assert "min-height: 0" in DEFAULT_HTML_TEMPLATE
        assert "height: auto" in DEFAULT_HTML_TEMPLATE
        assert "width: 780px" in DEFAULT_HTML_TEMPLATE
        assert "max-width: 780px" in DEFAULT_HTML_TEMPLATE
        assert "width: 100vw" not in DEFAULT_HTML_TEMPLATE
        assert "min-height: 100%" not in DEFAULT_HTML_TEMPLATE
        assert "justify-content: center" not in DEFAULT_HTML_TEMPLATE
        assert "#0a0f1c" not in DEFAULT_HTML_TEMPLATE

    @pytest.mark.parametrize(
        "dangerous",
        [
            "<script>alert(1)</script>",
            '<img src="https://example.com/a.png">',
            '<div onclick="alert(1)">x</div>',
            "<style>@import 'x.css';</style>",
            '<meta http-equiv="refresh" content="0">',
            '<meta http-equiv="Content-Security-Policy" content="default-src *">',
        ],
    )
    def test_dangerous_html_rejected(self, dangerous):
        with pytest.raises(ValueError):
            validate_html_template(dangerous)

    def test_preview_limits_sensitive_keys_and_csp(self):
        html, width = render_preview(
            "<html><head></head><body>{{ event.title }}</body></html>",
            {"title": "safe"},
            700,
        )
        assert width == 700
        assert "Content-Security-Policy" in html
        with pytest.raises(ValueError):
            render_preview("<p>x</p>", {"api_key": "hidden"}, 700)
        with pytest.raises(ValueError):
            render_preview("<p>x</p>", {"items": [0] * 201}, 700)

    def test_preview_injects_csp_when_body_mentions_header_name(self):
        html, _ = render_preview(
            "<html><head></head><body>Content-Security-Policy</body></html>",
            {},
            780,
        )
        assert '<meta http-equiv="Content-Security-Policy"' in html


# ─── 图片结果校验测试 ─────────────────────────────────────


class TestValidateImageResult:
    def test_valid_png_bytes(self):
        """PNG magic number 应通过校验。"""
        result = b"\x89PNG\r\n\x1a\n" + b"dummy_data"
        assert validate_image_result(result) is True

    def test_valid_jpeg_bytes(self):
        """JPEG magic number 应通过校验。"""
        result = b"\xff\xd8\xff" + b"dummy_data"
        assert validate_image_result(result) is True

    def test_valid_webp_bytes(self):
        """WebP RIFF....WEBP 应通过校验。"""
        result = b"RIFF\x00\x00\x00\x00WEBP" + b"dummy"
        assert validate_image_result(result) is True

    def test_invalid_bytes(self):
        """无效图片 bytes 应抛出 ValueError。"""
        result = b"\x00\x00\x00\x00\x00\x00\x00\x00"
        try:
            validate_image_result(result)
            assert False, "应抛出 ValueError"
        except ValueError:
            pass

    def test_empty_bytes(self):
        """空 bytes 应抛出 ValueError。"""
        try:
            validate_image_result(b"")
            assert False
        except ValueError:
            pass

    def test_base64_prefix(self):
        """base64:// 前缀的 PNG 应解码后校验。"""
        import base64

        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64_str = "base64://" + base64.b64encode(png_bytes).decode()
        assert validate_image_result(b64_str) is True

    def test_invalid_base64(self):
        """无法解码的 base64:// 应抛出 ValueError。"""
        result = "base64://not-valid-base64!!!"
        try:
            validate_image_result(result)
            assert False
        except ValueError:
            pass

    def test_data_url_png(self):
        """data:image/png;base64,... 应解码并校验。"""
        import base64

        png_bytes = b"\x89PNG\r\n\x1a\n"
        b64_str = base64.b64encode(png_bytes).decode()
        data_url = f"data:image/png;base64,{b64_str}"
        assert validate_image_result(data_url) is True

    def test_http_url(self):
        """HTTP URL 应通过校验（不下载）。"""
        assert validate_image_result("https://example.com/image.png") is True

    def test_none_result(self):
        """None 应抛出 ValueError。"""
        try:
            validate_image_result(None)
            assert False
        except ValueError:
            pass

    def test_unsupported_type(self):
        """不支持的类型应抛出 TypeError。"""
        try:
            validate_image_result(123)
            assert False
        except TypeError:
            pass


# ─── 视口空白裁切测试 ────────────────────────────────────


class TestTrimViewportWhitespace:
    def test_expected_canvas_right_when_viewport_width_honored(self):
        """viewport_width 生效时，按 812px 视口推断右边界。"""
        # 812 * 1.3 = 1055.6，内容右边界约 (16 + 780) * 1.3
        assert _expected_canvas_right(int(812 * 1.3), 812) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_old_viewport(self):
        """云端仍使用旧 viewport_width=860 时，也应按实际卡片宽度裁剪。"""
        assert _expected_canvas_right(int(860 * 1.3), 860) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_custom_viewport(self):
        """云端配置自定义 viewport_width=900 时，也应按实际卡片宽度裁剪。"""
        assert _expected_canvas_right(int(900 * 1.3), 900) == int(796 * 1.3)

    def test_expected_canvas_right_accepts_dedicated_card_width(self):
        """帮助卡片可传入 868px 专用宽度，避免按通知卡片宽度过度裁切。"""
        assert _expected_canvas_right(
            int(900 * 1.3), 900, card_width=868, body_padding=16
        ) == int(884 * 1.3)

    def test_expected_canvas_right_when_default_viewport_used(self):
        """旧 T2I 忽略 viewport_width 时，按 1280px 默认视口兜底推断。"""
        # 1280 * 1.3 = 1664，仍应裁到 812px 画布附近，而不是保留 1280px 视口。
        assert _expected_canvas_right(1664, 812) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_old_viewport_and_default_viewport(
        self,
    ):
        """旧配置 860 + 旧 T2I 默认 1280 视口时，仍应按 780px 卡片宽度裁剪。"""
        assert _expected_canvas_right(1664, 860) == int(796 * 1.3)

    def test_expected_canvas_right_uses_card_width_with_custom_viewport_and_default_viewport(
        self,
    ):
        """自定义配置 900 + 旧 T2I 默认 1280 视口时，仍应按 780px 卡片宽度裁剪。"""
        assert _expected_canvas_right(1664, 900) == int(796 * 1.3)

    def test_scaled_right_crop_padding_uses_fallback_viewport_scale(self):
        """旧 T2I 默认 1280 视口时，右侧裁剪留白应按真实 scale，而非整图比例。"""
        assert _scaled_right_crop_padding(1664, 812) == int(12 * 1.3)

    def test_url_passthrough(self):
        """URL 字符串应原样返回（不处理）。"""
        url = "https://example.com/img.png"
        assert trim_viewport_whitespace(url) is url

    def test_bytes_passthrough(self):
        """bytes 应原样返回。"""
        data = b"dummy bytes"
        assert trim_viewport_whitespace(data) is data

    def test_none_passthrough(self):
        """None 应原样返回。"""
        assert trim_viewport_whitespace(None) is None

    def test_local_file_cropped(self):
        """本地 PNG 截图，右侧/底部为纯背景，调用后尺寸应缩小。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        # 构造一张旧 T2I 默认 1280 视口、high scale=1.3 的图片：
        # - 白色内容区到 812px 画布附近，其余为灰色背景
        # - canvas_width=812 模拟插件传入的目标视口宽度
        width, height = 1664, 520
        img = Image.new("RGB", (width, height), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, int(796 * 1.3), 360], fill=(255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=812)
            assert result == tmp_path

            # 验证已裁切
            with Image.open(tmp_path) as cropped:
                assert cropped.width < width, "右侧空白应被裁切"
                assert cropped.height < height, "底部空白应被裁切"
                # 内容区不应被过度裁切
                assert cropped.width >= int(796 * 1.3)
                assert cropped.width <= int(808 * 1.3) + 2
                assert cropped.height >= 360
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    def test_different_formats_jpeg(self):
        """JPEG 格式应正确处理。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        width, height = 1280, 420
        img = Image.new("RGB", (width, height), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 796, 280], fill=(255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="JPEG", quality=95)

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=812)
            assert result == tmp_path

            with Image.open(tmp_path) as cropped:
                assert cropped.width < width
                assert cropped.width <= 810
                assert cropped.height < height
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    def test_no_crop_needed(self):
        """内容已铺满的图片不应被裁切（也不归一化）。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        width, height = 300, 200
        img = Image.new("RGB", (width, height), (255, 255, 255))  # 全部白色

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            result = trim_viewport_whitespace(tmp_path, canvas_width=812)
            assert result == tmp_path

            # 不应裁切（尺寸不变）
            with Image.open(tmp_path) as reloaded:
                assert reloaded.size == (width, height)
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    def test_small_image_skipped(self):
        """过小的图片应跳过裁切。"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        import tempfile

        img = Image.new("RGB", (100, 80), (255, 255, 255))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")

        try:
            # 尺寸 < 360x240，不应裁切
            result = trim_viewport_whitespace(tmp_path, canvas_width=200)
            assert result == tmp_path

            with Image.open(tmp_path) as reloaded:
                assert reloaded.size == (100, 80)
        finally:
            import os as _os

            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)
