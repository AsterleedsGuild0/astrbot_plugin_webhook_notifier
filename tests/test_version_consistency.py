from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import yaml
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TAG = "v1.1.0-rc.1"


def load_package_module():
    spec = importlib.util.spec_from_file_location(
        "package_plugin_version_contract", ROOT / "scripts" / "package_plugin.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_main_register_version() -> str:
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    match = re.search(
        r'@register\(\s*"astrbot_plugin_webhook_notifier",\s*'
        r'"AsterleedsGuild0",\s*.*?,\s*"([^"]+)"\s*,?\s*\)',
        text,
        re.DOTALL,
    )
    assert match
    return match.group(1)


def test_version_sources_are_pep440_equivalent() -> None:
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    metadata_version = metadata["version"]
    main_version = read_main_register_version()
    project_version = project["project"]["version"]

    assert metadata_version == EXPECTED_TAG
    assert main_version == EXPECTED_TAG
    assert Version(metadata_version.removeprefix("v")) == Version(project_version)


def test_release_flags_distinguish_rc_and_stable_versions() -> None:
    package_plugin = load_package_module()

    assert package_plugin.release_flags("v1.0.0-rc.1") == (True, False)
    assert package_plugin.release_flags("v1.1.0-rc.1") == (True, False)
    assert package_plugin.release_flags("v1.0.0") == (False, True)


def test_release_workflow_uses_dynamic_release_flags() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "from packaging.version import Version" in workflow
    assert "prerelease: ${{ steps.release_contract.outputs.prerelease }}" in workflow
    assert "make_latest: ${{ steps.release_contract.outputs.make_latest }}" in workflow
    assert "make_latest: true" not in workflow


def test_changelog_contains_stable_release_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.0.0 - 2026-07-21" in changelog


def test_changelog_contains_current_rc_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.1.0-rc.1 - 2026-07-23" in changelog
    assert "AstrBot WebUI" in changelog
    assert "Desktop" in changelog


def test_release_notes_extract_only_stable_release_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^##\s+{re.escape('v1.0.0')}(?:\s+-\s+[^\n]+)?\n"
        r"(?P<body>.*?)(?=\n---\n\n##\s+|\n##\s+v|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    match = pattern.search(changelog)
    assert match
    notes = match.group("body")
    assert "首个稳定版公共契约" in notes
    assert "市场安装与更新路径" in notes
    assert "完成 Registry v2" not in notes
