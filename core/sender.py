from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context

from .models import EndpointRecord, TargetAlias


class Sender:
    """消息发送器，使用 AstrBot context.send_message 进行投递。"""

    def __init__(self, context: Context) -> None:
        self._context = context

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

        # 消息链
        message_chain = MessageChain()
        message_chain.chain.append(Plain(text))
        message_chain.use_t2i(False)

        results: list[dict[str, Any]] = []
        for tgt in targets:
            result = await self._send_to_target(tgt, message_chain)
            results.append(result)

        return results

    async def _send_to_target(
        self,
        target: TargetAlias,
        message_chain: MessageChain,
    ) -> dict[str, Any]:
        """发送消息到单个目标。"""
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
