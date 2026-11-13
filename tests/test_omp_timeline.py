"""OMP 观察区间的严格协议与共用区间并集边界。"""
from __future__ import annotations

import copy

import pytest

from core.providers import ProviderError
from itertools import pairwise

from core.models import NormalizedEvent
from core.notification_policy import SessionScope
from core.renderer import (
    SUBAGENT_TIMELINE_FOLD_MIN_DURATION_MS,
    SUBAGENT_TIMELINE_FOLD_MIN_RATIO,
    SUBAGENT_TIMELINE_FOLD_WIDTH_PX,
    _TIMELINE_FOLD_TICK_GAP_PX,
    _TIMELINE_TICK_SAFETY_GAP_PX,
    _build_piecewise_timeline_axis,
    _build_subagent_timeline_view,
    _folded_tick_box,
    _format_folded_timeline_tick,
    _timeline_coverage_union_ms,
    _timeline_reliable_interval,
    render_html_default,
    render_subagent_timeline,
    render_text_default,
)
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


@pytest.mark.parametrize("model", [
    "local//private/PRIVATE_SENTINEL/model",
    r"local/C:\private\PRIVATE_SENTINEL\model",
    "local/~/PRIVATE_SENTINEL/model",
    r"local/\server\PRIVATE_SENTINEL\model",
])
@pytest.mark.parametrize("child", [False, True])
def test_composed_model_path_rejected_before_display(model, child):
    body = timeline_envelope()
    target = body["subagentTimeline"]["items"][0] if child else body["cycle"]
    target["model"] = model
    with pytest.raises(ProviderError) as error:
        parse(body)
    assert "PRIVATE_SENTINEL" not in str(error.value)


def _omp_run_event(
    *,
    duration_ms: int,
    items: list[dict] | None = None,
    waits: list[dict] | None = None,
    subagent_reasons: list[str] | None = None,
    wait_reasons: list[str] | None = None,
    session_name: str = "测试会话",
) -> NormalizedEvent:
    from datetime import datetime, timedelta, timezone

    body = envelope()
    body["session"]["name"] = session_name
    start_dt = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(milliseconds=duration_ms)
    body["emittedAt"] = end_dt.isoformat().replace("+00:00", "Z")
    body["cycle"].update(
        {
            "startedAt": start_dt.isoformat().replace("+00:00", "Z"),
            "endedAt": end_dt.isoformat().replace("+00:00", "Z"),
            "durationMs": duration_ms,
            "timeBasis": "receipt_monotonic",
        }
    )
    s_reasons = subagent_reasons or []
    w_reasons = wait_reasons or []
    item_list = items if items is not None else []
    wait_list = waits if waits is not None else []
    body["subagentTimeline"] = {
        "version": 1,
        "timeBasis": "omp_agent_observation",
        "partial": bool(s_reasons),
        "partialReasons": s_reasons,
        "truncated": "truncated" in s_reasons,
        "observedItemCount": len(item_list),
        "displayedItemCount": len(item_list),
        "items": item_list,
    }
    body["userWaitTimeline"] = {
        "version": 1,
        "timeBasis": "omp_interaction_observation",
        "partial": bool(w_reasons),
        "partialReasons": w_reasons,
        "truncated": "truncated" in w_reasons,
        "observedIntervalCount": len(wait_list),
        "displayedIntervalCount": len(wait_list),
        "intervals": wait_list,
    }
    return parse(body)


def _omp_subagent(
    idx: int,
    start_ms: int | None,
    end_ms: int | None,
    *,
    status: str = "completed",
    quality: str = "observed",
    name: str | None = None,
) -> dict:
    ref = f"{idx + 100:032x}"
    row: dict = {
        "ref": ref,
        "cycleRef": ref,
        "parentRef": "3" * 32,
        "depth": 1,
        "attempt": 1,
        "status": status,
        "timingQuality": quality,
        "name": name or f"子代理-{idx}",
        "agent": "task",
        "model": "fixture/model",
        "thinking": "high",
    }
    if start_ms is not None:
        row["startOffsetMs"] = start_ms
    if end_ms is not None:
        row["endOffsetMs"] = end_ms
    if quality == "observed" and start_ms is not None and end_ms is not None:
        row["durationMs"] = end_ms - start_ms
    return row


