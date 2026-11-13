"""Real offline Chrome geometry and screenshot smoke for the timeline canvas."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from core.renderer import (
    _build_subagent_timeline_view,
    render_subagent_timeline,
)
from tests.browser_timeline_smoke import capture_timeline_browser_geometry
from tests.test_omp_timeline import _omp_run_event, _omp_subagent, _omp_wait
from tests.test_renderer import (
    _timeline_event,
    _timeline_item,
    _wait_interval,
    _wait_timeline,
)

_LIGHT_PALETTE = {
    "page": "rgb(238, 242, 246)",
    "timeline": "rgb(248, 250, 252)",
    "axis": "rgb(233, 239, 246)",
    "border": "rgb(203, 213, 225)",
    "text": "rgb(36, 50, 71)",
    "muted": "rgb(96, 112, 134)",
    "axis_text": "rgb(67, 84, 106)",
    "completed": "rgb(111, 174, 136)",
    "failed": "rgb(217, 135, 141)",
    "running": "rgb(216, 173, 85)",
    "unknown": "rgb(127, 157, 190)",
}
_OLD_DARK_SURFACES = {
    "rgb(13, 20, 32)",
    "rgb(17, 25, 37)",
    "rgb(18, 27, 39)",
    "rgb(21, 31, 44)",
}


def _assert_pixel_close(
    actual: tuple[int, int, int], expected: tuple[int, int, int], tolerance: int = 4
) -> None:
    assert all(
        abs(actual_channel - expected_channel) <= tolerance
        for actual_channel, expected_channel in zip(actual, expected, strict=True)
    )


def _rgb_pixel(image: Image.Image, position: tuple[int, int]) -> tuple[int, int, int]:
    return cast(tuple[int, int, int], image.getpixel(position))


def _browser_scenario(item_count: int):
    chinese = ("长中文任务名称需要完整折行并保留末尾差异" * 12)[:188]
    english = (
        "Long English task name with spaces must remain complete and readable " * 8
    )[:188]
    no_space = ("NoSpaceUnbrokenTaskIdentifier" * 12)[:188]
    bases = (chinese, english, no_space)
    statuses = ("completed", "failed", "running", "unknown")
    long_span = 30 * 86_400_000
    items = []
    expected_names = []
    for index in range(item_count):
        name = f"{bases[index % len(bases)]}-{index:02d}"
        expected_names.append(name)
        start = index * 1000
        if index == 0:
            end = long_span
        elif index % 5 == 0:
            end = start + 1
        else:
            end = start + 30_000 + index * 17
        items.append(
            _timeline_item(
                index,
                start=start,
                end=end,
                depth=3 if index % 11 == 0 else 1,
                status=statuses[index % len(statuses)],
                name=name,
            )
        )
    return _timeline_event(items), expected_names


@pytest.mark.parametrize(
    ("item_count", "expected_density"),
    [(25, "compact"), (64, "dense")],
)
def test_real_chrome_timeline_geometry_and_screenshot(
    tmp_path: Path, item_count: int, expected_density: str
):
    event, expected_names = _browser_scenario(item_count)
    rendered = render_subagent_timeline(event)
    assert rendered is not None
    assert rendered.layout.density == expected_density

    geometry, screenshot_path, executable = capture_timeline_browser_geometry(
        rendered.html,
        viewport_width=rendered.layout.viewport_width,
        viewport_height=rendered.layout.viewport_height,
        output_dir=tmp_path,
        name=f"timeline-{item_count}",
    )
    assert Path(executable).is_file()

    document = geometry["document"]
    body = geometry["body"]
    card = geometry["card"]
    overview = geometry["overview"]
    metrics = geometry["metrics"]
    timeline = geometry["timeline"]
    styles = geometry["styles"]
    rows = geometry["rows"]
    wait = geometry["wait"]
    assert document["scrollWidth"] == rendered.layout.viewport_width
    assert body["scrollWidth"] == rendered.layout.viewport_width
    assert document["scrollHeight"] <= rendered.layout.viewport_height
    assert body["scrollHeight"] <= rendered.layout.viewport_height
    assert card["left"] == pytest.approx(rendered.layout.body_padding)
    assert card["right"] <= document["scrollWidth"] - rendered.layout.body_padding
    assert timeline["right"] < card["right"]
    assert timeline["width"] == pytest.approx(
        rendered.layout.name_column_width
        + rendered.layout.plot_width
        + rendered.layout.state_column_width
        + 2
    )
    assert [metric["label"] for metric in metrics] == [
        "子任务数",
        "峰值并发",
        "总任务时长",
        "子任务覆盖时长",
        "子任务覆盖率",
        "未分类时间 / 占比 · 观测受限",
    ]
    assert len(metrics) == 6
    assert metrics[0]["metric"]["top"] == pytest.approx(metrics[2]["metric"]["top"])
    assert metrics[3]["metric"]["top"] > metrics[0]["metric"]["bottom"] - 1
    assert metrics[3]["metric"]["top"] == pytest.approx(metrics[4]["metric"]["top"])
    assert metrics[4]["metric"]["top"] == pytest.approx(metrics[5]["metric"]["top"])
    for metric in metrics:
        assert metric["metric"]["left"] >= overview["left"] - 0.1
        assert metric["metric"]["right"] <= overview["right"] + 0.1
        assert metric["scrollWidth"] <= metric["clientWidth"]
        assert metric["labelScrollWidth"] <= metric["labelClientWidth"]
    assert styles["body"]["backgroundColor"] == _LIGHT_PALETTE["page"]
    assert styles["card"]["backgroundColor"] == "rgb(255, 255, 255)"
    assert styles["timeline"]["backgroundColor"] == _LIGHT_PALETTE["timeline"]
    assert styles["timeline"]["borderTopColor"] == _LIGHT_PALETTE["border"]
    assert styles["axis"]["backgroundColor"] == _LIGHT_PALETTE["axis"]
    assert styles["axis"]["color"] == _LIGHT_PALETTE["axis_text"]
    assert "rgb(237, 243, 248)" in styles["axisTrack"]["backgroundImage"]
    assert "rgb(228, 236, 244)" in styles["axisTrack"]["backgroundImage"]
    assert styles["timeline"]["boxShadow"] != "none"

    assert len(rows) == item_count
    assert wait is not None
    assert wait["row"]["top"] > timeline["top"]
    assert wait["row"]["bottom"] <= rows[0]["row"]["top"] + 0.1
    assert wait["task"]["width"] == pytest.approx(rendered.layout.name_column_width)
    assert wait["plot"]["width"] == pytest.approx(rendered.layout.plot_width)
    assert wait["state"]["width"] == pytest.approx(rendered.layout.state_column_width)
    assert wait["scrollWidth"] <= wait["clientWidth"]
    assert wait["bars"] == []
    assert [row["text"] for row in rows] == expected_names
    assert rows[-1]["row"]["bottom"] <= document["scrollHeight"]
    assert any(
        row["name"]["height"] > rendered.layout.task_font_size * 2 for row in rows
    )
    task_backgrounds = {row["styles"]["task"]["backgroundColor"] for row in rows}
    assert task_backgrounds == {"rgb(255, 255, 255)", "rgb(244, 247, 250)"}
    plot_backgrounds = {row["styles"]["plot"]["backgroundImage"] for row in rows}
    assert len(plot_backgrounds) == 2
    assert any("rgb(240, 245, 250)" in value for value in plot_backgrounds)
    assert any("rgb(245, 248, 251)" in value for value in plot_backgrounds)

    status_fill = {
        "completed": _LIGHT_PALETTE["completed"],
        "failed": _LIGHT_PALETTE["failed"],
        "running": _LIGHT_PALETTE["running"],
        "unknown": _LIGHT_PALETTE["unknown"],
    }
    observed_statuses: set[str] = set()
    for row in rows:
        assert row["row"]["right"] <= timeline["right"] - 0.9
        assert row["task"]["width"] == pytest.approx(rendered.layout.name_column_width)
        assert row["plot"]["width"] == pytest.approx(rendered.layout.plot_width)
        assert row["state"]["width"] == pytest.approx(
            rendered.layout.state_column_width
        )
        assert row["state"]["right"] <= timeline["right"] - 0.9
        assert row["bar"] is not None
        assert row["bar"]["width"] >= 7.9
        assert row["styles"]["name"]["color"] == _LIGHT_PALETTE["text"]
        if row["styles"]["agent"] is not None:
            assert row["styles"]["agent"]["color"] == _LIGHT_PALETTE["muted"]
        assert row["styles"]["state"]["backgroundColor"] in task_backgrounds
        status = next(
            candidate
            for candidate in status_fill
            if candidate in row["barClass"].split()
        )
        observed_statuses.add(status)
        assert row["styles"]["bar"]["backgroundColor"] == status_fill[status]
        metrics = row["nameMetrics"]
        assert metrics["whiteSpace"] == "normal"
        assert metrics["overflowWrap"] == "anywhere"
        assert metrics["wordBreak"] == "normal"
        assert metrics["scrollWidth"] <= metrics["clientWidth"]
        assert metrics["scrollHeight"] == metrics["clientHeight"]
    assert observed_statuses == set(status_fill)
    assert any(row["bar"]["width"] == pytest.approx(8, abs=0.1) for row in rows)

    surface_colors = {
        styles["body"]["backgroundColor"],
        styles["timeline"]["backgroundColor"],
        styles["axis"]["backgroundColor"],
        *task_backgrounds,
    }
    assert _OLD_DARK_SURFACES.isdisjoint(surface_colors)
    assert not any(
        old_color in background
        for old_color in _OLD_DARK_SURFACES
        for background in plot_backgrounds
    )

    with Image.open(screenshot_path) as screenshot:
        assert screenshot.width >= document["scrollWidth"]
        assert screenshot.height >= document["scrollHeight"]
        pixels = screenshot.convert("RGB")
        _assert_pixel_close(_rgb_pixel(pixels, (2, 2)), (238, 242, 246))
        _assert_pixel_close(
            _rgb_pixel(
                pixels, (int(card["left"] + card["width"] / 2), int(card["top"] + 100))
            ),
            (255, 255, 255),
        )
        _assert_pixel_close(
            _rgb_pixel(
                pixels,
                (int(timeline["left"] + 300), int(timeline["top"] + 8)),
            ),
            (233, 239, 246),
        )
        first_row = rows[0]
        task_pixel = _rgb_pixel(
            pixels,
            (
                int(first_row["task"]["right"] - 6),
                int(first_row["row"]["top"] + 7),
            ),
        )
        assert min(task_pixel) >= 240
        plot_pixel = _rgb_pixel(
            pixels,
            (
                int(first_row["plot"]["left"] + 6),
                int(first_row["row"]["top"] + 7),
            ),
        )
        assert min(plot_pixel) >= 232


def test_mixed_root_timeline_wait_lane_dom_order_and_overflow(tmp_path: Path):
    event = _timeline_event(
        [
            _timeline_item(0, start=0, end=4000, depth=3, name="规划与实现"),
            _timeline_item(1, start=500, end=2500, status="failed", name="验证"),
            _timeline_item(2, start=2600, end=5000, name="修复"),
            _timeline_item(3, start=5200, end=6500, name="复核"),
        ],
        total_duration_ms=10_000,
        user_wait_timeline=_wait_timeline(
            [
                _wait_interval(kind="question", start=800, end=1800),
                _wait_interval(kind="permission", start=3000, end=4200),
                _wait_interval(
                    kind="question", state="right_censored", start=7200, end=None
                ),
                _wait_interval(
                    kind="permission", state="left_censored", start=None, end=300
                ),
            ],
            partial=True,
            partial_reasons=["open_at_cycle_end", "orphan_resolution"],
        ),
    )
    rendered = render_subagent_timeline(event)
    assert rendered is not None

    geometry, screenshot_path, _ = capture_timeline_browser_geometry(
        rendered.html,
        viewport_width=rendered.layout.viewport_width,
        viewport_height=rendered.layout.viewport_height,
        output_dir=tmp_path,
        name="timeline-mixed-wait",
    )
    document = geometry["document"]
    body = geometry["body"]
    timeline = geometry["timeline"]
    wait = geometry["wait"]
    rows = geometry["rows"]
    assert wait is not None
    assert document["scrollWidth"] == rendered.layout.viewport_width
    assert body["scrollWidth"] == rendered.layout.viewport_width
    assert document["scrollHeight"] <= rendered.layout.viewport_height
    assert body["scrollHeight"] <= rendered.layout.viewport_height
    assert wait["row"]["top"] > timeline["top"]
    assert wait["row"]["bottom"] <= rows[0]["row"]["top"] + 0.1
    assert wait["scrollWidth"] <= wait["clientWidth"]
    assert len(wait["bars"]) == 4

    classes = [bar["className"] for bar in wait["bars"]]
    assert any("question complete" in value for value in classes)
    assert any("permission complete" in value for value in classes)
    assert any("question right-censored" in value for value in classes)
    assert any("permission left-censored" in value for value in classes)
    assert [bar["countedDuration"] for bar in wait["bars"]].count("true") == 2
    assert [bar["countedDuration"] for bar in wait["bars"]].count("false") == 2
    assert any("结束边界未知" in bar["title"] for bar in wait["bars"])
    assert any("开始边界未知" in bar["title"] for bar in wait["bars"])
    assert all("right_censored" not in bar["title"] for bar in wait["bars"])
    assert all("left_censored" not in bar["title"] for bar in wait["bars"])
    for bar in wait["bars"]:
        assert bar["bar"]["left"] >= wait["plot"]["left"] - 0.1
        assert bar["bar"]["right"] <= wait["plot"]["right"] + 1.1
    question_complete = next(
        bar for bar in wait["bars"] if "question complete" in bar["className"]
    )
    permission_complete = next(
        bar for bar in wait["bars"] if "permission complete" in bar["className"]
    )
    assert question_complete["bar"]["top"] < permission_complete["bar"]["top"]

    with Image.open(screenshot_path) as screenshot:
        assert screenshot.width >= document["scrollWidth"]
        assert screenshot.height >= document["scrollHeight"]


def test_only_wait_real_chrome_geometry_at_minimum_viewport(tmp_path: Path):
    event = _timeline_event(
        [],
        total_duration_ms=5000,
        user_wait_timeline=_wait_timeline(
            [_wait_interval(kind="question", start=1000, end=2000)]
        ),
    )
    event.subagent_timeline = None
    rendered = render_subagent_timeline(event)
    assert rendered is not None
    assert rendered.layout.viewport_width == 1440

    geometry, screenshot_path, _ = capture_timeline_browser_geometry(
        rendered.html,
        viewport_width=rendered.layout.viewport_width,
        viewport_height=rendered.layout.viewport_height,
        output_dir=tmp_path,
        name="timeline-only-wait",
    )
    document = geometry["document"]
    body = geometry["body"]
    card = geometry["card"]
    overview = geometry["overview"]
    metrics = geometry["metrics"]
    timeline = geometry["timeline"]
    wait = geometry["wait"]
    assert document["scrollWidth"] == 1440
    assert body["scrollWidth"] == 1440
    assert document["scrollHeight"] <= rendered.layout.viewport_height
    assert body["scrollHeight"] <= rendered.layout.viewport_height
    assert timeline["left"] >= card["left"] + 31
    assert timeline["right"] <= card["right"] - 31
    assert timeline["width"] == pytest.approx(
        rendered.layout.name_column_width
        + rendered.layout.plot_width
        + rendered.layout.state_column_width
        + 2
    )
    assert geometry["rows"] == []

    assert [metric["label"] for metric in metrics] == [
        "子任务数",
        "峰值并发",
        "总任务时长",
        "子任务覆盖时长",
        "子任务覆盖率",
        "未分类时间 / 占比 · 观测受限",
    ]
    assert [metric["value"] for metric in metrics] == [
        "0",
        "—",
        "5 秒",
        "0 毫秒",
        "0%",
        "至多 4 秒（80%）",
    ]
    for metric in metrics:
        assert metric["metric"]["left"] >= overview["left"] - 0.1
        assert metric["metric"]["right"] <= overview["right"] + 0.1
        assert metric["scrollWidth"] <= metric["clientWidth"]
        assert metric["labelScrollWidth"] <= metric["labelClientWidth"]

    assert wait is not None
    assert wait["row"]["top"] > timeline["top"]
    assert wait["row"]["bottom"] <= timeline["bottom"] + 0.1
    assert wait["scrollWidth"] <= wait["clientWidth"]
    assert len(wait["bars"]) == 1
    bar = wait["bars"][0]["bar"]
    expected_left = wait["plot"]["left"] + wait["plot"]["width"] * 0.2
    expected_width = wait["plot"]["width"] * 0.2
    assert bar["left"] == pytest.approx(expected_left, abs=1.1)
    assert bar["width"] == pytest.approx(expected_width, abs=1.1)
    assert bar["right"] <= wait["plot"]["right"] + 1.1

    with Image.open(screenshot_path) as screenshot:
        assert screenshot.width >= document["scrollWidth"]
        assert screenshot.height >= document["scrollHeight"]


def test_long_timeline_identity_wraps_without_clipping_at_minimum_width(
    tmp_path: Path,
):
    agent = "agent-" + "a" * 122
    model = "provider/" + "model-" + "m" * 113
    variant = "variant-" + "v" * 120
    identity = f"{agent} · {model}({variant})"
    event = _timeline_event(
        [
            _timeline_item(
                index,
                start=0,
                end=2000,
                name=f"task-{index}",
                agent=agent,
                model=model,
                model_variant=variant,
            )
            for index in range(4)
        ]
    )
    rendered = render_subagent_timeline(event)
    assert rendered is not None
    assert rendered.layout.viewport_width == 1440

    geometry, _, _ = capture_timeline_browser_geometry(
        rendered.html,
        viewport_width=rendered.layout.viewport_width,
        viewport_height=rendered.layout.viewport_height,
        output_dir=tmp_path,
        name="timeline-long-identity",
    )

    assert geometry["document"]["scrollWidth"] == 1440
    assert geometry["document"]["scrollHeight"] <= rendered.layout.viewport_height
    assert len(geometry["metrics"]) == 6
    assert geometry["overview"]["right"] <= geometry["card"]["right"] - 31
    for metric in geometry["metrics"]:
        assert metric["metric"]["right"] <= geometry["overview"]["right"] + 0.1
        assert metric["scrollWidth"] <= metric["clientWidth"]
        assert metric["labelScrollWidth"] <= metric["labelClientWidth"]
    for row in geometry["rows"]:
        metrics = row["agentMetrics"]
        assert metrics is not None
        assert metrics["text"] == identity
        assert metrics["whiteSpace"] == "normal"
        assert metrics["overflowWrap"] == "anywhere"
        assert metrics["wordBreak"] == "normal"
        assert metrics["scrollWidth"] <= metrics["clientWidth"]
        assert metrics["scrollHeight"] == metrics["clientHeight"]
        assert metrics["clientHeight"] > 40


def _folded_omp_browser_scenario():
    total_ms = (18 * 60 + 13) * 60_000
    h = 3_600_000
    m = 60_000
    return _omp_run_event(
        duration_ms=total_ms,
        session_name="长 Ask 等待时间线断轴折叠验收",
        items=[
            _omp_subagent(1, 4 * m, 52 * m, name="扫描仓库结构与时间线渲染入口"),
            _omp_subagent(2, 18 * m, 96 * m, name="梳理 OMP 交互与子代理区间边界"),
            _omp_subagent(3, 55 * m, 115 * m, name="推导分段线性比例尺与刻度约束"),
            _omp_subagent(4, 5 * h + 10 * m, 6 * h + 35 * m, name="实现断轴折叠映射与模板样式"),
            _omp_subagent(5, 6 * h + 5 * m, 7 * h + 45 * m, status="failed", name="验证深浅主题与刻度防重叠"),
            _omp_subagent(6, 15 * h + 12 * m, 17 * h + 5 * m, name="补充服务端回归测试与视觉验收"),
            _omp_subagent(7, 16 * h + 30 * m, 18 * h + 2 * m, name="整理变更说明与交付证据"),
        ],
        waits=[
            _omp_wait(1, "ask", 1 * h + 30 * m, 4 * h + 55 * m),
            _omp_wait(2, "approval", 5 * h + 25 * m, 5 * h + 55 * m),
            _omp_wait(3, "ask", 8 * h, 15 * h),
        ],
    )

@pytest.mark.parametrize("theme", ["light", "dark"])
def test_omp_folded_timeline_browser_geometry_and_themes(
    tmp_path: Path, theme: str
):
    event = _folded_omp_browser_scenario()
    rendered = render_subagent_timeline(event, theme=theme)
    unfolded_view = _build_subagent_timeline_view(event, allow_fold=False)
    folded_view = _build_subagent_timeline_view(event, allow_fold=True)
    assert rendered is not None and folded_view is not None and unfolded_view is not None
    assert folded_view["axis_folded"] is True
    assert folded_view["linear_px_per_ms"] > unfolded_view["linear_px_per_ms"] * 1.95

    geometry, screenshot_path, _ = capture_timeline_browser_geometry(
        rendered.html,
        viewport_width=rendered.layout.viewport_width,
        viewport_height=rendered.layout.viewport_height,
        output_dir=tmp_path,
        name=f"omp-folded-{theme}",
    )
    document = geometry["document"]
    assert document["scrollWidth"] == rendered.layout.viewport_width
    assert document["scrollHeight"] <= rendered.layout.viewport_height

    banner = geometry["foldBanner"]
    assert banner is not None
    assert "已折叠 2 段交互等待，共 10 小时" in banner["text"]
    assert banner["scrollWidth"] <= banner["clientWidth"]

    axis_folds = geometry["axisFolds"]
    assert len(axis_folds) == 2
    assert "3小时" in axis_folds[0]["text"] and "折叠" in axis_folds[0]["text"]
    assert "7小时" in axis_folds[1]["text"] and "折叠" in axis_folds[1]["text"]
    for fold in axis_folds:
        assert fold["scrollWidth"] <= fold["clientWidth"] + 1
        assert fold["scrollHeight"] <= fold["clientHeight"] + 1

    ticks = geometry["ticks"]
    assert len(ticks) >= 4
    for tick in ticks:
        t_rect = tick["rect"]
        for fold in axis_folds:
            f_rect = fold["rect"]
            assert t_rect["right"] <= f_rect["left"] + 0.5 or t_rect["left"] >= f_rect["right"] - 0.5
    for left_tick, right_tick in zip(ticks, ticks[1:]):
        assert right_tick["rect"]["left"] >= left_tick["rect"]["right"] + 4.0

    with Image.open(screenshot_path) as screenshot:
        assert screenshot.width >= document["scrollWidth"]
        assert screenshot.height >= document["scrollHeight"]
        pixels = screenshot.convert("RGB")
        bg = _rgb_pixel(pixels, (2, 2))
        if theme == "dark":
            assert max(bg) <= 36
        else:
            assert min(bg) >= 230


def test_omp_short_wait_smoke_timeline_stays_unfolded(tmp_path: Path):
    items = []
    for index, (begin, finish) in enumerate(((2000, 14_000), (6000, 20_000))):
        ref = f"{index + 100:032x}"
        items.append(
            {
                "ref": ref,
                "cycleRef": ref,
                "parentRef": "3" * 32,
                "depth": 1,
                "attempt": 1,
                "status": "completed",
                "name": f"冒烟子代理 {index + 1}",
                "timingQuality": "observed",
                "startOffsetMs": begin,
                "endOffsetMs": finish,
                "durationMs": finish - begin,
            }
        )
    event = _omp_run_event(
        duration_ms=30_000,
        session_name="[视觉基准] 多代理时间线",
        items=items,
        waits=[_omp_wait(1, "ask", 21_000, 25_000)],
    )
    for theme in ("light", "dark"):
        folded_enabled = render_subagent_timeline(event, theme=theme)
        unfolded_view = _build_subagent_timeline_view(event, allow_fold=False)
        folded_view = _build_subagent_timeline_view(event, allow_fold=True)
        assert folded_enabled is not None and folded_view == unfolded_view
        assert folded_view["axis_folded"] is False
        assert 'class="fold-banner"' not in folded_enabled.html
        assert 'class="axis-fold"' not in folded_enabled.html
        assert 'class="track-fold"' not in folded_enabled.html
        geometry, _, _ = capture_timeline_browser_geometry(
            folded_enabled.html,
            viewport_width=folded_enabled.layout.viewport_width,
            viewport_height=folded_enabled.layout.viewport_height,
            output_dir=tmp_path,
            name=f"omp-unfolded-{theme}",
        )
        assert geometry["foldBanner"] is None
        assert geometry["axisFolds"] == []
