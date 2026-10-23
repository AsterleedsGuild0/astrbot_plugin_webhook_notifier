"""OMP 观察区间的严格协议与共用区间并集边界。"""
from __future__ import annotations

import copy

import pytest

from core.providers import ProviderError
from core.renderer import _timeline_coverage_union_ms, _timeline_reliable_interval
from tests.test_omp_v2 import envelope, parse


def timeline_envelope():
    body = envelope()
    body["cycle"]["timeBasis"] = "receipt_monotonic"
    body["subagentTimeline"] = {
        "version": 1, "timeBasis": "omp_agent_observation", "partial": False,
        "partialReasons": [], "truncated": False, "observedItemCount": 3,
        "displayedItemCount": 3,
        "items": [
            {"ref": "4" * 32, "cycleRef": "4" * 32, "parentRef": "3" * 32, "depth": 1, "attempt": 1, "status": "completed", "name": "并发一", "agent": "task", "model": "fixture/model", "thinking": "high", "timingQuality": "observed", "startOffsetMs": 1000, "endOffsetMs": 8000, "durationMs": 7000},
            {"ref": "5" * 32, "cycleRef": "5" * 32, "parentRef": "3" * 32, "depth": 1, "attempt": 1, "status": "completed", "name": "并发二", "timingQuality": "observed", "startOffsetMs": 2000, "endOffsetMs": 9000, "durationMs": 7000},
            {"ref": "6" * 32, "cycleRef": "6" * 32, "parentRef": "4" * 32, "depth": 2, "attempt": 1, "status": "failed", "name": "嵌套任务", "timingQuality": "observed", "startOffsetMs": 3000, "endOffsetMs": 7000, "durationMs": 4000},
        ],
    }
    body["userWaitTimeline"] = {
        "version": 1, "timeBasis": "omp_interaction_observation", "partial": False,
        "partialReasons": [], "truncated": False, "observedIntervalCount": 1,
        "displayedIntervalCount": 1,
        "intervals": [{"ref": "7" * 32, "agentRef": "3" * 32, "kind": "ask", "result": "resolved", "intervalState": "complete", "startOffsetMs": 4000, "endOffsetMs": 6000, "durationMs": 2000}],
    }
    return body


def test_concurrent_and_nested_observations_use_union_not_duration_sum():
    event = parse(timeline_envelope())
    intervals = [_timeline_reliable_interval(item) for item in event.subagent_timeline["items"]]
    assert _timeline_coverage_union_ms(intervals, event.task_duration_ms) == 8000
    assert sum(item["durationMs"] for item in event.subagent_timeline["items"]) == 18000
    assert event.user_wait_timeline["intervals"][0]["kind"] == "ask"


@pytest.mark.parametrize("path,value", [
    (("version",), True), (("timeBasis",), "root_cycle"), (("partial",), True),
    (("partialReasons",), ["private-error"]), (("truncated",), True),
    (("observedItemCount",), True), (("displayedItemCount",), 2),
    (("observedItemCount",), 4097), (("observedItemCount",), 4),
    (("items", 0, "startOffsetMs"), True), (("items", 0, "endOffsetMs"), -1),
    (("items", 0, "endOffsetMs"), 21000), (("items", 0, "durationMs"), 7001),
    (("items", 0, "durationMs"), 7.5), (("items", 0, "durationMs"), float("nan")),
    (("items", 0, "depth"), 0), (("items", 0, "depth"), 9), (("items", 0, "attempt"), True),
    (("items", 0, "cycleRef"), "3" * 32), (("items", 0, "parentRef"), "4" * 32),
    (("items", 0, "parentRef"), "6" * 32), (("items", 0, "parentRef"), "a" * 32),
    (("items", 1, "ref"), "4" * 32), (("items", 1, "cycleRef"), "4" * 32),
    (("items", 0, "status"), "success"), (("items", 0, "timingQuality"), "fallback"),
    (("items", 0, "name"), "x" * 97), (("items", 0, "model"), "/private/sentinel"),
    (("items", 0, "thinking"), []), (("items", 0, "metadataChanged"), True),
    (("items", 0, "prompt"), "PRIVATE_SENTINEL"),
])
def test_invalid_subagent_timeline_rejected_without_echo(path, value):
    body = timeline_envelope()
    target = body["subagentTimeline"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProviderError) as error:
        parse(body)
    assert "PRIVATE_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("path,value", [
    (("timeBasis",), "root_cycle_receipt_monotonic"), (("partial",), True),
    (("observedIntervalCount",), 2), (("displayedIntervalCount",), 0),
    (("intervals", 0, "kind"), "question"), (("intervals", 0, "result"), "replied"),
    (("intervals", 0, "intervalState"), "right_censored"),
    (("intervals", 0, "agentRef"), "a" * 32), (("intervals", 0, "ref"), "raw-id"),
    (("intervals", 0, "durationMs"), True), (("intervals", 0, "durationMs"), 1999),
    (("intervals", 0, "startOffsetMs"), 7000),
    (("intervals", 0, "question"), "PRIVATE_SENTINEL"),
])
def test_invalid_interaction_timeline_rejected(path, value):
    body = timeline_envelope()
    target = body["userWaitTimeline"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProviderError):
        parse(body)


