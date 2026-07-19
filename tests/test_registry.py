from __future__ import annotations

import json
import inspect
import os
from pathlib import Path

import pytest

from core.models import EndpointStatus
from core.registry import (
    REGISTRY_FILENAME,
    EndpointRegistry,
    RegistryConflictError,
    RegistryLoadError,
    RegistryPersistenceError,
    build_endpoint_path,
    owner_path_hash,
)
from core.security import hash_token

PLATFORM = "aiocqhttp"


def _legacy_record(
    *,
    name: str = "legacy",
    path: str = "u/legacy/path",
    owner: str = "owner",
    targets: list[dict] | None = None,
    token_hash: str = "legacy-hash",
    status: str = "active",
    pending_request_id: str | None = None,
) -> dict:
    return {
        "name": name,
        "path": path,
        "provider": "omp",
        "token_hash": token_hash,
        "token_hash_algorithm": "hmac-sha256",
        "owner_user_id": owner,
        "targets": targets
        if targets is not None
        else [{"name": "default", "umo": "aiocqhttp:FriendMessage:10001"}],
        "status": status,
        "created_at": "2026-07-10T00:00:00+00:00",
        "pending_request_id": pending_request_id,
        "pending_code": None,
        "pending_expires_at": "2026-07-10T00:10:00+00:00"
        if pending_request_id
        else None,
    }


def _write_legacy(path: Path, records: dict, pending: dict | None = None) -> bytes:
    payload = json.dumps(
        {"records": records, "pending": pending or {}}, ensure_ascii=False
    ).encode()
    (path / REGISTRY_FILENAME).write_bytes(payload)
    return payload


def test_create_is_platform_scoped_and_path_is_generated_in_transaction(tmp_path):
    registry = EndpointRegistry(tmp_path)
    first, first_token = registry.create_private_endpoint(
        PLATFORM, "same-owner", "same-name", "aiocqhttp:FriendMessage:1"
    )
    second, _ = registry.create_private_endpoint(
        "qq_official", "same-owner", "same-name", "qq_official:FriendMessage:1"
    )

    assert first.path == build_endpoint_path(PLATFORM, "same-owner", "same-name")
    assert first.path != second.path
    assert owner_path_hash(PLATFORM, "same-owner") in first.path
    assert registry.authenticate_delivery(
        first.path, f"Bearer {first_token}"
    ).authorized
    with pytest.raises(RegistryConflictError):
        registry.create_private_endpoint(
            PLATFORM, "same-owner", "same-name", "aiocqhttp:FriendMessage:2"
        )


