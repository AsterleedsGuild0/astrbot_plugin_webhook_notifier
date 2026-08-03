from __future__ import annotations

import re
import math
import json
from datetime import datetime, timezone
from typing import Any, NoReturn

from .models import NormalizedEvent
from .notification_policy import SessionScope
from .providers import ProviderAdapter, ProviderError

_OPENCODE_KEY = "opencode"
_OPENCODE_VERSION = 1

# ─── 字段长度约束 ──────────────────────────────────────────

_MAX_ID_REF = 128
_MAX_NAME = 200  # Unicode chars
_MAX_AGENT_MODEL = 128
_MAX_CATEGORY_CODE = 64
_MAX_ACTION_TEXT = 512
_MAX_ACTION_SUMMARY = 256
_MAX_ACTION_ITEMS = 8
_MAX_ACTION_OPTIONS = 12
_MAX_PERMISSION_ITEMS = 16
_MAX_PERMISSION_PATTERNS = 16
_MAX_ACTION_COUNT = 1_000_000
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_DURATION_MS = 604800000  # 7 days
_MIN_DURATION_MS = 0
_MAX_TIMELINE_BYTES = 24 * 1024
_MAX_TIMELINE_ITEMS = 64
_MAX_TIMELINE_OBSERVED_ITEMS = 4096
_MAX_TIMELINE_DEPTH = 8
_MAX_TIMELINE_ATTEMPT = 1_000_000
_MAX_TIMELINE_REASONS = 6
_MAX_TIMELINE_NUMBER = 2**53 - 1
_MAX_USER_WAIT_TIMELINE_BYTES = 12 * 1024

# ─── 允许字段 ──────────────────────────────────────────────

_SESSION_ALLOW = frozenset({"ref", "name", "scope"})
_COUNTS_ALLOW = frozenset({"messages", "tools", "changes"})
_PERMISSION_ALLOW = frozenset({"count", "items"})
_PERMISSION_LEGACY_ALLOW = frozenset(
    {"category", "title", "summary", "description", "action", "target", "patterns"}
)
_PERMISSION_ITEM_ALLOW = _PERMISSION_LEGACY_ALLOW
_QUESTION_ALLOW = frozenset({"count", "optionCount", "summary", "items"})
_QUESTION_ITEM_ALLOW = frozenset({"text", "header", "recommended", "options"})
_QUESTION_OPTION_ALLOW = frozenset({"label", "description", "recommended"})
_ERROR_ALLOW = frozenset({"category", "code"})
_TIMELINE_ALLOW = frozenset(
    {
        "version",
        "partial",
        "partialReasons",
        "timeBasis",
        "observedItemCount",
        "displayedItemCount",
        "truncated",
        "items",
    }
)
_TIMELINE_ITEM_ALLOW = frozenset(
    {
        "ref",
        "parentRef",
        "name",
        "agent",
        "model",
        "modelVariant",
        "status",
        "startOffsetMs",
        "endOffsetMs",
        "durationMs",
        "timingQuality",
        "depth",
        "attempt",
    }
)
_TIMELINE_REASONS = (
    "missing_parent",
    "missing_start",
    "missing_end",
    "invalid_parent_graph",
    "truncated",
    "clamped",
)
_TIMELINE_STATUSES = frozenset(
    {"running", "completed", "failed", "cancelled", "unknown"}
)
_TIMELINE_QUALITIES = frozenset({"observed", "fallback", "partial", "unknown"})
_TIMELINE_REF_RE = re.compile(r"^[0-9a-f]{32}$")

# ─── userWaitTimeline 契约 ─────────────────────────────────

_USER_WAIT_ALLOW = frozenset(
    {
        "version",
        "timeBasis",
        "partial",
        "partialReasons",
        "observedIntervalCount",
        "displayedIntervalCount",
        "truncated",
        "intervals",
    }
)
_USER_WAIT_INTERVAL_ALLOW = frozenset(
    {
        "kind",
        "result",
        "intervalState",
        "startOffsetMs",
        "endOffsetMs",
        "durationMs",
    }
)
_USER_WAIT_REASONS = (
    "open_at_cycle_end",
    "orphan_resolution",
    "missing_request_id",
    "evicted",
    "truncated",
    "clock_invalid",
)
_USER_WAIT_KINDS = frozenset({"question", "permission"})
_USER_WAIT_RESULTS = frozenset({"replied", "rejected"})
_USER_WAIT_STATES = frozenset({"complete", "right_censored", "left_censored"})

# ─── 敏感字段 — 白名单自然拒绝，但列出便于可读 ───────────

_SENSITIVE_KEYS: frozenset[str] = frozenset()

# ─── 固定安全消息 ──────────────────────────────────────────

_MSG_SECURE: dict[str, str] = {
    "id": "无效的 id 字段",
    "event": "无效的 event 字段",
    "version": "无效的 version 字段",
    "emittedAt": "无效的 emittedAt 字段",
    "session": "无效的 session 字段",
    "session.ref": "无效的 session.ref 字段",
    "session.name": "无效的 session.name 字段",
    "session.scope": "无效的 session.scope 字段",
    "agent": "无效的 agent 字段",
    "model": "无效的 model 字段",
    "modelVariant": "无效的 modelVariant 字段",
    "durationMs": "无效的 durationMs 字段",
    "instanceDisplayName": "无效的 instanceDisplayName 字段",
    "projectName": "无效的 projectName 字段",
    "startedAt": "无效的 startedAt 字段",
    "taskStartedAt": "无效的 taskStartedAt 字段",
    "endedAt": "无效的 endedAt 字段",
    "counts": "无效的 counts 字段",
    "question": "无效的 question 字段",
    "question.count": "无效的 question.count 字段",
    "question.optionCount": "无效的 question.optionCount 字段",
    "question.summary": "无效的 question.summary 字段",
    "question.items": "无效的 question.items 字段",
    "permission.count": "无效的 permission.count 字段",
    "permission.items": "无效的 permission.items 字段",
    "permission.title": "无效的 permission.title 字段",
    "permission.summary": "无效的 permission.summary 字段",
    "permission.description": "无效的 permission.description 字段",
    "permission.action": "无效的 permission.action 字段",
    "permission.target": "无效的 permission.target 字段",
    "permission.patterns": "无效的 permission.patterns 字段",
    "permission": "无效的 permission 字段",
    "permission.category": "无效的 permission.category 字段",
    "error": "无效的 error 字段",
    "error.category": "无效的 error.category 字段",
    "error.code": "无效的 error.code 字段",
    "subagentTimeline": "无效的 subagentTimeline 字段",
    "subagentTimeline.version": "无效的 subagentTimeline.version 字段",
    "subagentTimeline.partial": "无效的 subagentTimeline.partial 字段",
    "subagentTimeline.partialReasons": "无效的 subagentTimeline.partialReasons 字段",
    "subagentTimeline.timeBasis": "无效的 subagentTimeline.timeBasis 字段",
    "subagentTimeline.observedItemCount": "无效的 subagentTimeline.observedItemCount 字段",
    "subagentTimeline.displayedItemCount": "无效的 subagentTimeline.displayedItemCount 字段",
    "subagentTimeline.truncated": "无效的 subagentTimeline.truncated 字段",
    "subagentTimeline.items": "无效的 subagentTimeline.items 字段",
    "subagentTimeline.item": "无效的 subagentTimeline item 字段",
    "userWaitTimeline": "无效的 userWaitTimeline 字段",
    "userWaitTimeline.version": "无效的 userWaitTimeline.version 字段",
    "userWaitTimeline.timeBasis": "无效的 userWaitTimeline.timeBasis 字段",
    "userWaitTimeline.partial": "无效的 userWaitTimeline.partial 字段",
    "userWaitTimeline.partialReasons": "无效的 userWaitTimeline.partialReasons 字段",
    "userWaitTimeline.observedIntervalCount": "无效的 userWaitTimeline.observedIntervalCount 字段",
    "userWaitTimeline.displayedIntervalCount": "无效的 userWaitTimeline.displayedIntervalCount 字段",
    "userWaitTimeline.truncated": "无效的 userWaitTimeline.truncated 字段",
    "userWaitTimeline.intervals": "无效的 userWaitTimeline.intervals 字段",
    "userWaitTimeline.interval": "无效的 userWaitTimeline interval 字段",
    "userWaitTimeline.interval.kind": "无效的 userWaitTimeline interval.kind 字段",
    "userWaitTimeline.interval.result": "无效的 userWaitTimeline interval.result 字段",
    "userWaitTimeline.interval.intervalState": "无效的 userWaitTimeline interval.intervalState 字段",
    "userWaitTimeline.interval.startOffsetMs": "无效的 userWaitTimeline interval.startOffsetMs 字段",
    "userWaitTimeline.interval.endOffsetMs": "无效的 userWaitTimeline interval.endOffsetMs 字段",
    "userWaitTimeline.interval.durationMs": "无效的 userWaitTimeline interval.durationMs 字段",
}


