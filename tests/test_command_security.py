"""聊天命令凭据与 URL 零泄漏契约。"""

# ruff: noqa: E402

from __future__ import annotations

import json
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
from astrbot_plugin_webhook_notifier.core.models import ServerConfig
from astrbot_plugin_webhook_notifier.core.registry import EndpointRegistry
from astrbot_plugin_webhook_notifier.core.server import WebhookServer
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin

CONFIGURED_DOMAIN = "configured.example"


class CommandEvent:
    def __init__(
        self,
        message: str,
        *,
        private: bool,
        sender_id: str = "10001",
        group_id: str = "20002",
        admin: bool = True,
    ) -> None:
        self.message_str = message
        self.session = SimpleNamespace(message_type="friend" if private else "group")
        self.unified_msg_origin = (
            f"aiocqhttp:FriendMessage:{sender_id}"
            if private
            else f"aiocqhttp:GroupMessage:{group_id}"
        )
        self._sender_id = sender_id
        self._group_id = group_id
        self._admin = admin
        self.sent_chains = []
        self.actions: list[str] = []

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_platform_id(self):
        return "aiocqhttp"

    def get_platform_name(self):
        return "aiocqhttp"

    async def get_group(self):
        return SimpleNamespace(owner_id=self._sender_id)

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        self.actions.append("result")
        return PlainResult(text)

    async def send(self, chain):
        self.actions.append("send")
        self.sent_chains.append(chain)


class PlainResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.t2i = True

    def use_t2i(self, value: bool):
        self.t2i = value
        return self


class QQOfficialCommandEvent(CommandEvent):
    def __init__(
        self,
        message: str,
        *,
        private: bool,
        sender_id: str = "c2c-owner",
        raw_data=None,
    ) -> None:
        super().__init__(message, private=private, sender_id=sender_id)
        self.message_obj = SimpleNamespace(
            raw_message=SimpleNamespace(raw_data=raw_data)
        )

    def get_platform_id(self):
        return "qq-bot-a"

    def get_platform_name(self):
        return "qq_official"


def assert_url_free(text: str) -> None:
    assert re.search(r"https?://", text, flags=re.I) is None
    assert CONFIGURED_DOMAIN not in text
    assert "OMP_SESSION_WEBHOOK_URL=" not in text


async def collect(plugin: WebhookNotifierPlugin, event: CommandEvent):
    return [item async for item in plugin.status_short(event)]


def sent_credential(event: CommandEvent) -> str:
    assert len(event.sent_chains) == 1
    chain = event.sent_chains[0]
    assert len(chain.chain) == 1
    assert isinstance(chain.chain[0], Plain)
    assert chain.get_use_t2i() is False
    assert chain.get_use_markdown() is False
    return chain.chain[0].text


@pytest.fixture
def plugin(tmp_path: Path):
    context = Context()
    instance = WebhookNotifierPlugin(
        context,
        AstrBotConfig(
            server={
                "host": "127.0.0.1",
                "port": 18080,
                "base_path": "/webhook",
                "public_base_url": f"https://{CONFIGURED_DOMAIN}/webhook",
            }
        ),
    )
    instance._registry = EndpointRegistry(tmp_path)
    instance._server_config = instance._server_config or None

    async def no_start():
        return None

    instance._ensure_server_running = no_start  # type: ignore[method-assign]
    return instance, context, tmp_path


@pytest.mark.asyncio
async def test_private_create_yields_summary_then_direct_sends_credential(plugin):
    instance, _, data_dir = plugin
    event = CommandEvent("whn token new private work", private=True)

    messages = await collect(instance, event)

    assert len(messages) == 1
    assert messages[0].t2i is False
    summary = messages[0].text
    credential = sent_credential(event)
    assert event.actions == ["result", "send"]
    assert "名称: work" in summary
    assert "Endpoint Path: u/" in summary
    assert "Plugin Page" in summary
    assert "Bearer Token:" not in summary
    assert re.fullmatch(r"Bearer Token: whn_[A-Za-z0-9_-]+", credential)
    token = credential.removeprefix("Bearer Token: ")
    assert token not in summary
    assert_url_free(summary)
    assert_url_free(credential)
    persisted = (data_dir / "webhook_tokens.json").read_text(encoding="utf-8")
    assert token not in persisted