def test_create_persists_a_record_that_real_reload_accepts(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, token = registry.create_private_endpoint(
        PLATFORM, "owner", "reloadable", "aiocqhttp:FriendMessage:1"
    )

    reloaded = EndpointRegistry(tmp_path)
    saved = reloaded.get_scoped(PLATFORM, "owner", "reloadable")

    assert saved == record
    assert reloaded.authenticate_delivery(record.path, f"Bearer {token}").authorized


@pytest.mark.parametrize("target_umo", [None, 123, ""])
def test_create_rejects_invalid_target_umo_before_writing(tmp_path, target_umo):
    registry = EndpointRegistry(tmp_path)

    with pytest.raises(RegistryLoadError):
        registry.create_private_endpoint(PLATFORM, "owner", "invalid", target_umo)

    assert registry.get_scoped(PLATFORM, "owner", "invalid") is None
    assert not (tmp_path / REGISTRY_FILENAME).exists()


def test_reads_are_copies_and_terminal_path_cannot_be_reused(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, _ = registry.create_private_endpoint(
        PLATFORM, "owner", "name", "aiocqhttp:FriendMessage:1"
    )
    copy = registry.get_scoped(PLATFORM, "owner", "name")
    assert copy is not None
    copy.status = EndpointStatus.REVOKED.value
    assert registry.get_scoped(PLATFORM, "owner", "name").status == "active"
    assert registry.revoke_endpoint(PLATFORM, "owner", "name")[0]
    with pytest.raises(RegistryConflictError):
        registry.create_private_endpoint(
            PLATFORM, "owner", "name", "aiocqhttp:FriendMessage:1"
        )
    assert registry.get_scoped(PLATFORM, "owner", "name").status == "revoked"
    assert not hasattr(registry, "get_by_path")


@pytest.mark.parametrize(
    ("targets", "managed"),
    [
        ([{"name": "a", "umo": "aiocqhttp:FriendMessage:1"}], True),
        ([], False),
        ([{"name": "a", "umo": "malformed"}], False),
        (
            [
                {"name": "a", "umo": "aiocqhttp:FriendMessage:1"},
                {"name": "b", "umo": "qq_official:FriendMessage:2"},
            ],
            False,
        ),
    ],
)
def test_v1_migration_infers_only_one_valid_platform(tmp_path, targets, managed):
    original = _write_legacy(
        tmp_path, {"owner\x1flegacy": _legacy_record(targets=targets)}
    )
    registry = EndpointRegistry(tmp_path)
    saved = json.loads((tmp_path / REGISTRY_FILENAME).read_text())

    assert saved["version"] == 2
    assert (tmp_path / f"{REGISTRY_FILENAME}.v1.bak").read_bytes() == original
    assert bool(saved["records"]) is managed
    assert bool(saved["quarantine"]) is (not managed)
    if managed:
        record = registry.get_scoped(PLATFORM, "owner", "legacy")
        assert record is not None and record.legacy_record_key == "owner\x1flegacy"
        assert record.path == "u/legacy/path"
    else:
        assert registry.list_by_owner(PLATFORM, "owner") == []


def test_v1_pending_is_cleared_and_associated_record_expires(tmp_path):
    request_id = "request-1"
    _write_legacy(
        tmp_path,
        {
            "owner\x1fpending": _legacy_record(
                name="pending",
                status="pending_verification",
                pending_request_id=request_id,
            )
        },
        {
            request_id: {
                "request_id": request_id,
                "endpoint_name": "pending",
                "owner_user_id": "owner",
            }
        },
    )
    registry = EndpointRegistry(tmp_path)
    record = registry.get_scoped(PLATFORM, "owner", "pending")
    saved = json.loads((tmp_path / REGISTRY_FILENAME).read_text())

    assert saved["pending"] == {}
    assert record is not None and record.status == EndpointStatus.EXPIRED.value
    assert record.pending_request_id is None
    assert record.pending_expires_at is None


def test_migration_reload_is_idempotent_and_backup_is_created_once(tmp_path):
    _write_legacy(tmp_path, {"owner\x1flegacy": _legacy_record()})
    first = EndpointRegistry(tmp_path)
    first_bytes = (tmp_path / REGISTRY_FILENAME).read_bytes()
    backup = tmp_path / f"{REGISTRY_FILENAME}.v1.bak"
    backup_stat = backup.stat()

    second = EndpointRegistry(tmp_path)
    third = EndpointRegistry(tmp_path)

    assert (
        first.count_deliverable()
        == second.count_deliverable()
        == third.count_deliverable()
    )
    assert (tmp_path / REGISTRY_FILENAME).read_bytes() == first_bytes
    assert backup.stat().st_mtime_ns == backup_stat.st_mtime_ns


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"foo":1}',
        b'{"version":3,"records":{},"quarantine":{},"pending":{}}',
        b'{"version":2,"records":{},"records":{},"quarantine":{},"pending":{}}',
    ],
)
def test_invalid_registry_fails_closed(tmp_path, payload):
    (tmp_path / REGISTRY_FILENAME).write_bytes(payload)
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)


def test_duplicate_path_fails_closed(tmp_path):
    _write_legacy(
        tmp_path,
        {
            "a": _legacy_record(name="a", owner="a", path="same"),
            "b": _legacy_record(name="b", owner="b", path="same"),
        },
    )
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)


