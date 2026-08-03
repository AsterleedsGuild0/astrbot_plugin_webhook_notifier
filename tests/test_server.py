"""Server tests - html_image flow."""

from __future__ import annotations

import json
import asyncio
from collections.abc import Sequence

import pytest
from astrbot.api.star import Context

from core.idempotency import IdempotencyStore
from core.models import EndpointRecord, NormalizedEvent, ServerConfig, TargetAlias
from core.omp import OmpProviderAdapter
from core.opencode import OpenCodeProviderAdapter
from core.providers import ProviderRegistry
from core.registry import EndpointRegistry
from core.renderer import render_subagent_timeline
from core.sender import Sender
from core.server import DEFAULT_RENDER_OPTIONS, WebhookServer
from core.template_registry import TemplateRegistry
from core.notification_policy import SessionScope


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


def _make_complex_event(
    item_count: int = 6, *, long_names: bool = False
) -> NormalizedEvent:
    """构造经过 provider 约束的复杂 root timeline fixture。"""
    root_ref = "a" * 32
    items = []
    for index in range(item_count):
        start = index * 1000
        end = start + 1800 if index < 2 else start + 800
        items.append(
            {
                "ref": f"{index + 1:032x}",
                "parentRef": root_ref,
                "status": "completed" if index != item_count - 1 else "failed",
                "timingQuality": "observed",
                "depth": 3 if index == item_count - 1 else 1,
                "attempt": 1,
                "name": (
                    f"worker-{index + 1}-" + "long-unbroken-name" * 8
                    if long_names
                    else f"worker-{index + 1}"
                ),
                "agent": "gpt-5.5",
                "startOffsetMs": start,
                "endOffsetMs": end,
                "durationMs": end - start,
            }
        )
    event = _make_event()
    event.event = "opencode.session_idle"
    event.session_scope = SessionScope.ROOT
    event.subagent_timeline = {
        "version": 1,
        "partial": False,
        "partialReasons": [],
        "timeBasis": "root_cycle",
        "observedItemCount": len(items),
        "displayedItemCount": len(items),
        "truncated": False,
        "items": items,
    }
    return event


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
        self.sent_image_batches: list[list[str | bytes]] = []
        self.delivered_image_batches: list[list[str | bytes]] = []
        self.send_image_calls = 0
        self.send_images_calls = 0
        self._fail_send: bool = False
        self._unsupported_image_batch: bool = False
        self._enable_private_notifications = True

    def set_fail_send(self, fail: bool = True) -> None:
        self._fail_send = fail

    def set_unsupported_image_batch(self, unsupported: bool = True) -> None:
        self._unsupported_image_batch = unsupported

    async def send_text(
        self,
        text: str,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback=None,
    ) -> list[dict]:
        self.sent_texts.append(text)
        if delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        if self._fail_send:
            return [{"name": "default", "ok": False, "error": "simulated_failure"}]
        return [{"name": "default", "ok": True, "error": None}]

    async def send_image(
        self,
        image_result: str | bytes,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback=None,
    ) -> list[dict]:
        self.send_image_calls += 1
        self.sent_images.append(image_result)
        self.sent_image_batches.append([image_result])
        if delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        if self._fail_send:
            return [{"name": "default", "ok": False, "error": "simulated_failure"}]
        self.delivered_image_batches.append([image_result])
        return [{"name": "default", "ok": True, "error": None}]

    async def send_images(
        self,
        image_results: Sequence[str | bytes],
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback=None,
    ) -> list[dict]:
        self.send_images_calls += 1
        batch = list(image_results)
        self.sent_images.extend(batch)
        self.sent_image_batches.append(batch)
        if self._unsupported_image_batch:
            return [
                {
                    "name": None,
                    "ok": False,
                    "error": "unsupported_image_result",
                }
            ]
        if delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        if self._fail_send:
            return [{"name": "default", "ok": False, "error": "simulated_failure"}]
        self.delivered_image_batches.append(batch)
        return [{"name": "default", "ok": True, "error": None}]


class BoundarySender(FakeSender):
    """支持发送边界回调的 sender，用于 server 并发/终态测试。"""

    def __init__(self) -> None:
        super().__init__()
        self.release_send = asyncio.Event()
        self.block_send = False

    async def send_text(
        self,
        text: str,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback=None,
    ) -> list[dict]:
        self.sent_texts.append(text)
        if delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        if self.block_send:
            await self.release_send.wait()
        return [{"name": "default", "ok": True, "error": None}]


