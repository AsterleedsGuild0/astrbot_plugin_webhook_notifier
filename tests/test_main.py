"""Template Web API registration tests."""

# ruff: noqa: E402

from __future__ import annotations

import sys
import json
import logging
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.star import Context
from astrbot.api.web import request
from astrbot_plugin_webhook_notifier.core.template_registry import (
    BUILT_IN_ID,
    TemplateRegistry,
)
from astrbot_plugin_webhook_notifier.core.registry import (
    EndpointRegistry,
    RegistryLoadError,
)
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin


class AdminEventStub:
    def __init__(
        self,
        *,
        super_admin: bool,
        sender_id: str = "admin-001",
        private: bool = True,
    ) -> None:
        self._super_admin = super_admin
        self._sender_id = sender_id
        self.unified_msg_origin = "aiocqhttp:FriendMessage:admin-001"
        self.session = SimpleNamespace(message_type="friend" if private else "group")

    def is_admin(self):
        return self._super_admin

    def get_sender_id(self):
        return self._sender_id


class PlainResultStub:
    def __init__(self, text: str) -> None:
        self.text = text
        self.t2i = True

    def use_t2i(self, value: bool):
        self.t2i = value
        return self


class CommandEventStub:
    def __init__(self, message_str: str, *, super_admin: bool = False) -> None:
        self.message_str = message_str
        self.unified_msg_origin = "aiocqhttp:FriendMessage:test-user"
        self._super_admin = super_admin

    def plain_result(self, text: str) -> PlainResultStub:
        return PlainResultStub(text)

    def chain_result(self, components) -> MessageChain:
        return MessageChain(list(components))

    def is_admin(self) -> bool:
        return self._super_admin


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("injected_args", "message_str", "expected"),
    [
        (("help",), "whn ignored", "help"),
        (("admin", "token", "list"), "whn ignored", "admin token list"),
        (("admin token list",), "whn ignored", "admin token list"),
        (
            (),
            "whn token new group 123456 test-group",
            "token new group 123456 test-group",
        ),
    ],
)
async def test_status_short_uses_injected_args_then_falls_back_to_message(
    injected_args, message_str, expected
):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = CommandEventStub(message_str)
    dispatched: list[str] = []

    async def record_dispatch(_event, args, _commands):
        dispatched.append(args)
        return f"handled:{args}"

    plugin._dispatch_whn_command = record_dispatch  # type: ignore[method-assign]

    yielded = [item async for item in plugin.status_short(event, *injected_args)]

    assert dispatched == [expected]
    assert len(yielded) == 1
    assert yielded[0].text == f"handled:{expected}"
    assert yielded[0].t2i is False


@pytest.mark.asyncio
async def test_status_short_root_command_without_injected_args_shows_status():
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = CommandEventStub("whn")

    yielded = [item async for item in plugin.status_short(event)]

    assert len(yielded) == 1
    assert "Webhook Notifier" in yielded[0].text
    assert yielded[0].t2i is False


def test_status_short_signature_hides_variadic_compatibility_parameter():
    parameters = list(inspect.signature(WebhookNotifierPlugin.status_short).parameters)
    assert parameters == ["self", "event"]


@pytest.fixture
def admin_plugin(tmp_path):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    registry = EndpointRegistry(tmp_path)
    active, token = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        name="admin-active",
        owner_user_id="owner-a",
        target_umo="aiocqhttp:FriendMessage:secret-umo-id",
    )
    pending, _, pending_code = registry.create_pending_verification(
        owner_platform_id="aiocqhttp",
        name="admin-pending",
        owner_user_id="owner-b",
        target_group_id="123456789",
    )
    plugin._registry = registry
    return plugin, active, pending, token, pending_code


