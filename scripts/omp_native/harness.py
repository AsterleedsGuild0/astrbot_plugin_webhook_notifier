"""原生 OMP 隔离验收的公共部分：隔离 HOME、子进程环境、客户端加载方式、RPC 与 TUI 驱动。

- 隔离 HOME 与日志位于仓库被 Git 忽略的 ``tmp/omp-native/``（可用 ``OMP_NATIVE_ROOT`` 改到其他忽略目录）。
- 子进程环境从空白构造，不继承调用方 shell 中的 ``OMP_SESSION_WEBHOOK_*``／``WEBHOOK_NOTIFIER_*``，
  避免把验收通知发往生产 Endpoint。
- 测试 Endpoint 只从 ``OMP_NATIVE_VERIFY_FILE`` 指向的 ``KEY=VALUE`` 文件读取（与 ``deploy_dev.py --verify`` 同格式）。
- 模型 provider 只把真实 ``models.yml`` 中一个 provider 的结构写入隔离 HOME；``apiKey`` 写成环境变量名，
  值在启动时从用户登录 shell 只读注入子进程，不落盘。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PACKAGE = REPO / "integrations/omp"
BASE = Path(os.environ.get("OMP_NATIVE_ROOT", REPO / "tmp/omp-native"))
HOME = BASE / "home"
AGENT = HOME / ".omp/agent"
LOGS = BASE / "logs"
WORK = BASE / "work"
WEBHOOK_PORT = 18765
MODEL_PORT = 18766
PROVIDER = os.environ.get("OMP_NATIVE_PROVIDER", "cpa")
MODEL = os.environ.get("OMP_NATIVE_MODEL", f"{PROVIDER}/deepseek-flash")
KEY_VARIABLE = "OMP_NATIVE_PROVIDER_KEY"
MARKER = "原生验收"


def real_home() -> Path:
    return Path(os.environ.get("REAL_HOME", os.path.expanduser("~")))


def verify_file() -> Path:
    value = os.environ.get("OMP_NATIVE_VERIFY_FILE")
    if not value:
        raise SystemExit("OMP_NATIVE_VERIFY_FILE 未设置：指向测试 Endpoint 的 KEY=VALUE 文件")
    return Path(value).expanduser()


def secret_value(key: str) -> str:
    for line in verify_file().read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"测试 Endpoint 文件缺少 {key}")


def real_provider() -> dict[str, Any]:
    models = yaml.safe_load((real_home() / ".omp/agent/models.yml").read_text())
    return dict(models["providers"][PROVIDER])


def setup() -> None:
    """只写 provider 结构；``baseUrl`` 指向本机模型代理，``apiKey`` 为环境变量名。"""
    provider = real_provider()
    provider["baseUrl"] = f"http://127.0.0.1:{MODEL_PORT}/v1"
    provider["apiKey"] = KEY_VARIABLE
    AGENT.mkdir(parents=True, exist_ok=True)
    (AGENT / "models.yml").write_text(yaml.safe_dump({"providers": {PROVIDER: provider}}, allow_unicode=True, sort_keys=False))
    if not (AGENT / "config.yml").exists():
        (AGENT / "config.yml").write_text("{}\n")
    for directory in (LOGS, WORK):
        directory.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"agent": str(AGENT), "provider": PROVIDER, "models": [m["id"] for m in provider.get("models", [])]}, ensure_ascii=False))


def provider_key() -> str:
    """真实 models.yml 的 apiKey 是环境变量名；只读取该变量的值注入子进程。"""
    name = str(real_provider()["apiKey"])
    if os.environ.get(name):
        return os.environ[name]
    value = subprocess.run(["zsh", "-ic", f'printf %s "${name}"'], capture_output=True, text=True).stdout
    if not value:
        raise SystemExit(f"{name} 不可用")
    return value


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"], "HOME": str(HOME), "LANG": "en_US.UTF-8", "TERM": "dumb", "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "PI_CODING_AGENT_DIR": str(AGENT),
        "OMP_SESSION_WEBHOOK_URL": f"http://127.0.0.1:{WEBHOOK_PORT}/omp",
        "OMP_SESSION_WEBHOOK_TOKEN": secret_value("WEBHOOK_NOTIFIER_TOKEN"),
        KEY_VARIABLE: provider_key(),
        "OMP_ACCEPT_EVENT_LOG": str(LOGS / "events.jsonl"),
    }
    env.update(extra or {})
    return env


def load_args() -> list[str]:
    """``package``（默认）：只显式加载仓库客户端包；``installed``：使用隔离 HOME 中安装的插件（marketplace 验收）。"""
    mode = os.environ.get("OMP_NATIVE_LOAD", "package")
    recorder = ["-e", str(HERE / "event-recorder.ts")]
    if mode == "package":
        return ["--no-extensions", "-e", str(PACKAGE), *recorder]
    if mode == "installed":
        return recorder
    raise SystemExit("OMP_NATIVE_LOAD 只能是 package 或 installed")


def omp_command(*extra: str) -> list[str]:
    return ["omp", *extra, "--no-skills", "--no-rules", "--no-lsp", "--no-title", *load_args(),
            "--model", MODEL, "--thinking", "low"]


def proxies() -> None:
    """前台运行本机代理：Webhook 原样转发到测试 Endpoint 并记录请求体与响应（不记录 Token）；模型代理可注入故障。"""
    LOGS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(ACCEPT_WEBHOOK_TARGET=secret_value("WEBHOOK_NOTIFIER_URL"), ACCEPT_WEBHOOK_LOG=str(LOGS / "webhook.jsonl"),
               ACCEPT_MODEL_LOG=str(LOGS / "model.jsonl"), ACCEPT_WEBHOOK_PORT=str(WEBHOOK_PORT), ACCEPT_MODEL_PORT=str(MODEL_PORT),
               ACCEPT_MODEL_TARGET=str(real_provider()["baseUrl"]).removesuffix("/v1"))
    os.execvpe("bun", ["bun", str(HERE / "proxies.ts")], env)


def rows(name: str, start: int = 0) -> list[dict[str, Any]]:
    path = LOGS / name
    return [json.loads(line) for line in list(path.open())[start:]] if path.exists() else []


def webhook_summary(items: list[dict[str, Any]]) -> list[str]:
    out = []
    for j in items:
        rq = j["request"]
        rs = j["response"] if isinstance(j["response"], dict) else {}
        data = rs.get("data", {})
        st, wt = rq.get("subagentTimeline") or {}, rq.get("userWaitTimeline") or {}
        out.append(" ".join(str(x) for x in (
            j["t"][11:19], f"{j['ms']}ms", rq["event"], rq.get("status"), rq.get("session", {}).get("scope"), rq.get("session", {}).get("name"),
            "| cycle", rq.get("cycle", {}).get("durationMs"), rq.get("cycle", {}).get("reasons"),
            "| inter", (rq.get("interaction") or {}).get("kind"),
            "| tl", [f"{i.get('depth')}:{i.get('name') or i.get('agent')}:{i.get('status')}" for i in st.get("items", [])],
            "| wait", [f"{i.get('kind')}:{i.get('result')}:{i.get('intervalState')}" for i in wt.get("intervals", [])],
            "| resp", j["status"], rs.get("message"), data.get("delivered"), data.get("skip_reason"))))
    return out


def event_summary(items: list[dict[str, Any]]) -> list[str]:
    skip = {"t", "mono", "session", "sessionName", "agent", "hasUI", "model", "thinking", "jobs", "type"}
    out = []
    for e in items:
        a = e.get("agent") or {}
        out.append(f"{e['t'][11:23]} {a.get('kind')} {a.get('id')} d{a.get('depth')} {e['type']} {json.dumps({k: v for k, v in e.items() if k not in skip}, ensure_ascii=False)}")
    return out


class Omp:
    """``--mode rpc-ui`` 驱动；扩展 UI 请求可由 ``ui`` 回调应答（Hook 可见 ``hasUI=true``）。"""

    def __init__(self, scenario: str, args: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
        self.scenario, self.args, self.cwd, self.extra_env = scenario, args, cwd, env
        self.frames: list[dict[str, Any]] = []
        self.waiters: list[tuple[Callable[[dict[str, Any]], bool], asyncio.Future]] = []
        self.ui: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
        self.n = 0
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log = open(LOGS / f"{scenario}.rpc.jsonl", "a")

    async def start(self) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        cmd = [*omp_command("--mode", "rpc-ui"), "--cwd", str(self.cwd), *self.args]
        self.proc = await asyncio.create_subprocess_exec(*cmd, cwd=self.cwd, env=child_env(self.extra_env), stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, stderr=open(LOGS / f"{self.scenario}.stderr.log", "a"), limit=1 << 26)
        self.reader = asyncio.create_task(self._read())
        await self.wait(lambda f: f.get("type") == "ready", 60)

    async def _read(self) -> None:
        assert self.proc.stdout
        while line := await self.proc.stdout.readline():
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            frame["_t"] = time.time()
            if frame.get("type") not in ("message_update", "tool_execution_update"):
                self.log.write(json.dumps(frame, ensure_ascii=False)[:4000] + "\n")
                self.log.flush()
            self.frames.append(frame)
            for pred, fut in list(self.waiters):
                if not fut.done() and pred(frame):
                    fut.set_result(frame)
            if frame.get("type") == "extension_ui_request" and frame.get("method") not in ("notify", "setStatus", "setWidget", "setTitle", "set_editor_text", "cancel") and self.ui:
                asyncio.create_task(self._answer(frame))

    async def _answer(self, frame: dict[str, Any]) -> None:
        reply = await self.ui(frame)  # type: ignore[misc]
        if reply is not None:
            await self.send({"type": "extension_ui_response", "id": frame["id"], **reply})

    async def send(self, cmd: dict[str, Any]) -> str:
        if "id" not in cmd and cmd.get("type") != "extension_ui_response":
            self.n += 1
            cmd["id"] = f"{self.scenario}-{self.n}"
        self.log.write(json.dumps({"_sent": cmd, "_t": time.time()}, ensure_ascii=False) + "\n")
        assert self.proc.stdin
        self.proc.stdin.write((json.dumps(cmd, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()
        return cmd.get("id", "")

    async def wait(self, pred: Callable[[dict[str, Any]], bool], timeout: float = 300, since: int = 0) -> dict[str, Any]:
        for frame in self.frames[since:]:
            if pred(frame):
                return frame
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.waiters.append((pred, fut))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.waiters.remove((pred, fut))

    async def call(self, cmd: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        rid = await self.send(cmd)
        return await self.wait(lambda f: f.get("type") == "response" and f.get("id") == rid, timeout)

    async def prompt(self, message: str, timeout: float = 600) -> dict[str, Any]:
        mark = len(self.frames)
        rid = await self.send({"type": "prompt", "message": message})
        return await self.wait(lambda f: f.get("type") == "prompt_result" and f.get("id") == rid, timeout, since=mark)

    async def settled(self, timeout: float = 600, since: int = 0) -> None:
        await self.wait(lambda f: f.get("type") == "session_settled", timeout, since=since)

    async def close(self, grace: float = 8) -> int:
        await asyncio.sleep(grace)  # 让 Hook 发送队列排空
        assert self.proc.stdin
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 60)
        except asyncio.TimeoutError:
            self.proc.terminate()
            await self.proc.wait()
        await asyncio.sleep(0.2)
        self.log.close()
        return self.proc.returncode or 0


class Tui:
    """伪终端中的交互式 OMP；RPC 没有 ``/plan``，计划评审只能在 TUI 中触发。原始终端输出写入 ``<名称>.tty.log``。"""

    DOWN = b"\x1b[B"
    UP = b"\x1b[A"

    def __init__(self, name: str, *args: str) -> None:
        self.name = name
        self.cwd = WORK / name.lower()
        self.cwd.mkdir(parents=True, exist_ok=True)
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log = (LOGS / f"{name}.tty.log").open("ab")
        self.screen = bytearray()
        env = child_env({"TERM": "xterm-256color", "COLUMNS": "160", "LINES": "48"})
        cmd = [*omp_command(*args), "--cwd", str(self.cwd)]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(self.cwd)
            os.execvpe(cmd[0], cmd, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 160, 0, 0))

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return
                self.screen.extend(data)
                self.log.write(data)
                self.log.flush()

    def wait_screen(self, text: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.5)
            if text.encode() in self.screen:
                return True
        return False

    def keys(self, data: bytes, pause: float = 0.4) -> None:
        os.write(self.fd, data)
        self.pump(pause)

    def type_line(self, text: str) -> None:
        for ch in text:
            os.write(self.fd, ch.encode())
            self.pump(0.02)
        self.pump(0.5)
        os.write(self.fd, b"\r")

    def wait_event(self, start: int, pred: Callable[[dict[str, Any]], bool], timeout: float) -> dict[str, Any] | None:
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.5)
            for event in rows("events.jsonl", start):
                if pred(event):
                    return event
        return None

    def alive(self) -> bool:
        return os.waitpid(self.pid, os.WNOHANG) == (0, 0)

    def quit(self) -> None:
        for _ in range(2):
            os.write(self.fd, b"\x03")
            self.pump(1)
        self.pump(4)
        try:
            os.kill(self.pid, 15)
        except ProcessLookupError:
            pass
        self.pump(3)
