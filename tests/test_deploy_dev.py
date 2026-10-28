from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image


SPEC = importlib.util.spec_from_file_location(
    "deploy_dev", Path(__file__).resolve().parents[1] / "scripts" / "deploy_dev.py"
)
assert SPEC and SPEC.loader
deploy_dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_dev)


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    # Record even absent keys so values loaded directly from an env-file are undone.
    for name in (
        "ASTRBOT_PLUGIN_DEPLOYER", "ASTRBOT_DEPLOY_ENVIRONMENT",
        "ASTRBOT_DASHBOARD_URL", "ASTRBOT_API_KEY",
        "WEBHOOK_NOTIFIER_URL", "WEBHOOK_NOTIFIER_TOKEN",
        "FAKE_DEPLOY_OUTPUT", "FAKE_DEPLOY_EXIT", "FAKE_DEPLOY_LOG",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def endpoint(monkeypatch):
    received = []
    reply = {"code": 0, "data": {"delivered": True, "render_mode": "html_image"}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append({
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                "authorization": self.headers.get("Authorization"),
            })
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/omp"
    monkeypatch.setenv("WEBHOOK_NOTIFIER_URL", url)
    monkeypatch.setenv("WEBHOOK_NOTIFIER_TOKEN", "smoke-test-token")
    try:
        yield url, received, reply
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def fake_deployer(tmp_path, monkeypatch):
    path = tmp_path / "external deployer.py"
    log = tmp_path / "calls.jsonl"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, os, sys\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('action', choices=('query', 'deploy', 'rollback'))\n"
        "parser.add_argument('--plugin', required=True)\n"
        "parser.add_argument('--environment', required=True)\n"
        "parser.add_argument('--zip', required=True)\n"
        "parser.add_argument('--env-file')\n"
        "parser.add_argument('--no-rollback', action='store_true')\n"
        "args = parser.parse_args()\n"
        "assert Path(args.zip).read_bytes() == b'test artifact owned by the external deployer'\n"
        "record = vars(args) | {'key': os.environ.get('ASTRBOT_API_KEY')}\n"
        "with open(os.environ['FAKE_DEPLOY_LOG'], 'a') as handle:\n"
        "    handle.write(json.dumps(record) + '\\n')\n"
        "print(os.environ['FAKE_DEPLOY_OUTPUT'])\n"
        "print('sensitive-child-diagnostic', file=sys.stderr)\n"
        "raise SystemExit(int(os.environ.get('FAKE_DEPLOY_EXIT', '0')))\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    package = tmp_path / "candidate.zip"
    package.write_bytes(b"test artifact owned by the external deployer")
    monkeypatch.setenv("FAKE_DEPLOY_LOG", str(log))
    monkeypatch.setenv("FAKE_DEPLOY_OUTPUT", json.dumps({
        "plugin_id": deploy_dev.PLUGIN_ID, "version": "v9.8.7-test.external",
        "activated": True, "previous": "v9.8.6",
    }))
    return path, package, log


def deployment_args(fake_deployer):
    path, package, _ = fake_deployer
    return ["--deployer", str(path), "--environment", "test", "--zip", str(package)]


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("configured", [False, True])
def test_unavailable_deployer_fails_without_sending(tmp_path, endpoint, capsys, configured):
    args = ["--environment", "test", "--zip", str(tmp_path / "candidate.zip")]
    if configured:
        args.extend(("--deployer", str(tmp_path / "missing.py")))
    assert deploy_dev.main(args) == 1
    assert endpoint[1] == []
    output = capsys.readouterr()
    assert "外部部署器" in output.err
    assert "Traceback" not in output.err


def test_environment_must_be_explicit(fake_deployer, endpoint, capsys):
    path, package, log = fake_deployer
    assert deploy_dev.main(["--deployer", str(path), "--zip", str(package)]) == 1
    assert not log.exists()
    assert endpoint[1] == []
    assert "显式指定部署环境" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
def test_child_failure_never_sends_or_echoes_output(fake_deployer, endpoint, monkeypatch, capsys, dry_run):
    monkeypatch.setenv("FAKE_DEPLOY_OUTPUT", "sensitive-child-response")
    monkeypatch.setenv("FAKE_DEPLOY_EXIT", "7")
    args = deployment_args(fake_deployer) + (["--dry-run"] if dry_run else [])
    assert deploy_dev.main(args) == 1
    assert endpoint[1] == []
    assert [call["action"] for call in calls(fake_deployer[2])] == ["query" if dry_run else "deploy"]
    output = capsys.readouterr()
    assert "exit 7" in output.err
    assert "sensitive-child" not in output.out + output.err


@pytest.mark.parametrize("response", [
    "sensitive-child-response",
    "[]",
    json.dumps({"plugin_id": "another_plugin", "version": "v1", "activated": True, "previous": None}),
    json.dumps({"plugin_id": deploy_dev.PLUGIN_ID, "version": "v1", "activated": False, "previous": None}),
    json.dumps({"plugin_id": deploy_dev.PLUGIN_ID, "version": None, "activated": True, "previous": None}),
    json.dumps({"plugin_id": deploy_dev.PLUGIN_ID, "version": "v1", "activated": True}),
])
def test_invalid_success_never_sends(fake_deployer, endpoint, monkeypatch, capsys, response):
    monkeypatch.setenv("FAKE_DEPLOY_OUTPUT", response)
    assert deploy_dev.main(deployment_args(fake_deployer)) == 1
    assert endpoint[1] == []
    output = capsys.readouterr()
    assert "sensitive-child" not in output.out + output.err


def test_deployment_smoke_uses_confirmed_version(fake_deployer, endpoint, capsys):
    assert deploy_dev.main(deployment_args(fake_deployer) + ["--no-rollback"]) == 0
    received = endpoint[1]
    assert len(received) == 4
    assert {item["body"]["status"] for item in received} == {"completed", "failed", "action_required"}
    assert all("[部署验收 v9.8.7-test.external]" in item["body"]["session"]["name"] for item in received)
    assert all(item["authorization"] == "Bearer smoke-test-token" for item in received)
    record, = calls(fake_deployer[2])
    assert record["action"] == "deploy"
    assert record["plugin"] == deploy_dev.PLUGIN_ID
    assert record["environment"] == "test"
    assert record["no_rollback"] is True
    output = capsys.readouterr()
    assert "冒烟全部送达" in output.out
    assert "sensitive-child" not in output.out + output.err


def test_smoke_failure_does_not_request_rollback(fake_deployer, endpoint):
    endpoint[2]["data"]["delivered"] = False
    assert deploy_dev.main(deployment_args(fake_deployer)) == 1
    assert len(endpoint[1]) == 4
    assert [call["action"] for call in calls(fake_deployer[2])] == ["deploy"]


def test_dry_run_queries_without_sending(fake_deployer, endpoint, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_DEPLOY_OUTPUT", json.dumps({
        "plugin_id": deploy_dev.PLUGIN_ID, "version": None, "activated": False,
    }))
    assert deploy_dev.main(deployment_args(fake_deployer) + ["--dry-run", "--no-rollback"]) == 0
    record, = calls(fake_deployer[2])
    assert record["action"] == "query"
    assert record["no_rollback"] is False
    assert endpoint[1] == []
    assert "未加载" in capsys.readouterr().out


