from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

import core.rebind as rebind_module
from core.rebind import RebindError, execute_rebind, plan_rebind, rollback_rebind
from core.registry import (
    QUARANTINED_LEGACY,
    REGISTRY_FILENAME,
    EndpointRegistry,
)
from scripts.rebind_platform_id import main as rebind_main


def _registry_path(tmp_path: Path) -> Path:
    return tmp_path / REGISTRY_FILENAME


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _sidecars(tmp_path: Path) -> set[str]:
    return {path.name for path in tmp_path.iterdir() if path.name != REGISTRY_FILENAME}


def test_dry_run_is_strictly_read_only(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    path = _registry_path(tmp_path)
    before_digest = _digest(path)
    before_mtime = path.stat().st_mtime_ns
    before_files = _sidecars(tmp_path)

    plan = plan_rebind(path, "old-bot", "new-bot")

    assert plan.selected_count == 1
    assert _digest(path) == before_digest
    assert path.stat().st_mtime_ns == before_mtime
    assert _sidecars(tmp_path) == before_files


def test_execute_rebind_preserves_path_and_token_then_new_scope_can_manage(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    manifest = tmp_path / "audit.json"

    audit = execute_rebind(
        _registry_path(tmp_path),
        "old-bot",
        "new-bot",
        manifest_path=manifest,
        confirm_offline=True,
    )
    rebound = EndpointRegistry(tmp_path)

    assert audit["selected_count"] == 1
    assert (tmp_path / audit["backup_file"]).stat().st_mode & 0o777 == 0o600
    assert rebound.get_scoped("old-bot", "owner", "name") is None
    moved = rebound.get_scoped("new-bot", "owner", "name")
    assert moved is not None
    assert moved.path == record.path
    assert moved.targets[0].umo == "new-bot:FriendMessage:owner"
    assert rebound.authenticate_delivery(record.path, f"Bearer {token}").authorized
    assert rebound.rotate_token("old-bot", "owner", "name")[0] is False
    rotated, new_token = rebound.rotate_token("new-bot", "owner", "name")
    assert rotated is True
    assert rebound.authenticate_delivery(record.path, f"Bearer {new_token}").authorized
    assert rebound.revoke_endpoint("new-bot", "owner", "name")[0] is True


@pytest.mark.parametrize(
    "case",
    ["destination-conflict", "umo-mismatch", "empty", "unknown-version", "quarantine"],
)
def test_rebind_validation_failures_write_nothing(tmp_path, case):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    owner_selector = None
    if case == "destination-conflict":
        registry.create_private_endpoint(
            "new-bot", "owner", "name", "new-bot:FriendMessage:owner"
        )
    elif case == "umo-mismatch":
        record = next(iter(registry._records.values()))
        record.targets[0].umo = "other-bot:FriendMessage:owner"
        _registry_path(tmp_path).write_bytes(
            registry._serialize(
                registry._records, registry._quarantine, registry._pending
            )
        )
    elif case == "empty":
        owner_selector = "missing-owner"
    elif case == "unknown-version":
        raw = json.loads(_registry_path(tmp_path).read_text())
        raw["version"] = 99
        _registry_path(tmp_path).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
        )
    elif case == "quarantine":
        record = deepcopy(next(iter(registry._records.values())))
        record.owner_platform_id = ""
        record.management_state = QUARANTINED_LEGACY
        record.legacy_record_key = "legacy-record"
        registry._records = {}
        registry._quarantine = {"legacy-record": record}
        _registry_path(tmp_path).write_bytes(
            registry._serialize(registry._records, registry._quarantine, {})
        )
        owner_selector = "owner"

    path = _registry_path(tmp_path)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    before_files = _sidecars(tmp_path)
    with pytest.raises(RebindError):
        execute_rebind(
            path,
            "old-bot",
            "new-bot",
            owner_selector,
            tmp_path / "audit.json",
            confirm_offline=True,
        )
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    assert _sidecars(tmp_path) == before_files


def test_execute_expires_selected_and_unselected_pending_and_clears_all(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_group_pending(
        "old-bot", "selected", "one", "prebound_group", "group-1"
    )
    registry.create_group_pending(
        "old-bot", "unselected", "two", "prebound_group", "group-2"
    )
    registry.create_group_pending(
        "other-bot", "other", "three", "prebound_group", "group-3"
    )

    execute_rebind(
        _registry_path(tmp_path),
        "old-bot",
        "new-bot",
        owner_user_id="selected",
        manifest_path=tmp_path / "audit.json",
        confirm_offline=True,
    )
    reloaded = EndpointRegistry(tmp_path)

    assert reloaded._pending == {}
    records = reloaded.list_all_for_admin()
    assert {record.status for record in records} == {"expired"}
    assert all(record.pending_request_id is None for record in records)
    assert reloaded.get_scoped("new-bot", "selected", "one") is not None
    assert reloaded.get_scoped("old-bot", "unselected", "two") is not None
    assert reloaded.get_scoped("other-bot", "other", "three") is not None


def test_rebind_defensively_expires_pending_quarantine_record(tmp_path, monkeypatch):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    quarantined = deepcopy(next(iter(registry._records.values())))
    quarantined.owner_platform_id = ""
    quarantined.management_state = QUARANTINED_LEGACY
    quarantined.legacy_record_key = "legacy-pending"
    quarantined.path = "legacy/pending"
    quarantined.status = "pending_verification"
    quarantined.pending_request_id = "request"
    quarantined.pending_code = "code"
    quarantined.pending_expires_at = "2099-01-01T00:00:00+00:00"
    registry._quarantine = {"legacy-pending": quarantined}
    path = _registry_path(tmp_path)
    path.write_bytes(registry._serialize(registry._records, registry._quarantine, {}))

    original_validate = EndpointRegistry._validate_snapshot

    def allow_legacy_pending(self, records, quarantine, pending):
        saved = quarantine["legacy-pending"].status
        quarantine["legacy-pending"].status = "expired"
        try:
            original_validate(self, records, quarantine, pending)
        finally:
            quarantine["legacy-pending"].status = saved

    monkeypatch.setattr(EndpointRegistry, "_validate_snapshot", allow_legacy_pending)
    execute_rebind(
        path,
        "old-bot",
        "new-bot",
        manifest_path=tmp_path / "audit.json",
        confirm_offline=True,
    )
    raw = json.loads(path.read_text())
    record = raw["quarantine"]["legacy-pending"]
    assert record["status"] == "expired"
    assert record["pending_request_id"] is None
    assert record["pending_code"] is None
    assert record["pending_expires_at"] is None


def test_rebind_reload_is_idempotent_and_canonical(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    execute_rebind(
        _registry_path(tmp_path),
        "old-bot",
        "new-bot",
        manifest_path=tmp_path / "audit.json",
        confirm_offline=True,
    )
    before = _registry_path(tmp_path).read_bytes()

    first = EndpointRegistry(tmp_path)
    second = EndpointRegistry(tmp_path)

    assert first.list_all_for_admin() == second.list_all_for_admin()
    assert _registry_path(tmp_path).read_bytes() == before


def test_rollback_restores_platform_without_restoring_pending_or_changing_token_path(
    tmp_path,
):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        "old-bot", "owner", "active", "old-bot:FriendMessage:owner"
    )
    registry.create_group_pending(
        "old-bot", "owner", "pending", "prebound_group", "group"
    )
    manifest = tmp_path / "audit.json"
    execute_rebind(
        _registry_path(tmp_path),
        "old-bot",
        "new-bot",
        manifest_path=manifest,
        confirm_offline=True,
    )

    rollback_rebind(
        _registry_path(tmp_path),
        manifest,
        audit_path=tmp_path / "rollback.json",
        confirm_offline=True,
    )
    restored = EndpointRegistry(tmp_path)

    active = restored.get_scoped("old-bot", "owner", "active")
    pending = restored.get_scoped("old-bot", "owner", "pending")
    assert active is not None and active.path == record.path
    assert restored.authenticate_delivery(record.path, f"Bearer {token}").authorized
    assert pending is not None and pending.status == "expired"
    assert restored._pending == {}
    assert restored.get_scoped("new-bot", "owner", "active") is None


def test_rollback_digest_guard_rejects_registry_changed_after_execute(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    manifest = tmp_path / "audit.json"
    execute_rebind(
        _registry_path(tmp_path),
        "old-bot",
        "new-bot",
        manifest_path=manifest,
        confirm_offline=True,
    )
    changed = EndpointRegistry(tmp_path)
    assert changed.rotate_token("new-bot", "owner", "name")[0] is True
    before = _registry_path(tmp_path).read_bytes()
    files = _sidecars(tmp_path)

    with pytest.raises(RebindError, match="digest"):
        rollback_rebind(
            _registry_path(tmp_path),
            manifest,
            audit_path=tmp_path / "rollback.json",
            confirm_offline=True,
        )

    assert _registry_path(tmp_path).read_bytes() == before
    assert _sidecars(tmp_path) == files


def test_audit_and_cli_output_are_redacted(tmp_path, capsys):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        "old-bot", "sensitive-owner", "name", "old-bot:FriendMessage:private-id"
    )
    manifest = tmp_path / "audit.json"

    exit_code = rebind_main(
        [
            "--registry",
            str(_registry_path(tmp_path)),
            "--source-platform-id",
            "old-bot",
            "--destination-platform-id",
            "new-bot",
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    audit = execute_rebind(
        _registry_path(tmp_path),
        "old-bot",
        "new-bot",
        manifest_path=manifest,
        confirm_offline=True,
    )
    combined = output + manifest.read_text() + json.dumps(audit, ensure_ascii=False)
    for secret in (
        "sensitive-owner",
        record.path,
        "old-bot:FriendMessage:private-id",
        token,
        record.token_hash,
    ):
        assert secret not in combined


def test_cli_execute_and_rollback_require_explicit_offline_confirmation(
    tmp_path, capsys
):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    path = _registry_path(tmp_path)
    before = path.read_bytes()

    code = rebind_main(
        [
            "--registry",
            str(path),
            "--source-platform-id",
            "old-bot",
            "--destination-platform-id",
            "new-bot",
            "--execute",
        ]
    )

    assert code == 2
    assert "--confirm-offline" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_core_execute_requires_explicit_offline_confirmation(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    path = _registry_path(tmp_path)
    before = path.read_bytes()
    files = _sidecars(tmp_path)

    with pytest.raises(RebindError, match="停止"):
        execute_rebind(path, "old-bot", "new-bot", manifest_path=tmp_path / "a.json")

    assert path.read_bytes() == before
    assert _sidecars(tmp_path) == files


def _prepared_execute(tmp_path: Path) -> tuple[Path, Path]:
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        "old-bot", "owner", "name", "old-bot:FriendMessage:owner"
    )
    return _registry_path(tmp_path), tmp_path / "execute.json"


def test_execute_manifest_write_failure_never_changes_registry(tmp_path, monkeypatch):
    path, manifest = _prepared_execute(tmp_path)
    before = path.read_bytes()
    original_write = rebind_module._atomic_write

    def fail_manifest(target, payload, mode=0o600):
        if target == manifest:
            raise RebindError("durable write 失败")
        return original_write(target, payload, mode)

    monkeypatch.setattr(rebind_module, "_atomic_write", fail_manifest)
    with pytest.raises(RebindError, match="durable write"):
        execute_rebind(
            path, "old-bot", "new-bot", manifest_path=manifest, confirm_offline=True
        )
    assert path.read_bytes() == before
    assert not manifest.exists()


def test_execute_registry_write_failure_leaves_prepared_manifest_and_pre_digest(
    tmp_path, monkeypatch
):
    path, manifest = _prepared_execute(tmp_path)
    before = path.read_bytes()

    def fail_registry(target, payload):
        raise RebindError("Registry 写入失败")

    monkeypatch.setattr(rebind_module, "_commit_registry", fail_registry)
    with pytest.raises(RebindError, match="Registry 写入失败"):
        execute_rebind(
            path, "old-bot", "new-bot", manifest_path=manifest, confirm_offline=True
        )
    saved = json.loads(manifest.read_text())
    assert saved["state"] == "prepared"
    assert sha256(before).hexdigest() == saved["pre_sha256"]
    assert path.read_bytes() == before
    with pytest.raises(RebindError, match="尚未提交"):
        rollback_rebind(
            path,
            manifest,
            audit_path=tmp_path / "rollback.json",
            confirm_offline=True,
        )


def test_execute_parent_fsync_failure_is_committed_warning(tmp_path, monkeypatch):
    path, manifest = _prepared_execute(tmp_path)
    original_fsync = rebind_module._fsync_parent

    def fail_registry_parent(target):
        if target == path:
            raise RebindError("parent fsync 失败")
        return original_fsync(target)

    monkeypatch.setattr(rebind_module, "_fsync_parent", fail_registry_parent)
    result = execute_rebind(
        path, "old-bot", "new-bot", manifest_path=manifest, confirm_offline=True
    )
    assert result["changed"] is True
    assert result["warnings"] == ["registry_parent_fsync_failed"]
    assert manifest.exists()
    assert _digest(path) == result["post_sha256"]


def _executed_rebind(tmp_path: Path) -> tuple[Path, Path, Path]:
    path, manifest = _prepared_execute(tmp_path)
    execute_rebind(
        path, "old-bot", "new-bot", manifest_path=manifest, confirm_offline=True
    )
    return path, manifest, tmp_path / "rollback.json"


def test_rollback_manifest_write_failure_never_changes_registry(tmp_path, monkeypatch):
    path, manifest, rollback_manifest = _executed_rebind(tmp_path)
    before = path.read_bytes()
    original_write = rebind_module._atomic_write

    def fail_manifest(target, payload, mode=0o600):
        if target == rollback_manifest:
            raise RebindError("durable write 失败")
        return original_write(target, payload, mode)

    monkeypatch.setattr(rebind_module, "_atomic_write", fail_manifest)
    with pytest.raises(RebindError, match="durable write"):
        rollback_rebind(
            path,
            manifest,
            audit_path=rollback_manifest,
            confirm_offline=True,
        )
    assert path.read_bytes() == before
    assert not rollback_manifest.exists()


def test_rollback_registry_write_failure_leaves_prepared_manifest(
    tmp_path, monkeypatch
):
    path, manifest, rollback_manifest = _executed_rebind(tmp_path)
    before = path.read_bytes()

    def fail_registry(target, payload):
        raise RebindError("Registry 写入失败")

    monkeypatch.setattr(rebind_module, "_commit_registry", fail_registry)
    with pytest.raises(RebindError, match="Registry 写入失败"):
        rollback_rebind(
            path,
            manifest,
            audit_path=rollback_manifest,
            confirm_offline=True,
        )
    saved = json.loads(rollback_manifest.read_text())
    assert saved["state"] == "prepared"
    assert saved["pre_sha256"] == sha256(before).hexdigest()
    assert path.read_bytes() == before


def test_rollback_parent_fsync_failure_is_committed_warning(tmp_path, monkeypatch):
    path, manifest, rollback_manifest = _executed_rebind(tmp_path)
    original_fsync = rebind_module._fsync_parent

    def fail_registry_parent(target):
        if target == path:
            raise RebindError("parent fsync 失败")
        return original_fsync(target)

    monkeypatch.setattr(rebind_module, "_fsync_parent", fail_registry_parent)
    result = rollback_rebind(
        path,
        manifest,
        audit_path=rollback_manifest,
        confirm_offline=True,
    )
    assert result["changed"] is True
    assert result["warnings"] == ["registry_parent_fsync_failed"]
    assert rollback_manifest.exists()
    assert _digest(path) == result["post_sha256"]


def test_cli_unexpected_exception_is_redacted(tmp_path, monkeypatch, capsys):
    secret_path = str(tmp_path / "sensitive-registry.json")

    def explode(*args, **kwargs):
        raise RuntimeError(f"private failure at {secret_path}")

    monkeypatch.setattr("scripts.rebind_platform_id.plan_rebind", explode)
    code = rebind_main(
        [
            "--registry",
            secret_path,
            "--source-platform-id",
            "old-bot",
            "--destination-platform-id",
            "new-bot",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "rebind 失败: 未预期的内部错误\n"
    assert secret_path not in captured.err


def test_cli_expected_io_failure_is_redacted(tmp_path, capsys):
    secret_path = str(tmp_path / "private" / "missing-registry.json")
    code = rebind_main(
        [
            "--registry",
            secret_path,
            "--source-platform-id",
            "old-bot",
            "--destination-platform-id",
            "new-bot",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "rebind 失败: 无法读取 Registry\n"
    assert secret_path not in captured.err
