from __future__ import annotations

# ruff: noqa: E402

import sys
import re
from html import unescape
from types import SimpleNamespace
from pathlib import Path

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.star import Context
from astrbot_plugin_webhook_notifier.core.help_card import (
    build_help_text,
    render_help_card_html,
)
from astrbot_plugin_webhook_notifier.core.markdown import MarkdownProviderAdapter
from astrbot_plugin_webhook_notifier.core.omp import OmpProviderAdapter
from astrbot_plugin_webhook_notifier.core.opencode import OpenCodeProviderAdapter
from astrbot_plugin_webhook_notifier.core.providers import ProviderRegistry
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
        self.unified_msg_origin = "aiocqhttp:FriendMessage:test-user"
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
    png_header = b"\x89PNG\r\n\x1a\n"

    async def fake_html_render(*_args, **_kwargs):
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
    text = unescape(re.sub(r"<[^>]+>", "", captured["html"]))
    assert "/whn admin token list" in text
    assert "/whn admin token revoke-path <endpoint-path>" in text
    assert "/whn admin token revoke-owner <platform_id> <owner_user_id> <名称>" in text
    assert "/whn admin config min-duration" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("super_admin", [False, True])
@pytest.mark.parametrize("config_error", [False, True])
@pytest.mark.parametrize("failure", ["exception", "invalid_image"])
async def test_help_render_failure_falls_back_to_plain_text(
    super_admin, config_error, failure
):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())

    async def failing_html_render(*_args, **_kwargs):
        if failure == "exception":
            raise RuntimeError("T2I unavailable")
        return b"not an image"

    plugin.html_render = failing_html_render  # type: ignore[method-assign]
    event = HelpEventStub("whn 帮助", super_admin=super_admin)

    command_root = "<AstrBot唤醒词>whn" if config_error else "/whn"
    result = await plugin._build_help_result(
        event, SimpleNamespace(short=command_root, config_error=config_error)
    )
    assert isinstance(result, PlainResultStub)
    assert result.t2i is False
    assert f"{command_root} token new private [名称]" in result.text
    assert f"{command_root} token delete <名称>" in result.text
    assert result.text == build_help_text(
        super_admin, command_root, config_error,
        providers=plugin._ensure_provider_registry().providers,
    )
    assert ("【管理员工具】" in result.text) is super_admin


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
        "❌ 未知子命令：does-not-exist\n发送 /whn help 查看可用命令。"
    )
    assert "token new private" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "super_admin", "expected"),
    [
        ("whn token unknown", False, "未知 token 子命令：unknown"),
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
    assert result.text.endswith("发送 /whn help 查看可用命令。")
    assert "admin token list" not in result.text
    assert "token new private" not in result.text


def test_help_card_uses_only_syntax_placeholders_without_sensitive_examples():
    html = render_help_card_html(
        True, "/whn", providers=frozenset({"markdown", "omp", "opencode"})
    )

    assert "whn_" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "aiocqhttp" in html
    assert "qq_official" in html
    assert "Bearer " not in html
    assert "123456" not in html


@pytest.mark.parametrize(
    "adapters",
    [
        (MarkdownProviderAdapter,),
        (OmpProviderAdapter, OpenCodeProviderAdapter),
        (OpenCodeProviderAdapter, MarkdownProviderAdapter, OmpProviderAdapter),
    ],
)
@pytest.mark.parametrize("render", [build_help_text, render_help_card_html])
def test_provider_help_options_follow_registered_set(adapters, render):
    registry = ProviderRegistry()
    for adapter in adapters:
        registry.register(adapter())
    registry.freeze()

    rendered = render(False, "/whn", providers=registry.providers)
    if render is render_help_card_html:
        rendered = unescape(re.sub(r"<[^>]+>", "", rendered))
    options = re.findall(r"--provider <([^<>]+)>", rendered)

    assert options
    for option in options:
        assert option.split("|") == sorted(registry.providers)