class ControlledSender:
    """可控地在发送边界前后阻塞、抛错或返回 False。"""

    def __init__(
        self,
        *,
        mark_before_wait: bool = False,
        wait_first: bool = False,
        fail: bool = False,
        raise_error: bool = False,
    ) -> None:
        self.mark_before_wait = mark_before_wait
        self.wait_first = wait_first
        self.fail = fail
        self.raise_error = raise_error
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    def preflight_private_notification_policy(self, *_args, **_kwargs):
        return None

    async def send_text(
        self,
        _text,
        _endpoint,
        _target_alias=None,
        delivery_attempt_callback=None,
    ):
        self.calls += 1
        self.entered.set()
        if self.mark_before_wait and delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        if self.raise_error and not self.mark_before_wait:
            raise RuntimeError("simulated send exception")
        if self.wait_first and self.calls == 1:
            await self.release.wait()
        if not self.mark_before_wait and delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        if self.raise_error:
            raise RuntimeError("simulated send exception")
        if self.fail:
            return [{"name": "default", "ok": False, "error": "simulated_failure"}]
        return [{"name": "default", "ok": True, "error": None}]


class CallbackIncompatibleSender:
    """故意不支持发送边界 callback，证明 server 不会无 tracker 降级调用。"""

    def __init__(self) -> None:
        self.calls = 0

    def preflight_private_notification_policy(self, *_args, **_kwargs):
        return None

    async def send_text(self, _text, _endpoint, _target_alias=None):
        self.calls += 1
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