def _omp_wait(
    idx: int,
    kind: str,
    start_ms: int | None,
    end_ms: int | None,
    *,
    state: str = "complete",
    result: str = "resolved",
) -> dict:
    row: dict = {
        "ref": f"{idx + 500:032x}",
        "agentRef": "3" * 32,
        "kind": kind,
        "intervalState": state,
    }
    if state == "complete":
        assert start_ms is not None and end_ms is not None
        row.update(
            {
                "startOffsetMs": start_ms,
                "endOffsetMs": end_ms,
                "durationMs": end_ms - start_ms,
                "result": result,
            }
        )
    elif state == "right_censored":
        assert start_ms is not None
        row["startOffsetMs"] = start_ms
    elif state == "left_censored":
        assert end_ms is not None
        row.update({"endOffsetMs": end_ms, "result": result})
    return row


@pytest.mark.parametrize("kind", ["ask", "approval", "plan_review"])
def test_fold_eligible_complete_interaction_waits_without_subagents(kind):
    event = _omp_run_event(
        duration_ms=1_200_000,
        items=[
            _omp_subagent(1, 0, 120_000),
            _omp_subagent(2, 960_000, 1_140_000),
        ],
        waits=[_omp_wait(1, kind, 180_000, 900_000)],
    )
    view = _build_subagent_timeline_view(event)
    assert view is not None and view["axis_folded"] is True
    assert view["px_per_ms"] is None
    assert view["fold_count"] == 1
    assert view["fold_total_ms"] == 720_000
    assert view["fold_segments"][0]["start_ms"] == 180_000
    assert view["fold_segments"][0]["end_ms"] == 900_000
    assert view["fold_segments"][0]["width_px"] == str(SUBAGENT_TIMELINE_FOLD_WIDTH_PX)
    assert view["fold_summary_text"] == "已折叠 1 段交互等待，共 12 分钟"


def test_fold_excludes_subagent_overlap_idle_gaps_background_and_censored():
    # 1) Partial subagent overlap: [100s, 300s] overlaps subagent, only [300s, 900s] folds.
    # 2) Idle main-agent gap [900s, 1500s] has neither subagent nor wait -> never folds.
    # 3) background_wait [1500s, 2100s] -> never folds (and blocks overlapping ask).
    # 4) right_censored ask [2200s, ...] -> never folds.
    event = _omp_run_event(
        duration_ms=2_800_000,
        items=[_omp_subagent(1, 50_000, 300_000)],
        waits=[
            _omp_wait(1, "ask", 100_000, 900_000),
            _omp_wait(2, "background_wait", 1_500_000, 2_100_000),
            _omp_wait(3, "ask", 1_550_000, 2_050_000),
            _omp_wait(4, "ask", 2_200_000, None, state="right_censored"),
        ],
        wait_reasons=["missing_end"],
    )
    view = _build_subagent_timeline_view(event)
    assert view is not None and view["axis_folded"] is True
    assert [(seg["start_ms"], seg["end_ms"]) for seg in view["fold_segments"]] == [
        (300_000.0, 900_000.0)
    ]
    # Left-censored and unknown waits also never fold.
    censored_only = _omp_run_event(
        duration_ms=1_200_000,
        items=[_omp_subagent(1, 600_000, 700_000)],
        waits=[
            _omp_wait(1, "ask", None, 500_000, state="left_censored"),
            _omp_wait(2, "approval", None, None, state="unknown"),
        ],
        wait_reasons=["missing_start", "missing_end"],
    )
    censored_view = _build_subagent_timeline_view(censored_only)
    assert censored_view is not None and censored_view["axis_folded"] is False


