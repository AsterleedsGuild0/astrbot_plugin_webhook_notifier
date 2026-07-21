"""Server tests - html_image flow."""

from __future__ import annotations

import json

import pytest
from astrbot.api.star import Context

from core.models import EndpointRecord, NormalizedEvent, ServerConfig, TargetAlias
from core.omp import OmpProviderAdapter
from core.providers import ProviderRegistry
from core.registry import EndpointRegistry
from core.sender import Sender
from core.server import DEFAULT_RENDER_OPTIONS, WebhookServer
from core.template_registry import TemplateRegistry


def _make_provider_registry() -> ProviderRegistry:
    """创建一个含 OMP adapter 的 ProviderRegistry（测试用工厂）。"""
    reg = ProviderRegistry()
    reg.register(OmpProviderAdapter())
    reg.freeze()
    return reg


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


def _make_endpoint() -> EndpointRecord:
    return EndpointRecord(
        name="test_ep",
        path="u/hash/test_ep",
        provider="omp",
        token_hash="abc123",
        token_hash_algorithm="hmac-sha256",
        owner_user_id="user_001",
        owner_platform_id="aiocqhttp",
        targets=[TargetAlias(name="default", umo="test:Platform:Message:1")],
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
        self._enable_private_notifications = True

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

    def authenticate_delivery(self, path: str, authorization_header: str | None):
        from core.models import DeliveryAuthentication

        return DeliveryAuthentication(True, None, "ok", _make_endpoint())


class RequestStub:
    def __init__(self, path: str, token: str) -> None:
        self.content_type = "application/json"
        self.content_length = 2
        self.path = path
        self.headers = {"Authorization": f"Bearer {token}"}

    async def read(self) -> bytes:
        return b'{"event": "omp.session_stop"}'


class HeaderRequestStub(RequestStub):
    def __init__(self, path: str, authorization: str | None) -> None:
        super().__init__(path, "unused")
        self.headers = {} if authorization is None else {"Authorization": authorization}


@pytest.mark.asyncio
async def test_server_uses_single_registry_authentication_api():
    class AtomicAuthRegistry:
        def __init__(self):
            self.calls = []

        def authenticate_delivery(self, path, token):
            from core.models import DeliveryAuthentication

            self.calls.append((path, token))
            return DeliveryAuthentication(True, None, "ok", _make_endpoint())

        def __getattr__(self, name):
            raise AssertionError(f"Server 不得调用二次 Registry 查询: {name}")

    registry = AtomicAuthRegistry()
    # 注入含 OMP adapter 的 ProviderRegistry，断言正常 200 响应
    srv = WebhookServer(
        ServerConfig(),
        registry,
        FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=_make_provider_registry(),
    )  # type: ignore[arg-type]
    resp = await srv._process_request(
        RequestStub("/webhook/u/hash/test_ep", "token"),  # type: ignore[arg-type]
        "atomic-auth",
    )
    import json

    body = json.loads(resp.body)
    assert registry.calls == [("u/hash/test_ep", "Bearer token")]
    assert resp.status == 200
    assert body.get("code") == 0


@pytest.fixture
def server() -> WebhookServer:
    config = ServerConfig()
    registry = FakeRegistry()
    sender = FakeSender()
    provider_registry = _make_provider_registry()

    async def fake_html_render(
        tmpl: str, data: dict, return_url: bool = True, options: dict | None = None
    ) -> str | bytes:
        # html_image 模式应传 return_url=False 获取本地路径
        assert return_url is False, "预期 return_url=False"
        # 返回一个真实的极小 PNG 文件路径
        import tempfile
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (812, 400), (255, 255, 255))  # 背景
        draw = ImageDraw.Draw(img)
        draw.rectangle([16, 16, 796, 384], fill=(255, 255, 255))  # 卡片区
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            img.save(tmp_path, format="PNG")
        return tmp_path

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
        provider_registry=provider_registry,
    )