def _safe_msg(key: str) -> str:
    return _MSG_SECURE.get(key, "请求无效")


# ─── 类型辅助 ──────────────────────────────────────────────


def _is_nonempty_str(val: Any) -> bool:
    return isinstance(val, str) and len(val) > 0


def _is_strict_int(val: Any) -> bool:
    """验证是否为严格 int 且非 bool。"""
    return isinstance(val, int) and not isinstance(val, bool)


def _is_iso8601_with_tz(val: str) -> bool:
    """检查 str 是否为带时区的 ISO-8601 (含 Z)。"""
    if not val or not isinstance(val, str):
        return False
    # 尝试用 Python 解析
    try:
        dt = datetime.fromisoformat(
            val.replace("Z", "+00:00", 1) if "Z" in val else val
        )
        # 要求有时区信息
        if dt.tzinfo is None:
            return False
        return True
    except (ValueError, TypeError):
        return False


def _try_parse_iso(val: str) -> str | None:
    """尝试解析并返回标准化 ISO-8601 字符串，失败返回 None。"""
    try:
        dt = datetime.fromisoformat(
            val.replace("Z", "+00:00", 1) if "Z" in val else val
        )
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


# ─── Unicode 安全 ──────────────────────────────────────────

_DANGEROUS_UNICODE: re.Pattern = re.compile(
    "["
    + "\u200b-\u200f"  # ZWSP, ZWNJ, ZWJ, LRM, RLM, LRE, RLE, PDF
    + "\u202a-\u202e"  # LRE, RLE, PDF, LRO, RLO
    + "\u2028\u2029"  # line/paragraph separator
    + "\u2066-\u2069"  # LRI, RLI, FSI, PDI
    + "\ufeff"  # BOM/ZWNBSP
    + "]"
)


def _strip_dangerous_unicode(s: str) -> str:
    """移除 Unicode Bidi 控制字符、零宽/format chars、行/段分隔符。

    保留普通 Unicode/CJK/emoji 可读字符。返回的空字符串由调用方处理。
    """
    return _DANGEROUS_UNICODE.sub("", s)


# ─── 字段校验器 ────────────────────────────────────────────


def _check_id(val: Any) -> str:
    if not isinstance(val, str):
        raise ProviderError("invalid_payload", _safe_msg("id"), retryable=False)
    trimmed = val.strip()
    if not trimmed or len(trimmed) > _MAX_ID_REF:
        raise ProviderError("invalid_payload", _safe_msg("id"), retryable=False)
    return trimmed


def _check_event(val: Any) -> str:
    allowed = frozenset(
        {
            "opencode.session_idle",
            "opencode.session_error",
            "opencode.permission_asked",
            "opencode.question_asked",
        }
    )
    if not _is_nonempty_str(val) or val not in allowed:
        raise ProviderError(
            "unsupported_event", "event 值不在支持枚举中", retryable=False
        )
    return val


def _check_version(val: Any) -> int:
    if not _is_strict_int(val) or val != _OPENCODE_VERSION:
        raise ProviderError(
            "unsupported_version", _safe_msg("version"), retryable=False
        )
    return val


def _check_emitted_at(val: Any) -> str:
    if not isinstance(val, str) or not _is_iso8601_with_tz(val):
        raise ProviderError("invalid_payload", _safe_msg("emittedAt"), retryable=False)
    normalized = _try_parse_iso(val)
    if normalized is None:
        raise ProviderError("invalid_payload", _safe_msg("emittedAt"), retryable=False)
    return normalized


def _check_session_ref(val: Any) -> str:
    if not isinstance(val, str):
        raise ProviderError(
            "invalid_payload", _safe_msg("session.ref"), retryable=False
        )
    trimmed = val.strip()
    if not trimmed or len(trimmed) > _MAX_ID_REF:
        raise ProviderError(
            "invalid_payload", _safe_msg("session.ref"), retryable=False
        )
    return trimmed


def _check_session_name(val: Any) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str) or len(val) > _MAX_NAME:
        raise ProviderError(
            "invalid_payload", _safe_msg("session.name"), retryable=False
        )
    stripped = _strip_dangerous_unicode(val)
    return stripped if stripped else None


def _check_session_scope(val: Any) -> SessionScope:
    if not isinstance(val, str) or val not in {
        SessionScope.ROOT.value,
        SessionScope.SUBAGENT.value,
        SessionScope.AUXILIARY.value,
        SessionScope.UNKNOWN.value,
    }:
        raise ProviderError(
            "invalid_payload", _safe_msg("session.scope"), retryable=False
        )
    return SessionScope(val)


def _check_agent_or_model(val: Any, field: str) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    trimmed = val.strip()
    if not trimmed:
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    if len(trimmed) > _MAX_AGENT_MODEL:
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    return trimmed