def test_incomplete_or_running_subagent_boundaries_block_folding():
    # Subagent missing end at 200s blocks [200s, end], preventing fold of [300s, 900s].
    missing_end = _omp_run_event(
        duration_ms=1_000_000,
        items=[
            _omp_subagent(
                1, 200_000, None, status="running", quality="partial"
            )
        ],
        waits=[_omp_wait(1, "ask", 300_000, 900_000)],
        subagent_reasons=["missing_end"],
    )
    assert _build_subagent_timeline_view(missing_end)["axis_folded"] is False

    # Subagent missing start ending at 800s blocks [0, 800s], preventing fold of [100s, 700s].
    missing_start = _omp_run_event(
        duration_ms=1_000_000,
        items=[_omp_subagent(1, None, 800_000, quality="partial")],
        waits=[_omp_wait(1, "ask", 100_000, 700_000)],
        subagent_reasons=["missing_start"],
    )
    assert _build_subagent_timeline_view(missing_start)["axis_folded"] is False

    # Unlocated subagent (unknown start & end) blocks entire axis.
    unlocated = _omp_run_event(
        duration_ms=1_000_000,
        items=[_omp_subagent(1, None, None, quality="unknown")],
        waits=[_omp_wait(1, "ask", 100_000, 700_000)],
        subagent_reasons=["missing_start", "missing_end"],
    )
    assert _build_subagent_timeline_view(unlocated)["axis_folded"] is False


def test_opencode_timeline_never_folds():
    event = NormalizedEvent(
        provider="opencode",
        event="opencode.session_idle",
        version=1,
        id="oc-1",
        emitted_at="2026-07-08T12:00:00Z",
        title="完成",
        status="success",
        summary="",
        source={"name": "OpenCode", "url": None},
        actor={"name": None, "url": None},
        fields=[],
        links=[],
        raw={},
        session_scope=SessionScope.ROOT,
        task_duration_ms=3_600_000,
        subagent_timeline={
            "version": 1,
            "timeBasis": "root_cycle",
            "partial": False,
            "partialReasons": [],
            "truncated": False,
            "observedItemCount": 1,
            "displayedItemCount": 1,
            "items": [
                {
                    "ref": "1" * 32,
                    "parentRef": "f" * 32,
                    "name": "worker-1",
                    "status": "completed",
                    "timingQuality": "observed",
                    "depth": 1,
                    "attempt": 1,
                    "startOffsetMs": 0,
                    "endOffsetMs": 60_000,
                    "durationMs": 60_000,
                }
            ],
        },
        user_wait_timeline={
            "version": 1,
            "timeBasis": "root_cycle_receipt_monotonic",
            "partial": False,
            "partialReasons": [],
            "truncated": False,
            "observedIntervalCount": 1,
            "displayedIntervalCount": 1,
            "intervals": [
                {
                    "kind": "question",
                    "intervalState": "complete",
                    "result": "replied",
                    "startOffsetMs": 120_000,
                    "endOffsetMs": 3_400_000,
                    "durationMs": 3_280_000,
                }
            ],
        },
    )
    view = _build_subagent_timeline_view(event)
    assert view is not None
    assert view["axis_folded"] is False
    assert isinstance(view["px_per_ms"], float)


@pytest.mark.parametrize(
    ("total_ms", "wait_ms", "expected_folded"),
    [
        # Duration threshold boundary (180_000 ms) when ratio is 30% >= 12%
        (600_000, SUBAGENT_TIMELINE_FOLD_MIN_DURATION_MS - 1, False),
        (600_000, SUBAGENT_TIMELINE_FOLD_MIN_DURATION_MS, True),
        # Ratio threshold boundary (12% of 2_000_000 = 240_000 ms >= 180_000 ms)
        (2_000_000, int(2_000_000 * SUBAGENT_TIMELINE_FOLD_MIN_RATIO) - 1, False),
        (2_000_000, int(2_000_000 * SUBAGENT_TIMELINE_FOLD_MIN_RATIO), True),
    ],
)
def test_fold_threshold_boundaries(total_ms, wait_ms, expected_folded):
    event = _omp_run_event(
        duration_ms=total_ms,
        items=[_omp_subagent(1, 0, 30_000)],
        waits=[_omp_wait(1, "ask", 60_000, 60_000 + wait_ms)],
    )
    view = _build_subagent_timeline_view(event)
    assert view is not None
    assert view["axis_folded"] is expected_folded