@pytest.mark.asyncio
async def test_admin_revoke_immediately_blocks_old_token(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        name="revoked-via-admin",
        owner_user_id="owner-admin-test",
        target_umo="test:Platform:Message:1",
    )
    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        html_render=None,
        plugin_config={"render_mode": "text"},
    )

    success, _ = registry.revoke_endpoint_by_path(record.path)
    response = await srv._process_request(
        RequestStub(f"/webhook/{record.path}", token),  # type: ignore[arg-type]
        "request-after-admin-revoke",
    )
    payload = json.loads(response.body)

    assert success is True
    assert response.status == 403
    assert payload["data"]["error"] == "endpoint_revoked"


@pytest.mark.asyncio
async def test_admin_revoke_owner_immediately_blocks_old_token(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        name="revoked-via-owner",
        owner_user_id="owner-admin-test",
        target_umo="test:Platform:Message:1",
    )
    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        html_render=None,
        plugin_config={"render_mode": "text"},
    )

    success, _ = registry.revoke_endpoint_by_owner_name(
        record.owner_platform_id, record.owner_user_id, record.name
    )
    response = await srv._process_request(
        RequestStub(f"/webhook/{record.path}", token),  # type: ignore[arg-type]
        "request-after-admin-owner-revoke",
    )
    payload = json.loads(response.body)

    assert success is True
    assert response.status == 403
    assert payload["data"]["error"] == "endpoint_revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization", "header_error"),
    [(None, "missing_authorization"), ("Basic abc", "invalid_token")],
)
@pytest.mark.parametrize("state", ["not-found", "revoked", "tokenless", "active"])
async def test_real_registry_header_error_priority_matrix(
    tmp_path, authorization, header_error, state
):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        "aiocqhttp", "owner", "matrix", "aiocqhttp:FriendMessage:1"
    )
    path = record.path
    if state == "not-found":
        path = "missing"
    elif state == "revoked":
        registry.revoke_endpoint("aiocqhttp", "owner", "matrix")
    elif state == "tokenless":
        registry._records[next(iter(registry._records))].token_hash = ""
    server = WebhookServer(
        ServerConfig(), registry, FakeSender(), plugin_config={"render_mode": "text"}
    )
    response = await server._process_request(
        HeaderRequestStub(f"/webhook/{path}", authorization),  # type: ignore[arg-type]
        f"matrix-{state}",
    )
    payload = json.loads(response.body)
    expected = {
        "not-found": (404, "not_found"),
        "revoked": (403, "endpoint_revoked"),
        "tokenless": (403, "token_unclaimed"),
        "active": (401, header_error),
    }[state]
    assert response.status == expected[0]
    assert payload["data"]["error"] == expected[1]
    assert token


# ─── _get_render_mode ──────────────────────────────────────


class TestGetRenderMode:
    def test_global_html_image(self):
        """全局 html_image 应返回 html_image。"""
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={"render_mode": "html_image"},
        )
        assert srv._get_render_mode() == "html_image"

    def test_global_text(self):
        """全局 text 应返回 text。"""
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={"render_mode": "text"},
        )
        assert srv._get_render_mode() == "text"

    def test_global_text_default(self, server: WebhookServer):
        """默认插件配置 text 应生效。"""
        assert server._get_render_mode() == "text"


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
        assert opts["full_page"] is False
        assert opts["viewport_width"] == DEFAULT_RENDER_OPTIONS["viewport_width"]
        assert opts["type"] == DEFAULT_RENDER_OPTIONS["type"]

    def test_empty_returns_default_options(self):
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={},
        )
        assert srv._get_render_options() == DEFAULT_RENDER_OPTIONS

    def test_invalid_json_returns_default_options(self):
        config = ServerConfig()
        reg = FakeRegistry()
        srv = WebhookServer(
            config=config,
            registry=reg,
            sender=FakeSender(),
            html_render=None,
            plugin_config={"render_options": "not-json"},
        )
        assert srv._get_render_options() == DEFAULT_RENDER_OPTIONS


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
        assert data["data"]["targets"] == ["default"]
        assert all(isinstance(name, str) for name in data["data"]["targets"])

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
        assert data["data"]["targets"] == ["default"]
        assert data["data"]["retryable"] is True
        assert "send_results" in data["data"]
        assert data["data"]["send_results"][0]["error"] == "session_not_found"

    def test_skipped_and_partial_delivery_responses(self):
        skipped = {
            "name": "private",
            "ok": True,
            "skipped": True,
            "error": None,
            "reason": "private_notifications_disabled",
        }
        resp = WebhookServer._build_render_response(
            "req-skip",
            "omp",
            "omp.session_stop",
            "text",
            "text",
            False,
            None,
            [skipped],
            rendered=False,
        )
        data = json.loads(resp.body)
        assert data["message"] == "skipped"
        assert data["data"]["delivered"] is False
        assert data["data"]["skipped"] is True
        assert data["data"]["retryable"] is False
        assert data["data"]["rendered"] is False
        assert data["data"]["targets"] == ["private"]
        assert data["data"]["skip_reason"] == "private_notifications_disabled"
        assert data["data"]["send_results"] == [skipped]

        resp = WebhookServer._build_render_response(
            "req-partial",
            "omp",
            "omp.session_stop",
            "text",
            "text",
            False,
            None,
            [skipped, {"name": "group", "ok": True, "error": None}],
        )
        data = json.loads(resp.body)
        assert data["message"] == "partial_delivery"
        assert data["data"]["delivered"] is True
        assert data["data"]["skipped"] is True
        assert data["data"]["retryable"] is False
        assert data["data"]["targets"] == ["private", "group"]
        assert data["data"]["skip_reason"] == "private_notifications_disabled"