def _check_model_variant(val: Any) -> str | None:
    """校验可选的 OpenCode variant；不把它解释为 provider 原始 reasoning effort。"""
    if val is None:
        return None
    return _check_action_text(val, "modelVariant", max_length=_MAX_AGENT_MODEL)


def _check_duration_ms(val: Any) -> int | None:
    if val is None:
        return None
    if not _is_strict_int(val) or val < _MIN_DURATION_MS or val > _MAX_DURATION_MS:
        raise ProviderError("invalid_payload", _safe_msg("durationMs"), retryable=False)
    return val


def _check_category(val: Any, field: str) -> str:
    if not isinstance(val, str):
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    trimmed = val.strip()
    if not trimmed or len(trimmed) > _MAX_CATEGORY_CODE:
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    return trimmed


def _check_action_text(
    val: Any, field: str, *, max_length: int = _MAX_ACTION_TEXT
) -> str:
    if not isinstance(val, str) or not val.strip() or len(val) > max_length:
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    cleaned = _strip_dangerous_unicode(val)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\r\n\t]", " ", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned).strip()
    if not cleaned:
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    return cleaned


def _check_optional_action_text(
    val: Any,
    field: str,
    *,
    max_length: int = _MAX_ACTION_TEXT,
) -> str | None:
    if val is None:
        return None
    return _check_action_text(val, field, max_length=max_length)


def _check_action_scalar(val: Any, field: str) -> str | bool | int | float:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        if abs(val) > _MAX_ACTION_COUNT:
            raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
        return val
    if isinstance(val, float):
        if not math.isfinite(val) or abs(val) > _MAX_ACTION_COUNT:
            raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
        return val
    return _check_action_text(val, field)


def _check_action_count(val: Any, field: str) -> int | None:
    if val is None:
        return None
    if not _is_strict_int(val) or val < 0 or val > _MAX_ACTION_COUNT:
        raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)
    return val


def _check_optional_timestamp(val: Any, field: str) -> str | None:
    if val is None:
        return None
    return _check_emitted_at(val) if isinstance(val, str) else _raise_invalid(field)


def _raise_invalid(field: str) -> None:
    raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)


def _timeline_invalid(field: str = "subagentTimeline") -> NoReturn:
    raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)


def _check_timeline_ref(val: Any, field: str) -> str:
    if not isinstance(val, str) or _TIMELINE_REF_RE.fullmatch(val) is None:
        _timeline_invalid(field)
    return val


def _check_timeline_number(val: Any, field: str) -> int | float:
    if (
        isinstance(val, bool)
        or not isinstance(val, (int, float))
        or not math.isfinite(val)
        or val < 0
        or val > _MAX_TIMELINE_NUMBER
    ):
        _timeline_invalid(field)
    return val


def _check_timeline_count(val: Any, field: str, maximum: int) -> int:
    if not _is_strict_int(val) or val < 0 or val > maximum:
        _timeline_invalid(field)
    return val


def _check_timeline_text(
    val: Any,
    field: str,
    *,
    max_length: int,
    name_style: bool = False,
) -> str:
    if not isinstance(val, str) or not val or len(val) > max_length:
        _timeline_invalid(field)
    cleaned = (
        _clean_session_name(val)
        if name_style
        else _check_action_text(val, field, max_length=max_length)
    )
    if cleaned is None:
        _timeline_invalid(field)
    return cleaned


