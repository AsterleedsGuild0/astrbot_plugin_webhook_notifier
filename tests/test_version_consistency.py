from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import yaml
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]


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

    assert main_version == metadata_version
    assert Version(metadata_version.removeprefix("v")) == Version(project_version)


def test_release_flags_distinguish_rc_and_stable_versions() -> None:
    package_plugin = load_package_module()

    assert package_plugin.release_flags("v1.0.0-rc.1") == (True, False)
    assert package_plugin.release_flags("v1.1.0-rc.1") == (True, False)
    assert package_plugin.release_flags("v1.1.0") == (False, True)
    assert package_plugin.release_flags("v1.2.0") == (False, True)
    assert package_plugin.release_flags("v1.3.0") == (False, True)
    assert package_plugin.release_flags("v1.0.0") == (False, True)