def test_piecewise_geometry_monotonicity_linear_width_ratios_and_tick_layout():
    # 18h 13m run modeled on Issue #42: two long Asks (3h and 7h), subagents in all 3 linear zones.
    total_ms = (18 * 60 + 13) * 60_000  # 65,580,000 ms
    h = 3_600_000
    m = 60_000
    event = _omp_run_event(
        duration_ms=total_ms,
        items=[
            _omp_subagent(1, 10 * m, 40 * m, name="早期探索"),      # 30 min in L1
            _omp_subagent(2, 5 * h + 15 * m, 6 * h + 15 * m, name="中期实现"),  # 60 min in L2
            _omp_subagent(3, 15 * h + 20 * m, 16 * h + 50 * m, name="后期回归"),  # 90 min in L3
        ],
        waits=[
            _omp_wait(1, "ask", 2 * h, 5 * h),    # 3h fold
            _omp_wait(2, "ask", 8 * h, 15 * h),   # 7h fold
        ],
    )
    view = _build_subagent_timeline_view(event)
    assert view is not None and view["axis_folded"] is True
    assert view["fold_count"] == 2
    assert view["fold_total_ms"] == 10 * h

    plot_width = view["layout"]["plot_width"]
    axis = _build_piecewise_timeline_axis(
        timeline_end_ms=float(total_ms),
        plot_scale_end_ms=float(total_ms),
        plot_width=plot_width,
        fold_intervals=[(2.0 * h, 5.0 * h), (8.0 * h, 15.0 * h)],
    )
    sample_points = [total_ms * idx / 400.0 for idx in range(401)]
    mapped = [axis.map_ms(point) for point in sample_points]
    assert mapped[0] == pytest.approx(0.0)
    assert mapped[-1] == pytest.approx(float(plot_width))
    assert all(right > left for left, right in pairwise(mapped))

    # Subagent width ratios across distinct linear segments equal exact duration ratios (1 : 2 : 3).
    bars = {item["name"].split(" · ")[0]: float(item["width_px"]) for item in view["gantt_items"]}
    assert bars["中期实现"] / bars["早期探索"] == pytest.approx(2.0, rel=1e-3)
    assert bars["后期回归"] / bars["早期探索"] == pytest.approx(3.0, rel=1e-3)

    # Folded segments have fixed pixel width regardless of real duration (3h vs 7h).
    fold_widths = [float(seg["width_px"]) for seg in view["fold_segments"]]
    assert fold_widths == [
        float(SUBAGENT_TIMELINE_FOLD_WIDTH_PX),
        float(SUBAGENT_TIMELINE_FOLD_WIDTH_PX),
    ]

    # Ticks: real relative time, never inside folded segments, never overlapping folds or neighbors.
    ticks = view["axis_ticks"]
    assert len(ticks) >= 4
    labels = [tick["label"] for tick in ticks]
    assert len(labels) == len(set(labels))
    positions = [float(tick["left_px"]) for tick in ticks]
    assert all(right > left for left, right in pairwise(positions))
    boxes: list[tuple[float, float]] = []
    for tick in ticks:
        val = float(tick["value_ms"])
        for fold in view["fold_segments"]:
            assert not (fold["start_ms"] <= val <= fold["end_ms"])
        # Label matches real relative time (post-break ticks must exceed 5h / 15h).
        if val >= 15 * h:
            assert tick["label"].startswith(("+15小时", "+16小时", "+17小时", "+18小时"))
        edge_class, box_left, box_right = _folded_tick_box(
            float(tick["left_px"]), float(tick["label_width_px"]), plot_width
        )
        assert edge_class == tick["edge_class"]
        assert box_left >= -1e-6 and box_right <= plot_width + 1e-6
        for fold in view["fold_segments"]:
            f_left = float(fold["left_px"])
            f_right = f_left + float(fold["width_px"])
            assert (
                box_right + _TIMELINE_FOLD_TICK_GAP_PX <= f_left + 1e-6
                or box_left - _TIMELINE_FOLD_TICK_GAP_PX >= f_right - 1e-6
            )
        boxes.append((box_left, box_right))
    for (_, left_box_right), (right_box_left, _) in pairwise(boxes):
        assert right_box_left - left_box_right + 1e-6 >= _TIMELINE_TICK_SAFETY_GAP_PX
    assert any(float(tick["value_ms"]) > 15 * h for tick in ticks)
    assert _format_folded_timeline_tick(3 * h + 2 * m + 30_000, 30_000) == "+3小时2分30秒"


