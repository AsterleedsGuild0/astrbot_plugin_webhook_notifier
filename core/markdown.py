from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from markupsafe import Markup

from .models import NormalizedEvent
from .notification_policy import SessionScope
from .providers import ProviderAdapter, ProviderError

MARKDOWN_EVENT = "markdown.message"
MAX_MARKDOWN_CHARS = 32_768
MAX_MARKDOWN_BYTES = 64 * 1024
MAX_MARKDOWN_TITLE = 200
MAX_MARKDOWN_ID = 128
MAX_TARGET_ALIAS = 128
MAX_LINK_URL = 2048
MAX_INLINE_NESTING = 16
MAX_INLINE_OPERATIONS = 400_000
INLINE_OPERATION_MULTIPLIER = 12

_ALLOWED_FIELDS = frozenset({"event", "id", "title", "markdown", "target_alias"})
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
_UNORDERED_RE = re.compile(r"^ {0,3}[-+*][ \t]+(.+)$")
_ORDERED_RE = re.compile(r"^ {0,3}(\d{1,9})[.)][ \t]+(.+)$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class MarkdownBlock:
    kind: str
    text: str = ""
    level: int = 0
    items: tuple[str, ...] = ()
    start: int = 1


class _InlineBudgetExceeded(RuntimeError):
    """受限 Markdown inline parser 已达到确定性预算。"""


@dataclass
class _InlineBudget:
    remaining: int

    @classmethod
    def for_text(cls, text: str) -> _InlineBudget:
        return cls(
            min(
                MAX_INLINE_OPERATIONS,
                max(4096, len(text) * INLINE_OPERATION_MULTIPLIER + 1024),
            )
        )

    def consume(self, amount: int = 1) -> None:
        amount = max(1, amount)
        if amount > self.remaining:
            raise _InlineBudgetExceeded
        self.remaining -= amount


def _invalid(field: str, message: str | None = None) -> ProviderError:
    return ProviderError(
        "invalid_payload",
        message or f"无效的 {field} 字段",
        retryable=False,
    )


def _bounded_optional_text(
    value: Any,
    field: str,
    *,
    maximum: int,
    single_line: bool,
) -> str:
    if not isinstance(value, str):
        raise _invalid(field)
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or _CONTROL_RE.search(cleaned):
        raise _invalid(field)
    if single_line and ("\n" in cleaned or "\r" in cleaned):
        raise _invalid(field)
    return cleaned


def normalize_markdown_payload(
    payload: dict[str, Any], received_at: str
) -> NormalizedEvent:
    unknown = set(payload) - _ALLOWED_FIELDS
    if unknown:
        raise _invalid("payload", "请求包含不支持的字段")

    if payload.get("event") != MARKDOWN_EVENT:
        raise ProviderError(
            "unsupported_event",
            f"event 必须是 {MARKDOWN_EVENT}",
            retryable=False,
        )

    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        raise _invalid("markdown")
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not markdown:
        raise _invalid("markdown", "markdown 必须是非空字符串")
    if (
        len(markdown) > MAX_MARKDOWN_CHARS
        or len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES
    ):
        raise ProviderError(
            "payload_too_large",
            "markdown 内容超过长度限制",
            retryable=False,
        )
    if _CONTROL_RE.search(markdown):
        raise _invalid("markdown")

    title = "Markdown 通知"
    if "title" in payload:
        title = _bounded_optional_text(
            payload["title"], "title", maximum=MAX_MARKDOWN_TITLE, single_line=True
        )

    event_id = ""
    if "id" in payload:
        event_id = _bounded_optional_text(
            payload["id"], "id", maximum=MAX_MARKDOWN_ID, single_line=True
        )

    if "target_alias" in payload:
        _bounded_optional_text(
            payload["target_alias"],
            "target_alias",
            maximum=MAX_TARGET_ALIAS,
            single_line=True,
        )

    return NormalizedEvent(
        provider="markdown",
        event=MARKDOWN_EVENT,
        id=event_id,
        emitted_at=received_at,
        title=title,
        status="info",
        session_scope=SessionScope.UNKNOWN,
        source={"name": "Markdown", "url": None},
        markdown=markdown,
    )


class MarkdownProviderAdapter(ProviderAdapter):
    @property
    def provider(self) -> str:
        return "markdown"

    def parse(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        received_at: str,
    ) -> NormalizedEvent:
        del headers
        return normalize_markdown_payload(payload, received_at)


def parse_markdown_blocks(markdown: str) -> list[MarkdownBlock]:
    lines = markdown.split("\n")
    blocks: list[MarkdownBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            code: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                closing = re.match(r"^ {0,3}([`~]{3,})\s*$", candidate)
                if (
                    closing
                    and closing.group(1)[0] == marker[0]
                    and len(closing.group(1)) >= len(marker)
                ):
                    index += 1
                    break
                code.append(candidate)
                index += 1
            blocks.append(MarkdownBlock("code", text="\n".join(code)))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append(
                MarkdownBlock(
                    "heading", text=heading.group(2), level=len(heading.group(1))
                )
            )
            index += 1
            continue

        unordered = _UNORDERED_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if unordered or ordered:
            kind = "ordered_list" if ordered else "unordered_list"
            start = int(ordered.group(1)) if ordered else 1
            items: list[str] = []
            while index < len(lines):
                match = (
                    _ORDERED_RE.match(lines[index])
                    if ordered
                    else _UNORDERED_RE.match(lines[index])
                )
                if not match:
                    break
                items.append(match.group(2) if ordered else match.group(1))
                index += 1
            blocks.append(MarkdownBlock(kind, items=tuple(items), start=start))
            continue

        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index]
            if (
                _FENCE_RE.match(candidate)
                or _HEADING_RE.match(candidate)
                or _UNORDERED_RE.match(candidate)
                or _ORDERED_RE.match(candidate)
            ):
                break
            paragraph.append(candidate)
            index += 1
        blocks.append(MarkdownBlock("paragraph", text="\n".join(paragraph)))
    return blocks


def _budgeted_find(text: str, needle: str, start: int, budget: _InlineBudget) -> int:
    position = text.find(needle, start)
    scanned = (position - start + len(needle)) if position >= 0 else len(text) - start
    budget.consume(scanned)
    return position


def _find_closing(text: str, marker: str, start: int, budget: _InlineBudget) -> int:
    position = _budgeted_find(text, marker, start, budget)
    while position >= 0:
        if position > start and text[position - 1] != "\\":
            return position
        position = _budgeted_find(text, marker, position + len(marker), budget)
    return -1


def _link_at(
    text: str, start: int, budget: _InlineBudget
) -> tuple[int, str, str] | None:
    if text[start] != "[" or (start > 0 and text[start - 1] == "!"):
        return None
    label_end = _budgeted_find(text, "](", start + 1, budget)
    if label_end < 0:
        return None
    url_end = _budgeted_find(text, ")", label_end + 2, budget)
    if url_end < 0:
        return None
    return url_end + 1, text[start + 1 : label_end], text[label_end + 2 : url_end]


def _image_at(
    text: str, start: int, budget: _InlineBudget
) -> tuple[int, str, str] | None:
    if not text.startswith("![", start):
        return None
    label_end = _budgeted_find(text, "](", start + 2, budget)
    if label_end < 0:
        return None
    url_end = _budgeted_find(text, ")", label_end + 2, budget)
    if url_end < 0:
        return None
    return url_end + 1, text[start + 2 : label_end], text[label_end + 2 : url_end]


def _safe_link_url(url: str) -> str | None:
    candidate = url.strip()
    if (
        not candidate
        or len(candidate) > MAX_LINK_URL
        or any(ch.isspace() for ch in candidate)
    ):
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _render_inline_html(text: str, budget: _InlineBudget, *, depth: int) -> str:
    if depth > MAX_INLINE_NESTING:
        raise _InlineBudgetExceeded
    output: list[str] = []
    index = 0
    while index < len(text):
        budget.consume()
        image = _image_at(text, index, budget)
        if image:
            end, _label, _url = image
            output.append(html.escape(text[index:end]))
            index = end
            continue

        link = _link_at(text, index, budget)
        if link:
            end, label, raw_url = link
            safe_url = _safe_link_url(raw_url)
            if safe_url is not None:
                output.append(
                    '<a href="'
                    + html.escape(safe_url, quote=True)
                    + '" rel="noreferrer noopener">'
                    + _render_inline_html(label, budget, depth=depth + 1)
                    + "</a>"
                )
            else:
                output.append(html.escape(text[index:end]))
            index = end
            continue

        if text[index] == "`":
            end = _find_closing(text, "`", index + 1, budget)
            if end >= 0:
                output.append("<code>" + html.escape(text[index + 1 : end]) + "</code>")
                index = end + 1
                continue

        marker = next(
            (
                value
                for value in ("**", "__", "*", "_")
                if text.startswith(value, index)
            ),
            None,
        )
        if marker:
            end = _find_closing(text, marker, index + len(marker), budget)
            if end >= 0:
                tag = "strong" if len(marker) == 2 else "em"
                output.append(
                    f"<{tag}>"
                    + _render_inline_html(
                        text[index + len(marker) : end], budget, depth=depth + 1
                    )
                    + f"</{tag}>"
                )
                index = end + len(marker)
                continue

        if (
            text[index] == "\\"
            and index + 1 < len(text)
            and text[index + 1] in r"\\`*_[]()"
        ):
            output.append(html.escape(text[index + 1]))
            index += 2
            continue

        if text[index] == "\n":
            output.append("<br>")
        else:
            output.append(html.escape(text[index]))
        index += 1
    return "".join(output)


def render_inline_html(text: str) -> Markup:
    """在全局预算和固定嵌套深度内渲染 inline Markdown。

    超预算或超深时将完整 inline 输入降级为 HTML 转义文本，避免请求进入
    RecursionError、无界扫描或 HTTP 500。
    """

    try:
        return Markup(_render_inline_html(text, _InlineBudget.for_text(text), depth=0))
    except _InlineBudgetExceeded:
        return Markup(html.escape(text))


def render_markdown_html(markdown: Any) -> Markup:
    if not isinstance(markdown, str):
        return Markup("")
    output: list[str] = []
    for block in parse_markdown_blocks(markdown):
        if block.kind == "heading":
            level = min(6, max(1, block.level))
            output.append(
                f'<h{level} class="md-heading">{render_inline_html(block.text)}</h{level}>'
            )
        elif block.kind == "paragraph":
            output.append(
                f'<p class="md-paragraph">{render_inline_html(block.text)}</p>'
            )
        elif block.kind == "code":
            output.append(
                '<pre class="md-code-block"><code>'
                + html.escape(block.text)
                + "</code></pre>"
            )
        elif block.kind in {"ordered_list", "unordered_list"}:
            tag = "ol" if block.kind == "ordered_list" else "ul"
            start = (
                f' start="{block.start}"' if tag == "ol" and block.start != 1 else ""
            )
            items = "".join(
                f"<li>{render_inline_html(item)}</li>" for item in block.items
            )
            output.append(f'<{tag} class="md-list"{start}>{items}</{tag}>')
    return Markup("".join(output))


def _inline_plain(text: str, budget: _InlineBudget, *, depth: int) -> str:
    if depth > MAX_INLINE_NESTING:
        raise _InlineBudgetExceeded
    output: list[str] = []
    index = 0
    while index < len(text):
        budget.consume()
        image = _image_at(text, index, budget)
        if image:
            end, _label, _url = image
            output.append(text[index:end])
            index = end
            continue
        link = _link_at(text, index, budget)
        if link:
            end, label, raw_url = link
            safe_url = _safe_link_url(raw_url)
            output.append(
                f"{_inline_plain(label, budget, depth=depth + 1)} ({safe_url})"
                if safe_url
                else text[index:end]
            )
            index = end
            continue
        marker = next(
            (
                value
                for value in ("**", "__", "*", "_", "`")
                if text.startswith(value, index)
            ),
            None,
        )
        if marker:
            end = _find_closing(text, marker, index + len(marker), budget)
            if end >= 0:
                output.append(
                    _inline_plain(
                        text[index + len(marker) : end], budget, depth=depth + 1
                    )
                )
                index = end + len(marker)
                continue
        if text[index] == "\\" and index + 1 < len(text):
            output.append(text[index + 1])
            index += 2
            continue
        output.append(text[index])
        index += 1
    return "".join(output)


def render_inline_text(text: str) -> str:
    """渲染可读纯文本；预算耗尽时保留原文，不抛出到 HTTP 层。"""

    try:
        return _inline_plain(text, _InlineBudget.for_text(text), depth=0)
    except _InlineBudgetExceeded:
        return text


def render_markdown_text(markdown: str) -> str:
    lines: list[str] = []
    for block in parse_markdown_blocks(markdown):
        if lines:
            lines.append("")
        if block.kind in {"heading", "paragraph"}:
            lines.append(render_inline_text(block.text))
        elif block.kind == "code":
            lines.append("代码：")
            lines.extend(block.text.split("\n"))
        elif block.kind == "unordered_list":
            lines.extend(f"- {render_inline_text(item)}" for item in block.items)
        elif block.kind == "ordered_list":
            lines.extend(
                f"{block.start + offset}. {render_inline_text(item)}"
                for offset, item in enumerate(block.items)
            )
    return "\n".join(lines).strip()
