from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, TypeVar

from astrbot.api import logger

from .models import (
    DeliveryAuthentication,
    EndpointRecord,
    EndpointStatus,
    TargetAlias,
)
from .security import (
    TOKEN_HASH_ALGORITHM,
    generate_request_id,
    generate_token,
    generate_verification_code,
    hash_token,
    load_server_secret,
    verify_token,
)

REGISTRY_FILENAME = "webhook_tokens.json"
REGISTRY_VERSION = 2
PENDING_EXPIRY_SECONDS = 600
MANAGED = "managed"
QUARANTINED_LEGACY = "quarantined_legacy"
PREBOUND_GROUP = "prebound_group"
BIND_CURRENT_GROUP = "bind_current_group"
GROUP_BINDING_MODES = {PREBOUND_GROUP, BIND_CURRENT_GROUP}
AWAITING_GROUP_ADMIN = "awaiting_group_admin"
GROUP_VERIFIED_WAITING_OWNER = "group_verified_waiting_owner"
PENDING_PHASES = {AWAITING_GROUP_ADMIN, GROUP_VERIFIED_WAITING_OWNER}
VISIBLE_STATUSES = {
    EndpointStatus.ACTIVE.value,
    EndpointStatus.PENDING_VERIFICATION.value,
}


class RegistryError(RuntimeError):
    """Registry 基础错误。"""


class RegistryLoadError(RegistryError):
    """Registry 加载或迁移失败。"""


class RegistryPersistenceError(RegistryError):
    """Registry durable write 失败。"""


class RegistryConflictError(RegistryError):
    """创建操作违反唯一性约束。"""


def normalize_endpoint_name(name: str, fallback: str = "default") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip())
    normalized = normalized.strip("-._")
    return normalized or fallback


def owner_path_hash(owner_platform_id: str, owner_user_id: str) -> str:
    material = f"v2\0{owner_platform_id}\0{owner_user_id}".encode()
    return sha256(material).hexdigest()[:12]


def build_endpoint_path(
    owner_platform_id: str, owner_user_id: str, endpoint_name: str
) -> str:
    return f"u/{owner_path_hash(owner_platform_id, owner_user_id)}/{endpoint_name}"


def _record_key(platform_id: str, owner_user_id: str, name: str) -> str:
    return json.dumps(
        [platform_id, owner_user_id, name], ensure_ascii=False, separators=(",", ":")
    )


def _pending_key(platform_id: str, request_id: str) -> str:
    return json.dumps(
        [platform_id, request_id], ensure_ascii=False, separators=(",", ":")
    )