def test_canonical_v2_rejects_pending_verification_in_quarantine(tmp_path):
    _write_legacy(
        tmp_path,
        {"legacy": _legacy_record(targets=[], path="legacy/quarantined")},
    )
    EndpointRegistry(tmp_path)
    path = tmp_path / REGISTRY_FILENAME
    raw = json.loads(path.read_text())
    record = raw["quarantine"]["legacy"]
    record["status"] = EndpointStatus.PENDING_VERIFICATION.value
    record["pending_request_id"] = "request"
    record["pending_code"] = "code"
    record["pending_expires_at"] = "2099-01-01T00:00:00+00:00"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")

    with pytest.raises(RegistryLoadError, match="quarantine"):
        EndpointRegistry(tmp_path)


def test_quarantine_delivery_is_compatible_but_management_is_hidden(tmp_path):
    seed = EndpointRegistry(tmp_path)
    token = "whn_legacy_token"
    token_hash = hash_token(seed.server_secret, token)
    _write_legacy(
        tmp_path,
        {
            "owner\x1flegacy": _legacy_record(
                targets=[], token_hash=token_hash, path="legacy/original"
            )
        },
    )
    registry = EndpointRegistry(tmp_path)

    auth = registry.authenticate_delivery("legacy/original", f"Bearer {token}")
    assert auth.authorized and auth.record is not None
    assert auth.record.management_state == "quarantined_legacy"
    assert registry.count_deliverable() == 1
    assert registry.get_scoped(PLATFORM, "owner", "legacy") is None
    assert registry.list_visible_by_owner(PLATFORM, "owner") == []
    assert registry.rotate_token(PLATFORM, "owner", "legacy")[0] is False
    assert registry.revoke_endpoint(PLATFORM, "owner", "legacy")[0] is False
    assert not hasattr(registry, "get_by_path")
    assert registry.revoke_endpoint_by_path("legacy/original")[0] is True
    assert (
        registry.authenticate_delivery("legacy/original", f"Bearer {token}").error_code
        == "endpoint_revoked"
    )


def test_create_persistence_failure_does_not_publish_memory_or_token(
    tmp_path, monkeypatch
):
    registry = EndpointRegistry(tmp_path)
    old_disk = (
        (tmp_path / REGISTRY_FILENAME).read_bytes()
        if (tmp_path / REGISTRY_FILENAME).exists()
        else None
    )
    monkeypatch.setattr(
        os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed"))
    )

    with pytest.raises(RegistryPersistenceError):
        registry.create_private_endpoint(
            PLATFORM, "owner", "name", "aiocqhttp:FriendMessage:1"
        )

    assert registry.get_scoped(PLATFORM, "owner", "name") is None
    current = (
        (tmp_path / REGISTRY_FILENAME).read_bytes()
        if (tmp_path / REGISTRY_FILENAME).exists()
        else None
    )
    assert current == old_disk


def test_rotate_persistence_failure_keeps_old_disk_memory_and_token(
    tmp_path, monkeypatch
):
    registry = EndpointRegistry(tmp_path)
    record, old_token = registry.create_private_endpoint(
        PLATFORM, "owner", "name", "aiocqhttp:FriendMessage:1"
    )
    old_disk = (tmp_path / REGISTRY_FILENAME).read_bytes()
    old_hash = record.token_hash
    monkeypatch.setattr(
        os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("fsync failed"))
    )

    with pytest.raises(RegistryPersistenceError):
        registry.rotate_token(PLATFORM, "owner", "name")

    assert (tmp_path / REGISTRY_FILENAME).read_bytes() == old_disk
    assert registry.get_scoped(PLATFORM, "owner", "name").token_hash == old_hash
    assert registry.authenticate_delivery(record.path, f"Bearer {old_token}").authorized


def test_create_parent_fsync_failure_publishes_disk_memory_and_reload(
    tmp_path, monkeypatch
):
    registry = EndpointRegistry(tmp_path)
    monkeypatch.setattr(
        EndpointRegistry,
        "_fsync_parent_required",
        lambda *_: (_ for _ in ()).throw(
            RegistryPersistenceError("injected parent fsync failure")
        ),
    )

    record, token = registry.create_private_endpoint(
        PLATFORM, "owner", "created", "aiocqhttp:FriendMessage:1"
    )
    memory = registry.get_scoped(PLATFORM, "owner", "created")
    reloaded = EndpointRegistry(tmp_path).get_scoped(PLATFORM, "owner", "created")

    assert memory == record == reloaded
    assert registry.authenticate_delivery(record.path, f"Bearer {token}").authorized