@pytest.mark.asyncio
class TestPrivatePolicyPreflight:
    @pytest.mark.parametrize("render_mode", ["text", "html_image"])
    async def test_all_private_skips_before_rendering(
        self, render_mode: str, monkeypatch
    ):
        ctx = Context()
        sender = Sender(ctx)
        html_calls = 0
        info_logs: list[str] = []

        async def html_render(*args, **kwargs):
            nonlocal html_calls
            html_calls += 1
            raise AssertionError("html_render must not be called")

        def text_render(*args, **kwargs):
            raise AssertionError("text renderer must not be called")

        monkeypatch.setattr("core.server.render_text_default", text_render)
        monkeypatch.setattr("core.server.logger.info", info_logs.append)
        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            html_render,
            {"render_mode": render_mode, "fallback_to_text": True},
        )
        endpoint = _make_endpoint()
        endpoint.name = "sensitive-endpoint-name"
        endpoint.path = "sensitive/endpoint/path"
        endpoint.owner_user_id = "sensitive-owner-id"
        endpoint.token_hash = "sensitive-token-hash"
        endpoint.targets = [
            TargetAlias(
                name="sensitive-target-one",
                umo="aiocqhttp:FriendMessage:sensitive-openid-one",
            ),
            TargetAlias(
                name="sensitive-target-two",
                umo="qqofficial:FriendMessage:sensitive-openid-two",
            ),
        ]

        request_id = f"req-{render_mode}"
        response = await server._dispatch_event(
            _make_event(), endpoint, None, request_id
        )
        data = json.loads(response.body)

        assert response.status == 200
        assert data["message"] == "skipped"
        assert data["data"]["skipped"] is True
        assert data["data"]["skip_reason"] == "private_notifications_disabled"
        assert data["data"]["retryable"] is False
        assert data["data"]["rendered"] is False
        assert html_calls == 0
        assert ctx.get_last_sent() is None

        # _dispatch_event 新增 event.provider/endpoint.provider 日志，取最后一条验证跳过消息
        assert len(info_logs) >= 1
        log = next(
            (l for l in reversed(info_logs) if "result=skipped" in l),
            info_logs[-1],
        )
        assert log.startswith("[WebhookNotifier] ")
        assert f"request_id={request_id}" in log
        assert "provider=omp" in log
        assert "event=omp.session_stop" in log
        assert "result=skipped" in log
        assert "reason=private_notifications_disabled" in log
        assert "skipped_target_count=2" in log
        assert "rendered=false" in log
        # _dispatch_event 日志包含 event.provider 但不包含敏感标记
        dispatch_log = info_logs[0]
        assert "event.provider=omp" in dispatch_log
        assert "endpoint.provider=omp" in dispatch_log
        for sensitive_marker in (
            endpoint.name,
            endpoint.path,
            endpoint.owner_user_id,
            endpoint.token_hash,
            "sensitive-target-one",
            "sensitive-target-two",
            "aiocqhttp:FriendMessage:sensitive-openid-one",
            "qqofficial:FriendMessage:sensitive-openid-two",
            "sensitive-openid-one",
            "sensitive-openid-two",
        ):
            assert sensitive_marker not in log

    async def test_mixed_targets_render_and_return_partial_delivery(self, monkeypatch):
        ctx = Context()
        info_logs: list[str] = []
        monkeypatch.setattr("core.server.logger.info", info_logs.append)
        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            Sender(ctx),
            plugin_config={"render_mode": "text"},
        )
        endpoint = _make_endpoint()
        endpoint.targets = [
            TargetAlias(name="private", umo="aiocqhttp:FriendMessage:10001"),
            TargetAlias(name="group", umo="aiocqhttp:GroupMessage:20001"),
        ]

        response = await server._dispatch_event(
            _make_event(), endpoint, None, "req-mixed"
        )
        data = json.loads(response.body)

        assert data["message"] == "partial_delivery"
        assert data["data"]["delivered"] is True
        assert data["data"]["skipped"] is True
        assert ctx.get_last_sent()[0] == "aiocqhttp:GroupMessage:20001"
        assert not any("result=skipped" in log for log in info_logs)

    async def test_normal_delivery_does_not_log_all_skipped(self, monkeypatch):
        info_logs: list[str] = []
        monkeypatch.setattr("core.server.logger.info", info_logs.append)
        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            FakeSender(),
            plugin_config={"render_mode": "text"},
        )

        response = await server._dispatch_event(
            _make_event(), _make_endpoint(), None, "req-normal"
        )
        data = json.loads(response.body)

        assert data["message"] == "ok"
        assert data["data"]["delivered"] is True
        assert not any("result=skipped" in log for log in info_logs)