@pytest.mark.asyncio
async def test_super_admin_can_list_and_revoke_path(admin_plugin):
    plugin, active, _, _, _ = admin_plugin
    event = AdminEventStub(super_admin=True)

    listing = await plugin._dispatch_whn_command(event, "admin token list")
    revoked = await plugin._dispatch_whn_command(
        event, f"admin token revoke-path /{active.path}"
    )

    assert "owner-a" in listing
    assert "owner-b" in listing
    assert "admin-active" in listing
    assert "endpoint 已撤销" in revoked
    assert (
        plugin._registry.get_scoped("aiocqhttp", "owner-a", "admin-active").status
        == "revoked"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "admin token list",
        "admin token revoke-path u/owner-a/admin-active",
        "admin token revoke-owner aiocqhttp owner-a admin-active",
    ],
)
async def test_super_admin_admin_commands_are_private_only(command):
    class RegistryMustNotBeQueried:
        def __getattr__(self, name):
            raise AssertionError(f"Registry 不应被访问: {name}")

    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    plugin._registry = RegistryMustNotBeQueried()  # type: ignore[assignment]
    event = AdminEventStub(super_admin=True, private=False)

    result = await plugin._dispatch_whn_command(event, command)

    assert "请在私聊中执行" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
async def test_non_super_admin_is_rejected_before_registry_query(private):
    class RegistryMustNotBeQueried:
        def __getattr__(self, name):
            raise AssertionError(f"Registry 不应被访问: {name}")

    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    plugin._registry = RegistryMustNotBeQueried()  # type: ignore[assignment]
    event = AdminEventStub(
        super_admin=False,
        sender_id="normal-or-group-admin",
        private=private,
    )

    result = await plugin._dispatch_whn_command(event, "admin token list")

    assert "仅 AstrBot 全局超级管理员" in result


@pytest.mark.asyncio
async def test_admin_revoke_owner_normalizes_name_and_is_owner_scoped(tmp_path):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    registry = EndpointRegistry(tmp_path)
    selected, _ = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        name="Fancy-Name",
        owner_user_id="selected-owner",
        target_umo="aiocqhttp:FriendMessage:10001",
    )
    other, _ = registry.create_private_endpoint(
        owner_platform_id="aiocqhttp",
        name="Fancy-Name",
        owner_user_id="other-owner",
        target_umo="aiocqhttp:FriendMessage:10002",
    )
    plugin._registry = registry
    event = AdminEventStub(super_admin=True)

    result = await plugin._dispatch_whn_command(
        event, "admin token revoke-owner aiocqhttp selected-owner Fancy Name!!"
    )

    assert "endpoint 已撤销" in result
    assert (
        registry.get_scoped("aiocqhttp", "selected-owner", "Fancy-Name").status
        == "revoked"
    )
    assert (
        registry.get_scoped("aiocqhttp", "other-owner", "Fancy-Name").status == "active"
    )


@pytest.mark.asyncio
async def test_admin_revoke_owner_not_found_and_idempotent(admin_plugin):
    plugin, active, _, _, _ = admin_plugin
    event = AdminEventStub(super_admin=True)

    missing_owner = await plugin._dispatch_whn_command(
        event, "admin token revoke-owner aiocqhttp missing-owner admin-active"
    )
    missing_name = await plugin._dispatch_whn_command(
        event, "admin token revoke-owner aiocqhttp owner-a missing-name"
    )
    first = await plugin._dispatch_whn_command(
        event, "admin token revoke-owner aiocqhttp owner-a admin-active"
    )
    repeated = await plugin._dispatch_whn_command(
        event, "admin token revoke-owner aiocqhttp owner-a admin-active"
    )

    assert "不存在" in missing_owner
    assert "不存在" in missing_name
    assert "已撤销" in first
    assert "无需重复" in repeated
    assert (
        plugin._registry.get_scoped("aiocqhttp", "owner-a", "admin-active").status
        == "revoked"
    )