def test_rotate_parent_fsync_failure_publishes_disk_memory_and_reload(
    tmp_path, monkeypatch
):
    registry = EndpointRegistry(tmp_path)
    record, old_token = registry.create_private_endpoint(
        PLATFORM, "owner", "rotated", "aiocqhttp:FriendMessage:1"
    )
    monkeypatch.setattr(
        EndpointRegistry,
        "_fsync_parent_required",
        lambda *_: (_ for _ in ()).throw(
            RegistryPersistenceError("injected parent fsync failure")
        ),
    )

    success, new_token = registry.rotate_token(PLATFORM, "owner", "rotated")
    memory = registry.get_scoped(PLATFORM, "owner", "rotated")
    reloaded_registry = EndpointRegistry(tmp_path)
    reloaded = reloaded_registry.get_scoped(PLATFORM, "owner", "rotated")

    assert success is True
    assert memory == reloaded
    assert not registry.authenticate_delivery(
        record.path, f"Bearer {old_token}"
    ).authorized
    assert registry.authenticate_delivery(record.path, f"Bearer {new_token}").authorized
    assert reloaded_registry.authenticate_delivery(
        record.path, f"Bearer {new_token}"
    ).authorized


def test_backup_failure_keeps_v1_file_and_does_not_publish_candidate(
    tmp_path, monkeypatch
):
    original = _write_legacy(tmp_path, {"owner\x1flegacy": _legacy_record()})
    real_fsync = os.fsync

    def fail_backup_fsync(fd):
        raise OSError("backup fsync failed")

    monkeypatch.setattr(os, "fsync", fail_backup_fsync)
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert (tmp_path / REGISTRY_FILENAME).read_bytes() == original
    assert not (tmp_path / f"{REGISTRY_FILENAME}.v1.bak").exists()


