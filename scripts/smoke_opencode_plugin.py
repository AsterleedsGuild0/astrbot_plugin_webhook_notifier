#!/usr/bin/env python3
"""在隔离环境中验证 OpenCode CLI 是否实际调用 V1 Plugin server。

该夹具只创建临时 HOME/XDG 目录、临时 wrapper、marker 和 token 文件。
它不会读取用户 auth/secrets，也不会写入用户配置；Desktop 模式默认只报告
安全的人工验证 SKIP，不会启动、连接或终止现有 Desktop 实例。
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

PASS = 0
FAIL = 1
SKIP = 2
USAGE = 3

DEFAULT_TIMEOUT_SECONDS = 20.0
_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b")


def build_wrapper(real_plugin_uri: str) -> str:
    """生成只增加 marker 的 V1 default-export wrapper。"""

    real_uri = json.dumps(real_plugin_uri)
    return f"""import real from {real_uri};
import {{ appendFileSync }} from "node:fs";

const marker = process.env.OPENCODE_SMOKE_MARKER;

const server = async (input, options) => {{
  if (marker) appendFileSync(marker, "server\\n", {{ encoding: "utf8" }});
  return await real.server(input, options);
}};

export default {{ id: real.id, server }};
"""


def build_config_content(wrapper_uri: str, token_file: Path) -> str:
    """生成 JSON 形式的 OPENCODE_CONFIG_CONTENT。

    使用真实的 V1 plugin tuple：``[module_url, options]``。URL 使用 env
    插值，Token 使用临时 file 插值，避免把凭据写入配置正文。
    """

    config = {
        "plugin": [
            [
                wrapper_uri,
                {
                    "url": "{env:OPENCODE_SMOKE_WEBHOOK_URL}",
                    "token": "{file:" + token_file.as_posix() + "}",
                    "timeoutMs": 1000,
                    "enabled": True,
                    "events": [
                        "session_idle",
                        "session_error",
                        "permission_asked",
                        "question_asked",
                    ],
                },
            ]
        ]
    }
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


def _emit_version(version: str | None) -> None:
    print(f"version={version or 'unknown'}")


def _emit_result(result: str, stage: str) -> None:
    print(f"{result} stage={stage}")


def _resolve_cli_binary(explicit: str | None) -> Path | None:
    candidates: list[str | Path] = []
    if explicit:
        candidates.append(explicit)
    env_binary = os.environ.get("OPENCODE_BIN")
    if env_binary:
        candidates.append(env_binary)
    found = shutil.which("opencode")
    if found:
        candidates.append(found)
    candidates.append(Path.home() / ".opencode" / "bin" / "opencode")

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _minimal_child_env(
    *,
    root: Path,
    config_content: str,
    marker: Path,
) -> dict[str, str]:
    """只传递运行 CLI 所需的非凭据环境，避免继承用户 OpenCode 状态。"""

    path_value = os.environ.get("PATH", "")
    env = {
        "PATH": path_value,
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "TMPDIR": str(root / "tmp"),
        "OPENCODE_CONFIG_CONTENT": config_content,
        "OPENCODE_SMOKE_WEBHOOK_URL": "http://127.0.0.1:9/opencode-smoke",
        "OPENCODE_SMOKE_MARKER": str(marker),
        "NO_COLOR": "1",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    (root / "gitconfig").write_text("", encoding="utf-8")
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
    ):
        (root / Path(env[key]).relative_to(root)).mkdir(parents=True, exist_ok=True)
    return env


def _read_version(binary: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search((completed.stdout or "") + (completed.stderr or ""))
    return match.group(0) if match else None


def _wait_for_marker(
    process: subprocess.Popen[bytes],
    marker: Path,
    project: Path,
    port: int,
    deadline: float,
) -> bool:
    while time.monotonic() < deadline:
        if marker.is_file():
            try:
                if "server" in marker.read_text(encoding="utf-8"):
                    return True
            except OSError:
                pass
        # A directory-scoped no-model session list creates the instance and
        # initializes OpenCode's Plugin Service in the headless runtime.  The
        # request may remain open while the instance bootstraps, so it
        # intentionally has a short timeout.
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/session?directory={urllib.parse.quote(str(project), safe='')}",
                timeout=0.5,
            ):
                pass
        except Exception:
            pass
        if process.poll() is not None:
            return False
        time.sleep(0.1)
    try:
        return "server" in marker.read_text(encoding="utf-8")
    except OSError:
        return False


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    """只终止本夹具创建的进程组，并等待所有自有进程退出。"""

    if process.poll() is not None:
        return True

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False

    try:
        process.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            return False


def run_cli(timeout_seconds: float, explicit_binary: str | None) -> int:
    binary = _resolve_cli_binary(explicit_binary)
    if binary is None:
        _emit_version(None)
        _emit_result("FAIL", "resolve-binary")
        return FAIL

    version = _read_version(binary)
    _emit_version(version)
    if version is None:
        _emit_result("FAIL", "version")
        return FAIL

    process: subprocess.Popen[bytes] | None = None
    cleanup_ok = True
    result = FAIL

    with tempfile.TemporaryDirectory(prefix="whn-opencode-smoke-") as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        try:
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=project,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            _emit_result("FAIL", "isolated-project")
            return FAIL
        marker = root / "marker"
        token_file = root / "token"
        token_file.write_text("smoke-token\n", encoding="utf-8")

        real_plugin = (
            Path(__file__).resolve().parents[1]
            / "integrations"
            / "opencode"
            / "webhook-notifier.ts"
        )
        if not real_plugin.is_file():
            _emit_result("FAIL", "real-plugin")
            return FAIL

        wrapper = root / "wrapper.ts"
        wrapper.write_text(build_wrapper(real_plugin.as_uri()), encoding="utf-8")
        config_content = build_config_content(wrapper.as_uri(), token_file)
        env = _minimal_child_env(
            root=root,
            config_content=config_content,
            marker=marker,
        )

        try:
            port = _free_local_port()
            process = subprocess.Popen(
                [
                    str(binary),
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=project,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            _emit_result("FAIL", "spawn")
            return FAIL

        try:
            if _wait_for_marker(
                process, marker, project, port, time.monotonic() + timeout_seconds
            ):
                _emit_result("PASS", "plugin-server")
                result = PASS
            else:
                _emit_result("FAIL", "plugin-server-timeout")
        finally:
            cleanup_ok = _terminate_process_group(process)

    if cleanup_ok:
        _emit_result("PASS", "cleanup")
    else:
        _emit_result("FAIL", "cleanup")
        result = FAIL

    if result == PASS and cleanup_ok:
        print("PASS")
        return PASS
    print("FAIL")
    return FAIL


def _desktop_version() -> str | None:
    app_info = Path("/Applications/OpenCode.app/Contents/Info.plist")
    try:
        with app_info.open("rb") as handle:
            info = plistlib.load(handle)
        value = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
        return str(value) if value else None
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return None


def run_desktop() -> int:
    _emit_version(_desktop_version())
    _emit_result("SKIP", "desktop-manual-only")
    print("manual=new-isolated-profile;do-not-touch-existing-instance")
    return SKIP


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--cli", action="store_true", help="运行隔离 CLI smoke（默认）")
    modes.add_argument(
        "--desktop", action="store_true", help="只报告安全的 Desktop 人工验证 SKIP"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="等待 Plugin Service marker 的秒数",
    )
    parser.add_argument(
        "--opencode-bin",
        help="可选：指定 opencode CLI 可执行文件；不输出此路径",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.desktop:
        return run_desktop()
    return run_cli(args.timeout, args.opencode_bin)


if __name__ == "__main__":
    sys.exit(main())
