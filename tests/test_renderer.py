"""Renderer tests - no AstrBot dependency."""

from __future__ import annotations

from html.parser import HTMLParser
from itertools import pairwise

import pytest

from core.display import (
    INVALID_DISPLAY_TIMEZONE_WARNING,
    MISSING_DEFAULT_TIMEZONE_WARNING,
    create_display_context,
    format_duration_ms,
    format_timestamp,
    prepare_display_fields,
    status_label,
)
from core.models import NormalizedEvent
from core.notification_policy import SessionScope
from core.renderer import (
    _TIMELINE_TICK_SAFETY_GAP_PX,
    _build_subagent_timeline_view,
    _expected_canvas_right,
    _scaled_right_crop_padding,
    _timeline_coverage_rate,
    _timeline_coverage_union_ms,
    DEFAULT_HTML_TEMPLATE,
    DEFAULT_TEXT_TEMPLATE,
    SUBAGENT_TIMELINE_HTML_TEMPLATE,
    SUBAGENT_TIMELINE_MAIN_ITEM_LIMIT,
    SUBAGENT_TIMELINE_MAX_ITEMS,
    SUBAGENT_TIMELINE_MAX_VIEWPORT_WIDTH,
    SUBAGENT_TIMELINE_MIN_BAR_WIDTH,
    SUBAGENT_TIMELINE_MIN_VIEWPORT_WIDTH,
    prepare_subagent_timeline,
    render_html,
    render_html_data,
    render_html_default,
    render_preview,
    render_subagent_timeline,
    render_subagent_timeline_html,
    render_text,
    render_text_default,
    trim_viewport_whitespace,
    validate_html_template,
    validate_image_result,
)


class _TimelineDOMProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.row_min_heights: list[int] = []
        self.task_names: list[str] = []
        self._task_depth = 0
        self._task_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "div" and "timeline-row" in classes:
            style = attributes.get("style") or ""
            declaration = next(
                part for part in style.split(";") if "min-height:" in part
            )
            self.row_min_heights.append(
                int(declaration.split(":", 1)[1].strip().removesuffix("px"))
            )
        if tag == "div" and "task-name" in classes:
            self._task_depth = 1
            self._task_parts = []
        elif self._task_depth:
            self._task_depth += 1

    def handle_endtag(self, tag):
        if not self._task_depth:
            return
        self._task_depth -= 1
        if self._task_depth == 0:
            self.task_names.append("".join(self._task_parts))

    def handle_data(self, data):
        if self._task_depth:
            self._task_parts.append(data)


def _assert_tick_layout(view: dict) -> None:
    ticks = view["axis_ticks"]
    labels = [tick["label"] for tick in ticks]
    positions = [float(tick["left_px"]) for tick in ticks]
    widths = [float(tick["label_width_px"]) for tick in ticks]
    assert 5 <= len(ticks) <= 10
    assert len(labels) == len(set(labels))
    assert all(right > left for left, right in pairwise(positions))
    for index, (left, right) in enumerate(pairwise(positions)):
        required = (
            widths[index] + widths[index + 1]
        ) / 2 + _TIMELINE_TICK_SAFETY_GAP_PX
        assert right - left + 0.03 >= required

    plot_width = float(view["layout"]["plot_width"])
    tail = plot_width - positions[-1]
    segments = [right - left for left, right in pairwise(positions)]
    if tail > 0.03:
        segments.append(tail)
    assert max(segments) / min(segments) < 1.75


def _parse_short_tick_label_ms(label: str) -> float:
    if label == "0":
        return 0.0
    assert label.startswith("+")
    if label.endswith("毫秒"):
        return float(label[1:-2])
    assert label.endswith("秒")
    return float(label[1:-1]) * 1000


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


def _timeline_item(
    index: int,
    *,
    start: int | None = None,
    end: int | None = None,
    depth: int = 1,
    status: str = "completed",
    timing_quality: str | None = None,
    name: str | None = None,
    agent: str | None = "worker",
    model: str | None = None,
    model_variant: str | None = None,
) -> dict:
    if start is None and end is None and timing_quality is None:
        timing_quality = "unknown"
    elif timing_quality is None:
        timing_quality = "observed"
    item = {
        "ref": f"{index + 1:032x}",
        "parentRef": "f" * 32,
        "name": name if name is not None else f"child-{index}",
        "status": status,
        "timingQuality": timing_quality,
        "depth": depth,
        "attempt": 1,
    }
    if agent is not None:
        item["agent"] = agent
    if model is not None:
        item["model"] = model
    if model_variant is not None:
        item["modelVariant"] = model_variant
    if start is not None:
        item["startOffsetMs"] = start
    if end is not None:
        item["endOffsetMs"] = end
    if start is not None and end is not None and timing_quality != "partial":
        item["durationMs"] = end - start
    return item


def _wait_interval(
    *,
    kind: str = "question",
    state: str = "complete",
    start: int | None = 100,
    end: int | None = 500,
    result: str = "replied",
) -> dict:
    interval: dict = {"kind": kind, "intervalState": state}
    if state == "complete":
        assert start is not None and end is not None
        interval.update(
            {
                "result": result,
                "startOffsetMs": start,
                "endOffsetMs": end,
                "durationMs": end - start,
            }
        )
    elif state == "right_censored":
        assert start is not None
        interval["startOffsetMs"] = start
    elif state == "left_censored":
        assert end is not None
        interval.update({"result": result, "endOffsetMs": end})
    return interval


def _wait_timeline(
    intervals: list[dict],
    *,
    partial: bool = False,
    partial_reasons: list[str] | None = None,
    truncated: bool = False,
    observed_count: int | None = None,
) -> dict:
    return {
        "version": 1,
        "timeBasis": "root_cycle_receipt_monotonic",
        "partial": partial,
        "partialReasons": partial_reasons or [],
        "observedIntervalCount": observed_count
        if observed_count is not None
        else len(intervals),
        "displayedIntervalCount": len(intervals),
        "truncated": truncated,
        "intervals": intervals,
    }


