from __future__ import annotations

import re
import time
import traceback as tb_module
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, cast

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context

from .models import EndpointRecord, TargetAlias

DeliveryAttemptCallback = Callable[[], None]


@dataclass(frozen=True)
class DeliveryContext:
    """用于投递日志的可选上下文，由 Server 层传递到 Sender 进行关联日志记录。

    request_id 来自 HTTP 请求头或自动生成。
    provider  为事件来源 provider（如 ``omp``、``opencode``）。
    endpoint_name 为 EndpointRecord.name。
    """

    request_id: str = ""
    provider: str = ""
    endpoint_name: str = ""


# ─── 凭据清洗 ───────────────────────────────────────────────

_SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie)\s*[=:]\s*[^\r\n]+"
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|token"
    r"|access[_-]?token|refresh[_-]?token)"
    r"\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s&#;,]+)"
)
_TOKEN_PATTERN = re.compile(r"\bwhn_[A-Za-z0-9_-]{43}\b")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+\S+")
_TOKEN_SCHEME_PATTERN = re.compile(r"(?i)\bToken\s+(?!\[REDACTED_TOKEN\])\S+")
_URL_USERINFO_PATTERN = re.compile(r"(?i)(https?://)[^/@\s]+:[^@\s]+@")


def sanitize_log_text(text: object) -> str:
    """移除日志文本中已知的凭据模式，保留其余诊断原文。"""
    try:
        rendered = text if isinstance(text, str) else str(text)
    except Exception:
        return "[UNPRINTABLE]"
    rendered = _TOKEN_PATTERN.sub("[REDACTED_TOKEN]", rendered)
    rendered = _BEARER_PATTERN.sub("Bearer [REDACTED]", rendered)
    rendered = _TOKEN_SCHEME_PATTERN.sub("Token [REDACTED]", rendered)
    rendered = _URL_USERINFO_PATTERN.sub(r"\1[REDACTED]@", rendered)
    rendered = _SENSITIVE_HEADER_PATTERN.sub(
        lambda m: f"{m.group(1)}=[REDACTED]",
        rendered,
    )
    rendered = _SENSITIVE_VALUE_PATTERN.sub(
        lambda m: f"{m.group(1)}=[REDACTED]",
        rendered,
    )
    return rendered


def sanitize_exception(exc: BaseException) -> str:
    """格式化异常完整 traceback 后清洗凭据。

    保留异常类型、stack frame 路径、行号、函数名等非敏感原文。
    non-BaseException 输入安全返回空字符串。
    """
    if not isinstance(exc, BaseException):
        return ""
    raw = "".join(tb_module.format_exception(type(exc), exc, exc.__traceback__))
    return sanitize_log_text(raw)


class DeliveryAttemptTracker:
    """记录当前 owner 是否已经跨过平台发送边界。"""

    def __init__(self) -> None:
        self.attempted = False

    def mark(self) -> None:
        self.attempted = True


def _mark_attempt(
    callback: DeliveryAttemptCallback | DeliveryAttemptTracker | None,
) -> None:
    if callback is None:
        return
    mark = getattr(callback, "mark", None)
    if callable(mark):
        mark()
        return
    cast(DeliveryAttemptCallback, callback)()


