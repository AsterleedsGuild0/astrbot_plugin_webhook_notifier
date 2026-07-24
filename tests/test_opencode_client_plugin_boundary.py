"""Cross-boundary validation: TS Plugin payload → Python OpenCodeProviderAdapter.

Verifies that the envelope produced by the TypeScript plugin is parseable
by the server-side ``OpenCodeProviderAdapter`` without error.

This test does NOT run the TypeScript code — it validates the envelope
SCHEMA that the plugin is expected to produce.  Actual TS→Python payload
fidelity is verified by ``test_webhook_notifier.ts`` (Bun tests).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.models import NormalizedEvent
from core.opencode import OpenCodeProviderAdapter
from core.providers import ProviderError


def _adapter() -> OpenCodeProviderAdapter:
    return OpenCodeProviderAdapter()


def _received_at() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestEnvelopeSchemaCompatibility:
    """Every payload shape that the TS Plugin can produce must be parseable."""

    MINIMAL_IDLE = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "event": "opencode.session_idle",
        "version": 1,
        "emittedAt": "2026-07-22T12:00:00.000Z",
        "session": {"ref": "abcdef1234567890abcdef1234567890"},
    }

    FULL_IDLE = {
        "id": "evt_full_idle",
        "event": "opencode.session_idle",
        "version": 1,
        "emittedAt": "2026-07-22T12:00:00.000Z",
        "session": {"ref": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "name": "My Session"},
        "agent": "my-agent",
        "model": "gpt-5",
        "durationMs": 15000,
    }

    ERROR_EVENT = {
        "id": "evt_err_001",
        "event": "opencode.session_error",
        "version": 1,
        "emittedAt": "2026-07-22T12:00:00.000Z",
        "session": {"ref": "fedcba0987654321fedcba0987654321"},
        "error": {"category": "timeouterror", "code": "500"},
    }

    PERMISSION_EVENT = {
        "id": "evt_perm_001",
        "event": "opencode.permission_asked",
        "version": 1,
        "emittedAt": "2026-07-22T12:00:00.000Z",
        "session": {"ref": "11223344556677889900aabbccddeeff"},
        "permission": {"category": "file_access"},
    }

    QUESTION_EVENT = {
        "id": "evt_question_001",
        "event": "opencode.question_asked",
        "version": 1,
        "emittedAt": "2026-07-22T12:00:00.000Z",
        "session": {"ref": "99887766554433221100ffeeddccbbaa"},
    }

    @pytest.mark.parametrize(
        "label, payload, headers, expected_event",
        [
            (
                "minimal_idle",
                MINIMAL_IDLE,
                {"x-opencode-event": "opencode.session_idle"},
                "opencode.session_idle",
            ),
            (
                "full_idle",
                FULL_IDLE,
                {"x-opencode-event": "opencode.session_idle"},
                "opencode.session_idle",
            ),
            (
                "error",
                ERROR_EVENT,
                {"x-opencode-event": "opencode.session_error"},
                "opencode.session_error",
            ),
            (
                "permission",
                PERMISSION_EVENT,
                {"x-opencode-event": "opencode.permission_asked"},
                "opencode.permission_asked",
            ),
            (
                "question",
                QUESTION_EVENT,
                {"x-opencode-event": "opencode.question_asked"},
                "opencode.question_asked",
            ),
        ],
    )
    def test_parseable(self, label, payload, headers, expected_event):
        event = _adapter().parse(
            headers=headers, payload=payload, received_at=_received_at()
        )
        assert isinstance(event, NormalizedEvent), f"{label}: expected NormalizedEvent"
        assert event.event == expected_event, f"{label}: event mismatch"
        assert event.id == payload["id"], f"{label}: id mismatch"
        assert event.version == 1, f"{label}: version mismatch"
        if label == "question":
            assert event.status == "action_required"
            assert event.summary == "等待问题回答"

    def test_session_name_cleaned(self):
        """TS plugin may send spaces/normal ascii; server cleans."""
        event = _adapter().parse(
            headers={"x-opencode-event": "opencode.session_idle"},
            payload={
                **self.MINIMAL_IDLE,
                "session": {"ref": "ref123", "name": "  My Task  "},
            },
            received_at=_received_at(),
        )
        assert "My Task" in event.title
        assert "  " not in event.title

    def test_ref12_in_title(self):
        """When session.name is omitted, server falls back to ref12."""
        raw_ref = "abcdef1234567890abcdef1234567890"
        event = _adapter().parse(
            headers=self.MINIMAL_IDLE["event"]
            and {"x-opencode-event": "opencode.session_idle"},
            payload={**self.MINIMAL_IDLE, "session": {"ref": raw_ref}},
            received_at=_received_at(),
        )
        assert "abcdef123456" in event.title  # first 12 safe chars

    def test_unknown_field_rejected(self):
        """Plugin must never send unknown fields; server rejects."""
        with pytest.raises(ProviderError, match="不允许"):
            _adapter().parse(
                headers={"x-opencode-event": "opencode.session_idle"},
                payload={**self.MINIMAL_IDLE, "cwd": "/tmp"},
                received_at=_received_at(),
            )

    def test_sensitive_payload_rejected(self):
        """Plugin must never send raw fields; server rejects."""
        for bad in (
            "cwd",
            "path",
            "username",
            "prompt",
            "messages",
            "tool",
            "diff",
            "raw",
            "questions",
        ):
            with pytest.raises(ProviderError):
                _adapter().parse(
                    headers={"x-opencode-event": "opencode.session_idle"},
                    payload={**self.MINIMAL_IDLE, bad: "xxx"},
                    received_at=_received_at(),
                )

    @pytest.mark.parametrize(
        "event_name",
        [
            "opencode.question_replied",
            "opencode.question_rejected",
        ],
    )
    def test_question_completion_variants_rejected(self, event_name):
        with pytest.raises(ProviderError):
            _adapter().parse(
                headers={"x-opencode-event": event_name},
                payload={**self.MINIMAL_IDLE, "event": event_name},
                received_at=_received_at(),
            )

    def test_header_event_mismatch_rejected(self):
        """Header X-OpenCode-Event must match body.event."""
        with pytest.raises(ProviderError, match="不匹配"):
            _adapter().parse(
                headers={"x-opencode-event": "opencode.session_idle"},
                payload={**self.MINIMAL_IDLE, "event": "opencode.session_error"},
                received_at=_received_at(),
            )
