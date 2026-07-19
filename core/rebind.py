from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import EndpointRecord, EndpointStatus, TargetAlias
from .registry import EndpointRegistry, REGISTRY_VERSION, _record_key


class RebindError(RuntimeError):
    """离线 platform_id rebind 失败。"""


@dataclass(frozen=True)
class RebindPlan:
    source_platform_id: str
    destination_platform_id: str
    selected_count: int
    total_managed_count: int
    pending_expired_count: int
    pre_sha256: str
    post_sha256: str
    selected_post_key_fingerprints: tuple[str, ...]

    def to_audit_summary(self) -> dict[str, Any]:
        return {
            "source_platform_id": self.source_platform_id,
            "destination_platform_id": self.destination_platform_id,
            "selected_count": self.selected_count,
            "total_managed_count": self.total_managed_count,
            "pending_expired_count": self.pending_expired_count,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "selected_post_key_fingerprints": list(self.selected_post_key_fingerprints),
        }


def _digest(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _key_fingerprint(key: str) -> str:
    return sha256(key.encode("utf-8")).hexdigest()


def _load_canonical(
    registry_path: Path,
) -> tuple[
    bytes,
    EndpointRegistry,
    dict[str, EndpointRecord],
    dict[str, EndpointRecord],
    dict[str, dict[str, Any]],
]:
    try:
        original = registry_path.read_bytes()
    except OSError as exc:
        raise RebindError("无法读取 Registry") from exc
    try:
        raw = json.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RebindError("Registry JSON 非法") from exc
    if not isinstance(raw, dict) or raw.get("version") != REGISTRY_VERSION:
        raise RebindError("仅支持 canonical Registry v2")
    registry = EndpointRegistry.__new__(EndpointRegistry)
    try:
        records, quarantine, pending = registry._parse_v2(raw)
        registry._validate_snapshot(records, quarantine, pending)
        canonical = registry._serialize(records, quarantine, pending)
    except Exception as exc:
        raise RebindError("Registry canonical 校验失败") from exc
    if original != canonical:
        raise RebindError("Registry 不是 canonical v2 序列化格式")
    return original, registry, records, quarantine, pending


def _validate_ids(
    source_platform_id: str,
    destination_platform_id: str,
    owner_user_id: str | None,
) -> None:
    if not source_platform_id or not destination_platform_id:
        raise RebindError("source/destination platform_id 不能为空")
    if source_platform_id == destination_platform_id:
        raise RebindError("source/destination platform_id 必须不同")
    if owner_user_id is not None and not owner_user_id:
        raise RebindError("owner selector 不能为空")


def _replace_target_platform(
    target: TargetAlias, expected: str, replacement: str
) -> None:
    parts = target.umo.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise RebindError("选中 record 包含不可解析的 target UMO")
    if parts[0] != expected:
        raise RebindError("选中 record 的 target UMO platform 前缀不匹配")
    target.umo = f"{replacement}:{parts[1]}:{parts[2]}"


def _expire_all_pending(
    records: dict[str, EndpointRecord],
    quarantine: dict[str, EndpointRecord],
    pending: dict[str, dict[str, Any]],
) -> int:
    count = 0
    for record in (*records.values(), *quarantine.values()):
        if record.status == EndpointStatus.PENDING_VERIFICATION.value:
            count += 1
            record.status = EndpointStatus.EXPIRED.value
        record.pending_request_id = None
        record.pending_code = None
        record.pending_expires_at = None
    pending.clear()
    return count


def _build_candidate(
    registry_path: Path,
    source_platform_id: str,
    destination_platform_id: str,
    owner_user_id: str | None,
) -> tuple[RebindPlan, bytes, bytes]:
    _validate_ids(source_platform_id, destination_platform_id, owner_user_id)
    original, registry, records, quarantine, pending = _load_canonical(registry_path)
    selected = [
        (key, record)
        for key, record in records.items()
        if record.owner_platform_id == source_platform_id
        and (owner_user_id is None or record.owner_user_id == owner_user_id)
    ]
    if not selected:
        raise RebindError("选择范围为空；未找到可 rebind 的 managed record")

    selected_keys = {key for key, _ in selected}
    post_keys: list[str] = []
    for _, record in selected:
        post_key = _record_key(
            destination_platform_id, record.owner_user_id, record.name
        )
        if post_key in records and post_key not in selected_keys:
            raise RebindError("destination scope 存在 record key 冲突")
        post_keys.append(post_key)
        for target in record.targets:
            _replace_target_platform(
                target, source_platform_id, destination_platform_id
            )

    moved: dict[str, EndpointRecord] = {}
    for (old_key, record), post_key in zip(selected, post_keys, strict=True):
        del records[old_key]
        record.owner_platform_id = destination_platform_id
        moved[post_key] = record
    if len(moved) != len(selected):
        raise RebindError("destination scope 产生重复 record key")
    records.update(moved)
    pending_expired_count = _expire_all_pending(records, quarantine, pending)
    try:
        registry._validate_snapshot(records, quarantine, pending)
        candidate = registry._serialize(records, quarantine, pending)
    except Exception as exc:
        raise RebindError("rebind candidate 校验失败") from exc
    plan = RebindPlan(
        source_platform_id=source_platform_id,
        destination_platform_id=destination_platform_id,
        selected_count=len(selected),
        total_managed_count=len(records),
        pending_expired_count=pending_expired_count,
        pre_sha256=_digest(original),
        post_sha256=_digest(candidate),
        selected_post_key_fingerprints=tuple(
            sorted(_key_fingerprint(key) for key in post_keys)
        ),
    )
    return plan, original, candidate


def plan_rebind(
    registry_path: str | Path,
    source_platform_id: str,
    destination_platform_id: str,
    owner_user_id: str | None = None,
) -> RebindPlan:
    plan, _, _ = _build_candidate(
        Path(registry_path), source_platform_id, destination_platform_id, owner_user_id
    )
    return plan


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise RebindError("parent fsync 失败") from exc


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        _fsync_parent(path)
    except OSError as exc:
        raise RebindError("durable write 失败") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _unique_sidecar(registry_path: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return registry_path.with_name(
        f"{registry_path.name}.{label}.{stamp}.{uuid4().hex[:8]}.json"
    )


def _path_exists(path: Path, label: str) -> bool:
    try:
        return path.exists()
    except OSError as exc:
        raise RebindError(f"无法检查{label}") from exc


def _parent_is_dir(path: Path, label: str) -> bool:
    try:
        return path.parent.is_dir()
    except OSError as exc:
        raise RebindError(f"无法检查{label}父目录") from exc


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RebindError(f"无法读取{label}") from exc


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()


def _commit_registry(path: Path, payload: bytes) -> list[str]:
    """Commit registry bytes; a post-replace parent fsync failure is a warning."""
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except OSError as exc:
        raise RebindError("Registry 写入失败") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    try:
        _fsync_parent(path)
    except RebindError:
        return ["registry_parent_fsync_failed"]
    return []


def _write_backup(registry_path: Path, original: bytes, label: str) -> Path:
    backup_path = _unique_sidecar(registry_path, label)
    if _path_exists(backup_path, "backup"):
        raise RebindError("backup 已存在，拒绝覆盖")
    _atomic_write(backup_path, original)
    return backup_path


def execute_rebind(
    registry_path: str | Path,
    source_platform_id: str,
    destination_platform_id: str,
    owner_user_id: str | None = None,
    manifest_path: str | Path | None = None,
    *,
    confirm_offline: bool = False,
) -> dict[str, Any]:
    if not confirm_offline:
        raise RebindError("execute 必须显式确认 AstrBot 与插件已停止")
    path = Path(registry_path)
    target_manifest = (
        Path(manifest_path) if manifest_path else _unique_sidecar(path, "rebind-audit")
    )
    if _path_exists(target_manifest, "manifest"):
        raise RebindError("manifest 已存在，拒绝覆盖")
    if not _parent_is_dir(target_manifest, "manifest"):
        raise RebindError("manifest 父目录不存在")
    plan, validated_original, candidate = _build_candidate(
        path, source_platform_id, destination_platform_id, owner_user_id
    )
    original = _read_bytes(path, "Registry")
    if _digest(original) != plan.pre_sha256:
        raise RebindError("Registry 在校验后发生变化，拒绝执行")
    if original != validated_original:
        raise RebindError("Registry 在校验后发生变化，拒绝执行")
    backup_path = _write_backup(path, original, "rebind-backup")
    manifest = {
        "audit_version": 1,
        "operation": "rebind",
        "state": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **plan.to_audit_summary(),
        "backup_file": backup_path.name,
    }
    _atomic_write(target_manifest, _manifest_bytes(manifest))
    warnings = _commit_registry(path, candidate)
    return {
        **manifest,
        "changed": True,
        "manifest_file": target_manifest.name,
        "warnings": warnings,
    }


def rollback_rebind(
    registry_path: str | Path,
    manifest_path: str | Path,
    audit_path: str | Path | None = None,
    *,
    confirm_offline: bool = False,
) -> dict[str, Any]:
    if not confirm_offline:
        raise RebindError("rollback 必须显式确认 AstrBot 与插件已停止")
    path = Path(registry_path)
    target_audit = (
        Path(audit_path) if audit_path else _unique_sidecar(path, "rollback-audit")
    )
    if _path_exists(target_audit, "rollback manifest"):
        raise RebindError("rollback audit 已存在，拒绝覆盖")
    if not _parent_is_dir(target_audit, "rollback manifest"):
        raise RebindError("rollback audit 父目录不存在")
    source_manifest_path = Path(manifest_path)
    source_manifest_bytes = _read_bytes(source_manifest_path, "rebind manifest")
    try:
        manifest = json.loads(source_manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RebindError("无法读取有效 rebind manifest") from exc
    required = {
        "audit_version",
        "operation",
        "state",
        "source_platform_id",
        "destination_platform_id",
        "selected_count",
        "pre_sha256",
        "post_sha256",
        "selected_post_key_fingerprints",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise RebindError("rebind manifest schema 非法")
    if (
        manifest["audit_version"] != 1
        or manifest["operation"] != "rebind"
        or manifest["state"] != "prepared"
    ):
        raise RebindError("rebind manifest 类型不受支持")
    source = manifest["source_platform_id"]
    destination = manifest["destination_platform_id"]
    _validate_ids(source, destination, None)
    original, registry, records, quarantine, pending = _load_canonical(path)
    current_digest = _digest(original)
    if current_digest == manifest["pre_sha256"]:
        raise RebindError("rebind manifest 尚未提交，拒绝 rollback")
    if current_digest != manifest["post_sha256"]:
        raise RebindError("Registry digest 与 manifest post digest 不一致")
    fingerprints = manifest["selected_post_key_fingerprints"]
    if (
        not isinstance(fingerprints, list)
        or not all(isinstance(item, str) and len(item) == 64 for item in fingerprints)
        or len(fingerprints) != manifest["selected_count"]
        or len(set(fingerprints)) != len(fingerprints)
    ):
        raise RebindError("manifest record fingerprint 集合非法")
    fingerprint_set = set(fingerprints)
    selected = [
        (key, record)
        for key, record in records.items()
        if _key_fingerprint(key) in fingerprint_set
    ]
    if len(selected) != len(fingerprint_set):
        raise RebindError("当前 Registry record 集合与 manifest 不符")
    selected_keys = {key for key, _ in selected}
    source_keys: list[str] = []
    for _, record in selected:
        if record.owner_platform_id != destination:
            raise RebindError("manifest 定位 record 的 platform_id 不匹配")
        source_key = _record_key(source, record.owner_user_id, record.name)
        if source_key in records and source_key not in selected_keys:
            raise RebindError("rollback source scope 存在 record key 冲突")
        source_keys.append(source_key)
        for target in record.targets:
            _replace_target_platform(target, destination, source)
    moved: dict[str, EndpointRecord] = {}
    for (old_key, record), source_key in zip(selected, source_keys, strict=True):
        del records[old_key]
        record.owner_platform_id = source
        moved[source_key] = record
    if len(moved) != len(selected):
        raise RebindError("rollback source scope 产生重复 record key")
    records.update(moved)
    pending_expired_count = _expire_all_pending(records, quarantine, pending)
    try:
        registry._validate_snapshot(records, quarantine, pending)
        candidate = registry._serialize(records, quarantine, pending)
    except Exception as exc:
        raise RebindError("rollback candidate 校验失败") from exc
    current = _read_bytes(path, "Registry")
    if current != original or _digest(current) != manifest["post_sha256"]:
        raise RebindError("Registry 在 rollback 校验后发生变化，拒绝执行")
    candidate_digest = _digest(candidate)
    backup_path = _write_backup(path, original, "rollback-backup")
    rollback_manifest = {
        "audit_version": 1,
        "operation": "rollback",
        "state": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_platform_id": destination,
        "destination_platform_id": source,
        "selected_count": len(selected),
        "total_managed_count": len(records),
        "pending_expired_count": pending_expired_count,
        "pre_sha256": _digest(original),
        "post_sha256": candidate_digest,
        "backup_file": backup_path.name,
        "selected_post_key_fingerprints": sorted(
            _key_fingerprint(key) for key in source_keys
        ),
        "rebind_manifest_sha256": _digest(source_manifest_bytes),
    }
    _atomic_write(target_audit, _manifest_bytes(rollback_manifest))
    warnings = _commit_registry(path, candidate)
    return {
        **rollback_manifest,
        "changed": True,
        "manifest_file": target_audit.name,
        "warnings": warnings,
    }
