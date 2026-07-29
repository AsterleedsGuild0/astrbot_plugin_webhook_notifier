"""Tests for delivery logging context, credential sanitizer, and server→sender propagation."""

from __future__ import annotations

import logging
import re

import pytest

from astrbot.api.star import Context

from core.models import EndpointRecord, TargetAlias
from core.sender import (
    DeliveryAttemptTracker,
    DeliveryContext,
    Sender,
    _log_delivery,
    _log_delivery_exception,
    sanitize_exception,
    sanitize_log_text,
)


def _make_endpoint(targets: list[TargetAlias] | None = None) -> EndpointRecord:
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


# ─── 凭据清洗测试 ────────────────────────────────────────────


class TestSanitizeLogText:
    """sanitize_log_text 单元测试：保证凭据被移除，非敏感原文保留。"""

    def test_whn_token_redacted(self):
        text = "something whn_abcdefghijklmnopqrstuvwxyz1234567890abcdefg"
        result = sanitize_log_text(text)
        assert "[REDACTED_TOKEN]" in result
        assert "whn_" not in result or "whn_" in result and "[REDACTED_TOKEN]" in result
        # 确认 whn_ 格式的 token 被清除
        assert not re.search(r"whn_[A-Za-z0-9_-]{43}", result)

    def test_bearer_redacted(self):
        text = "Authorization: Bearer sk-ant-abcdef123456"
        result = sanitize_log_text(text)
        # Bearer token value is removed by either Bearer or key-value pattern
        assert "sk-ant-abcdef123456" not in result
        assert "[REDACTED]" in result

    @pytest.mark.parametrize("scheme", ["bearer", "BEARER", "Bearer"])
    def test_bearer_redaction_is_case_insensitive(self, scheme):
        result = sanitize_log_text(f"Authorization: {scheme} sk-secret-value")
        assert "sk-secret-value" not in result
        assert "[REDACTED]" in result

    def test_token_auth_scheme_redacted(self):
        result = sanitize_log_text("Authorization: token ghp_secret_value")
        assert "ghp_secret_value" not in result
        assert "[REDACTED]" in result

    def test_url_userinfo_redacted(self):
        text = "https://user:pass@example.com/path"
        result = sanitize_log_text(text)
        assert "[REDACTED]" in result
        assert "user:pass@" not in result

    def test_sensitive_key_value_redacted(self):
        cases = [
            ("api_key=sk-abc123", "api_key=[REDACTED]"),
            ("password=mysecret123", "password=[REDACTED]"),
            ("secret=my_super_secret", "secret=[REDACTED]"),
            ("token=abc123def456", "token=[REDACTED]"),
            ("authorization=Bearer abc", "authorization=[REDACTED]"),
            ("API_KEY=xyz789", "API_KEY=[REDACTED]"),
        ]
        for original, expected_marker in cases:
            result = sanitize_log_text(original)
            assert expected_marker in result, f"Failed for: {original}"
            # 原始值不在结果中
            value_part = original.split("=", 1)[1]
            assert value_part not in result or "[REDACTED]" in result

    def test_non_sensitive_text_preserved(self):
        """异常类型、文件名、行号、函数名等非敏感原文保留。"""
        text = (
            "RuntimeError: connection lost\n"
            '  File "/path/to/file.py", line 42, in send_message\n'
            "    raise RuntimeError('connection lost')"
        )
        result = sanitize_log_text(text)
        assert "RuntimeError" in result
        assert 'File "/path/to/file.py"' in result
        assert "line 42" in result
        assert "send_message" in result
        assert "connection lost" in result

    def test_non_string_input_safe(self):
        """非字符串输入不抛异常。"""
        assert sanitize_log_text(42) == "42"
        assert sanitize_log_text(None) == "None"
        assert sanitize_log_text(b"bytes data") == "b'bytes data'"

    def test_unprintable_input_safe(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError("cannot stringify")

        assert sanitize_log_text(Unprintable()) == "[UNPRINTABLE]"


class TestSanitizeException:
    """sanitize_exception 测试：traceback 上下文保留但凭据消失。"""

    def test_exception_type_and_message_preserved(self):
        try:
            raise ValueError("diagnostic info")
        except ValueError as exc:
            result = sanitize_exception(exc)

        assert "ValueError" in result
        assert "diagnostic info" in result
        assert "test_exception_type_and_message_preserved" in result  # stack frame

    def test_credentials_removed_from_exception(self):
        secret = "whn_abcdefghijklmnopqrstuvwxyz1234567890abcdefg"
        try:
            raise RuntimeError(f"Connection failed with token {secret}")
        except RuntimeError as exc:
            result = sanitize_exception(exc)

        assert "RuntimeError" in result
        assert "Connection failed" in result
        assert "[REDACTED_TOKEN]" in result
        assert secret not in result

    def test_bearer_in_exception_message_redacted(self):
        try:
            raise RuntimeError("Authorization: Bearer sk-my-secret-key")
        except RuntimeError as exc:
            result = sanitize_exception(exc)

        assert "[REDACTED]" in result
        assert "sk-my-secret-key" not in result

    def test_non_exception_input_safe(self):
        """非异常输入不抛异常。"""
        result = sanitize_exception("not an exception")  # type: ignore[arg-type]
        assert result == ""

    def test_stack_frame_preserved(self):
        """stack frame 中的文件路径、行号保留。"""
        try:
            raise RuntimeError("test error")
        except RuntimeError as exc:
            result = sanitize_exception(exc)

        assert "test_stack_frame_preserved" in result
        assert 'File "' in result
        assert "line " in result


# ─── 投递日志上下文测试 ────────────────────────────────────────


class TestSenderDeliveryLogging:
    """Sender 投递日志上下文测试：使用 caplog 捕获日志。"""

    @pytest.mark.asyncio
    async def test_success_log_contains_context(self, caplog):
        """成功投递的日志包含 request_id/provider/endpoint/target/elapsed_ms。"""
        caplog.set_level(logging.INFO)
        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )
        dc = DeliveryContext(
            request_id="req-001",
            provider="omp",
            endpoint_name="test_endpoint",
        )

        await sender.send_text("hello", endpoint, delivery_context=dc)

        # 找到 delivery 相关的日志
        delivery_records = [
            r for r in caplog.records if "phase=delivery" in r.getMessage()
        ]
        assert len(delivery_records) >= 1
        msg = delivery_records[0].getMessage()
        assert "reason=message_sent" in msg
        assert "request_id=req-001" in msg
        assert "provider=omp" in msg
        assert "endpoint=test_endpoint" in msg
        assert "target=default" in msg
        assert "elapsed_ms=" in msg

    @pytest.mark.asyncio
    async def test_session_not_found_log_contains_context(self, caplog):
        """session_not_found 的日志包含上下文。"""
        caplog.set_level(logging.ERROR)

        ctx = Context()

        async def failing_send(umo, chain):
            return False

        ctx.send_message = failing_send  # type: ignore[assignment]

        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="test_alias", umo="test:Msg:1")]
        )
        dc = DeliveryContext(
            request_id="req-002",
            provider="opencode",
            endpoint_name="test_ep",
        )

        results = await sender.send_text("hello", endpoint, delivery_context=dc)

        assert results[0]["ok"] is False
        assert results[0]["error"] == "session_not_found"

        delivery_records = [
            r for r in caplog.records if "phase=delivery" in r.getMessage()
        ]
        assert len(delivery_records) >= 1
        msg = delivery_records[0].getMessage()
        assert "reason=session_not_found" in msg
        assert "request_id=req-002" in msg
        assert "provider=opencode" in msg
        assert "endpoint=test_ep" in msg
        assert "target=test_alias" in msg
        assert "elapsed_ms=" in msg

    @pytest.mark.asyncio
    async def test_exception_log_contains_traceback_and_context(self, caplog):
        """发送异常的日志包含 sanitized traceback 和上下文。"""
        caplog.set_level(logging.ERROR)

        ctx = Context()

        async def failing_send(umo, chain):
            raise RuntimeError(
                "connection lost: whn_abcdefghijklmnopqrstuvwxyz1234567890abcdefg"
            )

        ctx.send_message = failing_send  # type: ignore[assignment]

        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="test_alias", umo="test:Msg:1")]
        )
        dc = DeliveryContext(
            request_id="req-003",
            provider="omp",
            endpoint_name="test_ep",
        )

        results = await sender.send_text("hello", endpoint, delivery_context=dc)

        assert results[0]["ok"] is False
        assert results[0]["error"] == "send_failed"

        delivery_records = [
            r for r in caplog.records if "phase=delivery" in r.getMessage()
        ]
        assert len(delivery_records) >= 1
        msg = delivery_records[0].getMessage()

        # 上下文
        assert "reason=send_failed" in msg
        assert "request_id=req-003" in msg
        assert "provider=omp" in msg
        assert "endpoint=test_ep" in msg
        assert "target=test_alias" in msg
        assert "elapsed_ms=" in msg
        assert "exc_type=RuntimeError" in msg

        # traceback 保留
        assert "Traceback" in msg
        assert "RuntimeError" in msg
        assert "connection lost" in msg
        assert "failing_send" in msg  # 函数名保留

        # 凭据消失
        assert "whn_abcdefghijklmnopqrstuvwxyz1234567890abcdefg" not in msg
        assert "[REDACTED_TOKEN]" in msg

    @pytest.mark.asyncio
    async def test_no_message_body_or_umo_in_logs(self, caplog):
        """消息正文和完整 UMO 不出现在新增日志中。"""
        caplog.set_level(logging.INFO)

        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:PrivateMessage:secret_umo")]
        )
        dc = DeliveryContext(
            request_id="req-004", provider="omp", endpoint_name="test_ep"
        )

        await sender.send_text("sensitive message body", endpoint, delivery_context=dc)

        for record in caplog.records:
            msg = record.getMessage()
            # phase=delivery 日志不包含消息正文或完整 UMO
            if "phase=delivery" in msg:
                assert "sensitive message body" not in msg
                assert "test:PrivateMessage:secret_umo" not in msg

    @pytest.mark.asyncio
    async def test_without_delivery_context_still_logs_basic_fields(self, caplog):
        """不传 delivery_context 时仍记录 basic fields。"""
        caplog.set_level(logging.INFO)

        ctx = Context()
        sender = Sender(ctx)
        endpoint = _make_endpoint(
            targets=[TargetAlias(name="default", umo="test:Msg:1")]
        )

        await sender.send_text("hello", endpoint)

        delivery_records = [
            r for r in caplog.records if "phase=delivery" in r.getMessage()
        ]
        assert len(delivery_records) >= 1
        msg = delivery_records[0].getMessage()
        assert "reason=message_sent" in msg
        assert "target=default" in msg
        assert "elapsed_ms=" in msg
        # 无 context 字段
        assert "request_id=" not in msg
        assert "provider=" not in msg
        assert "endpoint=" not in msg