@pytest.mark.asyncio
async def test_group_pending_and_verify_do_not_deliver_credentials(plugin):
    instance, context, data_dir = plugin
    private_event = CommandEvent("whn token new group 20002 group-work", private=True)
    pending_messages = await collect(instance, private_event)
    assert len(pending_messages) == 1
    pending_text = pending_messages[0].text
    assert "Endpoint Path: u/" in pending_text
    assert "Bearer Token:" not in pending_text
    assert_url_free(pending_text)

    request_id = re.search(r"请求 ID: ([^\n]+)", pending_text).group(1)  # type: ignore[union-attr]
    code = re.search(r"验证码: ([^\n]+)", pending_text).group(1)  # type: ignore[union-attr]
    persisted_pending = (data_dir / "webhook_tokens.json").read_text(encoding="utf-8")
    assert code not in persisted_pending
    assert '"code"' not in persisted_pending

    group_event = CommandEvent(f"whn token verify {request_id} {code}", private=False)
    verified = await collect(instance, group_event)

    assert len(verified) == 1
    verify_text = verified[0].text
    assert "验证成功" in verify_text
    assert "主动私聊" in verify_text
    assert "token rotate group-work" in verify_text
    assert "Bearer Token:" not in verify_text
    assert_url_free(verify_text)
    assert context._sent_messages == []
    record = instance._registry.get_by_owner_name("aiocqhttp", "10001", "group-work")
    assert record is not None
    assert record.token_hash == ""
    saved = json.loads((data_dir / "webhook_tokens.json").read_text(encoding="utf-8"))
    assert saved["pending"] == {}
    assert code not in json.dumps(saved)


@pytest.mark.asyncio
async def test_rotate_yields_summary_then_direct_sends_credential(plugin):
    instance, _, _ = plugin
    event = CommandEvent("whn token new private rotate-me", private=True)
    created = await collect(instance, event)
    assert len(created) == 1
    old_token = sent_credential(event).removeprefix("Bearer Token: ")
    event.sent_chains.clear()
    event.actions.clear()

    event.message_str = "whn token rotate rotate-me"
    rotated = await collect(instance, event)

    assert len(rotated) == 1
    assert rotated[0].t2i is False
    summary = rotated[0].text
    credential = sent_credential(event)
    assert event.actions == ["result", "send"]
    assert "名称: rotate-me" in summary
    assert "Endpoint Path: u/" in summary
    assert "旧 Token 已立即失效" in summary
    assert "Bearer Token:" not in summary
    assert re.fullmatch(r"Bearer Token: whn_[A-Za-z0-9_-]+", credential)
    assert old_token not in summary + credential
    assert_url_free(summary)
    assert_url_free(credential)


@pytest.mark.asyncio
async def test_status_is_split_and_never_exposes_configured_base(plugin):
    instance, _, _ = plugin
    instance._server_config = None
    event = CommandEvent("whn status", private=True)

    messages = await collect(instance, event)

    assert len(messages) == 1
    text = messages[0].text
    assert "监听 IP：" in text
    assert "监听端口：" in text
    assert "基础路径：" in text
    assert "Base URL：请在 Plugin Page 中复制" in text
    assert "监听地址：" not in text
    assert_url_free(text)


@pytest.mark.asyncio
async def test_status_preserves_listener_fields_when_public_netloc_matches_host(plugin):
    instance, _, _ = plugin
    instance._server_config = ServerConfig(
        host="127.0.0.1",
        port=18080,
        base_path="/webhook",
        public_base_url="https://127.0.0.1:18080/webhook",
    )
    event = CommandEvent("whn status", private=True)

    messages = await collect(instance, event)

    assert len(messages) == 1
    text = messages[0].text
    assert "监听 IP：127.0.0.1" in text
    assert "监听端口：18080" in text
    assert "基础路径：/webhook" in text
    assert "https://" not in text

    untrusted = instance._sanitize_chat_text(
        "异常文本 https://127.0.0.1:18080/webhook 127.0.0.1:18080"
    )
    assert "https://" not in untrusted
    assert "127.0.0.1" not in untrusted


