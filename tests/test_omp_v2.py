"""OMP V2 协议、状态策略与隐私边界；不使用生产凭据。"""
from __future__ import annotations

import copy
import json

import pytest

from core.omp import OmpProviderAdapter, is_omp_session_stop
from core.notification_policy import should_filter_by_duration, should_notify
from core.providers import ProviderError
from core.renderer import render_html_default, render_text_default


def envelope(status="completed"):
    body = {
        "event": "omp.run_finished", "version": 2, "id": "1" * 32,
        "emittedAt": "2026-10-23T00:00:20Z", "status": status,
        "session": {"ref": "2" * 32, "scope": "root", "name": "测试会话", "cwdName": "project", "agent": "main"},
        "cycle": {"ref": "3" * 32, "partial": False, "reasons": [], "startedAt": "2026-10-23T00:00:00Z", "endedAt": "2026-10-23T00:00:20Z", "durationMs": 20000, "model": "test/model", "thinking": "high"},
    }
    if status == "action_required":
        body["event"] = "omp.action_required"
        body["interaction"] = {"ref": "4" * 32, "kind": "ask"}
        del body["cycle"]["endedAt"]
    return body


def parse(body, headers=None):
    return OmpProviderAdapter().parse(headers=headers or {}, payload=body, received_at="2026-10-23T01:00:00Z")


@pytest.mark.parametrize("status,label", [("completed", "已完成"), ("failed", "失败"), ("aborted", "已中止"), ("action_required", "待处理"), ("unknown", "未知")])
def test_result_display_and_duration_policy(status, label):
    event = parse(envelope(status))
    assert event.status == status
    assert label in render_text_default(event)
    assert (event.summary if status == "action_required" else label) in render_html_default(event)
    assert should_filter_by_duration(status, 1000, 15) is (status == "completed")
    if status != "completed":
        assert "已完成" not in render_text_default(event)
        assert should_notify("focused", "subagent", status)


@pytest.mark.parametrize("reason,status", [("stop", "unknown"), ("error", "failed"), ("aborted", "aborted"), ("length", "unknown"), (None, "unknown")])
def test_legacy_correction_does_not_invent_terminal_evidence(reason, status):
    body = {"event": "omp.session_stop", "version": 1, "round": {"durationMs": 1000, "lastAssistant": {"stopReason": reason}}}
    event = parse(body)
    assert event.status == status
    assert not should_filter_by_duration(event.status, event.task_duration_ms, 15)
    assert event.session_scope == "unknown"


@pytest.mark.parametrize("version", [None, 1, "1.0"])
def test_promised_legacy_versions(version):
    body = {"event": "session_stop"}
    if version is not None:
        body["version"] = version
    assert parse(body, {"X-OMP-Event": "session_stop"}).version == 1


@pytest.mark.parametrize("version", [2, True, "2", "1", 0, None])
def test_legacy_rejects_wrong_explicit_version(version):
    with pytest.raises(ProviderError):
        parse({"event": "omp.session_stop", "version": version})


def test_new_events_cannot_be_misread_by_old_identifier():
    for status in ("completed", "action_required"):
        body = envelope(status)
        assert is_omp_session_stop({"x-omp-event": body["event"]}, body)[0] is False
        with pytest.raises(ProviderError):
            parse(body, {"x-omp-event": "session_stop"})