def _validate_subagent_timeline(raw: Any) -> dict[str, Any]:
    """Validate and copy the Phase 1A root-session timeline envelope."""

    if not isinstance(raw, dict):
        _timeline_invalid()
    _check_unknown_fields(raw, _TIMELINE_ALLOW, "subagentTimeline")
    required = {
        "version",
        "partial",
        "partialReasons",
        "timeBasis",
        "observedItemCount",
        "displayedItemCount",
        "truncated",
        "items",
    }
    if not required.issubset(raw):
        _timeline_invalid()

    if not _is_strict_int(raw["version"]) or raw["version"] != 1:
        _timeline_invalid("subagentTimeline.version")
    partial = raw["partial"]
    truncated = raw["truncated"]
    if not isinstance(partial, bool):
        _timeline_invalid("subagentTimeline.partial")
    if not isinstance(truncated, bool):
        _timeline_invalid("subagentTimeline.truncated")
    if raw["timeBasis"] != "root_cycle":
        _timeline_invalid("subagentTimeline.timeBasis")

    raw_reasons = raw["partialReasons"]
    if (
        not isinstance(raw_reasons, list)
        or len(raw_reasons) > _MAX_TIMELINE_REASONS
        or any(reason not in _TIMELINE_REASONS for reason in raw_reasons)
        or len(set(raw_reasons)) != len(raw_reasons)
        or tuple(raw_reasons)
        != tuple(reason for reason in _TIMELINE_REASONS if reason in raw_reasons)
    ):
        _timeline_invalid("subagentTimeline.partialReasons")
    reasons = list(raw_reasons)

    observed_count = _check_timeline_count(
        raw["observedItemCount"],
        "subagentTimeline.observedItemCount",
        _MAX_TIMELINE_OBSERVED_ITEMS,
    )
    displayed_count = _check_timeline_count(
        raw["displayedItemCount"],
        "subagentTimeline.displayedItemCount",
        _MAX_TIMELINE_ITEMS,
    )
    if displayed_count > observed_count:
        _timeline_invalid("subagentTimeline.displayedItemCount")

    raw_items = raw["items"]
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_TIMELINE_ITEMS:
        _timeline_invalid("subagentTimeline.items")
    if len(raw_items) != displayed_count:
        _timeline_invalid("subagentTimeline.items")

    if partial != bool(reasons):
        _timeline_invalid()
    if truncated and (not partial or "truncated" not in reasons):
        _timeline_invalid()
    if "truncated" in reasons and not truncated:
        _timeline_invalid()
    if not partial and truncated:
        _timeline_invalid()

    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            _timeline_invalid("subagentTimeline.item")
        _check_unknown_fields(raw_item, _TIMELINE_ITEM_ALLOW, "subagentTimeline.item")
        required_item = {
            "ref",
            "parentRef",
            "status",
            "timingQuality",
            "depth",
            "attempt",
        }
        if not required_item.issubset(raw_item):
            _timeline_invalid("subagentTimeline.item")

        item: dict[str, Any] = {
            "ref": _check_timeline_ref(raw_item["ref"], "subagentTimeline.item.ref"),
            "parentRef": _check_timeline_ref(
                raw_item["parentRef"], "subagentTimeline.item.parentRef"
            ),
        }
        status = raw_item["status"]
        timing_quality = raw_item["timingQuality"]
        if not isinstance(status, str) or status not in _TIMELINE_STATUSES:
            _timeline_invalid("subagentTimeline.item.status")
        if (
            not isinstance(timing_quality, str)
            or timing_quality not in _TIMELINE_QUALITIES
        ):
            _timeline_invalid("subagentTimeline.item.timingQuality")
        item["status"] = status
        item["timingQuality"] = timing_quality

        depth = raw_item["depth"]
        if not _is_strict_int(depth) or not 1 <= depth <= _MAX_TIMELINE_DEPTH:
            _timeline_invalid("subagentTimeline.item.depth")
        item["depth"] = depth

        attempt = raw_item["attempt"]
        if not _is_strict_int(attempt) or not 1 <= attempt <= _MAX_TIMELINE_ATTEMPT:
            _timeline_invalid("subagentTimeline.item.attempt")
        item["attempt"] = attempt

        if "name" in raw_item:
            item["name"] = _check_timeline_text(
                raw_item["name"],
                "subagentTimeline.item.name",
                max_length=_MAX_NAME,
                name_style=True,
            )
        if "agent" in raw_item:
            item["agent"] = _check_timeline_text(
                raw_item["agent"],
                "subagentTimeline.item.agent",
                max_length=_MAX_AGENT_MODEL,
            )
        if "model" in raw_item:
            item["model"] = _check_timeline_text(
                raw_item["model"],
                "subagentTimeline.item.model",
                max_length=_MAX_AGENT_MODEL,
            )
        if "modelVariant" in raw_item:
            item["modelVariant"] = _check_timeline_text(
                raw_item["modelVariant"],
                "subagentTimeline.item.modelVariant",
                max_length=_MAX_AGENT_MODEL,
            )

        offsets: dict[str, int | float] = {}
        for key in ("startOffsetMs", "endOffsetMs", "durationMs"):
            if key in raw_item:
                offsets[key] = _check_timeline_number(
                    raw_item[key], f"subagentTimeline.item.{key}"
                )
        start = offsets.get("startOffsetMs")
        end = offsets.get("endOffsetMs")
        duration = offsets.get("durationMs")
        if start is not None and end is not None and end < start:
            _timeline_invalid("subagentTimeline.item.endOffsetMs")
        if duration is not None and (start is None or end is None):
            _timeline_invalid("subagentTimeline.item.durationMs")
        if duration is not None and timing_quality == "partial":
            _timeline_invalid("subagentTimeline.item.durationMs")
        if start is not None and end is not None and duration is not None:
            if duration != end - start:
                _timeline_invalid("subagentTimeline.item.durationMs")

        has_start = start is not None
        has_end = end is not None
        if not has_start and not has_end and timing_quality != "unknown":
            _timeline_invalid("subagentTimeline.item.timingQuality")
        if has_start != has_end and timing_quality != "partial":
            _timeline_invalid("subagentTimeline.item.timingQuality")
        if has_start and has_end and timing_quality == "unknown":
            _timeline_invalid("subagentTimeline.item.timingQuality")
        if (
            has_start
            and has_end
            and timing_quality == "partial"
            and "clamped" not in reasons
        ):
            _timeline_invalid("subagentTimeline.item.timingQuality")
        if not has_start and "missing_start" not in reasons:
            _timeline_invalid()
        if not has_end and "missing_end" not in reasons:
            _timeline_invalid()

        item.update(offsets)
        items.append(item)

    result = {
        "version": 1,
        "partial": partial,
        "partialReasons": reasons,
        "timeBasis": "root_cycle",
        "observedItemCount": observed_count,
        "displayedItemCount": displayed_count,
        "truncated": truncated,
        "items": items,
    }
    timeline_size = 0
    try:
        timeline_size = len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError, OverflowError):
        _timeline_invalid()
    if timeline_size > _MAX_TIMELINE_BYTES:
        _timeline_invalid()
    return result


def _user_wait_invalid(field: str = "userWaitTimeline") -> NoReturn:
    raise ProviderError("invalid_payload", _safe_msg(field), retryable=False)


def _check_user_wait_offset(val: Any, field: str) -> int:
    """非负有限严格 int；bool/float/NaN/负数一律拒绝。"""
    if not _is_strict_int(val) or val < 0 or val > _MAX_TIMELINE_NUMBER:
        _user_wait_invalid(field)
    return val


def _validate_user_wait_interval(raw: Any) -> dict[str, Any]:
    """Validate one user-wait interval against the strict state combination contract.

    偏移边界决策：遵循既有 subagentTimeline 兼容模式 —— adapter 不校验 offset 是否
    超过 top-level ``durationMs``（越界由 renderer 防御性裁剪到 ``[0, duration]``），
    与 ``_validate_subagent_timeline`` 行为保持一致，避免同轴双 timeline 契约分叉。
    """
    if not isinstance(raw, dict):
        _user_wait_invalid("userWaitTimeline.interval")
    _check_unknown_fields(raw, _USER_WAIT_INTERVAL_ALLOW, "userWaitTimeline.interval")

    kind = raw.get("kind")
    state = raw.get("intervalState")
    if not isinstance(kind, str) or kind not in _USER_WAIT_KINDS:
        _user_wait_invalid("userWaitTimeline.interval.kind")
    if not isinstance(state, str) or state not in _USER_WAIT_STATES:
        _user_wait_invalid("userWaitTimeline.interval.intervalState")

    # 可选 offset 字段：必须为严格非负有限 int（bool/float/NaN/负数拒绝）
    offsets: dict[str, int] = {}
    for key in ("startOffsetMs", "endOffsetMs", "durationMs"):
        if key in raw:
            offsets[key] = _check_user_wait_offset(
                raw[key], f"userWaitTimeline.interval.{key}"
            )

    if state == "complete":
        # 必须且只能包含 kind,result,intervalState,start,end,duration；result 非空枚举
        required = {
            "kind",
            "result",
            "intervalState",
            "startOffsetMs",
            "endOffsetMs",
            "durationMs",
        }
        if set(raw) != required:
            _user_wait_invalid("userWaitTimeline.interval")
        result = raw["result"]
        if not isinstance(result, str) or result not in _USER_WAIT_RESULTS:
            _user_wait_invalid("userWaitTimeline.interval.result")
        start = offsets["startOffsetMs"]
        end = offsets["endOffsetMs"]
        duration = offsets["durationMs"]
        if end < start:
            _user_wait_invalid("userWaitTimeline.interval.endOffsetMs")
        if duration != end - start:
            _user_wait_invalid("userWaitTimeline.interval.durationMs")
        return {
            "kind": kind,
            "result": result,
            "intervalState": state,
            "startOffsetMs": start,
            "endOffsetMs": end,
            "durationMs": duration,
        }

    if state == "right_censored":
        # 必须且只能包含 kind,intervalState,start；result/end/duration 省略
        required = {"kind", "intervalState", "startOffsetMs"}
        if set(raw) != required:
            _user_wait_invalid("userWaitTimeline.interval")
        return {
            "kind": kind,
            "intervalState": state,
            "startOffsetMs": offsets["startOffsetMs"],
        }

    # left_censored：必须且只能包含 kind,result,intervalState,end；result 非空枚举
    required = {"kind", "result", "intervalState", "endOffsetMs"}
    if set(raw) != required:
        _user_wait_invalid("userWaitTimeline.interval")
    result = raw["result"]
    if not isinstance(result, str) or result not in _USER_WAIT_RESULTS:
        _user_wait_invalid("userWaitTimeline.interval.result")
    return {
        "kind": kind,
        "result": result,
        "intervalState": state,
        "endOffsetMs": offsets["endOffsetMs"],
    }


