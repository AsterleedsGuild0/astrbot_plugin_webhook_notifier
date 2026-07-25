"""Real offline Chrome geometry and screenshot smoke for the timeline canvas."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from core.renderer import render_subagent_timeline
from tests.browser_timeline_smoke import capture_timeline_browser_geometry
from tests.test_renderer import _timeline_event, _timeline_item

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
    timeline = geometry["timeline"]
    styles = geometry["styles"]
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
