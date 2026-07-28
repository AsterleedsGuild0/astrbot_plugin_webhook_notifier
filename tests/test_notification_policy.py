"""Notification policy and pre-render server boundary tests."""

from __future__ import annotations

import json
import logging
from typing import cast

import pytest

from core.models import EndpointRecord, NormalizedEvent, ServerConfig, TargetAlias
from core.notification_policy import (
    MIN_COMPLETION_DURATION_DEFAULT,
    SessionScope,
    normalize_min_completion_duration,
    normalize_notification_mode,
    should_filter_by_duration,
    should_notify,
)
from core.server import WebhookServer


def _endpoint() -> EndpointRecord:
    return EndpointRecord(
        name="policy-test",
        path="policy/test",
        provider="opencode",
        token_hash="hash",
        token_hash_algorithm="hmac-sha256",
        owner_user_id="owner",
        owner_platform_id="test",
        targets=[TargetAlias(name="default", umo="test:GroupMessage:1")],
        status="active",
        created_at="2026-07-24T00:00:00Z",
    )


def _event(scope: str, status: str) -> NormalizedEvent:
    return NormalizedEvent(
        provider="opencode",
        event="opencode.session_idle",
        status=status,
        session_scope=SessionScope(scope),
    )


@pytest.mark.parametrize("mode", ["focused", "all"])
@pytest.mark.parametrize("scope", ["root", "subagent", "auxiliary", "unknown"])
@pytest.mark.parametrize("status", ["completed", "failed", "action_required"])
def test_policy_matrix(mode: str, scope: str, status: str) -> None:
    expected = not (
        mode == "focused"
        and scope in {"subagent", "auxiliary"}
        and status == "completed"
    )
    assert should_notify(mode, scope, status) is expected


def test_policy_unknown_status_and_invalid_modes_fail_open() -> None:
    assert should_notify("focused", "subagent", "future_status") is True
    assert should_notify("focused", "unknown", "completed") is True
    assert normalize_notification_mode() == "focused"
    assert normalize_notification_mode("invalid") == "all"
    assert should_notify("invalid", "subagent", "completed") is True


# ─── 最短完成通知时长测试 ──────────────────────────────────


class TestNormalizeMinCompletionDuration:
    def test_missing_defaults_to_15(self) -> None:
        assert normalize_min_completion_duration() == MIN_COMPLETION_DURATION_DEFAULT

    def test_valid_values(self) -> None:
        assert normalize_min_completion_duration(0) == 0
        assert normalize_min_completion_duration(15) == 15
        assert normalize_min_completion_duration(3600) == 3600
        assert normalize_min_completion_duration(1) == 1

    def test_invalid_values_fail_open_to_0(self) -> None:
        assert normalize_min_completion_duration(-1) == 0
        assert normalize_min_completion_duration(3601) == 0
        assert normalize_min_completion_duration(99999) == 0

    def test_non_int_fail_open_to_0(self) -> None:
        assert normalize_min_completion_duration("abc") == 0
        assert normalize_min_completion_duration(3.14) == 0
        assert normalize_min_completion_duration([15]) == 0

    def test_none_defaults_to_15(self) -> None:
        """None（config.get() 未配置）应默认 15。"""
        assert (
            normalize_min_completion_duration(None) == MIN_COMPLETION_DURATION_DEFAULT
        )

    def test_bool_fail_open_to_0(self) -> None:
        """bool 是 int 子类但必须拒绝。"""
        assert normalize_min_completion_duration(True) == 0
        assert normalize_min_completion_duration(False) == 0


class TestShouldFilterByDuration:
    def test_threshold_0_disables_filter(self) -> None:
        assert should_filter_by_duration("completed", 1000, 0) is False
        assert should_filter_by_duration("completed", 14000, 0) is False
        assert should_filter_by_duration("completed", None, 0) is False

    def test_completed_below_threshold_skips(self) -> None:
        assert should_filter_by_duration("completed", 14999, 15) is True
        assert should_filter_by_duration("completed", 0, 15) is True
        assert should_filter_by_duration("completed", 5000, 10) is True

    def test_completed_at_or_above_threshold_proceeds(self) -> None:
        assert should_filter_by_duration("completed", 15000, 15) is False
        assert should_filter_by_duration("completed", 20000, 15) is False
        assert should_filter_by_duration("completed", 10000, 10) is False

    def test_none_duration_fail_open(self) -> None:
        """duration 不可靠时始终放行。"""
        assert should_filter_by_duration("completed", None, 15) is False
        assert should_filter_by_duration("completed", None, 3600) is False

    def test_non_completed_status_always_proceeds(self) -> None:
        assert should_filter_by_duration("failed", 5000, 15) is False
        assert should_filter_by_duration("action_required", 5000, 15) is False
        assert should_filter_by_duration("unknown", 5000, 15) is False
        assert should_filter_by_duration("future_status", 5000, 15) is False