@pytest.mark.parametrize("empty_name", ["", "   "])
def test_admin_revoke_owner_rejects_empty_name_before_registry_query(empty_name):
    class RegistryMustNotBeQueried:
        def __getattr__(self, name):
            raise AssertionError(f"Registry 不应被访问: {name}")

    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    plugin._registry = RegistryMustNotBeQueried()  # type: ignore[assignment]

    result = plugin._handle_admin_token_revoke_owner(
        "admin-001", ["aiocqhttp", "some-owner", empty_name]
    )

    assert "revoke-owner <platform_id> <owner_user_id> <名称>" in result


@pytest.mark.asyncio
async def test_admin_command_usage_unknown_action_and_wrong_path(admin_plugin):
    plugin, _, _, _, _ = admin_plugin
    event = AdminEventStub(super_admin=True)

    no_args = await plugin._dispatch_whn_command(event, "admin")
    unknown = await plugin._dispatch_whn_command(event, "admin token delete")
    empty_path = await plugin._dispatch_whn_command(event, "admin token revoke-path")
    wrong_path = await plugin._dispatch_whn_command(
        event, "admin token revoke-path u/owner-a/admin"
    )

    assert "/whn admin token list" in no_args
    assert "未知 admin token 子命令" in unknown
    assert "revoke-path <endpoint-path>" in empty_path
    assert "revoke-owner <platform_id> <owner_user_id> <名称>" in no_args
    assert "path 不存在" in wrong_path


# ─── Admin config min-duration 命令测试 ──────────────────────────────────


@pytest.mark.asyncio
async def test_admin_config_min_duration_query():
    """查询当前默认值。"""
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration")
    assert "最短完成通知时长" in result
    assert "当前：15 秒" in result
    assert "低于 15 秒时跳过通知" in result
    assert "默认：15 秒" in result


@pytest.mark.asyncio
async def test_admin_config_min_duration_query_shows_filter_disabled():
    """查询 0 时明确说明仅关闭耗时过滤。"""
    plugin = WebhookNotifierPlugin(
        Context(), AstrBotConfig({"min_completion_duration_seconds": 0})
    )
    event = AdminEventStub(super_admin=True)

    result = await plugin._dispatch_whn_command(event, "admin config min-duration")

    assert "当前：0 秒（过滤已关闭）" in result
    assert "不按耗时跳过成功完成通知" in result


@pytest.mark.asyncio
async def test_admin_config_min_duration_query_shows_invalid_config_fail_open():
    """查询非法历史值时明确说明已按 0 安全关闭过滤。"""
    plugin = WebhookNotifierPlugin(
        Context(), AstrBotConfig({"min_completion_duration_seconds": "broken"})
    )
    event = AdminEventStub(super_admin=True)

    result = await plugin._dispatch_whn_command(event, "admin config min-duration")

    assert "当前：0 秒（检测到无效历史配置，已安全关闭过滤）" in result
    assert "不按耗时跳过成功完成通知" in result
    assert "broken" not in result


@pytest.mark.asyncio
async def test_admin_config_min_duration_set():
    """设置有效值并持久化。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 15})
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration 30")
    assert "已将最短完成通知时长设为 30 秒" in result
    assert "低于 30 秒时将跳过通知" in result
    assert config.get("min_completion_duration_seconds") == 30
    assert config.save_call_count >= 1


@pytest.mark.asyncio
async def test_admin_config_min_duration_set_zero():
    """设置 0 关闭过滤。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 15})
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration 0")
    assert "已关闭短任务通知过滤（0 秒）" in result
    assert "不再按耗时跳过" in result


@pytest.mark.asyncio
async def test_admin_config_min_duration_reset():
    """reset 恢复默认 15。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 100})
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(
        event, "admin config min-duration reset"
    )
    assert "已恢复默认值：15 秒" in result
    assert "低于 15 秒时将跳过通知" in result
    assert config.get("min_completion_duration_seconds") == 15


@pytest.mark.asyncio
async def test_admin_config_min_duration_invalid_input_not_saved():
    """非法输入不保存，旧值保留。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 15})
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration abc")
    assert "非法输入" in result
    assert "0–3600" in result
    assert "abc" not in result
    assert config.get("min_completion_duration_seconds") == 15