# ─── _handle_text ──────────────────────────────────────────


@pytest.mark.asyncio
class TestHandleText:
    async def test_text_success(self, server: WebhookServer):
        event = _make_event()
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()
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
        endpoint = _make_endpoint()

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
        endpoint = _make_endpoint()
        resp = await server._handle_text(
            event, endpoint, target_alias=None, request_id="req-txt-003"
        )
        data = json.loads(resp.body)
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["delivered"] is True

    async def test_custom_width_and_second_jinja_is_not_executed(self, tmp_path):
        registry = TemplateRegistry(tmp_path)
        registry.save(
            None,
            "Custom",
            "<html><body>{{ event.title }}</body></html>",
            1000,
            apply=True,
        )
        calls = []

        async def capture_render(tmpl, data, return_url=True, options=None):
            calls.append((tmpl, data, options))
            return b"\x89PNG\r\n\x1a\nimage"

        event = _make_event()
        event.title = "literal {{ 7 * 7 }}"
        srv = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            FakeSender(),
            capture_render,
            {"render_mode": "html_image"},
            registry,
        )
        response = await srv._handle_html_image(
            event, _make_endpoint(), None, "req-custom", True
        )
        assert json.loads(response.body)["data"]["render_mode"] == "html_image"
        assert calls[0][0] == "{{ rendered_html | safe }}"
        assert "literal {{ 7 * 7 }}" in calls[0][1]["rendered_html"]
        assert calls[0][2]["viewport_width"] == 1000

    async def test_custom_failure_retries_builtin_before_text(self, tmp_path):
        registry = TemplateRegistry(tmp_path)
        registry.save(
            None,
            "Broken at render",
            "<html><body>{{ 1 / 0 }}</body></html>",
            700,
            apply=True,
        )
        calls = []

        async def capture_render(tmpl, data, return_url=True, options=None):
            calls.append(options["viewport_width"])
            return b"\x89PNG\r\n\x1a\nimage"

        sender = FakeSender()
        srv = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
            registry,
        )
        response = await srv._handle_html_image(
            _make_event(), _make_endpoint(), None, "req-retry", True
        )
        assert json.loads(response.body)["data"]["render_mode"] == "html_image"
        assert calls == [812]
        assert len(sender.sent_images) == 1
        assert sender.sent_texts == []


