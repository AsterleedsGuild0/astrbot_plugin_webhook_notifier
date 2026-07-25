from __future__ import annotations

import asyncio

import pytest

from core.idempotency import (
    ClaimStatus,
    IdempotencyStore,
    TerminalState,
    digest_idempotency_key,
    make_idempotency_key,
)


def _key(event_id: str = "event-1", *, path: str = "endpoint"):
    return make_idempotency_key(
        endpoint_provider="opencode",
        endpoint_path=path,
        event_name="opencode.permission_asked",
        event_id=event_id,
        session_scope="root",
        target_alias=None,
    )


def test_finalize_replays_until_ttl_then_claims_again():
    now = [100.0]
    store = IdempotencyStore(clock=lambda: now[0])

    owner = store.claim(_key())
    assert owner.status is ClaimStatus.OWNER
    assert store.finalize(owner) is True
    assert store.claim(_key()).status is ClaimStatus.REPLAY

    now[0] += 600
    assert store.claim(_key()).status is ClaimStatus.OWNER


@pytest.mark.asyncio
async def test_three_concurrent_claims_have_one_owner_and_waiters_replay():
    store = IdempotencyStore()
    owner = store.claim(_key())
    waiters = [store.claim(_key()) for _ in range(2)]
    assert owner.status is ClaimStatus.OWNER
    assert [claim.status for claim in waiters] == [
        ClaimStatus.WAITER,
        ClaimStatus.WAITER,
    ]

    tasks = [asyncio.create_task(store.wait(claim)) for claim in waiters]
    await asyncio.sleep(0)
    assert not tasks[0].done()
    store.finalize(owner)
    results = await asyncio.gather(*tasks)
    assert [result.status for result in results] == [
        ClaimStatus.REPLAY,
        ClaimStatus.REPLAY,
    ]


@pytest.mark.asyncio
async def test_release_wakes_waiter_to_reclaim_owner():
    store = IdempotencyStore()
    owner = store.claim(_key())
    waiter = store.claim(_key())
    task = asyncio.create_task(store.wait(waiter))
    await asyncio.sleep(0)

    assert store.release(owner) is True
    replacement = await task
    assert replacement.status is ClaimStatus.OWNER
    store.finalize(replacement)
    assert store.claim(_key()).status is ClaimStatus.REPLAY


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_future():
    store = IdempotencyStore()
    owner = store.claim(_key())
    waiter = store.claim(_key())
    task = asyncio.create_task(store.wait(waiter))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiter._entry is not None
    assert waiter._entry.future.cancelled() is False

    store.finalize(owner)
    assert waiter._entry.future.result() is TerminalState.FINALIZED


def test_release_completes_owner_future_and_does_not_leak():
    store = IdempotencyStore()
    owner = store.claim(_key())
    assert owner._entry is not None
    future = owner._entry.future

    assert store.release(owner) is True
    assert future.done() is True
    assert future.result() is TerminalState.RELEASED
    assert store.size == 0


def test_lru_evicts_completed_but_never_in_flight_and_reports_full_capacity():
    store = IdempotencyStore(capacity=2)
    first = store.claim(_key("first"))
    second = store.claim(_key("second"))
    assert first.status is ClaimStatus.OWNER
    assert second.status is ClaimStatus.OWNER
    assert store.claim(_key("third")).status is ClaimStatus.CAPACITY

    store.finalize(first)
    third = store.claim(_key("third"))
    assert third.status is ClaimStatus.OWNER
    assert store.claim(_key("second")).status is ClaimStatus.WAITER


def test_key_dimensions_are_isolated_and_digest_does_not_expose_inputs():
    key = _key("secret-event-id", path="private/endpoint/path")
    other_path = _key("secret-event-id", path="other/path")
    digest = digest_idempotency_key(key)
    assert digest != digest_idempotency_key(other_path)
    assert "secret-event-id" not in digest
    assert "private/endpoint/path" not in digest

    store = IdempotencyStore()
    assert store.claim(key).status is ClaimStatus.OWNER
    assert store.claim(other_path).status is ClaimStatus.OWNER
    assert all(
        "secret-event-id" not in repr(value) for value in store._entries.values()
    )


def test_empty_event_id_bypasses_store():
    store = IdempotencyStore()
    assert store.claim(_key("")).status is ClaimStatus.BYPASS
    assert store.size == 0


@pytest.mark.parametrize("invalid_alias", ["", 0, False, [], object()])
def test_target_selector_rejects_invalid_values_before_claim(invalid_alias):
    with pytest.raises(ValueError, match="target_alias"):
        make_idempotency_key(
            endpoint_provider="opencode",
            endpoint_path="endpoint",
            event_name="opencode.session_idle",
            event_id="event-1",
            session_scope="root",
            target_alias=invalid_alias,
        )


def test_target_selector_preserves_exact_sender_aliases():
    aliases = ["Alias", " alias", "alias ", "Ａlias", "alias"]
    digests = {
        digest_idempotency_key(
            make_idempotency_key(
                endpoint_provider="opencode",
                endpoint_path="endpoint",
                event_name="opencode.session_idle",
                event_id="event-1",
                session_scope="root",
                target_alias=alias,
            )
        )
        for alias in aliases
    }
    assert len(digests) == len(aliases)

    no_target = make_idempotency_key(
        endpoint_provider="opencode",
        endpoint_path="endpoint",
        event_name="opencode.session_idle",
        event_id="event-1",
        session_scope="root",
        target_alias=None,
    )
    all_alias = make_idempotency_key(
        endpoint_provider="opencode",
        endpoint_path="endpoint",
        event_name="opencode.session_idle",
        event_id="event-1",
        session_scope="root",
        target_alias="all",
    )
    assert digest_idempotency_key(no_target) != digest_idempotency_key(all_alias)