def test_wait_bar_crossing_fold_shortens_visually_while_all_numbers_stay_identical():
    total_ms = 7_200_000  # 2h
    event = _omp_run_event(
        duration_ms=total_ms,
        items=[
            _omp_subagent(1, 60_000, 660_000),      # 1m..11m (overlaps start of Ask 1)
            _omp_subagent(2, 6_000_000, 6_900_000),  # 100m..115m
        ],
        waits=[
            # Ask 1: 6m..66m (60m total: 6m..11m overlaps subagent 1, 11m..66m folds!)
            _omp_wait(1, "ask", 360_000, 3_960_000),
            _omp_wait(2, "approval", 4_200_000, 5_700_000),  # 70m..95m (25m fold)
            _omp_wait(3, "background_wait", 6_900_000, 7_080_000),
        ],
    )
    folded = _build_subagent_timeline_view(event, allow_fold=True)
    unfolded = _build_subagent_timeline_view(event, allow_fold=False)
    assert folded is not None and unfolded is not None
    assert folded["axis_folded"] is True and unfolded["axis_folded"] is False
    assert folded["fold_count"] == 2

    # Ask 1 spans [6m, 66m]: linear 5m + folded 55m, visually much narrower than linear 60m.
    folded_ask = next(w for w in folded["wait_intervals"] if w["kind"] == "ask")
    assert folded_ask["spans_fold"] is True
    expected_ask_width = 300_000 * folded["linear_px_per_ms"] + SUBAGENT_TIMELINE_FOLD_WIDTH_PX
    assert float(folded_ask["width_px"]) == pytest.approx(expected_ask_width, abs=0.05)
    assert float(folded_ask["width_px"]) < 3_600_000 * folded["linear_px_per_ms"]

    # Every displayed number / summary / coverage / unclassified / state duration is 100% identical.
    assert folded["metrics"] == unfolded["metrics"]
    assert folded["wait_summary_text"] == unfolded["wait_summary_text"]
    assert folded["wait_summary_detail"] == unfolded["wait_summary_detail"]
    assert folded["summary_text"] == unfolded["summary_text"]
    assert folded["timing_summary"] == unfolded["timing_summary"]
    assert folded["flags"] == unfolded["flags"]
    assert folded["notice"] == unfolded["notice"]
    assert folded["coverage_union_ms"] == unfolded["coverage_union_ms"]
    assert folded["coverage_rate"] == unfolded["coverage_rate"]
    assert folded["unclassified_ms"] == unfolded["unclassified_ms"]
    assert folded["unclassified_rate"] == unfolded["unclassified_rate"]
    assert [
        (track["label"], track["state_label"], track["state_time"])
        for track in folded["wait_tracks"]
    ] == [
        (track["label"], track["state_label"], track["state_time"])
        for track in unfolded["wait_tracks"]
    ]
    assert [w["timing_label"] for w in folded["wait_intervals"]] == [
        w["timing_label"] for w in unfolded["wait_intervals"]
    ]
    assert [
        (item["name"], item["status_label"], item["timing_label"])
        for item in folded["gantt_items"]
    ] == [
        (item["name"], item["status_label"], item["timing_label"])
        for item in unfolded["gantt_items"]
    ]

    # Attachment HTML contains break markers, folded durations, and header summary in both themes.
    for theme in ("light", "dark"):
        rendered = render_subagent_timeline(event, theme=theme)
        assert rendered is not None
        assert "已折叠 2 段交互等待" in rendered.html
        assert 'class="axis-fold"' in rendered.html
        assert 'class="track-fold"' in rendered.html
    assert "已折叠" not in render_html_default(event)
    assert "已折叠" not in render_text_default(event)


def test_short_subagent_before_fold_boundary_keeps_minimum_width_outside_fold():
    event = _omp_run_event(
        duration_ms=3_600_000,
        items=[_omp_subagent(1, 599_000, 600_000)],
        waits=[_omp_wait(1, "ask", 600_000, 3_000_000)],
    )
    view = _build_subagent_timeline_view(event)
    assert view is not None and view["axis_folded"] is True
    bar = view["gantt_items"][0]
    fold = view["fold_segments"][0]
    assert bar["minimum_width_applied"] is True
    assert float(bar["width_px"]) == pytest.approx(8.0)
    assert float(bar["left_px"]) + float(bar["width_px"]) <= float(fold["left_px"]) + 1e-6