class WebhookRequest:
    def __init__(self, path: str, credential: str) -> None:
        self.content_type = "application/json"
        self.content_length = None
        self.path = f"/webhook/{path}"
        self.headers = {
            "Authorization": f"Bearer {credential}",
            "X-OMP-Event": "session_stop",
        }

    async def read(self) -> bytes:
        return json.dumps(
            {
                "event": "omp.session_stop",
                "session": {"id": "session-1"},
                "round": {"turnId": "turn-1"},
            }
        ).encode()


class SenderStub:
    def preflight_private_notification_policy(self, endpoint, target_alias):
        return None

    async def send_text(self, rendered, endpoint, target_alias):
        return [{"ok": True, "name": "default"}]


@pytest.mark.asyncio
async def test_group_verify_then_private_rotate_enables_server_auth(plugin):
    instance, _, data_dir = plugin
    pending_event = CommandEvent("whn token new group 20002 group-auth", private=True)
    pending = await collect(instance, pending_event)
    pending_text = pending[0].text
    request_id = re.search(r"请求 ID: ([^\n]+)", pending_text).group(1)  # type: ignore[union-attr]
    code = re.search(r"验证码: ([^\n]+)", pending_text).group(1)  # type: ignore[union-attr]

    verify_event = CommandEvent(f"whn token verify {request_id} {code}", private=False)
    verified = await collect(instance, verify_event)
    assert "主动私聊" in verified[0].text

    registry = instance._registry
    assert registry is not None
    record = registry.get_by_owner_name("aiocqhttp", "10001", "group-auth")
    assert record is not None
    assert record.token_hash == ""
    assert (
        registry.is_endpoint_active(
            record.owner_platform_id, record.owner_user_id, record.name
        )
        is False
    )

    server = WebhookServer(
        config=ServerConfig(),
        registry=registry,
        sender=SenderStub(),  # type: ignore[arg-type]
    )
    pending_response = await server._process_request(
        WebhookRequest(record.path, code), "pending-credential"
    )
    pending_body = json.loads(pending_response.body)
    assert pending_response.status == 403
    assert pending_body["data"]["error"] == "token_unclaimed"
    assert pending_body["message"] == "Token 尚未领取，请先在私聊中执行 token rotate"

    rotate_event = CommandEvent("whn token rotate group-auth", private=True)
    rotated = await collect(instance, rotate_event)
    assert len(rotated) == 1
    new_token = sent_credential(rotate_event).removeprefix("Bearer Token: ")
    assert new_token.startswith("whn_")
    assert (
        registry.is_endpoint_active(
            record.owner_platform_id, record.owner_user_id, record.name
        )
        is True
    )

    accepted = await server._process_request(
        WebhookRequest(record.path, new_token), "rotated-credential"
    )
    accepted_body = json.loads(accepted.body)
    assert accepted.status == 200
    assert accepted_body["code"] == 0
    assert accepted_body["data"]["delivered"] is True

    stale_pending = await server._process_request(
        WebhookRequest(record.path, code), "stale-pending-credential"
    )
    stale_body = json.loads(stale_pending.body)
    assert stale_pending.status == 401
    assert stale_body["data"]["error"] == "invalid_token"

    persisted = (data_dir / "webhook_tokens.json").read_text(encoding="utf-8")
    assert code not in persisted
    assert new_token not in persisted
    assert request_id not in persisted


