"""Server tests - html_image flow."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from core.models import EndpointRecord, NormalizedEvent, ServerConfig, TargetAlias
from core.registry import EndpointRegistry
from core.renderer import render_text_default
from core.sender import Sender
from core.server import WebhookServer


def _make_event() -> NormalizedEvent:
    return NormalizedEvent(
        provider="omp",
        event="omp.session_stop",
        version=1,
        id="sess_001:turn_001",
        emitted_at="2026-07-09T12:00:00.000Z",
        title="会话完成",
        status="success",
        summary="测试任务已完成",
        source={"name": "oh-my-pi", "url": None},
        actor={"name": None, "url": None},
        fields=[
            {"label": "模型", "value": "gpt-5.5", "short": True},
            {"label": "耗时", "value": "57.7s", "short": True},
        ],
        links=[],
        raw={},
    )


def _make_endpoint(render_mode: str = "text") -> EndpointRecord:
    return EndpointRecord(
        name="test_ep",
        path="u/hash/test_ep",
        provider="omp",
        token_hash="abc123",
        token_hash_algorithm="hmac-sha256",
        owner_user_id="user_001",
        targets=[TargetAlias(name="default", umo="test:Platform:Message:1")],
        render_mode=render_mode,
        template=None,
        status="active",
        created_at="2026-07-09T12:00:00",
    )


class FakeSender(Sender):
    """Sender stub that records sent data."""

    def __init__(self) -> None:
        # 不调用 super().__init__，避免依赖 Context
        self.sent_texts: list[str] = []
        self.sent_images: list[str | bytes] = []
        self._fail_send: bool = False

    def set_fail_send(self, fail: bool = True) -> None:
        self._fail_send = fail

    async def send_text(
        self, text: str, endpoint: EndpointRecord, target_alias: str | None = None
    ) -> list[dict]:
        self.sent_texts.append(text)
        if self._fail_send:
            return [{"name": "default", "ok": False, "error": "simulated_failure"}]
        return [{"name": "default", "ok": True, "error": None}]

    async def send_image(
        self,
        image_result: str | bytes,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
    ) -> list[dict]:
        self.sent_images.append(image_result)
        if self._fail_send:
            return [{"name": "default", "ok": False, "error": "simulated_failure"}]
        return [{"name": "default", "ok": True, "error": None}]


class FakeRegistry:
    """Registry stub to avoid file I/O."""

    def __init__(self) -> None:
        self.server_secret = "a" * 128

    def is_endpoint_active(self, name: str, owner_user_id: str) -> bool:
        return True


@pytest.fixture
def server() -> WebhookServer:
    config = ServerConfig()
    registry = FakeRegistry()
    sender = FakeSender()

    async def fake_html_render(
        tmpl: str, data: dict, return_url: bool = True, options: dict | None = None
    ) -> str | bytes:
        return "https://example.com/rendered_image.png"

    return WebhookServer(
        config=config,
        registry=registry,
        sender=sender,
        html_render=fake_html_render,
        plugin_config={
            "render_mode": "text",
            "fallback_to_text": True,
            "render_options": '{"full_page": true, "type": "png"}',
        },
    )


# ─── _get_render_mode ──────────────────────────────────────


class TestGetRenderMode:
    def test_endpoint_mode_priority(self, server: WebhookServer):
        """endpoint 级 render_mode 应优先于插件默认。"""
        ep = _make_endpoint(render_mode="html_image")
        assert server._get_render_mode(ep) == "html_image"

    def test_text_mode(self, server: WebhookServer):
        """text 模式应正常返回。"""
        ep = _make_endpoint(render_mode="text")
        assert server._get_render_mode(ep) == "text"

    def test_fallback_to_plugin_config(self, server: WebhookServer):
        """endpoint 未设置 render_mode 时回退到插件配置。"""
        ep = _make_endpoint(render_mode="")
        # 插件默认是 text
        assert server._get_render_mode(ep) == "text"

    def test_plugin_html_image_mode(self):
        """插件默认 html_image 应生效（endpoint 未覆盖时）。"""
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={"render_mode": "html_image"},
        )
        ep = _make_endpoint(render_mode="")
        assert srv._get_render_mode(ep) == "html_image"


# ─── _get_fallback_to_text ─────────────────────────────────


class TestGetFallbackToText:
    def test_default_true(self, server: WebhookServer):
        assert server._get_fallback_to_text() is True

    def test_explicit_false(self):
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={"fallback_to_text": False},
        )
        assert srv._get_fallback_to_text() is False


# ─── _get_render_options ───────────────────────────────────


class TestGetRenderOptions:
    def test_parses_json_string(self, server: WebhookServer):
        opts = server._get_render_options()
        assert opts is not None
        assert opts.get("full_page") is True
        assert opts.get("type") == "png"

    def test_dict_direct(self):
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={"render_options": {"full_page": False}},
        )
        opts = srv._get_render_options()
        assert opts == {"full_page": False}

    def test_empty_returns_none(self):
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={},
        )
        assert srv._get_render_options() is None


# ─── _build_render_response ────────────────────────────────


class TestBuildRenderResponse:
    def test_success_response(self):
        resp = WebhookServer._build_render_response(
            request_id="req-001",
            provider="omp",
            event_name="omp.session_stop",
            render_mode="html_image",
            requested_render_mode="html_image",
            fallback_to_text=True,
            fallback_reason=None,
            send_results=[{"name": "default", "ok": True, "error": None}],
        )
        data = json.loads(resp.body)
        assert data["code"] == 0
        assert data["data"]["render_mode"] == "html_image"
        assert data["data"]["fallback_to_text"] is True
        assert data["data"]["fallback_reason"] is None
        assert data["data"]["delivered"] is True

    def test_partial_failure(self):
        resp = WebhookServer._build_render_response(
            request_id="req-002",
            provider="omp",
            event_name="omp.session_stop",
            render_mode="text",
            requested_render_mode="html_image",
            fallback_to_text=True,
            fallback_reason="image_validation_failed",
            send_results=[
                {"name": "default", "ok": False, "error": "session_not_found"}
            ],
        )
        data = json.loads(resp.body)
        assert data["code"] == 0
        assert data["message"] == "partial_failure"
        assert data["data"]["delivered"] is False
        assert data["data"]["fallback_to_text"] is True
        assert data["data"]["fallback_reason"] == "image_validation_failed"
        assert "send_results" in data["data"]


# ─── _handle_text ──────────────────────────────────────────


@pytest.mark.asyncio
class TestHandleText:
    async def test_text_success(self, server: WebhookServer):
        event = _make_event()
        endpoint = _make_endpoint(render_mode="text")
        resp = await server._handle_text(
            event, endpoint, target_alias=None, request_id="req-txt-001"
        )
        data = json.loads(resp.body)
        assert data["code"] == 0
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["delivered"] is True
        assert data["data"]["requested_render_mode"] == "text"
        assert data["data"]["fallback_to_text"] is False

    async def test_text_response_format(self, server: WebhookServer):
        """验证响应中包含 render_mode/requested_render_mode/fallback 字段。"""
        event = _make_event()
        endpoint = _make_endpoint(render_mode="text")
        resp = await server._handle_text(
            event, endpoint, target_alias=None, request_id="req-txt-002"
        )
        data = json.loads(resp.body)
        d = data["data"]
        assert "render_mode" in d
        assert "requested_render_mode" in d
        assert "fallback_to_text" in d
        assert "fallback_reason" in d
        assert d["render_mode"] == "text"
        assert d["requested_render_mode"] == "text"
        assert d["fallback_reason"] is None


# ─── _handle_html_image ────────────────────────────────────


@pytest.mark.asyncio
class TestHandleHtmlImage:
    async def test_html_image_success(self, server: WebhookServer):
        """HTML 图片模式成功时应返回 html_image render_mode。"""
        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")
        resp = await server._handle_html_image(
            event,
            endpoint,
            target_alias=None,
            request_id="req-html-001",
            fallback_to_text=True,
        )
        data = json.loads(resp.body)
        assert data["code"] == 0
        assert data["data"]["render_mode"] == "html_image"
        assert data["data"]["requested_render_mode"] == "html_image"
        assert data["data"]["fallback_to_text"] is True
        assert data["data"]["fallback_reason"] is None
        assert data["data"]["delivered"] is True

    async def test_html_render_failure_with_fallback(self, server: WebhookServer):
        """html_render 截图失败时应降级为 text。"""

        # 替换 html_render 为失败版本
        async def failing_render(tmpl, data, return_url=True, options=None):
            raise RuntimeError("T2I service unavailable")

        server._html_render = failing_render

        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")
        resp = await server._handle_html_image(
            event,
            endpoint,
            target_alias=None,
            request_id="req-html-002",
            fallback_to_text=True,
        )
        data = json.loads(resp.body)
        assert data["code"] == 0
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["requested_render_mode"] == "html_image"
        assert data["data"]["fallback_to_text"] is True
        assert data["data"]["fallback_reason"] == "html_render_failed"
        # 降级后应发送纯文本
        assert len(server._sender.sent_texts) >= 1
        assert "会话完成" in server._sender.sent_texts[0]

    async def test_html_render_failure_no_fallback(self, server: WebhookServer):
        """fallback 关闭时 html_render 失败应返回 500。"""

        async def failing_render(tmpl, data, return_url=True, options=None):
            raise RuntimeError("T2I service unavailable")

        server._html_render = failing_render

        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")
        resp = await server._handle_html_image(
            event,
            endpoint,
            target_alias=None,
            request_id="req-html-003",
            fallback_to_text=False,
        )
        data = json.loads(resp.body)
        assert data["code"] == 1  # error
        assert resp.status == 500
        assert "render_failed" in data["data"].get("error", "")

    async def test_no_html_render_callback(self, server: WebhookServer):
        """html_render 回调未设置时应降级或报错。"""
        server._html_render = None

        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")
        resp = await server._handle_html_image(
            event,
            endpoint,
            target_alias=None,
            request_id="req-html-004",
            fallback_to_text=True,
        )
        data = json.loads(resp.body)
        assert data["code"] == 0
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["fallback_reason"] == "html_render_not_available"

    async def test_image_validation_failure_with_fallback(self, server: WebhookServer):
        """图片校验失败时应降级。"""

        async def return_invalid_image(tmpl, data, return_url=True, options=None):
            return b"\x00\x00\x00\x00"  # invalid image bytes

        server._html_render = return_invalid_image

        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")
        resp = await server._handle_html_image(
            event,
            endpoint,
            target_alias=None,
            request_id="req-html-005",
            fallback_to_text=True,
        )
        data = json.loads(resp.body)
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["fallback_reason"] == "image_validation_failed"

    async def test_send_image_delivery_failure(self, server: WebhookServer):
        """发送图片但目标不可达时，render_mode 仍为 html_image，delivered=False。"""
        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")
        server._sender.set_fail_send(True)
        resp = await server._handle_html_image(
            event,
            endpoint,
            target_alias=None,
            request_id="req-html-006",
            fallback_to_text=True,
        )
        data = json.loads(resp.body)
        # 非异常发送失败不触发降级
        assert data["data"]["render_mode"] == "html_image"
        assert data["data"]["delivered"] is False
        assert "send_results" in data["data"]

    async def test_send_image_exception_with_fallback(self, server: WebhookServer):
        """发送图片抛出异常时应降级为 text。"""
        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")

        async def raise_send(*args, **kwargs):
            raise RuntimeError("send crashed")

        server._sender.send_image = raise_send  # type: ignore[assignment]

        resp = await server._handle_html_image(
            event,
            endpoint,
            target_alias=None,
            request_id="req-html-007",
            fallback_to_text=True,
        )
        data = json.loads(resp.body)
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["fallback_reason"] == "send_image_failed"
        # 降级文本发送成功
        assert data["data"]["delivered"] is True

    async def test_text_mode_still_works(self, server: WebhookServer):
        """text 模式仍应正常工作不受 html_image 影响。"""
        event = _make_event()
        endpoint = _make_endpoint(render_mode="text")
        resp = await server._handle_text(
            event, endpoint, target_alias=None, request_id="req-txt-003"
        )
        data = json.loads(resp.body)
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["delivered"] is True


# ─── _fallback_to_text ─────────────────────────────────────


@pytest.mark.asyncio
class TestFallbackToText:
    async def test_fallback_response_format(self, server: WebhookServer):
        """降级响应应包含完整 fallback 标记。"""
        event = _make_event()
        endpoint = _make_endpoint(render_mode="html_image")
        resp = await server._fallback_to_text(
            event,
            endpoint,
            target_alias=None,
            request_id="req-fb-001",
            fallback_reason="template_render_failed",
        )
        data = json.loads(resp.body)
        assert data["code"] == 0
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["requested_render_mode"] == "html_image"
        assert data["data"]["fallback_to_text"] is True
        assert data["data"]["fallback_reason"] == "template_render_failed"
