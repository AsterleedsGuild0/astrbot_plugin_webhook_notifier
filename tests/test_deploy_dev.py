from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "deploy_dev", Path(__file__).resolve().parents[1] / "scripts" / "deploy_dev.py"
)
assert SPEC and SPEC.loader
deploy_dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_dev)


@pytest.mark.parametrize("no_rollback", [False, True])
def test_failed_install_respects_no_rollback(tmp_path, monkeypatch, no_rollback):
    plugin_id = "astrbot_plugin_webhook_notifier"
    candidate = tmp_path / "candidate.zip"
    previous = tmp_path / "previous.zip"
    for path, version in ((candidate, "v2.4.1"), (previous, "v2.4.0")):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{plugin_id}/", b"")
            archive.writestr(
                f"{plugin_id}/metadata.yaml", f"name: {plugin_id}\nversion: {version}\n"
            )
    state_file = tmp_path / "last-deployed.json"
    state_file.write_text(json.dumps({"zip": str(previous), "version": "v2.4.0"}))
    monkeypatch.setattr(deploy_dev, "STATE_FILE", state_file)
    monkeypatch.setenv("ASTRBOT_DASHBOARD_URL", "http://test.invalid")
    monkeypatch.setenv("ASTRBOT_API_KEY", "test-only")

    class Dashboard:
        current = {"name": plugin_id, "version": "v2.4.0"}

        def plugin(self, _plugin_id):
            return self.current

        def failed(self):
            return {}

        def uninstall(self, _plugin_id):
            self.current = None

        def upload(self, path):
            if path == candidate:
                raise deploy_dev.DeployError("install rejected")
            _, version = deploy_dev.zip_identity(path)
            self.current = {"name": plugin_id, "version": version}

    dashboard = Dashboard()
    monkeypatch.setattr(deploy_dev, "Dashboard", lambda *_args: dashboard)
    args = ["--zip", str(candidate), "--skip-smoke"]
    if no_rollback:
        args.append("--no-rollback")
    assert deploy_dev.main(args) == 1
    assert dashboard.current == (None if no_rollback else {"name": plugin_id, "version": "v2.4.0"})
    assert json.loads(state_file.read_text())["version"] == "v2.4.0"