@pytest.mark.asyncio
async def test_admin_config_min_duration_out_of_range_not_saved():
    """超出有效范围不保存。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 15})
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(
        event, "admin config min-duration 99999"
    )
    assert "非法范围" in result
    assert "0–3600" in result
    assert config.get("min_completion_duration_seconds") == 15


@pytest.mark.asyncio
async def test_admin_config_min_duration_non_admin_rejected():
    """非超级管理员拒绝。"""
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = AdminEventStub(super_admin=False)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration 30")
    assert "仅 AstrBot 全局超级管理员" in result


@pytest.mark.asyncio
async def test_admin_config_min_duration_group_chat_rejected():
    """群聊中执行拒绝。"""
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = AdminEventStub(super_admin=True, private=False)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration 30")
    assert result == "❌ 私聊限制：管理员命令请在私聊中执行。"


@pytest.mark.asyncio
async def test_admin_config_min_duration_unknown_subcommand():
    """未知子命令显示用法。"""
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config unknown-sub")
    assert "用法" in result


@pytest.mark.asyncio
async def test_admin_config_min_duration_rejects_extra_arguments():
    """多余参数不会被静默忽略。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 15})
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)

    result = await plugin._dispatch_whn_command(
        event, "admin config min-duration 30 unexpected"
    )

    assert "参数过多" in result
    assert "<0..3600|reset>" in result
    assert config.get("min_completion_duration_seconds") == 15
    assert config.save_call_count == 0


@pytest.mark.asyncio
async def test_admin_config_save_failure_rollback():
    """保存失败时回滚内存值。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 15})
    config.set_fail_save(True)
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration 30")
    assert "配置保存失败" in result
    assert "新值未应用" in result
    assert "当前仍为 15 秒" in result
    assert "simulated save failure" not in result
    assert config.get("min_completion_duration_seconds") == 15


@pytest.mark.asyncio
async def test_admin_config_save_success_updates_server():
    """保存成功后 Server 运行时值更新。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 15})
    plugin = WebhookNotifierPlugin(Context(), config)
    server_duration = None

    class ServerStub:
        def set_min_completion_duration_seconds(self, value: int) -> None:
            nonlocal server_duration
            server_duration = value

    plugin._server = ServerStub()  # type: ignore[assignment]
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration 45")
    assert "45 秒" in result
    assert server_duration == 45


@pytest.mark.asyncio
async def test_admin_config_save_failure_rollback_without_old_key():
    """保存失败，旧 key 不存在时完全移除。"""
    config = AstrBotConfig()  # 没有预置 key
    config.set_fail_save(True)
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin config min-duration 30")
    assert "配置保存失败" in result
    assert "min_completion_duration_seconds" not in config


@pytest.mark.asyncio
async def test_admin_config_save_failure_preserves_present_none_value(caplog):
    """保存失败时精确保留值为 None 的历史 key，且日志不泄露异常正文。"""
    config = AstrBotConfig({"min_completion_duration_seconds": None})
    config.set_fail_save(True)
    plugin = WebhookNotifierPlugin(Context(), config)
    event = AdminEventStub(super_admin=True)

    result = await plugin._dispatch_whn_command(event, "admin config min-duration 30")

    assert "配置保存失败" in result
    assert "min_completion_duration_seconds" in config
    assert config["min_completion_duration_seconds"] is None
    assert "simulated save failure" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_admin_config_reconstruct_retains_value():
    """重新构造插件后配置值仍保留（模拟持久化生效）。"""
    config = AstrBotConfig({"min_completion_duration_seconds": 60})
    # 证明对象被保存
    config.save_config()
    plugin = WebhookNotifierPlugin(Context(), config)
    assert plugin.config.get("min_completion_duration_seconds") == 60


@pytest.mark.asyncio
async def test_admin_config_min_duration_help_includes_new_commands():
    """admin 命令用法文本中包含新的 config 命令。"""
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = AdminEventStub(super_admin=True)
    usage = await plugin._dispatch_whn_command(event, "admin")
    assert "admin config min-duration" in usage


