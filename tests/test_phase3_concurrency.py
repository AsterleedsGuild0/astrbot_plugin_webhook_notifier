from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import core.registry as registry_module
from core.registry import (
    BIND_CURRENT_GROUP,
    REGISTRY_FILENAME,
    EndpointRegistry,
    RegistryConflictError,
    RegistryPersistenceError,
)


def _race(callables: list[Callable[[], Any]]) -> list[tuple[str, Any]]:
    barrier = threading.Barrier(len(callables) + 1)
    results: list[tuple[str, Any] | None] = [None] * len(callables)

    def run(index, callback):
        barrier.wait()
        try:
            results[index] = ("ok", callback())
        except Exception as exc:  # noqa: BLE001 - 测试需要捕获线程结果
            results[index] = ("error", exc)

    threads = [
        threading.Thread(target=run, args=(index, callback))
        for index, callback in enumerate(callables)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert all(result is not None for result in results)
    return [result for result in results if result is not None]


def test_threaded_same_scope_create_has_one_winner_and_one_disk_record(tmp_path):
    registry = EndpointRegistry(tmp_path)
    calls = [
        lambda: registry.create_private_endpoint(
            "bot-a", "owner", "same", "bot-a:FriendMessage:owner"
        )
        for _ in range(4)
    ]

    results = _race(calls)

    assert sum(result[0] == "ok" for result in results) == 1
    errors = [result[1] for result in results if result[0] == "error"]
    assert all(isinstance(error, RegistryConflictError) for error in errors)
    saved = json.loads((tmp_path / REGISTRY_FILENAME).read_text())
    assert len(saved["records"]) == 1
    assert len(EndpointRegistry(tmp_path).list_by_owner("bot-a", "owner")) == 1


def test_threaded_cross_platform_same_owner_name_both_succeed(tmp_path):
    registry = EndpointRegistry(tmp_path)
    results = _race(
        [
            lambda: registry.create_private_endpoint(
                "bot-a", "owner", "same", "bot-a:FriendMessage:owner"
            ),
            lambda: registry.create_private_endpoint(
                "bot-b", "owner", "same", "bot-b:FriendMessage:owner"
            ),
        ]
    )

    assert all(result[0] == "ok" for result in results)
    records = [result[1][0] for result in results]
    assert len({record.path for record in records}) == 2
    assert len(json.loads((tmp_path / REGISTRY_FILENAME).read_text())["records"]) == 2


def test_threaded_double_verify_consumes_pending_once(tmp_path):
    registry = EndpointRegistry(tmp_path)
    _, request_id, code = registry.create_pending_verification(
        "bot-a", "owner", "group", "123"
    )
    results = _race(
        [
            lambda: registry.verify_group_endpoint(
                "bot-a", request_id, code, "owner", "123", "owner"
            ),
            lambda: registry.verify_group_endpoint(
                "bot-a", request_id, code, "owner", "123", "owner"
            ),
        ]
    )

    statuses = [result[1][0] for result in results]
    assert statuses.count("ok") == 1
    assert statuses.count("error") == 1
    assert registry.get_pending_descriptor("bot-a", request_id) is None
    record = registry.get_scoped("bot-a", "owner", "group")
    assert record is not None and record.status == "active"


def test_threaded_qq_confirm_generates_one_plain_token(tmp_path, monkeypatch):
    registry = EndpointRegistry(tmp_path)
    _, request_id, code = registry.create_group_pending(
        "qq-bot", "owner", "group", BIND_CURRENT_GROUP, None
    )
    registry.verify_group_endpoint(
        "qq-bot", request_id, code, None, "group-openid", "admin"
    )
    generated: list[str] = []
    real_generate = registry_module.generate_token

    def spy_generate():
        token = real_generate()
        generated.append(token)
        return token

    monkeypatch.setattr(registry_module, "generate_token", spy_generate)
    results = _race(
        [
            lambda: registry.confirm_group_endpoint("qq-bot", request_id, "owner"),
            lambda: registry.confirm_group_endpoint("qq-bot", request_id, "owner"),
        ]
    )

    values = [result[1] for result in results]
    assert [value[0] for value in values].count("ok") == 1
    assert len(generated) == 1
    returned = [value[3] for value in values if value[3] is not None]
    assert returned == generated


def test_threaded_rotates_are_linearizable_and_reload_matches(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, old_token = registry.create_private_endpoint(
        "bot-a", "owner", "name", "bot-a:FriendMessage:owner"
    )
    results = _race(
        [
            lambda: registry.rotate_token("bot-a", "owner", "name"),
            lambda: registry.rotate_token("bot-a", "owner", "name"),
        ]
    )

    tokens = [result[1][1] for result in results]
    assert all(result[1][0] is True for result in results)
    assert not registry.authenticate_delivery(
        record.path, f"Bearer {old_token}"
    ).authorized
    assert (
        sum(
            registry.authenticate_delivery(record.path, f"Bearer {token}").authorized
            for token in tokens
        )
        == 1
    )
    reloaded = EndpointRegistry(tmp_path)
    assert reloaded.get_scoped("bot-a", "owner", "name") == registry.get_scoped(
        "bot-a", "owner", "name"
    )
    assert (
        sum(
            reloaded.authenticate_delivery(record.path, f"Bearer {token}").authorized
            for token in tokens
        )
        == 1
    )


def test_threaded_rotate_vs_revoke_has_explainable_final_state(tmp_path):
    registry = EndpointRegistry(tmp_path)
    record, old_token = registry.create_private_endpoint(
        "bot-a", "owner", "name", "bot-a:FriendMessage:owner"
    )
    results = _race(
        [
            lambda: registry.rotate_token("bot-a", "owner", "name"),
            lambda: registry.revoke_endpoint("bot-a", "owner", "name"),
        ]
    )

    rotate = results[0][1]
    revoke = results[1][1]
    assert revoke[0] is True
    assert rotate[0] in {True, False}
    final = registry.get_scoped("bot-a", "owner", "name")
    assert final is not None and final.status == "revoked"
    assert not registry.authenticate_delivery(
        record.path, f"Bearer {old_token}"
    ).authorized
    if rotate[0]:
        assert not registry.authenticate_delivery(
            record.path, f"Bearer {rotate[1]}"
        ).authorized
    reloaded = EndpointRegistry(tmp_path).get_scoped("bot-a", "owner", "name")
    assert reloaded is not None and reloaded.status == "revoked"


def test_persistence_failure_candidate_is_not_visible_to_other_thread(
    tmp_path, monkeypatch
):
    registry = EndpointRegistry(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    observer_done = threading.Event()
    original_write = registry._atomic_write_bytes

    def failing_write(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        raise RegistryPersistenceError("injected failure")

    monkeypatch.setattr(registry, "_atomic_write_bytes", failing_write)
    mutation_result: list[Exception] = []
    observed = []

    def mutate():
        try:
            registry.create_private_endpoint(
                "bot-a", "owner", "name", "bot-a:FriendMessage:owner"
            )
        except Exception as exc:  # noqa: BLE001
            mutation_result.append(exc)

    def observe():
        observed.append(registry.get_scoped("bot-a", "owner", "name"))
        observer_done.set()

    writer = threading.Thread(target=mutate)
    writer.start()
    assert entered.wait(timeout=5)
    reader = threading.Thread(target=observe)
    reader.start()
    time.sleep(0.05)
    assert not observer_done.is_set()
    release.set()
    writer.join(timeout=5)
    reader.join(timeout=5)

    assert isinstance(mutation_result[0], RegistryPersistenceError)
    assert observed == [None]
    monkeypatch.setattr(registry, "_atomic_write_bytes", original_write)
    assert (
        registry.create_private_endpoint(
            "bot-a", "owner", "name", "bot-a:FriendMessage:owner"
        )[0].name
        == "name"
    )
