from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, cast

import pytest

from core.markdown import (
    MARKDOWN_EVENT,
    MAX_MARKDOWN_CHARS,
    MAX_INLINE_NESTING,
    MarkdownProviderAdapter,
    render_markdown_html,
)
from core.models import (
    DeliveryAuthentication,
    EndpointRecord,
    ServerConfig,
    TargetAlias,
)
from core.providers import ProviderError, ProviderRegistry
from core.renderer import (
    render_html,
    render_html_data,
    render_html_default,
    render_text_default,
)
from core.server import WebhookServer


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": MARKDOWN_EVENT,
        "id": "cpa-update-stable",
        "title": "CPA 自动更新",
        "markdown": "## 更新完成\n\n- CPA：`x → y`\n- 状态：**成功**",
        "target_alias": "default",
    }
    payload.update(overrides)
    return payload


def _adapter() -> MarkdownProviderAdapter:
    return MarkdownProviderAdapter()


def _endpoint() -> EndpointRecord:
    return EndpointRecord(
        name="markdown-test",
        path="u/hash/markdown-test",
        provider="markdown",
        token_hash="unused",
        token_hash_algorithm="hmac-sha256",
        owner_user_id="owner",
        owner_platform_id="test",
        targets=[
            TargetAlias(name="default", umo="test:GroupMessage:1"),
            TargetAlias(name="ops", umo="test:GroupMessage:2"),
        ],
        status="active",
        created_at="2026-08-13T00:00:00+00:00",
    )


class _Registry:
    def authenticate_delivery(self, _path: str, _authorization: str | None):
        return DeliveryAuthentication(True, None, "ok", _endpoint())


class _Sender:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.images: list[str | bytes] = []
        self.target_aliases: list[str | None] = []

    def preflight_private_notification_policy(self, *_args, **_kwargs):
        return None

    async def send_text(
        self,
        text: str,
        _endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback=None,
        **_kwargs,
    ) -> list[dict[str, Any]]:
        if delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        self.texts.append(text)
        self.target_aliases.append(target_alias)
        return [{"name": target_alias or "all", "ok": True, "error": None}]

    async def send_image(
        self,
        image: str | bytes,
        _endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback=None,
        **_kwargs,
    ) -> list[dict[str, Any]]:
        if delivery_attempt_callback is not None:
            delivery_attempt_callback.mark()
        self.images.append(image)
        self.target_aliases.append(target_alias)
        return [{"name": target_alias or "all", "ok": True, "error": None}]

    async def send_images(
        self,
        images: Sequence[str | bytes],
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback=None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        assert len(images) == 1
        return await self.send_image(
            images[0],
            endpoint,
            target_alias,
            delivery_attempt_callback=delivery_attempt_callback,
            **kwargs,
        )


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.content_type = "application/json"
        self.path = "/webhook/u/hash/markdown-test"
        self.headers = {"Authorization": "Bearer test"}
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.content_length = len(self._body)

    async def read(self) -> bytes:
        return self._body


def _provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(MarkdownProviderAdapter())
    registry.freeze()
    return registry


def test_valid_payload_is_normalized() -> None:
    event = _adapter().parse(
        headers={}, payload=_payload(), received_at="2026-08-13T00:00:00+00:00"
    )

    assert event.provider == "markdown"
    assert event.event == MARKDOWN_EVENT
    assert event.id == "cpa-update-stable"
    assert event.title == "CPA 自动更新"
    assert event.markdown and "`x → y`" in event.markdown


@pytest.mark.parametrize("markdown", [None, 1, {}, [], True])
def test_markdown_must_be_string(markdown: Any) -> None:
    with pytest.raises(ProviderError) as exc_info:
        _adapter().parse(
            headers={}, payload=_payload(markdown=markdown), received_at="now"
        )
    assert exc_info.value.code == "invalid_payload"


@pytest.mark.parametrize("markdown", ["", "  \n\t "])
def test_markdown_must_not_be_empty(markdown: str) -> None:
    with pytest.raises(ProviderError, match="非空字符串"):
        _adapter().parse(
            headers={}, payload=_payload(markdown=markdown), received_at="now"
        )


def test_markdown_length_limit() -> None:
    with pytest.raises(ProviderError) as exc_info:
        _adapter().parse(
            headers={},
            payload=_payload(markdown="x" * (MAX_MARKDOWN_CHARS + 1)),
            received_at="now",
        )
    assert exc_info.value.code == "payload_too_large"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "x" * 201),
        ("id", "x" * 129),
        ("target_alias", "x" * 129),
    ],
)
def test_optional_field_length_limits(field: str, value: str) -> None:
    with pytest.raises(ProviderError) as exc_info:
        _adapter().parse(
            headers={}, payload=_payload(**{field: value}), received_at="now"
        )
    assert exc_info.value.code == "invalid_payload"


