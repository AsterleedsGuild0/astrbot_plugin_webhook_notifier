from __future__ import annotations

# ruff: noqa: E402

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api import AstrBotConfig
from astrbot.api.message_components import Plain
from astrbot.api.star import Context
from astrbot_plugin_webhook_notifier.core.registry import EndpointRegistry
from astrbot_plugin_webhook_notifier.main import (
    _ExactSecretFilter,
    _TokenDeliveryText,
    WebhookNotifierPlugin,
)


class SensitiveEvent:
    def __init__(self, send_impl=None) -> None:
        self._send_impl = send_impl
        self.sent = []

    async def send(self, chain):
        self.sent.append(chain)
        if self._send_impl:
            return await self._send_impl(chain)
        return None


def assert_direct_chain(event: SensitiveEvent, expected: str) -> None:
    assert len(event.sent) == 1
    chain = event.sent[0]
    assert len(chain.chain) == 1
    assert isinstance(chain.chain[0], Plain)
    assert chain.chain[0].text == expected
    assert chain.get_use_t2i() is False
    assert chain.get_use_markdown() is False


def assert_filter_removed() -> None:
    loggers = [logging.getLogger()]
    loggers.extend(
        candidate
        for candidate in logging.Logger.manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    )
    for logger in loggers:
        assert not any(isinstance(item, _ExactSecretFilter) for item in logger.filters)
        for handler in logger.handlers:
            assert not any(
                isinstance(item, _ExactSecretFilter) for item in handler.filters
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "logged-success", "logged-error"])
async def test_sensitive_direct_send_redacts_all_relevant_logs_and_cleans_filter(
    caplog, mode
):
    secret = "whn_TEST_ONLY_EXACT_SECRET_123456789012345678901"
    delivery = _TokenDeliveryText(f"Bearer Token: {secret}", "recover-me")
    caplog.set_level(logging.DEBUG)

    async def send_impl(_chain):
        if mode != "success":
            for name in ("", "astrbot.respond.stage", "botpy.client", "aiocqhttp.api"):
                logging.getLogger(name).debug("adapter payload=%s", secret)
        if mode == "logged-error":
            try:
                raise RuntimeError(f"adapter exception contains {secret}")
            except RuntimeError:
                logging.getLogger("astrbot.adapter").exception("adapter failed")
            raise RuntimeError(f"send failed with {secret}")
        return None

    event = SensitiveEvent(send_impl)
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())

    sent = await plugin._send_sensitive_plain(event, delivery)

    assert sent is (mode != "logged-error")
    assert_direct_chain(event, f"Bearer Token: {secret}")
    assert secret not in caplog.text
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in (record.exc_text or "")
    assert_filter_removed()


class RespondStageLikeEvent:
    def __init__(self) -> None:
        self.message_str = "whn token new private respond-stage"
        self.session = SimpleNamespace(message_type="friend")
        self.unified_msg_origin = "aiocqhttp:FriendMessage:owner"
        self.sent = []

    def get_sender_id(self):
        return "owner"

    def get_platform_id(self):
        return "aiocqhttp"

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return StageResult(text)

    async def send(self, chain):
        logging.getLogger("aiocqhttp").debug("direct chain=%r", chain)
        self.sent.append(chain)


class StageResult:
    def __init__(self, text: str) -> None:
        self.text = text

    def use_t2i(self, _value: bool):
        return self


@pytest.mark.asyncio
async def test_respond_stage_like_prepare_to_send_contains_summary_only(
    tmp_path, caplog
):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    plugin._registry = EndpointRegistry(tmp_path)

    async def no_start():
        return None

    plugin._ensure_server_running = no_start  # type: ignore[method-assign]
    event = RespondStageLikeEvent()
    caplog.set_level(logging.DEBUG)
    yielded = []
    async for result in plugin.status_short(event):
        logging.getLogger("astrbot.respond.stage").info(
            "Prepare to send: Plain(%s)", result.text
        )
        yielded.append(result)

    assert len(yielded) == 1
    assert "Endpoint Path" in yielded[0].text
    assert "Bearer Token" not in yielded[0].text
    prepare_records = [
        record for record in caplog.records if "Prepare to send" in record.getMessage()
    ]
    assert len(prepare_records) == 1
    assert "Bearer Token" not in prepare_records[0].getMessage()
    assert len(event.sent) == 1
    secret = event.sent[0].chain[0].text.removeprefix("Bearer Token: ")
    assert secret not in caplog.text
