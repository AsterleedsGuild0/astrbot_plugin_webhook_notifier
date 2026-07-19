from __future__ import annotations

# ruff: noqa: E402

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_webhook_notifier.core import registry as registry_module
from astrbot_plugin_webhook_notifier.core.models import EndpointStatus
from astrbot_plugin_webhook_notifier.core.registry import (
    BIND_CURRENT_GROUP,
    PREBOUND_GROUP,
    EndpointRegistry,
    RegistryLoadError,
)
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin


class Phase2Event:
    def __init__(
        self,
        *,
        platform_id: str,
        platform_name: str,
        sender_id: str,
        private: bool,
        group_id: str = "group-1",
        group_info=None,
        group_error: Exception | None = None,
        raw_data=None,
        super_admin: bool = False,
    ) -> None:
        self.session = SimpleNamespace(message_type="friend" if private else "group")
        self.unified_msg_origin = (
            f"{platform_id}:FriendMessage:{sender_id}"
            if private
            else f"{platform_id}:GroupMessage:{group_id}"
        )
        self._platform_id = platform_id
        self._platform_name = platform_name
        self._sender_id = sender_id
        self._group_id = group_id
        self._group_info = group_info
        self._group_error = group_error
        self._super_admin = super_admin
        self.get_group_calls = 0
        self.message_obj = SimpleNamespace(
            raw_message=SimpleNamespace(raw_data=raw_data)
        )

    def get_platform_id(self):
        return self._platform_id

    def get_platform_name(self):
        return self._platform_name

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    async def get_group(self):
        self.get_group_calls += 1
        if self._group_error is not None:
            raise self._group_error
        return self._group_info

    def is_admin(self):
        return self._super_admin


@pytest.fixture
def plugin(tmp_path):
    instance = WebhookNotifierPlugin(Context(), AstrBotConfig())
    instance._registry = EndpointRegistry(tmp_path)

    async def no_start():
        return None

    instance._ensure_server_running = no_start  # type: ignore[method-assign]
    return instance


def test_same_adapter_kind_different_platform_ids_are_fully_isolated(tmp_path):
    registry = EndpointRegistry(tmp_path)
    first, _ = registry.create_private_endpoint(
        "onebot-a", "same-owner", "same-name", "onebot-a:FriendMessage:1"
    )
    second, _ = registry.create_private_endpoint(
        "onebot-b", "same-owner", "same-name", "onebot-b:FriendMessage:1"
    )

    assert first.path != second.path
    assert [
        item.path for item in registry.list_visible_by_owner("onebot-a", "same-owner")
    ] == [first.path]
    assert registry.rotate_token("onebot-a", "same-owner", "same-name")[0] is True
    assert registry.get_scoped("onebot-b", "same-owner", "same-name") == second
    assert registry.revoke_endpoint("onebot-a", "same-owner", "same-name")[0] is True
    assert registry.get_scoped("onebot-b", "same-owner", "same-name").status == "active"


def test_pending_binding_schema_rejects_invalid_mode_target_combinations(tmp_path):
    registry = EndpointRegistry(tmp_path)
    with pytest.raises(RegistryLoadError):
        registry.create_group_pending(
            "bot-a", "owner", "bad-current", BIND_CURRENT_GROUP, "prebound"
        )
    with pytest.raises(RegistryLoadError):
        registry.create_group_pending(
            "bot-a", "owner", "bad-prebound", PREBOUND_GROUP, None
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda info: info.__setitem__("phase", "unknown"),
        lambda info: info.__setitem__("verified_group_id", "premature"),
        lambda info: info.__setitem__("code_hash", ""),
    ],
)
def test_pending_phase_challenge_combinations_fail_closed_on_reload(tmp_path, mutate):
    registry = EndpointRegistry(tmp_path)
    registry.create_group_pending(
        "qq-bot-a", "owner", "group", BIND_CURRENT_GROUP, None
    )
    path = tmp_path / "webhook_tokens.json"
    raw = json.loads(path.read_text())
    mutate(next(iter(raw["pending"].values())))
    path.write_text(json.dumps(raw))

    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)