def test_event_is_fixed() -> None:
    with pytest.raises(ProviderError) as exc_info:
        _adapter().parse(
            headers={}, payload=_payload(event="markdown.other"), received_at="now"
        )
    assert exc_info.value.code == "unsupported_event"


def test_raw_html_and_remote_image_are_rendered_as_text() -> None:
    rendered = str(
        render_markdown_html(
            '<script>alert(1)</script>\n\n<img src="https://evil.invalid/x.png">\n\n![远程图](https://evil.invalid/x.png)'
        )
    )
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "![远程图]" in rendered


def test_links_and_code_are_safely_escaped() -> None:
    rendered = str(
        render_markdown_html(
            "[文档](https://example.com/a?x=1&y=2) [危险](javascript:alert(1)) `</code><script>`\n\n```html\n<img src=x onerror=alert(1)>\n```"
        )
    )
    assert 'href="https://example.com/a?x=1&amp;y=2"' in rendered
    assert "javascript:" in rendered
    assert 'href="javascript:' not in rendered
    assert "<script>" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered


def test_max_length_inline_render_stays_bounded() -> None:
    markdown = "[" * MAX_MARKDOWN_CHARS
    started = time.monotonic()
    rendered = str(render_markdown_html(markdown))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert rendered.startswith('<p class="md-paragraph">')
    assert rendered.count("[") == MAX_MARKDOWN_CHARS


def test_deep_interleaved_markers_degrade_without_recursion_error() -> None:
    depth = MAX_INLINE_NESTING + 200
    markdown = ("**_" * depth) + "payload" + ("_**" * depth)
    started = time.monotonic()
    rendered = str(render_markdown_html(markdown))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert "payload" in rendered
    assert "RecursionError" not in rendered
    assert rendered.count("<strong>") == rendered.count("</strong>")
    assert rendered.count("<em>") == rendered.count("</em>")


def test_malformed_links_are_bounded_and_remain_text() -> None:
    malformed = ("[label](https://example.com/" * 900)[:MAX_MARKDOWN_CHARS]
    started = time.monotonic()
    rendered = str(render_markdown_html(malformed))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert "[label]" in rendered
    assert '<a href="' not in rendered


def test_text_mode_is_readable_plain_text() -> None:
    event = _adapter().parse(headers={}, payload=_payload(), received_at="now")
    rendered = render_text_default(event)
    assert rendered.startswith("[Markdown] CPA 自动更新")
    assert "更新完成" in rendered
    assert "- CPA：x → y" in rendered
    assert "- 状态：成功" in rendered
    assert "**" not in rendered


def test_html_mode_reuses_card_and_csp() -> None:
    event = _adapter().parse(headers={}, payload=_payload(), received_at="now")
    rendered = render_html_default(event)
    assert 'class="card"' in rendered
    assert 'class="markdown-body"' in rendered
    assert "Content-Security-Policy" in rendered
    assert "default-src 'none'" in rendered
    assert "CPA 自动更新" in rendered
    assert "<code>x → y</code>" in rendered


def test_custom_template_cannot_access_raw_markdown_with_safe() -> None:
    raw = '<img src="https://evil.invalid/x.png" onerror="alert(1)">{{ 7 * 7 }}'
    event = _adapter().parse(
        headers={}, payload=_payload(markdown=raw), received_at="now"
    )
    context = render_html_data(event)["event"]

    assert "markdown" not in context
    rendered = render_html(
        event,
        "<html><head></head><body>raw={{ event.markdown|default('missing')|safe }}"
        "<div>{{ event.markdown_html|safe }}</div>"
        "<pre>{{ event.markdown_text|safe }}</pre></body></html>",
    )
    assert "raw=missing" in rendered
    assert "<img" not in rendered
    assert "&lt;img" in rendered
    assert "49" not in rendered
    assert "{{ 7 * 7 }}" in rendered


