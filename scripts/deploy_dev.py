#!/usr/bin/env python3
"""经 AstrBot Dashboard Open API 部署插件测试包并做 Webhook 冒烟验收。

步骤与 WebUI 手工流程一致：卸载现有插件（固定保留配置和数据）→ 上传 ZIP →
核对线上加载的版本 → 向 OMP Endpoint 发送合成 V2 事件并检查业务结果。

凭据只从环境变量或 --env-file 读取，不写入日志：
  ASTRBOT_DASHBOARD_URL   Dashboard 根地址，例如 https://host:6185
  ASTRBOT_API_KEY         具备 plugin scope 的 Open API Key
  WEBHOOK_NOTIFIER_URL    OMP Endpoint 完整 URL（冒烟用，可用 --skip-smoke 跳过）
  WEBHOOK_NOTIFIER_TOKEN  对应 Bearer Token

接口依据 AstrBot v4.27.2 的 /api/v1/plugins*；其他版本使用前先核对。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "tmp" / "deploy-dev" / "last-deployed.json"
REQUIRED_ENV = ("ASTRBOT_DASHBOARD_URL", "ASTRBOT_API_KEY")
SMOKE_ENV = ("WEBHOOK_NOTIFIER_URL", "WEBHOOK_NOTIFIER_TOKEN")


class DeployError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_env(names: tuple[str, ...]) -> dict[str, str]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise DeployError(f"缺少环境变量：{', '.join(missing)}")
    return {name: os.environ[name] for name in names}


def zip_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        root = archive.namelist()[0]
        metadata = yaml.safe_load(archive.read(f"{root}metadata.yaml"))
    return str(metadata["name"]), str(metadata["version"])


def build_package(label: str) -> Path:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package_plugin.py"), "--dev-version", "--test-label", label],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip().splitlines()[-1])


class Dashboard:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base = base_url.rstrip("/") + "/api/v1"
        self.api_key = api_key

    def call(self, method: str, path: str, *, body: bytes | None = None,
             content_type: str = "application/json", timeout: float = 120) -> dict:
        request = urllib.request.Request(self.base + path, data=body, method=method)
        request.add_header("X-API-Key", self.api_key)
        if body is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise DeployError(f"{method} {path} 返回 HTTP {exc.code}") from None
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            message = payload.get("message") if isinstance(payload, dict) else None
            raise DeployError(f"{method} {path} 失败：{message or '响应无效'}")
        return payload

    def plugin(self, plugin_id: str) -> dict | None:
        try:
            data = self.call("GET", f"/plugins/by-id?plugin_id={plugin_id}")["data"]
        except DeployError:
            return None
        return data if isinstance(data, dict) and data.get("name") == plugin_id else None

    def failed(self) -> dict:
        data = self.call("GET", "/plugins/failed")["data"]
        return data if isinstance(data, dict) else {}

    def uninstall(self, plugin_id: str) -> None:
        # 固定不删除配置与持久化数据：Registry 保存全部 Endpoint 与 Token 哈希。
        body = json.dumps({"delete_config": False, "delete_data": False}).encode()
        self.call("DELETE", f"/plugins/by-id?plugin_id={plugin_id}", body=body)

    def uninstall_failed(self, dir_name: str) -> None:
        body = json.dumps({"delete_config": False, "delete_data": False}).encode()
        self.call("DELETE", f"/plugins/failed/{dir_name}", body=body)

    def upload(self, path: Path) -> None:
        boundary = "----deploy-dev-" + secrets.token_hex(12)
        head = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: application/zip\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        self.call("POST", "/plugins/install/upload", body=head + path.read_bytes() + tail,
                  content_type=f"multipart/form-data; boundary={boundary}", timeout=300)


def deploy(dashboard: Dashboard, zip_path: Path) -> dict:
    plugin_id, version = zip_identity(zip_path)
    before = dashboard.plugin(plugin_id)
    failed = dashboard.failed()
    print(f"线上当前：{before.get('version') if before else '未加载'}；目标：{version}")
    if before:
        dashboard.uninstall(plugin_id)
    elif plugin_id in failed:
        dashboard.uninstall_failed(plugin_id)
    try:
        dashboard.upload(zip_path)
        deadline = time.monotonic() + 60
        while True:
            current = dashboard.plugin(plugin_id)
            if current and current.get("version") == version and current.get("activated", True):
                break
            if time.monotonic() > deadline:
                raise DeployError("上传后 60 秒内未加载目标版本")
            time.sleep(2)
    except DeployError:
        rollback = _last_deployed()
        if rollback and rollback != zip_path and rollback.is_file():
            print(f"安装失败，回装上次部署包：{rollback.name}")
            if dashboard.plugin(plugin_id) is None and plugin_id not in dashboard.failed():
                dashboard.upload(rollback)
        raise
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"zip": str(zip_path), "version": version}, ensure_ascii=False) + "\n")
    print(f"已加载：{plugin_id} {version}")
    return {"plugin_id": plugin_id, "version": version, "previous": before.get("version") if before else None}


def _last_deployed() -> Path | None:
    try:
        return Path(json.loads(STATE_FILE.read_text())["zip"])
    except (OSError, ValueError, KeyError):
        return None


def _ref() -> str:
    return secrets.token_hex(16)


def smoke_payloads(marker: str) -> list[tuple[str, dict]]:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(seconds=30)
    iso = lambda value: value.isoformat().replace("+00:00", "Z")  # noqa: E731

    def envelope(status: str, name: str, timeline: bool = False) -> dict:
        cycle_ref = _ref()
        body = {
            "event": "omp.run_finished", "version": 2, "id": _ref(), "emittedAt": iso(end), "status": status,
            "session": {"ref": _ref(), "scope": "root", "name": f"{marker} {name}", "cwdName": "deploy-smoke"},
            "cycle": {"ref": cycle_ref, "partial": False, "reasons": [], "startedAt": iso(start),
                      "endedAt": iso(end), "durationMs": 30000, "timeBasis": "receipt_monotonic",
                      "model": "smoke/model", "thinking": "medium"},
        }
        if timeline:
            items = []
            for index, (begin, finish) in enumerate(((2000, 14000), (6000, 20000))):
                ref = _ref()
                items.append({"ref": ref, "cycleRef": ref, "parentRef": cycle_ref, "depth": 1, "attempt": 1,
                              "status": "completed", "name": f"冒烟子代理 {index + 1}", "timingQuality": "observed",
                              "startOffsetMs": begin, "endOffsetMs": finish, "durationMs": finish - begin})
            body["subagentTimeline"] = {"version": 1, "timeBasis": "omp_agent_observation", "partial": False,
                                        "partialReasons": [], "truncated": False, "observedItemCount": 2,
                                        "displayedItemCount": 2, "items": items}
            body["userWaitTimeline"] = {"version": 1, "timeBasis": "omp_interaction_observation", "partial": False,
                                        "partialReasons": [], "truncated": False, "observedIntervalCount": 1,
                                        "displayedIntervalCount": 1,
                                        "intervals": [{"ref": _ref(), "agentRef": cycle_ref, "kind": "ask",
                                                       "result": "resolved", "intervalState": "complete",
                                                       "startOffsetMs": 21000, "endOffsetMs": 25000,
                                                       "durationMs": 4000}]}
        return body

    action = envelope("action_required", "待处理")
    action["event"] = "omp.action_required"
    del action["cycle"]["endedAt"]
    action["interaction"] = {"ref": _ref(), "kind": "ask", "questionCount": 1, "questions": [{
        "header": "冒烟选择", "text": "部署后的待处理卡是否显示完整？",
        "options": [{"label": "显示完整", "description": "状态条、会话标题、问题与选项都可读。", "recommended": True},
                    {"label": "需要调整"}],
    }]}
    return [
        ("简单完成", envelope("completed", "简单完成")),
        ("多代理时间线", envelope("completed", "多代理时间线", timeline=True)),
        ("失败", envelope("failed", "失败")),
        ("待处理", action),
    ]


def smoke(url: str, token: str, version: str) -> list[dict]:
    results = []
    deadline = time.monotonic() + 30
    for label, body in smoke_payloads(f"[部署验收 {version}]"):
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-OMP-Event", body["event"])
        while True:
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    status, payload = response.status, json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, None
                break
            except urllib.error.URLError:
                # 重载后 Webhook 监听可能稍晚就绪；只对连接失败有界重试。
                if time.monotonic() > deadline:
                    raise DeployError("Webhook 端口 30 秒内未就绪") from None
                time.sleep(2)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        ok = status == 200 and isinstance(payload, dict) and payload.get("code") == 0 and data.get("delivered") is True
        results.append({"case": label, "http": status, "ok": ok, "render_mode": data.get("render_mode"),
                        "fallback_reason": data.get("fallback_reason"), "message": payload.get("message") if isinstance(payload, dict) else None})
        print(f"冒烟 {label}：HTTP {status}，delivered={data.get('delivered')}，render_mode={data.get('render_mode')}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--zip", type=Path, help="部署已有 ZIP；省略时按 --label 新建测试包")
    source.add_argument("--label", default="dev", help="新建测试包的标签，默认 dev")
    parser.add_argument("--env-file", type=Path, help="KEY=VALUE 格式的凭据文件；已有环境变量优先")
    parser.add_argument("--skip-smoke", action="store_true", help="只部署，不发送冒烟通知")
    parser.add_argument("--dry-run", action="store_true", help="只核对包与线上版本，不卸载、不上传")
    args = parser.parse_args(argv)
    try:
        if args.env_file:
            load_env_file(args.env_file)
        env = require_env(REQUIRED_ENV)
        smoke_env = None if args.skip_smoke or args.dry_run else require_env(SMOKE_ENV)
        zip_path = (args.zip or build_package(args.label)).resolve()
        plugin_id, version = zip_identity(zip_path)
        dashboard = Dashboard(env["ASTRBOT_DASHBOARD_URL"], env["ASTRBOT_API_KEY"])
        if args.dry_run:
            current = dashboard.plugin(plugin_id)
            print(f"包：{zip_path.name}（{version}）；线上：{current.get('version') if current else '未加载'}")
            return 0
        deploy(dashboard, zip_path)
        if smoke_env:
            results = smoke(smoke_env["WEBHOOK_NOTIFIER_URL"], smoke_env["WEBHOOK_NOTIFIER_TOKEN"], version)
            if not all(item["ok"] for item in results):
                raise DeployError("冒烟未全部送达：" + json.dumps(results, ensure_ascii=False))
            print(f"冒烟全部送达；请在聊天中确认 {len(results)} 张卡片的显示效果。")
        return 0
    except (DeployError, subprocess.CalledProcessError) as exc:
        print(f"部署失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
