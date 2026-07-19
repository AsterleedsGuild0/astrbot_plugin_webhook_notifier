from __future__ import annotations

# ruff: noqa: E402

import json
import logging
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context
from core.models import EndpointRecord, ServerConfig
from core.registry import REGISTRY_FILENAME, EndpointRegistry, RegistryPersistenceError
from core.server import WebhookServer
from astrbot_plugin_webhook_notifier.main import WebhookNotifierPlugin, _CommandRoots
from tests.test_server import FakeSender, RequestStub


PLATFORM = "aiocqhttp"


class DeleteEvent:
    def __init__(self, *, owner: str = "owner", platform: str = PLATFORM, private=True):
        self._owner = owner
        self._platform = platform
        self.session = SimpleNamespace(message_type="friend" if private else "group")

    def get_sender_id(self):
        return self._owner

    def get_platform_id(self):
        return self._platform


def _expire(registry: EndpointRegistry, owner: str, name: str) -> tuple[str, str]:
    record, request_id, _ = registry.create_pending_verification(
        PLATFORM, owner, name, "123"
    )
    pending_key = next(
        key
        for key, value in registry._pending.items()
        if value["request_id"] == request_id
    )
    registry._pending[pending_key]["expires_at"] = "2020-01-01T00:00:00+00:00"
    registry.expire_stale_pending()
    return record.path, request_id


@pytest.mark.parametrize("terminal", ["revoked", "expired"])
def test_delete_terminal_removes_record_and_allows_safe_recreate(tmp_path, terminal):
    registry = EndpointRegistry(tmp_path)
    if terminal == "revoked":
        record, old_token = registry.create_private_endpoint(
            PLATFORM, "owner", "same", f"{PLATFORM}:FriendMessage:owner"
        )
        registry.revoke_endpoint(PLATFORM, "owner", "same")
    else:
        path, _ = _expire(registry, "owner", "same")
        record = registry.get_scoped(PLATFORM, "owner", "same")
        assert record is not None and record.path == path
        old_token = "never-valid"

    assert registry.delete_endpoint(PLATFORM, "owner", "same") == (
        "deleted",
        terminal,
    )
    assert registry.get_scoped(PLATFORM, "owner", "same") is None
    assert (
        registry.authenticate_delivery(record.path, f"Bearer {old_token}").error_code
        == "not_found"
    )

    recreated, new_token = registry.create_private_endpoint(
        PLATFORM, "owner", "same", f"{PLATFORM}:FriendMessage:owner"
    )
    assert recreated.path == record.path
    assert registry.authenticate_delivery(
        recreated.path, f"Bearer {new_token}"
    ).authorized
    assert not registry.authenticate_delivery(
        recreated.path, f"Bearer {old_token}"
    ).authorized
    assert EndpointRegistry(tmp_path).get_scoped(PLATFORM, "owner", "same") == recreated


def test_delete_rejects_nonterminal_notfound_and_scope_mismatch(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "name", f"{PLATFORM}:FriendMessage:1"
    )
    registry.create_pending_verification(PLATFORM, "owner", "pending", "123")
    registry.create_private_endpoint(
        "other-platform", "owner", "name", "other-platform:FriendMessage:1"
    )
    registry.create_private_endpoint(
        PLATFORM, "other-owner", "name", f"{PLATFORM}:FriendMessage:2"
    )

    assert registry.delete_endpoint(PLATFORM, "owner", "name") == ("active", "active")
    assert registry.delete_endpoint(PLATFORM, "owner", "pending") == (
        "pending",
        "pending_verification",
    )
    assert registry.delete_endpoint(PLATFORM, "missing", "name") == ("not_found", None)
    assert registry.get_scoped("other-platform", "owner", "name") is not None
    assert registry.get_scoped(PLATFORM, "other-owner", "name") is not None


def test_delete_quarantine_is_not_discoverable(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry._quarantine["legacy"] = EndpointRecord(
        name="legacy",
        path="legacy/path",
        provider="omp",
        token_hash="",
        token_hash_algorithm="hmac-sha256",
        owner_user_id="legacy-owner",
        targets=[],
        status="revoked",
        created_at="2026-01-01T00:00:00+00:00",
        owner_platform_id="",
        management_state="quarantined_legacy",
        legacy_record_key="legacy",
    )
    assert registry.delete_endpoint(PLATFORM, "owner", "legacy") == ("not_found", None)


def test_delete_defensively_cleans_only_matching_platform_pending(tmp_path):
    registry = EndpointRegistry(tmp_path)
    _, current_request = _expire(registry, "owner", "terminal")
    other_record, other_request, _ = registry.create_pending_verification(
        "other-platform", "owner", "terminal", "456"
    )
    current_key = next(
        key for key, value in registry._records.items() if value.name == "terminal"
    )
    registry._records[current_key].pending_request_id = current_request
    other_pending_key = next(
        key
        for key, value in registry._pending.items()
        if value["request_id"] == other_request
    )
    stale = deepcopy(registry._pending[other_pending_key])
    stale.update(
        owner_platform_id=PLATFORM,
        owner_user_id="owner",
        endpoint_name="terminal",
        request_id=current_request,
        expires_at=registry._records[current_key].created_at,
    )
    stale_key = json.dumps([PLATFORM, current_request], separators=(",", ":"))
    registry._pending[stale_key] = stale

    assert registry.delete_endpoint(PLATFORM, "owner", "terminal")[0] == "deleted"
    assert registry.get_pending_descriptor("other-platform", other_request) is not None
    assert registry.get_scoped("other-platform", "owner", "terminal") == other_record


def test_delete_persistence_failure_rolls_back_memory_and_disk(tmp_path, monkeypatch):
    registry = EndpointRegistry(tmp_path)
    record, _ = registry.create_private_endpoint(
        PLATFORM, "owner", "terminal", f"{PLATFORM}:FriendMessage:1"
    )
    registry.revoke_endpoint(PLATFORM, "owner", "terminal")
    before = (tmp_path / REGISTRY_FILENAME).read_bytes()
    monkeypatch.setattr(
        registry,
        "_atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RegistryPersistenceError("fail")
        ),
    )

    with pytest.raises(RegistryPersistenceError):
        registry.delete_endpoint(PLATFORM, "owner", "terminal")
    assert registry.get_scoped(PLATFORM, "owner", "terminal") is not None
    assert (tmp_path / REGISTRY_FILENAME).read_bytes() == before
    assert record.path