@pytest.mark.asyncio
async def test_server_forwards_target_alias_in_text_mode() -> None:
    sender = _Sender()
    server = WebhookServer(
        ServerConfig(),
        _Registry(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        plugin_config={"render_mode": "text", "min_completion_duration_seconds": 0},
        provider_registry=_provider_registry(),
    )
    response = await server._process_request(
        cast(Any, _Request(_payload(target_alias="ops"))), "req-md-text"
    )

    assert response.status == 200
    assert sender.target_aliases == ["ops"]
    assert sender.texts and "CPA 自动更新" in sender.texts[0]


@pytest.mark.asyncio
async def test_server_rejects_unbound_target_alias() -> None:
    sender = _Sender()
    server = WebhookServer(
        ServerConfig(),
        _Registry(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        plugin_config={"render_mode": "text", "min_completion_duration_seconds": 0},
        provider_registry=_provider_registry(),
    )
    response = await server._process_request(
        cast(Any, _Request(_payload(target_alias="missing"))), "req-md-target"
    )
    body = json.loads(cast(bytes, response.body))

    assert response.status == 400
    assert body["data"]["error"] == "invalid_target_alias"
    assert not sender.texts


@pytest.mark.asyncio
async def test_id_reuses_existing_idempotency_chain() -> None:
    sender = _Sender()
    server = WebhookServer(
        ServerConfig(),
        _Registry(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        plugin_config={"render_mode": "text", "min_completion_duration_seconds": 0},
        provider_registry=_provider_registry(),
    )
    first = await server._process_request(
        cast(Any, _Request(_payload(id="same-stable-id"))), "req-md-first"
    )
    replay = await server._process_request(
        cast(Any, _Request(_payload(id="same-stable-id"))), "req-md-replay"
    )
    replay_body = json.loads(cast(bytes, replay.body))

    assert first.status == replay.status == 200
    assert len(sender.texts) == 1
    assert replay_body["data"]["skip_reason"] == "idempotency_replay"


@pytest.mark.asyncio
async def test_html_image_and_fallback_paths() -> None:
    captured: dict[str, Any] = {}

    async def html_render(_template, data, **_kwargs):
        captured["html"] = data["rendered_html"]
        return b"\x89PNG\r\n\x1a\n"

    sender = _Sender()
    server = WebhookServer(
        ServerConfig(),
        _Registry(),  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        html_render=html_render,
        plugin_config={
            "render_mode": "html_image",
            "fallback_to_text": True,
            "min_completion_duration_seconds": 0,
        },
        provider_registry=_provider_registry(),
    )
    server._render_image_attempt = _fake_image_attempt(captured)  # type: ignore[method-assign]
    response = await server._process_request(
        cast(Any, _Request(_payload())), "req-md-image"
    )
    assert response.status == 200
    assert sender.images == [b"image"]
    assert 'class="markdown-body"' in captured["html"]

    async def fail_image(*_args, **_kwargs):
        error = RuntimeError("fail")
        error.reason = "html_render_failed"  # type: ignore[attr-defined]
        raise error

    fallback_sender = _Sender()
    fallback_server = WebhookServer(
        ServerConfig(),
        _Registry(),  # type: ignore[arg-type]
        fallback_sender,  # type: ignore[arg-type]
        html_render=html_render,
        plugin_config={
            "render_mode": "html_image",
            "fallback_to_text": True,
            "min_completion_duration_seconds": 0,
        },
        provider_registry=_provider_registry(),
    )
    fallback_server._render_image_attempt = fail_image  # type: ignore[method-assign]
    fallback_response = await fallback_server._process_request(
        cast(Any, _Request(_payload(id="fallback-id"))), "req-md-fallback"
    )
    fallback_body = json.loads(cast(bytes, fallback_response.body))
    assert fallback_body["data"]["render_mode"] == "text"
    assert fallback_body["data"]["fallback_reason"] == "html_render_failed"
    assert fallback_sender.texts and "更新完成" in fallback_sender.texts[0]


def _fake_image_attempt(captured: dict[str, Any]):
    async def fake(event, template, _request_id, _prepared):
        captured["html"] = render_html(event, template.content)
        return b"image", {}

    return fake