class TestDurationFilterInDispatch:
    """集成测试：时长过滤在 dispatch_event 中的行为。"""

    class _RecordingSender:
        def __init__(self) -> None:
            self.preflight_calls = 0
            self.text_calls = 0
            self.image_calls = 0

        def preflight_private_notification_policy(self, *_args, **_kwargs):
            self.preflight_calls += 1
            raise AssertionError("duration filter must precede sender preflight")

        async def send_text(self, *_args, delivery_attempt_callback=None, **_kwargs):
            self.text_calls += 1
            raise AssertionError("duration filtered event must not send text")

        async def send_image(self, *_args, delivery_attempt_callback=None, **_kwargs):
            self.image_calls += 1
            raise AssertionError("duration filtered event must not send image")

    @pytest.mark.asyncio
    async def test_completed_below_threshold_skips_render_and_send(
        self, monkeypatch, caplog
    ):
        caplog.set_level(logging.INFO, logger="astrbot")
        sender = self._RecordingSender()
        html_calls = 0

        async def html_render(*_args, **_kwargs):
            nonlocal html_calls
            html_calls += 1
            raise AssertionError("duration filtered event must not call html renderer")

        monkeypatch.setattr(
            "core.server.render_text_default",
            lambda _event: (_ for _ in ()).throw(
                AssertionError("duration filtered event must not call text renderer")
            ),
        )
        server = WebhookServer(
            config=ServerConfig(),
            registry=object(),  # type: ignore[arg-type]
            sender=sender,  # type: ignore[arg-type]
            html_render=html_render,
            plugin_config={
                "render_mode": "text",
                "notification_mode": "all",
                "min_completion_duration_seconds": 10,
            },
        )

        event = _event("root", "completed")
        event.task_duration_ms = 5000  # 5s < 10s

        response = await server._dispatch_event(
            event, _endpoint(), None, "duration-skip-request"
        )
        payload = json.loads(cast(bytes, response.body).decode())

        assert response.status == 200
        assert payload["message"] == "skipped"
        assert payload["data"]["skip_reason"] == "completion_below_duration_threshold"
        assert payload["data"]["reason"] == "completion_below_duration_threshold"
        assert payload["data"]["rendered"] is False
        assert payload["data"]["delivered"] is False
        assert payload["data"]["retryable"] is False
        assert payload["data"]["duration_ms"] == 5000
        assert payload["data"]["threshold_seconds"] == 10
        assert sender.preflight_calls == 0
        assert sender.text_calls == 0
        assert sender.image_calls == 0
        assert html_calls == 0
        assert "已跳过短任务完成通知" in caplog.text
        assert "Provider=opencode" in caplog.text
        assert "任务耗时=5 秒" in caplog.text
        assert "通知阈值=10 秒" in caplog.text
        assert "duration_ms=5000" in caplog.text
        assert "reason=completion_below_duration_threshold" in caplog.text

    @pytest.mark.asyncio
    async def test_completed_at_threshold_proceeds(self):
        """等于阈值的任务正常发送。"""
        sender = self._RecordingSender()
        sender.preflight_private_notification_policy = lambda *a, **kw: None

        async def send_text(*_a, **_kw):
            return [{"name": "default", "ok": True, "error": None}]

        sender.send_text = send_text  # type: ignore[method-assign]

        server = WebhookServer(
            config=ServerConfig(),
            registry=object(),  # type: ignore[arg-type]
            sender=sender,  # type: ignore[arg-type]
            plugin_config={
                "render_mode": "text",
                "notification_mode": "all",
                "min_completion_duration_seconds": 15,
            },
        )

        event = _event("root", "completed")
        event.task_duration_ms = 15000  # = 15s threshold

        response = await server._dispatch_event(
            event, _endpoint(), None, "duration-at-threshold"
        )
        assert json.loads(cast(bytes, response.body).decode())["message"] == "ok"

    @pytest.mark.asyncio
    async def test_missing_duration_proceeds(self):
        """task_duration_ms 为 None 时放行。"""
        sender = self._RecordingSender()
        sender.preflight_private_notification_policy = lambda *a, **kw: None

        async def send_text(*_a, **_kw):
            return [{"name": "default", "ok": True, "error": None}]

        sender.send_text = send_text  # type: ignore[method-assign]

        server = WebhookServer(
            config=ServerConfig(),
            registry=object(),  # type: ignore[arg-type]
            sender=sender,  # type: ignore[arg-type]
            plugin_config={
                "render_mode": "text",
                "notification_mode": "all",
                "min_completion_duration_seconds": 15,
            },
        )

        event = _event("root", "completed")
        event.task_duration_ms = None

        response = await server._dispatch_event(
            event, _endpoint(), None, "duration-none"
        )
        assert json.loads(cast(bytes, response.body).decode())["message"] == "ok"

    @pytest.mark.asyncio
    async def test_threshold_0_proceeds(self):
        """threshold 为 0 时关闭过滤。"""
        sender = self._RecordingSender()
        sender.preflight_private_notification_policy = lambda *a, **kw: None

        async def send_text(*_a, **_kw):
            return [{"name": "default", "ok": True, "error": None}]

        sender.send_text = send_text  # type: ignore[method-assign]

        server = WebhookServer(
            config=ServerConfig(),
            registry=object(),  # type: ignore[arg-type]
            sender=sender,  # type: ignore[arg-type]
            plugin_config={
                "render_mode": "text",
                "notification_mode": "all",
                "min_completion_duration_seconds": 0,
            },
        )

        event = _event("root", "completed")
        event.task_duration_ms = 1000

        response = await server._dispatch_event(
            event, _endpoint(), None, "duration-zero"
        )
        assert json.loads(cast(bytes, response.body).decode())["message"] == "ok"

    @pytest.mark.asyncio
    async def test_failed_status_bypasses_duration_filter(self):
        """failed 状态不应被时长过滤。"""
        sender = self._RecordingSender()
        sender.preflight_private_notification_policy = lambda *a, **kw: None

        async def send_text(*_a, **_kw):
            return [{"name": "default", "ok": True, "error": None}]

        sender.send_text = send_text  # type: ignore[method-assign]

        server = WebhookServer(
            config=ServerConfig(),
            registry=object(),  # type: ignore[arg-type]
            sender=sender,  # type: ignore[arg-type]
            plugin_config={
                "render_mode": "text",
                "notification_mode": "all",
                "min_completion_duration_seconds": 15,
            },
        )

        event = _event("subagent", "failed")
        event.task_duration_ms = 1000

        response = await server._dispatch_event(
            event, _endpoint(), None, "duration-failed"
        )
        assert json.loads(cast(bytes, response.body).decode())["message"] == "ok"

    @pytest.mark.asyncio
    async def test_mode_filter_takes_priority_over_duration(self):
        """notification_mode 过滤在时长之前判断。"""
        sender = self._RecordingSender()
        server = WebhookServer(
            config=ServerConfig(),
            registry=object(),  # type: ignore[arg-type]
            sender=sender,  # type: ignore[arg-type]
            plugin_config={
                "render_mode": "text",
                "notification_mode": "focused",
                "min_completion_duration_seconds": 15,
            },
        )

        event = _event("subagent", "completed")
        event.task_duration_ms = 30000  # would pass duration check

        response = await server._dispatch_event(
            event, _endpoint(), None, "mode-before-duration"
        )
        payload = json.loads(cast(bytes, response.body).decode())

        assert payload["data"]["skip_reason"] == "notification_mode_filtered"
        assert payload["data"]["rendered"] is False
        assert sender.preflight_calls == 0