def _timeline_event(
    items: list[dict],
    *,
    partial: bool = False,
    partial_reasons: list[str] | None = None,
    truncated: bool = False,
    observed_count: int | None = None,
    total_duration_ms: int | None | object = ...,
    user_wait_timeline: dict | None | object = ...,
) -> NormalizedEvent:
    event = _make_event(title="根任务已完成", fields=[])
    event.provider = "opencode"
    event.event = "opencode.session_idle"
    event.session_scope = SessionScope.ROOT
    event.subagent_timeline = {
        "version": 1,
        "partial": partial,
        "partialReasons": partial_reasons or [],
        "timeBasis": "root_cycle",
        "observedItemCount": observed_count
        if observed_count is not None
        else len(items),
        "displayedItemCount": len(items),
        "truncated": truncated,
        "items": items,
    }
    if total_duration_ms is ...:
        valid_ends = [
            item["endOffsetMs"]
            for item in items
            if isinstance(item.get("endOffsetMs"), (int, float))
            and not isinstance(item.get("endOffsetMs"), bool)
        ]
        event.task_duration_ms = int(max(valid_ends, default=0))
    else:
        event.task_duration_ms = (
            total_duration_ms if isinstance(total_duration_ms, int) else None
        )
    if user_wait_timeline is not ...:
        event.user_wait_timeline = (
            user_wait_timeline if isinstance(user_wait_timeline, dict) else None
        )
    return event


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

    def test_opencode_timeline_text_fallback_is_readable(self):
        event = _make_event(fields=[])
        event.provider = "opencode"
        event.event = "opencode.session_idle"
        event.session_scope = SessionScope.ROOT
        event.subagent_timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [
                {
                    "ref": "a" * 32,
                    "parentRef": "b" * 32,
                    "name": "Build child",
                    "agent": "worker",
                    "status": "completed",
                    "startOffsetMs": 0,
                    "endOffsetMs": 65_000,
                    "durationMs": 65_000,
                    "timingQuality": "observed",
                    "depth": 1,
                    "attempt": 1,
                }
            ],
        }

        result = render_text_default(event)
        assert "子任务时间线：" in result
        assert "任务数：1（展示 1）" in result
        assert "状态：完整" in result
        assert "Build child（worker）：已完成，耗时 1 分钟 5 秒" in result
        assert "a" * 32 not in result
        assert "b" * 32 not in result

    def test_opencode_timeline_text_fallback_handles_partial_missing_fields(self):
        event = _make_event(fields=[])
        event.provider = "opencode"
        event.event = "opencode.session_idle"
        event.session_scope = SessionScope.ROOT
        event.subagent_timeline = {
            "version": 1,
            "partial": True,
            "partialReasons": ["missing_start", "missing_end"],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [
                {
                    "ref": "c" * 32,
                    "parentRef": "d" * 32,
                    "status": "unknown",
                    "timingQuality": "unknown",
                    "depth": 1,
                    "attempt": 1,
                }
            ],
        }

        result = render_text_default(event)
        assert "状态：部分数据" in result
        assert "- 子任务：未知" in result
        assert "耗时" not in result.split("子任务时间线：", 1)[1]
        assert "c" * 32 not in result
        assert "d" * 32 not in result

    def test_opencode_timeline_partial_or_clamped_never_shows_exact_duration(self):
        event = _make_event(fields=[])
        event.provider = "opencode"
        event.event = "opencode.session_idle"
        event.session_scope = SessionScope.ROOT
        event.subagent_timeline = {
            "version": 1,
            "partial": True,
            "partialReasons": ["clamped"],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [
                {
                    "ref": "a" * 32,
                    "parentRef": "b" * 32,
                    "status": "completed",
                    "startOffsetMs": 0,
                    "endOffsetMs": 1000,
                    "timingQuality": "partial",
                    "depth": 1,
                    "attempt": 1,
                }
            ],
        }

        result = render_text_default(event)
        timeline_text = result.split("子任务时间线：", 1)[1]
        assert "耗时" not in timeline_text
        assert "区间不完整" in timeline_text

    def test_opencode_timeline_text_fallback_is_bounded(self):
        event = _make_event(fields=[])
        event.provider = "opencode"
        event.event = "opencode.session_idle"
        event.session_scope = SessionScope.ROOT
        event.subagent_timeline = {
            "version": 1,
            "partial": True,
            "partialReasons": ["truncated"],
            "timeBasis": "root_cycle",
            "observedItemCount": 20,
            "displayedItemCount": 20,
            "truncated": True,
            "items": [
                {
                    "ref": f"{index:032x}",
                    "parentRef": "e" * 32,
                    "name": f"Child {index}",
                    "status": "running",
                    "timingQuality": "unknown",
                    "depth": 1,
                    "attempt": 1,
                }
                for index in range(20)
            ],
        }

        result = render_text_default(event)
        assert "状态：部分数据、已截断" in result
        assert "Child 11" in result
        assert "Child 12" not in result
        assert "其余 8 个任务未展开" in result

    def test_mixed_timeline_text_keeps_bounded_subagents_and_adds_wait_residual(self):
        event = _timeline_event(
            [
                _timeline_item(index, start=0, end=1000, name=f"mixed-{index}")
                for index in range(20)
            ],
            total_duration_ms=5000,
            user_wait_timeline=_wait_timeline(
                [
                    _wait_interval(start=2000, end=3000),
                    _wait_interval(state="right_censored", start=4000, end=None),
                ],
                partial=True,
                partial_reasons=["open_at_cycle_end"],
            ),
        )
        text = render_text_default(event)
        timeline_text = text.split("子任务时间线：", 1)[1]
        assert "等待用户至少 1 秒 · 至少 1 次" in timeline_text
        assert "结束时仍在等待" in timeline_text
        assert "未分类时间 / 占比 · 观测受限：至多 3 秒（60%）" in timeline_text
        assert "mixed-11" in timeline_text
        assert "mixed-12" not in timeline_text
        assert "其余 8 个任务未展开" in timeline_text
        assert timeline_text.count("子任务时间线：") == 0

    @pytest.mark.parametrize(
        ("total_duration_ms", "expected"),
        [
            (5000, "未分类时间 / 占比：3 秒（60%）"),
            (None, "未分类时间 / 占比：不可计算"),
        ],
    )
    def test_mixed_timeline_text_preserves_exact_and_unavailable_residual_semantics(
        self, total_duration_ms, expected
    ):
        event = _timeline_event(
            [_timeline_item(0, start=0, end=1000)],
            total_duration_ms=total_duration_ms,
            user_wait_timeline=_wait_timeline([_wait_interval(start=2000, end=3000)]),
        )
        text = render_text_default(event)
        assert "等待用户 1 秒 · 1 次" in text
        assert "观测完整 · Question 1" in text
        assert expected in text

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

    def test_timeline_is_preserved_for_phase_two_without_raw_leakage(self):
        timeline = {
            "version": 1,
            "partial": False,
            "partialReasons": [],
            "timeBasis": "root_cycle",
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "truncated": False,
            "items": [
                {
                    "ref": "f" * 32,
                    "parentRef": "0" * 32,
                    "status": "completed",
                    "timingQuality": "unknown",
                    "depth": 1,
                    "attempt": 1,
                }
            ],
        }
        event = _make_event()
        event.provider = "opencode"
        event.event = "opencode.session_idle"
        event.session_scope = SessionScope.ROOT
        event.subagent_timeline = timeline

        assert event.raw == {}
        assert event.to_dict()["subagent_timeline"] == timeline
        html_data = render_html_data(event)["event"]
        assert html_data["subagent_timeline"] == timeline
        assert "raw" in html_data and html_data["raw"] == {}

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

    def test_opencode_child_card_shows_escaped_session_hierarchy_in_order(self):
        event = _make_event(title="子会话 <Child>")
        event.provider = "opencode"
        event.event = "opencode.permission_asked"
        event.status = "action_required"
        event.fields = [
            {"label": "permission[1].summary", "value": "Write config"},
            {"label": "sessionRootName", "value": "主会话 <Root> & 审核"},
            {"label": "sessionName", "value": "子会话 <Child> & 执行"},
        ]

        html = render_html_default(event)

        assert "<h1>子会话 &lt;Child&gt;</h1>" in html
        assert "子会话 &lt;Child&gt; &amp; 执行" in html
        assert "主会话 &lt;Root&gt; &amp; 审核" in html
        assert html.index("会话名称") < html.index("所属主会话")
        assert html.count("所属主会话") == 1

    def test_legacy_card_without_root_name_does_not_add_hierarchy_row(self):
        event = _make_event(
            fields=[{"label": "sessionName", "value": "Legacy child session"}]
        )
        event.provider = "opencode"

        html = render_html_default(event)

        assert "会话名称" in html
        assert "Legacy child session" in html
        assert "所属主会话" not in html