def test_pending_keys_are_platform_scoped_and_verify_is_one_time(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, request_id, code = registry.create_pending_verification(
        PLATFORM, "owner", "group", "123456"
    )
    assert registry.get_pending_verification("qq_official", request_id) is None
    assert (
        registry.verify_group_endpoint(
            "qq_official",
            request_id,
            code,
            "owner",
            "123456",
            "owner",
        )[0]
        == "error"
    )
    assert (
        registry.verify_group_endpoint(
            PLATFORM,
            request_id,
            code,
            "owner",
            "123456",
            "owner",
        )[0]
        == "ok"
    )
    assert (
        registry.verify_group_endpoint(
            PLATFORM,
            request_id,
            code,
            "owner",
            "123456",
            "owner",
        )[0]
        == "error"
    )
    assert registry.get_scoped(PLATFORM, "owner", "group").path == record.path


def test_verify_constructs_target_umo_from_pending_platform_and_verified_group(
    tmp_path,
):
    registry = EndpointRegistry(tmp_path)
    _, request_id, code = registry.create_pending_verification(
        PLATFORM, "owner", "group", "123456"
    )
    assert (
        registry.verify_group_endpoint(
            PLATFORM, request_id, code, "owner", "123456", "admin"
        )[0]
        == "ok"
    )
    record = registry.get_scoped(PLATFORM, "owner", "group")
    assert record is not None
    assert record.targets[0].umo == "aiocqhttp:GroupMessage:123456"
    assert (
        "group_target_umo"
        not in inspect.signature(registry.verify_group_endpoint).parameters
    )


@pytest.mark.parametrize(
    ("supplied", "mutation", "error_code"),
    [
        ("wrong", None, "invalid_token"),
        ("valid", "revoke", "endpoint_revoked"),
        ("valid", "expire", "endpoint_disabled"),
        ("valid", "unclaim", "token_unclaimed"),
    ],
)
def test_authentication_failure_modes(tmp_path, supplied, mutation, error_code):
    registry = EndpointRegistry(tmp_path)
    record, valid_token = registry.create_private_endpoint(
        PLATFORM, "owner", "auth", "aiocqhttp:FriendMessage:1"
    )
    if mutation == "revoke":
        registry.revoke_endpoint(PLATFORM, "owner", "auth")
    elif mutation in {"expire", "unclaim"}:
        current = registry._records[next(iter(registry._records))]
        current.status = "expired" if mutation == "expire" else "active"
        if mutation == "unclaim":
            current.token_hash = ""
    result = registry.authenticate_delivery(
        record.path, f"Bearer {valid_token if supplied == 'valid' else supplied}"
    )
    assert result.authorized is False
    assert result.error_code == error_code


def test_authentication_not_found(tmp_path):
    result = EndpointRegistry(tmp_path).authenticate_delivery("missing", "Bearer token")
    assert result.error_code == "not_found"


@pytest.mark.parametrize(
    ("state", "authorization", "error_code"),
    [
        ("not-found", None, "not_found"),
        ("not-found", "Basic bad", "not_found"),
        ("revoked", None, "endpoint_revoked"),
        ("revoked", "Basic bad", "endpoint_revoked"),
        ("tokenless", None, "token_unclaimed"),
        ("tokenless", "Basic bad", "token_unclaimed"),
        ("active", None, "missing_authorization"),
        ("active", "Basic bad", "invalid_token"),
    ],
)
def test_authentication_preserves_path_status_tokenless_priority(
    tmp_path, state, authorization, error_code
):
    registry = EndpointRegistry(tmp_path)
    record, _ = registry.create_private_endpoint(
        PLATFORM, "owner", "priority", "aiocqhttp:FriendMessage:1"
    )
    path = record.path
    if state == "not-found":
        path = "missing"
    elif state == "revoked":
        registry.revoke_endpoint(PLATFORM, "owner", "priority")
    elif state == "tokenless":
        registry._records[next(iter(registry._records))].token_hash = ""

    result = registry.authenticate_delivery(path, authorization)

    assert result.authorized is False
    assert result.error_code == error_code


@pytest.mark.parametrize("operation", ["get", "list", "rotate", "revoke"])
def test_managed_operations_do_not_cross_platform(tmp_path, operation):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "scoped", "aiocqhttp:FriendMessage:1"
    )
    if operation == "get":
        assert registry.get_scoped("qq_official", "owner", "scoped") is None
    elif operation == "list":
        assert registry.list_visible_by_owner("qq_official", "owner") == []
    elif operation == "rotate":
        assert registry.rotate_token("qq_official", "owner", "scoped")[0] is False
    else:
        assert registry.revoke_endpoint("qq_official", "owner", "scoped")[0] is False
    assert registry.get_scoped(PLATFORM, "owner", "scoped").status == "active"


def test_v2_persistence_shape_and_keys_are_unambiguous(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner\x1fwith-separator", "name", "aiocqhttp:FriendMessage:1"
    )
    _, request_id, _ = registry.create_pending_verification(
        PLATFORM, "owner", "pending", "123"
    )
    saved = json.loads((tmp_path / REGISTRY_FILENAME).read_text())
    record_key = next(key for key in saved["records"] if "with-separator" in key)
    pending_key = next(iter(saved["pending"]))
    assert saved.keys() == {"version", "records", "quarantine", "pending"}
    assert json.loads(record_key) == [PLATFORM, "owner\x1fwith-separator", "name"]
    assert json.loads(pending_key) == [PLATFORM, request_id]


@pytest.mark.parametrize(
    ("code", "owner", "group", "message"),
    [
        ("000000", "owner", "123", "验证码不匹配"),
        (None, "attacker", "123", "执行者不是申请者"),
        (None, "owner", "999", "当前群不是申请目标群"),
    ],
)
def test_pending_verification_rejects_wrong_binding(
    tmp_path, code, owner, group, message
):
    registry = EndpointRegistry(tmp_path)
    _, request_id, valid_code = registry.create_pending_verification(
        PLATFORM, "owner", "pending", "123"
    )
    result = registry.verify_group_endpoint(
        PLATFORM,
        request_id,
        code or valid_code,
        owner,
        group,
        "owner",
    )
    assert result[0] == "error" and message in result[1]
    assert registry.get_pending_verification(PLATFORM, request_id) is not None