class _RecordingSender:
    def __init__(self) -> None:
        self.preflight_calls = 0
        self.text_calls = 0
        self.image_calls = 0

    def preflight_private_notification_policy(self, *_args, **_kwargs):
        self.preflight_calls += 1
        raise AssertionError("notification filtering must precede sender preflight")

    async def send_text(self, *_args, delivery_attempt_callback=None, **_kwargs):
        self.text_calls += 1
        raise AssertionError("filtered event must not send text")

    async def send_image(self, *_args, delivery_attempt_callback=None, **_kwargs):
        self.image_calls += 1
        raise AssertionError("filtered event must not send image")


@pytest.mark.asyncio
@pytest.mark.parametrize("render_mode", ["text", "html_image"])
async def test_filtered_event_skips_all_render_and_send_stages(
    render_mode: str, monkeypatch
) -> None:
    sender = _RecordingSender()
    html_calls = 0

    async def html_render(*_args, **_kwargs):
        nonlocal html_calls
        html_calls += 1
        raise AssertionError("filtered event must not call html renderer")

    monkeypatch.setattr(
        "core.server.render_text_default",
        lambda _event: (_ for _ in ()).throw(
            AssertionError("filtered event must not call text renderer")
        ),
    )
    server = WebhookServer(
        config=ServerConfig(),
        registry=object(),  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
        html_render=html_render,
        plugin_config={"render_mode": render_mode, "notification_mode": "focused"},
    )

    response = await server._dispatch_event(
        _event("subagent", "completed"), _endpoint(), None, "policy-request"
    )
    payload = json.loads(cast(bytes, response.body).decode())

    assert response.status == 200
    assert payload["message"] == "skipped"
    assert payload["data"]["skip_reason"] == "notification_mode_filtered"
    assert payload["data"]["rendered"] is False
    assert payload["data"]["delivered"] is False
    assert payload["data"]["retryable"] is False
    assert sender.preflight_calls == 0
    assert sender.text_calls == 0
    assert sender.image_calls == 0
    assert html_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["subagent", "auxiliary"])