# ─── _fallback_to_text ─────────────────────────────────────


@pytest.mark.asyncio
class TestFallbackToText:
    async def test_fallback_response_format(self, server: WebhookServer):
        """降级响应应包含完整 fallback 标记。"""
        event = _make_event()
        endpoint = _make_endpoint()
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


# ─── 全链路集成测试 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_integration_real_registry_full_omp_flow(tmp_path):
    """真实 EndpointRegistry+Bearer auth+真实 OMP payload+ProviderRegistry，验证 200。

    同时验证 error response 中包含 retryable 字段。
    """
    import json

    from core.registry import EndpointRegistry

    # 1. 创建真实 EndpointRegistry 并创建 omp endpoint
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        owner_user_id="integration-test",
        name="integration-omp",
        target_umo="aiocqhttp:GroupMessage:10001",
        provider="omp",
    )

    # 2. 创建 ProviderRegistry + OMP adapter
    provider_reg = ProviderRegistry()
    provider_reg.register(OmpProviderAdapter())
    provider_reg.freeze()

    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=provider_reg,
    )

    # 3. 发送合法 OMP payload
    request_id = "integration-test-001"
    response = await srv._process_request(
        _make_auth_request(record.path, token),  # type: ignore[arg-type]
        request_id,
    )
    body = json.loads(response.body)
    assert response.status == 200, f"预期 200，得到 {response.status}: {body}"
    assert body["code"] == 0
    assert body["data"]["delivered"] is True
    assert body["data"]["request_id"] == request_id
    assert body["data"]["provider"] == "omp"
    # retryable 字段在成功响应中也应有（由 _build_render_response 设置）
    assert "retryable" in body["data"]


@pytest.mark.asyncio
async def test_integration_provider_unavailable_returns_500_retryable(tmp_path):
    """真实 EndpointRegistry + 未注册 provider 应返回 500 retryable=true。"""
    import json

    from core.registry import EndpointRegistry

    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        owner_user_id="int-unreg",
        name="unreg",
        target_umo="aiocqhttp:GroupMessage:10001",
        provider="unregistered_provider",
    )

    # 使用空 ProviderRegistry（无任何 adapter）
    empty_reg = ProviderRegistry()
    empty_reg.freeze()

    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=empty_reg,
    )

    response = await srv._process_request(
        _make_auth_request(record.path, token),  # type: ignore[arg-type]
        "int-unreg",
    )
    body = json.loads(response.body)
    assert response.status == 500
    assert body["code"] == 1
    assert body["data"]["error"] == "provider_unavailable"
    assert body["data"]["retryable"] is True


@pytest.mark.asyncio
async def test_integration_error_response_includes_retryable(tmp_path):
    """验证各种错误响应中均包含 retryable 字段且默认为 false。"""
    import json

    from core.registry import EndpointRegistry

    registry = EndpointRegistry(tmp_path)
    record, token1 = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        owner_user_id="int-err",
        name="err-test",
        target_umo="aiocqhttp:GroupMessage:10001",
        provider="omp",
    )

    provider_reg = ProviderRegistry()
    provider_reg.register(OmpProviderAdapter())
    provider_reg.freeze()

    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=provider_reg,
    )

    # 错误的 Content-Type 应返回 415，retryable=false
    req = _make_auth_request(record.path, token1)
    req.content_type = "text/plain"
    response = await srv._process_request(req, "err-415")  # type: ignore[arg-type]
    body = json.loads(response.body)
    assert response.status == 415
    assert body["data"]["retryable"] is False


class _AuthRequest:
    """携带合法 Authorization 和合法 OMP body 的请求 stub。"""

    def __init__(self, path: str, token: str, body_bytes: bytes | None = None) -> None:
        self.content_type = "application/json"
        self.content_length = len(body_bytes) if body_bytes else 2
        self.path = path
        self.headers = {"Authorization": f"Bearer {token}"}
        self._body = body_bytes or b'{"event": "omp.session_stop"}'

    async def read(self) -> bytes:
        return self._body


def _make_auth_request(path: str, token: str) -> _AuthRequest:
    return _AuthRequest(path, token)