def _decode_key(key: str, length: int, label: str) -> list[str]:
    try:
        value = json.loads(key)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RegistryLoadError(f"非法 {label} key") from exc
    if (
        not isinstance(value, list)
        or len(value) != length
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise RegistryLoadError(f"非法 {label} key")
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if key != canonical:
        raise RegistryLoadError(f"非 canonical {label} key")
    return value


def _json_no_duplicates(data: bytes) -> Any:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RegistryLoadError(f"JSON 存在重复 key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryLoadError(f"Registry JSON 非法: {exc}") from exc


def _platform_from_targets(targets: list[TargetAlias]) -> str | None:
    if not targets:
        return None
    platforms: set[str] = set()
    for target in targets:
        if not all(
            isinstance(value, str) and value for value in (target.name, target.umo)
        ):
            return None
        parts = target.umo.split(":")
        if len(parts) < 3 or any(not part for part in parts[:3]):
            return None
        platforms.add(parts[0])
    return next(iter(platforms)) if len(platforms) == 1 else None


T = TypeVar("T")


class EndpointRegistry:
    """带 durable transaction、v1 安全迁移和 quarantine 的 Registry v2。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._registry_path = self._data_dir / REGISTRY_FILENAME
        self._backup_path = self._data_dir / f"{REGISTRY_FILENAME}.v1.bak"
        self._server_secret = load_server_secret(self._data_dir)
        self._lock = threading.RLock()
        self._records: dict[str, EndpointRecord] = {}
        self._quarantine: dict[str, EndpointRecord] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def server_secret(self) -> str:
        return self._server_secret

    def _load(self) -> None:
        with self._lock:
            if not self._registry_path.exists():
                return
            try:
                original = self._registry_path.read_bytes()
                raw = _json_no_duplicates(original)
                if not isinstance(raw, dict):
                    raise RegistryLoadError("Registry 顶层必须是 JSON object")
                if "version" not in raw:
                    records, quarantine, pending = self._migrate_v1(raw)
                    self._validate_snapshot(records, quarantine, pending)
                    payload = self._serialize(records, quarantine, pending)
                    self._write_backup(original)
                    self._atomic_write_bytes(payload, rollback_bytes=original)
                elif raw.get("version") == REGISTRY_VERSION:
                    records, quarantine, pending = self._parse_v2(raw)
                    self._validate_snapshot(records, quarantine, pending)
                else:
                    raise RegistryLoadError(
                        f"不支持的 Registry version: {raw.get('version')!r}"
                    )
            except RegistryLoadError:
                raise
            except RegistryPersistenceError as exc:
                raise RegistryLoadError(f"Registry 迁移持久化失败: {exc}") from exc
            except (OSError, TypeError, ValueError, KeyError) as exc:
                raise RegistryLoadError(f"Registry 加载失败: {exc}") from exc
            self._records = records
            self._quarantine = quarantine
            self._pending = pending
            logger.info(
                "[WebhookNotifier] 已加载 "
                f"{len(records)} 个 managed endpoint，{len(quarantine)} 个 quarantine"
            )

    def _migrate_v1(
        self, raw: dict[str, Any]
    ) -> tuple[dict[str, EndpointRecord], dict[str, EndpointRecord], dict[str, Any]]:
        if set(raw) != {"records", "pending"}:
            raise RegistryLoadError("v1 顶层字段非法")
        records_raw = raw.get("records", {})
        pending_raw = raw.get("pending", {})
        if not isinstance(records_raw, dict) or not isinstance(pending_raw, dict):
            raise RegistryLoadError("v1 records/pending 必须是 object")
        records: dict[str, EndpointRecord] = {}
        quarantine: dict[str, EndpointRecord] = {}
        for legacy_key, data in records_raw.items():
            if not isinstance(legacy_key, str) or not legacy_key:
                raise RegistryLoadError("v1 record key 非法")
            record = self._parse_record(data, legacy_key=legacy_key)
            platform_id = _platform_from_targets(record.targets)
            if record.status == EndpointStatus.PENDING_VERIFICATION.value:
                record.status = EndpointStatus.EXPIRED.value
            record.pending_request_id = None
            record.pending_code = None
            record.pending_expires_at = None
            record.legacy_record_key = legacy_key
            if platform_id:
                record.owner_platform_id = platform_id
                record.management_state = MANAGED
                key = _record_key(platform_id, record.owner_user_id, record.name)
                if key in records:
                    raise RegistryLoadError("v1 迁移产生重复 managed key")
                records[key] = record
            else:
                record.owner_platform_id = ""
                record.management_state = QUARANTINED_LEGACY
                quarantine[legacy_key] = record
        return records, quarantine, {}

    def _parse_v2(
        self, raw: dict[str, Any]
    ) -> tuple[dict[str, EndpointRecord], dict[str, EndpointRecord], dict[str, Any]]:
        if set(raw) != {"version", "records", "quarantine", "pending"}:
            raise RegistryLoadError("v2 顶层字段非法")
        records_raw = raw.get("records")
        quarantine_raw = raw.get("quarantine")
        pending_raw = raw.get("pending")
        if not all(
            isinstance(value, dict)
            for value in (records_raw, quarantine_raw, pending_raw)
        ):
            raise RegistryLoadError("v2 records/quarantine/pending 必须是 object")
        records: dict[str, EndpointRecord] = {}
        quarantine: dict[str, EndpointRecord] = {}
        for key, data in records_raw.items():
            platform_id, owner_id, name = _decode_key(key, 3, "record")
            record = self._parse_record(data)
            if (
                record.owner_platform_id != platform_id
                or record.owner_user_id != owner_id
                or record.name != name
                or record.management_state != MANAGED
            ):
                raise RegistryLoadError("managed key 与 record 字段不一致")
            records[key] = record
        for key, data in quarantine_raw.items():
            if not isinstance(key, str) or not key:
                raise RegistryLoadError("非法 quarantine key")
            record = self._parse_record(data)
            if record.management_state != QUARANTINED_LEGACY:
                raise RegistryLoadError("quarantine management_state 非法")
            quarantine[key] = record
        pending: dict[str, dict[str, Any]] = {}
        for key, info in pending_raw.items():
            platform_id, request_id = _decode_key(key, 2, "pending")
            if not isinstance(info, dict):
                raise RegistryLoadError("pending value 必须是 object")
            if (
                info.get("owner_platform_id") != platform_id
                or info.get("request_id") != request_id
            ):
                raise RegistryLoadError("pending key 与字段不一致")
            pending[key] = deepcopy(info)
        return records, quarantine, pending

    def _parse_record(self, data: Any, legacy_key: str | None = None) -> EndpointRecord:
        if not isinstance(data, dict):
            raise RegistryLoadError("record 必须是 object")
        targets_raw = data.get("targets", [])
        if not isinstance(targets_raw, list):
            raise RegistryLoadError("record targets 必须是 array")
        targets: list[TargetAlias] = []
        for target in targets_raw:
            if not isinstance(target, dict):
                raise RegistryLoadError("target 格式非法")
            targets.append(TargetAlias(name=target["name"], umo=target["umo"]))
        return EndpointRecord(
            name=data.get("name"),
            path=data.get("path"),
            provider=data.get("provider", "omp"),
            token_hash=data.get("token_hash", ""),
            token_hash_algorithm=data.get("token_hash_algorithm", TOKEN_HASH_ALGORITHM),
            owner_user_id=data.get("owner_user_id"),
            targets=targets,
            status=data.get("status", EndpointStatus.REVOKED.value),
            created_at=data.get("created_at", ""),
            revoked_at=data.get("revoked_at"),
            pending_request_id=data.get("pending_request_id"),
            pending_code=data.get("pending_code"),
            pending_expires_at=data.get("pending_expires_at"),
            description=data.get("description"),
            owner_platform_id=data.get("owner_platform_id", ""),
            management_state=data.get("management_state", MANAGED),
            legacy_record_key=data.get("legacy_record_key", legacy_key),
        )

    def _validate_snapshot(
        self,
        records: dict[str, EndpointRecord],
        quarantine: dict[str, EndpointRecord],
        pending: dict[str, dict[str, Any]],
    ) -> None:
        paths: set[str] = set()
        for key, record in records.items():
            self._validate_record(key, record, MANAGED)
            if record.path in paths:
                raise RegistryLoadError(f"重复 endpoint path: {record.path}")
            paths.add(record.path)
        for key, record in quarantine.items():
            self._validate_record(key, record, QUARANTINED_LEGACY)
            if record.status == EndpointStatus.PENDING_VERIFICATION.value:
                raise RegistryLoadError(
                    "quarantine record 不得处于 pending_verification"
                )
            if record.path in paths:
                raise RegistryLoadError(f"managed/quarantine path 冲突: {record.path}")
            paths.add(record.path)
        pending_records: set[str] = set()
        for key, info in pending.items():
            platform_id, request_id = _decode_key(key, 2, "pending")
            if (
                set(info)
                != {
                    "owner_platform_id",
                    "endpoint_name",
                    "request_id",
                    "code_hash",
                    "owner_user_id",
                    "group_binding_mode",
                    "phase",
                    "target_group_id",
                    "verified_group_id",
                    "expires_at",
                }
                or info.get("owner_platform_id") != platform_id
                or info.get("request_id") != request_id
            ):
                raise RegistryLoadError("pending 不变量失败")
            required_pending_strings = (
                "owner_platform_id",
                "endpoint_name",
                "request_id",
                "owner_user_id",
                "group_binding_mode",
                "phase",
                "expires_at",
            )
            if not all(
                isinstance(info.get(field), str) and info[field]
                for field in required_pending_strings
            ):
                raise RegistryLoadError("pending 字段必须是非空字符串")
            binding_mode = info["group_binding_mode"]
            phase = info["phase"]
            code_hash = info["code_hash"]
            target_group_id = info["target_group_id"]
            verified_group_id = info["verified_group_id"]
            if binding_mode not in GROUP_BINDING_MODES:
                raise RegistryLoadError("pending group_binding_mode 非法")
            if phase not in PENDING_PHASES:
                raise RegistryLoadError("pending phase 非法")
            if not isinstance(code_hash, str):
                raise RegistryLoadError("pending code_hash 必须是字符串")
            if binding_mode == PREBOUND_GROUP:
                if not isinstance(target_group_id, str) or not target_group_id:
                    raise RegistryLoadError("prebound_group 必须包含 target_group_id")
                if (
                    phase != AWAITING_GROUP_ADMIN
                    or verified_group_id is not None
                    or not code_hash
                ):
                    raise RegistryLoadError("prebound_group phase 组合非法")
            elif target_group_id is not None:
                raise RegistryLoadError("bind_current_group 不得预设 target_group_id")
            elif phase == AWAITING_GROUP_ADMIN:
                if verified_group_id is not None or not code_hash:
                    raise RegistryLoadError("awaiting_group_admin 组合非法")
            elif (
                not isinstance(verified_group_id, str)
                or not verified_group_id
                or code_hash != ""
            ):
                raise RegistryLoadError("group_verified_waiting_owner 组合非法")
            record_key = _record_key(
                platform_id, info["owner_user_id"], info["endpoint_name"]
            )
            record = records.get(record_key)
            if (
                record is None
                or record.status != EndpointStatus.PENDING_VERIFICATION.value
                or record.pending_request_id != request_id
                or record.owner_platform_id != platform_id
                or record.owner_user_id != info["owner_user_id"]
                or record.name != info["endpoint_name"]
                or record.pending_expires_at != info["expires_at"]
            ):
                raise RegistryLoadError("pending 未绑定对应 record")
            pending_records.add(record_key)
        for key, record in records.items():
            is_pending = record.status == EndpointStatus.PENDING_VERIFICATION.value
            if is_pending != (key in pending_records):
                raise RegistryLoadError("pending record 双向绑定失败")
            if not is_pending and any(
                value is not None
                for value in (
                    record.pending_request_id,
                    record.pending_code,
                    record.pending_expires_at,
                )
            ):
                raise RegistryLoadError("非 pending record 含 pending 字段")

    @staticmethod
    def _validate_record(
        key: str, record: EndpointRecord, expected_management_state: str
    ) -> None:
        required_strings = {
            "owner_user_id": record.owner_user_id,
            "name": record.name,
            "path": record.path,
            "provider": record.provider,
            "token_hash_algorithm": record.token_hash_algorithm,
            "created_at": record.created_at,
        }
        for field, value in required_strings.items():
            if not isinstance(value, str) or not value:
                raise RegistryLoadError(f"record {field} 必须是非空字符串")
        if not isinstance(record.token_hash, str):
            raise RegistryLoadError("record token_hash 必须是字符串")
        if record.token_hash_algorithm != TOKEN_HASH_ALGORITHM:
            raise RegistryLoadError("record token_hash_algorithm 非法")
        if not isinstance(record.owner_platform_id, str):
            raise RegistryLoadError("record owner_platform_id 必须是字符串")
        if not isinstance(record.management_state, str):
            raise RegistryLoadError("record management_state 必须是字符串")
        if record.management_state != expected_management_state:
            raise RegistryLoadError("record management_state 与分区不一致")
        if not isinstance(record.status, str) or record.status not in {
            status.value for status in EndpointStatus
        }:
            raise RegistryLoadError(f"非法 endpoint status: {record.status}")
        if record.path.startswith("/"):
            raise RegistryLoadError("endpoint path 含不受支持的前导斜杠")
        if not isinstance(record.targets, list):
            raise RegistryLoadError("record targets 必须是 array")
        for target in record.targets:
            if not isinstance(target, TargetAlias) or not all(
                isinstance(value, str) and value for value in (target.name, target.umo)
            ):
                raise RegistryLoadError("target name/umo 必须是非空字符串")
        if record.legacy_record_key is not None and (
            not isinstance(record.legacy_record_key, str)
            or not record.legacy_record_key
        ):
            raise RegistryLoadError("record legacy_record_key 非法")

        if expected_management_state == MANAGED:
            scope = (record.owner_platform_id, record.owner_user_id, record.name)
            if not all(isinstance(value, str) and value for value in scope):
                raise RegistryLoadError("managed record scope 不能为空")
            if key != _record_key(*scope):
                raise RegistryLoadError("managed record key 与 scope 不一致")
        elif (
            not isinstance(key, str)
            or not key
            or record.owner_platform_id != ""
            or record.legacy_record_key != key
        ):
            raise RegistryLoadError("quarantine record 不变量失败")

    def _serialize(
        self,
        records: dict[str, EndpointRecord],
        quarantine: dict[str, EndpointRecord],
        pending: dict[str, dict[str, Any]],
    ) -> bytes:
        def record_dict(record: EndpointRecord) -> dict[str, Any]:
            data = asdict(record)
            data["targets"] = [asdict(target) for target in record.targets]
            return data

        raw = {
            "version": REGISTRY_VERSION,
            "records": {key: record_dict(value) for key, value in records.items()},
            "quarantine": {
                key: record_dict(value) for key, value in quarantine.items()
            },
            "pending": deepcopy(pending),
        }
        return (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode()

    def _write_backup(self, original: bytes) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        if self._backup_path.exists():
            try:
                if self._backup_path.read_bytes() == original:
                    return
            except OSError as exc:
                raise RegistryLoadError(f"读取迁移备份失败: {exc}") from exc
            raise RegistryLoadError("迁移备份已存在且内容不匹配")
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self._data_dir, prefix=f".{REGISTRY_FILENAME}.v1.bak."
            )
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            if self._backup_path.exists():
                raise RegistryLoadError("迁移备份已存在，拒绝覆盖")
            os.replace(tmp_path, self._backup_path)
            tmp_path = None
            self._fsync_parent_required("迁移备份")
        except (OSError, RegistryPersistenceError) as exc:
            try:
                self._backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RegistryLoadError(f"写入迁移备份失败: {exc}") from exc
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _atomic_write_bytes(
        self,
        payload: bytes,
        rollback_bytes: bytes | None = None,
        *,
        parent_fsync_required: bool = True,
    ) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path: str | None = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._data_dir,
                prefix=f".{REGISTRY_FILENAME}.",
                delete=False,
            ) as handle:
                tmp_path = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._registry_path)
            replaced = True
            tmp_path = None
            if parent_fsync_required:
                self._fsync_parent_required("Registry")
            else:
                self._fsync_parent_best_effort("Registry")
        except OSError as exc:
            raise RegistryPersistenceError(f"Registry 持久化失败: {exc}") from exc
        except RegistryPersistenceError:
            if replaced and rollback_bytes is not None:
                self._restore_registry_bytes(rollback_bytes)
            raise
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _restore_registry_bytes(self, payload: bytes) -> None:
        """迁移 replace 后 parent fsync 失败时，尽力恢复原始 v1 文件。"""
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._data_dir,
                prefix=f".{REGISTRY_FILENAME}.rollback.",
                delete=False,
            ) as handle:
                tmp_path = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._registry_path)
            tmp_path = None
            self._fsync_parent_required("Registry rollback")
        except (OSError, RegistryPersistenceError) as exc:
            raise RegistryPersistenceError(f"Registry 迁移回滚失败: {exc}") from exc
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _fsync_parent_required(self, label: str) -> None:
        try:
            fd = os.open(self._data_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise RegistryPersistenceError(f"{label} parent fsync 失败: {exc}") from exc

    def _fsync_parent_best_effort(self, label: str) -> None:
        try:
            self._fsync_parent_required(label)
        except RegistryPersistenceError as exc:
            logger.warning(f"[WebhookNotifier] {exc}；replace 已成功，继续发布内存快照")

    def _transaction(
        self,
        mutate: Callable[
            [
                dict[str, EndpointRecord],
                dict[str, EndpointRecord],
                dict[str, dict[str, Any]],
            ],
            T,
        ],
    ) -> T:
        with self._lock:
            records = deepcopy(self._records)
            quarantine = deepcopy(self._quarantine)
            pending = deepcopy(self._pending)
            result = mutate(records, quarantine, pending)
            self._validate_snapshot(records, quarantine, pending)
            self._atomic_write_bytes(
                self._serialize(records, quarantine, pending),
                parent_fsync_required=False,
            )
            self._records = records
            self._quarantine = quarantine
            self._pending = pending
            return deepcopy(result)

    def create_private_endpoint(
        self,
        owner_platform_id: str,
        owner_user_id: str,
        name: str,
        target_umo: str,
        description: str | None = None,
        provider: str = "omp",
    ) -> tuple[EndpointRecord, str]:
        token_plain = generate_token()
        token_hash_value = hash_token(self._server_secret, token_plain)

        def mutate(records, quarantine, pending):
            key = _record_key(owner_platform_id, owner_user_id, name)
            path = build_endpoint_path(owner_platform_id, owner_user_id, name)
            if key in records:
                raise RegistryConflictError(
                    "owner/platform/name 对应的 endpoint 已存在"
                )
            if any(
                record.path == path
                for record in (*records.values(), *quarantine.values())
            ):
                raise RegistryConflictError("endpoint path 已存在")
            record = EndpointRecord(
                name=name,
                path=path,
                provider=provider,
                token_hash=token_hash_value,
                token_hash_algorithm=TOKEN_HASH_ALGORITHM,
                owner_user_id=owner_user_id,
                targets=[TargetAlias(name="default", umo=target_umo)],
                status=EndpointStatus.ACTIVE.value,
                created_at=datetime.now(timezone.utc).isoformat(),
                description=description or f"私聊 endpoint for {owner_user_id}",
                owner_platform_id=owner_platform_id,
            )
            records[key] = record
            return record

        record = self._transaction(mutate)
        return record, token_plain

    def create_group_pending(
        self,
        owner_platform_id: str,
        owner_user_id: str,
        name: str,
        group_binding_mode: str,
        target_group_id: str | None,
        description: str | None = None,
        provider: str = "omp",
    ) -> tuple[EndpointRecord, str, str]:
        request_id = generate_request_id()
        code = generate_verification_code()
        expires_at = datetime.fromtimestamp(
            time.time() + PENDING_EXPIRY_SECONDS, tz=timezone.utc
        )

        def mutate(records, quarantine, pending):
            key = _record_key(owner_platform_id, owner_user_id, name)
            path = build_endpoint_path(owner_platform_id, owner_user_id, name)
            if key in records:
                raise RegistryConflictError(
                    "owner/platform/name 对应的 endpoint 已存在"
                )
            if any(
                record.path == path
                for record in (*records.values(), *quarantine.values())
            ):
                raise RegistryConflictError("endpoint path 已存在")
            record = EndpointRecord(
                name=name,
                path=path,
                provider=provider,
                token_hash="",
                token_hash_algorithm=TOKEN_HASH_ALGORITHM,
                owner_user_id=owner_user_id,
                targets=[],
                status=EndpointStatus.PENDING_VERIFICATION.value,
                created_at=datetime.now(timezone.utc).isoformat(),
                pending_request_id=request_id,
                pending_expires_at=expires_at.isoformat(),
                description=description or f"群聊 endpoint for {owner_user_id}",
                owner_platform_id=owner_platform_id,
            )
            records[key] = record
            pending[_pending_key(owner_platform_id, request_id)] = {
                "owner_platform_id": owner_platform_id,
                "endpoint_name": name,
                "request_id": request_id,
                "code_hash": hash_token(
                    self._server_secret, f"pending:{request_id}:{code}"
                ),
                "owner_user_id": owner_user_id,
                "group_binding_mode": group_binding_mode,
                "phase": AWAITING_GROUP_ADMIN,
                "target_group_id": target_group_id,
                "verified_group_id": None,
                "expires_at": expires_at.isoformat(),
            }
            return record

        record = self._transaction(mutate)
        return record, request_id, code

    def create_pending_verification(
        self,
        owner_platform_id: str,
        owner_user_id: str,
        name: str,
        target_group_id: str,
        description: str | None = None,
    ) -> tuple[EndpointRecord, str, str]:
        """兼容 Phase 1 调用；新代码应使用 create_group_pending。"""
        return self.create_group_pending(
            owner_platform_id,
            owner_user_id,
            name,
            PREBOUND_GROUP,
            target_group_id,
            description,
        )

    def get_pending_verification(
        self, owner_platform_id: str, request_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            value = self._pending.get(_pending_key(owner_platform_id, request_id))
            return deepcopy(value)

    def get_pending_descriptor(
        self, owner_platform_id: str, request_id: str
    ) -> dict[str, Any] | None:
        return self.get_pending_verification(owner_platform_id, request_id)

    def verify_group_endpoint(
        self,
        owner_platform_id: str,
        request_id: str,
        code: str,
        stable_owner_user_id: str | None,
        group_id: str,
        verified_role: str,
    ) -> tuple[str, str, str | None]:
        result: tuple[str, str, str | None]

        def mutate(records, quarantine, pending):
            key = _pending_key(owner_platform_id, request_id)
            info = pending.get(key)
            if not info:
                return ("error", "验证请求不存在或已过期", None)
            record_key = _record_key(
                owner_platform_id,
                str(info.get("owner_user_id", "")),
                str(info.get("endpoint_name", "")),
            )
            record = records.get(record_key)
            if (
                not record
                or record.owner_platform_id != owner_platform_id
                or record.owner_user_id != info.get("owner_user_id")
                or record.name != info.get("endpoint_name")
                or record.status != EndpointStatus.PENDING_VERIFICATION.value
                or record.pending_request_id != request_id
                or record.pending_expires_at != info.get("expires_at")
            ):
                pending.pop(key, None)
                return ("error", "验证请求已失效", None)
            try:
                expires_at = datetime.fromisoformat(str(info.get("expires_at", "")))
                if expires_at.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                self._expire_pending_candidate(record, key, pending)
                return ("error", "验证请求数据无效，请重新申请", None)
            if datetime.now(timezone.utc) > expires_at:
                self._expire_pending_candidate(record, key, pending)
                return ("error", "验证请求已过期", None)
            if info.get("phase") != AWAITING_GROUP_ADMIN:
                if info.get("phase") == GROUP_VERIFIED_WAITING_OWNER:
                    return (
                        "waiting_owner",
                        "群管理员验证已完成，请由原申请者私聊 confirm",
                        None,
                    )
                return ("error", "验证请求数据无效，请重新申请", None)
            supplied = hash_token(self._server_secret, f"pending:{request_id}:{code}")
            if not hmac.compare_digest(str(info.get("code_hash", "")), supplied):
                return ("error", "验证码不匹配", None)
            if verified_role not in {"owner", "admin"}:
                return ("error", "权限不足：群聊 token 验证需要群主或群管理员", None)
            if not isinstance(group_id, str) or not group_id:
                return ("error", "验证失败：无法确定当前群", None)
            binding_mode = info.get("group_binding_mode")
            if binding_mode == PREBOUND_GROUP:
                if info.get("owner_user_id") != stable_owner_user_id:
                    return ("error", "验证失败：执行者不是申请者", None)
                if info.get("target_group_id") != group_id:
                    return ("error", "验证失败：当前群不是申请目标群", None)
            if (
                binding_mode == BIND_CURRENT_GROUP
                and info.get("target_group_id") is not None
            ):
                return ("error", "验证请求数据无效，请重新申请", None)
            if binding_mode not in GROUP_BINDING_MODES:
                return ("error", "验证请求数据无效，请重新申请", None)
            if binding_mode == BIND_CURRENT_GROUP:
                info["phase"] = GROUP_VERIFIED_WAITING_OWNER
                info["verified_group_id"] = group_id
                info["code_hash"] = ""
                return (
                    "waiting_owner",
                    "群管理员验证成功，等待原申请者私聊确认",
                    None,
                )
            record.status = EndpointStatus.ACTIVE.value
            record.targets = [
                TargetAlias(
                    name="default",
                    umo=f"{owner_platform_id}:GroupMessage:{group_id}",
                )
            ]
            record.token_hash = ""
            record.pending_request_id = None
            record.pending_code = None
            record.pending_expires_at = None
            pending.pop(key, None)
            return ("ok", "验证成功", None)

        result = self._transaction(mutate)
        return result

    def confirm_group_endpoint(
        self,
        owner_platform_id: str,
        request_id: str,
        owner_user_id: str,
    ) -> tuple[str, str, EndpointRecord | None, str | None]:
        def mutate(records, quarantine, pending):
            key = _pending_key(owner_platform_id, request_id)
            info = pending.get(key)
            if not info:
                return ("error", "验证请求不存在或已过期", None)
            record = records.get(
                _record_key(
                    owner_platform_id,
                    str(info.get("owner_user_id", "")),
                    str(info.get("endpoint_name", "")),
                )
            )
            if (
                not record
                or record.owner_platform_id != owner_platform_id
                or record.owner_user_id != info.get("owner_user_id")
                or record.name != info.get("endpoint_name")
                or record.status != EndpointStatus.PENDING_VERIFICATION.value
                or record.pending_request_id != request_id
                or record.pending_expires_at != info.get("expires_at")
            ):
                pending.pop(key, None)
                return ("error", "验证请求已失效", None)
            try:
                expires_at = datetime.fromisoformat(str(info.get("expires_at", "")))
                if expires_at.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                self._expire_pending_candidate(record, key, pending)
                return ("error", "验证请求数据无效，请重新申请", None)
            if datetime.now(timezone.utc) > expires_at:
                self._expire_pending_candidate(record, key, pending)
                return ("error", "验证请求已过期", None)
            if info.get("phase") != GROUP_VERIFIED_WAITING_OWNER:
                return ("error", "验证请求尚未完成群管理员验证", None)
            if info.get("owner_user_id") != owner_user_id:
                return ("error", "确认失败：当前用户不是原申请者", None)
            verified_group_id = info.get("verified_group_id")
            if (
                info.get("group_binding_mode") != BIND_CURRENT_GROUP
                or info.get("target_group_id") is not None
                or not isinstance(verified_group_id, str)
                or not verified_group_id
                or info.get("code_hash") != ""
            ):
                return ("error", "验证请求数据无效，请重新申请", None)
            token_plain = generate_token()
            token_hash_value = hash_token(self._server_secret, token_plain)
            record.status = EndpointStatus.ACTIVE.value
            record.targets = [
                TargetAlias(
                    name="default",
                    umo=f"{owner_platform_id}:GroupMessage:{verified_group_id}",
                )
            ]
            record.token_hash = token_hash_value
            record.pending_request_id = None
            record.pending_code = None
            record.pending_expires_at = None
            pending.pop(key, None)
            return ("ok", "确认成功", record, token_plain)

        result = self._transaction(mutate)
        if result[0] != "ok":
            status, message, record = result
            return status, message, record, None
        return result

    @staticmethod
    def _expire_pending_candidate(record, key, pending) -> None:
        if record and record.status == EndpointStatus.PENDING_VERIFICATION.value:
            record.status = EndpointStatus.EXPIRED.value
            record.pending_request_id = None
            record.pending_code = None
            record.pending_expires_at = None
        pending.pop(key, None)

    def get_scoped(
        self, owner_platform_id: str, owner_user_id: str, name: str
    ) -> EndpointRecord | None:
        with self._lock:
            return deepcopy(
                self._records.get(_record_key(owner_platform_id, owner_user_id, name))
            )

    def get_by_owner_name(
        self, owner_platform_id: str, owner_user_id: str, name: str
    ) -> EndpointRecord | None:
        return self.get_scoped(owner_platform_id, owner_user_id, name)

    def list_by_owner(
        self, owner_platform_id: str, owner_user_id: str
    ) -> list[EndpointRecord]:
        with self._lock:
            return deepcopy(
                [
                    record
                    for record in self._records.values()
                    if record.owner_platform_id == owner_platform_id
                    and record.owner_user_id == owner_user_id
                ]
            )

    def list_visible_by_owner(
        self, owner_platform_id: str, owner_user_id: str
    ) -> list[EndpointRecord]:
        return [
            record
            for record in self.list_by_owner(owner_platform_id, owner_user_id)
            if record.status in VISIBLE_STATUSES
        ]

    def list_all_for_admin(self) -> list[EndpointRecord]:
        with self._lock:
            return sorted(
                deepcopy(list(self._records.values())),
                key=lambda record: (
                    record.created_at,
                    record.owner_platform_id,
                    record.owner_user_id,
                    record.path,
                ),
            )

    def count_active(self) -> int:
        with self._lock:
            return sum(
                record.status == EndpointStatus.ACTIVE.value
                for record in self._records.values()
            )

    def count_deliverable(self) -> int:
        with self._lock:
            return sum(
                record.status == EndpointStatus.ACTIVE.value
                and record.revoked_at is None
                and bool(record.token_hash)
                for record in (*self._records.values(), *self._quarantine.values())
            )

    def rotate_token(
        self, owner_platform_id: str, owner_user_id: str, name: str
    ) -> tuple[bool, str]:
        token_plain = generate_token()
        token_hash_value = hash_token(self._server_secret, token_plain)

        def mutate(records, quarantine, pending):
            record = records.get(_record_key(owner_platform_id, owner_user_id, name))
            if not record:
                return (False, "endpoint 不存在")
            if record.status != EndpointStatus.ACTIVE.value:
                return (False, "只有 active 状态的 endpoint 可以轮换 token")
            record.token_hash = token_hash_value
            return (True, token_plain)

        return self._transaction(mutate)

    def revoke_endpoint(
        self, owner_platform_id: str, owner_user_id: str, name: str
    ) -> tuple[bool, str]:
        return self._revoke_managed(owner_platform_id, owner_user_id, name)

    def delete_endpoint(
        self, owner_platform_id: str, owner_user_id: str, name: str
    ) -> tuple[str, str | None]:
        """永久删除当前 scope 中的终态 managed endpoint。"""

        def mutate(records, quarantine, pending):
            key = _record_key(owner_platform_id, owner_user_id, name)
            record = records.get(key)
            if record is None:
                return ("not_found", None)
            if record.status == EndpointStatus.ACTIVE.value:
                return ("active", record.status)
            if record.status == EndpointStatus.PENDING_VERIFICATION.value:
                return ("pending", record.status)
            if record.status not in {
                EndpointStatus.REVOKED.value,
                EndpointStatus.EXPIRED.value,
            }:
                return ("unsupported_status", record.status)

            request_id = record.pending_request_id
            stale_pending_keys = [
                pending_key
                for pending_key, info in pending.items()
                if info.get("owner_platform_id") == owner_platform_id
                and (
                    (request_id is not None and info.get("request_id") == request_id)
                    or (
                        info.get("owner_user_id") == owner_user_id
                        and info.get("endpoint_name") == name
                    )
                )
            ]
            for pending_key in stale_pending_keys:
                pending.pop(pending_key, None)
            records.pop(key)
            return ("deleted", record.status)

        return self._transaction(mutate)

    def revoke_endpoint_by_owner_name(
        self, owner_platform_id: str, owner_user_id: str, name: str
    ) -> tuple[bool, str]:
        return self._revoke_managed(owner_platform_id, owner_user_id, name, admin=True)

    def _revoke_managed(
        self, platform_id: str, owner_id: str, name: str, admin: bool = False
    ) -> tuple[bool, str]:
        def mutate(records, quarantine, pending):
            key = _record_key(platform_id, owner_id, name)
            record = records.get(key)
            if not record:
                return (
                    False,
                    "owner/name 对应的 endpoint 不存在" if admin else "endpoint 不存在",
                )
            if record.status == EndpointStatus.REVOKED.value:
                return (True, "endpoint 已撤销，无需重复操作")
            if record.status not in VISIBLE_STATUSES:
                return (False, "该 endpoint 已是终态，无法撤销")
            record.status = EndpointStatus.REVOKED.value
            record.revoked_at = datetime.now(timezone.utc).isoformat()
            if record.pending_request_id:
                pending.pop(_pending_key(platform_id, record.pending_request_id), None)
                record.pending_request_id = None
                record.pending_code = None
                record.pending_expires_at = None
            return (True, "endpoint 已撤销")

        return self._transaction(mutate)

    def revoke_endpoint_by_path(self, path: str) -> tuple[bool, str]:
        normalized = path.strip().lstrip("/")
        if not normalized:
            return (False, "endpoint path 不能为空")

        def mutate(records, quarantine, pending):
            matches = [
                ("records", key, record)
                for key, record in records.items()
                if record.path == normalized
            ]
            matches += [
                ("quarantine", key, record)
                for key, record in quarantine.items()
                if record.path == normalized
            ]
            if not matches:
                return (False, "endpoint path 不存在")
            if len(matches) != 1:
                return (False, "Registry 数据异常：该 path 存在重复记录，未执行撤销")
            section, key, record = matches[0]
            if record.status == EndpointStatus.REVOKED.value:
                return (True, "endpoint 已撤销，无需重复操作")
            if record.status not in VISIBLE_STATUSES:
                return (False, "该 endpoint 已是终态，无法撤销")

            selected = records[key] if section == "records" else quarantine[key]
            selected.status = EndpointStatus.REVOKED.value
            selected.revoked_at = datetime.now(timezone.utc).isoformat()
            if selected.pending_request_id and selected.owner_platform_id:
                pending.pop(
                    _pending_key(
                        selected.owner_platform_id, selected.pending_request_id
                    ),
                    None,
                )
            selected.pending_request_id = None
            selected.pending_code = None
            selected.pending_expires_at = None
            return (True, "endpoint 已撤销")

        return self._transaction(mutate)

    def expire_stale_pending(self) -> int:
        now = datetime.now(timezone.utc)

        def mutate(records, quarantine, pending):
            stale = []
            for key, info in pending.items():
                try:
                    expires = datetime.fromisoformat(str(info.get("expires_at", "")))
                    if expires.tzinfo is None or now > expires:
                        stale.append(key)
                except (TypeError, ValueError):
                    stale.append(key)
            if not stale:
                return 0
            for key in stale:
                info = pending.get(key, {})
                record = records.get(
                    _record_key(
                        str(info.get("owner_platform_id", "")),
                        str(info.get("owner_user_id", "")),
                        str(info.get("endpoint_name", "")),
                    )
                )
                self._expire_pending_candidate(record, key, pending)
            return len(stale)

        return self._transaction(mutate)

    def is_endpoint_active(
        self, owner_platform_id: str, owner_user_id: str, name: str
    ) -> bool:
        record = self.get_scoped(owner_platform_id, owner_user_id, name)
        return bool(
            record
            and record.status == EndpointStatus.ACTIVE.value
            and record.revoked_at is None
            and record.token_hash
        )

    def authenticate_delivery(
        self, path: str, authorization_header: str | None
    ) -> DeliveryAuthentication:
        normalized = path.lstrip("/")
        with self._lock:
            record = next(
                (
                    item
                    for item in (*self._records.values(), *self._quarantine.values())
                    if item.path == normalized
                ),
                None,
            )
            if not record:
                return DeliveryAuthentication(False, "not_found", "endpoint 未找到")
            if record.status == EndpointStatus.REVOKED.value:
                return DeliveryAuthentication(
                    False, "endpoint_revoked", "endpoint 已撤销"
                )
            if (
                record.status != EndpointStatus.ACTIVE.value
                or record.revoked_at is not None
            ):
                return DeliveryAuthentication(
                    False, "endpoint_disabled", f"endpoint 状态: {record.status}"
                )
            if not record.token_hash:
                return DeliveryAuthentication(
                    False,
                    "token_unclaimed",
                    "Token 尚未领取，请先在私聊中执行 token rotate",
                )
            if not authorization_header:
                return DeliveryAuthentication(
                    False, "missing_authorization", "缺少 Authorization 请求头"
                )
            if not authorization_header.startswith("Bearer "):
                return DeliveryAuthentication(
                    False, "invalid_token", "Authorization 格式必须为 Bearer <token>"
                )
            bearer_token = authorization_header[7:].strip()
            if not verify_token(
                self._server_secret,
                bearer_token,
                record.token_hash,
                record.token_hash_algorithm,
            ):
                return DeliveryAuthentication(
                    False, "invalid_token", "Bearer Token 不匹配"
                )
            return DeliveryAuthentication(True, None, "ok", deepcopy(record))
