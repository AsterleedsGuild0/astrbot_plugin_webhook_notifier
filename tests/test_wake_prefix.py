from __future__ import annotations

# ruff: noqa: E402

import ast
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.star import Context
from astrbot_plugin_webhook_notifier.main import (
    _TokenDeliveryText,
    WebhookNotifierPlugin,
)


UMO = "aiocqhttp:FriendMessage:wake-prefix-test"


class PlainResultStub:
    def __init__(self, text: str) -> None:
        self.text = text
        self.t2i = True

    def use_t2i(self, value: bool):
        self.t2i = value
        return self


class WakeEventStub:
    def __init__(self, message_str: str = "whn help") -> None:
        self.message_str = message_str
        self.unified_msg_origin = UMO
        self.session = SimpleNamespace(message_type="friend")

    def is_admin(self) -> bool:
        return False

    def plain_result(self, text: str) -> PlainResultStub:
        return PlainResultStub(text)

    def chain_result(self, components) -> MessageChain:
        return MessageChain(list(components))

    async def send(self, _chain) -> None:
        raise RuntimeError("direct send failed")


@pytest.mark.parametrize(
    ("wake_prefix", "short_root", "long_root"),
    [
        (["/"], "/whn", "/webhook_notifier"),
        (["!"], "!whn", "!webhook_notifier"),
        (["?", "!"], "?whn", "?webhook_notifier"),
        ([], "whn", "webhook_notifier"),
        ([""], "whn", "webhook_notifier"),
    ],
)
def test_runtime_resolver_accepts_valid_wake_prefixes(
    wake_prefix, short_root, long_root
):
    context = Context(configs={UMO: {"wake_prefix": wake_prefix}})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())

    commands = plugin._resolve_command_roots(WakeEventStub())

    assert commands.short == short_root
    assert commands.long == long_root
    assert commands.config_error is False
    assert context.get_config_calls == [UMO]


@pytest.mark.parametrize(
    ("config", "category"),
    [
        ([], "config_container_type"),
        ({}, "wake_prefix_missing"),
        ({"wake_prefix": "/"}, "wake_prefix_container_type"),
        ({"wake_prefix": ("/",)}, "wake_prefix_container_type"),
        ({"wake_prefix": ["/", 1]}, "wake_prefix_element_type"),
        ({"wake_prefix": ["!\n"]}, "wake_prefix_control_character"),
        ({"wake_prefix": ["!\x00"]}, "wake_prefix_control_character"),
    ],
)
def test_runtime_resolver_rejects_invalid_config_without_logging_values(
    config, category, caplog
):
    context = Context(configs={UMO: config})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())
    caplog.set_level(logging.ERROR, logger="astrbot")

    commands = plugin._resolve_command_roots(WakeEventStub())

    assert commands.short == "<AstrBot唤醒词>whn"
    assert commands.long == "<AstrBot唤醒词>webhook_notifier"
    assert commands.config_error is True
    assert category in caplog.text
    assert UMO not in caplog.text
    assert repr(config) not in caplog.text


def test_runtime_resolver_handles_get_config_exception_without_sensitive_log(caplog):
    context = Context(config_exceptions={UMO: RuntimeError("secret-config-value")})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())
    caplog.set_level(logging.ERROR, logger="astrbot")

    commands = plugin._resolve_command_roots(WakeEventStub())

    assert commands.config_error is True
    assert "get_config_exception" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "secret-config-value" not in caplog.text
    assert UMO not in caplog.text


@pytest.mark.asyncio
async def test_help_html_uses_complete_escaped_commands_and_reads_config_once():
    context = Context(configs={UMO: {"wake_prefix": ["<script>"]}})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())
    captured: dict[str, str] = {}

    async def fake_html_render(_tmpl, data, **_kwargs):
        captured["html"] = data["rendered_html"]
        return b"\x89PNG\r\n\x1a\n"

    plugin.html_render = fake_html_render  # type: ignore[method-assign]

    results = [item async for item in plugin.status_short(WakeEventStub())]

    assert isinstance(results[0], MessageChain)
    assert "&lt;script&gt;whn token list" in captured["html"]
    assert "&lt;script&gt;whn token delete &lt;名称&gt;" in captured["html"]
    assert "<script>whn" not in captured["html"]
    assert "当前命令根" in captured["html"]
    assert context.get_config_calls == [UMO]


@pytest.mark.asyncio
async def test_help_text_fallback_keeps_placeholder_and_diagnostic_on_config_error():
    context = Context(configs={UMO: {}})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())

    async def failing_html_render(*_args, **_kwargs):
        raise RuntimeError("T2I unavailable")

    plugin.html_render = failing_html_render  # type: ignore[method-assign]

    results = [item async for item in plugin.status_short(WakeEventStub())]

    assert isinstance(results[0], PlainResultStub)
    assert "<AstrBot唤醒词>whn token list" in results[0].text
    assert "<AstrBot唤醒词>whn token delete <名称>" in results[0].text
    assert "无法读取当前会话唤醒词，请检查 AstrBot 配置和插件日志" in results[0].text
    assert context.get_config_calls == [UMO]


@pytest.mark.asyncio
async def test_help_html_keeps_placeholder_and_diagnostic_on_config_error():
    context = Context(configs={UMO: {"wake_prefix": ["bad\n"]}})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())
    captured: dict[str, str] = {}

    async def fake_html_render(_tmpl, data, **_kwargs):
        captured["html"] = data["rendered_html"]
        return b"\x89PNG\r\n\x1a\n"

    plugin.html_render = fake_html_render  # type: ignore[method-assign]

    results = [item async for item in plugin.status_short(WakeEventStub())]

    assert isinstance(results[0], MessageChain)
    assert "&lt;AstrBot唤醒词&gt;whn token list" in captured["html"]
    assert "无法读取当前会话唤醒词，请检查 AstrBot 配置和插件日志" in captured["html"]


@pytest.mark.asyncio
async def test_status_and_unknown_command_use_current_runtime_roots():
    context = Context(configs={UMO: {"wake_prefix": ["!"]}})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())

    status = [item async for item in plugin.status_short(WakeEventStub("whn"))]
    unknown = [item async for item in plugin.status_short(WakeEventStub("whn unknown"))]

    assert "!whn" in status[0].text
    assert "!webhook_notifier" in status[0].text
    assert "发送 !whn help" in unknown[0].text


@pytest.mark.asyncio
async def test_direct_send_failure_recovery_uses_current_runtime_command():
    context = Context(configs={UMO: {"wake_prefix": ["!"]}})
    plugin = WebhookNotifierPlugin(context, AstrBotConfig())

    async def return_sensitive(_event, _args, _commands):
        return _TokenDeliveryText(
            "Bearer Token: whn_TEST_ONLY_1234567890123456789012345678901234567",
            "recover-me",
        )

    plugin._dispatch_whn_command = return_sensitive  # type: ignore[method-assign]

    results = [
        item async for item in plugin.status_short(WakeEventStub("whn token rotate"))
    ]

    assert len(results) == 1
    assert "!whn token rotate recover-me" in results[0].text
    assert "/whn" not in results[0].text


def test_main_user_visible_string_literals_do_not_hardcode_default_commands():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
    }
    offending = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ("/whn" in node.value or "/webhook_notifier" in node.value)
        and node.value not in docstrings
    ]

    assert offending == []
