#!/usr/bin/env python3
"""经 AstrBot Dashboard Open API 部署插件测试包并做 Webhook 冒烟验收。

步骤与 WebUI 手工流程一致：卸载现有插件（固定保留配置和数据）→ 上传 ZIP →
核对线上加载的版本 → 向 OMP Endpoint 发送合成 V2 事件并检查业务结果。

凭据只从环境变量或 --env-file 读取，不写入日志：
  ASTRBOT_DASHBOARD_URL   Dashboard 根地址，例如 https://host:6185
  ASTRBOT_API_KEY         具备 plugin scope 的 Open API Key
  WEBHOOK_NOTIFIER_URL    OMP Endpoint 完整 URL（冒烟用，可用 --skip-smoke 跳过）
  WEBHOOK_NOTIFIER_TOKEN  对应 Bearer Token

接收端验收（--verify ENV_FILE --probe PROBE）：改为向 ENV_FILE 中的测试 Endpoint 发送固定内容的
冒烟，由 PROBE 从接收账号读回消息，核对每条文字、文字与主卡在同一条消息、图片张数，并与
tests/visual/baselines/ 中的基准图逐像素比较；首次或有意改版时加 --update-baselines。

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


def smoke_payloads(marker: str, at: datetime | None = None) -> list[tuple[str, dict]]:
    # at 固定时，卡片上的时间与时长都确定，接收端图片可与基准图逐像素比较。
    end = at or datetime.now(timezone.utc).replace(microsecond=0)
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


def smoke(url: str, token: str, marker: str, at: datetime | None = None) -> list[dict]:
    results = []
    deadline = time.monotonic() + 30
    for label, body in smoke_payloads(marker, at):
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


VISUAL_MARKER = "[视觉基准]"
VISUAL_AT = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
# 冒烟用例 → 接收端应看到的文字（独立写出，不复用插件实现）与图片张数。
VISUAL_EXPECTED = (
    ("simple", "【已完成】[视觉基准] 简单完成", 1),
    ("timeline", "【已完成】[视觉基准] 多代理时间线", 2),
    ("failed", "【失败】[视觉基准] 失败", 1),
    ("action", "【待处理】[视觉基准] 待处理 · 等待问题回答", 1),
)
BASELINE_DIR = ROOT / "tests" / "visual" / "baselines"
PIXEL_TOLERANCE = 16
MAX_DIFF_RATIO = 0.001
RECEIVE_TIMEOUT_S = 120


def read_env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def run_probe(probe: Path, since: str) -> dict:
    result = subprocess.run([str(probe), "messages", since, VISUAL_MARKER],
                            capture_output=True, text=True, timeout=180)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise DeployError(f"接收端探测输出无法解析（exit {result.returncode}）") from None
    if not payload.get("ok") or payload.get("history_status") != "ok":
        raise DeployError("接收端探测失败：" + json.dumps(
            {k: payload.get(k) for k in ("stage", "peer_matches", "history_status", "history_retcode")}, ensure_ascii=False))
    return payload


def group_received(messages: list[dict]) -> tuple[list[dict], int]:
    """按会话顺序把文字与其后的图片归为一组；文字之前的孤立图片单独计数。"""
    groups: list[dict] = []
    orphans = 0
    for message in messages:
        segments = message.get("segments", [])
        types = [segment.get("type") for segment in segments]
        images = [segment for segment in segments if segment.get("type") == "image"]
        if message.get("marked"):
            # 文字必须与主卡同条：通知预览只显示最后一条，拆开发送会只剩“[图片]”。
            groups.append({"caption": message.get("text", ""), "with_card": "image" in types, "images": images})
        elif groups:
            groups[-1]["images"].extend(images)
        else:
            orphans += len(images)
    return groups, orphans


def compare_image(actual: Path, baseline: Path, diff_path: Path) -> str | None:
    from PIL import Image, ImageChops

    with Image.open(actual) as a, Image.open(baseline) as b:
        a, b = a.convert("RGB"), b.convert("RGB")
        if a.size != b.size:
            return f"尺寸 {a.size} 与基准 {b.size} 不同"
        diff = ImageChops.difference(a, b).convert("L")
        changed = sum(diff.histogram()[PIXEL_TOLERANCE + 1:])
        ratio = changed / (a.size[0] * a.size[1])
        if ratio <= MAX_DIFF_RATIO:
            return None
        diff.point(lambda value: 255 if value > PIXEL_TOLERANCE else 0).save(diff_path)
        return f"{ratio:.2%} 像素与基准不同，差异图 {diff_path}"


def verify(url: str, token: str, probe: Path, update_baselines: bool) -> list[str]:
    """向测试接收端发送固定内容冒烟，读回并逐项核对；返回问题列表，空表示通过。"""
    since = (datetime.now(timezone.utc) - timedelta(seconds=2)).replace(microsecond=0)
    sent = smoke(url, token, VISUAL_MARKER, at=VISUAL_AT)
    problems = [f"{item['case']}：HTTP {item['http']} 未送达" for item in sent if not item["ok"]]
    if problems:
        return problems
    since_text = since.isoformat().replace("+00:00", "Z")
    deadline = time.monotonic() + RECEIVE_TIMEOUT_S
    while True:
        groups, orphans = group_received(run_probe(probe, since_text).get("messages", []))
        complete = len(groups) >= len(VISUAL_EXPECTED) and all(
            len(group["images"]) >= expected for group, (_, _, expected) in zip(groups, VISUAL_EXPECTED))
        if complete or time.monotonic() > deadline:
            break
        time.sleep(5)

    if orphans:
        problems.append(f"有 {orphans} 张图片出现在第一行文字之前")
    if len(groups) != len(VISUAL_EXPECTED):
        problems.append(f"收到 {len(groups)} 组带标记的消息，预期 {len(VISUAL_EXPECTED)} 组")
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for group, (key, caption, expected) in zip(groups, VISUAL_EXPECTED):
        if group["caption"] != caption:
            problems.append(f"{key}：文字为 {group['caption']!r}，预期 {caption!r}")
        if not group["with_card"]:
            problems.append(f"{key}：文字没有和主卡在同一条消息里")
        if len(group["images"]) != expected:
            problems.append(f"{key}：收到 {len(group['images'])} 张图片，预期 {expected} 张")
        for index, image in enumerate(group["images"], start=1):
            if image.get("format") != "png" or "path" not in image:
                problems.append(f"{key}#{index}：图片不可用（{image.get('format') or image.get('error')}）")
                continue
            actual = Path(image["path"])
            baseline = BASELINE_DIR / f"{key}-{index}.png"
            print(f"接收 {key}#{index}：{image['width']}×{image['height']} {actual}")
            if update_baselines:
                baseline.write_bytes(actual.read_bytes())
                continue
            if not baseline.exists():
                problems.append(f"{key}#{index}：缺少基准图 {baseline.relative_to(ROOT)}，确认画面后用 --update-baselines 生成")
                continue
            mismatch = compare_image(actual, baseline, actual.with_name(f"diff-{key}-{index}.png"))
            if mismatch:
                problems.append(f"{key}#{index}：{mismatch}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--zip", type=Path, help="部署已有 ZIP；省略时按 --label 新建测试包")
    source.add_argument("--label", default="dev", help="新建测试包的标签，默认 dev")
    parser.add_argument("--env-file", type=Path, help="KEY=VALUE 格式的凭据文件；已有环境变量优先")
    parser.add_argument("--skip-smoke", action="store_true", help="只部署，不发送冒烟通知")
    parser.add_argument("--dry-run", action="store_true", help="只核对包与线上版本，不卸载、不上传")
    parser.add_argument("--verify", type=Path, metavar="ENV_FILE",
                        help="接收端验收：向该文件中的测试 Endpoint 发送固定内容冒烟，读回核对文字、顺序、图片数量并与基准图比较；"
                             "代替默认冒烟，不读取环境变量中的 WEBHOOK_NOTIFIER_*")
    parser.add_argument("--probe", type=Path, help="接收端探测命令，接口为 `<probe> messages <since> <marker>`；--verify 必填")
    parser.add_argument("--update-baselines", action="store_true", help="--verify 时用本次收到的图片覆盖基准图")
    parser.add_argument("--skip-deploy", action="store_true", help="不打包部署，只对线上现有版本执行 --verify")
    args = parser.parse_args(argv)
    if (args.verify is None) != (args.probe is None):
        parser.error("--verify 与 --probe 必须同时提供")
    if (args.update_baselines or args.skip_deploy) and args.verify is None:
        parser.error("--update-baselines 与 --skip-deploy 只用于 --verify")
    try:
        if args.env_file:
            load_env_file(args.env_file)
        verify_env = None
        if args.verify:
            values = read_env_values(args.verify)
            missing = [name for name in SMOKE_ENV if not values.get(name)]
            if missing:
                raise DeployError(f"{args.verify} 缺少 {', '.join(missing)}")
            verify_env = values
        if not args.skip_deploy:
            env = require_env(REQUIRED_ENV)
            smoke_env = None if args.skip_smoke or args.dry_run or verify_env else require_env(SMOKE_ENV)
            zip_path = (args.zip or build_package(args.label)).resolve()
            plugin_id, version = zip_identity(zip_path)
            dashboard = Dashboard(env["ASTRBOT_DASHBOARD_URL"], env["ASTRBOT_API_KEY"])
            if args.dry_run:
                current = dashboard.plugin(plugin_id)
                print(f"包：{zip_path.name}（{version}）；线上：{current.get('version') if current else '未加载'}")
                return 0
            deploy(dashboard, zip_path)
            if smoke_env:
                results = smoke(smoke_env["WEBHOOK_NOTIFIER_URL"], smoke_env["WEBHOOK_NOTIFIER_TOKEN"], f"[部署验收 {version}]")
                if not all(item["ok"] for item in results):
                    raise DeployError("冒烟未全部送达：" + json.dumps(results, ensure_ascii=False))
                print(f"冒烟全部送达；请在聊天中确认 {len(results)} 张卡片的显示效果。")
        if verify_env:
            problems = verify(verify_env["WEBHOOK_NOTIFIER_URL"], verify_env["WEBHOOK_NOTIFIER_TOKEN"],
                              args.probe, args.update_baselines)
            if problems:
                raise DeployError("接收端验收未通过：\n- " + "\n- ".join(problems))
            print("接收端验收通过" + ("；基准图已更新" if args.update_baselines else "；与基准图一致"))
        return 0
    except (DeployError, subprocess.CalledProcessError) as exc:
        print(f"部署失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
