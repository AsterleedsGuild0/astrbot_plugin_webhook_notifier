"""进程内 Webhook 幂等 single-flight 存储。

该模块只在内存中保存规范 key 的 SHA-256 digest，不保存事件、路径或 payload。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


IDEMPOTENCY_TTL_SECONDS = 10 * 60
IDEMPOTENCY_MAX_ENTRIES = 2048

TargetSelector = tuple[str, ...]
IdempotencyKey = tuple[str, str, str, str, str, TargetSelector | None]


class ClaimStatus(str, Enum):
    OWNER = "owner"
    WAITER = "waiter"
    REPLAY = "replay"
    CAPACITY = "capacity"
    BYPASS = "bypass"


class TerminalState(str, Enum):
    FINALIZED = "finalized"
    RELEASED = "released"


def normalize_target_selector(target_alias: str | None) -> TargetSelector:
    """将目标选择器转换为与 Sender 精确匹配一致的结构化值。"""
    if target_alias is None:
        return ("all",)
    if not isinstance(target_alias, str) or not target_alias:
        raise ValueError("target_alias must be None or a non-empty string")
    return ("alias", target_alias)


def make_idempotency_key(
    *,
    endpoint_provider: str,
    endpoint_path: str,
    event_name: str,
    event_id: str,
    session_scope: str,
    target_alias: str | None,
) -> IdempotencyKey:
    """创建幂等 key；调用方应在 event.id 为空时跳过调用。"""
    return (
        str(endpoint_provider),
        str(endpoint_path),
        str(event_name),
        str(event_id),
        str(session_scope),
        normalize_target_selector(target_alias),
    )


def digest_idempotency_key(key: IdempotencyKey) -> str:
    """对结构化 key 做带类型标记的规范序列化并计算 SHA-256。"""
    provider, path, event_name, event_id, scope, selector = key
    selector = normalize_target_selector(None) if selector is None else tuple(selector)
    typed_key: list[Any] = [
        ["str", provider],
        ["str", path],
        ["str", event_name],
        ["str", event_id],
        ["str", scope],
        [["str", value] for value in selector],
    ]
    serialized = json.dumps(
        typed_key,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


class _EntryState(str, Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


@dataclass
class _Entry:
    digest: str
    future: asyncio.Future[TerminalState]
    state: _EntryState = _EntryState.IN_FLIGHT
    completed_at: float | None = None


@dataclass
class ClaimResult:
    status: ClaimStatus
    digest: str | None = None
    _entry: _Entry | None = field(default=None, repr=False)


class IdempotencyStore:
    """小型 async single-flight TTL/LRU store。

    claim/finalize/release 本身不需要 await：它们只在单个 asyncio event loop
    中修改有界字典；wait 使用 shield，取消 waiter 不会取消 owner 的 Future。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = IDEMPOTENCY_TTL_SECONDS,
        capacity: int = IDEMPOTENCY_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.capacity = int(capacity)
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._entries)

    def claim(self, key: IdempotencyKey) -> ClaimResult:
        """申请 owner、等待共享执行、回放已完成结果或返回容量不足。"""
        if not key[3]:
            return ClaimResult(ClaimStatus.BYPASS)

        digest = digest_idempotency_key(key)
        now = self._clock()
        self._cleanup(now)
        existing = self._entries.get(digest)
        if existing is not None:
            if existing.state is _EntryState.IN_FLIGHT:
                return ClaimResult(ClaimStatus.WAITER, digest, existing)
            self._entries.move_to_end(digest)
            return ClaimResult(ClaimStatus.REPLAY, digest, existing)

        if not self._make_room_for_owner():
            return ClaimResult(ClaimStatus.CAPACITY, digest)

        future = self._new_future()
        entry = _Entry(digest=digest, future=future)
        self._entries[digest] = entry
        return ClaimResult(ClaimStatus.OWNER, digest, entry)

    async def wait(self, claim: ClaimResult) -> ClaimResult:
        """等待 waiter 的 owner 终态；RELEASED 时重新竞争 owner。"""
        if claim.status is not ClaimStatus.WAITER or claim._entry is None:
            return claim

        current = claim
        while current.status is ClaimStatus.WAITER and current._entry is not None:
            terminal = await asyncio.shield(current._entry.future)
            if terminal is TerminalState.FINALIZED:
                return ClaimResult(ClaimStatus.REPLAY, current.digest)
            if terminal is TerminalState.RELEASED:
                current = self._claim_digest(current.digest)
                continue
            raise RuntimeError("未知的幂等终态")
        return current

    def finalize(self, claim: ClaimResult) -> bool:
        """将 owner 标记为 completed，并完成所有 waiter 的 Future。"""
        entry = self._owner_entry(claim)
        if entry is None:
            return False
        entry.state = _EntryState.COMPLETED
        entry.completed_at = self._clock()
        self._entries.move_to_end(entry.digest)
        self._complete(entry, TerminalState.FINALIZED)
        return True

    def release(self, claim: ClaimResult) -> bool:
        """释放尚未跨过发送边界的 owner，并完成所有 waiter 的 Future。"""
        entry = self._owner_entry(claim)
        if entry is None:
            return False
        self._entries.pop(entry.digest, None)
        self._complete(entry, TerminalState.RELEASED)
        return True

    def _claim_digest(self, digest: str | None) -> ClaimResult:
        if not digest:
            return ClaimResult(ClaimStatus.CAPACITY)
        self._cleanup(self._clock())
        existing = self._entries.get(digest)
        if existing is not None:
            if existing.state is _EntryState.IN_FLIGHT:
                return ClaimResult(ClaimStatus.WAITER, digest, existing)
            self._entries.move_to_end(digest)
            return ClaimResult(ClaimStatus.REPLAY, digest, existing)
        if not self._make_room_for_owner():
            return ClaimResult(ClaimStatus.CAPACITY, digest)
        entry = _Entry(digest=digest, future=self._new_future())
        self._entries[digest] = entry
        return ClaimResult(ClaimStatus.OWNER, digest, entry)

    def _owner_entry(self, claim: ClaimResult) -> _Entry | None:
        if claim.status is not ClaimStatus.OWNER or claim._entry is None:
            return None
        entry = claim._entry
        if self._entries.get(entry.digest) is not entry:
            return None
        if entry.state is not _EntryState.IN_FLIGHT:
            return None
        return entry

    def _cleanup(self, now: float) -> None:
        expired = [
            digest
            for digest, entry in self._entries.items()
            if entry.state is _EntryState.COMPLETED
            and entry.completed_at is not None
            and now - entry.completed_at >= self.ttl_seconds
        ]
        for digest in expired:
            self._entries.pop(digest, None)

    def _make_room_for_owner(self) -> bool:
        if self.capacity <= 0:
            return False
        while len(self._entries) >= self.capacity:
            completed_digest = next(
                (
                    digest
                    for digest, entry in self._entries.items()
                    if entry.state is _EntryState.COMPLETED
                ),
                None,
            )
            if completed_digest is None:
                return False
            self._entries.pop(completed_digest, None)
        return True

    @staticmethod
    def _new_future() -> asyncio.Future[TerminalState]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 允许纯同步单测先 claim/finalize；实际 HTTP 路径始终在运行中的
            # request loop 内创建 Future。
            loop = asyncio.new_event_loop()
        return loop.create_future()

    @staticmethod
    def _complete(entry: _Entry, terminal: TerminalState) -> None:
        if not entry.future.done():
            entry.future.set_result(terminal)