class Sender:
    """消息发送器，使用 AstrBot context.send_message 进行投递。"""

    def __init__(
        self, context: Context, enable_private_notifications: bool = False
    ) -> None:
        self._context = context
        self._enable_private_notifications = enable_private_notifications

    def preflight_private_notification_policy(
        self,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """渲染前检查所选目标是否全部被私聊通知策略跳过。"""
        targets = _resolve_targets(endpoint, target_alias)
        if not targets:
            return None

        skipped = [
            _private_policy_result(target)
            for target in targets
            if _should_skip_target(self, target)
        ]
        return skipped if len(skipped) == len(targets) else None

    async def send_text(
        self,
        text: str,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback: DeliveryAttemptCallback
        | DeliveryAttemptTracker
        | None = None,
        delivery_context: DeliveryContext | None = None,
    ) -> list[dict[str, Any]]:
        """向 endpoint 绑定的目标发送纯文本消息。

        Args:
            text: 要发送的文本。
            endpoint: Endpoint 记录。
            target_alias: 可选的目标别名，必须是 endpoint.targets 白名单内的别名。
                          None 表示发送给所有绑定目标。
            delivery_attempt_callback: 可选发送边界回调。
            delivery_context: 可选投递日志上下文（request_id / provider / endpoint_name）。

        Returns:
            每个目标的发送结果列表：
            [{"name": str, "ok": bool, "error": str | None}, ...]
        """
        targets = _resolve_targets(endpoint, target_alias)
        if not targets:
            logger.warning("[WebhookNotifier] reason=no_targets")
            return [{"name": None, "ok": False, "error": "no_targets"}]

        preflight_results = self.preflight_private_notification_policy(
            endpoint, target_alias
        )
        if preflight_results is not None:
            return preflight_results

        # 消息链
        message_chain = MessageChain()
        message_chain.chain.append(Plain(text))
        message_chain.use_t2i(False)

        results: list[dict[str, Any]] = []
        for tgt in targets:
            result = await self._send_to_target(
                tgt, message_chain, delivery_attempt_callback, delivery_context
            )
            results.append(result)

        return results

    async def send_image(
        self,
        image_result: str | bytes,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback: DeliveryAttemptCallback
        | DeliveryAttemptTracker
        | None = None,
        delivery_context: DeliveryContext | None = None,
    ) -> list[dict[str, Any]]:
        """向 endpoint 绑定的目标发送图片消息。

        图片结果必须是已渲染生成的图片，不再经过 T2I。
        支持的 image_result 类型：
        - str URL（http:// 或 https://）
        - str ``base64://...``
        - str ``data:image/...;base64,...``
        - str 本地文件路径
        - bytes

        Args:
            image_result: 图片渲染结果（URL / base64 / 路径 / bytes）。
            endpoint: Endpoint 记录。
            target_alias: 可选的目标别名。
            delivery_attempt_callback: 可选发送边界回调。
            delivery_context: 可选投递日志上下文。

        Returns:
            每个目标的发送结果列表。
        """
        if delivery_attempt_callback is None and delivery_context is None:
            return await self.send_images([image_result], endpoint, target_alias)
        return await self.send_images(
            [image_result],
            endpoint,
            target_alias,
            delivery_attempt_callback=delivery_attempt_callback,
            delivery_context=delivery_context,
        )

    async def send_images(
        self,
        image_results: Sequence[str | bytes],
        endpoint: EndpointRecord,
        target_alias: str | None = None,
        delivery_attempt_callback: DeliveryAttemptCallback
        | DeliveryAttemptTracker
        | None = None,
        delivery_context: DeliveryContext | None = None,
    ) -> list[dict[str, Any]]:
        """向 endpoint 绑定的目标发送一条多图消息链。

        Args:
            image_results: 图片结果序列（1～2 个）。
            endpoint: Endpoint 记录。
            target_alias: 可选的目标别名。
            delivery_attempt_callback: 可选发送边界回调。
            delivery_context: 可选投递日志上下文。
        """
        if isinstance(image_results, (str, bytes)) or not isinstance(
            image_results, Sequence
        ):
            return [{"name": None, "ok": False, "error": "invalid_image_count"}]

        image_values = list(image_results)
        if not 1 <= len(image_values) <= 2:
            return [{"name": None, "ok": False, "error": "invalid_image_count"}]

        targets = _resolve_targets(endpoint, target_alias)
        if not targets:
            logger.warning("[WebhookNotifier] reason=no_targets")
            return [{"name": None, "ok": False, "error": "no_targets"}]

        preflight_results = self.preflight_private_notification_policy(
            endpoint, target_alias
        )
        if preflight_results is not None:
            return preflight_results

        # 先构造全部图片组件；任一失败时整条消息不发送。
        images: list[Image] = []
        for image_result in image_values:
            try:
                image = self._build_image_component(image_result)
            except Exception as exc:
                logger.warning(
                    "[WebhookNotifier] reason=unsupported_image_result "
                    f"type={type(exc).__name__}"
                )
                image = None
            if image is None:
                logger.warning(
                    "[WebhookNotifier] reason=unsupported_image_result "
                    "action=message_not_sent"
                )
                return [
                    {
                        "name": None,
                        "ok": False,
                        "error": "unsupported_image_result",
                    }
                ]
            images.append(image)

        # 消息链 — 图片已生成，不再 T2I
        message_chain = MessageChain()
        message_chain.chain.extend(images)
        message_chain.use_t2i(False)

        results: list[dict[str, Any]] = []
        for tgt in targets:
            result = await self._send_to_target(
                tgt, message_chain, delivery_attempt_callback, delivery_context
            )
            results.append(result)

        return results

    @staticmethod
    def _build_image_component(image_result: str | bytes) -> Image | None:
        """根据 image_result 类型构造 Image 组件。

        Args:
            image_result: 图片渲染结果。

        Returns:
            Image 组件，或 None（无法识别的类型）。
        """
        if isinstance(image_result, str):
            result_str = image_result.strip()

            # base64:// 前缀 — 直接传给 Image
            if result_str.startswith("base64://"):
                return Image(file=result_str)

            # data:image/...;base64,... — 直接传给 Image
            if result_str.startswith("data:"):
                return Image(file=result_str)

            # URL
            if result_str.startswith("http://") or result_str.startswith("https://"):
                return Image(file=result_str)

            # 本地路径
            import os

            if os.path.exists(result_str):
                return Image(file=result_str)

            # 尝试作为纯 base64 解码
            try:
                import base64

                decoded = base64.b64decode(result_str)
                return Image(file=decoded)
            except Exception:
                pass

            logger.warning("[WebhookNotifier] reason=unsupported_image_result")
            return None

        if isinstance(image_result, bytes):
            return Image(file=image_result)

        logger.warning("[WebhookNotifier] reason=unsupported_image_result")
        return None

    async def _send_to_target(
        self,
        target: TargetAlias,
        message_chain: MessageChain,
        delivery_attempt_callback: DeliveryAttemptCallback
        | DeliveryAttemptTracker
        | None = None,
        delivery_context: DeliveryContext | None = None,
    ) -> dict[str, Any]:
        """发送消息到单个目标。

        Args:
            target: 目标别名。
            message_chain: 消息链。
            delivery_attempt_callback: 可选发送边界回调。
            delivery_context: 可选投递日志上下文。
        """
        if _should_skip_target(self, target):
            return _private_policy_result(target)

        _start = time.monotonic()
        try:
            _mark_attempt(delivery_attempt_callback)
            sent = await self._context.send_message(target.umo, message_chain)
            elapsed_ms = int((time.monotonic() - _start) * 1000)
            if not sent:
                _log_delivery(
                    logger.error,
                    reason="session_not_found",
                    target_name=target.name,
                    elapsed_ms=elapsed_ms,
                    delivery_context=delivery_context,
                )
                return {"name": target.name, "ok": False, "error": "session_not_found"}
            _log_delivery(
                logger.info,
                reason="message_sent",
                target_name=target.name,
                elapsed_ms=elapsed_ms,
                delivery_context=delivery_context,
            )
            return {"name": target.name, "ok": True, "error": None}
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - _start) * 1000)
            _log_delivery_exception(
                exc,
                target_name=target.name,
                elapsed_ms=elapsed_ms,
                delivery_context=delivery_context,
            )
            return {"name": target.name, "ok": False, "error": "send_failed"}


