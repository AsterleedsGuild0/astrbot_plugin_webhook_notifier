"""Endpoint 卡片主题：创建、切换、拒绝路径、旧记录兼容与渲染传递。"""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api.star import Context

from astrbot_plugin_webhook_notifier.core.models import ServerConfig
from astrbot_plugin_webhook_notifier.core.registry import (
    REGISTRY_FILENAME,
    EndpointRegistry,
    RegistryLoadError,
)
from astrbot_plugin_webhook_notifier.core.renderer import (
    render_html_data,
    render_html_template,
    render_subagent_timeline,
)
from astrbot_plugin_webhook_notifier.core.server import WebhookServer
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin
from astrbot_plugin_webhook_notifier.tests.test_server import (
    FakeRegistry,
    FakeSender,
    _make_complex_event,
    _make_endpoint,
)

PLATFORM = "aiocqhttp"


class _PrivateEvent:
    session = SimpleNamespace(message_type="friend")

    def __init__(self, sender: str = "owner") -> None:
        self.sender = sender
        self.unified_msg_origin = f"{PLATFORM}:FriendMessage:{sender}"

    def get_sender_id(self) -> str:
        return self.sender

    def get_platform_id(self) -> str:
        return PLATFORM

    def get_platform_name(self) -> str:
        return PLATFORM


@pytest.fixture
def plugin(tmp_path):
    instance = WebhookNotifierPlugin(Context(), {})  # type: ignore[arg-type]
    instance._registry = EndpointRegistry(tmp_path)

    async def no_start():
        return None

    instance._ensure_server_running = no_start  # type: ignore[method-assign]
    return instance


def _registry_bytes(tmp_path: Path) -> bytes | None:
    path = tmp_path / REGISTRY_FILENAME
    return path.read_bytes() if path.exists() else None


def _theme(tmp_path: Path, name: str, owner: str = "owner") -> str:
    record = EndpointRegistry(tmp_path).get_scoped(PLATFORM, owner, name)
    assert record is not None
    return record.theme


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], "light"), (["--theme", "light"], "light"), (["--theme", "dark"], "dark")],
)
async def test_omp_creation_persists_requested_theme(plugin, tmp_path, flags, expected):
    result = await plugin._handle_token_new(
        _PrivateEvent(), ["private", "ep", "--provider", "omp", *flags]
    )

    assert isinstance(result, list)
    assert _theme(tmp_path, "ep") == expected
    assert expected in result[0]


@pytest.mark.asyncio
async def test_group_pending_keeps_theme_until_activation(plugin, tmp_path):
    await plugin._handle_token_new(
        _PrivateEvent(), ["group", "123456", "grp", "--theme", "dark", "--provider", "omp"]
    )

    record = EndpointRegistry(tmp_path).get_scoped(PLATFORM, "owner", "grp")
    assert record is not None
    assert (record.status, record.theme) == ("pending_verification", "dark")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flags",
    [
        ["--theme", "dark"],
        ["--theme", "light"],
        ["--provider", "opencode", "--theme", "dark"],
        ["--provider", "markdown", "--theme", "light"],
        ["--provider", "omp", "--theme", "auto"],
        ["--provider", "omp", "--theme", "Dark"],
        ["--provider", "omp", "--theme=dark"],
        ["--provider", "omp", "--theme"],
        ["--provider", "omp", "--theme", "dark", "--theme", "dark"],
    ],
)
async def test_invalid_theme_creation_is_rejected_without_writing(plugin, tmp_path, flags):
    result = await plugin._handle_token_new(_PrivateEvent(), ["private", "ep", *flags])

    assert isinstance(result, str) and result.startswith("❌")
    assert _registry_bytes(tmp_path) is None