# ─── 最短完成通知时长 Status/Schema 测试 ────────────────────────────────


def test_min_duration_schema_defaults_to_15():
    """_conf_schema.json 中 min_completion_duration_seconds 默认 15。"""
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    setting = schema["min_completion_duration_seconds"]
    assert setting["type"] == "int"
    assert setting["default"] == 15
    assert setting["min"] == 0
    assert setting["max"] == 3600
    assert "低于阈值时跳过" in setting["hint"]
    assert "0 可关闭此过滤" in setting["hint"]


def test_min_duration_status_missing_defaults_to_15():
    """配置缺失时状态显示 15 秒。"""
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    text = plugin._build_status_text()
    assert "最短完成通知时长：15 秒" in text
    assert "低于此时长的成功完成通知会被跳过" in text


def test_min_duration_status_shows_0_as_closed():
    """配置为 0 时状态显示已关闭。"""
    plugin = WebhookNotifierPlugin(
        Context(), AstrBotConfig({"min_completion_duration_seconds": 0})
    )
    text = plugin._build_status_text()
    assert "0 秒（过滤已关闭" in text
    assert "不按耗时跳过通知" in text


def test_min_duration_status_shows_invalid_as_safe():
    """无效历史配置在状态中显示安全关闭。"""
    plugin = WebhookNotifierPlugin(
        Context(), AstrBotConfig({"min_completion_duration_seconds": -1})
    )
    text = plugin._build_status_text()
    assert "0 秒（检测到无效配置，已安全关闭过滤）" in text


def test_min_duration_status_negative_value_normalized_to_0():
    """负数归一化为 0。"""
    plugin = WebhookNotifierPlugin(
        Context(), AstrBotConfig({"min_completion_duration_seconds": -5})
    )
    assert "0 秒（检测到无效配置，已安全关闭过滤）" in (plugin._build_status_text())


@pytest.mark.asyncio
async def test_admin_config_min_duration_unknown_action():
    """未知 admin action（非 token/config）显示未知子命令。"""
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    event = AdminEventStub(super_admin=True)
    result = await plugin._dispatch_whn_command(event, "admin delete")
    assert "未知 admin 子命令" in result


@pytest.mark.asyncio
async def test_admin_list_does_not_expose_secrets_or_full_umo(admin_plugin):
    plugin, active, pending, token, pending_code = admin_plugin
    event = AdminEventStub(super_admin=True)

    listing = await plugin._dispatch_whn_command(event, "admin token list")

    assert active.token_hash not in listing
    assert "token_hash" not in listing
    assert token not in listing
    assert pending_code not in listing
    assert pending.pending_code is None
    assert "aiocqhttp:FriendMessage:secret-umo-id" not in listing


@pytest.mark.asyncio
async def test_admin_outputs_and_logs_do_not_leak_secrets_or_raw_targets(
    admin_plugin, caplog
):
    plugin, active, pending, token, pending_code = admin_plugin
    event = AdminEventStub(super_admin=True, sender_id="audit-admin")
    target_owner = active.owner_user_id
    target_path = active.path
    target_umo = active.targets[0].umo

    caplog.set_level(logging.INFO, logger="astrbot")
    caplog.clear()
    listing = await plugin._dispatch_whn_command(event, "admin token list")
    path_result = await plugin._dispatch_whn_command(
        event, f"admin token revoke-path {target_path}"
    )
    owner_result = await plugin._dispatch_whn_command(
        event, "admin token revoke-owner aiocqhttp owner-b admin-pending"
    )
    output = "\n".join((listing, path_result, owner_result))

    assert token not in output
    assert active.token_hash not in output
    assert pending_code not in output
    assert target_umo not in output
    assert target_owner not in caplog.text
    assert target_path not in caplog.text
    assert pending.owner_user_id not in caplog.text
    assert "audit-admin" in caplog.text