class TestDisplayTimezone:
    @pytest.mark.asyncio
    async def test_server_uses_configured_timezone_for_text_rendering(self):
        sender = FakeSender()
        srv = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            plugin_config={"render_mode": "text", "display_timezone": "UTC"},
        )
        event = _make_event()
        event.fields.append({"label": "startedAt", "value": "2026-07-24T01:44:35Z"})
        response = await srv._handle_text(
            event, _make_endpoint(), None, "timezone-text"
        )
        assert response.status == 200
        assert "2026-07-24 01:44:35 UTC (UTC+00:00)" in sender.sent_texts[0]

    def test_server_invalid_timezone_warns_without_config_value(self, monkeypatch):
        warnings: list[str] = []
        monkeypatch.setattr("core.server.logger.warning", warnings.append)
        secret_config = "Private/Server-Timezone"
        srv = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            FakeSender(),
            plugin_config={"display_timezone": secret_config},
        )
        assert srv._display_context.timezone_name == "Asia/Shanghai"
        assert warnings == [
            "[WebhookNotifier] display_timezone 无效，已回退到 Asia/Shanghai"
        ]
        assert secret_config not in warnings[0]


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
            (entry for entry in reversed(info_logs) if "result=skipped" in entry),
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

    async def test_html_render_failure_timeline_fallback_keeps_wait_and_residual(
        self, server: WebhookServer
    ):
        async def failing_render(tmpl, data, return_url=True, options=None):
            raise RuntimeError("T2I service unavailable")

        server._html_render = failing_render
        event = _make_complex_event()
        event.task_duration_ms = 10_000
        event.user_wait_timeline = {
            "version": 1,
            "timeBasis": "root_cycle_receipt_monotonic",
            "partial": False,
            "partialReasons": [],
            "observedIntervalCount": 1,
            "displayedIntervalCount": 1,
            "truncated": False,
            "intervals": [
                {
                    "kind": "question",
                    "result": "replied",
                    "intervalState": "complete",
                    "startOffsetMs": 7000,
                    "endOffsetMs": 8000,
                    "durationMs": 1000,
                }
            ],
        }

        response = await server._handle_html_image(
            event,
            _make_endpoint(),
            target_alias=None,
            request_id="req-html-timeline-fallback",
            fallback_to_text=True,
        )
        data = json.loads(response.body)
        assert data["data"]["render_mode"] == "text"
        assert data["data"]["fallback_reason"] == "html_render_failed"
        assert server._sender.sent_texts
        fallback_text = server._sender.sent_texts[-1]
        assert "等待用户 1 秒 · 1 次" in fallback_text
        assert "观测完整 · Question 1" in fallback_text
        assert "未分类时间 / 占比：" in fallback_text

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
        assert server._sender.send_image_calls == 1
        assert server._sender.send_images_calls == 0
        assert server._sender.sent_texts == []

    async def test_send_image_exception_with_fallback(self, server: WebhookServer):
        """发送图片抛出异常时应降级为 text。"""
        event = _make_event()
        endpoint = _make_endpoint()
        send_calls = 0

        async def raise_send(*args, **kwargs):
            nonlocal send_calls
            send_calls += 1
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
        assert send_calls == 1
        assert server._sender.sent_texts

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

    async def test_simple_event_renders_once_and_sends_one_image(self):
        sender = FakeSender()
        render_calls = 0

        async def capture_render(tmpl, data, return_url=True, options=None):
            nonlocal render_calls
            render_calls += 1
            return b"\x89PNG\r\n\x1a\nmain"

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
        )
        response = await server._handle_html_image(
            _make_event(), _make_endpoint(), None, "req-simple", True
        )
        data = json.loads(response.body)

        assert response.status == 200
        assert data["data"]["render_mode"] == "html_image"
        assert render_calls == 1
        assert sender.send_image_calls == 1
        assert sender.send_images_calls == 0
        assert sender.sent_image_batches == [[b"\x89PNG\r\n\x1a\nmain"]]
        assert sender.sent_texts == []

    async def test_complex_event_renders_two_images_in_one_sender_call(self):
        sender = FakeSender()
        render_calls = 0

        async def capture_render(tmpl, data, return_url=True, options=None):
            nonlocal render_calls
            render_calls += 1
            return b"\x89PNG\r\n\x1a\nimage"

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
        )
        response = await server._handle_html_image(
            _make_complex_event(), _make_endpoint(), None, "req-complex", True
        )
        data = json.loads(response.body)

        assert response.status == 200
        assert data["data"]["render_mode"] == "html_image"
        assert render_calls == 2
        assert sender.send_images_calls == 1
        assert sender.send_image_calls == 0
        assert sender.sent_image_batches == [
            [b"\x89PNG\r\n\x1a\nimage", b"\x89PNG\r\n\x1a\nimage"]
        ]
        assert sender.sent_texts == []

    async def test_timeline_viewport_timeout_scale_and_trim_share_renderer_layout(
        self, monkeypatch
    ):
        event = _make_complex_event(49, long_names=True)
        rendered = render_subagent_timeline(event)
        assert rendered is not None
        captured_options = None
        trim_calls = []

        async def capture_render(tmpl, data, return_url=True, options=None):
            nonlocal captured_options
            captured_options = dict(options)
            return b"\x89PNG\r\n\x1a\nimage"

        def capture_trim(result, *, canvas_width, card_width, body_padding):
            trim_calls.append((canvas_width, card_width, body_padding))
            return result

        monkeypatch.setattr("core.server.trim_viewport_whitespace", capture_trim)
        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            FakeSender(),
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
        )
        result = await server._render_timeline_image_attempt(event, "req-layout")

        assert result == b"\x89PNG\r\n\x1a\nimage"
        assert captured_options is not None
        assert captured_options["viewport_width"] == rendered.layout.viewport_width
        assert captured_options["timeout"] == rendered.layout.render_timeout_ms
        assert captured_options["device_scale_factor_level"] == "normal"
        assert trim_calls == [
            (
                rendered.layout.viewport_width,
                rendered.layout.card_width,
                rendered.layout.body_padding,
            )
        ]

    @pytest.mark.parametrize("item_count", [48, 64])
    async def test_timeline_forces_full_page_and_dynamic_height_over_plugin_config(
        self, item_count
    ):
        event = _make_complex_event(item_count, long_names=item_count == 64)
        rendered = render_subagent_timeline(event)
        assert rendered is not None
        captured = []

        async def capture_render(tmpl, data, return_url=True, options=None):
            captured.append((dict(options), data["rendered_html"]))
            return b"\x89PNG\r\n\x1a\nimage"

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            FakeSender(),
            capture_render,
            {
                "render_mode": "html_image",
                "render_options": {
                    "full_page": False,
                    "viewport_height": 1200,
                    "timeout": 5000,
                },
            },
        )
        result = await server._render_timeline_image_attempt(event, "req-full-page")

        assert result == b"\x89PNG\r\n\x1a\nimage"
        options, html = captured[0]
        assert options["full_page"] is True
        assert options["viewport_height"] == rendered.layout.viewport_height
        assert options["viewport_height"] >= rendered.layout.estimated_height
        assert options["viewport_height"] > 1200
        assert html.count('<div class="row timeline-row"') == item_count

    async def test_strict_fake_renderer_png_matches_timeline_canvas(self, tmp_path):
        from PIL import Image, ImageDraw

        event = _make_complex_event(64, long_names=True)
        rendered = render_subagent_timeline(event)
        assert rendered is not None
        png_path = tmp_path / "timeline-canvas.png"
        captured_options = None

        async def strict_png_render(tmpl, data, return_url=True, options=None):
            nonlocal captured_options
            captured_options = dict(options)
            width = int(options["viewport_width"])
            height = int(options["viewport_height"])
            image = Image.new("RGB", (width, height), (255, 255, 255))
            draw = ImageDraw.Draw(image)
            draw.rectangle((16, 16, width - 16, height - 16), fill=(17, 25, 37))
            image.save(png_path, format="PNG")
            return str(png_path)

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            FakeSender(),
            strict_png_render,
            {"render_mode": "html_image", "render_options": {"full_page": False}},
        )
        result = await server._render_timeline_image_attempt(event, "req-png-canvas")

        assert result == str(png_path)
        assert captured_options is not None
        with Image.open(png_path) as image:
            assert image.size == (
                rendered.layout.viewport_width,
                rendered.layout.viewport_height,
            )
        expected_content_height = (
            rendered.layout.vertical_chrome_height
            + rendered.layout.located_rows_height
            + (
                60 + rendered.layout.unlocated_rows_height
                if rendered.layout.unlocated_rows_height
                else 0
            )
        )
        assert expected_content_height == rendered.layout.estimated_height
        assert captured_options["viewport_height"] >= expected_content_height
        assert captured_options["full_page"] is True

    async def test_production_double_image_path_builds_timeline_layout_once(
        self, monkeypatch
    ):
        from core import renderer

        event = _make_complex_event(25)
        original = renderer._build_subagent_timeline_view
        build_calls = 0

        def counted(current_event):
            nonlocal build_calls
            build_calls += 1
            return original(current_event)

        async def capture_render(tmpl, data, return_url=True, options=None):
            return b"\x89PNG\r\n\x1a\nimage"

        monkeypatch.setattr(renderer, "_build_subagent_timeline_view", counted)
        sender = FakeSender()
        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
        )
        response = await server._handle_html_image(
            event, _make_endpoint(), None, "req-prepared-layout", True
        )

        assert response.status == 200
        assert sender.send_images_calls == 1
        assert build_calls == 1

    async def test_custom_main_and_builtin_timeline_render_in_order(self, tmp_path):
        registry = TemplateRegistry(tmp_path)
        registry.save(
            None,
            "Custom",
            "<html><body>{{ event.title }}</body></html>",
            1000,
            apply=True,
        )
        sender = FakeSender()
        widths: list[int] = []

        async def capture_render(tmpl, data, return_url=True, options=None):
            widths.append(options["viewport_width"])
            return b"\x89PNG\r\n\x1a\nimage"

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
            registry,
        )
        response = await server._handle_html_image(
            _make_complex_event(), _make_endpoint(), None, "req-custom-complex", True
        )

        timeline = render_subagent_timeline(_make_complex_event())
        assert timeline is not None
        assert json.loads(response.body)["data"]["render_mode"] == "html_image"
        assert widths == [1000, timeline.layout.viewport_width]
        assert sender.send_images_calls == 1
        assert sender.sent_image_batches == [
            [b"\x89PNG\r\n\x1a\nimage", b"\x89PNG\r\n\x1a\nimage"]
        ]

    @pytest.mark.parametrize(
        "failure_stage, expected_calls, reason",
        [
            ("helper", 1, "timeline_template_render_failed"),
            ("html", 2, "timeline_html_render_failed"),
            ("validate", 2, "timeline_image_validation_failed"),
            ("trim", 2, "timeline_image_trim_failed"),
        ],
    )
    async def test_timeline_failure_keeps_main_image_only(
        self, monkeypatch, caplog, failure_stage, expected_calls, reason
    ):
        sender = FakeSender()
        render_calls = 0
        marker = "HTML <img> base64://secret /Volumes/private/ref-secret"

        async def capture_render(tmpl, data, return_url=True, options=None):
            nonlocal render_calls
            render_calls += 1
            if failure_stage == "html" and render_calls == 2:
                raise RuntimeError(marker)
            return b"\x89PNG\r\n\x1a\nimage"

        def failing_timeline(event):
            raise RuntimeError(marker)

        if failure_stage == "helper":
            monkeypatch.setattr(
                "core.server.render_subagent_timeline", failing_timeline
            )
        elif failure_stage == "validate":
            original_validate = __import__(
                "core.server", fromlist=["validate_image_result"]
            ).validate_image_result
            validation_calls = 0

            def fail_timeline_validation(result):
                nonlocal validation_calls
                validation_calls += 1
                if validation_calls == 2:
                    raise ValueError(marker)
                return original_validate(result)

            monkeypatch.setattr(
                "core.server.validate_image_result", fail_timeline_validation
            )
        elif failure_stage == "trim":
            trim_calls = 0

            def fail_timeline_trim(
                result, canvas_width=812, card_width=780, body_padding=16
            ):
                nonlocal trim_calls
                trim_calls += 1
                if trim_calls == 2:
                    raise RuntimeError(marker)
                return result

            monkeypatch.setattr(
                "core.server.trim_viewport_whitespace", fail_timeline_trim
            )

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
        )
        response = await server._handle_html_image(
            _make_complex_event(), _make_endpoint(), None, "req-timeline-failure", True
        )
        data = json.loads(response.body)

        assert response.status == 200
        assert data["data"]["render_mode"] == "html_image"
        assert data["data"]["fallback_reason"] is None
        assert render_calls == expected_calls
        assert sender.send_image_calls == 1
        assert sender.send_images_calls == 0
        assert sender.sent_texts == []
        assert sender.sent_image_batches == [[b"\x89PNG\r\n\x1a\nimage"]]
        assert any(f"reason={reason}" in record.message for record in caplog.records)
        assert not any(marker in record.message for record in caplog.records)

    @pytest.mark.parametrize(
        "failure_stage, expected_calls, reason",
        [
            ("template", 0, "template_render_failed"),
            ("html", 1, "html_render_failed"),
            ("validate", 1, "image_validation_failed"),
            ("trim", 1, "image_trim_failed"),
        ],
    )
    async def test_main_render_failures_are_safe_without_text_fallback(
        self, monkeypatch, caplog, failure_stage, expected_calls, reason
    ):
        sender = FakeSender()
        html_calls = 0
        marker = "HTML <img> base64://secret /Volumes/private/ref-secret"

        async def capture_render(tmpl, data, return_url=True, options=None):
            nonlocal html_calls
            html_calls += 1
            if failure_stage == "html":
                raise RuntimeError(marker)
            return b"\x89PNG\r\n\x1a\nimage"

        if failure_stage == "template":

            def fail_template(template, event):
                raise RuntimeError(marker)

            monkeypatch.setattr("core.server.render_html_template", fail_template)
        elif failure_stage == "validate":

            def fail_validate(result):
                raise ValueError(marker)

            monkeypatch.setattr("core.server.validate_image_result", fail_validate)
        elif failure_stage == "trim":

            def fail_trim(result, canvas_width=812):
                raise RuntimeError(marker)

            monkeypatch.setattr("core.server.trim_viewport_whitespace", fail_trim)

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": False},
        )
        response = await server._handle_html_image(
            _make_event(), _make_endpoint(), None, "req-main-failure", False
        )
        data = json.loads(response.body)

        assert response.status == 500
        assert data["code"] == 1
        assert data["data"]["error"] == "render_failed"
        assert marker not in response.text
        assert html_calls == expected_calls
        assert sender.send_image_calls == 0
        assert sender.send_images_calls == 0
        assert sender.sent_texts == []
        assert any(f"reason={reason}" in record.message for record in caplog.records)
        assert not any(marker in record.message for record in caplog.records)

    async def test_multi_image_construction_failure_sends_main_only(self):
        sender = FakeSender()
        sender.set_unsupported_image_batch()

        async def capture_render(tmpl, data, return_url=True, options=None):
            return b"\x89PNG\r\n\x1a\nimage"

        server = WebhookServer(
            ServerConfig(),
            FakeRegistry(),
            sender,
            capture_render,
            {"render_mode": "html_image", "fallback_to_text": True},
        )

        response = await server._handle_html_image(
            _make_complex_event(), _make_endpoint(), None, "req-build-failure", True
        )
        data = json.loads(response.body)

        assert response.status == 200
        assert data["data"]["render_mode"] == "html_image"
        assert sender.send_images_calls == 1
        assert sender.send_image_calls == 1
        assert sender.sent_image_batches == [
            [b"\x89PNG\r\n\x1a\nimage", b"\x89PNG\r\n\x1a\nimage"],
            [b"\x89PNG\r\n\x1a\nimage"],
        ]
        assert sender.delivered_image_batches == [[b"\x89PNG\r\n\x1a\nimage"]]


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

    def __init__(
        self,
        path: str,
        token: str,
        body_bytes: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.content_type = "application/json"
        self.content_length = len(body_bytes) if body_bytes else 2
        self.path = path
        self.headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            self.headers.update(extra_headers)
        self._body = body_bytes or b'{"event": "omp.session_stop"}'

    async def read(self) -> bytes:
        return self._body


def _make_auth_request(path: str, token: str) -> _AuthRequest:
    return _AuthRequest(path, token)


# ─── OpenCode 全链路集成测试 ──────────────────────────────


_VALID_OPENCODE_BODY = {
    "id": "evt_openc_test",
    "event": "opencode.session_idle",
    "version": 1,
    "emittedAt": "2026-07-22T12:00:00.000Z",
    "session": {"ref": "sess_secure_ref_abc"},
}


def _make_opencode_auth_request(
    path: str, token: str, event: str = "opencode.session_idle"
) -> _AuthRequest:
    body = dict(_VALID_OPENCODE_BODY)
    body["event"] = event
    return _AuthRequest(
        path,
        token,
        body_bytes=json.dumps(body).encode(),
        extra_headers={"X-OpenCode-Event": event},
    )


@pytest.mark.asyncio
async def test_integration_opencode_real_registry_full_flow(tmp_path):
    """真实 EndpointRegistry+Bearer auth+真实 V1 payload+ProviderRegistry(OMP+OC)，验证 200。"""
    from core.registry import EndpointRegistry

    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        owner_user_id="oc-integration",
        name="oc-test",
        target_umo="aiocqhttp:GroupMessage:10001",
        provider="opencode",
    )

    provider_reg = ProviderRegistry()
    provider_reg.register(OmpProviderAdapter())
    provider_reg.register(OpenCodeProviderAdapter())
    provider_reg.freeze()

    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=provider_reg,
    )

    response = await srv._process_request(
        _make_opencode_auth_request(record.path, token),  # type: ignore[arg-type]
        "oc-integration-001",
    )
    body = json.loads(response.body)
    assert response.status == 200, f"预期 200，得到 {response.status}: {body}"
    assert body["code"] == 0
    assert body["data"]["delivered"] is True
    assert body["data"]["provider"] == "opencode"
    assert body["data"]["request_id"] == "oc-integration-001"
    assert "retryable" in body["data"]