class TestSubagentTimelineVisuals:
    def test_empty_and_legacy_events_do_not_render_timeline_sections(self):
        empty = _timeline_event([])
        assert _build_subagent_timeline_view(empty) is None
        assert render_subagent_timeline_html(empty) is None
        assert "子任务执行" not in render_html_default(empty)

        omp = _make_event()
        old_opencode = _make_event()
        old_opencode.provider = "opencode"
        old_opencode.event = "opencode.session_idle"
        old_opencode.session_scope = SessionScope.ROOT
        for event in (omp, old_opencode):
            assert render_subagent_timeline_html(event) is None
            assert "子任务执行" not in render_html_default(event)

    def test_simple_timeline_uses_compact_cards_with_status_depth_and_duration(self):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=1000, status="completed"),
                _timeline_item(1, start=1000, end=2500, status="failed"),
                _timeline_item(2, start=1000, end=1800, depth=2, status="running"),
                _timeline_item(3, start=2500, end=3000, status="cancelled"),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "simple"
        assert view["peak_concurrency"] == 2
        assert render_subagent_timeline_html(event) is None

        html = render_html_default(event)
        assert "子任务执行" in html
        assert html.count('<li class="subagent-item') == 4
        assert "depth-2" in html
        assert "并行" in html
        assert "已完成" in html
        assert "失败" in html
        assert "进行中" in html
        assert "已取消" in html
        assert "1 秒" in html
        assert "+1秒" in html
        assert "详细时间线见附图" not in html

    def test_timeline_identity_combines_agent_model_and_variant_with_safe_fallbacks(
        self,
    ):
        event = _timeline_event(
            [
                _timeline_item(
                    0,
                    start=0,
                    end=1000,
                    agent="agent",
                    model="model",
                    model_variant="high",
                ),
                _timeline_item(
                    1,
                    start=1000,
                    end=2000,
                    agent="agent",
                    model="model",
                    model_variant="default",
                ),
                _timeline_item(
                    2,
                    start=2000,
                    end=3000,
                    agent="agent",
                    model=None,
                    model_variant="xhigh",
                ),
                _timeline_item(
                    3,
                    start=3000,
                    end=4000,
                    agent=None,
                    model="model",
                    model_variant="xhigh",
                ),
                _timeline_item(
                    4,
                    start=4000,
                    end=5000,
                    agent=None,
                    model=None,
                    model_variant="xhigh",
                ),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "simple"
        assert [item["identity"] for item in view["main_items"]] == [
            "agent · model(high)",
            "agent · model",
            "agent",
            "model(xhigh)",
            "",
        ]

        html = render_html_default(event)
        assert "agent · model(high)" in html
        assert "agent · model" in html
        assert "model(xhigh)" in html
        assert "model(default)" not in html

    def test_complex_timeline_and_text_fallback_use_the_same_identity(self):
        event = _timeline_event(
            [
                _timeline_item(
                    index, start=0, end=2000, model="model", model_variant="xhigh"
                )
                for index in range(4)
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        assert view["gantt_items"][0]["identity"] == "worker · model(xhigh)"
        gantt = render_subagent_timeline_html(event)
        assert gantt is not None
        assert "worker · model(xhigh)" in gantt

        text = render_text_default(event)
        assert "child-0（worker · model(xhigh)）" in text

    def test_long_model_only_identity_contributes_to_gantt_row_height(self):
        model = "provider/" + "model-" + "m" * 113
        variant = "variant-" + "v" * 120
        event = _timeline_event(
            [
                _timeline_item(
                    index,
                    start=0,
                    end=2000,
                    agent=None,
                    model=model,
                    model_variant=variant,
                )
                for index in range(4)
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        assert view["layout"]["viewport_width"] == SUBAGENT_TIMELINE_MIN_VIEWPORT_WIDTH
        assert all(
            item["estimated_row_height"] > view["layout"]["row_min_height"]
            for item in view["gantt_items"]
        )

        html = render_subagent_timeline_html(event)
        assert html is not None
        assert f"{model}({variant})" in html
        assert "text-overflow: ellipsis" not in html
        assert "overflow-wrap: anywhere" in html

    def test_timeline_model_paths_are_filtered_from_safe_view(self):
        event = _timeline_event(
            [
                _timeline_item(
                    0, start=0, end=1000, model="/private/model", model_variant="high"
                )
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["main_items"][0]["model"] == ""
        assert view["main_items"][0]["identity"] == "worker"
        assert "/private/model" not in render_html_default(event)

    def test_auxiliary_smartfetch_is_excluded_from_visual_counts_and_names(self):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=1000, name="explorer"),
                _timeline_item(
                    1,
                    start=0,
                    end=500,
                    name="smartfetch-secondary",
                    agent="smartfetch-secondary",
                ),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["item_count"] == 1
        assert view["observed_count"] == 1
        html = render_html_default(event)
        assert "explorer" in html
        assert "smartfetch-secondary" not in html

    def test_complexity_is_not_driven_by_count_alone(self):
        high_concurrency = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)]
        )
        deep = _timeline_event(
            [
                _timeline_item(0, start=0, end=1000),
                _timeline_item(1, start=1000, end=2000, depth=3),
            ]
        )
        linear_eight = _timeline_event(
            [
                _timeline_item(index, start=index * 1000, end=(index + 1) * 1000)
                for index in range(8)
            ]
        )

        high_view = _build_subagent_timeline_view(high_concurrency)
        deep_view = _build_subagent_timeline_view(deep)
        linear_view = _build_subagent_timeline_view(linear_eight)
        assert high_view is not None and high_view["mode"] == "complex"
        assert deep_view is not None and deep_view["mode"] == "complex"
        assert linear_view is not None and linear_view["mode"] == "simple"
        assert render_subagent_timeline_html(high_concurrency) is not None
        assert render_subagent_timeline_html(deep) is not None
        assert render_subagent_timeline_html(linear_eight) is None

    def test_complex_main_copy_is_accurate_without_a_timeline_attachment(
        self, monkeypatch
    ):
        event = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)]
        )

        def fail_timeline_render(_event):
            raise RuntimeError("timeline render failed")

        monkeypatch.setattr(
            "core.renderer.render_subagent_timeline_html", fail_timeline_render
        )
        html = render_html_default(event)
        assert "流程较复杂，主卡仅展示关键摘要" in html
        assert "见附图" not in html
        assert "子任务" in html
        assert "峰值并发" in html
        assert "总任务时长" in html
        assert "子任务覆盖时长" in html
        assert "子任务覆盖率" in html

    def test_complete_timeline_reports_root_duration_coverage_union_and_rate(self):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=2000),
                _timeline_item(1, start=0, end=2000),
                _timeline_item(2, start=1500, end=3000),
                _timeline_item(3, start=3000, end=4000),
            ],
            total_duration_ms=5000,
            user_wait_timeline=_wait_timeline([]),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        assert view["total_duration_ms"] == 5000
        assert view["coverage_union_ms"] == 4000
        assert view["coverage_rate"] == pytest.approx(80)
        assert view["timing_summary"] == (
            "峰值并发 3 · 总任务时长 5 秒 · 子任务覆盖时长 4 秒 · 子任务覆盖率 80%"
        )
        assert [metric["label"] for metric in view["metrics"]] == [
            "子任务数",
            "峰值并发",
            "总任务时长",
            "子任务覆盖时长",
            "子任务覆盖率",
            "未分类时间 / 占比",
        ]
        assert [metric["value"] for metric in view["metrics"]] == [
            "4",
            "3",
            "5 秒",
            "4 秒",
            "80%",
            "1 秒（20%）",
        ]

        main_html = render_html_default(event)
        timeline_html = render_subagent_timeline_html(event)
        assert timeline_html is not None
        assert "已观测峰值并发" not in main_html
        assert "已观测子任务覆盖" not in main_html
        assert "峰值并发" in timeline_html
        assert "总任务时长" in timeline_html
        assert "子任务覆盖时长" in timeline_html
        assert "子任务覆盖率" in timeline_html

    def test_timeline_session_context_uses_normalized_field_and_expands_layout(self):
        items = [
            _timeline_item(index, start=index * 500, end=index * 500 + 1200)
            for index in range(8)
        ]
        baseline = render_subagent_timeline(_timeline_event(items))
        event = _timeline_event(items)
        event.fields = [
            {
                "label": "sessionName",
                "value": "修复 OpenCode Desktop webhook 会话标题回退",
            }
        ]

        rendered = render_subagent_timeline(event)

        assert baseline is not None and rendered is not None
        assert 'class="session-context"' in rendered.html
        assert "所属会话" in rendered.html
        assert "修复 OpenCode Desktop webhook 会话标题回退" in rendered.html
        assert (
            rendered.layout.vertical_chrome_height
            > baseline.layout.vertical_chrome_height
        )
        assert rendered.layout.estimated_height > baseline.layout.estimated_height
        assert rendered.layout.viewport_height > baseline.layout.viewport_height

    def test_timeline_session_context_prefers_root_name_over_child_name(self):
        event = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)]
        )
        event.fields = [
            {"label": "sessionName", "value": "子会话：实现 renderer"},
            {"label": "sessionRootName", "value": "主会话：Webhook 通知改进"},
        ]

        html = render_subagent_timeline_html(event)

        assert html is not None
        assert "主会话：Webhook 通知改进" in html
        assert "子会话：实现 renderer" not in html

    @pytest.mark.parametrize(
        "root_name",
        ["", "  \t ", "OpenCode Session a1b2c3d4e5f6"],
    )
    def test_timeline_session_context_falls_back_from_invalid_root_name(
        self, root_name
    ):
        event = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)]
        )
        event.fields = [
            {"label": "sessionRootName", "value": root_name},
            {"label": "sessionName", "value": "合法子会话名称"},
        ]

        html = render_subagent_timeline_html(event)

        assert html is not None
        assert "合法子会话名称" in html
        assert 'class="session-context"' in html

    def test_timeline_session_context_escapes_name(self):
        event = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)]
        )
        event.fields = [{"label": "sessionName", "value": "设计 <b>评审</b> & 确认"}]

        html = render_subagent_timeline_html(event)

        assert html is not None
        assert "设计 &lt;b&gt;评审&lt;/b&gt; &amp; 确认" in html
        assert "设计 <b>评审</b> & 确认" not in html

    @pytest.mark.parametrize(
        "fields",
        [
            [],
            [{"label": "sessionName", "value": "   \t  "}],
            [
                {
                    "label": "sessionName",
                    "value": "OpenCode Session a1b2c3d4e5f6",
                }
            ],
            [
                {
                    "label": "sessionName",
                    "value": "opencode session A1B2C3D4E5F6",
                }
            ],
            [
                {
                    "label": "sessionRootName",
                    "value": "OpenCode Session f1e2d3c4b5a6",
                },
                {"label": "sessionName", "value": "   "},
            ],
        ],
    )
    def test_hidden_timeline_session_context_preserves_existing_layout(self, fields):
        items = [
            _timeline_item(index, start=index * 500, end=index * 500 + 1200)
            for index in range(8)
        ]
        baseline = render_subagent_timeline(_timeline_event(items))
        event = _timeline_event(items)
        event.fields = fields
        event.raw = {"sessionName": "raw-only name must stay hidden"}

        rendered = render_subagent_timeline(event)

        assert baseline is not None and rendered is not None
        assert 'class="session-context"' not in rendered.html
        assert "所属会话" not in rendered.html
        assert "raw-only name must stay hidden" not in rendered.html
        assert rendered.layout.vertical_chrome_height == (
            baseline.layout.vertical_chrome_height
        )
        assert rendered.layout.estimated_height == baseline.layout.estimated_height
        assert rendered.layout.viewport_height == baseline.layout.viewport_height

    def test_long_timeline_session_name_uses_two_line_clamp(self):
        event = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)]
        )
        long_name = "很长的所属会话名称用于验证独立时间线卡片最多显示两行" * 12
        event.fields = [{"label": "sessionName", "value": long_name}]

        html = render_subagent_timeline_html(event)

        assert html is not None
        assert long_name in html
        assert 'class="session-copy"' in html
        assert "-webkit-line-clamp: 2;" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert "max-height: 46.4px;" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert "overflow-wrap: anywhere;" in SUBAGENT_TIMELINE_HTML_TEMPLATE

    @pytest.mark.parametrize(
        ("items", "total_duration_ms", "expected_union", "expected_rate"),
        [
            (
                [
                    _timeline_item(0, start=0, end=4000),
                    _timeline_item(1, start=0, end=4000),
                    _timeline_item(2, start=0, end=4000),
                    _timeline_item(3, start=0, end=4000),
                ],
                4000,
                4000,
                100,
            ),
            (
                [
                    _timeline_item(0, start=0, end=2000),
                    _timeline_item(1, start=1500, end=3000),
                    _timeline_item(2, start=3000, end=3500),
                    _timeline_item(3, start=4500, end=6000),
                ],
                5000,
                4000,
                80,
            ),
            (
                [
                    _timeline_item(0, start=0, end=1000),
                    _timeline_item(1, start=1000, end=2000),
                    _timeline_item(2, start=2000, end=3000),
                    _timeline_item(3, start=3000, end=4000),
                ],
                4000,
                4000,
                100,
            ),
        ],
    )
    def test_coverage_merges_overlap_and_adjacent_intervals_and_clips_bounds(
        self, items, total_duration_ms, expected_union, expected_rate
    ):
        view = _build_subagent_timeline_view(
            _timeline_event(items, total_duration_ms=total_duration_ms)
        )
        assert view is not None
        assert view["coverage_union_ms"] == expected_union
        assert view["coverage_rate"] == pytest.approx(expected_rate)
        assert view["coverage_rate"] <= 100

    @pytest.mark.parametrize("total_duration_ms", [None, 0])
    def test_missing_or_zero_total_duration_uses_safe_unavailable_rate(
        self, total_duration_ms
    ):
        event = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)],
            total_duration_ms=total_duration_ms,
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        expected_union = None if total_duration_ms is None else 0
        assert view["coverage_union_ms"] == expected_union
        assert view["coverage_rate"] is None
        values = {metric["label"]: metric["value"] for metric in view["metrics"]}
        assert values["总任务时长"] == (
            "不可用" if total_duration_ms is None else "0 毫秒"
        )
        assert values["子任务覆盖时长"] == (
            "不可用" if total_duration_ms is None else "0 毫秒"
        )
        assert values["子任务覆盖率"] == "不可用"
        if total_duration_ms == 0:
            assert view["timeline_end_ms"] == 0
            assert [tick["label"] for tick in view["axis_ticks"]] == ["0"]

    def test_empty_timeline_stays_hidden_and_has_no_inferred_root_row(self):
        event = _timeline_event([], total_duration_ms=5000)
        coverage_union_ms = _timeline_coverage_union_ms([], 5000)
        assert coverage_union_ms == 0
        assert _timeline_coverage_rate(coverage_union_ms, 5000) == 0
        assert _build_subagent_timeline_view(event) is None
        assert render_subagent_timeline_html(event) is None

    def test_only_user_wait_without_subagents_renders_unified_root_timeline(self):
        event = _timeline_event(
            [],
            total_duration_ms=5000,
            user_wait_timeline=_wait_timeline([_wait_interval(start=500, end=1700)]),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["mode"] == "complex"
        assert view["item_count"] == 0
        assert view["wait_complete_count"] == 1
        assert view["wait_union_ms"] == 1200
        assert view["timeline_end_ms"] == 5000
        assert view["timeline_end_source"] == "root_duration"

        main_html = render_html_default(event)
        timeline_html = render_subagent_timeline_html(event)
        assert timeline_html is not None
        assert "根任务时间线" in main_html
        assert "等待用户 1.2 秒 · 1 次" in main_html
        assert "根任务执行时间线" in timeline_html
        assert timeline_html.count('class="row wait-row"') == 1
        assert 'class="row timeline-row"' not in timeline_html

    def test_root_duration_axis_preserves_visible_tail_after_all_intervals_end(self):
        event = _timeline_event(
            [_timeline_item(0, start=0, end=4000, depth=3)],
            total_duration_ms=10_000,
            user_wait_timeline=_wait_timeline([_wait_interval(start=1000, end=2000)]),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        assert view["timeline_end_ms"] == 10_000
        assert view["timeline_end_source"] == "root_duration"
        subagent = view["gantt_items"][0]
        wait = view["wait_intervals"][0]
        plot_width = view["layout"]["plot_width"]
        assert float(subagent["width_px"]) == pytest.approx(plot_width * 0.4)
        assert float(wait["left_px"]) == pytest.approx(plot_width * 0.1)
        assert float(wait["width_px"]) == pytest.approx(plot_width * 0.1)
        assert float(subagent["left_px"]) + float(subagent["width_px"]) < plot_width

    def test_question_permission_and_censored_waits_keep_visual_and_stats_semantics(
        self,
    ):
        wait = _wait_timeline(
            [
                _wait_interval(kind="question", start=100, end=500),
                _wait_interval(
                    kind="permission", start=400, end=900, result="rejected"
                ),
                _wait_interval(
                    kind="question", state="right_censored", start=1200, end=None
                ),
                _wait_interval(
                    kind="permission", state="left_censored", start=None, end=80
                ),
            ],
            partial=True,
            partial_reasons=["open_at_cycle_end", "orphan_resolution"],
        )
        event = _timeline_event(
            [_timeline_item(index, start=0, end=1000) for index in range(4)],
            total_duration_ms=2000,
            user_wait_timeline=wait,
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        assert view["wait_union_ms"] == 800
        assert view["wait_complete_count"] == 2
        assert view["wait_rate"] == pytest.approx(40)
        assert view["wait_summary_text"] == "等待用户至少 800 毫秒 · 至少 2 次"
        assert "Question 1" in view["wait_summary_detail"]
        assert "Permission 1" in view["wait_summary_detail"]
        assert "2 个边界区间未计时" in view["wait_summary_detail"]

        visual = view["wait_intervals"]
        assert {item["kind"] for item in visual} == {"question", "permission"}
        censored = [item for item in visual if not item["counted_duration"]]
        assert {item["state_class"] for item in censored} == {
            "right-censored",
            "left-censored",
        }
        assert all(item["timing_label"] == "未计入时长" for item in censored)
        assert all(float(item["width_px"]) > 0 for item in censored)

        html = render_subagent_timeline_html(event)
        assert html is not None
        assert "wait-bar question complete" in html
        assert "wait-bar permission complete" in html
        assert "wait-bar question right-censored" in html
        assert "wait-bar permission left-censored" in html
        assert html.count('data-counted-duration="false"') == 2

    def test_partial_wait_reasons_use_concise_user_copy_even_when_censored_hidden(self):
        event = _timeline_event(
            [],
            total_duration_ms=3000,
            user_wait_timeline=_wait_timeline(
                [_wait_interval(start=100, end=500)],
                partial=True,
                partial_reasons=[
                    "open_at_cycle_end",
                    "orphan_resolution",
                    "missing_request_id",
                    "evicted",
                    "truncated",
                    "clock_invalid",
                ],
                truncated=True,
                observed_count=4,
            ),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["wait_summary_text"] == "等待用户至少 400 毫秒 · 至少 1 次"
        assert "结束时仍在等待" in view["wait_summary_detail"]
        assert "仅观测到等待结束" in view["wait_summary_detail"]
        assert "部分等待无法关联" in view["wait_summary_detail"]
        assert "部分记录未保留" in view["wait_summary_detail"]
        assert "等待记录已截断" in view["wait_summary_detail"]
        assert "时间信息不完整" in view["wait_summary_detail"]
        html = render_subagent_timeline_html(event)
        assert html is not None
        assert "evicted" not in html
        assert "clock_invalid" not in html
        assert "open_at_cycle_end" not in html
        assert "orphan_resolution" not in html
        assert "missing_request_id" not in html

    def test_reliable_empty_wait_is_zero_but_none_remains_unavailable(self):
        reliable_empty = _timeline_event(
            [],
            total_duration_ms=5000,
            user_wait_timeline=_wait_timeline([]),
        )
        empty_view = _build_subagent_timeline_view(reliable_empty)
        assert empty_view is not None
        assert empty_view["mode"] == "summary"
        assert empty_view["wait_union_ms"] == 0
        assert empty_view["wait_summary_text"] == "等待用户 0 毫秒 · 0 次"
        assert render_subagent_timeline_html(reliable_empty) is None
        assert "等待用户 0 毫秒 · 0 次" in render_html_default(reliable_empty)

        unavailable = _timeline_event([], total_duration_ms=5000)
        assert _build_subagent_timeline_view(unavailable) is None

        complex_unavailable = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)],
            total_duration_ms=5000,
        )
        unavailable_view = _build_subagent_timeline_view(complex_unavailable)
        assert unavailable_view is not None
        assert unavailable_view["wait_union_ms"] is None
        assert unavailable_view["wait_summary_text"] == "等待用户：不可用"
        assert unavailable_view["metrics"][5] == {
            "value": "至多 3 秒（60%）",
            "label": "未分类时间 / 占比 · 观测受限",
        }
        unavailable_html = render_subagent_timeline_html(complex_unavailable)
        assert unavailable_html is not None
        assert "不可推断为 0" in unavailable_html

    def test_wait_track_does_not_change_subagent_count_peak_or_coverage(self):
        items = [
            _timeline_item(0, start=0, end=2000),
            _timeline_item(1, start=500, end=2500),
            _timeline_item(2, start=2500, end=3000),
            _timeline_item(3, start=3000, end=4000),
        ]
        without_wait = _build_subagent_timeline_view(
            _timeline_event(items, total_duration_ms=5000)
        )
        with_wait = _build_subagent_timeline_view(
            _timeline_event(
                items,
                total_duration_ms=5000,
                user_wait_timeline=_wait_timeline(
                    [
                        _wait_interval(kind="question", start=200, end=1800),
                        _wait_interval(kind="permission", start=1500, end=3500),
                    ]
                ),
            )
        )
        assert without_wait is not None and with_wait is not None
        for key in (
            "item_count",
            "observed_count",
            "peak_concurrency",
            "reliable_peak_concurrency",
            "coverage_union_ms",
            "coverage_rate",
        ):
            assert with_wait[key] == without_wait[key]
        assert [metric["value"] for metric in with_wait["metrics"][:5]] == [
            metric["value"] for metric in without_wait["metrics"][:5]
        ]

    @pytest.mark.parametrize(
        ("wait_intervals", "expected_known", "expected_unclassified", "expected_rate"),
        [
            (
                [_wait_interval(start=3000, end=4000)],
                4000,
                6000,
                60,
            ),
            (
                [_wait_interval(start=1500, end=3500)],
                4500,
                5500,
                55,
            ),
        ],
    )
    def test_unclassified_uses_union_across_subagent_and_wait_without_double_counting(
        self,
        wait_intervals,
        expected_known,
        expected_unclassified,
        expected_rate,
    ):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=2000),
                _timeline_item(1, start=6000, end=7000),
            ],
            total_duration_ms=10_000,
            user_wait_timeline=_wait_timeline(wait_intervals),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["known_union_ms"] == expected_known
        assert view["unclassified_ms"] == expected_unclassified
        assert view["unclassified_rate"] == pytest.approx(expected_rate)
        assert view["unclassified_observation_limited"] is False
        metric = view["metrics"][5]
        assert metric["label"] == "未分类时间 / 占比"
        assert metric["value"] == (
            f"{format_duration_ms(expected_unclassified)}（{expected_rate}%）"
        )

    def test_unclassified_excludes_censored_and_uses_upper_bound_when_limited(self):
        event = _timeline_event(
            [_timeline_item(0, start=0, end=2000)],
            total_duration_ms=10_000,
            user_wait_timeline=_wait_timeline(
                [
                    _wait_interval(start=3000, end=4000),
                    _wait_interval(state="right_censored", start=5000, end=None),
                ],
                partial=True,
                partial_reasons=["open_at_cycle_end"],
            ),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["known_union_ms"] == 3000
        assert view["unclassified_ms"] == 7000
        assert view["unclassified_rate"] == pytest.approx(70)
        assert view["unclassified_observation_limited"] is True
        assert view["metrics"][5] == {
            "value": "至多 7 秒（70%）",
            "label": "未分类时间 / 占比 · 观测受限",
        }

    @pytest.mark.parametrize("total_duration_ms", [None, 0])
    def test_unclassified_is_not_calculated_without_positive_reliable_duration(
        self, total_duration_ms
    ):
        event = _timeline_event(
            [_timeline_item(0, start=0, end=1000)],
            total_duration_ms=total_duration_ms,
            user_wait_timeline=_wait_timeline([_wait_interval(start=1200, end=1800)]),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["known_union_ms"] is None
        assert view["unclassified_ms"] is None
        assert view["unclassified_rate"] is None
        assert view["metrics"][5] == {
            "value": "不可计算",
            "label": "未分类时间 / 占比",
        }

    def test_complete_wait_axis_clipping_counts_only_intersections_and_marks_limited(
        self,
    ):
        event = _timeline_event(
            [],
            total_duration_ms=1000,
            user_wait_timeline=_wait_timeline(
                [
                    _wait_interval(kind="question", start=-500, end=-100),
                    _wait_interval(kind="permission", start=1200, end=1600),
                    _wait_interval(kind="question", start=-200, end=200),
                    _wait_interval(kind="permission", start=800, end=1200),
                ]
            ),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["wait_interval_clipped"] is True
        assert view["wait_observation_limited"] is True
        assert view["wait_complete_count"] == 2
        assert view["wait_union_ms"] == 400
        assert len(view["wait_intervals"]) == 2
        assert view["wait_summary_text"] == "等待用户至少 400 毫秒 · 至少 2 次"
        assert "按根任务边界裁剪" in view["wait_summary_detail"]
        assert "Question 1" in view["wait_summary_detail"]
        assert "Permission 1" in view["wait_summary_detail"]
        assert all(
            "按根任务边界裁剪" in item["title"] for item in view["wait_intervals"]
        )
        assert view["known_union_ms"] == 400
        assert view["metrics"][5]["value"] == "至多 600 毫秒（60%）"

    def test_reliable_empty_timelines_make_whole_positive_root_duration_unclassified(
        self,
    ):
        event = _timeline_event(
            [],
            total_duration_ms=5000,
            user_wait_timeline=_wait_timeline([]),
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["known_union_ms"] == 0
        assert view["unclassified_ms"] == 5000
        assert view["unclassified_rate"] == pytest.approx(100)
        assert view["unclassified_observation_limited"] is False
        assert view["metrics"][5]["value"] == "5 秒（100%）"

    def test_only_wait_with_missing_subagent_timeline_has_upper_bound_and_text_summary(
        self,
    ):
        event = _timeline_event(
            [],
            total_duration_ms=5000,
            user_wait_timeline=_wait_timeline([_wait_interval(start=1000, end=2000)]),
        )
        event.subagent_timeline = None
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["wait_union_ms"] == 1000
        assert view["known_union_ms"] == 1000
        assert view["unclassified_ms"] == 4000
        assert view["unclassified_observation_limited"] is True
        assert view["metrics"][5]["value"] == "至多 4 秒（80%）"

        text = render_text_default(event)
        assert "根任务时间线：" in text
        assert "等待用户 1 秒 · 1 次" in text
        assert "未分类时间 / 占比 · 观测受限：至多 4 秒（80%）" in text

    def test_coverage_union_ignores_invalid_intervals_defensively(self):
        coverage_union_ms = _timeline_coverage_union_ms(
            [
                (-1, 1000),
                (2000, 1000),
                (float("nan"), 3000),
                (3000, float("inf")),
                (1000, 2500),
            ],
            5000,
        )
        assert coverage_union_ms == 1500

    @pytest.mark.parametrize("truncated", [False, True])
    def test_partial_or_truncated_timeline_uses_observed_metric_labels(self, truncated):
        items = [
            _timeline_item(index, start=0, end=2000, depth=3 if index == 0 else 1)
            for index in range(4)
        ]
        reasons = ["truncated"] if truncated else ["clamped"]
        if not truncated:
            items[0] = _timeline_item(
                0,
                start=0,
                end=2000,
                depth=3,
                timing_quality="partial",
            )
        event = _timeline_event(
            items,
            partial=True,
            partial_reasons=reasons,
            truncated=truncated,
            observed_count=6 if truncated else None,
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        assert "已观测峰值并发" in view["timing_summary"]
        assert "总任务时长" in view["timing_summary"]
        assert "已观测子任务覆盖时长" in view["timing_summary"]
        assert "已观测子任务覆盖率" in view["timing_summary"]
        assert [metric["label"] for metric in view["metrics"]] == [
            "子任务数",
            "已观测峰值并发",
            "总任务时长",
            "已观测子任务覆盖时长",
            "已观测子任务覆盖率",
            "未分类时间 / 占比 · 观测受限",
        ]

        main_html = render_html_default(event)
        timeline_html = render_subagent_timeline_html(event)
        assert timeline_html is not None
        for html in (main_html, timeline_html):
            assert "已观测峰值并发" in html
            assert "已观测子任务覆盖时长" in html
            assert "已观测子任务覆盖率" in html
        assert "指标按已观测范围计算" in timeline_html

    def test_unknown_or_partial_intervals_are_excluded_from_observed_coverage(self):
        items = [
            _timeline_item(0, start=0, end=1000),
            _timeline_item(1, start=1000, end=2000, timing_quality="fallback"),
            _timeline_item(2, start=2000, end=5000, timing_quality="partial"),
            _timeline_item(3),
        ]
        event = _timeline_event(
            items,
            partial=True,
            partial_reasons=["missing_start", "missing_end", "clamped"],
            total_duration_ms=4000,
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["coverage_union_ms"] == 2000
        assert view["coverage_rate"] == pytest.approx(50)
        assert "已观测子任务覆盖时长 2 秒" in view["timing_summary"]
        assert "已观测子任务覆盖率 50%" in view["timing_summary"]

    def test_missing_ratio_over_limit_degrades_and_boundary_can_render_gantt(self):
        degraded_items = [
            _timeline_item(index, start=index * 1000, end=(index + 1) * 1000)
            for index in range(3)
        ] + [_timeline_item(3), _timeline_item(4)]
        degraded = _timeline_event(
            degraded_items,
            partial=True,
            partial_reasons=["missing_start", "missing_end"],
        )
        degraded_view = _build_subagent_timeline_view(degraded)
        assert degraded_view is not None
        assert degraded_view["missing_ratio"] == pytest.approx(0.4)
        assert degraded_view["mode"] == "degraded"
        assert render_subagent_timeline_html(degraded) is None
        degraded_html = render_html_default(degraded)
        assert "部分时间数据缺失" in degraded_html
        assert "详细时间线见附图" not in degraded_html

        boundary_items = [
            _timeline_item(0, start=0, end=4000),
            _timeline_item(1, start=1000, end=2000, depth=3, status="failed"),
            _timeline_item(2, start=2000, end=3000),
            _timeline_item(3),
        ]
        boundary = _timeline_event(
            boundary_items,
            partial=True,
            partial_reasons=["missing_start", "missing_end"],
        )
        boundary_view = _build_subagent_timeline_view(boundary)
        assert boundary_view is not None
        assert boundary_view["missing_ratio"] == pytest.approx(0.25)
        assert boundary_view["mode"] == "complex"
        gantt = render_subagent_timeline_html(boundary)
        assert gantt is not None
        failed_item = next(
            item
            for item in boundary_view["gantt_items"]
            if item["status_class"] == "failed"
        )
        assert float(failed_item["left_px"]) > 0
        assert float(failed_item["width_px"]) > SUBAGENT_TIMELINE_MIN_BAR_WIDTH
        assert f"left: {failed_item['left_px']}px;" in gantt
        assert "bar failed" in gantt
        assert "峰值并发" in gantt
        assert "未定位任务" in gantt

    def test_main_card_detail_is_bounded_to_eight_items(self):
        items = [
            _timeline_item(index, start=index * 1000, end=(index + 1) * 1000)
            for index in range(7)
        ] + [_timeline_item(index) for index in range(7, 10)]
        event = _timeline_event(
            items,
            partial=True,
            partial_reasons=["missing_start", "missing_end"],
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "degraded"
        assert len(view["main_items"]) == SUBAGENT_TIMELINE_MAIN_ITEM_LIMIT
        assert view["main_hidden_count"] == 2

        html = render_html_default(event)
        assert html.count('<li class="subagent-item') == 8
        assert "另有 2 项未展开" in html

    def test_gantt_shows_all_payload_items_without_twenty_four_row_truncation(self):
        items = [
            _timeline_item(index, start=index * 10, end=10_000) for index in range(29)
        ]
        items.append(_timeline_item(29, name="unlocated-child"))
        event = _timeline_event(
            items,
            partial=True,
            partial_reasons=["missing_start", "missing_end", "truncated"],
            truncated=True,
            observed_count=36,
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        assert SUBAGENT_TIMELINE_MAX_ITEMS == 64
        assert len(view["gantt_items"]) + len(view["unlocated_items"]) == 30
        assert view["layout"]["density"] == "compact"

        html = render_subagent_timeline_html(event)
        assert html is not None
        assert html.count('<div class="row timeline-row"') == 29
        assert "unlocated-child" in html
        assert "其余 12 项未展开" not in html
        assert "观测 36 个 · 本图展示 30 个" in html
        assert "记录已截断" in html
        assert "10秒" in html
        assert "bar completed" in html
        assert "最大" not in html

    @pytest.mark.parametrize(
        ("item_count", "density"),
        [
            (1, "comfortable"),
            (24, "comfortable"),
            (25, "compact"),
            (48, "compact"),
            (49, "dense"),
            (64, "dense"),
        ],
    )
    def test_gantt_density_boundaries(self, item_count, density):
        event = _timeline_event(
            [
                _timeline_item(index, start=index * 1000, end=(index + 1) * 1000)
                for index in range(item_count)
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        assert view["layout"]["density"] == density
        assert len(view["gantt_items"]) == item_count

    def test_gantt_template_uses_light_palette_tokens_and_semantic_status_styles(self):
        palette = {
            "timeline-page": "#eef2f6",
            "timeline-shell": "#f8fafc",
            "timeline-axis": "#e9eff6",
            "timeline-row-odd": "#ffffff",
            "timeline-row-even": "#f4f7fa",
            "timeline-border": "#cbd5e1",
            "timeline-text": "#243247",
            "timeline-muted": "#607086",
            "timeline-completed": "#6fae88",
            "timeline-failed": "#d9878d",
            "timeline-running": "#d8ad55",
            "timeline-unknown": "#7f9dbe",
        }
        for token, value in palette.items():
            assert f"--{token}: {value};" in SUBAGENT_TIMELINE_HTML_TEMPLATE

        assert "background: var(--timeline-page);" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert "background: var(--timeline-shell);" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert ".timeline-row:nth-child(odd) .track" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert ".timeline-row:nth-child(even) .track" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert ".bar.completed" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert ".bar.failed" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert ".bar.running" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert ".bar.unknown" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert ".bar.partial" in SUBAGENT_TIMELINE_HTML_TEMPLATE
        assert "#0d1420" not in SUBAGENT_TIMELINE_HTML_TEMPLATE.lower()
        assert "#111925" not in SUBAGENT_TIMELINE_HTML_TEMPLATE.lower()

    def test_gantt_dynamic_width_reaches_both_bounds(self):
        minimum = _timeline_event([_timeline_item(0, start=0, end=1000, name="短任务")])
        minimum_view = _build_subagent_timeline_view(minimum)
        assert minimum_view is not None
        assert (
            minimum_view["layout"]["viewport_width"]
            == SUBAGENT_TIMELINE_MIN_VIEWPORT_WIDTH
        )

        long_name = "相同前缀任务" * 30
        maximum = _timeline_event(
            [
                _timeline_item(
                    index,
                    start=index * 60_000,
                    end=5_400_000,
                    name=f"{long_name}-{index:02d}",
                )
                for index in range(64)
            ]
        )
        maximum_view = _build_subagent_timeline_view(maximum)
        assert maximum_view is not None
        assert (
            maximum_view["layout"]["viewport_width"]
            == SUBAGENT_TIMELINE_MAX_VIEWPORT_WIDTH
        )
        assert maximum_view["layout"]["name_column_width"] == 560
        assert maximum_view["layout"]["plot_width"] > 1500
        assert maximum_view["layout"]["estimated_height"] > 3000
        assert maximum_view["layout"]["soft_height_exceeded"] is True
        assert (
            maximum_view["layout"]["viewport_height"]
            >= maximum_view["layout"]["estimated_height"]
        )
        assert len(maximum_view["gantt_items"]) == 64

    def test_twenty_four_to_twenty_five_keeps_every_item_and_stable_order(self):
        for item_count, expected_density in ((24, "comfortable"), (25, "compact")):
            event = _timeline_event(
                [
                    _timeline_item(
                        index,
                        start=index * 1000,
                        end=(index + 1) * 1000,
                        name=f"ordered-{index:02d}",
                    )
                    for index in range(item_count)
                ]
            )
            view = _build_subagent_timeline_view(event)
            assert view is not None
            assert view["layout"]["density"] == expected_density
            assert [item["name"] for item in view["gantt_items"]] == [
                f"ordered-{index:02d}" for index in range(item_count)
            ]

    def test_sixty_four_items_all_render_in_stable_order(self):
        event = _timeline_event(
            [
                _timeline_item(
                    index,
                    start=index * 1000,
                    end=(index + 1) * 1000,
                    name=f"all-items-{index:02d}",
                )
                for index in range(64)
            ]
        )
        view = _build_subagent_timeline_view(event)
        html = render_subagent_timeline_html(event)
        assert view is not None and html is not None
        assert len(view["gantt_items"]) == 64
        assert html.count('<div class="row timeline-row"') == 64
        positions = [html.index(f"all-items-{index:02d}") for index in range(64)]
        assert positions == sorted(positions)

    def test_long_names_wrap_fully_without_ellipsis_and_escape_dangerous_text(self):
        chinese = "多个相同前缀的中文子任务需要显示完整差异" * 5
        english = "UnbrokenEnglishTaskName" * 8
        dangerous = '<script>alert("x")</script>&final'
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=3000, name=chinese),
                _timeline_item(1, start=0, end=2000, name=english),
                _timeline_item(2, start=1000, end=3000, depth=3, name=dangerous),
            ]
        )
        html = render_subagent_timeline_html(event)
        assert html is not None
        assert chinese in html
        assert english in html
        assert "&lt;script&gt;alert" in html
        assert '<script>alert("x")</script>' not in html
        assert "overflow-wrap: anywhere" in html
        assert ".task-name {\n      white-space: normal;" in html
        assert ".task-name, .task-agent" not in html

    def test_extremely_short_nonzero_bar_is_at_least_eight_pixels(self):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=1, name="one-millisecond"),
                _timeline_item(1, start=0, end=3_600_000, depth=3, name="one-hour"),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        short = next(
            item for item in view["gantt_items"] if item["name"] == "one-millisecond"
        )
        assert float(short["width_px"]) == SUBAGENT_TIMELINE_MIN_BAR_WIDTH
        assert short["minimum_width_applied"] is True

    def test_tick_count_and_labels_follow_plot_width_and_duration(self):
        short = _timeline_event(
            [
                _timeline_item(0, start=0, end=600_000),
                _timeline_item(1, start=0, end=300_000, depth=3),
            ]
        )
        long = _timeline_event(
            [
                _timeline_item(index, start=0, end=5_400_000, name="宽布局任务" * 20)
                for index in range(49)
            ]
        )
        short_view = _build_subagent_timeline_view(short)
        long_view = _build_subagent_timeline_view(long)
        assert short_view is not None and long_view is not None
        _assert_tick_layout(short_view)
        _assert_tick_layout(long_view)
        assert short_view["axis_ticks"] != long_view["axis_ticks"]
        assert all("left_px" in tick for tick in long_view["axis_ticks"])
        assert long_view["axis_ticks"][0]["label"] == "0"
        assert long_view["axis_ticks"][-1]["label"].startswith("+")

    def test_one_hour_plus_one_millisecond_ticks_are_unique_and_separated(self):
        span = 3_600_001
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=span),
                _timeline_item(1, start=0, end=span // 2, depth=3),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        _assert_tick_layout(view)
        ticks = view["axis_ticks"]
        assert ticks[-1]["label"] == "+1小时"
        assert span - ticks[-1]["value_ms"] <= 1

    def test_thirty_day_ticks_cover_the_whole_span_evenly(self):
        span = 30 * 24 * 60 * 60 * 1000
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=span),
                _timeline_item(1, start=0, end=span // 2, depth=3),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        _assert_tick_layout(view)
        ticks = view["axis_ticks"]
        values = [float(tick["value_ms"]) for tick in ticks]
        positions = [float(tick["left_px"]) for tick in ticks]
        assert values[0] == 0
        assert values[-1] >= 25 * 86_400_000
        assert view["layout"]["plot_width"] - positions[-1] < 300

    @pytest.mark.parametrize(
        "span",
        [
            1,
            2,
            3,
            999,
            1000,
            1001,
            10_000,
            59_999,
            60_000,
            60_001,
            3_599_999,
            3_600_000,
            3_600_001,
            86_399_999,
            86_400_000,
            86_400_001,
            26 * 86_400_000 + 2 * 3_600_000 + 56 * 60_000,
            30 * 86_400_000,
        ],
    )
    def test_tick_algorithm_handles_short_normal_and_long_spans(self, span):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=span),
                _timeline_item(1, start=0, end=max(1, span // 2), depth=3),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        _assert_tick_layout(view)
        ticks = view["axis_ticks"]
        assert ticks[0]["value_ms"] == 0
        assert ticks[-1]["value_ms"] <= span

    @pytest.mark.parametrize("span", [2, 3])
    def test_millisecond_ticks_preserve_fractional_precision(self, span):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=span),
                _timeline_item(1, start=0, end=max(1, span // 2), depth=3),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        _assert_tick_layout(view)
        labels = [tick["label"] for tick in view["axis_ticks"]]
        assert any("." in label for label in labels)

    @pytest.mark.parametrize("span", [1, 10, 10_000, 10_001])
    def test_short_tick_labels_preserve_their_exact_numeric_values(self, span):
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=span),
                _timeline_item(1, start=0, end=max(1, span // 2), depth=3),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        _assert_tick_layout(view)
        ticks = view["axis_ticks"]
        for tick in ticks:
            assert _parse_short_tick_label_ms(tick["label"]) == pytest.approx(
                float(tick["value_ms"]), abs=1e-9
            )

        labels = {tick["label"] for tick in ticks}
        if span == 1:
            assert {"+0.25毫秒", "+0.75毫秒"} <= labels
        elif span == 10:
            assert {"+2.5毫秒", "+7.5毫秒"} <= labels
        elif span == 10_000:
            assert {"+2.5秒", "+7.5秒"} <= labels
        else:
            assert ticks[-1]["value_ms"] == span
            assert ticks[-1]["label"] == "+10.001秒"

    def test_twenty_six_day_boundary_avoids_long_terminal_label_overlap(self):
        span = 26 * 86_400_000 + 2 * 3_600_000 + 56 * 60_000
        event = _timeline_event(
            [
                _timeline_item(0, start=0, end=span),
                _timeline_item(1, start=0, end=span // 2, depth=3),
            ]
        )
        view = _build_subagent_timeline_view(event)
        assert view is not None
        _assert_tick_layout(view)
        ticks = view["axis_ticks"]
        assert ticks[-1]["value_ms"] <= span
        assert span - ticks[-1]["value_ms"] <= 5 * 86_400_000

    def test_tick_geometry_across_nice_step_boundaries(self):
        for unit in (1, 1000, 60_000, 3_600_000, 86_400_000):
            for factor in (1, 2, 2.5, 5, 10):
                base = unit * factor
                for delta in (-1, 0, 1):
                    span = max(1, int(base + delta))
                    event = _timeline_event(
                        [
                            _timeline_item(0, start=0, end=span),
                            _timeline_item(
                                1,
                                start=0,
                                end=max(1, span // 2),
                                depth=3,
                            ),
                        ]
                    )
                    view = _build_subagent_timeline_view(event)
                    assert view is not None
                    _assert_tick_layout(view)

    def test_dom_rows_use_dynamic_heights_and_keep_complete_wrapped_names(self):
        names = [
            "长中文任务名称需要完整折行并保留末尾差异" * 12 + "甲",
            "Long English task name with spaces must remain complete " * 12 + "beta",
            "NoSpaceUnbrokenTaskIdentifier" * 24 + "omega",
        ]
        event = _timeline_event(
            [
                _timeline_item(
                    index,
                    start=0,
                    end=3000 - index * 500,
                    depth=3 if index == 2 else 1,
                    name=name,
                )
                for index, name in enumerate(names)
            ]
        )
        rendered = render_subagent_timeline(event)
        assert rendered is not None
        view = _build_subagent_timeline_view(event)
        assert view is not None
        probe = _TimelineDOMProbe()
        probe.feed(rendered.html)

        expected_heights = [
            item["estimated_row_height"] for item in view["gantt_items"]
        ]
        assert probe.task_names == names
        assert probe.row_min_heights == expected_heights
        assert all(item["estimated_name_lines"] > 1 for item in view["gantt_items"])
        assert all(
            height > rendered.layout.row_min_height for height in expected_heights
        )
        assert rendered.layout.estimated_height == (
            rendered.layout.vertical_chrome_height + rendered.layout.located_rows_height
        )
        assert rendered.layout.viewport_height >= rendered.layout.estimated_height

    def test_prepared_timeline_reuses_one_view_for_main_and_attachment(
        self, monkeypatch
    ):
        event = _timeline_event(
            [_timeline_item(index, start=0, end=2000) for index in range(4)]
        )
        from core import renderer

        original = renderer._build_subagent_timeline_view
        calls = 0

        def counted(current_event):
            nonlocal calls
            calls += 1
            return original(current_event)

        monkeypatch.setattr(renderer, "_build_subagent_timeline_view", counted)
        prepared = prepare_subagent_timeline(event)
        main = render_html_data(event, prepared_timeline=prepared)
        attachment = render_subagent_timeline(event, prepared)
        assert main["event"]["subagent_timeline_view"]["mode"] == "complex"
        assert attachment is not None
        assert calls == 1

    def test_partial_clamped_interval_uses_pattern_without_exact_duration(self):
        items = [
            _timeline_item(0, start=0, end=4000),
            _timeline_item(
                1,
                start=1000,
                end=3000,
                depth=3,
                timing_quality="partial",
                name="clamped-child",
            ),
            _timeline_item(2, start=3000, end=4000),
        ]
        event = _timeline_event(items, partial=True, partial_reasons=["clamped"])
        view = _build_subagent_timeline_view(event)
        assert view is not None and view["mode"] == "complex"
        clamped = next(
            item for item in view["gantt_items"] if item["name"] == "clamped-child"
        )
        assert clamped["partial_interval"] is True
        assert clamped["timing_label"] == "区间不完整"
        assert "2 秒" not in clamped["meta_labels"]

        html = render_subagent_timeline_html(event)
        assert html is not None
        assert "bar completed partial" in html
        assert "区间不完整" in html

    def test_timeline_html_escapes_names_and_never_surfaces_sensitive_structure(self):
        ref = "a" * 32
        path = "/Users/alice/private/task.txt"
        items = [
            _timeline_item(
                0,
                start=0,
                end=3000,
                depth=3,
                name="<b>unsafe child</b>",
                agent="worker<&>",
            ),
            _timeline_item(1, start=1000, end=2000, name=path),
            _timeline_item(2, start=2000, end=3000, name=ref, agent='{"raw":1}'),
        ]
        event = _timeline_event(items)
        html = render_subagent_timeline_html(event)
        assert html is not None
        assert "&lt;b&gt;unsafe child&lt;/b&gt;" in html
        assert "worker&lt;&amp;&gt;" in html
        assert "<b>unsafe child</b>" not in html
        assert path not in html
        assert ref not in html
        assert '{"raw":1}' not in html
        assert "未命名子任务" in html
        for forbidden in (
            "parentRef",
            "rootRef",
            "startOffsetMs",
            "endOffsetMs",
            "durationMs",
        ):
            assert forbidden not in html


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
