from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import NormalizedEvent

OMP_SOURCE_NAME = "oh-my-pi"


def is_omp_session_stop(
    headers: dict[str, str], body: dict[str, Any]
) -> tuple[bool, str]:
    """判断请求是否为 OMP session_stop 事件。

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

    # 读取 header（大小写不敏感）
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
        provided = header_event or body_event or "unknown"
        return (False, f"不支持的 OMP 事件类型: {provided}")

    return (False, "未识别到 OMP 事件")


def normalize_omp_payload(
    body: dict[str, Any],
    request_time: str | None = None,
) -> NormalizedEvent:
    """将 OMP session_stop payload 标准化为 NormalizedEvent。

    Args:
        body: OMP 请求体 JSON。
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
    session_name = session.get("name") or ""
    session_file = session.get("file") or ""
    session_cwd = session.get("cwd") or ""
    session_model = session.get("model") or ""
    session_id = session.get("id") or ""

    # session.name 缺失时使用 session.file basename
    if not session_name and session_file:
        session_name = _basename(session_file)

    # 提取 round 信息
    round_data = body.get("round", {}) or {}
    turn_id = round_data.get("turnId") or ""
    started_at = round_data.get("startedAt") or ""
    ended_at = round_data.get("endedAt") or ""
    duration_ms = round_data.get("durationMs")
    prompt = round_data.get("prompt") or ""
    prompt_length = round_data.get("promptLength")
    image_count = round_data.get("imageCount")
    entry_count_delta = round_data.get("entryCountDelta")
    message_count_delta = round_data.get("messageCountDelta")
    stop_hook_active = round_data.get("stopHookActive")

    # session.model 缺失时使用 round.lastAssistant.model
    last_assistant = round_data.get("lastAssistant", {}) or {}
    assistant_model = last_assistant.get("model") or ""
    assistant_stop_reason = last_assistant.get("stopReason") or ""
    assistant_timestamp = last_assistant.get("timestamp") or ""
    assistant_duration_ms = last_assistant.get("durationMs")

    if not session_model and assistant_model:
        session_model = assistant_model

    # 计算持续时间
    if duration_ms is None and started_at and ended_at:
        try:
            start_dt = datetime.fromisoformat(started_at)
            end_dt = datetime.fromisoformat(ended_at)
            duration_ms = int((end_dt - start_dt).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    # 生成事件 ID
    event_id = (
        f"{session_id}:{turn_id}"
        if session_id and turn_id
        else session_id or turn_id or ""
    )

    # 生成标题
    title = f"会话完成"
    if session_name:
        title = f"会话完成"

    # 生成摘要
    summary = ""
    if session_name:
        summary = f"会话 {session_name}"
    if session_model:
        summary = (
            f"{summary} 模型 {session_model}" if summary else f"模型 {session_model}"
        )
    if not summary:
        summary = "OMP 会话已完成"

    # 构建 fields
    fields: list[dict[str, Any]] = []

    if session_name:
        fields.append({"label": "会话", "value": session_name, "short": True})

    if session_model:
        fields.append({"label": "模型", "value": session_model, "short": True})

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

    # 最后状态
    if assistant_stop_reason:
        fields.append(
            {"label": "最后状态", "value": assistant_stop_reason, "short": True}
        )

    # 条目变化
    if entry_count_delta is not None:
        delta_val = int(entry_count_delta)
        sign = "+" if delta_val >= 0 else ""
        fields.append(
            {"label": "条目变化", "value": f"{sign}{delta_val}", "short": True}
        )

    # 构建 raw
    raw: dict[str, Any] = {}
    metadata = body.get("metadata", {}) or {}
    if metadata.get("version"):
        raw["metadata.version"] = metadata["version"]
    if metadata.get("eventName"):
        raw["metadata.eventName"] = metadata["eventName"]
    if stop_hook_active is not None:
        raw["round.stopHookActive"] = stop_hook_active
    if assistant_timestamp:
        raw["round.lastAssistant.timestamp"] = assistant_timestamp

    return NormalizedEvent(
        provider="omp",
        event="omp.session_stop",
        version=1,
        id=event_id,
        emitted_at=emitted_at,
        title=title,
        status="success",
        summary=summary.strip(),
        source={"name": OMP_SOURCE_NAME, "url": None},
        actor={"name": None, "url": None},
        fields=fields,
        links=[],
        raw=raw,
    )


def _basename(path: str) -> str:
    """返回路径的文件名部分（不含目录），安全处理空字符串。"""
    if not path:
        return ""
    return path.rstrip("/").split("/")[-1] or path


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