@pytest.mark.parametrize("expires_at", ["not-a-date", "2020-01-01T00:00:00+00:00"])
def test_expire_stale_pending_is_atomic(tmp_path, expires_at):
    registry = EndpointRegistry(tmp_path)
    _, request_id, _ = registry.create_pending_verification(
        PLATFORM, "owner", "pending", "123"
    )
    key = next(iter(registry._pending))
    registry._pending[key]["expires_at"] = expires_at
    assert registry.expire_stale_pending() == 1
    assert registry.get_pending_verification(PLATFORM, request_id) is None
    assert registry.get_scoped(PLATFORM, "owner", "pending").status == "expired"
    assert (
        EndpointRegistry(tmp_path).get_scoped(PLATFORM, "owner", "pending").status
        == "expired"
    )


@pytest.mark.parametrize("failure", ["write", "replace", "candidate_fsync"])
def test_migration_candidate_failure_keeps_legacy_disk(tmp_path, monkeypatch, failure):
    original = _write_legacy(tmp_path, {"owner\x1flegacy": _legacy_record()})
    if failure == "write":
        monkeypatch.setattr(
            EndpointRegistry,
            "_atomic_write_bytes",
            lambda *_: (_ for _ in ()).throw(
                RegistryPersistenceError("injected write failure")
            ),
        )
    elif failure == "replace":
        monkeypatch.setattr(
            os,
            "replace",
            lambda *_: (_ for _ in ()).throw(OSError("injected replace failure")),
        )
    else:
        (tmp_path / f"{REGISTRY_FILENAME}.v1.bak").write_bytes(original)
        monkeypatch.setattr(
            os,
            "fsync",
            lambda *_: (_ for _ in ()).throw(
                OSError("injected candidate fsync failure")
            ),
        )
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)
    assert (tmp_path / REGISTRY_FILENAME).read_bytes() == original


def test_rotate_revoked_and_pending_records_is_rejected(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "revoked", "aiocqhttp:FriendMessage:1"
    )
    registry.revoke_endpoint(PLATFORM, "owner", "revoked")
    registry.create_pending_verification(PLATFORM, "owner", "pending", "123")
    assert registry.rotate_token(PLATFORM, "owner", "revoked")[0] is False
    assert registry.rotate_token(PLATFORM, "owner", "pending")[0] is False


def test_admin_list_excludes_quarantine(tmp_path):
    seed = EndpointRegistry(tmp_path)
    token_hash = hash_token(seed.server_secret, "legacy")
    _write_legacy(
        tmp_path,
        {"legacy": _legacy_record(targets=[], token_hash=token_hash)},
    )
    assert EndpointRegistry(tmp_path).list_all_for_admin() == []


def test_revoke_by_path_is_idempotent(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, _ = registry.create_private_endpoint(
        PLATFORM, "owner", "name", "aiocqhttp:FriendMessage:1"
    )
    assert registry.revoke_endpoint_by_path(record.path)[0] is True
    success, message = registry.revoke_endpoint_by_path(record.path)
    assert success is True and "无需重复" in message


def test_owner_name_apis_require_explicit_platform_scope():
    for method_name in (
        "get_scoped",
        "get_by_owner_name",
        "list_by_owner",
        "list_visible_by_owner",
        "rotate_token",
        "revoke_endpoint",
        "delete_endpoint",
        "revoke_endpoint_by_owner_name",
    ):
        parameters = inspect.signature(
            getattr(EndpointRegistry, method_name)
        ).parameters
        platform = parameters["owner_platform_id"]
        assert platform.default is inspect.Parameter.empty


def test_count_deliverable_excludes_tokenless_active_until_rotate(tmp_path):
    registry = EndpointRegistry(tmp_path)
    _, request_id, code = registry.create_pending_verification(
        PLATFORM, "owner", "pending", "123"
    )
    registry.verify_group_endpoint(
        PLATFORM,
        request_id,
        code,
        "owner",
        "123",
        "owner",
    )
    assert registry.count_active() == 1
    assert registry.count_deliverable() == 0
    assert registry.rotate_token(PLATFORM, "owner", "pending")[0] is True
    assert registry.count_deliverable() == 1


def test_native_v2_reload_never_creates_migration_backup(tmp_path):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "native", "aiocqhttp:FriendMessage:1"
    )
    EndpointRegistry(tmp_path)
    assert not (tmp_path / f"{REGISTRY_FILENAME}.v1.bak").exists()


