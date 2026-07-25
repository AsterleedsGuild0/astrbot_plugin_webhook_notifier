"""Sender tests - uses fake_astrbot stubs."""

from __future__ import annotations

import pytest

from astrbot.api.message_components import Image
from astrbot.api.star import Context

from core.models import EndpointRecord, TargetAlias
from core.sender import DeliveryAttemptTracker, Sender


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
        assert results[0]["error"] == "send_failed"


class TestSendImages:
    """send_images 真实消息链与发送次数回归。"""

    @pytest.mark.asyncio
    async def test_one_image_is_compatible_and_disables_t2i(self):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )

        results = await sender.send_images(["https://example.com/main.png"], endpoint)

        assert results == [{"name": "default", "ok": True, "error": None}]
        assert len(ctx._sent_messages) == 1
        _, chain = ctx._sent_messages[0]
        assert len(chain.chain) == 1
        assert isinstance(chain.chain[0], Image)
        assert chain.chain[0].file == "https://example.com/main.png"
        assert chain.get_use_t2i() is False

    @pytest.mark.asyncio
    async def test_two_images_share_ordered_message_chain(self):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[
                TargetAlias(name="default", umo="test:Msg:1"),
                TargetAlias(name="backup", umo="test:Msg:2"),
            ]
        )

        results = await sender.send_images(
            ["https://example.com/main.png", "https://example.com/timeline.png"],
            endpoint,
        )

        assert results[0]["ok"] is True
        assert len(ctx._sent_messages) == 2
        _, chain = ctx._sent_messages[0]
        assert ctx._sent_messages[1][1] is chain
        assert len(chain.chain) == 2
        assert [component.file for component in chain.chain] == [
            "https://example.com/main.png",
            "https://example.com/timeline.png",
        ]
        assert all(isinstance(component, Image) for component in chain.chain)
        assert chain.get_use_t2i() is False

    @pytest.mark.asyncio
    async def test_targets_alias_and_private_policy_send_once_per_target(self):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[
                TargetAlias(name="group_a", umo="test:Group:1"),
                TargetAlias(name="group_b", umo="test:Group:2"),
            ]
        )

        results = await sender.send_images(["https://example.com/main.png"], endpoint)
        assert [result["name"] for result in results] == ["group_a", "group_b"]
        assert len(ctx._sent_messages) == 2
        assert [umo for umo, _ in ctx._sent_messages] == [
            "test:Group:1",
            "test:Group:2",
        ]

        ctx.clear_sent()
        alias_results = await sender.send_images(
            ["https://example.com/main.png"], endpoint, target_alias="group_b"
        )
        assert alias_results == [{"name": "group_b", "ok": True, "error": None}]
        assert len(ctx._sent_messages) == 1
        assert ctx._sent_messages[0][0] == "test:Group:2"

        private_endpoint = _make_endpoint(
            targets=[TargetAlias(name="private", umo="test:FriendMessage:1")]
        )
        ctx.clear_sent()
        skipped = await sender.send_images(
            ["https://example.com/main.png"], private_endpoint
        )
        assert skipped[0]["skipped"] is True
        assert ctx._sent_messages == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "image_results",
        [[], ["a", "b", "c"], ["https://example.com/main.png", object()]],
    )
    async def test_invalid_image_batches_fail_before_any_send(self, image_results):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[
                TargetAlias(name="group_a", umo="test:Group:1"),
                TargetAlias(name="group_b", umo="test:Group:2"),
            ]
        )

        results = await sender.send_images(image_results, endpoint)  # type: ignore[arg-type]

        assert len(ctx._sent_messages) == 0
        assert results[0]["ok"] is False
        assert results[0]["error"] in {
            "invalid_image_count",
            "unsupported_image_result",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raises", [False, True])
    async def test_send_failure_attempts_each_target_once_without_retry(self, raises):
        ctx = Context()
        calls: list[str] = []

        async def failing_send(umo, chain):
            calls.append(umo)
            if raises:
                raise RuntimeError("backend secret: /private/path")
            return False

        ctx.send_message = failing_send  # type: ignore[assignment]
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[
                TargetAlias(name="group_a", umo="test:Group:1"),
                TargetAlias(name="group_b", umo="test:Group:2"),
            ]
        )

        results = await sender.send_images(["https://example.com/main.png"], endpoint)

        assert calls == ["test:Group:1", "test:Group:2"]
        assert len(results) == 2
        assert all(result["ok"] is False for result in results)
        if raises:
            assert all(result["error"] == "send_failed" for result in results)
        else:
            assert all(result["error"] == "session_not_found" for result in results)

    @pytest.mark.asyncio
    async def test_send_image_is_a_thin_send_images_wrapper(self, monkeypatch):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        captured: dict[str, object] = {}

        async def fake_send_images(
            image_results,
            actual_endpoint,
            target_alias=None,
            delivery_attempt_callback=None,
        ):
            captured["image_results"] = image_results
            captured["endpoint"] = actual_endpoint
            captured["target_alias"] = target_alias
            return [{"name": "default", "ok": True, "error": None}]

        monkeypatch.setattr(sender, "send_images", fake_send_images)
        result = await sender.send_image(
            "https://example.com/main.png", endpoint, target_alias="default"
        )

        assert result[0]["ok"] is True
        assert captured == {
            "image_results": ["https://example.com/main.png"],
            "endpoint": endpoint,
            "target_alias": "default",
        }


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