@pytest.mark.parametrize("configuration", ["file", "environment", "explicit"])
def test_configuration_precedence(fake_deployer, tmp_path, monkeypatch, configuration):
    path, package, log = fake_deployer
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        f"ASTRBOT_PLUGIN_DEPLOYER={path}\nASTRBOT_DEPLOY_ENVIRONMENT=file-target\n"
        "ASTRBOT_API_KEY=file-test-key\n",
        encoding="utf-8",
    )
    args = ["--env-file", str(env_file), "--zip", str(package), "--skip-smoke"]
    expected = "file-target"
    if configuration in ("environment", "explicit"):
        monkeypatch.setenv("ASTRBOT_PLUGIN_DEPLOYER", str(path))
        monkeypatch.setenv("ASTRBOT_DEPLOY_ENVIRONMENT", "environment-target")
        monkeypatch.setenv("ASTRBOT_API_KEY", "environment-test-key")
        expected = "environment-target"
    if configuration == "explicit":
        monkeypatch.setenv("ASTRBOT_PLUGIN_DEPLOYER", str(tmp_path / "unavailable.py"))
        args.extend(("--deployer", str(path), "--environment", "explicit-target"))
        expected = "explicit-target"
    assert deploy_dev.main(args) == 0
    record, = calls(log)
    assert record["environment"] == expected
    assert record["env_file"] == str(env_file)
    assert record["key"] == ("file-test-key" if configuration == "file" else "environment-test-key")


