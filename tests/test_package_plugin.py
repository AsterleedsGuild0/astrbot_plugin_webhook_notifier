from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_package_module():
    spec = importlib.util.spec_from_file_location(
        "package_plugin", ROOT / "scripts" / "package_plugin.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dev_version_includes_local_hour_and_minute(monkeypatch) -> None:
    package_plugin = load_package_module()

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 15, 9, 5, tzinfo=tz)

    monkeypatch.setattr(package_plugin, "datetime", FixedDateTime)

    assert package_plugin.build_dev_version("v0.2.0") == "v0.2.0-test.20260715.0905"


def test_dev_version_can_include_recognizable_label(monkeypatch) -> None:
    package_plugin = load_package_module()

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 15, 9, 5, tzinfo=tz)

    monkeypatch.setattr(package_plugin, "datetime", FixedDateTime)

    assert (
        package_plugin.build_dev_version("v0.2.0", "template-manager")
        == "v0.2.0-test.20260715.0905.template-manager"
    )


def test_dev_version_rejects_invalid_label() -> None:
    package_plugin = load_package_module()

    with pytest.raises(ValueError, match="test-label"):
        package_plugin.build_dev_version("v0.2.0", "模板 管理")
