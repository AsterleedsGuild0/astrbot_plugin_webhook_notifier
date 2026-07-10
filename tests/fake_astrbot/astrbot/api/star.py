"""Stubs for astrbot.api.star."""

from __future__ import annotations

from typing import Any

from .event import MessageChain


class Context:
    """AstrBot 上下文，用于发送消息等操作。"""

    def __init__(self) -> None:
        self._sent_messages: list[tuple[str, MessageChain]] = []

    async def send_message(self, umo: str, message_chain: MessageChain) -> bool:
        """发送消息。

        Args:
            umo: 目标 UMO。
            message_chain: 消息链。

        Returns:
            True 表示发送成功，False 表示失败。
        """
        self._sent_messages.append((umo, message_chain))
        return True

    def get_last_sent(self) -> tuple[str, MessageChain] | None:
        if not self._sent_messages:
            return None
        return self._sent_messages[-1]

    def clear_sent(self) -> None:
        self._sent_messages.clear()


class Star:
    """插件基类，AstrBot 插件的基类。"""

    def __init__(self, context: Context) -> None:
        self.context = context

    async def html_render(
        self,
        tmpl: str,
        data: dict[str, Any],
        return_url: bool = True,
        options: dict[str, Any] | None = None,
    ) -> str | bytes:
        """渲染 HTML 为图片。

        Args:
            tmpl: HTML 模板字符串。
            data: 模板数据。
            return_url: 是否返回 URL 而非 bytes。
            options: T2I 渲染选项。

        Returns:
            URL 字符串或图片 bytes。
        """
        raise NotImplementedError("Unit test stub — implement mock")


class register:
    """插件注册装饰器。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __call__(self, cls: type) -> type:
        return cls


class StarTools:
    """插件工具类。"""

    @staticmethod
    def get_data_dir(plugin_name: str) -> str:
        import tempfile
        import os

        return tempfile.mkdtemp(prefix=f"{plugin_name}_")
