"""OpenCode CLI smoke 夹具的轻量契约测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_opencode_plugin.py"
SPEC = importlib.util.spec_from_file_location("smoke_opencode_plugin", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_config_uses_v1_plugin_tuple_and_safe_interpolation(tmp_path):
    wrapper_uri = "file:///tmp/smoke-wrapper.ts"
    content = smoke.build_config_content(wrapper_uri, tmp_path / "token")
    config = json.loads(content)

    plugin = config["plugin"]
    assert isinstance(plugin, list) and len(plugin) == 1
    assert plugin[0][0] == wrapper_uri
    options = plugin[0][1]
    assert options["url"] == "{env:OPENCODE_SMOKE_WEBHOOK_URL}"
    assert options["token"].startswith("{file:")
    assert options["token"].endswith("}")


def test_wrapper_shape_delegates_server_and_marks_invocation():
    wrapper = smoke.build_wrapper("file:///tmp/real-webhook-notifier.ts")

    assert "real.server(input, options)" in wrapper
    assert "appendFileSync(marker" in wrapper
    assert "export default { id: real.id, server };" in wrapper


def test_opencode_docs_cover_required_smoke_and_contract_sections():
    document = (ROOT / "docs" / "opencode-integration.md").read_text(encoding="utf-8")
    required_terms = (
        "v1.17.9",
        "1.18.4",
        "--provider opencode",
        "plugin tuple",
        "session_idle",
        "session_error",
        "permission_asked",
        "匿名",
        "白名单",
        "at-least-once",
        "--cli",
        "--desktop",
    )
    for term in required_terms:
        assert term in document
