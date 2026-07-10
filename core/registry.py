from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import EndpointRecord, EndpointStatus, TargetAlias
from .security import (
    TOKEN_HASH_ALGORITHM,
    generate_request_id,
    generate_token,
    generate_verification_code,
    hash_token,
    load_server_secret,
)

REGISTRY_FILENAME = "webhook_tokens.json"
PENDING_EXPIRY_SECONDS = 600  # 10 分钟


def normalize_endpoint_name(name: str, fallback: str = "default") -> str:
    """将用户输入的 endpoint 名称规范化为可用于命令和 URL 的安全片段。"""
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip())
    normalized = normalized.strip("-._")
    return normalized or fallback


def owner_path_hash(owner_user_id: str) -> str:
    """返回不直接暴露用户 ID 的稳定短 hash，用于 URL 命名空间。"""
    return sha256(owner_user_id.encode("utf-8")).hexdigest()[:12]


def build_endpoint_path(owner_user_id: str, endpoint_name: str) -> str:
    """构造用户隔离的 endpoint path。"""
    return f"u/{owner_path_hash(owner_user_id)}/{endpoint_name}"


def _record_key(owner_user_id: str, name: str) -> str:
    return f"{owner_user_id}\x1f{name}"


class EndpointRegistry:
    """Endpoint 注册表，管理 endpoint/token 持久化。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._registry_path = self._data_dir / REGISTRY_FILENAME
        self._server_secret = load_server_secret(self._data_dir)
        self._records: dict[str, EndpointRecord] = {}  # owner_user_id + name -> record
        self._pending: dict[str, dict[str, Any]] = {}  # request_id -> pending info
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        """从 JSON 文件加载注册表。"""
        if not self._registry_path.exists():
            self._records = {}
            self._pending = {}
            return
        try:
            raw = json.loads(self._registry_path.read_text(encoding="utf-8"))
            records_raw = raw.get("records", {})
            self._records = {}
            for key, data in records_raw.items():
                targets = [
                    TargetAlias(**t) if isinstance(t, dict) else t
                    for t in data.get("targets", [])
                ]
                record = EndpointRecord(
                    name=data.get("name", key),
                    path=data.get("path", ""),
                    provider=data.get("provider", "omp"),
                    token_hash=data.get("token_hash", ""),
                    token_hash_algorithm=data.get(
                        "token_hash_algorithm", TOKEN_HASH_ALGORITHM
                    ),
                    owner_user_id=data.get("owner_user_id", ""),
                    targets=targets,
                    render_mode=data.get("render_mode", "text"),
                    template=data.get("template"),
                    status=data.get("status", EndpointStatus.REVOKED.value),
                    created_at=data.get("created_at", ""),
                    revoked_at=data.get("revoked_at"),
                    pending_request_id=data.get("pending_request_id"),
                    pending_code=data.get("pending_code"),
                    pending_expires_at=data.get("pending_expires_at"),
                    description=data.get("description"),
                )
                self._records[_record_key(record.owner_user_id, record.name)] = record
            self._pending = raw.get("pending", {})
            logger.info(
                f"[WebhookNotifier] 已加载 {len(self._records)} 个 endpoint 记录"
            )
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.error(
                f"[WebhookNotifier] 加载 endpoint registry 失败: {e}，将使用空注册表"
            )
            self._records = {}
            self._pending = {}

    def _save(self) -> None:
        """保存注册表到 JSON 文件。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        records_raw: dict[str, dict[str, Any]] = {}
        for rec in self._records.values():
            records_raw[_record_key(rec.owner_user_id, rec.name)] = {
                "name": rec.name,
                "path": rec.path,
                "provider": rec.provider,
                "token_hash": rec.token_hash,
                "token_hash_algorithm": rec.token_hash_algorithm,
                "owner_user_id": rec.owner_user_id,
                "targets": [{"name": t.name, "umo": t.umo} for t in rec.targets],
                "render_mode": rec.render_mode,
                "template": rec.template,
                "status": rec.status,
                "created_at": rec.created_at,
                "revoked_at": rec.revoked_at,
                "pending_request_id": rec.pending_request_id,
                "pending_code": rec.pending_code,
                "pending_expires_at": rec.pending_expires_at,
                "description": rec.description,
            }
        data = {"records": records_raw, "pending": self._pending}
        tmp = str(self._registry_path) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._registry_path)
        except OSError as e:
            logger.error(f"[WebhookNotifier] 保存 endpoint registry 失败: {e}")

    # ---- Server Secret ----

    @property
    def server_secret(self) -> str:
        return self._server_secret

    # ---- 创建 endpoint ----

    def create_private_endpoint(
        self,
        name: str,
        path: str,
        owner_user_id: str,
        target_umo: str,
        render_mode: str = "text",
        description: str | None = None,
    ) -> tuple[EndpointRecord, str]:
        """创建私聊目标 endpoint，直接进入 active 状态。

        Returns:
            (EndpointRecord, token_plaintext)
        """
        token_plain = generate_token()
        token_hash = hash_token(self._server_secret, token_plain)
        now_iso = datetime.now(timezone.utc).isoformat()
        record = EndpointRecord(
            name=name,
            path=path,
            provider="omp",
            token_hash=token_hash,
            token_hash_algorithm=TOKEN_HASH_ALGORITHM,
            owner_user_id=owner_user_id,
            targets=[TargetAlias(name="default", umo=target_umo)],
            render_mode=render_mode,
            template=None,
            status=EndpointStatus.ACTIVE.value,
            created_at=now_iso,
            description=description or f"私聊 endpoint for {owner_user_id}",
        )
        self._records[_record_key(owner_user_id, name)] = record
        self._save()
        return record, token_plain

    def create_pending_verification(
        self,
        name: str,
        path: str,
        owner_user_id: str,
        target_group_id: str,
        render_mode: str = "text",
        description: str | None = None,
    ) -> tuple[EndpointRecord, str, str]:
        """创建群聊待验证 endpoint。

        Returns:
            (EndpointRecord, request_id, code)
        """
        request_id = generate_request_id()
        code = generate_verification_code()
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(
            time.time() + PENDING_EXPIRY_SECONDS, tz=timezone.utc
        )
        record = EndpointRecord(
            name=name,
            path=path,
            provider="omp",
            token_hash="",
            token_hash_algorithm=TOKEN_HASH_ALGORITHM,
            owner_user_id=owner_user_id,
            targets=[],  # 验证通过后填充
            render_mode=render_mode,
            template=None,
            status=EndpointStatus.PENDING_VERIFICATION.value,
            created_at=now.isoformat(),
            pending_request_id=request_id,
            pending_code=code,
            pending_expires_at=expires_at.isoformat(),
            description=description
            or f"群聊 endpoint for {owner_user_id} 目标群 {target_group_id}",
        )
        self._records[_record_key(owner_user_id, name)] = record
        pending_info = {
            "endpoint_name": name,
            "request_id": request_id,
            "code": code,
            "owner_user_id": owner_user_id,
            "target_group_id": target_group_id,
            "expires_at": expires_at.isoformat(),
        }
        self._pending[request_id] = pending_info
        self._save()
        return record, request_id, code

    def verify_group_endpoint(
        self,
        request_id: str,
        code: str,
        verify_user_id: str,
        verify_group_id: str,
        group_target_umo: str,
    ) -> tuple[str, str, str | None]:
        """验证群聊 endpoint。

        Args:
            request_id: 申请时生成的 request_id。
            code: 验证码。
            verify_user_id: 执行验证的用户 ID。
            verify_group_id: 执行验证的群 ID。
            group_target_umo: 群聊目标 UMO。

        Returns:
            (status, message, token_or_none)
            status 为 "ok" / "error"。
            成功时 token_or_none 为 token 明文。
        """
        pending = self._pending.get(request_id)
        if not pending:
            return ("error", "验证请求不存在或已过期", None)

        # 校验 code
        if pending.get("code") != code:
            return ("error", "验证码不匹配", None)

        # 校验过期
        expires_at_str = pending.get("expires_at", "")
        try:
            expires_dt = datetime.fromisoformat(expires_at_str)
            if datetime.now(timezone.utc) > expires_dt:
                self._cleanup_pending(request_id)
                return ("error", "验证请求已过期", None)
        except (ValueError, TypeError):
            pass

        # 校验用户
        if pending.get("owner_user_id") != verify_user_id:
            return ("error", "验证失败：执行者不是申请者", None)

        # 校验群
        if pending.get("target_group_id") != verify_group_id:
            return ("error", "验证失败：当前群不是申请目标群", None)

        # 校验 endpoint 状态
        endpoint_name = pending["endpoint_name"]
        record = self.get_by_owner_name(pending.get("owner_user_id", ""), endpoint_name)
        if not record or record.status != EndpointStatus.PENDING_VERIFICATION.value:
            self._cleanup_pending(request_id)
            return ("error", "验证请求已失效", None)

        # 激活 endpoint
        token_plain = generate_token()
        token_hash = hash_token(self._server_secret, token_plain)
        record.token_hash = token_hash
        record.token_hash_algorithm = TOKEN_HASH_ALGORITHM
        record.status = EndpointStatus.ACTIVE.value
        record.targets = [TargetAlias(name="default", umo=group_target_umo)]
        record.pending_request_id = None
        record.pending_code = None
        record.pending_expires_at = None

        self._cleanup_pending(request_id)
        self._save()

        return ("ok", "验证成功", token_plain)

    def _cleanup_pending(self, request_id: str) -> None:
        self._pending.pop(request_id, None)
        self._save()

    # ---- 查询 ----

    def get_by_name(self, name: str) -> EndpointRecord | None:
        for rec in self._records.values():
            if rec.name == name:
                return rec
        return None

    def get_by_owner_name(self, owner_user_id: str, name: str) -> EndpointRecord | None:
        return self._records.get(_record_key(owner_user_id, name))

    def get_by_path(self, path: str) -> EndpointRecord | None:
        """按 endpoint path 查找记录。"""
        for rec in self._records.values():
            if rec.path == path:
                return rec
        return None

    def list_by_owner(self, owner_user_id: str) -> list[EndpointRecord]:
        return [
            rec for rec in self._records.values() if rec.owner_user_id == owner_user_id
        ]

    def list_visible_by_owner(self, owner_user_id: str) -> list[EndpointRecord]:
        """列出用户默认可见的 endpoint。

        revoked/expired 记录仍持久化用于审计和安全排查，但不在普通用户的
        token list 中展示，避免撤销后仍看起来可继续使用。
        """
        visible_states = {
            EndpointStatus.ACTIVE.value,
            EndpointStatus.PENDING_VERIFICATION.value,
        }
        return [
            rec
            for rec in self._records.values()
            if rec.owner_user_id == owner_user_id and rec.status in visible_states
        ]

    def count_active(self) -> int:
        return sum(
            1
            for rec in self._records.values()
            if rec.status == EndpointStatus.ACTIVE.value
        )

    # ---- 轮换 ----

    def rotate_token(self, name: str, owner_user_id: str) -> tuple[bool, str]:
        """轮换 endpoint 的 token。

        Returns:
            (success, token_plain_or_error_msg)
        """
        record = self.get_by_owner_name(owner_user_id, name)
        if not record:
            return (False, "endpoint 不存在")
        if record.status != EndpointStatus.ACTIVE.value:
            return (False, "只有 active 状态的 endpoint 可以轮换 token")

        token_plain = generate_token()
        token_hash = hash_token(self._server_secret, token_plain)
        record.token_hash = token_hash
        self._save()
        return (True, token_plain)

    # ---- 撤销 ----

    def revoke_endpoint(self, name: str, owner_user_id: str) -> tuple[bool, str]:
        """撤销 endpoint。

        Returns:
            (success, message)
        """
        record = self.get_by_owner_name(owner_user_id, name)
        if not record:
            return (False, "endpoint 不存在")

        valid_states = {
            EndpointStatus.PENDING_VERIFICATION.value,
            EndpointStatus.ACTIVE.value,
        }
        if record.status not in valid_states:
            return (False, "该 endpoint 已是终态，无需撤销")

        record.status = EndpointStatus.REVOKED.value
        record.revoked_at = datetime.now(timezone.utc).isoformat()
        # 清理关联的 pending
        if record.pending_request_id:
            self._cleanup_pending(record.pending_request_id)
            record.pending_request_id = None
            record.pending_code = None
            record.pending_expires_at = None
        self._save()
        return (True, "endpoint 已撤销")

    # ---- 过期清理 ----

    def expire_stale_pending(self) -> int:
        """清理所有过期的待验证申请。

        Returns:
            清理数量。
        """
        now = datetime.now(timezone.utc)
        expired_ids = []
        for rid, info in self._pending.items():
            expires_at_str = info.get("expires_at", "")
            try:
                expires_dt = datetime.fromisoformat(expires_at_str)
                if now > expires_dt:
                    expired_ids.append(rid)
            except (ValueError, TypeError):
                expired_ids.append(rid)

        cleaned = 0
        for rid in expired_ids:
            info = self._pending.get(rid)
            if info:
                endpoint_name = info.get("endpoint_name")
                owner_user_id = info.get("owner_user_id", "")
                if not isinstance(endpoint_name, str):
                    self._pending.pop(rid, None)
                    continue
                record = self.get_by_owner_name(owner_user_id, endpoint_name)
                if (
                    record
                    and record.status == EndpointStatus.PENDING_VERIFICATION.value
                ):
                    record.status = EndpointStatus.EXPIRED.value
                    record.pending_request_id = None
                    record.pending_code = None
                    record.pending_expires_at = None
                self._pending.pop(rid, None)
                cleaned += 1

        if cleaned:
            self._save()
        return cleaned

    # ---- 检查 endpoint 是否可用 ----

    def is_endpoint_active(self, name: str, owner_user_id: str | None = None) -> bool:
        record = (
            self.get_by_owner_name(owner_user_id, name)
            if owner_user_id is not None
            else self.get_by_name(name)
        )
        if not record:
            return False
        return (
            record.status == EndpointStatus.ACTIVE.value
            and record.revoked_at is None
            and bool(record.token_hash)
        )
