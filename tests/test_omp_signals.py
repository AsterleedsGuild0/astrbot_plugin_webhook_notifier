"""OMP V2 可选扩展（#34／#35）：错误类型、所属主会话、会话创建时间、运行健康与资源信号、凭据停用告警。"""
from __future__ import annotations

import copy
import json

import pytest

from core.providers import ProviderError
from core.renderer import render_html_data, render_html_default, render_image_caption, render_text_default
from tests.test_omp_v2 import envelope, parse

SENTINEL = "PRIVATE_SIGNAL_SENTINEL"
SIGNALS = {
    "context": {"tokens": 8890, "window": 1_000_000},
    "compaction": {"count": 2, "failed": 1},
    "fallback": {"from": "cpa/deepseek-flash:low", "to": "cpa/glm-5.3-flash", "succeeded": True},
    "usage": {"input": 2618, "output": 478, "cacheRead": 31872, "cacheWrite": 0, "total": 34968},
    "goal": {"objective": "发布 v2.2.0，见 …", "status": "budget-limited", "tokensUsed": 210000, "tokenBudget": 200000, "timeUsedSeconds": 61},
    "todo": {"reminders": 2, "maxReminders": 3, "pendingCount": 10, "pending": ["修复 core/omp.py", "补测"]},
}


def extended(status="failed"):
    body = envelope(status)
    body["session"]["createdAt"] = "2026-10-22T22:00:00Z"
    if status != "action_required":
        body["signals"] = copy.deepcopy(SIGNALS)
    if status in ("failed", "unknown"):
        body["error"] = {"category": "server", "retried": True}
    return body


def labels(event):
    return {field["label"]: field["value"] for field in render_html_data(event)["event"]["fields"]}


def test_old_v2_payload_without_extensions_is_unchanged():
    shown = labels(parse(envelope()))
    assert not {"错误类型", "所属主会话", "会话开始时间", "会话已持续", "上下文占用", "本次运行用量"} & shown.keys()


def test_extended_failure_card_shows_every_signal_with_scoped_wording():
    event = parse(extended())
    shown = labels(event)
    assert shown["错误类型"] == "服务端错误（HTTP 5xx），自动重试后仍失败"
    assert shown["会话开始时间"].startswith("2026-10-23 06:00:00")
    assert shown["会话已持续"] == "2 小时 20 秒"
    assert shown["上下文占用"] == "0.9%（8.9k / 1M，当前窗口，非累计用量）"
    assert shown["自动压缩"] == "2 次，另有 1 次失败或中止"
    assert shown["模型降级"] == "cpa/deepseek-flash:low → cpa/glm-5.3-flash，备用模型完成请求"
    assert shown["本次运行用量"] == "共 35k tokens（输入 2.6k · 输出 478 · 缓存读 31.9k）"
    assert shown["目标"] == "发布 v2.2.0，见 …"
    assert shown["目标状态"] == "⚠ 预算已耗尽 · 已用 210k / 200k · 用时 1 分钟 1 秒"
    assert shown["待办提醒"] == "第 2/3 次提醒时仍有 10 项未完成；结束时可能仍未完成"
    assert shown["未完成待办"] == "修复 core/omp.py；补测；等共 10 项"
    text = render_text_default(event)
    assert "错误类型" in text and "本次运行用量" in text and "会话已持续" in text
    html = render_html_default(event)
    assert "本次运行用量" in html and "上下文占用" in html


def test_subagent_card_shows_root_session_only_for_subagents():
    body = envelope("failed")
    body["session"].update(scope="subagent", rootName="发布主会话")
    assert labels(parse(body))["所属主会话"] == "发布主会话"
    body = envelope("failed")
    body["session"]["rootName"] = "发布主会话"
    with pytest.raises(ProviderError):
        parse(body)


def test_action_card_keeps_task_start_and_lists_session_age():
    body = extended("action_required")
    view = render_html_data(parse(body))["event"]["action_view"]
    assert any(item.endswith("开始") for item in view["meta"])
    assert {extra["label"] for extra in view["extras"]} >= {"会话开始时间", "会话已持续"}


@pytest.mark.parametrize("category,text", [
    ("auth", "鉴权失败（HTTP 401/403）"), ("rate_limit", "请求限流（HTTP 429）"), ("request", "请求被拒绝（HTTP 4xx）"),
    ("truncated", "输出达到长度上限"), ("retry_exhausted", "自动重试耗尽"),
])
def test_error_categories(category, text):
    body = envelope("unknown" if category == "truncated" else "failed")
    body["error"] = {"category": category}
    assert labels(parse(body))["错误类型"] == text