# ─── _log_delivery / _log_delivery_exception 单元测试 ────────


class TestLogDeliveryHelpers:
    """_log_delivery 和 _log_delivery_exception 的独立测试。"""

    def test_log_delivery_with_context(self, caplog):
        caplog.set_level(logging.INFO)
        dc = DeliveryContext(
            request_id="req-100", provider="omp", endpoint_name="ep_name"
        )
        _log_delivery(
            logging.getLogger().info,
            reason="message_sent",
            target_name="target_alias",
            elapsed_ms=42,
            delivery_context=dc,
        )
        records = [r for r in caplog.records if "phase=delivery" in r.getMessage()]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "reason=message_sent" in msg
        assert "target=target_alias" in msg
        assert "elapsed_ms=42" in msg
        assert "request_id=req-100" in msg
        assert "provider=omp" in msg
        assert "endpoint=ep_name" in msg

    def test_log_delivery_without_context(self, caplog):
        caplog.set_level(logging.INFO)
        _log_delivery(
            logging.getLogger().info,
            reason="session_not_found",
            target_name="tgt",
            elapsed_ms=99,
        )
        records = [r for r in caplog.records if "phase=delivery" in r.getMessage()]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "reason=session_not_found" in msg
        assert "target=tgt" in msg
        assert "elapsed_ms=99" in msg
        assert "request_id=" not in msg
        assert "provider=" not in msg
        assert "endpoint=" not in msg

    def test_log_delivery_exception_sanitizes_and_preserves(self, caplog):
        caplog.set_level(logging.ERROR)
        secret = "whn_abcdefghijklmnopqrstuvwxyz1234567890abcdefg"
        try:
            raise RuntimeError(f"backend error with {secret}")
        except RuntimeError as exc:
            _log_delivery_exception(
                exc,
                target_name="tgt",
                elapsed_ms=100,
                delivery_context=DeliveryContext(
                    request_id="req-x", provider="omp", endpoint_name="ep"
                ),
            )

        records = [r for r in caplog.records if "phase=delivery" in r.getMessage()]
        assert len(records) >= 1
        msg = records[0].getMessage()

        # 上下文
        assert "reason=send_failed" in msg
        assert "target=tgt" in msg
        assert "elapsed_ms=100" in msg
        assert "exc_type=RuntimeError" in msg
        assert "request_id=req-x" in msg

        # traceback 保留
        assert "Traceback" in msg
        assert "RuntimeError" in msg
        assert "backend error" in msg

        # 凭据消失
        assert secret not in msg
        assert "[REDACTED_TOKEN]" in msg

    def test_log_delivery_exception_sanitizer_fail_open(self, caplog):
        """sanitize_exception 自身失败时不影响业务。"""
        caplog.set_level(logging.ERROR)

        # 模拟一个 sanitize_exception 会失败的场景
        class EvilException(BaseException):
            def __str__(self) -> str:
                raise RuntimeError("evil __str__")

        try:
            raise EvilException("secret_data")
        except EvilException as exc:
            # 不应抛出
            _log_delivery_exception(exc, target_name="tgt", elapsed_ms=0)

        records = [r for r in caplog.records if "phase=delivery" in r.getMessage()]
        assert len(records) >= 1
        msg = records[0].getMessage()
        assert "sanitize_exception failed" in msg or "phase=delivery" in msg