@pytest.mark.parametrize("path,value", [
    (("version",), True), (("version",), 1), (("version",), "2"),
    (("event",), "omp.unknown"), (("status",), "success"), (("status",), []),
    (("id",), "raw-id"), (("emittedAt",), "not-a-date"), (("emittedAt",), "2026-10-23T00:00:00"),
    (("session", "scope"), "auxiliary"), (("session", "ref"), "raw-session"),
    (("session", "name"), "x" * 97), (("session", "cwdName"), "/private/sentinel"),
    (("session", "name"), "line\ncontrol"), (("session", "prompt"), "PRIVATE_SENTINEL"),
    (("cycle", "durationMs"), True), (("cycle", "durationMs"), -1), (("cycle", "durationMs"), 1.5),
    (("cycle", "durationMs"), 86400001), (("cycle", "durationMs"), 30000),
    (("cycle", "partial"), True), (("cycle", "reasons"), ["private-error"]),
    (("cycle", "reasons"), ["missing_start", "missing_start"]),
    (("cycle", "thinking"), "x" * 33), (("cycle", "model"), "x" * 129),
    (("cycle", "metadataChanged"), True), (("cycle", "metadataChanged"), 1),
    (("cycle", "raw"), {}), (("target_alias",), ""), (("extra",), "PRIVATE_SENTINEL"),
])
def test_invalid_v2_fails_closed_without_echo(path, value):
    body = envelope()
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProviderError) as error:
        parse(body)
    assert "PRIVATE_SENTINEL" not in str(error.value)
    assert not error.value.retryable


@pytest.mark.parametrize("key", ["event", "version", "id", "emittedAt", "status", "session", "cycle"])
def test_missing_required_field_rejected(key):
    body = envelope()
    del body[key]
    with pytest.raises(ProviderError):
        parse(body)


def test_relative_path_in_session_name_is_accepted():
    body = envelope()
    body["session"]["name"] = "NODE: 测试 Phowx/script 的 … 用法"
    assert "Phowx/script" in render_text_default(parse(body))


def ask_questions():
    return [
        {"text": "这次单次验证，用哪种方式取得 Cookie？", "header": "取得方式", "options": [
            {"label": "手动复制，本机导入", "description": "你在浏览器中复制 Cookie 后告知", "recommended": True},
            {"label": "临时 MITM 捕获", "description": "会修改 Surge 配置和 MITM 状态"},
        ]},
        {"text": "完成后是否清理临时设置？"},
    ]


def test_ask_interaction_renders_like_opencode_question_card():
    body = envelope("action_required")
    body["interaction"].update(questionCount=2, questions=ask_questions())
    event = parse(body)
    text, html = render_text_default(event), render_html_default(event)
    assert event.summary == "等待问题回答"
    assert "问题 1 标题" in text and "取得方式" in text
    assert "1. 手动复制，本机导入（推荐）" in text
    for content in (text, html):
        assert "这次单次验证，用哪种方式取得 Cookie？" in content and "完成后是否清理临时设置？" in content
        assert "会修改 Surge 配置和 MITM 状态" in content
    assert '<div class="action-headline">等待问题回答</div>' in html
    assert "已运行 20 秒" in html and "<h1>测试会话</h1>" in html
    assert "project<span" in html and "test/model(high)" in html and "08:00:00 开始" in html
    assert '<div class="action-block-title">取得方式</div>' in html
    assert '<div class="action-block-title">问题 2</div>' in html
    recommended = html.index('class="action-option action-option-recommended"')
    assert recommended < html.index("手动复制，本机导入") < html.index(">推荐<") < html.index("临时 MITM 捕获")


@pytest.mark.parametrize("kind,label", [("approval", "审批工具"), ("plan_review", "计划标题")])
def test_approval_and_plan_titles_are_shown(kind, label):
    body = envelope("action_required")
    body["interaction"].update(kind=kind, title="bash")
    assert f"{label}：bash" in render_text_default(parse(body))


@pytest.mark.parametrize("mutate", [
    lambda i: i.update(questionCount=1, questions=[]),
    lambda i: i.update(questionCount=1, questions=ask_questions()),
    lambda i: i.update(questionCount=5, questions=ask_questions() * 3),
    lambda i: i.update(questionCount=2, questions=[{"text": "x" * 301}, {"text": "y"}]),
    lambda i: i.update(questionCount=1, questions=[{"text": "/private/PRIVATE_SENTINEL"}]),
    lambda i: i.update(questionCount=1, questions=[{"text": "q", "answer": "PRIVATE_SENTINEL"}]),
    lambda i: i.update(questionCount=1, questions=[{"text": "q", "options": [{"label": "a"}, {"label": "a"}]}]),
    lambda i: i.update(questionCount=1, questions=[{"text": "q", "options": [{"label": "a", "recommended": True}, {"label": "b", "recommended": True}]}]),
    lambda i: i.update(questionCount=1, questions=[{"text": "q", "options": [{"label": "a", "description": "x" * 201}]}]),
    lambda i: i.update(title="PRIVATE_SENTINEL"),
    lambda i: i.update(kind="approval", questionCount=1, questions=[{"text": "q"}]),
    lambda i: i.update(kind="approval", title="x" * 201),
])
def test_invalid_interaction_detail_rejected_without_echo(mutate):
    body = envelope("action_required")
    mutate(body["interaction"])
    with pytest.raises(ProviderError) as error:
        parse(body)
    assert "PRIVATE_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("kind", ["approval", "plan_review", "ask"])