@pytest.mark.asyncio
async def test_integration_opencode_header_mismatch_400_retryable_false(tmp_path):
    """Header/Body event 不一致应返回 400 retryable=false。"""
    from core.registry import EndpointRegistry

    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        owner_user_id="oc-mismatch",
        name="oc-mismatch",
        target_umo="aiocqhttp:GroupMessage:10001",
        provider="opencode",
    )

    provider_reg = ProviderRegistry()
    provider_reg.register(OmpProviderAdapter())
    provider_reg.register(OpenCodeProviderAdapter())
    provider_reg.freeze()

    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=provider_reg,
    )

    bad_body = dict(_VALID_OPENCODE_BODY)
    bad_body["event"] = "opencode.session_error"
    req = _AuthRequest(
        record.path,
        token,
        body_bytes=json.dumps(bad_body).encode(),
        extra_headers={"X-OpenCode-Event": "opencode.session_idle"},  # 故意不一致
    )
    response = await srv._process_request(req, "oc-mismatch")  # type: ignore[arg-type]
    body = json.loads(response.body)
    assert response.status == 400
    assert body["data"]["error"] == "event_mismatch"
    assert body["data"]["retryable"] is False


@pytest.mark.asyncio
async def test_integration_opencode_payload_on_omp_endpoint_returns_incompatible(
    tmp_path,
):
    """opencode payload 发送到 omp endpoint 应返回 provider_incompatible。"""
    from core.registry import EndpointRegistry

    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        owner_user_id="oc-wrong-ep",
        name="oc-wrong-ep",
        target_umo="aiocqhttp:GroupMessage:10001",
        provider="omp",
    )

    provider_reg = ProviderRegistry()
    provider_reg.register(OmpProviderAdapter())
    provider_reg.register(OpenCodeProviderAdapter())
    provider_reg.freeze()

    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=provider_reg,
    )

    # OMP endpoint 收到 OpenCode payload
    oc_body = dict(_VALID_OPENCODE_BODY)
    req = _AuthRequest(
        record.path,
        token,
        body_bytes=json.dumps(oc_body).encode(),
    )
    response = await srv._process_request(req, "oc-wrong-ep")  # type: ignore[arg-type]
    body = json.loads(response.body)
    # OMP adapter 检测到 opencode. 前缀事件 → provider_incompatible
    assert response.status == 400
    assert body["data"]["error"] == "provider_incompatible"
    assert body["data"]["retryable"] is False