def _log_delivery(
    log_fn: Any,
    *,
    reason: str,
    target_name: str,
    elapsed_ms: int,
    delivery_context: DeliveryContext | None = None,
) -> None:
    """记录单次投递结果日志（成功或 session_not_found）。"""
    parts = [
        "[WebhookNotifier]",
        "phase=delivery",
        f"reason={reason}",
        f"target={target_name}",
        f"elapsed_ms={elapsed_ms}",
    ]
    if delivery_context:
        parts.append(f"request_id={delivery_context.request_id}")
        parts.append(f"provider={delivery_context.provider}")
        parts.append(f"endpoint={delivery_context.endpoint_name}")
    log_fn(" ".join(parts))


def _log_delivery_exception(
    exc: BaseException,
    *,
    target_name: str,
    elapsed_ms: int,
    delivery_context: DeliveryContext | None = None,
) -> None:
    """记录投递异常日志：保留 sanitized traceback 和上下文。"""
    parts = [
        "[WebhookNotifier]",
        "phase=delivery",
        "reason=send_failed",
        f"target={target_name}",
        f"elapsed_ms={elapsed_ms}",
        f"exc_type={type(exc).__name__}",
    ]
    if delivery_context:
        parts.append(f"request_id={delivery_context.request_id}")
        parts.append(f"provider={delivery_context.provider}")
        parts.append(f"endpoint={delivery_context.endpoint_name}")
    summary = " ".join(parts)
    # 安全格式化 traceback 并清洗凭据后记录
    try:
        sanitized_tb = sanitize_exception(exc)
    except Exception:
        sanitized_tb = (
            f"[WebhookNotifier] sanitize_exception failed for type={type(exc).__name__}"
        )
    logger.error(f"{summary}\n{sanitized_tb}")


def _should_skip_target(sender: Sender, target: TargetAlias) -> bool:
    return not sender._enable_private_notifications and _is_private_umo(target.umo)


def _is_private_umo(umo: str) -> bool:
    parts = umo.split(":", 2)
    return len(parts) >= 2 and parts[1] == "FriendMessage"


def _private_policy_result(target: TargetAlias) -> dict[str, Any]:
    return {
        "name": target.name,
        "ok": True,
        "skipped": True,
        "error": None,
        "reason": "private_notifications_disabled",
    }


def _resolve_targets(
    endpoint: EndpointRecord, target_alias: str | None
) -> list[TargetAlias]:
    """解析目标列表。

    如果指定了 target_alias，则只返回白名单中匹配的别名。
    如果未指定，返回所有目标。
    """
    if not endpoint.targets:
        return []

    if target_alias is None:
        return list(endpoint.targets)

    matched = [t for t in endpoint.targets if t.name == target_alias]
    return matched
