"""Pure notification filtering policy.

The policy intentionally knows nothing about providers, renderers, targets, or
transport.  It only decides whether a normalized event should continue to the
delivery pipeline.
"""

from __future__ import annotations

import enum
import logging
from typing import Any, Final

_logger = logging.getLogger("astrbot.webhook_notifier.policy")

# ─── 最短完成通知时长阈值 ─────────────────────────────────

MIN_COMPLETION_DURATION_DEFAULT: Final = 15  # seconds
MIN_COMPLETION_DURATION_MIN: Final = 0
MIN_COMPLETION_DURATION_MAX: Final = 3600

# ─── 规范 completed 状态 ──────────────────────────────────

_CANONICAL_COMPLETED = frozenset({"completed"})


def _is_canonical_completed(status: Any) -> bool:
    """Return True only for the canonical ``"completed"`` string."""
    return isinstance(status, str) and status in _CANONICAL_COMPLETED


_MISSING_DURATION: Final = object()


def normalize_min_completion_duration(value: Any = _MISSING_DURATION) -> int:
    """Resolve ``min_completion_duration_seconds`` to a safe valid integer.

    Rules (per design):
    - Missing / None → default 15.
    - Valid int in [0, 3600] → as-is.
    - Invalid (non-int, out of range, bool, etc.) → 0 (fail-open).
    - 0 disables the filter entirely.
    """
    if value is _MISSING_DURATION or value is None:
        return MIN_COMPLETION_DURATION_DEFAULT
    if isinstance(value, bool):
        # bool is a subclass of int but must not be accepted
        _logger.warning(
            "min_completion_duration_seconds: bool 值不被接受，已归一化为 0（安全打开）"
        )
        return 0
    if isinstance(value, int):
        if MIN_COMPLETION_DURATION_MIN <= value <= MIN_COMPLETION_DURATION_MAX:
            return value
        _logger.warning(
            f"min_completion_duration_seconds: 值 {value} 超出有效范围 "
            f"[{MIN_COMPLETION_DURATION_MIN}, {MIN_COMPLETION_DURATION_MAX}]，"
            "已归一化为 0（安全打开）"
        )
        return 0
    _logger.warning(
        f"min_completion_duration_seconds: 非整数类型 {type(value).__name__}，"
        "已归一化为 0（安全打开）"
    )
    return 0


def should_filter_by_duration(
    status: Any,
    task_duration_ms: int | None,
    threshold_seconds: int,
) -> bool:
    """Decide whether to skip a notification based on completion duration.

    Args:
        status: The canonical status of the event.
        task_duration_ms: Reliable task/prompt duration in ms, or None if
            unavailable/unreliable.
        threshold_seconds: The configured threshold (0 disables).

    Returns:
        True when the notification SHOULD be skipped (filtered out).
        False when it should proceed.
    """
    # 0 disables the filter
    if threshold_seconds <= 0:
        return False
    # Only canonical "completed" participates
    if not _is_canonical_completed(status):
        return False
    # No reliable duration → fail-open, proceed
    if task_duration_ms is None:
        return False
    # duration < threshold → skip; >= threshold → proceed
    return task_duration_ms < threshold_seconds * 1000


class NotificationMode(str, enum.Enum):
    """Supported global notification modes."""

    FOCUSED = "focused"
    ALL = "all"


class SessionScope(str, enum.Enum):
    """OpenCode session scope used by the notification policy."""

    ROOT = "root"
    SUBAGENT = "subagent"
    AUXILIARY = "auxiliary"
    UNKNOWN = "unknown"


_MISSING: Final = object()


def normalize_notification_mode(value: Any = _MISSING) -> str:
    """Resolve a runtime configuration value using the fail-open contract.

    Missing configuration defaults to ``focused``.  Any explicitly supplied
    invalid value falls back to ``all`` so a bad runtime configuration cannot
    silently discard notifications.
    """

    if value is _MISSING:
        return NotificationMode.FOCUSED.value
    if value == NotificationMode.FOCUSED.value:
        return NotificationMode.FOCUSED.value
    if value == NotificationMode.ALL.value:
        return NotificationMode.ALL.value
    return NotificationMode.ALL.value


def should_notify(
    mode: Any = _MISSING,
    session_scope: Any = SessionScope.UNKNOWN.value,
    status: Any = "unknown",
) -> bool:
    """Return whether a standardized event may enter rendering/delivery.

    ``focused`` rejects exactly successful ``subagent`` and ``auxiliary`` completions.  Every
    other scope or status is allowed, including unknown and future values.
    Invalid modes are normalized to ``all`` and therefore fail open.
    """

    resolved_mode = normalize_notification_mode(mode)
    return not (
        resolved_mode == NotificationMode.FOCUSED.value
        and session_scope
        in {
            SessionScope.SUBAGENT.value,
            SessionScope.AUXILIARY.value,
        }
        and status == "completed"
    )


# Explicitly named aliases keep the policy easy to discover for callers while
# retaining one implementation of the rule.
notification_allowed = should_notify
allows_notification = should_notify


__all__ = [
    "MIN_COMPLETION_DURATION_DEFAULT",
    "MIN_COMPLETION_DURATION_MIN",
    "MIN_COMPLETION_DURATION_MAX",
    "NotificationMode",
    "SessionScope",
    "normalize_notification_mode",
    "normalize_min_completion_duration",
    "should_filter_by_duration",
    "should_notify",
    "notification_allowed",
    "allows_notification",
    "_CANONICAL_COMPLETED",
    "_is_canonical_completed",
]