def test_v1_orphan_and_mismatched_pending_records_always_expire_on_reload(tmp_path):
    _write_legacy(
        tmp_path,
        {
            "orphan": _legacy_record(
                name="orphan",
                path="legacy/orphan",
                status="pending_verification",
                pending_request_id="absent",
            ),
            "mismatch": _legacy_record(
                name="mismatch",
                owner="other",
                path="legacy/mismatch",
                status="pending_verification",
                pending_request_id="request-present",
            ),
            "active-junk": _legacy_record(
                name="active-junk",
                owner="third",
                path="legacy/active-junk",
                status="active",
                pending_request_id="stale-on-active",
            ),
        },
        {"request-present": {"request_id": "wrong", "owner_user_id": "attacker"}},
    )
    migrated = EndpointRegistry(tmp_path)
    reloaded = EndpointRegistry(tmp_path)

    for registry, owner, name in (
        (migrated, "owner", "orphan"),
        (reloaded, "other", "mismatch"),
    ):
        record = registry.get_scoped(PLATFORM, owner, name)
        assert record is not None
        assert record.status == EndpointStatus.EXPIRED.value
        assert record.pending_request_id is None
        assert record.pending_code is None
        assert record.pending_expires_at is None
    assert json.loads((tmp_path / REGISTRY_FILENAME).read_text())["pending"] == {}
    active = reloaded.get_scoped(PLATFORM, "third", "active-junk")
    assert active is not None and active.status == EndpointStatus.ACTIVE.value
    assert active.pending_request_id is None
    assert active.pending_code is None
    assert active.pending_expires_at is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["records"].__setitem__(
            '[ "aiocqhttp", "owner", "native" ]',
            raw["records"].pop('["aiocqhttp","owner","native"]'),
        ),
        lambda raw: next(iter(raw["records"].values())).__setitem__("name", "other"),
        lambda raw: next(iter(raw["records"].values())).__setitem__(
            "owner_platform_id", ""
        ),
    ],
)
def test_native_v2_noncanonical_mismatch_and_empty_scope_fail_closed(tmp_path, mutate):
    registry = EndpointRegistry(tmp_path)
    registry.create_private_endpoint(
        PLATFORM, "owner", "native", "aiocqhttp:FriendMessage:1"
    )
    path = tmp_path / REGISTRY_FILENAME
    raw = json.loads(path.read_text())
    mutate(raw)
    path.write_text(json.dumps(raw))
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)


def test_native_v2_dangling_pending_and_quarantine_mismatch_fail_closed(tmp_path):
    registry = EndpointRegistry(tmp_path)
    _, request_id, _ = registry.create_pending_verification(
        PLATFORM, "owner", "pending", "123"
    )
    path = tmp_path / REGISTRY_FILENAME
    raw = json.loads(path.read_text())
    raw["records"].pop(next(iter(raw["records"])))
    path.write_text(json.dumps(raw))
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)

    other = tmp_path / "quarantine"
    other.mkdir()
    _write_legacy(other, {"legacy-key": _legacy_record(targets=[])})
    EndpointRegistry(other)
    qpath = other / REGISTRY_FILENAME
    qraw = json.loads(qpath.read_text())
    qraw["quarantine"]["wrong-key"] = qraw["quarantine"].pop("legacy-key")
    qpath.write_text(json.dumps(qraw))
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(other)
    assert request_id