@pytest.mark.asyncio
async def test_qq_official_verify_then_confirm_direct_sends_safe_plain(plugin):
    instance, _, data_dir = plugin
    private = QQOfficialCommandEvent(
        "whn token new group current official-auth", private=True
    )
    created = await collect(instance, private)
    pending_text = created[0].text
    request_id = re.search(r"请求 ID: ([^\n]+)", pending_text).group(1)  # type: ignore[union-attr]
    code = re.search(r"验证码: ([^\n]+)", pending_text).group(1)  # type: ignore[union-attr]
    group = QQOfficialCommandEvent(
        f"whn token verify {request_id} {code}",
        private=False,
        sender_id="unrelated-member-id",
        raw_data={
            "group_openid": "production-group-openid",
            "author": {"member_openid": "member-id", "member_role": "admin"},
        },
    )

    verified = await collect(instance, group)
    assert "token confirm" in verified[0].text
    assert "Bearer Token" not in verified[0].text

    private.message_str = f"whn token confirm {request_id}"
    private.actions.clear()
    confirmed = await collect(instance, private)

    assert len(confirmed) == 1
    assert confirmed[0].t2i is False
    summary = confirmed[0].text
    credential = sent_credential(private)
    assert private.actions == ["result", "send"]
    assert "名称: official-auth" in summary
    assert "Endpoint Path: u/" in summary
    assert "Plugin Page" in summary
    assert "Bearer Token" not in summary
    assert re.fullmatch(r"Bearer Token: whn_[A-Za-z0-9_-]+", credential)
    token = credential.removeprefix("Bearer Token: ")
    persisted = (data_dir / "webhook_tokens.json").read_text(encoding="utf-8")
    assert token not in persisted
    assert request_id not in persisted
    record = instance._registry.get_scoped("qq-bot-a", "c2c-owner", "official-auth")
    assert record is not None
    assert record.targets[0].umo == "qq-bot-a:GroupMessage:production-group-openid"


@pytest.mark.asyncio
async def test_confirm_delivery_failure_does_not_rollback_and_rotate_recovers(plugin):
    instance, _, _ = plugin
    private = QQOfficialCommandEvent(
        "whn token new group current delivery-failure", private=True
    )
    created = await collect(instance, private)
    text = created[0].text
    request_id = re.search(r"请求 ID: ([^\n]+)", text).group(1)  # type: ignore[union-attr]
    code = re.search(r"验证码: ([^\n]+)", text).group(1)  # type: ignore[union-attr]
    group = QQOfficialCommandEvent(
        f"whn token verify {request_id} {code}",
        private=False,
        raw_data={
            "group_openid": "group",
            "author": {"member_openid": "member", "member_role": "owner"},
        },
    )
    await collect(instance, group)

    async def fail_send(_chain):
        raise RuntimeError("simulated delivery failure")

    private.send = fail_send  # type: ignore[method-assign]
    private.message_str = f"whn token confirm {request_id}"
    messages = await collect(instance, private)

    registry = instance._registry
    record = registry.get_scoped("qq-bot-a", "c2c-owner", "delivery-failure")
    assert record is not None and record.status == "active" and record.token_hash
    assert (
        registry.confirm_group_endpoint("qq-bot-a", request_id, "c2c-owner")[0]
        == "error"
    )
    assert len(messages) == 2
    assert "凭据发送未确认" in messages[1].text

    recovered = QQOfficialCommandEvent(
        "whn token rotate delivery-failure", private=True
    )
    rotated = await collect(instance, recovered)
    assert len(rotated) == 1
    assert sent_credential(recovered).startswith("Bearer Token: whn_")


