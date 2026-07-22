from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import NormalizedEvent
from .providers import ProviderAdapter, ProviderError

_OPENCODE_KEY = "opencode"
_OPENCODE_VERSION = 1

# ─── 字段长度约束 ──────────────────────────────────────────

_MAX_ID_REF = 128
_MAX_NAME = 200  # Unicode chars
_MAX_AGENT_MODEL = 128
_MAX_CATEGORY_CODE = 64
_MAX_DURATION_MS = 604800000  # 7 days
_MIN_DURATION_MS = 0

# ─── 允许字段 ──────────────────────────────────────────────

_SESSION_ALLOW = frozenset({"ref", "name"})
_PERMISSION_ALLOW = frozenset({"category"})
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
    "agent": "无效的 agent 字段",
    "model": "无效的 model 字段",
    "durationMs": "无效的 durationMs 字段",
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
                f"不允许的字段",
                retryable=False,
            )


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
                    "durationMs",
                    "permission",
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

        # 5. 可选标量
        agent = _check_agent_or_model(payload.get("agent"), "agent")
        model = _check_agent_or_model(payload.get("model"), "model")
        duration_ms = _check_duration_ms(payload.get("durationMs"))

        # 6. 事件特有校验
        permission_raw = payload.get("permission")
        error_raw = payload.get("error")

        if event == "opencode.permission_asked":
            if not isinstance(permission_raw, dict):
                raise ProviderError(
                    "invalid_payload", _safe_msg("permission"), retryable=False
                )
            _check_unknown_fields(permission_raw, _PERMISSION_ALLOW, "permission")
            _check_category(permission_raw.get("category"), "permission.category")
            if error_raw is not None:
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
            if permission_raw is not None:
                raise ProviderError(
                    "invalid_payload", _safe_msg("permission"), retryable=False
                )

        elif event == "opencode.session_idle":
            if permission_raw is not None:
                raise ProviderError(
                    "invalid_payload", _safe_msg("permission"), retryable=False
                )
            if error_raw is not None:
                raise ProviderError(
                    "invalid_payload", _safe_msg("error"), retryable=False
                )

        # 7. 构建 NormalizedEvent
        return self._build_event(
            env_id=env_id,
            event=event,
            emitted_at=emitted_at,
            session_ref=session_ref,
            session_name_raw=session_name_raw,
            agent=agent,
            model=model,
            duration_ms=duration_ms,
            permission_raw=permission_raw,
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
        agent: str | None,
        model: str | None,
        duration_ms: int | None,
        permission_raw: dict[str, Any] | None,
        error_raw: dict[str, Any] | None,
    ) -> NormalizedEvent:
        # status
        status_map = {
            "opencode.session_idle": "completed",
            "opencode.session_error": "failed",
            "opencode.permission_asked": "action_required",
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
        }
        summary = summary_map.get(event, "")

        # fields — 仅 allowlist，sessionRef 使用 ref12（全 ref 不进入 NormalizedEvent）
        fields: list[dict[str, Any]] = []
        if agent:
            fields.append({"label": "agent", "value": agent, "short": True})
        if model:
            fields.append({"label": "model", "value": model, "short": True})
        if duration_ms is not None:
            fields.append(
                {"label": "durationMs", "value": str(duration_ms), "short": True}
            )
        if ref12:
            fields.append({"label": "sessionRef", "value": ref12, "short": True})
        if permission_raw:
            cat = str(permission_raw.get("category", ""))
            fields.append({"label": "permission.category", "value": cat, "short": True})
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
            summary=summary,
            source={"name": display_name, "url": None},
            actor={"name": actor_name, "url": None},
            fields=fields,
            links=[],
            raw={},
        )