@pytest.mark.asyncio
class TestPrivateNotificationPolicy:
    async def test_private_text_and_image_are_skipped_by_default(self):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            [TargetAlias(name="private", umo="aiocqhttp:FriendMessage:10001")]
        )

        text_results = await sender.send_text("hello", endpoint)
        image_results = await sender.send_image("not_an_image", endpoint)

        expected = {
            "name": "private",
            "ok": True,
            "skipped": True,
            "error": None,
            "reason": "private_notifications_disabled",
        }
        assert text_results == [expected]
        assert image_results == [expected]
        assert ctx.get_last_sent() is None

    async def test_private_notifications_can_be_enabled(self):
        ctx = Context()
        sender = Sender(ctx, enable_private_notifications=True)
        endpoint = _make_endpoint(
            [TargetAlias(name="private", umo="aiocqhttp:FriendMessage:10001")]
        )

        results = await sender.send_text("hello", endpoint)

        assert results == [{"name": "private", "ok": True, "error": None}]
        assert ctx.get_last_sent() is not None

    async def test_private_image_notifications_can_be_enabled(self):
        ctx = Context()
        sender = Sender(ctx, enable_private_notifications=True)
        endpoint = _make_endpoint(
            [TargetAlias(name="private", umo="aiocqhttp:FriendMessage:10001")]
        )

        results = await sender.send_image("https://example.com/image.png", endpoint)

        assert results == [{"name": "private", "ok": True, "error": None}]
        assert ctx.get_last_sent() is not None

    async def test_group_is_always_sent_and_mixed_targets_skip_only_private(self):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            [
                TargetAlias(name="private", umo="aiocqhttp:FriendMessage:10001"),
                TargetAlias(name="group", umo="aiocqhttp:GroupMessage:20001"),
            ]
        )

        results = await sender.send_text("hello", endpoint)

        assert results[0]["skipped"] is True
        assert results[1] == {"name": "group", "ok": True, "error": None}
        assert ctx.get_last_sent()[0] == "aiocqhttp:GroupMessage:20001"

    async def test_target_alias_only_evaluates_selected_target(self):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            [
                TargetAlias(name="private", umo="aiocqhttp:FriendMessage:10001"),
                TargetAlias(name="group", umo="aiocqhttp:GroupMessage:20001"),
            ]
        )

        results = await sender.send_text("hello", endpoint, target_alias="group")

        assert results == [{"name": "group", "ok": True, "error": None}]

    async def test_session_id_with_colon_does_not_change_message_type(self):
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            [TargetAlias(name="group", umo="aiocqhttp:GroupMessage:room:thread")]
        )

        results = await sender.send_text("hello", endpoint)

        assert results == [{"name": "group", "ok": True, "error": None}]


@pytest.mark.asyncio
async def test_delivery_attempt_callback_marks_immediately_before_platform_call():
    ctx = Context()
    tracker = DeliveryAttemptTracker()
    observed: list[bool] = []

    async def send_message(umo, chain):
        observed.append(tracker.attempted)
        return True

    ctx.send_message = send_message  # type: ignore[assignment]
    sender = Sender(ctx)
    endpoint = _make_endpoint([TargetAlias(name="default", umo="test:Msg:1")])

    await sender.send_text("hello", endpoint, delivery_attempt_callback=tracker)

    assert observed == [True]
    assert tracker.attempted is True


@pytest.mark.asyncio
async def test_unsupported_image_does_not_mark_delivery_attempt_callback():
    ctx = Context()
    tracker = DeliveryAttemptTracker()
    sender = Sender(ctx)
    endpoint = _make_endpoint([TargetAlias(name="default", umo="test:Msg:1")])

    results = await sender.send_image(
        "not-an-image", endpoint, delivery_attempt_callback=tracker
    )

    assert results[0]["error"] == "unsupported_image_result"
    assert tracker.attempted is False
    assert ctx.get_last_sent() is None


@pytest.mark.asyncio
async def test_multi_target_delivery_attempt_callback_marks_every_platform_call():
    ctx = Context()
    callback_count = 0
    observed_counts: list[int] = []

    def mark_attempt() -> None:
        nonlocal callback_count
        callback_count += 1

    async def send_message(umo, chain):
        observed_counts.append(callback_count)
        return True

    ctx.send_message = send_message  # type: ignore[assignment]
    endpoint = _make_endpoint(
        [
            TargetAlias(name="first", umo="test:Group:1"),
            TargetAlias(name="second", umo="test:Group:2"),
        ]
    )

    await Sender(ctx).send_images(
        ["https://example.com/main.png"],
        endpoint,
        delivery_attempt_callback=mark_attempt,
    )

    assert callback_count == 2
    assert observed_counts == [1, 2]