@pytest.mark.parametrize("status", ["failed", "action_required"])
async def test_focused_allows_failed_and_action_required(
    status: str, scope: str
) -> None:
    class Sender:
        def __init__(self):
            self.sent = 0

        def preflight_private_notification_policy(self, *_args, **_kwargs):
            return None

        async def send_text(self, *_args, delivery_attempt_callback=None, **_kwargs):
            self.sent += 1
            if delivery_attempt_callback is not None:
                delivery_attempt_callback.mark()
            return [{"name": "default", "ok": True, "error": None}]

    sender = Sender()
    server = WebhookServer(
        config=ServerConfig(),
        registry=object(),  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
        plugin_config={"notification_mode": "focused", "render_mode": "text"},
    )
    response = await server._dispatch_event(
        _event(scope, status), _endpoint(), None, "allowed-request"
    )
    assert json.loads(cast(bytes, response.body).decode())["message"] == "ok"
    assert sender.sent == 1


@pytest.mark.asyncio
async def test_focused_filters_auxiliary_completion_with_scope_and_reason() -> None:
    sender = _RecordingSender()
    server = WebhookServer(
        config=ServerConfig(),
        registry=object(),  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
        plugin_config={"notification_mode": "focused", "render_mode": "text"},
    )
    response = await server._dispatch_event(
        _event("auxiliary", "completed"), _endpoint(), None, "auxiliary-request"
    )
    payload = json.loads(cast(bytes, response.body).decode())
    assert payload["message"] == "skipped"
    assert payload["data"]["scope"] == "auxiliary"
    assert payload["data"]["skip_reason"] == "notification_mode_filtered"
    assert payload["data"]["reason"] == "notification_mode_filtered"


@pytest.mark.asyncio
async def test_all_sends_auxiliary_completion() -> None:
    class Sender:
        def __init__(self):
            self.sent = 0

        def preflight_private_notification_policy(self, *_args, **_kwargs):
            return None

        async def send_text(self, *_args, delivery_attempt_callback=None, **_kwargs):
            self.sent += 1
            if delivery_attempt_callback is not None:
                delivery_attempt_callback.mark()
            return [{"name": "default", "ok": True, "error": None}]

    sender = Sender()
    server = WebhookServer(
        config=ServerConfig(),
        registry=object(),  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
        plugin_config={"notification_mode": "all", "render_mode": "text"},
    )
    response = await server._dispatch_event(
        _event("auxiliary", "completed"), _endpoint(), None, "auxiliary-all-request"
    )
    assert json.loads(cast(bytes, response.body).decode())["message"] == "ok"
    assert sender.sent == 1