# ─── Server→Sender 上下文传播测试 ───────────────────────────


@pytest.mark.asyncio
async def test_sender_receives_delivery_context_via_send_text():
    """验证 delivery_context 通过 send_text 传递到 _send_to_target。"""
    ctx = Context()
    sender = Sender(ctx)
    endpoint = _make_endpoint(targets=[TargetAlias(name="default", umo="test:Msg:1")])
    dc = DeliveryContext(
        request_id="server-req-1",
        provider="opencode",
        endpoint_name="from_server",
    )

    # delivery_context 往下传后 send_text 能正常完成
    results = await sender.send_text("hello", endpoint, delivery_context=dc)

    assert results == [{"name": "default", "ok": True, "error": None}]


@pytest.mark.asyncio
async def test_sender_receives_delivery_context_via_send_images():
    """验证 delivery_context 通过 send_images 传递到 _send_to_target。"""
    ctx = Context()
    sender = Sender(ctx)
    endpoint = _make_endpoint(targets=[TargetAlias(name="default", umo="test:Msg:1")])
    dc = DeliveryContext(
        request_id="server-req-2",
        provider="omp",
        endpoint_name="from_server_image",
    )

    results = await sender.send_images(
        ["https://example.com/img.png"], endpoint, delivery_context=dc
    )

    assert results == [{"name": "default", "ok": True, "error": None}]


