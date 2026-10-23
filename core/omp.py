from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .models import NormalizedEvent
from .notification_policy import SessionScope
from .providers import ProviderAdapter, ProviderError

OMP_SOURCE_NAME = "oh-my-pi"
_OMP_ADAPTER_ERROR_MSG = "OMP payload 处理失败"
_ANON_KEY = secrets.token_bytes(32)
_V2_EVENTS = {"omp.run_finished", "omp.action_required"}
_RESULTS = {"completed", "failed", "aborted", "action_required", "unknown"}
_REASONS = {
    "missing_start", "session_changed", "shutdown", "expired", "capacity",
    "unpaired_interaction", "incomplete_metadata", "clock_changed",
}
_TITLES = {
    "completed": "运行正常结束", "failed": "运行失败", "aborted": "运行已中止",
    "action_required": "需要处理", "unknown": "运行结果未知",
}


def _safe_name(value: Any, limit: int = 96, *, model: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = "".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cf"}).strip()
    if re.search(r"https?://|(?:^|\s)(?:[A-Za-z]:[\\/]|[~\\/])", value):
        return ""
    if not model and ("/" in value or "\\" in value):
        return ""
    return value[:limit]


def _object(value: Any, required: set[str], optional: set[str]) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError("shape")
    return value


def _ref(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{32}", value) is None:
        raise ValueError("reference")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone")
    return result


def _bounded_text(value: Any, limit: int = 96, *, model: bool = False) -> None:
    if not isinstance(value, str) or not value or _safe_name(value, limit, model=model) != value:
        raise ValueError("name")


def _normalize_v2(body: dict[str, Any]) -> NormalizedEvent:
    _object(body, {"event", "version", "id", "emittedAt", "session", "cycle", "status"},
            {"instance", "interaction", "target_alias"})
    if type(body["version"]) is not int or body["version"] != 2:
        raise ValueError("version")
    if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 65536:
        raise ValueError("size")
    _ref(body["id"])
    emitted = _timestamp(body["emittedAt"])
    status = body["status"]
    if not isinstance(status, str) or status not in _RESULTS:
        raise ValueError("status")
    action = body["event"] == "omp.action_required"
    if action != (status == "action_required") or action != ("interaction" in body):
        raise ValueError("event_status")
    session = _object(body["session"], {"ref", "scope"}, {"name", "cwdName", "agent"})
    _ref(session["ref"])
    if session["scope"] not in ("root", "subagent", "unknown"):
        raise ValueError("scope")
    for key in ("name", "cwdName", "agent"):
        if key in session:
            _bounded_text(session[key])
    for key, limit in (("instance", 96), ("target_alias", 128)):
        if key in body:
            _bounded_text(body[key], limit)
    cycle = _object(body["cycle"], {"ref", "partial", "reasons"},
                    {"startedAt", "endedAt", "durationMs", "model", "thinking", "metadataChanged"})
    _ref(cycle["ref"])
    reasons = cycle["reasons"]
    if (type(cycle["partial"]) is not bool or not isinstance(reasons, list)
            or len(reasons) > len(_REASONS)
            or any(not isinstance(r, str) or r not in _REASONS for r in reasons)
            or len(set(reasons)) != len(reasons) or cycle["partial"] != bool(reasons)):
        raise ValueError("partial")
    start = _timestamp(cycle["startedAt"]) if "startedAt" in cycle else None
    end = _timestamp(cycle["endedAt"]) if "endedAt" in cycle else None
    if action and end is not None or not action and end is None:
        raise ValueError("end")
    if start is None and "missing_start" not in reasons:
        raise ValueError("missing_start")
    if "durationMs" in cycle:
        duration = cycle["durationMs"]
        if type(duration) is not int or not 0 <= duration <= 86400000 or start is None:
            raise ValueError("duration")
        if abs(((end or emitted) - start).total_seconds() * 1000 - duration) > 1:
            raise ValueError("duration_bounds")
    for key, limit in (("model", 128), ("thinking", 32)):
        if key in cycle:
            _bounded_text(cycle[key], limit, model=key == "model")
    if "metadataChanged" in cycle and type(cycle["metadataChanged"]) is not bool:
        raise ValueError("metadata")
    if cycle.get("metadataChanged") and ("model" in cycle or "thinking" in cycle):
        raise ValueError("mixed_metadata")
    fields = []
    for label, value in (
        ("实例", body.get("instance")),
        ("sessionName", session.get("name") or f"OMP Session {session['ref'][:12]}"),
        ("projectName", session.get("cwdName")), ("agent", session.get("agent")),
        ("model", cycle.get("model")), ("实际 thinking", cycle.get("thinking")),
        ("taskStartedAt", cycle.get("startedAt")), ("durationMs", cycle.get("durationMs")),
    ):
        if value is not None:
            fields.append({"label": label, "value": value, "short": True})
    if cycle.get("metadataChanged"):
        fields.append({"label": "模型与档位", "value": "运行中发生变化，省略单一值", "short": False})
    if cycle["partial"]:
        fields.append({"label": "观察范围", "value": "不完整", "short": True})
    summary = "正常结束不代表业务目标已验证完成。" if status == "completed" else ""
    if action:
        interaction = _object(body["interaction"], {"ref", "kind"}, set())
        _ref(interaction["ref"])
        labels = {"approval": "审批请求（不证明 UI 已呈现）",
                  "plan_review": "计划审阅已打开（区间可能包含处理时间）",
                  "ask": "ask 调用待解决（不证明 UI 已呈现）"}
        if interaction["kind"] not in labels:
            raise ValueError("interaction_kind")
        summary = labels[interaction["kind"]]
    return NormalizedEvent(
        provider="omp", event=body["event"], version=2, id=body["id"],
        emitted_at=body["emittedAt"], title=_TITLES[status], status=status,
        session_scope=SessionScope(session["scope"]), summary=summary,
        source={"name": OMP_SOURCE_NAME, "url": None}, fields=fields,
        task_duration_ms=cycle.get("durationMs") if not action else None,
    )


def is_omp_session_stop(
    headers: dict[str, str], body: dict[str, Any]
) -> tuple[bool, str]:
    """判断请求是否兼容 OMP Hook 的 session_stop V1 输出。

    Args:
        headers: HTTP 请求头（小写 key）。
        body: 解析后的 JSON body。

    Returns:
        (is_valid, error_message)
        如果 isValid=True，error_message 为空字符串。
        如果 isValid=False，error_message 为错误描述。
    """
    header_event = None
    body_event = None

    # 读取 OMP Hook 使用的 header（大小写不敏感）
    headers_lower = {k.lower(): v for k, v in headers.items()}
    raw_header = headers_lower.get("x-omp-event", "")
    if raw_header:
        header_event = raw_header.strip().lower()

    # 读取 body
    body_event_raw = body.get("event", "")
    if isinstance(body_event_raw, str) and body_event_raw.strip():
        body_event = body_event_raw.strip().lower()

    # 语义一致映射
    def normalize_event(val: str | None) -> str | None:
        if val in ("session_stop", "omp.session_stop"):
            return "omp.session_stop"
        return val

    header_norm = normalize_event(header_event)
    body_norm = normalize_event(body_event)

    # 如果 headers 和 body 同时指定了事件但不一致，拒绝
    if header_event and body_event and header_norm != body_norm:
        return (False, "Header X-OMP-Event 与 body event 字段不一致")

    # 至少有一个是 session_stop
    if header_norm == "omp.session_stop" or body_norm == "omp.session_stop":
        return (True, "")

    # 有 event 但不支持
    if header_event or body_event:
        return (False, "不支持的 OMP 事件类型")

    return (False, "未识别到 OMP 事件")


def normalize_omp_payload(
    body: dict[str, Any],
    request_time: str | None = None,
) -> NormalizedEvent:
    """将兼容 OMP Hook V1 的 payload 标准化为 NormalizedEvent。

    Args:
        body: OMP Hook 请求体 JSON。
        request_time: 请求接收时间的 ISO-8601 字符串，None 则使用当前时间。

    Returns:
        NormalizedEvent 对象。
    """
    if request_time is None:
        request_time = datetime.now(timezone.utc).isoformat()

    emitted_at = body.get("emittedAt", request_time) or request_time
    emitted_at = str(emitted_at)

    # 提取 session 信息
    session = body.get("session", {}) or {}
    session_name = _safe_name(session.get("name"))
    session_cwd = _safe_name(re.split(r"[/\\]", _string_or_empty(session.get("cwd")).rstrip("/\\"))[-1])
    session_id = _string_or_empty(session.get("id"))

    # 提取 round 信息
    round_data = body.get("round", {}) or {}
    turn_id = _string_or_empty(round_data.get("turnId"))
    started_at = _string_or_empty(round_data.get("startedAt"))
    ended_at = _string_or_empty(round_data.get("endedAt"))
    duration_ms = round_data.get("durationMs")
    prompt = round_data.get("prompt") or ""
    prompt_length = round_data.get("promptLength")
    image_count = round_data.get("imageCount")
    entry_count_delta = round_data.get("entryCountDelta")
    message_count_delta = round_data.get("messageCountDelta")

    # session.model 缺失时使用 round.lastAssistant.model
    last_assistant = round_data.get("lastAssistant", {}) or {}
    assistant_provider = _string_or_empty(last_assistant.get("provider"))
    session_model = _model_display_name(
        session.get("model"), fallback_provider=assistant_provider
    )
    assistant_model = _model_display_name(
        last_assistant.get("model"), fallback_provider=assistant_provider
    )
    assistant_stop_reason = _string_or_empty(last_assistant.get("stopReason"))
    if not session_model and assistant_model:
        session_model = assistant_model
    session_model = _safe_name(session_model, 128, model=True)
    status = {"error": "failed", "aborted": "aborted"}.get(assistant_stop_reason, "unknown")

    # 计算持续时间
    if duration_ms is None and started_at and ended_at:
        try:
            start_dt = datetime.fromisoformat(started_at)
            end_dt = datetime.fromisoformat(ended_at)
            duration_ms = int((end_dt - start_dt).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    # 生成事件 ID
    legacy_id = f"{session_id}:{turn_id}" if session_id and turn_id else session_id or turn_id
    event_id = hmac.new(_ANON_KEY, legacy_id.encode(), hashlib.sha256).hexdigest()[:32] if legacy_id else ""
    title = _TITLES[status]

    # 生成摘要
    summary = ""

    # 构建 fields
    fields: list[dict[str, Any]] = []

    if session_name:
        fields.append({"label": "会话", "value": session_name, "short": True})

    if session_cwd:
        fields.append({"label": "cwd", "value": session_cwd, "short": False})

    if session_model:
        fields.append({"label": "模型", "value": session_model, "short": True})

    if started_at:
        fields.append({"label": "开始时间", "value": started_at, "short": True})

    # 耗时 - 格式化
    duration_display = _format_duration(duration_ms)
    if duration_display:
        fields.append({"label": "耗时", "value": duration_display, "short": True})

    # 输入规模
    input_parts = []
    if prompt_length is not None:
        input_parts.append(f"{prompt_length} 字")
    elif prompt:
        input_parts.append(f"{len(prompt)} 字")
    if image_count is not None:
        input_parts.append(f"{image_count} 张图")
    if input_parts:
        fields.append(
            {"label": "输入", "value": " / ".join(input_parts), "short": True}
        )

    # 消息变化
    if message_count_delta is not None:
        delta_val = int(message_count_delta)
        sign = "+" if delta_val >= 0 else ""
        fields.append(
            {"label": "消息变化", "value": f"{sign}{delta_val}", "short": True}
        )

    if assistant_stop_reason in {"stop", "error", "aborted", "length", "toolUse"}:
        fields.append({"label": "最后状态", "value": assistant_stop_reason, "short": True})

    # 条目变化
    if entry_count_delta is not None:
        delta_val = int(entry_count_delta)
        sign = "+" if delta_val >= 0 else ""
        fields.append(
            {"label": "条目变化", "value": f"{sign}{delta_val}", "short": True}
        )


    return NormalizedEvent(
        provider="omp",
        event="omp.session_stop",
        version=1,
        id=event_id,
        emitted_at=emitted_at,
        title=title,
        status=status,
        session_scope=SessionScope.UNKNOWN,
        summary=summary.strip(),
        source={"name": OMP_SOURCE_NAME, "url": None},
        actor={"name": None, "url": None},
        fields=fields,
        links=[],
        raw={},
        task_duration_ms=duration_ms if type(duration_ms) in (int, float) and 0 <= duration_ms <= 86400000 else None,
    )


def _string_or_empty(value: Any) -> str:
    """将字段安全转换为字符串，同时保留 0 这类有效值。"""
    if value is None:
        return ""
    return str(value)


def _model_display_name(model: Any, fallback_provider: str = "") -> str:
    """提取兼容 OMP Hook 输出的模型展示名。

    OMP 客户端可能发送字符串模型名，也可能发送形如
    {"provider": "openai", "id": "gpt-5.5", "name": "GPT-5.5"} 的对象。
    对象按 name -> id 顺序回退，展示时优先拼接 provider/name。
    """
    if model is None:
        return ""
    if isinstance(model, dict):
        provider = _string_or_empty(model.get("provider")) or fallback_provider
        name = model.get("name")
        if name:
            return _join_provider_model(provider, str(name))
        model_id = model.get("id")
        if model_id:
            return _join_provider_model(provider, str(model_id))
        return ""
    return _join_provider_model(fallback_provider, str(model))


def _join_provider_model(provider: str, model: str) -> str:
    """拼接 provider/model，避免重复拼接。"""
    provider = provider.strip()
    model = model.strip()
    if not model:
        return ""
    if not provider or model.startswith(f"{provider}/"):
        return model
    return f"{provider}/{model}"


class OmpProviderAdapter(ProviderAdapter):
    """OMP provider 薄适配层。

    复用现有 ``is_omp_session_stop`` 和 ``normalize_omp_payload``，
    失败时转换为标准 ``ProviderError``。不复制/重写 OMP parser。
    """

    @property
    def provider(self) -> str:
        return "omp"

    def parse(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        received_at: str,
    ) -> NormalizedEvent:
        # 拒绝异源 provider payload（OpenCode）
        headers_lower = {k.lower(): v for k, v in headers.items()}
        if "x-opencode-event" in headers_lower:
            raise ProviderError(
                "provider_incompatible", "不兼容的 provider 请求", retryable=False
            )
        body_event_raw = payload.get("event")
        if isinstance(body_event_raw, str):
            body_event_lower = body_event_raw.strip().lower()
            if body_event_lower.startswith("opencode."):
                raise ProviderError(
                    "provider_incompatible", "不兼容的 provider 请求", retryable=False
                )

        header_event = headers_lower.get("x-omp-event")
        if isinstance(body_event_raw, str) and body_event_raw in _V2_EVENTS:
            if header_event is not None and header_event != body_event_raw:
                raise ProviderError("event_mismatch", "OMP 事件不匹配", retryable=False)
            try:
                return _normalize_v2(payload)
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                raise ProviderError("invalid_payload", _OMP_ADAPTER_ERROR_MSG, retryable=False) from None
        if "version" in payload and (isinstance(payload["version"], bool) or payload["version"] not in (1, "1.0")):
            raise ProviderError("unsupported_version", "不支持的 OMP 版本", retryable=False)
        is_valid, err_msg = is_omp_session_stop(headers, payload)
        if not is_valid:
            if "不一致" in err_msg:
                raise ProviderError(
                    "event_mismatch",
                    "请求的 Header 与 Body 事件字段不匹配",
                    retryable=False,
                )
            if "不支持" in err_msg:
                raise ProviderError(
                    "unsupported_event", "不支持的 OMP 事件类型", retryable=False
                )
            raise ProviderError(
                "unrecognized_event", "无法识别 OMP 请求事件", retryable=False
            )
        try:
            return normalize_omp_payload(payload, received_at)
        except Exception:
            raise ProviderError(
                "invalid_payload",
                _OMP_ADAPTER_ERROR_MSG,
                retryable=False,
            )


def _format_duration(duration_ms: int | None | float) -> str:
    """将毫秒格式化为可读时间字符串。"""
    if duration_ms is None:
        return ""
    try:
        ms = float(duration_ms)
        if ms < 1000:
            return f"{ms:.0f}ms"
        if ms < 60000:
            return f"{ms / 1000:.1f}s"
        minutes = ms / 60000
        return f"{minutes:.1f}m"
    except (ValueError, TypeError):
        return ""
