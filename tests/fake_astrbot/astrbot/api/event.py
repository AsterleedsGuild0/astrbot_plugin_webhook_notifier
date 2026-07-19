"""Stubs for astrbot.api.event."""

from __future__ import annotations

from typing import Any


class MessageChain:
    """消息链。

    包含 Plain、Image 等组件的列表。通过 .chain 属性访问组件。
    """

    def __init__(self, components: list[Any] | None = None) -> None:
        self.chain: list[Any] = components or []
        self._use_t2i_value: bool = True
        self._use_markdown_value: bool = True

    def use_t2i(self, value: bool) -> MessageChain:
        self._use_t2i_value = value
        return self

    def get_use_t2i(self) -> bool:
        return self._use_t2i_value

    def use_markdown(self, value: bool) -> MessageChain:
        self._use_markdown_value = value
        return self

    def get_use_markdown(self) -> bool:
        return self._use_markdown_value

    def __repr__(self) -> str:
        return f"MessageChain({self.chain!r})"


class AstrMessageEvent:
    """消息事件，仅用于类型标注。"""

    pass


class filter:
    """命令过滤器装饰器，仅用于类型标注。"""

    @staticmethod
    def command(name: str):
        def decorator(func):
            func._command = name
            return func

        return decorator
