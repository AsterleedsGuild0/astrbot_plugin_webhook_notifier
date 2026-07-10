"""Stubs for astrbot.api.message_components."""

from __future__ import annotations


class Plain:
    """纯文本组件。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"Plain({self.text!r})"


class Image:
    """图片组件。

    支持 file（URL/base64/bytes/path）、path（本地路径）、url（URL）等。
    """

    def __init__(
        self,
        file: str | bytes | None = None,
        path: str | None = None,
        url: str | None = None,
        base64: str | None = None,
    ) -> None:
        self.file = file
        self.path = path
        self.url = url
        self.base64 = base64

    def __repr__(self) -> str:
        return f"Image(file={self.file!r})"
