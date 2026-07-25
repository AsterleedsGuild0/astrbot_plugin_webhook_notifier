"""Real offline Chrome geometry and screenshot smoke for the timeline canvas."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from core.renderer import render_subagent_timeline
from tests.browser_timeline_smoke import capture_timeline_browser_geometry
from tests.test_renderer import _timeline_event, _timeline_item


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
    timeline = geometry["timeline"]
    rows = geometry["rows"]
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

    assert len(rows) == item_count
    assert [row["text"] for row in rows] == expected_names
    assert rows[-1]["row"]["bottom"] <= document["scrollHeight"]
    assert any(
        row["name"]["height"] > rendered.layout.task_font_size * 2 for row in rows
    )
    for row in rows:
        assert row["row"]["right"] <= timeline["right"] - 0.9
        assert row["task"]["width"] == pytest.approx(rendered.layout.name_column_width)
        assert row["plot"]["width"] == pytest.approx(rendered.layout.plot_width)
        assert row["state"]["width"] == pytest.approx(
            rendered.layout.state_column_width
        )
        assert row["state"]["right"] <= timeline["right"] - 0.9
        metrics = row["nameMetrics"]
        assert metrics["whiteSpace"] == "normal"
        assert metrics["overflowWrap"] == "anywhere"
        assert metrics["wordBreak"] == "normal"
        assert metrics["scrollWidth"] <= metrics["clientWidth"]
        assert metrics["scrollHeight"] == metrics["clientHeight"]

    with Image.open(screenshot_path) as screenshot:
        assert screenshot.width >= document["scrollWidth"]
        assert screenshot.height >= document["scrollHeight"]
