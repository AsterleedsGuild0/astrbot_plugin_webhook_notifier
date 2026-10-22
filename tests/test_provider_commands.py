"""Provider selection through real command creation and persisted records."""

# ruff: noqa: E402

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api.star import Context

from astrbot_plugin_webhook_notifier.core.omp import OmpProviderAdapter
from astrbot_plugin_webhook_notifier.core.opencode import OpenCodeProviderAdapter
from astrbot_plugin_webhook_notifier.core.providers import ProviderRegistry
from astrbot_plugin_webhook_notifier.core.registry import EndpointRegistry
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin


class _PrivateEventStub:
    session = SimpleNamespace(message_type="friend")

    def __init__(self, platform: str = "aiocqhttp") -> None:
        self.platform = platform
        self.unified_msg_origin = f"{platform}:FriendMessage:user"

    def get_sender_id(self) -> str:
        return "user"

    def get_platform_id(self) -> str:
        return self.platform

    def get_platform_name(self) -> str:
        return self.platform


@pytest.fixture
def plugin(tmp_path):
    instance = WebhookNotifierPlugin(Context(), {})  # type: ignore[arg-type]
    instance._registry = EndpointRegistry(tmp_path)

    async def no_start():
        return None

    instance._ensure_server_running = no_start  # type: ignore[method-assign]
    return instance


def _assert_default_notice(message: str) -> None:
    # These are protocol instructions a caller needs, not an exact prose snapshot.
    assert "默认" in message
    assert "markdown.message" in message
    assert "--provider omp" in message
    assert "--provider opencode" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "args", "name", "pending"),
    [
        ("aiocqhttp", ["private", "direct"], "direct", False),
        ("aiocqhttp", ["group", "123456", "group"], "group", True),
        ("qq_official", ["group", "current", "group"], "group", True),
    ],
)
async def test_omitted_provider_persists_markdown_and_explains_protocol(
    plugin, tmp_path, platform, args, name, pending
):
    result = await plugin._handle_token_new(_PrivateEventStub(platform), args)
    saved = EndpointRegistry(tmp_path)
    record = saved.get_scoped(platform, "user", name)

    assert record is not None
    assert record.provider == "markdown"
    assert record.status == ("pending_verification" if pending else "active")
    if pending:
        assert isinstance(result, str)
        _assert_default_notice(result)
        assert "Bearer Token" not in result
        assert saved.get_pending_descriptor(
            platform, record.pending_request_id or ""
        ) is not None
    else:
        assert isinstance(result, list) and len(result) == 2
        summary, token_message = result
        _assert_default_notice(summary)
        assert "Bearer Token" not in summary
        assert token_message.startswith("Bearer Token: ")
        token = token_message.removeprefix("Bearer Token: ")
        assert token not in summary
        assert saved.authenticate_delivery(record.path, f"Bearer {token}").authorized
        assert "markdown.message" not in token_message
        assert "--provider" not in token_message
        assert "默认" not in token_message


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["omp", "opencode", "markdown"])
@pytest.mark.parametrize(
    ("platform", "args", "name"),
    [
        ("aiocqhttp", ["private", "explicit"], "explicit"),
        ("aiocqhttp", ["group", "123456", "explicit"], "explicit"),
        ("qq_official", ["group", "current", "explicit"], "explicit"),
    ],
)
async def test_explicit_provider_is_preserved_without_default_notice(
    plugin, tmp_path, provider, platform, args, name
):
    result = await plugin._handle_token_new(
        _PrivateEventStub(platform), [*args, "--provider", provider]
    )

    record = EndpointRegistry(tmp_path).get_scoped(platform, "user", name)
    assert record is not None and record.provider == provider
    messages = result if isinstance(result, list) else [result]
    assert all("markdown.message" not in message for message in messages)
    assert all("--provider" not in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flags",
    [
        ["--provider"],
        ["--provider", "omp", "--provider", "markdown"],
        ["--provider", "omp", "--provider", "omp"],
        ["--provider=markdown"],
        ["--provider", "--other"],
        ["--provider", ""],
        ["--provider", "   "],
        ["--provider", "unknown"],
    ],
)
async def test_invalid_provider_never_creates_or_falls_back(plugin, tmp_path, flags):
    result = await plugin._handle_token_new(
        _PrivateEventStub(), ["private", "invalid", *flags]
    )

    assert isinstance(result, str)
    assert result.startswith("❌")
    assert "markdown.message" not in result
    assert plugin._registry.list_all_for_admin() == []
    assert EndpointRegistry(tmp_path).list_all_for_admin() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "args"),
    [
        ("aiocqhttp", ["private", "existing"]),
        ("aiocqhttp", ["group", "123456", "existing"]),
        ("qq_official", ["group", "current", "existing"]),
    ],
)
async def test_creation_conflict_does_not_announce_default_success(
    plugin, tmp_path, platform, args
):
    event = _PrivateEventStub(platform)
    await plugin._handle_token_new(event, [*args, "--provider", "omp"])
    before = EndpointRegistry(tmp_path).get_scoped(platform, "user", "existing")

    result = await plugin._handle_token_new(event, args)

    assert isinstance(result, str) and result.startswith("❌")
    assert "markdown.message" not in result
    assert "--provider" not in result
    assert EndpointRegistry(tmp_path).get_scoped(platform, "user", "existing") == before


