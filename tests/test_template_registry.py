"""TemplateRegistry persistence and safety tests."""

from __future__ import annotations

import json
import os

import pytest

from core.template_registry import (
    BUILT_IN_ID,
    TemplateConflictError,
    TemplateRegistry,
)


TEMPLATE = "<html><head></head><body>{{ event.title }}</body></html>"


def test_default_built_in(tmp_path):
    registry = TemplateRegistry(tmp_path)
    assert registry.snapshot.active == BUILT_IN_ID
    assert registry.get_active().id == BUILT_IN_ID
    assert registry.list_templates()[0] == {
        "id": BUILT_IN_ID,
        "display_name": "Built-in",
        "canvas_width": 812,
        "revision": 0,
        "updated_at": "",
        "built_in": True,
        "active": True,
        "valid": True,
        "read_only": True,
    }
    assert registry.export_template(BUILT_IN_ID) == {
        "id": BUILT_IN_ID,
        "display_name": "Built-in",
        "content": registry.get_active().content,
        "canvas_width": 812,
        "revision": 0,
        "updated_at": "",
        "valid": True,
        "built_in": True,
        "active": True,
    }


def test_create_save_apply_restart_and_active_save(tmp_path):
    registry = TemplateRegistry(tmp_path)
    created = registry.save(None, "Custom", TEMPLATE, 900, apply=True)
    assert created.id.startswith("custom-")
    assert registry.get_active().canvas_width == 900
    listed = registry.list_templates()
    assert listed[0]["active"] is False
    assert listed[1]["built_in"] is False
    assert listed[1]["active"] is True
    assert listed[1]["valid"] is True
    exported = registry.export_template(created.id)
    assert exported is not None
    assert exported["built_in"] is False
    assert exported["active"] is True
    assert exported["valid"] is True

    saved = registry.save(
        created.id,
        "Custom 2",
        TEMPLATE.replace("title", "summary"),
        960,
        expected_revision=created.revision,
    )
    assert registry.snapshot.active == created.id
    assert registry.get_active().revision == saved.revision
    assert registry.get_active().canvas_width == 960

    restarted = TemplateRegistry(tmp_path)
    assert restarted.get_active().content == saved.content
    assert restarted.get_active().canvas_width == 960


def test_revision_conflict(tmp_path):
    registry = TemplateRegistry(tmp_path)
    created = registry.save(None, "Custom", TEMPLATE, 812)
    with pytest.raises(TemplateConflictError):
        registry.save(created.id, "Custom", TEMPLATE, 812, expected_revision=0)


def test_registry_replace_failure_preserves_snapshot(tmp_path, monkeypatch):
    registry = TemplateRegistry(tmp_path)
    created = registry.save(None, "Custom", TEMPLATE, 812)
    before = registry.snapshot
    real_replace = os.replace

    def fail_registry(source, target):
        if str(target).endswith("templates.json"):
            raise OSError("simulated replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_registry)
    with pytest.raises(OSError):
        registry.save(
            created.id, "Changed", TEMPLATE, 900, expected_revision=created.revision
        )
    assert registry.snapshot is before
    current = registry.get(created.id)
    assert current is not None
    assert current.revision == created.revision


def test_corrupt_unknown_version_and_missing_file(tmp_path):
    (tmp_path / "templates.json").write_text("not json", encoding="utf-8")
    recovered = TemplateRegistry(tmp_path)
    assert recovered.get_active().id == BUILT_IN_ID
    assert list(tmp_path.glob("templates.json.corrupt-*"))

    (tmp_path / "templates.json").write_text(
        json.dumps({"version": 99, "active": "built-in", "templates": {}}),
        encoding="utf-8",
    )
    unknown = TemplateRegistry(tmp_path)
    assert unknown.read_only is True

    data = {
        "version": 1,
        "active": "custom-missing",
        "templates": {
            "custom-missing": {
                "display_name": "Missing",
                "file": "custom-missing-1.html",
                "canvas_width": 812,
                "revision": 1,
                "updated_at": "now",
            }
        },
    }
    (tmp_path / "templates.json").write_text(json.dumps(data), encoding="utf-8")
    missing = TemplateRegistry(tmp_path)
    assert missing.snapshot.active == "custom-missing"
    assert missing.snapshot.effective_active == BUILT_IN_ID
    assert missing.list_templates()[1]["valid"] is False


def test_symlink_revision_is_invalid(tmp_path):
    registry = TemplateRegistry(tmp_path)
    created = registry.save(None, "Custom", TEMPLATE, 812, apply=True)
    file_path = tmp_path / "templates" / f"{created.id}-{created.revision}.html"
    target = tmp_path / "outside.html"
    target.write_text(TEMPLATE, encoding="utf-8")
    file_path.unlink()
    try:
        file_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink unavailable")
    restarted = TemplateRegistry(tmp_path)
    assert restarted.snapshot.effective_active == BUILT_IN_ID