@pytest.fixture
def verification_target(tmp_path, endpoint, monkeypatch):
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    monkeypatch.setattr(deploy_dev, "BASELINE_DIR", baselines)
    image = tmp_path / "received.png"
    Image.new("RGB", (2, 2), "white").save(image)
    messages = []
    for key, caption, count in deploy_dev.VISUAL_EXPECTED:
        segments = [{"type": "text"}]
        for index in range(1, count + 1):
            (baselines / f"{key}-{index}.png").write_bytes(image.read_bytes())
            segments.append({"type": "image", "format": "png", "width": 2, "height": 2, "path": str(image)})
        messages.append({"marked": caption is not None, "text": caption, "segments": segments})
    probe = tmp_path / "probe"
    probe.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "assert sys.argv[1] == 'messages'\nassert sys.argv[3] == '[视觉基准]'\n"
        f"print({json.dumps({'ok': True, 'history_status': 'ok', 'messages': messages})!r})\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    env_file = tmp_path / "verify.env"
    env_file.write_text(
        f"WEBHOOK_NOTIFIER_URL={endpoint[0]}\nWEBHOOK_NOTIFIER_TOKEN=verification-test-token\n",
        encoding="utf-8",
    )
    # Ambient smoke configuration must not override the explicitly isolated target.
    monkeypatch.setenv("WEBHOOK_NOTIFIER_URL", "http://unused.invalid")
    monkeypatch.setenv("WEBHOOK_NOTIFIER_TOKEN", "ambient-test-token")
    return ["--verify", str(env_file), "--probe", str(probe)]


def test_standalone_verification_without_deployer(verification_target, endpoint, capsys):
    assert not os.environ.get("ASTRBOT_PLUGIN_DEPLOYER")
    assert not os.environ.get("ASTRBOT_DEPLOY_ENVIRONMENT")
    assert not os.environ.get("ASTRBOT_DASHBOARD_URL")
    assert not os.environ.get("ASTRBOT_API_KEY")
    assert deploy_dev.main(["--skip-deploy", *verification_target]) == 0
    assert len(endpoint[1]) == 7
    assert all(item["authorization"] == "Bearer verification-test-token" for item in endpoint[1])
    assert "接收端验收通过" in capsys.readouterr().out


def test_deploy_then_verify_replaces_default_smoke(fake_deployer, verification_target, endpoint):
    assert deploy_dev.main(deployment_args(fake_deployer) + verification_target) == 0
    assert len(endpoint[1]) == 7
    assert all(item["authorization"] == "Bearer verification-test-token" for item in endpoint[1])
    assert [call["action"] for call in calls(fake_deployer[2])] == ["deploy"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_failed_deploy_or_query_does_not_verify(fake_deployer, verification_target, endpoint, monkeypatch, dry_run):
    monkeypatch.setenv("FAKE_DEPLOY_EXIT", "9")
    args = deployment_args(fake_deployer) + verification_target + (["--dry-run"] if dry_run else [])
    assert deploy_dev.main(args) == 1
    assert endpoint[1] == []