@pytest.mark.parametrize("kind", ["approval", "plan_review", "ask", "background_wait"])
def test_complete_observation_keeps_kind_and_not_pure_human_semantics(kind):
    body = timeline_envelope()
    body["userWaitTimeline"]["intervals"][0]["kind"] = kind
    event = parse(body)
    assert event.user_wait_timeline["timeBasis"] == "omp_interaction_observation"
    assert event.user_wait_timeline["intervals"][0]["kind"] == kind


def test_open_interval_does_not_gain_exact_duration_or_complete_coverage():
    body = timeline_envelope()
    timeline = body["subagentTimeline"]
    item = timeline["items"][0]
    item.pop("endOffsetMs")
    item.pop("durationMs")
    item.update(timingQuality="partial", status="running")
    timeline.update(partial=True, partialReasons=["missing_end"])
    waits = body["userWaitTimeline"]
    waits["intervals"][0] = {"ref": "7" * 32, "agentRef": "3" * 32, "kind": "approval", "intervalState": "right_censored", "startOffsetMs": 4000}
    waits.update(partial=True, partialReasons=["missing_end"])
    event = parse(body)
    assert _timeline_reliable_interval(event.subagent_timeline["items"][0]) is None
    assert "durationMs" not in event.user_wait_timeline["intervals"][0]
    body["userWaitTimeline"]["intervals"][0]["durationMs"] = 16000
    with pytest.raises(ProviderError):
        parse(body)


def test_worker_continuation_is_distinct_segment_in_same_cycle():
    body = timeline_envelope()
    items = body["subagentTimeline"]["items"]
    items[0]["status"] = "continued"
    items[1].update(cycleRef=items[0]["cycleRef"], attempt=2, startOffsetMs=10000, endOffsetMs=11000, durationMs=1000)
    parsed = parse(body).subagent_timeline["items"]
    assert parsed[0]["cycleRef"] == parsed[1]["cycleRef"]
    assert parsed[0]["ref"] != parsed[1]["ref"]


def test_clock_jump_requires_explicit_evidence_with_monotonic_basis():
    body = timeline_envelope()
    body["cycle"]["endedAt"] = "2026-10-23T02:00:00Z"
    with pytest.raises(ProviderError):
        parse(body)
    body["cycle"].update(partial=True, reasons=["clock_changed"])
    assert parse(body).task_duration_ms == 20000


@pytest.mark.parametrize("scope", ["subagent", "unknown"])
def test_only_root_terminal_events_carry_aggregate_timelines(scope):
    body = timeline_envelope()
    body["session"]["scope"] = scope
    with pytest.raises(ProviderError):
        parse(body)


def test_action_notification_cannot_carry_completion_aggregate():
    action = envelope("action_required")
    action["cycle"]["timeBasis"] = "receipt_monotonic"
    action["subagentTimeline"] = timeline_envelope()["subagentTimeline"]
    with pytest.raises(ProviderError):
        parse(action)


def test_bounded_truncation_preserves_partial_and_never_fills_missing_records():
    body = timeline_envelope()
    timeline = body["subagentTimeline"]
    timeline.update(partial=True, partialReasons=["truncated"], truncated=True, observedItemCount=80)
    event = parse(body)
    assert event.subagent_timeline["observedItemCount"] == 80
    assert event.subagent_timeline["displayedItemCount"] == 3
    oversized = copy.deepcopy(body)
    oversized["subagentTimeline"]["items"] *= 22
    oversized["subagentTimeline"]["displayedItemCount"] = 66
    with pytest.raises(ProviderError):
        parse(oversized)


@pytest.mark.parametrize("waits", [False, True])
def test_timeline_byte_limit_is_independent_of_envelope_limit(waits):
    body = timeline_envelope()
    key = "userWaitTimeline" if waits else "subagentTimeline"
    rows_key, count_key = ("intervals", "Interval") if waits else ("items", "Item")
    timeline = body[key]
    template = timeline[rows_key][0]
    rows = []
    for i in range(64 if waits else 30):
        row = dict(template, ref=f"{i + 16:032x}")
        if not waits:
            row.update(cycleRef=row["ref"], name="长" * 96, agent="代" * 96, model="m" * 128)
        rows.append(row)
    timeline.update({rows_key: rows, f"observed{count_key}Count": len(rows), f"displayed{count_key}Count": len(rows)})
    with pytest.raises(ProviderError):
        parse(body)