@pytest.mark.asyncio
async def test_integration_omp_payload_on_opencode_endpoint_returns_incompatible(
    tmp_path,
):
    """OMP payload 发送到 opencode endpoint 应返回 provider_incompatible。"""
    from core.registry import EndpointRegistry

    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        owner_user_id="oc-rev-test",
        name="oc-rev-test",
        target_umo="aiocqhttp:GroupMessage:10001",
        provider="opencode",
    )

    provider_reg = ProviderRegistry()
    provider_reg.register(OmpProviderAdapter())
    provider_reg.register(OpenCodeProviderAdapter())
    provider_reg.freeze()

    srv = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=FakeSender(),
        plugin_config={"render_mode": "text"},
        provider_registry=provider_reg,
    )

    # OpenCode endpoint 收到 OMP payload
    omp_body = {"event": "omp.session_stop"}
    req = _AuthRequest(
        record.path,
        token,
        body_bytes=json.dumps(omp_body).encode(),
        extra_headers={
            "X-OpenCode-Event": "opencode.session_idle"
        },  # 会被 OpenCode adapter 先校验 header
    )
    response = await srv._process_request(req, "oc-rev-test")  # type: ignore[arg-type]
    body = json.loads(response.body)
    # OpenCode adapter 检查到 event=omp.session_stop → provider_incompatible
    assert response.status == 400
    assert body["data"]["error"] == "provider_incompatible"
    assert body["data"]["retryable"] is False


