from __future__ import annotations

import importlib.util
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SUPPORT_PLATFORMS = ["aiocqhttp", "qq_official"]


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


def test_help_card_template_is_in_package_file_list() -> None:
    package_plugin = load_package_module()

    assert "templates/help_card.html" in package_plugin.iter_package_files()


def test_rebind_cli_is_explicitly_in_package_file_list() -> None:
    package_plugin = load_package_module()

    package_files = package_plugin.iter_package_files()
    assert "scripts/rebind_platform_id.py" in package_files
    assert not any(
        path.startswith("scripts/") and path != "scripts/rebind_platform_id.py"
        for path in package_files
    )


def test_metadata_declares_exact_verified_platforms() -> None:
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))

    assert isinstance(metadata["support_platforms"], list)
    # These are AstrBot ADAPTER_NAME_2_TYPE keys; WebSocket is not a separate key.
    assert metadata["support_platforms"] == EXPECTED_SUPPORT_PLATFORMS


def test_dev_version_metadata_patch_preserves_support_platforms(
    tmp_path: Path,
) -> None:
    package_plugin = load_package_module()
    output = tmp_path / "plugin-dev.zip"

    package_plugin.build_archive(
        output,
        flat=False,
        package_version="v0.2.0-test.20260718.1200",
    )

    with zipfile.ZipFile(output) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith("/metadata.yaml")
        )
        metadata = yaml.safe_load(archive.read(metadata_name))

    assert metadata["version"] == "v0.2.0-test.20260718.1200"
    assert metadata["support_platforms"] == EXPECTED_SUPPORT_PLATFORMS


def test_package_metadata_preserves_support_platforms(tmp_path: Path) -> None:
    package_plugin = load_package_module()
    output = tmp_path / "plugin.zip"

    package_plugin.build_archive(output, flat=False)

    with zipfile.ZipFile(output) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith("/metadata.yaml")
        )
        metadata = yaml.safe_load(archive.read(metadata_name))

    assert metadata["support_platforms"] == EXPECTED_SUPPORT_PLATFORMS