@pytest.mark.asyncio
async def test_creation_rejects_unregistered_provider_and_reports_available_set(
    plugin, tmp_path
):
    registered = OpenCodeProviderAdapter()
    providers = ProviderRegistry()
    providers.register(registered)
    providers.freeze()
    plugin._provider_registry = providers

    accepted = await plugin._handle_token_new(
        _PrivateEventStub(), ["private", "accepted", "--provider", registered.provider]
    )
    rejected = await plugin._handle_token_new(
        _PrivateEventStub(), ["private", "rejected", "--provider", "markdown"]
    )
    unknown = await plugin._handle_token_new(
        _PrivateEventStub(), ["private", "unknown", "--provider", "not-installed"]
    )

    assert isinstance(accepted, list)
    saved = EndpointRegistry(tmp_path)
    assert saved.get_scoped("aiocqhttp", "user", "accepted").provider == registered.provider
    assert saved.get_scoped("aiocqhttp", "user", "rejected") is None
    assert saved.get_scoped("aiocqhttp", "user", "unknown") is None
    assert isinstance(rejected, str) and rejected.startswith("❌")
    assert isinstance(unknown, str) and unknown.startswith("❌")
    assert registered.provider in unknown
    for unavailable in {"omp", "opencode", "markdown"} - providers.providers:
        assert unavailable not in unknown


@pytest.mark.asyncio
async def test_missing_default_adapter_does_not_fall_back_to_registered_omp(
    plugin, tmp_path
):
    providers = ProviderRegistry()
    providers.register(OmpProviderAdapter())
    providers.freeze()
    plugin._provider_registry = providers

    result = await plugin._handle_token_new(_PrivateEventStub(), ["private", "missing"])

    assert isinstance(result, str) and result.startswith("❌")
    assert "markdown.message" not in result
    assert EndpointRegistry(tmp_path).list_all_for_admin() == []


@pytest.mark.parametrize("provider", ["omp", "opencode", "markdown"])
def test_provider_survives_rotation_revocation_and_reload(tmp_path, provider):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        "aiocqhttp", "user", "immutable", "aiocqhttp:FriendMessage:user",
        provider=provider,
    )

    ok, new_token = registry.rotate_token("aiocqhttp", "user", "immutable")
    assert ok
    reloaded = EndpointRegistry(tmp_path)
    rotated = reloaded.get_scoped("aiocqhttp", "user", "immutable")
    assert rotated is not None and rotated.provider == provider
    assert not reloaded.authenticate_delivery(record.path, f"Bearer {token}").authorized
    assert reloaded.authenticate_delivery(record.path, f"Bearer {new_token}").authorized
    assert reloaded.revoke_endpoint("aiocqhttp", "user", "immutable")[0]
    revoked = EndpointRegistry(tmp_path).get_scoped("aiocqhttp", "user", "immutable")
    assert revoked is not None and revoked.provider == provider
    assert revoked.status == "revoked"