@pytest.mark.asyncio
async def test_owner_switches_theme_and_it_survives_reload(plugin, tmp_path):
    await plugin._handle_token_new(_PrivateEvent(), ["private", "ep", "--provider", "omp"])

    dark = plugin._handle_token_theme(_PrivateEvent(), ["ep", "dark"])
    assert dark.startswith("✅") and _theme(tmp_path, "ep") == "dark"
    light = plugin._handle_token_theme(_PrivateEvent(), ["ep", "light"])
    assert light.startswith("✅") and _theme(tmp_path, "ep") == "light"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "sender", "args"),
    [
        ("omp", "intruder", ["ep", "dark"]),
        ("opencode", "owner", ["ep", "dark"]),
        ("markdown", "owner", ["ep", "light"]),
        ("omp", "owner", ["ep", "auto"]),
        ("omp", "owner", ["ep", "DARK"]),
        ("omp", "owner", ["missing", "dark"]),
        ("omp", "owner", ["dark"]),
    ],
)
async def test_theme_switch_rejections_leave_registry_untouched(
    plugin, tmp_path, provider, sender, args
):
    await plugin._handle_token_new(_PrivateEvent(), ["private", "ep", "--provider", provider])
    before = _registry_bytes(tmp_path)

    result = plugin._handle_token_theme(_PrivateEvent(sender), args)

    assert result.startswith("❌")
    assert _registry_bytes(tmp_path) == before
    assert _theme(tmp_path, "ep") == "light"


@pytest.mark.asyncio
async def test_theme_switch_requires_private_chat_and_live_endpoint(plugin, tmp_path):
    await plugin._handle_token_new(_PrivateEvent(), ["private", "ep", "--provider", "omp"])
    group_event = _PrivateEvent()
    group_event.session = SimpleNamespace(message_type="group")
    assert plugin._handle_token_theme(group_event, ["ep", "dark"]).startswith("❌")

    plugin._registry.revoke_endpoint(PLATFORM, "owner", "ep")
    before = _registry_bytes(tmp_path)
    assert plugin._handle_token_theme(_PrivateEvent(), ["ep", "dark"]).startswith("❌")
    assert _registry_bytes(tmp_path) == before


@pytest.mark.asyncio
async def test_list_and_status_show_theme(plugin, tmp_path):
    await plugin._handle_token_new(
        _PrivateEvent(), ["private", "dark-ep", "--provider", "omp", "--theme", "dark"]
    )
    await plugin._handle_token_new(_PrivateEvent(), ["private", "md-ep"])

    listing = plugin._handle_token_list(_PrivateEvent())
    blocks = {
        block.split("\n")[0].split()[-1]: block
        for block in listing.split("\n\n")
        if "Provider" in block
    }
    assert "dark（深色）" in blocks["dark-ep"]
    assert "主题" not in blocks["md-ep"]
    status = plugin._build_status_text()
    assert "深色 1 个" in status and "浅色 0 个" in status


def test_record_without_theme_loads_as_light(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "legacy", f"{PLATFORM}:FriendMessage:owner", provider="omp"
    )
    path = tmp_path / REGISTRY_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    for record in raw["records"].values():
        del record["theme"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert _theme(tmp_path, "legacy") == "light"


@pytest.mark.parametrize(("provider", "theme"), [("omp", "auto"), ("markdown", "dark")])
def test_registry_refuses_to_load_invalid_theme(tmp_path, provider, theme):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "bad", f"{PLATFORM}:FriendMessage:owner", provider=provider
    )
    path = tmp_path / REGISTRY_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    for record in raw["records"].values():
        record["theme"] = theme
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)


def test_custom_template_reads_theme_and_unaware_template_is_unchanged():
    event = _make_complex_event()
    aware = "<html><body><p>{{ event.title }}|{{ event.theme }}</p></body></html>"
    unaware = "<html><body><p>{{ event.title }}</p></body></html>"

    def render(template: str, theme: str) -> str:
        return render_html_template(template, render_html_data(event, theme=theme)["event"])

    assert "|dark" in render(aware, "dark")
    assert "|light" in render(aware, "light")
    assert render(unaware, "dark") == render(unaware, "light")
    with pytest.raises(ValueError):
        render_html_data(event, theme="auto")


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_endpoint_theme_reaches_main_card_and_timeline_attachment(theme):
    htmls: list[str] = []

    async def capture_render(tmpl, data, return_url=True, options=None):
        htmls.append(data["rendered_html"])
        return b"\x89PNG\r\n\x1a\nimage"

    endpoint = _make_endpoint()
    endpoint.theme = theme
    server = WebhookServer(
        ServerConfig(),
        FakeRegistry(),
        FakeSender(),
        capture_render,
        {"render_mode": "html_image", "fallback_to_text": True},
    )
    response = await server._handle_html_image(
        _make_complex_event(), endpoint, None, "req-theme", True
    )

    assert response.status == 200
    assert len(htmls) == 2
    assert render_subagent_timeline(_make_complex_event(), theme=theme) is not None
    assert all(f'data-theme="{theme}"' in html for html in htmls)
