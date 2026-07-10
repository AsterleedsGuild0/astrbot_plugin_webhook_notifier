"""Sender tests - uses fake_astrbot stubs."""

from __future__ import annotations

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context

from core.models import EndpointRecord, TargetAlias
from core.sender import Sender


def _make_endpoint(
    targets: list[TargetAlias] | None = None,
) -> EndpointRecord:
    return EndpointRecord(
        name="test_endpoint",
        path="u/hash/test_endpoint",
        provider="omp",
        token_hash="abc123",
        token_hash_algorithm="hmac-sha256",
        owner_user_id="user_001",
        targets=targets or [],
        render_mode="text",
        template=None,
        status="active",
        created_at="2026-07-09T12:00:00",
    )


class TestSendImage:
    """send_image 测试。"""

    @pytest.mark.asyncio
    async def test_send_image_url(self):
        """URL 图片应构造 Image(file=URL) 并发送。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        results = await sender.send_image("https://example.com/image.png", endpoint)
        assert len(results) == 1
        assert results[0]["ok"] is True

        last = ctx.get_last_sent()
        assert last is not None
        _, chain = last
        assert len(chain.chain) == 1
        img = chain.chain[0]
        assert isinstance(img, Image)
        assert img.file == "https://example.com/image.png"

    @pytest.mark.asyncio
    async def test_send_image_base64_prefix(self):
        """base64:// 前缀图片应保持原样传给 Image。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        b64_str = (
            "base64://iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        )
        results = await sender.send_image(b64_str, endpoint)
        assert results[0]["ok"] is True
        last = ctx.get_last_sent()
        assert last is not None
        img = last[1].chain[0]
        assert isinstance(img, Image)
        assert img.file == b64_str

    @pytest.mark.asyncio
    async def test_send_image_data_url(self):
        """data:image 前缀应保持原样传给 Image。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        results = await sender.send_image(data_url, endpoint)
        assert results[0]["ok"] is True
        last = ctx.get_last_sent()
        assert last is not None
        img = last[1].chain[0]
        assert isinstance(img, Image)
        assert img.file == data_url

    @pytest.mark.asyncio
    async def test_send_image_bytes(self):
        """bytes 图片应构造 Image(file=bytes) 并发送。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        img_bytes = b"\x89PNG\r\n\x1a\n" + b"dummy_data"
        results = await sender.send_image(img_bytes, endpoint)
        assert results[0]["ok"] is True
        last = ctx.get_last_sent()
        assert last is not None
        img = last[1].chain[0]
        assert isinstance(img, Image)
        assert img.file == img_bytes

    @pytest.mark.asyncio
    async def test_use_t2i_false(self):
        """图片消息链 use_t2i(False) 应生效。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        await sender.send_image("https://example.com/img.png", endpoint)
        last = ctx.get_last_sent()
        assert last is not None
        assert last[1].get_use_t2i() is False

    @pytest.mark.asyncio
    async def test_no_targets(self):
        """无目标时应返回错误结果。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(targets=[])  # no targets
        results = await sender.send_image("https://example.com/img.png", endpoint)
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["error"] == "no_targets"

    @pytest.mark.asyncio
    async def test_context_send_message_false(self):
        """send_message 返回 False 时结果应为失败。"""
        ctx = Context()

        # 覆写 send_message 返回 False
        async def failing_send(umo, chain):
            return False

        ctx.send_message = failing_send  # type: ignore[assignment]

        sender = Sender(ctx)
        endpoint = _make_endpoint(targets=[TargetAlias(name="test", umo="test:Msg:1")])
        results = await sender.send_image("https://example.com/img.png", endpoint)
        assert results[0]["ok"] is False
        assert "session_not_found" in results[0].get("error", "")

    @pytest.mark.asyncio
    async def test_context_send_message_exception(self):
        """send_message 抛出异常时结果应为失败。"""
        ctx = Context()

        async def failing_send(umo, chain):
            raise RuntimeError("connection lost")

        ctx.send_message = failing_send  # type: ignore[assignment]

        sender = Sender(ctx)
        endpoint = _make_endpoint(targets=[TargetAlias(name="test", umo="test:Msg:1")])
        results = await sender.send_image("https://example.com/img.png", endpoint)
        assert results[0]["ok"] is False
        assert "connection lost" in results[0].get("error", "")


class TestBuildImageComponent:
    """_build_image_component 直接测试：构造失败语义。"""

    def test_unsupported_string_returns_none(self):
        """无法识别的图片字符串应返回 None，不抛异常。"""
        result = Sender._build_image_component("this is not an image string")
        assert result is None

    def test_empty_string_does_not_raise(self):
        """空字符串不会抛异常（b64decode('') 返回空 bytes，被包装为 Image）。"""
        result = Sender._build_image_component("")
        # base64.b64decode('') 返回 b''，不会抛异常，Image(file=b'') 是当前合理行为
        assert result is not None

    def test_url_string_returns_image(self):
        """URL 字符串应返回 Image 组件。"""
        result = Sender._build_image_component("https://example.com/img.png")
        assert result is not None
        assert result.file == "https://example.com/img.png"

    def test_base64_prefix_returns_image(self):
        """base64:// 前缀应返回 Image 组件。"""
        result = Sender._build_image_component("base64://dGVzdA==")
        assert result is not None
        assert result.file == "base64://dGVzdA=="

    def test_bytes_returns_image(self):
        """bytes 应返回 Image 组件。"""
        result = Sender._build_image_component(b"\x89PNG\x0d\x0a\x1a\x0a")
        assert result is not None
        assert result.file == b"\x89PNG\x0d\x0a\x1a\x0a"

    @pytest.mark.asyncio
    async def test_send_image_unsupported_result_error(self):
        """send_image 传入无法识别的字符串应返回 unsupported_image_result。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        results = await sender.send_image("not_an_image", endpoint)
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["error"] == "unsupported_image_result"


class TestSendImageTargetAlias:
    """send_image 目标别名测试。"""

    @pytest.mark.asyncio
    async def test_send_to_specific_alias(self):
        """指定 target_alias 应只发送到匹配目标。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[
                TargetAlias(name="group_a", umo="test:Group:1"),
                TargetAlias(name="group_b", umo="test:Group:2"),
            ]
        )
        results = await sender.send_image(
            "https://example.com/img.png", endpoint, target_alias="group_a"
        )
        assert len(results) == 1
        assert results[0]["name"] == "group_a"
        assert results[0]["ok"] is True

    @pytest.mark.asyncio
    async def test_send_to_nonexistent_alias(self):
        """指定的 alias 不在白名单时应返回错误、不发送。"""
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[
                TargetAlias(name="group_a", umo="test:Group:1"),
            ]
        )
        results = await sender.send_image(
            "https://example.com/img.png", endpoint, target_alias="nonexistent"
        )
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["error"] == "no_targets"
