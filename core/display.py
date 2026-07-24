"""通用通知展示层格式化。

这里的转换只作用于 NormalizedEvent 的展示副本，不改变 provider envelope
或服务端契约字段。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import DisplayContext


DEFAULT_DISPLAY_TIMEZONE = "Asia/Shanghai"
INVALID_DISPLAY_TIMEZONE_WARNING = (
    "[WebhookNotifier] display_timezone 无效，已回退到 Asia/Shanghai"
)
MISSING_DEFAULT_TIMEZONE_WARNING = (
    "[WebhookNotifier] 系统缺少 Asia/Shanghai 时区数据，已回退到 UTC"
)


_STATUS_LABELS = {
    "success": "已完成",
    "ok": "已完成",
    "succeeded": "已完成",
    "completed": "已完成",
    "完成": "已完成",
    "已完成": "已完成",
    "error": "失败",
    "failed": "失败",
    "fail": "失败",
    "错误": "失败",
    "失败": "失败",
    "warning": "待处理",
    "warn": "待处理",
    "action_required": "待处理",
    "警告": "待处理",
    "告警": "待处理",
    "待处理": "待处理",
}

_FIELD_LABELS = {
    "projectDisplayName": "项目",
    "sessionName": "会话名称",
    "会话": "会话名称",
    "agent": "执行代理",
    "model": "模型",
    "modelProvider": "模型提供方",
    "duration": "当前任务耗时",
    "durationMs": "当前任务耗时",
    "startedAt": "会话开始时间",
    "taskStartedAt": "当前任务开始时间",
    "endedAt": "当前任务结束时间",
    "sessionRef": "匿名会话标识",
    "questionCount": "问题数量",
    "optionCount": "选项数量",
    "question.summary": "问题摘要",
    "messageCount": "消息数量",
    "toolCount": "工具数量",
    "changeCount": "变更数量",
    "permission.category": "权限类型",
    "permission.title": "权限标题",
    "permission.summary": "权限摘要",
    "permission.description": "权限说明",
    "permission.action": "权限操作",
    "permission.target": "权限目标",
    "permission.patterns": "权限范围",
    "error.category": "错误类型",
    "error.code": "错误代码",
}

_QUESTION_LABEL_RE = re.compile(
    r"^question\[(\d+)\](?:\.(header|options|recommended))?$"
)
_ENGLISH_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|[smhd])", re.IGNORECASE
)
_MODEL_PROVIDERS = {
    "anthropic",
    "aws-bedrock",
    "azure",
    "azure-openai",
    "bedrock",
    "cohere",
    "copilot",
    "cpa",
    "deepseek",
    "fireworks",
    "gemini",
    "github-copilot",
    "google",
    "groq",
    "lmstudio",
    "mistral",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "together",
    "vertex",
    "vertexai",
    "xai",
}
_OPENCODE_FIELD_ORDER = {
    "项目": 0,
    "会话名称": 10,
    "执行代理": 20,
    "模型提供方": 29,
    "模型": 30,
    "当前任务耗时": 40,
    "会话已持续": 50,
    "会话开始时间": 60,
    "当前任务开始时间": 70,
    "当前任务结束时间": 80,
}


def status_label(status: Any) -> str:
    """返回状态的简洁中文展示文案；未知状态保留原值。"""

    raw = "" if status is None else str(status)
    return _STATUS_LABELS.get(raw.strip().lower(), raw)


def localize_field_label(label: Any) -> str:
    """映射已知字段，动态 question index 和未知字段保留原键名。"""

    raw = "" if label is None else str(label)
    if raw in _FIELD_LABELS:
        return _FIELD_LABELS[raw]
    match = _QUESTION_LABEL_RE.match(raw)
    if match:
        index, suffix = match.groups()
        if suffix == "header":
            return f"问题 {index} 标题"
        if suffix == "options":
            return f"问题 {index} 选项"
        if suffix == "recommended":
            return f"问题 {index} 推荐选项"
        return f"问题 {index}"
    return raw


def format_duration_ms(duration_ms: int | float) -> str:
    """将毫秒转换为自然中文时长。"""

    total_ms = max(0, int(duration_ms))
    if total_ms < 1000:
        return f"{total_ms} 毫秒"

    total_seconds = total_ms / 1000
    if total_seconds < 60:
        seconds = f"{total_seconds:.1f}".rstrip("0").rstrip(".")
        return f"{seconds} 秒"

    total_seconds_int = total_ms // 1000
    minutes, seconds = divmod(total_seconds_int, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)

    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if seconds and len(parts) < 3:
        parts.append(f"{seconds} 秒")
    return " ".join(parts) or "0 秒"


def _parse_english_duration(value: str) -> int | None:
    matches = list(_ENGLISH_DURATION_RE.finditer(value.strip()))
    if not matches or "".join(match.group(0) for match in matches).replace(
        " ", ""
    ) not in value.replace(" ", ""):
        return None
    total_ms = 0.0
    multipliers = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    for match in matches:
        total_ms += (
            float(match.group("value")) * multipliers[match.group("unit").lower()]
        )
    return int(total_ms)


def format_duration_value(value: Any) -> Any:
    """格式化 duration/durationMs 的展示值，未知值原样保留。"""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_duration_ms(value)
    if isinstance(value, str):
        if value.strip().isdigit():
            return format_duration_ms(int(value.strip()))
        parsed = _parse_english_duration(value)
        if parsed is not None:
            return format_duration_ms(parsed)
    return value


@lru_cache(maxsize=32)
def _load_timezone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def create_display_context(
    value: Any = DEFAULT_DISPLAY_TIMEZONE,
    *,
    warn: Callable[[str], None] | None = None,
) -> DisplayContext:
    """校验展示时区；非法配置不回显原值，默认 zoneinfo 缺失时回退 UTC。"""

    timezone_name = (
        value.strip()
        if isinstance(value, str) and value.strip()
        else DEFAULT_DISPLAY_TIMEZONE
    )
    invalid_config = value is not None and (
        not isinstance(value, str) or not value.strip()
    )
    try:
        display_timezone = _load_timezone(timezone_name)
    except ZoneInfoNotFoundError:
        invalid_config = timezone_name != DEFAULT_DISPLAY_TIMEZONE or invalid_config
        if invalid_config and warn is not None:
            warn(INVALID_DISPLAY_TIMEZONE_WARNING)
        try:
            display_timezone = _load_timezone(DEFAULT_DISPLAY_TIMEZONE)
            timezone_name = DEFAULT_DISPLAY_TIMEZONE
        except ZoneInfoNotFoundError:
            if warn is not None:
                warn(MISSING_DEFAULT_TIMEZONE_WARNING)
            return DisplayContext(timezone_name="UTC", timezone=timezone.utc)
    else:
        if invalid_config and warn is not None:
            warn(INVALID_DISPLAY_TIMEZONE_WARNING)

    return DisplayContext(timezone_name=timezone_name, timezone=display_timezone)


@lru_cache(maxsize=1)
def default_display_context() -> DisplayContext:
    return create_display_context()


def _utc_offset_label(dt: datetime) -> str:
    offset = dt.utcoffset()
    if offset is None:
        return "UTC+00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _parse_aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def format_timestamp(value: Any, display_context: DisplayContext | None = None) -> Any:
    """把带时区 ISO 时间转换到统一展示时区；naive/非法值安全保留。"""

    dt = _parse_aware_timestamp(value)
    if dt is None:
        return value
    context = display_context or default_display_context()
    localized = dt.astimezone(context.timezone)
    zone_name = localized.tzname() or context.timezone_name
    return f"{localized:%Y-%m-%d %H:%M:%S} {zone_name} ({_utc_offset_label(localized)})"


def _is_recommended(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "recommended", "推荐"}
    return False


def _split_option_text(value: Any) -> tuple[str, str, bool]:
    text = "" if value is None else str(value).strip()
    recommended = bool(re.search(r"\(recommended=true\)", text, flags=re.IGNORECASE))
    text = re.sub(
        r"\s*\(recommended=(?:true|false)\)", "", text, flags=re.IGNORECASE
    ).strip()
    if ": " in text:
        label, description = text.split(": ", 1)
        return label.strip(), description.strip(), recommended
    if "：" in text:
        label, description = text.split("：", 1)
        return label.strip(), description.strip(), recommended
    return text, "", recommended


def _normalise_option_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        options = value.get("options")
        return options if isinstance(options, list) else [value]
    if not isinstance(value, str):
        return [value]

    text = value.strip()
    if not text:
        return []
    if text[:1] in {"[", "{"}:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if parsed is not None:
            return _normalise_option_items(parsed)
    if "|" in text:
        return [item.strip() for item in re.split(r"\s*\|\s*", text) if item.strip()]
    return [text]


def _format_option_lines(value: Any) -> Any:
    lines: list[str] = []
    for index, item in enumerate(_normalise_option_items(value), start=1):
        if isinstance(item, dict):
            label = item.get("label", item.get("name", item.get("text", "")))
            description = item.get("description", item.get("summary", ""))
            recommended = _is_recommended(item.get("recommended"))
            label_text = "" if label is None else str(label).strip()
            description_text = "" if description is None else str(description).strip()
        else:
            label_text, description_text, recommended = _split_option_text(item)

        if not label_text and description_text:
            label_text, description_text = description_text, ""
        if not label_text:
            continue
        marker = "（推荐）" if recommended else ""
        lines.append(f"{index}. {label_text}{marker}")
        if description_text:
            lines.append(f"   {description_text}")
    return "\n".join(lines) if lines else value


def _normalise_compare_text(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def _is_provider_only_model(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return "/" not in normalized and normalized in _MODEL_PROVIDERS


def prepare_display_fields(
    fields: Any,
    *,
    title: str = "",
    display_context: DisplayContext | None = None,
) -> list[dict[str, Any]]:
    """复制并本地化 fields，消除普通卡片中的重复信息。"""

    if not isinstance(fields, list):
        return []
    has_duration = any(
        isinstance(field, dict)
        and str(field.get("label", "")) in {"duration", "耗时", "当前任务耗时"}
        for field in fields
    )
    question_details = [
        field
        for field in fields
        if isinstance(field, dict)
        and _QUESTION_LABEL_RE.match(str(field.get("label", "")))
    ]
    question_texts: list[Any] = []
    for field in question_details:
        match = _QUESTION_LABEL_RE.match(str(field.get("label", "")))
        if match and match.group(2) is None:
            question_texts.append(field.get("value", ""))
    summary_values = [
        field.get("value", "")
        for field in fields
        if isinstance(field, dict) and field.get("label") == "question.summary"
    ]
    duplicate_summary = (
        len(question_texts) == 1
        and len(summary_values) == 1
        and _normalise_compare_text(question_texts[0])
        == _normalise_compare_text(summary_values[0])
    )
    result: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        raw_label = str(field.get("label", field.get("name", field.get("key", "字段"))))
        value = field.get("value", "")
        if raw_label in {"sessionRef", "匿名会话标识"}:
            continue
        if raw_label in {"questionCount", "optionCount"} and question_details:
            continue
        if raw_label == "question.summary" and duplicate_summary:
            continue
        if raw_label == "durationMs" and has_duration:
            continue
        item = dict(field)
        item["label"] = (
            "模型提供方"
            if raw_label == "model" and _is_provider_only_model(value)
            else localize_field_label(raw_label)
        )
        if raw_label in {"duration", "durationMs", "耗时", "当前任务耗时"}:
            value = format_duration_value(value)
        elif raw_label in {
            "startedAt",
            "taskStartedAt",
            "endedAt",
            "开始时间",
            "结束时间",
            "会话开始时间",
            "当前任务开始时间",
            "当前任务结束时间",
        }:
            value = format_timestamp(value, display_context)
        if "options" in raw_label:
            value = _format_option_lines(value)
        item["value"] = value if value is not None else ""
        result.append(item)
    return result


def _derive_opencode_session_elapsed(data: dict[str, Any]) -> str | None:
    """仅按 OpenCode 的 startedAt 会话语义派生当前会话已持续时长。"""

    if data.get("provider") != "opencode":
        return None
    emitted_at = _parse_aware_timestamp(data.get("emitted_at"))
    if emitted_at is None:
        return None

    fields = data.get("fields")
    if not isinstance(fields, list):
        return None
    started_at: datetime | None = None
    for field in fields:
        if not isinstance(field, dict):
            continue
        raw_label = str(field.get("label", field.get("name", field.get("key", ""))))
        if raw_label != "startedAt":
            continue
        started_at = _parse_aware_timestamp(field.get("value"))
        if started_at is not None:
            break
    if started_at is None:
        return None

    duration_ms = (emitted_at - started_at).total_seconds() * 1000
    if not math.isfinite(duration_ms) or duration_ms < 0:
        return None
    return format_duration_ms(duration_ms)


def _order_opencode_display_fields(
    fields: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """稳定排列 OpenCode 的任务、会话时间与交互内容。"""

    def sort_key(indexed_field: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, field = indexed_field
        label = str(field.get("label", ""))
        if label in _OPENCODE_FIELD_ORDER:
            return _OPENCODE_FIELD_ORDER[label], index
        if label.startswith(("问题 ", "权限")):
            return 100, index
        return 90, index

    return [field for _, field in sorted(enumerate(fields), key=sort_key)]


def build_display_event_data(
    data: dict[str, Any],
    *,
    flatten_source: bool = False,
    display_context: DisplayContext | None = None,
) -> dict[str, Any]:
    """从标准化事件 dict 构造安全展示副本。"""

    result = dict(data)
    context = display_context or default_display_context()
    if flatten_source and isinstance(result.get("source"), dict):
        result["source"] = result["source"].get("name") or "AstrBot"
    result["status_display"] = status_label(result.get("status"))
    display_fields = prepare_display_fields(
        result.get("fields"),
        title=str(result.get("title", "")),
        display_context=context,
    )
    if result.get("provider") == "opencode":
        session_elapsed = _derive_opencode_session_elapsed(result)
        if session_elapsed is not None:
            display_fields.append({"label": "会话已持续", "value": session_elapsed})
        display_fields = _order_opencode_display_fields(display_fields)
    result["fields"] = display_fields
    result["display_timezone"] = context.timezone_name
    return result
