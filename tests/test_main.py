"""Template Web API registration tests."""

from __future__ import annotations

import sys
import json
from pathlib import Path

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context
from astrbot.api.web import request
from astrbot_plugin_webhook_notifier.core.template_registry import (
    BUILT_IN_ID,
    TemplateRegistry,
)
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin


def test_private_notifications_schema_defaults_to_false():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    setting = schema["enable_private_notifications"]
    assert setting["type"] == "bool"
    assert setting["default"] is False


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
        def is_owner_name_available(self, owner_id, endpoint_name):
            return True

        def is_path_available(self, endpoint_path):
            return True

        def create_private_endpoint(self, **kwargs):
            return object(), "secret-token"

    class EventStub:
        unified_msg_origin = "aiocqhttp:FriendMessage:10001"

        def get_sender_id(self):
            return "10001"

    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    plugin._registry = RegistryStub()  # type: ignore[assignment]

    async def no_start():
        return None

    plugin._ensure_server_running = no_start  # type: ignore[method-assign]
    message = await plugin._create_private_endpoint(EventStub(), "private")  # type: ignore[arg-type]

    assert "endpoint 和 Token 仍有效" in message
    assert "Webhook 通知会返回 skipped" in message
    assert "enable_private_notifications" in message


def test_registers_six_template_apis_in_constructor():
    context = Context()
    WebhookNotifierPlugin(context, AstrBotConfig())
    routes = {item[0] for item in context.web_apis}
    assert routes == {
        "/astrbot_plugin_webhook_notifier/templates",
        "/astrbot_plugin_webhook_notifier/templates/<template_id>",
        "/astrbot_plugin_webhook_notifier/templates/save",
        "/astrbot_plugin_webhook_notifier/templates/apply",
        "/astrbot_plugin_webhook_notifier/templates/delete",
        "/astrbot_plugin_webhook_notifier/templates/preview",
    }


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