@pytest.mark.asyncio
async def test_duplicate_event_is_rendered_and_sent_once_with_compatible_skip():
    sender = FakeSender()
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
        provider_registry=_make_provider_registry(),
    )
    event = _make_event()
    endpoint = _make_endpoint()

    first = await server._dispatch_event(event, endpoint, None, "first-request")
    duplicate = await server._dispatch_event(event, endpoint, None, "retry-request")
    first_data = json.loads(first.body)["data"]
    duplicate_body = json.loads(duplicate.body)

    assert first_data["delivered"] is True
    assert duplicate.status == 200
    assert duplicate_body["message"] == "skipped"
    assert duplicate_body["data"] == {
        "request_id": "retry-request",
        "provider": "omp",
        "event": "omp.session_stop",
        "delivered": False,
        "targets": [],
        "render_mode": "text",
        "requested_render_mode": "text",
        "fallback_to_text": False,
        "fallback_reason": None,
        "skipped": True,
        "skip_reason": "idempotency_replay",
        "deduplicated": True,
        "rendered": False,
        "retryable": False,
    }
    assert len(sender.sent_texts) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_waits_for_owner_and_does_not_send_twice():
    sender = BoundarySender()
    sender.block_send = True
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
        provider_registry=_make_provider_registry(),
    )
    event = _make_event()
    endpoint = _make_endpoint()

    owner_task = asyncio.create_task(
        server._dispatch_event(event, endpoint, None, "owner-request")
    )
    await asyncio.sleep(0)
    duplicate_task = asyncio.create_task(
        server._dispatch_event(event, endpoint, None, "duplicate-request")
    )
    await asyncio.sleep(0)
    assert not duplicate_task.done()

    sender.release_send.set()
    owner, duplicate = await asyncio.gather(owner_task, duplicate_task)
    assert owner.status == duplicate.status == 200
    assert json.loads(duplicate.body)["data"]["deduplicated"] is True
    assert len(sender.sent_texts) == 1


