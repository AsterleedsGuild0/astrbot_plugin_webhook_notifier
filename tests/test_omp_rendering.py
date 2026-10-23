"""OMP 共用展示的区间语义与主通知保留；浏览器视觉验收另记。"""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from core.models import ServerConfig
from core.renderer import prepare_subagent_timeline, render_html_default, render_subagent_timeline, render_text_default
from core.server import WebhookServer
from tests.test_omp_timeline import timeline_envelope
from tests.test_omp_v2 import parse
from tests.test_server import FakeRegistry, FakeSender, _make_endpoint


def mixed_event():
    body = timeline_envelope()
    body["instance"] = "隔离测试机"
    waits = body["userWaitTimeline"]
    waits["intervals"] = [
        {"ref": f"{index + 16:032x}", "agentRef": "3" * 32, "kind": kind, "result": "resolved", "intervalState": "complete", "startOffsetMs": start, "endOffsetMs": end, "durationMs": end - start}
        for index, (kind, start, end) in enumerate([
            ("ask", 1000, 5000), ("approval", 3000, 7000),
            ("plan_review", 5000, 9000), ("background_wait", 7000, 11000),
        ])
    ]
    waits["observedIntervalCount"] = waits["displayedIntervalCount"] = 4
    return parse(body)


def test_omp_shared_view_separates_kinds_and_unions_overlapping_observations():
    event = mixed_event()
    view = prepare_subagent_timeline(event).view
    assert view["coverage_union_ms"] == 8000
    assert view["wait_union_ms"] == 10000  # 不将四个 4s 区间错误相加为 16s
    assert view["known_union_ms"] == 10000  # 子代理与等待也不重复相加
    assert view["unclassified_ms"] == 10000
    assert [{item["kind"] for item in track["intervals"]} for track in view["wait_tracks"]] == [{"ask"}, {"approval"}, {"plan_review"}, {"background_wait"}]
    text, html = render_text_default(event), render_html_default(event)
    attachment = render_subagent_timeline(event)
    assert attachment is not None
    for content in (text, html, attachment.html):
        assert "等待用户" not in content
        assert "交互／后台观察" in content
        assert "不等于纯人工等待" not in content  # 完整观察不重复免责声明
    assert "隔离测试机" in text and "隔离测试机" in html
    # 与 OpenCode 一致：实际 thinking 放进模型括号，主卡与子代理条目均如此。
    assert "模型：test/model(high)" in text and "thinking：" not in text
    assert "fixture/model(high)" in attachment.html and "thinking high" not in attachment.html


def test_thinking_without_model_is_shown_separately():
    body = timeline_envelope()
    body["cycle"].pop("model")
    text = render_text_default(parse(body))
    assert "thinking：high" in text and "模型：" not in text


def test_simple_omp_flow_stays_on_main_card_without_attachment():
    body = timeline_envelope()
    body.pop("userWaitTimeline")
    body["subagentTimeline"]["items"] = body["subagentTimeline"]["items"][:1]
    body["subagentTimeline"].update(observedItemCount=1, displayedItemCount=1)
    event = parse(body)
    assert render_subagent_timeline(event) is None
    assert "并发一" in render_html_default(event)
    assert "并发一" in render_text_default(event)


def test_missing_observations_never_display_precise_zero_coverage():
    body = timeline_envelope()
    body["subagentTimeline"].update(items=[], observedItemCount=0, displayedItemCount=0, partial=True, partialReasons=["loading_gap"])
    body["userWaitTimeline"].update(intervals=[], observedIntervalCount=0, displayedIntervalCount=0, partial=True, partialReasons=["loading_gap"])
    event = parse(body)
    view = prepare_subagent_timeline(event).view
    assert view["coverage_union_ms"] is None
    assert view["coverage_rate"] is None
    assert view["wait_union_ms"] is None
    assert view["wait_rate"] is None
    assert view["unclassified_observation_limited"] is True
    html, text = render_html_default(event), render_text_default(event)
    for content in (html, text):
        assert "统计仅覆盖已观测范围" in content  # 缺口时才提示范围限制