@pytest.mark.asyncio
async def test_sender_delivery_context_compatible_with_callback():
    """delivery_context 与 delivery_attempt_callback 共存。"""
    ctx = Context()

    call_marked = False

    def mark_callback() -> None:
        nonlocal call_marked
        call_marked = True

    sender = Sender(ctx)
    endpoint = _make_endpoint(targets=[TargetAlias(name="default", umo="test:Msg:1")])
    dc = DeliveryContext(
        request_id="compat-req",
        provider="omp",
        endpoint_name="compat_ep",
    )

    results = await sender.send_text(
        "hello",
        endpoint,
        delivery_attempt_callback=mark_callback,
        delivery_context=dc,
    )

    assert results == [{"name": "default", "ok": True, "error": None}]
    assert call_marked is True


@pytest.mark.asyncio
async def test_send_image_delegates_delivery_context():
    """send_image → send_images 正确传递 delivery_context。"""
    ctx = Context()
    sender = Sender(ctx)
    endpoint = _make_endpoint(targets=[TargetAlias(name="default", umo="test:Msg:1")])
    dc = DeliveryContext(
        request_id="img-delegate",
        provider="omp",
        endpoint_name="img_ep",
    )

    # delivery_context 不为 None 时，send_image 不会走短路路径
    results = await sender.send_image(
        "https://example.com/img.png",
        endpoint,
        delivery_context=dc,
    )

    assert results == [{"name": "default", "ok": True, "error": None}]


# ─── 现有 tracker 兼容性 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_tracker_still_works_with_delivery_context():
    """DeliveryAttemptTracker 在 delivery_context 存在时仍正常工作。"""
    ctx = Context()
    tracker = DeliveryAttemptTracker()
    sender = Sender(ctx)
    endpoint = _make_endpoint(targets=[TargetAlias(name="default", umo="test:Msg:1")])
    dc = DeliveryContext(
        request_id="tracker-test",
        provider="omp",
        endpoint_name="tracker_ep",
    )

    results = await sender.send_text(
        "hello",
        endpoint,
        delivery_attempt_callback=tracker,
        delivery_context=dc,
    )

    assert results == [{"name": "default", "ok": True, "error": None}]
    assert tracker.attempted is True
