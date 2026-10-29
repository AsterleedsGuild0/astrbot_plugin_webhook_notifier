from __future__ import annotations

# ruff: noqa: E402

import logging
import re
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
    _TokenDeliveryText,
    WebhookNotifierPlugin,
)

from .test_command_security import (
    CommandEvent,
    QQOfficialCommandEvent,
    collect,
    sent_credential,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True], ids=["success", "send-error"])
async def test_sensitive_direct_send_logs_no_credential_or_exception_details(
    caplog, fails
):
    secret = "whn_TEST_ONLY_EXACT_SECRET_123456789012345678901"
    exception_text = f"adapter exception contains {secret}"
    delivery = _TokenDeliveryText(f"Bearer Token: {secret}", "recover-me")
    caplog.set_level(logging.DEBUG, logger="astrbot")

    async def send_impl(_chain):
        if fails:
            raise RuntimeError(exception_text)

    event = SensitiveEvent(send_impl)
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())

    sent = await plugin._send_sensitive_plain(event, delivery)

    assert sent is not fails
    assert_direct_chain(event, f"Bearer Token: {secret}")
    assert secret not in caplog.text
    assert exception_text not in caplog.text
    records = [record for record in caplog.records if record.name == "astrbot"]
    assert len(records) == int(fails)
    if fails:
        record = records[0]
        assert record.levelno == logging.ERROR
        assert record.getMessage() == (
            "[WebhookNotifier] 敏感凭据 direct send 失败 error=RuntimeError"
        )
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None


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
    assert "--provider omp" in yielded[0].text
    assert "--provider opencode" in yielded[0].text
    prepare_records = [
        record for record in caplog.records if "Prepare to send" in record.getMessage()
    ]
    assert len(prepare_records) == 1
    assert "Bearer Token" not in prepare_records[0].getMessage()
    assert len(event.sent) == 1
    secret = event.sent[0].chain[0].text.removeprefix("Bearer Token: ")
    assert secret not in caplog.text
    assert_direct_chain(event, f"Bearer Token: {secret}")
    saved = EndpointRegistry(tmp_path)
    record = saved.get_scoped("aiocqhttp", "owner", "respond-stage")
    assert record is not None and record.provider == "markdown"
    assert saved.authenticate_delivery(record.path, f"Bearer {secret}").authorized


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "rotate", "qq_official-confirm"])
async def test_token_commands_direct_send_each_credential_once(
    tmp_path, caplog, operation
):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    plugin._registry = EndpointRegistry(tmp_path)

    async def no_start():
        return None

    plugin._ensure_server_running = no_start  # type: ignore[method-assign]
    caplog.set_level(logging.DEBUG, logger="astrbot")
    previous_token = None
    if operation == "qq_official-confirm":
        event = QQOfficialCommandEvent(
            "whn token new group current once", private=True
        )
        pending = await collect(plugin, event)
        request_match = re.search(r"请求 ID: ([^\n]+)", pending[0].text)
        code_match = re.search(r"验证码: ([^\n]+)", pending[0].text)
        assert request_match is not None and code_match is not None
        request_id = request_match.group(1)
        group = QQOfficialCommandEvent(
            f"whn token verify {request_id} {code_match.group(1)}",
            private=False,
            sender_id="group-admin",
            raw_data={
                "group_openid": "verified-group",
                "author": {"member_openid": "group-admin", "member_role": "admin"},
            },
        )
        verified = await collect(plugin, group)
        assert "token confirm" in verified[0].text
        assert not event.sent_chains
        assert not group.sent_chains
        event.message_str = f"whn token confirm {request_id}"
    else:
        event = CommandEvent("whn token new private once", private=True)
        if operation == "rotate":
            created = await collect(plugin, event)
            previous_token = sent_credential(event).removeprefix("Bearer Token: ")
            assert all(previous_token not in result.text for result in created)
            event = CommandEvent("whn token rotate once", private=True)

    results = await collect(plugin, event)

    credential = sent_credential(event)
    assert re.fullmatch(r"Bearer Token: whn_[A-Za-z0-9_-]+", credential)
    token = credential.removeprefix("Bearer Token: ")
    assert credential.count(token) == 1
    assert len(results) == 1
    assert all(token not in result.text for result in results)
    assert token not in caplog.text
    if previous_token is not None:
        assert token != previous_token
        assert previous_token not in caplog.text
    record = plugin._registry.get_scoped(
        event.get_platform_id(), event.get_sender_id(), "once"
    )
    assert record is not None
    assert plugin._registry.authenticate_delivery(
        record.path, f"Bearer {token}"
    ).authorized
    if previous_token is not None:
        assert not plugin._registry.authenticate_delivery(
            record.path, f"Bearer {previous_token}"
        ).authorized