@pytest.mark.asyncio
async def test_cross_platform_verify_is_uniform_and_does_not_check_group_role(plugin):
    registry = plugin._registry
    assert registry is not None
    _, request_id, code = registry.create_group_pending(
        "bot-a", "owner", "group", PREBOUND_GROUP, "123"
    )
    event = Phase2Event(
        platform_id="bot-b",
        platform_name="aiocqhttp",
        sender_id="owner",
        private=False,
        group_id="123",
        group_info=SimpleNamespace(owner_id="owner"),
    )

    result = await plugin._handle_token_verify(event, [request_id, code])

    assert result == "❌ 验证请求不存在或已过期"
    assert event.get_group_calls == 0
    assert registry.get_pending_descriptor("bot-a", request_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_info", "sender", "super_admin", "expected"),
    [
        (SimpleNamespace(owner_id="owner", admin_ids=[]), "owner", False, "验证成功"),
        (
            SimpleNamespace(owner_id="other", admin_ids=["owner"]),
            "owner",
            False,
            "验证成功",
        ),
        (SimpleNamespace(owner_id="other", admin_ids=[]), "owner", False, "权限不足"),
        (SimpleNamespace(owner_id="other", admin_ids=[]), "owner", True, "权限不足"),
        (None, "owner", False, "无法校验"),
    ],
)
async def test_aiocqhttp_awaits_group_and_never_uses_super_admin_shortcut(
    plugin, group_info, sender, super_admin, expected
):
    registry = plugin._registry
    assert registry is not None
    name = f"group-{len(registry.list_all_for_admin())}"
    _, request_id, code = registry.create_group_pending(
        "onebot-main", "owner", name, PREBOUND_GROUP, "123456"
    )
    event = Phase2Event(
        platform_id="onebot-main",
        platform_name="aiocqhttp",
        sender_id=sender,
        private=False,
        group_id="123456",
        group_info=group_info,
        super_admin=super_admin,
    )

    result = await plugin._handle_token_verify(event, [request_id, code])

    assert expected in result
    assert event.get_group_calls == 1


@pytest.mark.asyncio
async def test_qq_official_private_create_uses_bind_current_without_group_openid(
    plugin,
):
    event = Phase2Event(
        platform_id="qq-bot-a",
        platform_name="qq_official",
        sender_id="stable-user",
        private=True,
    )

    result = await plugin._create_group_pending(event, ["current", "official-group"])

    assert "验证时绑定当前群" in result
    assert "group_openid" not in result
    assert "Bearer Token" not in result
    assert "http://" not in result and "https://" not in result
    registry = plugin._registry
    assert registry is not None
    record = registry.get_scoped("qq-bot-a", "stable-user", "official-group")
    assert record is not None
    descriptor = registry.get_pending_descriptor(
        "qq-bot-a", record.pending_request_id or ""
    )
    assert descriptor is not None
    assert descriptor["group_binding_mode"] == BIND_CURRENT_GROUP
    assert descriptor["target_group_id"] is None


@pytest.mark.asyncio
async def test_qq_official_rejects_numeric_group_and_webhook_adapter_is_unsupported(
    plugin,
):
    official = Phase2Event(
        platform_id="qq-bot-a",
        platform_name="qq_official",
        sender_id="stable-user",
        private=True,
    )
    webhook = Phase2Event(
        platform_id="qq-webhook-a",
        platform_name="qq_official_webhook",
        sender_id="stable-user",
        private=True,
    )

    numeric = await plugin._create_group_pending(official, ["123456", "bad"])
    unsupported = await plugin._create_group_pending(webhook, ["current", "bad"])

    assert "group current" in numeric
    assert "不支持" in unsupported


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_qq_official_raw_owner_and_admin_bind_current_group(plugin, role):
    registry = plugin._registry
    assert registry is not None
    name = f"official-{role}"
    _, request_id, code = registry.create_group_pending(
        "qq-bot-a", "stable-user", name, BIND_CURRENT_GROUP, None
    )
    event = Phase2Event(
        platform_id="qq-bot-a",
        platform_name="qq_official",
        sender_id="member-openid-must-not-be-used",
        private=False,
        group_id="event-group-must-not-be-used",
        raw_data={
            "group_openid": "raw-group",
            "author": {
                "member_openid": "different-member",
                "member_role": role,
            },
        },
    )

    result = await plugin._handle_token_verify(event, [request_id, code])

    assert "群管理员验证成功" in result
    assert f"token confirm {request_id}" in result
    assert "Bearer Token" not in result
    record = registry.get_scoped("qq-bot-a", "stable-user", name)
    assert record is not None
    assert record.status == EndpointStatus.PENDING_VERIFICATION.value
    assert record.targets == []
    assert record.token_hash == ""
    descriptor = registry.get_pending_descriptor("qq-bot-a", request_id)
    assert descriptor is not None
    assert descriptor["phase"] == "group_verified_waiting_owner"
    assert descriptor["verified_group_id"] == "raw-group"
    assert descriptor["code_hash"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_data",
    [
        {
            "group_openid": "group",
            "author": {"member_openid": "member", "member_role": "member"},
        },
        {"group_openid": "group", "author": {"member_openid": "member"}},
        {
            "group_openid": "group",
            "author": {"member_openid": "member", "member_role": "unknown"},
        },
        {
            "group_openid": "group",
            "author": {"member_openid": "member", "member_role": 1},
        },
        {
            "group_openid": "group",
            "author": {"member_role": "owner"},
        },
        {"author": {"member_openid": "member", "member_role": "owner"}},
    ],
)
async def test_qq_official_raw_validation_fails_closed(plugin, raw_data):
    registry = plugin._registry
    assert registry is not None
    name = f"closed-{len(registry.list_all_for_admin())}"
    _, request_id, code = registry.create_group_pending(
        "qq-bot-a", "stable-user", name, BIND_CURRENT_GROUP, None
    )
    event = Phase2Event(
        platform_id="qq-bot-a",
        platform_name="qq_official",
        sender_id="stable-user",
        private=False,
        raw_data=raw_data,
    )

    result = await plugin._handle_token_verify(event, [request_id, code])

    if raw_data.get("author", {}).get("member_role") == "member":
        assert "权限不足" in result
    else:
        assert "无法校验" in result
    assert registry.get_pending_descriptor("qq-bot-a", request_id) is not None