def test_private_notifications_schema_defaults_to_false():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    setting = schema["enable_private_notifications"]
    assert setting["type"] == "bool"
    assert setting["default"] is False


def test_notification_mode_schema_and_status_defaults_to_focused():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    setting = schema["notification_mode"]
    assert setting["type"] == "string"
    assert setting["options"] == ["focused", "all"]
    assert setting["default"] == "focused"

    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    assert "通知降噪模式：focused" in plugin._build_status_text()


def test_display_timezone_schema_and_runtime_defaults_to_asia_shanghai():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    setting = schema["display_timezone"]
    assert setting["type"] == "string"
    assert setting["default"] == "Asia/Shanghai"

    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    assert plugin._display_context.timezone_name == "Asia/Shanghai"
    assert "展示时区：Asia/Shanghai" in plugin._build_status_text()


def test_valid_display_timezone_is_accepted():
    plugin = WebhookNotifierPlugin(
        Context(), AstrBotConfig(display_timezone="Asia/Tokyo")
    )
    assert plugin._display_context.timezone_name == "Asia/Tokyo"


def test_invalid_display_timezone_warning_is_fixed_and_redacted(caplog):
    secret_config = "Private/Customer-Timezone"
    caplog.set_level(logging.WARNING, logger="astrbot")
    plugin = WebhookNotifierPlugin(
        Context(), AstrBotConfig(display_timezone=secret_config)
    )
    assert plugin._display_context.timezone_name == "Asia/Shanghai"
    assert "display_timezone 无效，已回退到 Asia/Shanghai" in caplog.text
    assert secret_config not in caplog.text


def test_invalid_notification_mode_is_fail_open_in_status():
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig(notification_mode="bad"))
    assert "通知降噪模式：all" in plugin._build_status_text()


@pytest.mark.asyncio
async def test_missing_private_notification_config_initializes_sender_disabled(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "astrbot_plugin_webhook_notifier.main.StarTools.get_data_dir",
        lambda plugin_name: tmp_path,
    )
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())

    await plugin.initialize()

    assert plugin._sender is not None
    assert plugin._sender._enable_private_notifications is False


@pytest.mark.asyncio
async def test_initialize_registry_load_error_never_starts_server(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "astrbot_plugin_webhook_notifier.main.StarTools.get_data_dir",
        lambda plugin_name: tmp_path,
    )
    monkeypatch.setattr(
        "astrbot_plugin_webhook_notifier.main.EndpointRegistry",
        lambda *_: (_ for _ in ()).throw(RegistryLoadError("invalid registry")),
    )
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    started = False

    async def mark_started():
        nonlocal started
        started = True

    plugin._start_server = mark_started  # type: ignore[method-assign]
    with pytest.raises(RegistryLoadError):
        await plugin.initialize()
    assert started is False
    assert plugin._server is None


def test_status_shows_private_notification_state():
    disabled = WebhookNotifierPlugin(Context(), AstrBotConfig())
    enabled = WebhookNotifierPlugin(
        Context(), AstrBotConfig(enable_private_notifications=True)
    )

    assert "Webhook 私聊状态通知：关闭" in disabled._build_status_text()
    assert "Webhook 私聊状态通知：开启" in enabled._build_status_text()


@pytest.mark.asyncio
async def test_private_endpoint_creation_warns_when_notifications_disabled():
    class RegistryStub:
        def create_private_endpoint(self, **kwargs):
            return SimpleNamespace(
                path="u/generated/private", provider="omp"
            ), "secret-token"

    class EventStub:
        unified_msg_origin = "aiocqhttp:FriendMessage:10001"

        def get_sender_id(self):
            return "10001"

        def get_platform_id(self):
            return "aiocqhttp"

    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    plugin._registry = RegistryStub()  # type: ignore[assignment]

    async def no_start():
        return None

    plugin._ensure_server_running = no_start  # type: ignore[method-assign]
    message = await plugin._create_private_endpoint(EventStub(), "private")  # type: ignore[arg-type]

    assert isinstance(message, list)
    assert "endpoint 和 Token 仍有效" in message[0]
    assert "Webhook 通知会返回 skipped" in message[0]
    assert "enable_private_notifications" in message[0]