def test_action_contract_is_separate_from_completion(kind):
    body = envelope("action_required")
    body["interaction"]["kind"] = kind
    event = parse(body)
    assert event.task_duration_ms is None
    assert event.summary in {"等待工具审批", "等待计划审阅", "等待问题回答"}
    invalid = copy.deepcopy(body)
    invalid["cycle"]["endedAt"] = invalid["emittedAt"]
    with pytest.raises(ProviderError):
        parse(invalid)
    invalid = copy.deepcopy(body)
    invalid["interaction"]["question"] = "PRIVATE_SENTINEL"
    with pytest.raises(ProviderError):
        parse(invalid)


def test_missing_boundary_is_partial_and_has_no_duration():
    body = envelope("unknown")
    body["cycle"] = {"ref": "3" * 32, "partial": True, "reasons": ["missing_start"], "endedAt": body["emittedAt"]}
    event = parse(body)
    assert event.task_duration_ms is None
    assert "不完整" in render_text_default(event)
    body["cycle"]["durationMs"] = 0
    with pytest.raises(ProviderError):
        parse(body)


def test_legacy_sensitive_data_does_not_reach_normalized_or_default_card():
    sentinel = "PRIVATE_SENTINEL"
    event = parse({
        "event": "omp.session_stop", "session": {"id": sentinel, "file": f"/private/{sentinel}.json", "cwd": f"/private/{sentinel}/project"},
        "round": {"turnId": sentinel, "prompt": sentinel, "lastAssistant": {"stopReason": sentinel}},
        "metadata": {"eventName": sentinel, "version": sentinel},
    })
    for output in (json.dumps(event.to_dict()), render_text_default(event), render_html_default(event)):
        assert sentinel not in output
        assert "/private/" not in output
    assert "project" in render_text_default(event)


def test_size_and_deep_unknown_fields_are_rejected():
    body = envelope()
    body["session"]["name"] = "界" * 40000
    with pytest.raises(ProviderError):
        parse(body)
    body = envelope()
    body["cycle"]["reasons"] = [["capacity"]] * 2000
    with pytest.raises(ProviderError):
        parse(body)


@pytest.mark.asyncio
async def test_omp_delivery_failure_never_logs_exception_body_or_stack(caplog):
    import logging
    from astrbot.api.star import Context
    from core.models import EndpointRecord, TargetAlias
    from core.sender import DeliveryContext, Sender

    class FailingContext(Context):
        async def send_message(self, umo, message_chain):
            raise RuntimeError("PRIVATE_SENTINEL /private/business/path")

    endpoint = EndpointRecord(
        name="fixture", path="fixture", provider="omp", token_hash="hash",
        token_hash_algorithm="hmac-sha256", owner_user_id="fixture",
        targets=[TargetAlias("default", "test:GroupMessage:fixture")],
        status="active", created_at="2026-10-23T00:00:00Z",
    )
    caplog.set_level(logging.ERROR)
    results = await Sender(FailingContext()).send_text(
        "safe", endpoint, delivery_context=DeliveryContext("safe-ref", "omp", "fixture")
    )
    assert results == [{"name": "default", "ok": False, "error": "send_failed"}]
    assert "reason=send_failed" in caplog.text
    assert "PRIVATE_SENTINEL" not in caplog.text
    assert "/private/business" not in caplog.text
    assert "Traceback" not in caplog.text