@pytest.mark.asyncio
async def test_qq_official_member_openid_is_not_compared_with_private_owner(
    plugin,
):
    registry = plugin._registry
    assert registry is not None
    _, request_id, code = registry.create_group_pending(
        "qq-bot-a", "stable-user", "identity", BIND_CURRENT_GROUP, None
    )
    event = Phase2Event(
        platform_id="qq-bot-a",
        platform_name="qq_official",
        sender_id="stable-user",
        private=False,
        raw_data={
            "group_openid": "group",
            "author": {
                "member_openid": "unrelated-group-identity",
                "member_role": "owner",
            },
        },
    )

    result = await plugin._handle_token_verify(event, [request_id, code])

    assert "群管理员验证成功" in result
    descriptor = registry.get_pending_descriptor("qq-bot-a", request_id)
    assert descriptor is not None
    assert "member_openid" not in descriptor
    assert "user_openid" not in descriptor


def test_bind_current_confirm_activates_with_token_only_in_owner_platform(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, request_id, code = registry.create_group_pending(
        "qq-bot-a", "stable-user", "group", BIND_CURRENT_GROUP, None
    )
    first = registry.verify_group_endpoint(
        "qq-bot-a", request_id, code, "stable-user", "group-openid", "owner"
    )
    second = registry.verify_group_endpoint(
        "qq-bot-a", request_id, code, "stable-user", "group-openid", "owner"
    )

    assert first[0] == "waiting_owner"
    descriptor_after_first = registry.get_pending_descriptor("qq-bot-a", request_id)
    assert second[0] == "waiting_owner"
    assert (
        registry.get_pending_descriptor("qq-bot-a", request_id)
        == descriptor_after_first
    )
    pending = registry.get_scoped("qq-bot-a", "stable-user", "group")
    assert pending is not None
    assert pending.status == EndpointStatus.PENDING_VERIFICATION.value
    before = registry.confirm_group_endpoint("other-bot", request_id, "stable-user")
    assert before[:2] == ("error", "验证请求不存在或已过期")
    status, _, active, token = registry.confirm_group_endpoint(
        "qq-bot-a", request_id, "stable-user"
    )
    assert status == "ok" and active is not None and token is not None
    assert active.targets[0].umo == "qq-bot-a:GroupMessage:group-openid"
    assert active.token_hash
    assert registry.authenticate_delivery(record.path, f"Bearer {token}").authorized
    assert registry.confirm_group_endpoint("qq-bot-a", request_id, "stable-user")[
        :2
    ] == ("error", "验证请求不存在或已过期")


def test_pending_persistence_contract_contains_explicit_binding_mode(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_group_pending(
        "qq-bot-a", "stable-user", "group", BIND_CURRENT_GROUP, None
    )
    saved = json.loads((tmp_path / "webhook_tokens.json").read_text())
    descriptor = next(iter(saved["pending"].values()))
    assert descriptor["group_binding_mode"] == "bind_current_group"
    assert descriptor["target_group_id"] is None
    assert descriptor["phase"] == "awaiting_group_admin"
    assert descriptor["verified_group_id"] is None


def test_confirm_before_verify_and_wrong_owner_do_not_change_pending(tmp_path):
    registry = EndpointRegistry(tmp_path)
    _, request_id, code = registry.create_group_pending(
        "qq-bot-a", "private-owner", "group", BIND_CURRENT_GROUP, None
    )
    original = registry.get_pending_descriptor("qq-bot-a", request_id)

    before = registry.confirm_group_endpoint("qq-bot-a", request_id, "private-owner")
    assert before[0] == "error" and "尚未完成" in before[1]
    assert registry.get_pending_descriptor("qq-bot-a", request_id) == original

    registry.verify_group_endpoint(
        "qq-bot-a", request_id, code, None, "group-openid", "admin"
    )
    wrong = registry.confirm_group_endpoint("qq-bot-a", request_id, "other-owner")
    assert wrong[0] == "error" and "不是原申请者" in wrong[1]
    assert registry.get_pending_descriptor("qq-bot-a", request_id) is not None


@pytest.mark.asyncio
async def test_cross_platform_confirm_is_uniform_and_does_not_consume(plugin):
    registry = plugin._registry
    assert registry is not None
    _, request_id, code = registry.create_group_pending(
        "qq-bot-a", "private-owner", "group", BIND_CURRENT_GROUP, None
    )
    registry.verify_group_endpoint(
        "qq-bot-a", request_id, code, None, "group-openid", "owner"
    )
    other_bot = Phase2Event(
        platform_id="qq-bot-b",
        platform_name="qq_official",
        sender_id="private-owner",
        private=True,
    )

    result = await plugin._handle_token_confirm(other_bot, [request_id])

    assert result == "❌ 验证请求不存在或已过期"
    assert registry.get_pending_descriptor("qq-bot-a", request_id) is not None


def test_waiting_owner_expiry_expires_record_and_deletes_challenge(tmp_path):
    registry = EndpointRegistry(tmp_path)
    _, request_id, code = registry.create_group_pending(
        "qq-bot-a", "private-owner", "group", BIND_CURRENT_GROUP, None
    )
    registry.verify_group_endpoint(
        "qq-bot-a", request_id, code, None, "group-openid", "owner"
    )
    key = next(iter(registry._pending))
    expired = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    registry._pending[key]["expires_at"] = expired
    registry._records[next(iter(registry._records))].pending_expires_at = expired

    result = registry.confirm_group_endpoint("qq-bot-a", request_id, "private-owner")

    assert result[0] == "error" and "已过期" in result[1]
    record = registry.get_scoped("qq-bot-a", "private-owner", "group")
    assert record is not None and record.status == EndpointStatus.EXPIRED.value
    assert record.pending_request_id is None
    assert registry.get_pending_descriptor("qq-bot-a", request_id) is None


def test_concurrent_confirm_generates_and_delivers_exactly_one_token(
    tmp_path, monkeypatch
):
    registry = EndpointRegistry(tmp_path)
    _, request_id, code = registry.create_group_pending(
        "qq-bot-a", "private-owner", "group", BIND_CURRENT_GROUP, None
    )
    registry.verify_group_endpoint(
        "qq-bot-a", request_id, code, None, "group-openid", "owner"
    )
    generated_tokens: list[str] = []
    original_generate_token = registry_module.generate_token

    def spy_generate_token() -> str:
        token = original_generate_token()
        generated_tokens.append(token)
        return token

    token_spy = Mock(side_effect=spy_generate_token)
    monkeypatch.setattr(registry_module, "generate_token", token_spy)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: registry.confirm_group_endpoint(
                    "qq-bot-a", request_id, "private-owner"
                ),
                range(2),
            )
        )

    assert [result[0] for result in results].count("ok") == 1
    assert [result[0] for result in results].count("error") == 1
    assert token_spy.call_count == 1
    assert len(generated_tokens) == 1
    successful = next(result for result in results if result[0] == "ok")
    failed = next(result for result in results if result[0] == "error")
    assert successful[3] == generated_tokens[0]
    assert failed[3] is None
    assert generated_tokens[0] not in repr(failed)


class ActionFailed(Exception):
    pass


class NetworkError(Exception):
    pass


ActionFailed.__module__ = "aiocqhttp.exceptions"
NetworkError.__module__ = "astrbot.core.platform.sources.aiocqhttp"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ActionFailed(), NetworkError(), TimeoutError()])
async def test_aiocqhttp_controlled_adapter_errors_leave_pending_unchanged(
    plugin, error, caplog
):
    registry = plugin._registry
    assert registry is not None
    _, request_id, code = registry.create_group_pending(
        "onebot-main", "owner", "error", PREBOUND_GROUP, "123"
    )
    original = registry.get_pending_descriptor("onebot-main", request_id)
    event = Phase2Event(
        platform_id="onebot-main",
        platform_name="aiocqhttp",
        sender_id="owner",
        private=False,
        group_id="123",
        group_error=error,
    )

    result = await plugin._handle_token_verify(event, [request_id, code])

    assert "无法校验" in result
    assert registry.get_pending_descriptor("onebot-main", request_id) == original
    assert type(error).__name__ in caplog.text
    assert code not in caplog.text
    assert "owner" not in caplog.text
    assert "123" not in caplog.text