def _validate_user_wait_timeline(raw: Any) -> dict[str, Any]:
    """Validate and copy the user-wait timeline envelope (strict contract).

    顶层输入 ``userWaitTimeline``；可选缺失表示旧 client，空完整 timeline 表示可靠 0。
    """
    if not isinstance(raw, dict):
        _user_wait_invalid()
    _check_unknown_fields(raw, _USER_WAIT_ALLOW, "userWaitTimeline")
    required = {
        "version",
        "timeBasis",
        "partial",
        "partialReasons",
        "observedIntervalCount",
        "displayedIntervalCount",
        "truncated",
        "intervals",
    }
    if not required.issubset(raw):
        _user_wait_invalid()

    if not _is_strict_int(raw["version"]) or raw["version"] != 1:
        _user_wait_invalid("userWaitTimeline.version")
    if raw["timeBasis"] != "root_cycle_receipt_monotonic":
        _user_wait_invalid("userWaitTimeline.timeBasis")
    partial = raw["partial"]
    truncated = raw["truncated"]
    if not isinstance(partial, bool):
        _user_wait_invalid("userWaitTimeline.partial")
    if not isinstance(truncated, bool):
        _user_wait_invalid("userWaitTimeline.truncated")

    raw_reasons = raw["partialReasons"]
    if (
        not isinstance(raw_reasons, list)
        or len(raw_reasons) > _MAX_TIMELINE_REASONS
        or any(reason not in _USER_WAIT_REASONS for reason in raw_reasons)
        or len(set(raw_reasons)) != len(raw_reasons)
        or tuple(raw_reasons)
        != tuple(reason for reason in _USER_WAIT_REASONS if reason in raw_reasons)
    ):
        _user_wait_invalid("userWaitTimeline.partialReasons")
    reasons = list(raw_reasons)

    observed_count = _check_timeline_count(
        raw["observedIntervalCount"],
        "userWaitTimeline.observedIntervalCount",
        _MAX_TIMELINE_OBSERVED_ITEMS,
    )
    displayed_count = _check_timeline_count(
        raw["displayedIntervalCount"],
        "userWaitTimeline.displayedIntervalCount",
        _MAX_TIMELINE_ITEMS,
    )
    if displayed_count > observed_count:
        _user_wait_invalid("userWaitTimeline.displayedIntervalCount")

    raw_intervals = raw["intervals"]
    if not isinstance(raw_intervals, list) or len(raw_intervals) > _MAX_TIMELINE_ITEMS:
        _user_wait_invalid("userWaitTimeline.intervals")
    if len(raw_intervals) != displayed_count:
        _user_wait_invalid("userWaitTimeline.intervals")

    # 一致性：partial 与不完整证据双向绑定
    if partial != bool(reasons):
        _user_wait_invalid()
    if truncated and (not partial or "truncated" not in reasons):
        _user_wait_invalid()
    if "truncated" in reasons and not truncated:
        _user_wait_invalid()
    if truncated and not (observed_count > displayed_count):
        _user_wait_invalid()

    intervals: list[dict[str, Any]] = []
    has_right_censored = False
    has_left_censored = False
    for raw_interval in raw_intervals:
        interval = _validate_user_wait_interval(raw_interval)
        state = interval["intervalState"]
        if state == "right_censored":
            has_right_censored = True
        elif state == "left_censored":
            has_left_censored = True
        intervals.append(interval)

    # censored interval 也要求 partial=true
    if (has_right_censored or has_left_censored) and not partial:
        _user_wait_invalid()

    # reason 强绑定（仅 truncated=false 时）：open_at_cycle_end→至少一个
    # right_censored；orphan_resolution→至少一个 left_censored；其他 reason
    # 不强绑具体 interval。truncated=true 时对应 censored interval 可能已被
    # 截断出 displayed 列表（Oracle Gate1 P1），允许 reason 存在而 displayed
    # 中无对应 censored；truncated=false 时保留严格绑定。
    if not truncated:
        if "open_at_cycle_end" in reasons and not has_right_censored:
            _user_wait_invalid()
        if "orphan_resolution" in reasons and not has_left_censored:
            _user_wait_invalid()

    result = {
        "version": 1,
        "timeBasis": "root_cycle_receipt_monotonic",
        "partial": partial,
        "partialReasons": reasons,
        "observedIntervalCount": observed_count,
        "displayedIntervalCount": displayed_count,
        "truncated": truncated,
        "intervals": intervals,
    }
    timeline_size = 0
    try:
        timeline_size = len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError, OverflowError):
        _user_wait_invalid()
    if timeline_size > _MAX_USER_WAIT_TIMELINE_BYTES:
        _user_wait_invalid()
    return result


# ─── Session Name 清洗 ─────────────────────────────────────


def _clean_session_name(raw: str | None) -> str | None:
    """清洗 session.name：trim、Unicode 危险字符移除、控制字符归一、连续空白压缩。"""
    if raw is None:
        return None
    s = str(raw).strip()
    # 移除 Unicode Bidi 控制字符、零宽/format chars、行/段分隔符
    s = _strip_dangerous_unicode(s)
    # 控制字符/CR/LF/TAB 替换为空格
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\r\n\t]", " ", s)
    # 压缩连续空白
    s = re.sub(r" {2,}", " ", s).strip()
    if not s:
        return None
    # 长度限制
    if len(s) > _MAX_NAME:
        s = s[:_MAX_NAME].rstrip()
    return s if s else None


def _build_ref12(ref: str) -> str:
    """从 session.ref 构建最多 12 字符的安全标识。

    仅保留 a-zA-Z0-9._-，其余换 `-`。
    """
    safe = ""
    for ch in ref:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            safe += ch
        else:
            safe += "-"
    return safe[:12] if len(safe) > 12 else safe


def _build_display_name(session_name: str | None, ref12: str) -> str:
    """构建展示名：清洗后的 name，或 ``OpenCode Session <ref12>``。"""
    cleaned = _clean_session_name(session_name)
    if cleaned:
        return cleaned
    return f"OpenCode Session {ref12}" if ref12 else "OpenCode Session"


# ─── 顶层 payload 校验 ─────────────────────────────────────


