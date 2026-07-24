"""Notification policy and pre-render server boundary tests."""

from __future__ import annotations

import json
from typing import cast

import pytest

from core.models import EndpointRecord, NormalizedEvent, ServerConfig, TargetAlias
from core.notification_policy import (
    SessionScope,
    normalize_notification_mode,
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


class _RecordingSender:
    def __init__(self) -> None:
        self.preflight_calls = 0
        self.text_calls = 0
        self.image_calls = 0

    def preflight_private_notification_policy(self, *_args, **_kwargs):
        self.preflight_calls += 1
        raise AssertionError("notification filtering must precede sender preflight")

    async def send_text(self, *_args, **_kwargs):
        self.text_calls += 1
        raise AssertionError("filtered event must not send text")

    async def send_image(self, *_args, **_kwargs):
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

        async def send_text(self, *_args, **_kwargs):
            self.sent += 1
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

        async def send_text(self, *_args, **_kwargs):
            self.sent += 1
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