def test_registers_plugin_page_apis_in_constructor():
    context = Context()
    WebhookNotifierPlugin(context, AstrBotConfig())
    routes = {item[0] for item in context.web_apis}
    assert routes == {
        "/astrbot_plugin_webhook_notifier/base-url",
        "/astrbot_plugin_webhook_notifier/templates",
        "/astrbot_plugin_webhook_notifier/templates/<template_id>",
        "/astrbot_plugin_webhook_notifier/templates/save",
        "/astrbot_plugin_webhook_notifier/templates/apply",
        "/astrbot_plugin_webhook_notifier/templates/delete",
        "/astrbot_plugin_webhook_notifier/templates/preview",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "expected", "configured"),
    [
        (
            {
                "host": "0.0.0.0",
                "port": 19090,
                "base_path": "/hooks/",
                "public_base_url": "",
            },
            "http://0.0.0.0:19090/hooks",
            False,
        ),
        (
            {
                "host": "127.0.0.1",
                "port": 18080,
                "base_path": "/webhook",
                "public_base_url": "https://configured.example/webhook/",
            },
            "https://configured.example/webhook",
            True,
        ),
    ],
)
async def test_base_url_api_returns_only_minimum_fields(server, expected, configured):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig(server=server))

    body, status = await plugin._api_base_url()

    assert status == 200
    assert body == {"base_url": expected, "configured": configured}
    assert not {
        "token",
        "registry",
        "endpoints",
        "owner",
        "umo",
        "server_secret",
    } & set(body)


@pytest.mark.asyncio
async def test_template_api_response_shapes_and_save_conflict(tmp_path):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    _, status = await plugin._api_templates()
    assert status == 503

    plugin._template_registry = TemplateRegistry(tmp_path)
    listing, status = await plugin._api_templates()
    assert status == 200
    assert listing["active"] == BUILT_IN_ID
    assert listing["requested_active"] == BUILT_IN_ID
    assert listing["effective_active"] == BUILT_IN_ID
    assert listing["templates"][0]["built_in"] is True
    assert listing["templates"][0]["active"] is True
    assert listing["templates"][0]["valid"] is True

    detail, status = await plugin._api_template_detail(BUILT_IN_ID)
    assert status == 200
    assert detail["built_in"] is True
    assert detail["active"] is True
    assert detail["valid"] is True

    request.json = {
        "id": None,
        "display_name": "Custom",
        "content": "<html><body>{{ event.title }}</body></html>",
        "canvas_width": 812,
        "expected_revision": None,
        "apply": True,
    }
    saved, status = await plugin._api_template_save()
    assert status == 200
    assert set(saved) == {"template", "active", "effective_active"}
    assert saved["active"] == saved["template"]["id"]
    assert saved["effective_active"] == saved["template"]["id"]
    assert saved["template"]["built_in"] is False
    assert saved["template"]["active"] is True
    assert saved["template"]["valid"] is True

    request.json = {"id": BUILT_IN_ID, "expected_revision": 0}
    applied, status = await plugin._api_template_apply()
    assert status == 200
    assert applied == {
        "active": BUILT_IN_ID,
        "effective_active": BUILT_IN_ID,
    }

    request.json = {
        "id": saved["template"]["id"],
        "expected_revision": saved["template"]["revision"],
    }
    deleted, status = await plugin._api_template_delete()
    assert status == 200
    assert deleted == {
        "deleted": True,
        "active": BUILT_IN_ID,
        "effective_active": BUILT_IN_ID,
    }

    request.json = {
        "id": None,
        "display_name": "Conflict",
        "content": "<html><body>{{ event.title }}</body></html>",
        "canvas_width": 812,
        "expected_revision": 0,
        "apply": False,
    }
    _, status = await plugin._api_template_save()
    assert status == 409