def test_mutation_candidate_rejects_empty_scope_without_writing(tmp_path):
    registry = EndpointRegistry(tmp_path)
    with pytest.raises(RegistryLoadError):
        registry.create_private_endpoint(
            "", "owner", "name", "aiocqhttp:FriendMessage:1"
        )
    assert not (tmp_path / REGISTRY_FILENAME).exists()


def test_migration_preserves_legacy_path_bytes_and_backup_is_private(tmp_path):
    literal = "legacy//case-sensitive/path"
    original = _write_legacy(tmp_path, {"legacy": _legacy_record(path=literal)})
    EndpointRegistry(tmp_path)
    reloaded = EndpointRegistry(tmp_path)
    record = reloaded.get_scoped(PLATFORM, "owner", "legacy")
    assert record is not None and record.path == literal
    backup = tmp_path / f"{REGISTRY_FILENAME}.v1.bak"
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600


def test_migration_registry_parent_fsync_failure_restores_original(
    tmp_path, monkeypatch
):
    original = _write_legacy(tmp_path, {"legacy": _legacy_record()})
    real = EndpointRegistry._fsync_parent_required
    calls = 0

    def fail_registry_parent(self, label):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RegistryPersistenceError("injected registry parent fsync failure")
        return real(self, label)

    monkeypatch.setattr(
        EndpointRegistry, "_fsync_parent_required", fail_registry_parent
    )
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)
    assert (tmp_path / REGISTRY_FILENAME).read_bytes() == original


def test_v1_leading_slash_path_fails_closed_without_rewriting(tmp_path):
    original = _write_legacy(tmp_path, {"legacy": _legacy_record(path="/legacy/path")})
    with pytest.raises(RegistryLoadError):
        EndpointRegistry(tmp_path)
    assert (tmp_path / REGISTRY_FILENAME).read_bytes() == original


def test_six_record_production_shaped_v1_migration_reload(tmp_path):
    seed = EndpointRegistry(tmp_path)
    active_token = "fixture-active-token"
    quarantine_token = "fixture-quarantine-token"
    records = {
        "owner-a-active": _legacy_record(
            name="active",
            owner="owner-a",
            path="legacy/active",
            token_hash=hash_token(seed.server_secret, active_token),
        ),
        "owner-a-revoked": _legacy_record(
            name="revoked", owner="owner-a", path="legacy/revoked", status="revoked"
        ),
        "owner-b-pending": _legacy_record(
            name="pending",
            owner="owner-b",
            path="legacy/pending",
            status="pending_verification",
            pending_request_id="orphan-request",
        ),
        "owner-b-group": _legacy_record(
            name="group",
            owner="owner-b",
            path="legacy/group",
            targets=[{"name": "group", "umo": "aiocqhttp:GroupMessage:20002"}],
        ),
        "mixed-targets": _legacy_record(
            name="mixed",
            owner="owner-c",
            path="legacy/mixed",
            targets=[
                {"name": "a", "umo": "aiocqhttp:FriendMessage:1"},
                {"name": "b", "umo": "qq_official:FriendMessage:2"},
            ],
        ),
        "empty-targets": _legacy_record(
            name="empty",
            owner="owner-d",
            path="legacy/empty",
            targets=[],
            token_hash=hash_token(seed.server_secret, quarantine_token),
        ),
    }
    _write_legacy(
        tmp_path,
        records,
        {"mismatched": {"request_id": "mismatched", "owner_user_id": "nobody"}},
    )
    migrated = EndpointRegistry(tmp_path)
    reloaded = EndpointRegistry(tmp_path)

    assert len(reloaded.list_all_for_admin()) == 4
    assert reloaded.get_scoped(PLATFORM, "owner-b", "pending").status == "expired"
    assert json.loads((tmp_path / REGISTRY_FILENAME).read_text())["pending"] == {}
    assert migrated.authenticate_delivery(
        "legacy/active", f"Bearer {active_token}"
    ).authorized
    assert reloaded.authenticate_delivery(
        "legacy/empty", f"Bearer {quarantine_token}"
    ).authorized
    assert reloaded.get_scoped(PLATFORM, "owner-c", "mixed") is None
    assert reloaded.get_scoped(PLATFORM, "owner-d", "empty") is None