@pytest.mark.asyncio
async def test_render_failure_releases_claim_for_next_request(monkeypatch):
    sender = FakeSender()
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
        provider_registry=_make_provider_registry(),
    )
    endpoint = _make_endpoint()
    event = _make_event()
    original = __import__(
        "core.server", fromlist=["render_text_default"]
    ).render_text_default
    calls = 0

    def fail_once(current_event, display_context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("render failed")
        return original(current_event, display_context)

    monkeypatch.setattr("core.server.render_text_default", fail_once)
    failed = await server._dispatch_event(event, endpoint, None, "failed-request")
    recovered = await server._dispatch_event(event, endpoint, None, "recovered-request")

    assert failed.status == 500
    assert recovered.status == 200
    assert json.loads(recovered.body)["data"]["delivered"] is True
    assert len(sender.sent_texts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target_alias", ["", "nonexistent", 123])
async def test_invalid_target_alias_is_rejected_before_idempotency_claim(target_alias):
    store = IdempotencyStore()
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        FakeSender(),
        plugin_config={"render_mode": "text"},
        idempotency_store=store,
    )

    response = await server._dispatch_event(
        _make_event(), _make_endpoint(), target_alias, "invalid-alias"
    )
    payload = json.loads(response.body)

    assert response.status == 400
    assert payload["data"]["error"] == "invalid_target_alias"
    assert store.size == 0


@pytest.mark.asyncio
async def test_callback_incompatible_sender_never_falls_back_without_tracker():
    store = IdempotencyStore()
    sender = CallbackIncompatibleSender()
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
        idempotency_store=store,
    )
    event = _make_event()
    endpoint = _make_endpoint()

    with pytest.raises(TypeError, match="delivery_attempt_callback"):
        await server._dispatch_event(event, endpoint, None, "unsupported-callback")
    with pytest.raises(TypeError, match="delivery_attempt_callback"):
        await server._dispatch_event(event, endpoint, None, "unsupported-retry")

    assert sender.calls == 0
    assert store.size == 0


@pytest.mark.asyncio
async def test_exact_target_alias_is_routed_and_keyed_without_normalization():
    ctx = Context()
    sender = Sender(ctx, enable_private_notifications=True)
    endpoint = _make_endpoint()
    endpoint.targets = [
        TargetAlias(name="Alias", umo="test:GroupMessage:1"),
        TargetAlias(name=" alias", umo="test:GroupMessage:2"),
    ]
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
    )
    event = _make_event()

    first = await server._dispatch_event(event, endpoint, "Alias", "alias-one")
    second = await server._dispatch_event(event, endpoint, " alias", "alias-two")
    duplicate = await server._dispatch_event(event, endpoint, "Alias", "alias-retry")

    assert first.status == second.status == duplicate.status == 200
    assert json.loads(duplicate.body)["data"]["deduplicated"] is True
    assert [umo for umo, _ in ctx._sent_messages] == [
        "test:GroupMessage:1",
        "test:GroupMessage:2",
    ]


@pytest.mark.asyncio
async def test_cancel_before_delivery_boundary_releases_claim_for_retry():
    sender = ControlledSender(wait_first=True)
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
    )
    event = _make_event()
    endpoint = _make_endpoint()

    owner = asyncio.create_task(
        server._dispatch_event(event, endpoint, None, "cancel-before")
    )
    await sender.entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    sender.wait_first = False
    retry = await server._dispatch_event(event, endpoint, None, "retry-before")

    assert retry.status == 200
    assert json.loads(retry.body)["data"]["delivered"] is True
    assert sender.calls == 2


@pytest.mark.asyncio
async def test_cancel_after_delivery_boundary_finalizes_claim():
    sender = ControlledSender(mark_before_wait=True, wait_first=True)
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
    )
    event = _make_event()
    endpoint = _make_endpoint()

    owner = asyncio.create_task(
        server._dispatch_event(event, endpoint, None, "cancel-after")
    )
    await sender.entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    duplicate = await server._dispatch_event(event, endpoint, None, "retry-after")

    assert duplicate.status == 200
    assert json.loads(duplicate.body)["data"]["deduplicated"] is True
    assert sender.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_before_failure", [False, True])