def test_double_delete_has_at_most_one_success_and_reload_matches(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "terminal", f"{PLATFORM}:FriendMessage:1"
    )
    registry.revoke_endpoint(PLATFORM, "owner", "terminal")
    barrier = threading.Barrier(3)
    results = []

    def run():
        barrier.wait()
        results.append(registry.delete_endpoint(PLATFORM, "owner", "terminal"))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
    assert [result[0] for result in results].count("deleted") == 1
    assert [result[0] for result in results].count("not_found") == 1
    assert EndpointRegistry(tmp_path).get_scoped(PLATFORM, "owner", "terminal") is None


@pytest.mark.parametrize("competitor", ["create", "rotate", "revoke"])
def test_delete_races_are_linearizable_and_reload_consistent(tmp_path, competitor):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "terminal", f"{PLATFORM}:FriendMessage:1"
    )
    registry.revoke_endpoint(PLATFORM, "owner", "terminal")
    barrier = threading.Barrier(3)
    outcomes = []

    def delete():
        barrier.wait()
        outcomes.append(
            ("delete", registry.delete_endpoint(PLATFORM, "owner", "terminal"))
        )

    def compete():
        barrier.wait()
        try:
            if competitor == "create":
                result = registry.create_private_endpoint(
                    PLATFORM, "owner", "terminal", f"{PLATFORM}:FriendMessage:1"
                )
            elif competitor == "rotate":
                result = registry.rotate_token(PLATFORM, "owner", "terminal")
            else:
                result = registry.revoke_endpoint(PLATFORM, "owner", "terminal")
            outcomes.append((competitor, result))
        except Exception as exc:  # noqa: BLE001 - 线程竞争结果需要收集
            outcomes.append((competitor, exc))

    threads = [threading.Thread(target=delete), threading.Thread(target=compete)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    final = registry.get_scoped(PLATFORM, "owner", "terminal")
    assert EndpointRegistry(tmp_path).get_scoped(PLATFORM, "owner", "terminal") == final
    assert next(value for name, value in outcomes if name == "delete")[0] == "deleted"
    if competitor == "create" and final is not None:
        assert final.status == "active"
    elif competitor != "create":
        assert final is None


@pytest.mark.asyncio
async def test_deleted_endpoint_real_server_returns_404(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        PLATFORM, "owner", "terminal", f"{PLATFORM}:FriendMessage:1"
    )
    registry.revoke_endpoint(PLATFORM, "owner", "terminal")
    registry.delete_endpoint(PLATFORM, "owner", "terminal")
    server = WebhookServer(
        ServerConfig(), registry, FakeSender(), plugin_config={"render_mode": "text"}
    )

    response = await server._process_request(  # type: ignore[arg-type]
        RequestStub(f"/webhook/{record.path}", token), "deleted"
    )
    assert response.status == 404
    assert json.loads(response.body)["data"]["error"] == "not_found"


@pytest.mark.asyncio
async def test_delete_command_private_dynamic_safe_and_audit_redacted(tmp_path, caplog):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        PLATFORM, "secret-owner", "Fancy-Name", f"{PLATFORM}:FriendMessage:secret-umo"
    )
    registry.revoke_endpoint(PLATFORM, "secret-owner", "Fancy-Name")
    plugin._registry = registry
    caplog.set_level(logging.INFO, logger="astrbot")

    success = await plugin._handle_token_delete(
        DeleteEvent(owner="secret-owner"),
        ["Fancy", "Name!!"],
        _CommandRoots(short="!whn", long="!webhook_notifier"),
    )
    output = success + caplog.text
    assert "Endpoint 已永久删除" in success and "Fancy-Name" in success
    assert "不可恢复" in success
    for secret in (record.path, token, record.token_hash, "secret-owner", "secret-umo"):
        assert secret not in output
    assert "operation=delete" in caplog.text
    assert "platform=aiocqhttp" in caplog.text

    group = await plugin._handle_token_delete(
        DeleteEvent(private=False),
        ["name"],
        _CommandRoots(short="!whn", long="!webhook_notifier"),
    )
    usage = await plugin._handle_token_delete(
        DeleteEvent(), [], _CommandRoots(short="!whn", long="!webhook_notifier")
    )
    assert "私聊" in group
    assert "!whn token delete <名称>" in usage


@pytest.mark.asyncio
async def test_delete_command_distinguishes_active_pending_and_not_found(tmp_path):
    plugin = WebhookNotifierPlugin(Context(), AstrBotConfig())
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "active", f"{PLATFORM}:FriendMessage:owner"
    )
    registry.create_pending_verification(PLATFORM, "owner", "pending", "123")
    plugin._registry = registry
    commands = _CommandRoots(short="!whn", long="!webhook_notifier")
    event = DeleteEvent()

    active = await plugin._handle_token_delete(event, ["active"], commands)
    pending = await plugin._handle_token_delete(event, ["pending"], commands)
    missing = await plugin._handle_token_delete(event, ["missing"], commands)

    assert "!whn token revoke active" in active
    assert "pending_verification" in pending and "强制删除" not in pending
    assert "不存在" in missing
