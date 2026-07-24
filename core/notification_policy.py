"""Pure notification filtering policy.

The policy intentionally knows nothing about providers, renderers, targets, or
transport.  It only decides whether a normalized event should continue to the
delivery pipeline.
"""

from __future__ import annotations

import enum
from typing import Any, Final


class NotificationMode(str, enum.Enum):
    """Supported global notification modes."""

    FOCUSED = "focused"
    ALL = "all"


class SessionScope(str, enum.Enum):
    """OpenCode session scope used by the notification policy."""

    ROOT = "root"
    SUBAGENT = "subagent"
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

    ``focused`` rejects exactly successful ``subagent`` completions.  Every
    other scope or status is allowed, including unknown and future values.
    Invalid modes are normalized to ``all`` and therefore fail open.
    """

    resolved_mode = normalize_notification_mode(mode)
    return not (
        resolved_mode == NotificationMode.FOCUSED.value
        and session_scope == SessionScope.SUBAGENT.value
        and status == "completed"
    )


# Explicitly named aliases keep the policy easy to discover for callers while
# retaining one implementation of the rule.
notification_allowed = should_notify
allows_notification = should_notify


__all__ = [
    "NotificationMode",
    "SessionScope",
    "normalize_notification_mode",
    "should_notify",
    "notification_allowed",
    "allows_notification",
]