def test_trivial_complete_run_has_no_timeline_or_boilerplate():
    body = timeline_envelope()
    body["subagentTimeline"].update(items=[], observedItemCount=0, displayedItemCount=0)
    body["userWaitTimeline"].update(intervals=[], observedIntervalCount=0, displayedIntervalCount=0)
    event = parse(body)
    assert prepare_subagent_timeline(event).view is None
    assert render_subagent_timeline(event) is None
    html, text = render_html_default(event), render_text_default(event)
    for content in (html, text):
        assert "观察时间线" not in content
        assert "正常结束不代表" not in content
        assert "执行代理" not in content  # 默认 main 不显示
        assert "未分类时间" not in content
    assert "生成时间" not in html and "事件时间" in html  # OMP 只保留事件时间


def test_private_use_icon_glyphs_are_dropped_from_names():
    body = timeline_envelope()
    body["session"]["name"] = "\U000f0219 GROK: 核查"
    body["subagentTimeline"]["items"][0]["name"] = "\ue7a8"
    event = parse(body)
    text = render_text_default(event)
    assert "会话名称：GROK: 核查" in text
    assert "\ue7a8" not in text and "\U000f0219" not in text
    assert "name" not in event.subagent_timeline["items"][0]


def test_censored_interactions_do_not_enter_exact_total():
    body = timeline_envelope()
    waits = body["userWaitTimeline"]
    waits["intervals"] = [{"ref": "7" * 32, "agentRef": "3" * 32, "kind": "ask", "intervalState": "right_censored", "startOffsetMs": 4000}]
    waits.update(partial=True, partialReasons=["missing_end"])
    view = prepare_subagent_timeline(parse(body)).view
    assert view["wait_union_ms"] is None
    assert view["wait_complete_count"] == 0
    assert not view["wait_intervals"][0]["counted_duration"]


def test_mixed_metadata_omits_single_value_and_names_fall_back_anonymously():
    body = timeline_envelope()
    body["session"].pop("name")
    body["cycle"].pop("model")
    body["cycle"].pop("thinking")
    body["cycle"]["metadataChanged"] = True
    item = body["subagentTimeline"]["items"][0]
    item.pop("model")
    item.pop("thinking")
    item["metadataChanged"] = True
    event = parse(body)
    view = prepare_subagent_timeline(event).view
    assert "fixture/model" not in view["gantt_items"][0]["identity"]
    assert "thinking high" not in view["gantt_items"][0]["identity"]
    for content in (render_text_default(event), render_html_default(event)):
        assert "OMP Session 222222222222" in content
        assert "test/model" not in content
        assert "fixture/model" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["main", "timeline"])
async def test_omp_image_failure_preserves_notification_and_hides_error(failure, caplog):
    image = io.BytesIO()
    Image.new("RGB", (800, 400), (220, 230, 240)).save(image, format="PNG")
    png = image.getvalue()
    calls = 0
    sentinel = "PRIVATE_OMP_RENDER_SENTINEL"

    async def capture_render(template, data, return_url=True, options=None):
        nonlocal calls
        calls += 1
        if failure == "main" or calls == 2:
            raise RuntimeError(sentinel)
        return png

    sender = FakeSender()
    server = WebhookServer(ServerConfig(), FakeRegistry(), sender, capture_render,
                           {"render_mode": "html_image", "fallback_to_text": True})
    response = await server._handle_html_image(mixed_event(), _make_endpoint(), None, "omp-render", True)
    result = json.loads(response.body)
    assert result["data"]["delivered"] is True
    assert sentinel not in caplog.text + response.text
    if failure == "timeline":
        assert result["data"]["render_mode"] == "html_image"
        assert len(sender.delivered_image_batches) == 1
        assert len(sender.delivered_image_batches[0]) == 1
        assert not sender.sent_texts
    else:
        assert result["data"]["render_mode"] == "text"
        assert "交互／后台观察" in sender.sent_texts[-1]
        assert "等待用户" not in sender.sent_texts[-1]