def test_compaction_without_success_and_goal_without_budget():
    body = envelope()
    body["signals"] = {"compaction": {"count": 0, "failed": 1}, "goal": {"objective": "整理文档", "status": "active", "tokensUsed": 5, "timeUsedSeconds": 3}}
    shown = labels(parse(body))
    assert shown["自动压缩"] == "1 次失败或中止"
    assert shown["目标状态"] == "进行中 · 已用 5 · 用时 3 秒"


def _set(body, path, value):
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize("path,value", [
    (("error", "category"), "timeout"), (("error", "retried"), False), (("error", "message"), SENTINEL),
    (("session", "createdAt"), "2026-10-23T00:00:30Z"), (("session", "createdAt"), SENTINEL), (("session", "email"), SENTINEL),
    (("signals", "context", "tokens"), 2_000_000), (("signals", "context", "window"), 0), (("signals", "context", "percent"), 1.5),
    (("signals", "compaction", "count"), -1), (("signals", "compaction", "failed"), 0), (("signals", "compaction", "count"), True),
    (("signals", "fallback", "reason"), SENTINEL), (("signals", "fallback", "to"), f"local//private/{SENTINEL}"), (("signals", "fallback", "succeeded"), "yes"),
    (("signals", "usage", "total"), 0), (("signals", "usage", "cost"), 1), (("signals", "usage", "input"), 1.5),
    (("signals", "goal", "status"), "running"), (("signals", "goal", "objective"), f"/private/{SENTINEL}"), (("signals", "goal", "objective"), "x" * 201),
    (("signals", "todo", "pending"), [f"https://e.invalid/{SENTINEL}"]), (("signals", "todo", "pending"), ["x"] * 9), (("signals", "todo", "reminders"), 4),
    (("signals", "todo", "details"), SENTINEL), (("signals", "credential"), SENTINEL), (("signals",), {}),
])
def test_strict_rejection_does_not_echo_input(path, value):
    body = extended()
    _set(body, path, value)
    with pytest.raises(ProviderError) as caught:
        parse(body)
    assert SENTINEL not in str(caught.value) and SENTINEL not in repr(caught.value.__dict__)


@pytest.mark.parametrize("status", ["completed", "aborted"])
def test_error_only_on_failed_or_unknown(status):
    body = envelope(status)
    body["error"] = {"category": "server"}
    with pytest.raises(ProviderError):
        parse(body)


def test_action_required_rejects_signals_and_error():
    for key, value in (("signals", {"context": {"tokens": 1, "window": 2}}), ("error", {"category": "auth"})):
        body = envelope("action_required")
        body[key] = value
        with pytest.raises(ProviderError):
            parse(body)


def credential_alert(**extra):
    return {"event": "omp.credential_disabled", "version": 2, "id": "5" * 32, "emittedAt": "2026-10-23T00:00:00Z", "provider": "anthropic", **extra}


def test_credential_alert_is_a_standalone_pending_card():
    event = parse(credential_alert(instance="工作机"), {"X-OMP-Event": "omp.credential_disabled"})
    assert (event.event, event.status, event.title, event.summary) == ("omp.credential_disabled", "action_required", "OMP 凭据已停用", "凭据已被自动停用")
    data = render_html_data(event)["event"]
    assert "anthropic" in data["action_view"]["meta"]
    assert render_image_caption(event) == "【待处理】OMP 凭据已停用 · 凭据已被自动停用"
    assert "重新登录" in render_text_default(event)


@pytest.mark.parametrize("extra", [
    {"disabledCause": SENTINEL}, {"email": SENTINEL}, {"accountId": SENTINEL}, {"credentialId": 1}, {"orgName": SENTINEL},
    {"provider": f"/private/{SENTINEL}"}, {"version": 1}, {"session": {}},
])
def test_credential_alert_rejects_identity_fields(extra):
    with pytest.raises(ProviderError) as caught:
        parse(credential_alert(**extra))
    assert SENTINEL not in json.dumps(caught.value.__dict__, default=str)


def test_credential_alert_header_must_match():
    with pytest.raises(ProviderError):
        parse(credential_alert(), {"X-OMP-Event": "omp.run_finished"})