@pytest.mark.asyncio
async def test_create_and_rotate_send_failures_keep_committed_security_state(plugin):
    instance, _, data_dir = plugin
    create_event = CommandEvent("whn token new private send-failure", private=True)
    attempted: list[str] = []

    async def fail_create(chain):
        attempted.append(chain.chain[0].text)
        raise RuntimeError("adapter failed")

    create_event.send = fail_create  # type: ignore[method-assign]
    created = await collect(instance, create_event)
    assert len(created) == 2
    assert "凭据发送未确认" in created[1].text
    first_token = attempted[-1].removeprefix("Bearer Token: ")
    registry = instance._registry
    record = registry.get_scoped("aiocqhttp", "10001", "send-failure")
    assert record is not None and record.status == "active" and record.token_hash
    persisted = (data_dir / "webhook_tokens.json").read_text(encoding="utf-8")
    assert first_token not in persisted

    recovery = CommandEvent("whn token rotate send-failure", private=True)
    await collect(instance, recovery)
    old_token = sent_credential(recovery).removeprefix("Bearer Token: ")
    assert registry.authenticate_delivery(record.path, f"Bearer {old_token}").authorized

    rotate_failure = CommandEvent("whn token rotate send-failure", private=True)
    attempted.clear()

    async def fail_rotate(chain):
        attempted.append(chain.chain[0].text)
        raise RuntimeError("adapter failed")

    rotate_failure.send = fail_rotate  # type: ignore[method-assign]
    failed = await collect(instance, rotate_failure)
    assert len(failed) == 2
    assert "凭据发送未确认" in failed[1].text
    failed_delivery_token = attempted[-1].removeprefix("Bearer Token: ")
    assert not registry.authenticate_delivery(
        record.path, f"Bearer {old_token}"
    ).authorized
    assert registry.authenticate_delivery(
        record.path, f"Bearer {failed_delivery_token}"
    ).authorized

    final_recovery = CommandEvent("whn token rotate send-failure", private=True)
    recovered = await collect(instance, final_recovery)
    assert len(recovered) == 1
    final_token = sent_credential(final_recovery).removeprefix("Bearer Token: ")
    assert registry.authenticate_delivery(
        record.path, f"Bearer {final_token}"
    ).authorized
    assert not registry.authenticate_delivery(
        record.path, f"Bearer {failed_delivery_token}"
    ).authorized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "whn token new private missing-registry",
        "whn token new group bad-group missing-registry",
        "whn token verify missing code",
        "whn token rotate missing",
        "whn token revoke missing",
        "whn unknown",
    ],
)
async def test_error_and_degraded_paths_are_url_free(message):
    instance = WebhookNotifierPlugin(
        Context(),
        AstrBotConfig(
            server={"public_base_url": f"https://{CONFIGURED_DOMAIN}/webhook"}
        ),
    )
    event = CommandEvent(message, private=True)

    messages = await collect(instance, event)

    assert messages
    for result in messages:
        assert result.t2i is False
        assert_url_free(result.text)


def test_chat_sanitizer_removes_urls_configured_value_and_url_env_line():
    instance = WebhookNotifierPlugin(
        Context(),
        AstrBotConfig(
            server={"public_base_url": f"https://{CONFIGURED_DOMAIN}/webhook"}
        ),
    )
    raw = (
        f"bad https://other.example/path {CONFIGURED_DOMAIN}\n"
        "OMP_SESSION_WEBHOOK_URL=https://other.example/path\n"
        "safe"
    )

    sanitized = instance._sanitize_chat_text(raw)

    assert_url_free(sanitized)
    assert "safe" in sanitized


@pytest.mark.asyncio
async def test_untrusted_token_is_hidden_but_dedicated_delivery_is_preserved(plugin):
    instance, _, _ = plugin
    event = CommandEvent("whn token new private token-delivery", private=True)
    messages = await collect(instance, event)
    assert len(messages) == 1
    credential = sent_credential(event)
    assert re.fullmatch(r"Bearer Token: whn_[A-Za-z0-9_-]{43}", credential)
    assert "[Token 已隐藏]" not in credential
    delivered_token = credential.removeprefix("Bearer Token: ")

    async def leaked_dispatch(_event, _args, _commands):
        return f"异常信息意外包含 {delivered_token}，请检查日志"

    instance._dispatch_whn_command = leaked_dispatch  # type: ignore[method-assign]
    event.message_str = "whn compatibility-output"
    sanitized_messages = await collect(instance, event)
    assert delivered_token not in sanitized_messages[0].text
    assert "[Token 已隐藏]" in sanitized_messages[0].text