async def test_send_exception_releases_or_finalizes_by_delivery_boundary(
    mark_before_failure: bool,
):
    sender = ControlledSender(
        mark_before_wait=mark_before_failure,
        raise_error=True,
    )
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
    )
    event = _make_event()
    endpoint = _make_endpoint()

    with pytest.raises(RuntimeError, match="simulated send exception"):
        await server._dispatch_event(event, endpoint, None, "exception-owner")

    if mark_before_failure:
        duplicate = await server._dispatch_event(
            event, endpoint, None, "exception-retry"
        )
        assert json.loads(duplicate.body)["data"]["deduplicated"] is True
        assert sender.calls == 1
    else:
        sender.raise_error = False
        retry = await server._dispatch_event(event, endpoint, None, "exception-retry")
        assert json.loads(retry.body)["data"]["delivered"] is True
        assert sender.calls == 2


@pytest.mark.asyncio
async def test_false_after_delivery_boundary_is_not_retried():
    sender = ControlledSender(mark_before_wait=True, fail=True)
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
    )
    event = _make_event()
    endpoint = _make_endpoint()

    first = await server._dispatch_event(event, endpoint, None, "false-owner")
    duplicate = await server._dispatch_event(event, endpoint, None, "false-retry")

    assert json.loads(first.body)["message"] == "partial_failure"
    assert json.loads(duplicate.body)["data"]["deduplicated"] is True
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_capacity_returns_503_and_expired_entry_recovers():
    now = [100.0]
    store = IdempotencyStore(capacity=1, clock=lambda: now[0])
    sender = BoundarySender()
    sender.block_send = True
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        plugin_config={"render_mode": "text"},
        idempotency_store=store,
    )
    endpoint = _make_endpoint()
    first_event = _make_event()
    owner = asyncio.create_task(
        server._dispatch_event(first_event, endpoint, None, "capacity-owner")
    )
    await asyncio.sleep(0)

    second_event = _make_event()
    second_event.id = "different-event-id"
    full = await server._dispatch_event(second_event, endpoint, None, "capacity-full")
    assert full.status == 503
    assert json.loads(full.body)["data"]["error"] == "idempotency_capacity"

    sender.release_send.set()
    await owner
    now[0] += 600

    third_event = _make_event()
    third_event.id = "expired-event-id"
    recovered = await server._dispatch_event(
        third_event, endpoint, None, "capacity-recovered"
    )
    assert recovered.status == 200
    assert json.loads(recovered.body)["data"]["delivered"] is True


@pytest.mark.asyncio
async def test_filter_and_private_skip_do_not_pollute_idempotency_store():
    filtered_store = IdempotencyStore()
    filtered_sender = FakeSender()
    filtered_server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        filtered_sender,
        plugin_config={"render_mode": "text", "notification_mode": "focused"},
        idempotency_store=filtered_store,
    )
    filtered_event = _make_event()
    filtered_event.session_scope = SessionScope.SUBAGENT
    filtered_event.status = "completed"
    filtered = await filtered_server._dispatch_event(
        filtered_event, _make_endpoint(), None, "filtered"
    )
    assert json.loads(filtered.body)["data"]["skip_reason"] == (
        "notification_mode_filtered"
    )
    assert filtered_store.size == 0

    private_store = IdempotencyStore()
    ctx = Context()
    private_sender = Sender(ctx)
    private_endpoint = _make_endpoint()
    private_endpoint.targets = [TargetAlias(name="private", umo="test:FriendMessage:1")]
    private_server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        private_sender,
        plugin_config={"render_mode": "text"},
        idempotency_store=private_store,
    )
    private_event = _make_event()
    skipped = await private_server._dispatch_event(
        private_event, private_endpoint, None, "private-skip"
    )
    assert json.loads(skipped.body)["message"] == "skipped"
    assert private_store.size == 0

    private_sender._enable_private_notifications = True
    delivered = await private_server._dispatch_event(
        private_event, private_endpoint, None, "private-retry"
    )
    assert json.loads(delivered.body)["data"]["delivered"] is True
    assert private_store.size == 1


@pytest.mark.asyncio
async def test_image_failure_after_delivery_boundary_is_not_retried():
    sender = FakeSender()
    sender.set_fail_send(True)

    async def render_image(*_args, **_kwargs):
        return b"\x89PNG\r\n\x1a\nimage"

    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        sender,
        html_render=render_image,
        plugin_config={"render_mode": "html_image", "fallback_to_text": True},
    )
    event = _make_event()
    endpoint = _make_endpoint()

    first = await server._dispatch_event(event, endpoint, None, "image-failure")
    duplicate = await server._dispatch_event(event, endpoint, None, "image-retry")

    assert json.loads(first.body)["message"] == "partial_failure"
    assert json.loads(duplicate.body)["data"]["deduplicated"] is True
    assert sender.send_image_calls == 1
