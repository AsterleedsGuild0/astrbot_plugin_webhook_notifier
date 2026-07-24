from __future__ import annotations

import re
import math
import json
from datetime import datetime, timezone
from typing import Any

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

        # 5. 可选标量
        agent = _check_agent_or_model(payload.get("agent"), "agent")
        model = _check_agent_or_model(payload.get("model"), "model")
        model_variant = _check_model_variant(payload.get("modelVariant"))
        duration_ms = _check_duration_ms(payload.get("durationMs"))
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
        )