def _check_headers(headers: dict[str, str], body: dict[str, Any]) -> str:
    """检查 X-OpenCode-Event Header 与 body event 的一致性。"""
    headers_lower = {k.lower(): v for k, v in headers.items()}
    header_event = headers_lower.get("x-opencode-event", "").strip()
    body_event = body.get("event")

    if not header_event:
        raise ProviderError(
            "invalid_payload", "缺少 X-OpenCode-Event 请求头", retryable=False
        )
    if body_event is None:
        raise ProviderError("invalid_payload", "缺少 event 字段", retryable=False)
    # 双方都提供后比较
    if not isinstance(body_event, str) or header_event != body_event:
        raise ProviderError(
            "event_mismatch", "X-OpenCode-Event 与 body event 不匹配", retryable=False
        )
    return header_event


def _check_unknown_fields(
    payload: dict[str, Any],
    allow: frozenset[str],
    label: str,
) -> None:
    """检查未知字段，出现则直接拒绝。"""
    for key in payload:
        if key not in allow:
            raise ProviderError(
                "invalid_payload",
                "不允许的字段",
                retryable=False,
            )


def _validate_counts(raw: Any) -> dict[str, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProviderError("invalid_payload", _safe_msg("counts"), retryable=False)
    _check_unknown_fields(raw, _COUNTS_ALLOW, "counts")
    result: dict[str, int] = {}
    for key in _COUNTS_ALLOW:
        value = _check_action_count(raw.get(key), "counts")
        if value is not None:
            result[key] = value
    return result or None


def _validate_permission(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProviderError("invalid_payload", _safe_msg("permission"), retryable=False)

    # Low-cost rolling-upgrade compatibility: accept the previous single-item
    # shape and normalize it to the aggregate contract before validation.
    if "count" not in raw and "items" not in raw and "category" in raw:
        _check_unknown_fields(raw, _PERMISSION_LEGACY_ALLOW, "permission")
        return {"count": 1, "items": [_validate_permission_item(raw)]}

    _check_unknown_fields(raw, _PERMISSION_ALLOW, "permission")
    count = _check_action_count(raw.get("count"), "permission.count")
    items = raw.get("items")
    if count is None or not isinstance(items, list) or not items or count < len(items):
        raise ProviderError("invalid_payload", _safe_msg("permission"), retryable=False)
    if len(items) > _MAX_PERMISSION_ITEMS:
        raise ProviderError(
            "invalid_payload", _safe_msg("permission.items"), retryable=False
        )
    return {
        "count": count,
        "items": [_validate_permission_item(item) for item in items],
    }


def _validate_permission_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProviderError(
            "invalid_payload", _safe_msg("permission.items"), retryable=False
        )
    _check_unknown_fields(raw, _PERMISSION_ITEM_ALLOW, "permission.items")
    result: dict[str, Any] = {
        "category": _check_category(raw.get("category"), "permission.category")
    }
    for key in ("title", "description", "action", "target"):
        value = _check_optional_action_text(raw.get(key), f"permission.{key}")
        if value is not None:
            result[key] = value
    summary = _check_optional_action_text(
        raw.get("summary"), "permission.summary", max_length=_MAX_ACTION_SUMMARY
    )
    if summary is not None:
        result["summary"] = summary
    patterns = raw.get("patterns")
    if patterns is not None:
        if not isinstance(patterns, list) or len(patterns) > _MAX_PERMISSION_PATTERNS:
            raise ProviderError(
                "invalid_payload", _safe_msg("permission.patterns"), retryable=False
            )
        clean_patterns = [
            _check_action_text(pattern, "permission.patterns") for pattern in patterns
        ]
        if clean_patterns:
            result["patterns"] = clean_patterns
    return result


def _validate_question(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProviderError("invalid_payload", _safe_msg("question"), retryable=False)
    _check_unknown_fields(raw, _QUESTION_ALLOW, "question")
    result: dict[str, Any] = {}
    for key in ("count", "optionCount"):
        value = _check_action_count(raw.get(key), f"question.{key}")
        if value is not None:
            result[key] = value
    summary = _check_optional_action_text(
        raw.get("summary"), "question.summary", max_length=_MAX_ACTION_SUMMARY
    )
    if summary is not None:
        result["summary"] = summary

    items = raw.get("items")
    if items is not None:
        if not isinstance(items, list) or len(items) > _MAX_ACTION_ITEMS:
            raise ProviderError(
                "invalid_payload", _safe_msg("question.items"), retryable=False
            )
        clean_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ProviderError(
                    "invalid_payload", _safe_msg("question.items"), retryable=False
                )
            _check_unknown_fields(item, _QUESTION_ITEM_ALLOW, "question.items")
            clean_item: dict[str, Any] = {}
            for key in ("text", "header"):
                value = _check_optional_action_text(item.get(key), "question.items")
                if value is not None:
                    clean_item[key] = value
            if "recommended" in item:
                clean_item["recommended"] = _check_action_scalar(
                    item["recommended"], "question.items"
                )
            options = item.get("options")
            if options is not None:
                if not isinstance(options, list) or len(options) > _MAX_ACTION_OPTIONS:
                    raise ProviderError(
                        "invalid_payload", _safe_msg("question.items"), retryable=False
                    )
                clean_options: list[dict[str, Any]] = []
                for option in options:
                    if not isinstance(option, dict):
                        raise ProviderError(
                            "invalid_payload",
                            _safe_msg("question.items"),
                            retryable=False,
                        )
                    _check_unknown_fields(
                        option, _QUESTION_OPTION_ALLOW, "question.items"
                    )
                    clean_option: dict[str, Any] = {}
                    for key in ("label", "description"):
                        value = _check_optional_action_text(
                            option.get(key), "question.items"
                        )
                        if value is not None:
                            clean_option[key] = value
                    if "recommended" in option:
                        clean_option["recommended"] = _check_action_scalar(
                            option["recommended"], "question.items"
                        )
                    if clean_option:
                        clean_options.append(clean_option)
                if clean_options:
                    clean_item["options"] = clean_options
            if clean_item:
                clean_items.append(clean_item)
        if clean_items:
            result["items"] = clean_items
    if not result:
        raise ProviderError("invalid_payload", _safe_msg("question"), retryable=False)
    return result


def _format_duration_ms(duration_ms: int) -> str:
    """将毫秒转换成稳定、适合标准化字段的可读时长。"""
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    total_seconds = duration_ms / 1000
    if total_seconds < 60:
        seconds_text = f"{total_seconds:.1f}".rstrip("0").rstrip(".")
        return f"{seconds_text}s"
    total_seconds_int = int(total_seconds)
    minutes, seconds = divmod(total_seconds_int, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _format_timestamp_for_display(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00", 1))
    except (ValueError, TypeError):
        return value
    if dt.tzinfo is None:
        return value
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ─── 主解析 ────────────────────────────────────────────────


class OpenCodeProviderAdapter(ProviderAdapter):
    """OpenCode V1 provider。

    只接收第一方 ``webhook-notifier.ts`` Plugin 转换后的稳定 Envelope。
    不解析 OpenCode 原始 event object。
    """

    @property
    def provider(self) -> str:
        return _OPENCODE_KEY

    def parse(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        received_at: str,
    ) -> NormalizedEvent:
        if not isinstance(payload, dict):
            raise ProviderError(
                "invalid_payload", "请求体必须是 JSON 对象", retryable=False
            )
        try:
            payload_size = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except (TypeError, ValueError, OverflowError):
            raise ProviderError(
                "invalid_payload", "请求体不是有效 JSON", retryable=False
            ) from None
        if payload_size > _MAX_PAYLOAD_BYTES:
            raise ProviderError(
                "invalid_payload", "请求体超过大小限制", retryable=False
            )

        # 0. 拒绝异源 provider payload（OMP）
        headers_lower = {k.lower(): v for k, v in headers.items()}
        if "x-omp-event" in headers_lower:
            raise ProviderError(
                "provider_incompatible", "不兼容的 provider 请求", retryable=False
            )
        body_event_raw = payload.get("event")
        if isinstance(body_event_raw, str):
            body_event_lower = body_event_raw.strip().lower()
            if body_event_lower in (
                "omp.session_stop",
                "session_stop",
            ) or body_event_lower.startswith("omp."):
                raise ProviderError(
                    "provider_incompatible", "不兼容的 provider 请求", retryable=False
                )

        # 1. Header/Body event 一致性
        event = _check_headers(headers, payload)

        # 2. 顶层 allowlist
        _check_unknown_fields(
            payload,
            frozenset(
                {
                    "id",
                    "event",
                    "version",
                    "emittedAt",
                    "session",
                    "agent",
                    "model",
                    "modelVariant",
                    "durationMs",
                    "instanceDisplayName",
                    "projectName",
                    "startedAt",
                    "taskStartedAt",
                    "endedAt",
                    "counts",
                    "permission",
                    "question",
                    "error",
                    "subagentTimeline",
                    "userWaitTimeline",
                }
            ),
            "payload",
        )

        # 3. 必填标量
        env_id = _check_id(payload.get("id"))
        _check_event(
            payload.get("event")
        )  # 已由 _check_headers 隐式检查，但保持显式 schema 校验
        _check_version(payload.get("version"))
        emitted_at = _check_emitted_at(payload.get("emittedAt"))

        # 4. session object
        session_raw = payload.get("session")
        if not isinstance(session_raw, dict):
            raise ProviderError(
                "invalid_payload", _safe_msg("session"), retryable=False
            )
        _check_unknown_fields(session_raw, _SESSION_ALLOW, "session")
        session_ref = _check_session_ref(session_raw.get("ref"))
        session_name_raw = _check_session_name(session_raw.get("name"))
        session_scope = (
            _check_session_scope(session_raw["scope"])
            if "scope" in session_raw
            else SessionScope.UNKNOWN
        )

        subagent_timeline: dict[str, Any] | None = None
        if "subagentTimeline" in payload:
            if event != "opencode.session_idle" or session_scope != SessionScope.ROOT:
                _timeline_invalid()
            subagent_timeline = _validate_subagent_timeline(payload["subagentTimeline"])

        # 5. 可选标量
        agent = _check_agent_or_model(payload.get("agent"), "agent")
        model = _check_agent_or_model(payload.get("model"), "model")
        model_variant = _check_model_variant(payload.get("modelVariant"))
        duration_ms = _check_duration_ms(payload.get("durationMs"))

        user_wait_timeline: dict[str, Any] | None = None
        if "userWaitTimeline" in payload:
            # 只允许 root session_idle 携带；非 root、error、question/permission action 均拒绝
            if event != "opencode.session_idle" or session_scope != SessionScope.ROOT:
                _user_wait_invalid()
            user_wait_timeline = _validate_user_wait_timeline(
                payload["userWaitTimeline"]
            )
        instance_display_name = _clean_session_name(
            _check_session_name(payload.get("instanceDisplayName"))
        )
        project_name = _clean_session_name(
            _check_session_name(payload.get("projectName"))
        )
        started_at = _check_optional_timestamp(payload.get("startedAt"), "startedAt")
        task_started_at = _check_optional_timestamp(
            payload.get("taskStartedAt"), "taskStartedAt"
        )
        ended_at = _check_optional_timestamp(payload.get("endedAt"), "endedAt")
        counts = _validate_counts(payload.get("counts"))

        # 6. 事件特有校验
        permission_raw = payload.get("permission")
        question_raw = payload.get("question")
        error_raw = payload.get("error")

        if event == "opencode.permission_asked":
            permission_raw = _validate_permission(permission_raw)
            if "question" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("question"), retryable=False
                )
            if "error" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("error"), retryable=False
                )

        elif event == "opencode.session_error":
            if not isinstance(error_raw, dict):
                raise ProviderError(
                    "invalid_payload", _safe_msg("error"), retryable=False
                )
            _check_unknown_fields(error_raw, _ERROR_ALLOW, "error")
            _check_category(error_raw.get("category"), "error.category")
            # code 可选
            err_code = error_raw.get("code")
            if err_code is not None:
                if not _is_nonempty_str(err_code) or len(err_code) > _MAX_CATEGORY_CODE:
                    raise ProviderError(
                        "invalid_payload", _safe_msg("error.code"), retryable=False
                    )
            if "permission" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("permission"), retryable=False
                )
            if "question" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("question"), retryable=False
                )

        elif event == "opencode.session_idle":
            if "permission" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("permission"), retryable=False
                )
            if "error" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("error"), retryable=False
                )
            if "question" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("question"), retryable=False
                )

        elif event == "opencode.question_asked":
            if "permission" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("permission"), retryable=False
                )
            if "error" in payload:
                raise ProviderError(
                    "invalid_payload", _safe_msg("error"), retryable=False
                )
            if "question" in payload:
                question_raw = _validate_question(question_raw)

        # 7. 构建 NormalizedEvent
        return self._build_event(
            env_id=env_id,
            event=event,
            emitted_at=emitted_at,
            session_ref=session_ref,
            session_name_raw=session_name_raw,
            session_scope=session_scope,
            agent=agent,
            model=model,
            model_variant=model_variant,
            duration_ms=duration_ms,
            instance_display_name=instance_display_name,
            project_name=project_name,
            started_at=started_at,
            task_started_at=task_started_at,
            ended_at=ended_at,
            counts=counts,
            permission_raw=permission_raw,
            question_raw=question_raw,
            error_raw=error_raw,
            subagent_timeline=subagent_timeline,
            user_wait_timeline=user_wait_timeline,
        )

    @staticmethod
    def _build_event(
        *,
        env_id: str,
        event: str,
        emitted_at: str,
        session_ref: str,
        session_name_raw: str | None,
        session_scope: SessionScope,
        agent: str | None,
        model: str | None,
        model_variant: str | None,
        duration_ms: int | None,
        instance_display_name: str | None,
        project_name: str | None,
        started_at: str | None,
        task_started_at: str | None,
        ended_at: str | None,
        counts: dict[str, int] | None,
        permission_raw: dict[str, Any] | None,
        question_raw: dict[str, Any] | None,
        error_raw: dict[str, Any] | None,
        subagent_timeline: dict[str, Any] | None,
        user_wait_timeline: dict[str, Any] | None,
    ) -> NormalizedEvent:
        # status
        status_map = {
            "opencode.session_idle": "completed",
            "opencode.session_error": "failed",
            "opencode.permission_asked": "action_required",
            "opencode.question_asked": "action_required",
        }
        status = status_map.get(event, "completed")

        # 显示名
        ref12 = _build_ref12(session_ref)
        display_name = _build_display_name(session_name_raw, ref12)

        # summary — 固定安全文本
        summary_map = {
            "opencode.session_idle": "会话完成",
            "opencode.session_error": "会话出错",
            "opencode.permission_asked": "等待权限批准",
            "opencode.question_asked": "等待问题回答",
        }
        summary = summary_map.get(event, "")

        # fields — 仅 allowlist，sessionRef 使用 ref12（全 ref 不进入 NormalizedEvent）
        fields: list[dict[str, Any]] = []
        fields.append({"label": "sessionName", "value": display_name, "short": False})
        if project_name:
            fields.append(
                {"label": "projectName", "value": project_name, "short": True}
            )
        if agent:
            fields.append({"label": "agent", "value": agent, "short": True})
        if model:
            fields.append({"label": "model", "value": model, "short": True})
        if model_variant:
            fields.append(
                {"label": "modelVariant", "value": model_variant, "short": True}
            )
        if duration_ms is not None:
            fields.append(
                {"label": "durationMs", "value": str(duration_ms), "short": True}
            )
            fields.append(
                {
                    "label": "duration",
                    "value": _format_duration_ms(duration_ms),
                    "short": True,
                }
            )
        if started_at:
            fields.append(
                {
                    "label": "startedAt",
                    "value": started_at,
                    "short": True,
                }
            )
        if task_started_at:
            fields.append(
                {
                    "label": "taskStartedAt",
                    "value": task_started_at,
                    "short": True,
                }
            )
        if ended_at:
            fields.append(
                {
                    "label": "endedAt",
                    "value": ended_at,
                    "short": True,
                }
            )
        if counts:
            count_labels = (
                ("messages", "messageCount"),
                ("tools", "toolCount"),
                ("changes", "changeCount"),
            )
            for count_key, label in count_labels:
                if count_key in counts:
                    fields.append(
                        {"label": label, "value": str(counts[count_key]), "short": True}
                    )
        if ref12:
            fields.append({"label": "sessionRef", "value": ref12, "short": True})
        if permission_raw:
            permission_count = permission_raw.get("count")
            if permission_count is not None:
                fields.append(
                    {
                        "label": "permissionCount",
                        "value": str(permission_count),
                        "short": True,
                    }
                )
            permission_items = permission_raw.get("items", [])
            for index, item in enumerate(permission_items, start=1):
                cat = str(item.get("category", ""))
                fields.append(
                    {
                        "label": f"permission[{index}].category",
                        "value": cat,
                        "short": True,
                    }
                )
                for key in ("summary", "title", "description", "action", "target"):
                    value = item.get(key)
                    if value:
                        fields.append(
                            {
                                "label": f"permission[{index}].{key}",
                                "value": value,
                                "short": key in {"summary", "title", "action"},
                            }
                        )
                patterns = item.get("patterns")
                if patterns:
                    fields.append(
                        {
                            "label": f"permission[{index}].patterns",
                            "value": ", ".join(patterns),
                            "short": False,
                        }
                    )
        if question_raw:
            question_count = question_raw.get("count")
            if question_count is not None:
                fields.append(
                    {
                        "label": "questionCount",
                        "value": str(question_count),
                        "short": True,
                    }
                )
            option_count = question_raw.get("optionCount")
            if option_count is not None:
                fields.append(
                    {"label": "optionCount", "value": str(option_count), "short": True}
                )
            question_summary = question_raw.get("summary")
            if question_summary:
                fields.append(
                    {
                        "label": "question.summary",
                        "value": question_summary,
                        "short": False,
                    }
                )
            for index, item in enumerate(question_raw.get("items", []), start=1):
                if item.get("header"):
                    fields.append(
                        {
                            "label": f"question[{index}].header",
                            "value": item["header"],
                            "short": True,
                        }
                    )
                if item.get("text"):
                    fields.append(
                        {
                            "label": f"question[{index}]",
                            "value": item["text"],
                            "short": False,
                        }
                    )
                if "recommended" in item:
                    fields.append(
                        {
                            "label": f"question[{index}].recommended",
                            "value": str(item["recommended"]),
                            "short": True,
                        }
                    )
                option_text: list[str] = []
                for option in item.get("options", []):
                    label = str(option.get("label", ""))
                    description = option.get("description")
                    recommendation = option.get("recommended")
                    detail = label
                    if description:
                        detail = (
                            f"{detail}: {description}" if detail else str(description)
                        )
                    if recommendation is not None:
                        detail = f"{detail} (recommended={recommendation})"
                    if detail:
                        option_text.append(detail)
                if option_text:
                    fields.append(
                        {
                            "label": f"question[{index}].options",
                            "value": " | ".join(option_text),
                            "short": False,
                        }
                    )
        if error_raw:
            cat = str(error_raw.get("category", ""))
            fields.append({"label": "error.category", "value": cat, "short": True})
            code = error_raw.get("code")
            if code is not None:
                fields.append(
                    {"label": "error.code", "value": str(code), "short": True}
                )

        # actor — 可用 agent 安全值
        actor_name = agent if agent else None

        return NormalizedEvent(
            provider=_OPENCODE_KEY,
            event=event,
            version=_OPENCODE_VERSION,
            id=env_id,
            emitted_at=emitted_at,
            title=display_name,
            status=status,
            session_scope=session_scope,
            summary=summary,
            source={
                "name": instance_display_name or "OpenCode",
                "url": None,
            },
            actor={"name": actor_name, "url": None},
            model_variant=model_variant,
            fields=fields,
            links=[],
            raw={},
            subagent_timeline=subagent_timeline,
            user_wait_timeline=user_wait_timeline,
            # 仅使用严格校验后的 payload durationMs（busy→idle 当前任务耗时）
            task_duration_ms=duration_ms if duration_ms is not None else None,
        )
