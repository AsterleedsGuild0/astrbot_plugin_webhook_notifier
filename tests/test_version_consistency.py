from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import yaml
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TAG = "v1.3.0"

# ── setup-uv 固定 SHA ────────────────────────────────────────────────────
# 来自 astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9  # v9.0.0
SETUP_UV_PINNED_SHA = "c771a70e6277c0a99b617c7a806ffedaca235ff9"
SETUP_UV_PINNED_VERSION = "v9.0.0"


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


def workflow_text() -> str:
    return (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


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
    assert package_plugin.release_flags("v1.1.0") == (False, True)
    assert package_plugin.release_flags("v1.2.0") == (False, True)
    assert package_plugin.release_flags("v1.3.0") == (False, True)
    assert package_plugin.release_flags("v1.0.0") == (False, True)


def test_release_workflow_uses_dynamic_release_flags() -> None:
    workflow = workflow_text()

    assert "from packaging.version import Version" in workflow
    assert "prerelease: ${{ steps.release_contract.outputs.prerelease }}" in workflow
    assert "make_latest: ${{ steps.release_contract.outputs.make_latest }}" in workflow
    assert "make_latest: true" not in workflow


def test_changelog_contains_stable_release_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.0.0 - 2026-07-21" in changelog
    assert "## v1.1.0 - 2026-07-30" in changelog
    assert "## v1.2.0 - 2026-08-10" in changelog
    assert "## v1.3.0 - 2026-08-19" in changelog


def test_changelog_contains_current_rc_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.1.0-rc.1 - 2026-07-29" in changelog
    assert "AstrBot WebUI" in changelog
    assert "Desktop" in changelog


def _extract_release_notes(changelog: str, tag: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(tag)}(?:\s+-\s+[^\n]+)?\n"
        r"(?P<body>.*?)(?=\n---\n\n##\s+|\n##\s+v|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog)
    assert match, f"Cannot find {tag} section in CHANGELOG.md"
    return match.group("body")


def test_release_notes_extract_only_stable_release_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    notes = _extract_release_notes(changelog, "v1.0.0")
    assert "首个稳定版公共契约" in notes
    assert "市场安装与更新路径" in notes
    assert "完成 Registry v2" not in notes


def test_v110_release_notes_extract_without_rc_content() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    notes = _extract_release_notes(changelog, "v1.1.0")
    assert "定稿为 v1.1.0" in notes
    assert "prerelease=false" in notes
    assert "make_latest=true" in notes
    assert "AstrBot v4.26.7" in notes
    assert "1018 Python tests" in notes
    # Must not contain RC chapter content or RC-only phrases
    assert "AstrBot WebUI" not in notes
    assert "Desktop" not in notes


def test_v120_release_notes_preserve_historical_scope() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    notes = _extract_release_notes(changelog, "v1.2.0")
    assert "metadataDiagnostics=anomaly" in notes
    assert "#25" in notes
    assert "#26" in notes
    assert "userWaitTimeline" in notes
    assert "服务端先升级并重载" in notes
    assert "v1.1.0` 已验证范围定稿" not in notes


def test_v130_release_notes_cover_current_scope() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = _extract_release_notes(changelog, "v1.3.0")

    assert "markdown.message" in notes
    assert "session.rootName" in notes
    assert "所属主会话" in notes
    assert "Bun 279" in notes
    assert "HTTP 200" in notes
    assert "完整 Release 门禁" in notes


def test_current_release_docs_target_v130_without_claiming_remote_assets() -> None:
    release = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")

    assert "本轮正式发布目标为 `v1.3.0`" in release
    assert "本地准备步骤不创建 tag、GitHub Release 或远端资产" in release
    assert "`v1.3.0` 的 tag、Release、正式 ZIP" in release


# ══════════════════════════════════════════════════════════════════════════
# #11 CI 契约测试
# ══════════════════════════════════════════════════════════════════════════


def test_setup_uv_pinned_sha() -> None:
    """断言 workflow 使用 astral-sh/setup-uv 且固定 SHA。"""
    workflow = workflow_text()
    assert "astral-sh/setup-uv@" in workflow
    assert SETUP_UV_PINNED_SHA in workflow
    assert SETUP_UV_PINNED_VERSION in workflow


def test_setup_uv_python_313() -> None:
    """断言 setup-uv 显式配置 Python 3.13。"""
    workflow = workflow_text()
    assert 'python-version: "3.13"' in workflow


def test_setup_uv_enable_cache() -> None:
    """断言 setup-uv 启用 uv cache。"""
    workflow = workflow_text()
    assert "enable-cache: true" in workflow


def test_setup_uv_cache_dependency_glob() -> None:
    """断言 setup-uv cache-dependency-glob 至少包含 pyproject.toml 和 uv.lock。"""
    workflow = workflow_text()
    assert "pyproject.toml" in workflow
    assert "uv.lock" in workflow


def test_uv_lock_check_present() -> None:
    """断言 workflow 包含 uv lock --check。"""
    workflow = workflow_text()
    assert "uv lock --check" in workflow


def test_uv_sync_frozen_group_dev() -> None:
    """断言 workflow 使用 uv sync --frozen --group dev。"""
    workflow = workflow_text()
    assert "uv sync --frozen --group dev" in workflow


def test_ruff_check_present() -> None:
    """断言 workflow 包含 ruff check（lint 门禁）。"""
    workflow = workflow_text()
    assert "ruff check" in workflow


def test_no_pip_install_in_workflow() -> None:
    """断言 workflow 不再使用 pip install（已迁移到 uv）。"""
    workflow = workflow_text()
    assert "pip install" not in workflow
    assert "setup-python" not in workflow


def test_workflow_dispatch_is_dry_run() -> None:
    """断言 workflow_dispatch 不会调用 action-gh-release（dry-run 条件）。"""
    workflow = workflow_text()
    # 更直接的断言：Publish step 有 if 条件
    publish_step = workflow[workflow.index("Publish GitHub Release") :]
    assert "if:" in publish_step[:200]
    assert "github.event_name == 'push'" in publish_step
    assert "startsWith(github.ref, 'refs/tags/v')" in publish_step


def test_tag_push_release_condition() -> None:
    """断言正式 Release 只在 push v* tag 时执行。"""
    workflow = workflow_text()
    publish_step = workflow[workflow.index("Publish GitHub Release") :]
    assert "if:" in publish_step[:200]
    assert "github.event_name == 'push'" in publish_step
    assert "startsWith(github.ref, 'refs/tags/v')" in publish_step


def test_artifact_upload_present() -> None:
    """断言 artifact 上传仍在 release job 中。"""
    workflow = workflow_text()
    assert "actions/upload-artifact@v4" in workflow
    assert "dist/*.zip" in workflow


def test_ruff_format_check_not_enabled() -> None:
    """断言 workflow 不启用 ruff format --check（#11 只引入 lint 门禁）。"""
    workflow = workflow_text()
    assert "ruff format" not in workflow


def test_setup_uv_version_pinned() -> None:
    """断言 setup-uv 固定了 uv 版本。"""
    workflow = workflow_text()
    assert 'version: "0.11.12"' in workflow


def test_workflow_dispatch_tag_input_used() -> None:
    """断言 workflow_dispatch 声明了 tag 输入。"""
    workflow = workflow_text()
    assert "inputs:" in workflow
    assert "tag:" in workflow


def test_workflow_dispatch_checkout_uses_trigger_ref() -> None:
    """断言 dry-run checkout 当前触发 ref，而不是尚未创建的候选 tag。"""
    workflow = workflow_text()
    checkout_step = workflow[workflow.index("- name: Checkout") :]
    checkout_step = checkout_step[: checkout_step.index("- name: Set up uv")]
    assert "ref: ${{ github.ref }}" in checkout_step
    assert "inputs.tag" not in checkout_step
