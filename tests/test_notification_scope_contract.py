"""OpenCode scope and OMP fail-open contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.models import NormalizedEvent
from core.omp import OmpProviderAdapter, normalize_omp_payload
from core.opencode import OpenCodeProviderAdapter
from core.providers import ProviderError


def _received_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(session: dict) -> dict:
    return {
        "id": "scope-event",
        "event": "opencode.session_idle",
        "version": 1,
        "emittedAt": "2026-07-24T00:00:00Z",
        "session": session,
    }


@pytest.mark.parametrize("scope", ["root", "subagent", "unknown"])
def test_opencode_scope_round_trips_without_render_fields(scope: str) -> None:
    event = OpenCodeProviderAdapter().parse(
        headers={"x-opencode-event": "opencode.session_idle"},
        payload=_payload({"ref": "safe-ref", "scope": scope}),
        received_at=_received_at(),
    )
    assert event.session_scope == scope
    assert all(field["label"] != "sessionScope" for field in event.fields)
    assert "session_scope" not in event.to_dict()


def test_opencode_missing_scope_is_unknown() -> None:
    event = OpenCodeProviderAdapter().parse(
        headers={"x-opencode-event": "opencode.session_idle"},
        payload=_payload({"ref": "safe-ref"}),
        received_at=_received_at(),
    )
    assert event.session_scope == "unknown"


@pytest.mark.parametrize("scope", [None, "invalid", 1, True])
def test_opencode_invalid_scope_is_rejected(scope) -> None:
    with pytest.raises(ProviderError):
        OpenCodeProviderAdapter().parse(
            headers={"x-opencode-event": "opencode.session_idle"},
            payload=_payload({"ref": "safe-ref", "scope": scope}),
            received_at=_received_at(),
        )


def test_opencode_parent_id_is_rejected_and_never_stored() -> None:
    with pytest.raises(ProviderError):
        OpenCodeProviderAdapter().parse(
            headers={"x-opencode-event": "opencode.session_idle"},
            payload=_payload({"ref": "safe-ref", "parentID": "secret-parent"}),
            received_at=_received_at(),
        )


def test_omp_explicitly_uses_unknown_scope_and_focused_allows_it() -> None:
    event = normalize_omp_payload({"event": "omp.session_stop"})
    assert isinstance(event, NormalizedEvent)
    assert event.session_scope == "unknown"
    assert event.status == "success"
    adapter_event = OmpProviderAdapter().parse(
        headers={"x-omp-event": "session_stop"},
        payload={"event": "omp.session_stop"},
        received_at=_received_at(),
    )
    assert adapter_event.session_scope == "unknown"
