from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context

from .models import EndpointRecord, TargetAlias


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
        targets = self._resolve_targets(endpoint, target_alias)
        if not targets:
            return None

        skipped = [
            self._private_policy_result(target)
            for target in targets
            if self._should_skip_target(target)
        ]
        return skipped if len(skipped) == len(targets) else None

    async def send_text(
        self,
        text: str,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
    ) -> list[dict[str, Any]]:
        """向 endpoint 绑定的目标发送纯文本消息。

        Args:
            text: 要发送的文本。
            endpoint: Endpoint 记录。
            target_alias: 可选的目标别名，必须是 endpoint.targets 白名单内的别名。
                          None 表示发送给所有绑定目标。

        Returns:
            每个目标的发送结果列表：
            [{"name": str, "ok": bool, "error": str | None}, ...]
        """
        targets = self._resolve_targets(endpoint, target_alias)
        if not targets:
            logger.warning(f"[WebhookNotifier] endpoint {endpoint.name} 没有可用的目标")
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
            result = await self._send_to_target(tgt, message_chain)
            results.append(result)

        return results

    async def send_image(
        self,
        image_result: str | bytes,
        endpoint: EndpointRecord,
        target_alias: str | None = None,
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

        Returns:
            每个目标的发送结果列表。
        """
        targets = self._resolve_targets(endpoint, target_alias)
        if not targets:
            logger.warning(f"[WebhookNotifier] endpoint {endpoint.name} 没有可用的目标")
            return [{"name": None, "ok": False, "error": "no_targets"}]

        preflight_results = self.preflight_private_notification_policy(
            endpoint, target_alias
        )
        if preflight_results is not None:
            return preflight_results

        # 构造图片组件
        image = self._build_image_component(image_result)
        if image is None:
            return [{"name": None, "ok": False, "error": "unsupported_image_result"}]

        # 消息链 — 图片已生成，不再 T2I
        message_chain = MessageChain()
        message_chain.chain.append(image)
        message_chain.use_t2i(False)

        results: list[dict[str, Any]] = []
        for tgt in targets:
            result = await self._send_to_target(tgt, message_chain)
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

            logger.warning(
                f"[WebhookNotifier] 无法识别的图片结果字符串: {result_str[:80]}..."
            )
            return None

        if isinstance(image_result, bytes):
            return Image(file=image_result)

        logger.warning(
            f"[WebhookNotifier] 不支持的图片结果类型: {type(image_result).__name__}"
        )
        return None

    async def _send_to_target(
        self,
        target: TargetAlias,
        message_chain: MessageChain,
    ) -> dict[str, Any]:
        """发送消息到单个目标。"""
        if self._should_skip_target(target):
            return self._private_policy_result(target)

        try:
            sent = await self._context.send_message(target.umo, message_chain)
            if not sent:
                logger.error(
                    f"[WebhookNotifier] 发送到目标 {target.name} ({target.umo}) 失败: 找不到平台会话"
                )
                return {"name": target.name, "ok": False, "error": "session_not_found"}
            logger.info(
                f"[WebhookNotifier] 消息已发送到目标 {target.name} ({target.umo})"
            )
            return {"name": target.name, "ok": True, "error": None}
        except Exception as e:
            logger.error(
                f"[WebhookNotifier] 发送到目标 {target.name} ({target.umo}) 失败: {e}"
            )
            return {"name": target.name, "ok": False, "error": str(e)}

    def _should_skip_target(self, target: TargetAlias) -> bool:
        return not self._enable_private_notifications and self._is_private_umo(
            target.umo
        )

    @staticmethod
    def _is_private_umo(umo: str) -> bool:
        parts = umo.split(":", 2)
        return len(parts) >= 2 and parts[1] == "FriendMessage"

    @staticmethod
    def _private_policy_result(target: TargetAlias) -> dict[str, Any]:
        return {
            "name": target.name,
            "ok": True,
            "skipped": True,
            "error": None,
            "reason": "private_notifications_disabled",
        }

    @staticmethod
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
