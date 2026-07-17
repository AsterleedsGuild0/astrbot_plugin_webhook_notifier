from __future__ import annotations

# ruff: noqa: E402

import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.star import Context
from astrbot_plugin_webhook_notifier.core.help_card import (
    ADMIN_HELP_SECTION,
    HELP_CARD_CANVAS_WIDTH,
    PUBLIC_HELP_SECTIONS,
    build_help_text,
    render_help_card_html,
)
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin


class PlainResultStub:
    def __init__(self, text: str) -> None:
        self.text = text
        self.t2i = True

    def use_t2i(self, value: bool):
        self.t2i = value
        return self


class HelpEventStub:
    def __init__(self, message_str: str, *, super_admin: bool = False) -> None:
        self.message_str = message_str
        self._super_admin = super_admin
        self.session = SimpleNamespace(message_type="friend")

    def is_admin(self) -> bool:
        return self._super_admin

    def get_sender_id(self) -> str:
        return "test-user"

    def plain_result(self, text: str) -> PlainResultStub:
        return PlainResultStub(text)

    def chain_result(self, components) -> MessageChain:
        return MessageChain(list(components))


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["help", "帮助"])
async def test_help_aliases_render_image_result(alias):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    captured: dict[str, Any] = {}
    png_header = b"\x89PNG\r\n\x1a\n"

    async def fake_html_render(tmpl, data, return_url=True, options=None):
        captured.update(
            tmpl=tmpl,
            html=data["rendered_html"],
            return_url=return_url,
            options=options,
        )
        return png_header

    plugin.html_render = fake_html_render  # type: ignore[method-assign]
    event = HelpEventStub(f"whn {alias}")

    yielded = [item async for item in plugin.status_short(event)]

    assert len(yielded) == 1
    result = yielded[0]
    assert isinstance(result, MessageChain)
    assert result.get_use_t2i() is False
    assert len(result.chain) == 1
    assert result.chain[0].file == png_header
    assert captured["return_url"] is False
    assert captured["options"]["viewport_width"] == HELP_CARD_CANVAS_WIDTH
    assert "管理员工具" not in captured["html"]


@pytest.mark.asyncio
async def test_super_admin_help_includes_private_admin_tools():
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    captured: dict[str, str] = {}

    async def fake_html_render(_tmpl, data, **_kwargs):
        captured["html"] = data["rendered_html"]
        return b"\x89PNG\r\n\x1a\n"

    plugin.html_render = fake_html_render  # type: ignore[method-assign]
    event = HelpEventStub("whn help", super_admin=True)

    yielded = [item async for item in plugin.status_short(event)]

    assert isinstance(yielded[0], MessageChain)
    assert "管理员工具" in captured["html"]
    assert "仅 AstrBot 超级管理员 · 仅私聊" in captured["html"]
    assert "admin token list" in captured["html"]
    assert "admin token revoke-path &lt;endpoint-path&gt;" in captured["html"]
    assert (
        "admin token revoke-owner &lt;owner_user_id&gt; &lt;名称&gt;"
        in captured["html"]
    )


@pytest.mark.asyncio
async def test_help_render_failure_falls_back_to_plain_text():
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())

    async def failing_html_render(*_args, **_kwargs):
        raise RuntimeError("T2I unavailable")

    plugin.html_render = failing_html_render  # type: ignore[method-assign]
    event = HelpEventStub("whn 帮助")

    yielded = [item async for item in plugin.status_short(event)]

    assert len(yielded) == 1
    result = yielded[0]
    assert isinstance(result, PlainResultStub)
    assert result.t2i is False
    assert "【创建与验证】" in result.text
    assert "token new private [名称]" in result.text
    assert "管理员工具" not in result.text


@pytest.mark.asyncio
async def test_unknown_subcommand_is_short_and_points_to_help():
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = HelpEventStub("whn does-not-exist")

    yielded = [item async for item in plugin.status_short(event)]

    assert len(yielded) == 1
    result = yielded[0]
    assert isinstance(result, PlainResultStub)
    assert result.t2i is False
    assert result.text == (
        "❌ 未知子命令：does-not-exist\n发送 whn help 查看可用命令。"
    )
    assert "token new private" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "super_admin", "expected"),
    [
        ("whn token delete", False, "未知 token 子命令：delete"),
        ("whn admin token delete", True, "未知 admin token 子命令：delete"),
        ("whn admin delete", True, "未知 admin 子命令：delete"),
    ],
)
async def test_nested_unknown_subcommands_stay_short(command, super_admin, expected):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = HelpEventStub(command, super_admin=super_admin)

    yielded = [item async for item in plugin.status_short(event)]

    result = yielded[0]
    assert isinstance(result, PlainResultStub)
    assert expected in result.text
    assert result.text.endswith("发送 whn help 查看可用命令。")
    assert "admin token list" not in result.text
    assert "token new private" not in result.text


def test_public_and_admin_help_content_are_separated():
    public_html = render_help_card_html(False)
    admin_html = render_help_card_html(True)
    public_text = build_help_text(False)
    admin_text = build_help_text(True)

    assert "管理员工具" not in public_html
    assert "管理员工具" not in public_text
    assert "管理员工具" in admin_html
    assert "管理员工具" in admin_text
    assert "仅 AstrBot 超级管理员 · 仅私聊" in admin_text


def test_help_card_uses_only_syntax_placeholders_without_sensitive_examples():
    html = render_help_card_html(True)
    command_syntaxes = [
        command["syntax"]
        for section in (*PUBLIC_HELP_SECTIONS, ADMIN_HELP_SECTION)
        for command in section["commands"]
    ]

    assert "whn_" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "aiocqhttp" not in html
    assert "Bearer " not in html
    assert "123456" not in html
    assert all(not syntax.startswith(("/", "&")) for syntax in command_syntaxes)
    assert "<群号>" in command_syntaxes[3]
    assert "<owner_user_id>" in command_syntaxes[-1]
