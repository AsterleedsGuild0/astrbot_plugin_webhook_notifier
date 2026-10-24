#!/usr/bin/env python3
"""macOS OMP 客户端包开发同步：把扁平 extension 包目录单向复制到 OMP 扩展目录。

不执行客户端，不操作 Git；仅依赖 Python 标准库。目标是独立副本，源所在磁盘未挂载时 OMP 仍可正常加载。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

LABEL = "io.github.asterleedsguild0.webhook-notifier.omp-hook-sync"
INTERVAL = 2.0
MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 64


class SyncError(Exception):
    """只携带固定错误码，避免回显文件正文或异常中的敏感内容。"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


def safe_path(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise SyncError("symlink_refused")


def read_file(path: Path) -> bytes | None:
    safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise SyncError("regular_file_required")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(data) > MAX_BYTES:
            raise SyncError("file_too_large")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise SyncError("file_changing")
        return data


def read_package(path: Path, *, source: bool) -> dict[str, bytes] | None:
    """读取扁平包目录快照；目录不存在返回 None。

    源只取运行文件：跳过隐藏条目、子目录与 ``*.test.ts``，与插件 ZIP 的打包规则一致。
    目标读取全部条目，任何额外文件或子目录都算独立修改。
    """
    safe_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        raise SyncError("directory_required") from None
    try:
        names = sorted(os.listdir(fd))
    finally:
        os.close(fd)
    files: dict[str, bytes] = {}
    total = 0
    for name in names:
        child = path / name
        if source and (name.startswith(".") or name.endswith(".test.ts") or (child.is_dir() and not child.is_symlink())):
            continue
        data = read_file(child)
        if data is None:
            raise SyncError("file_changing")
        total += len(data)
        files[name] = data
        if len(files) > MAX_FILES or total > MAX_BYTES:
            raise SyncError("package_too_large")
    return files


def package_digest(files: dict[str, bytes] | None) -> str | None:
    if files is None:
        return None
    value = hashlib.sha256()
    for name in sorted(files):
        data = files[name]
        value.update(f"{name}\0{len(data)}\0".encode())
        value.update(data)
    return value.hexdigest()


def write_files(directory: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def replace_package(path: Path, files: dict[str, bytes]) -> None:
    """在目标同级的隐藏暂存目录写好完整包，再以两次 rename 换入；OMP 自动发现不扫描隐藏条目。"""
    safe_path(path)
    staging = Path(tempfile.mkdtemp(prefix=".omp-sync-", dir=path.parent))
    retired = path.parent / f".omp-sync-old-{uuid.uuid4().hex}"
    try:
        write_files(staging, files)
        safe_path(path)
        if os.path.lexists(path):
            os.rename(path, retired)
        try:
            os.rename(staging, path)
        except OSError:
            if os.path.lexists(retired):
                os.rename(retired, path)
            raise
    finally:
        for leftover in (staging, retired):
            if os.path.lexists(leftover):
                shutil.rmtree(leftover)


def atomic_write(path: Path, data: bytes) -> None:
    safe_path(path)
    # .tmp 后缀不会被 OMP 的 *.ts 自动发现。
    fd, name = tempfile.mkstemp(prefix=".omp-sync-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path: Path) -> dict:
    data = read_file(path)
    if data is None:
        raise SyncError("not_installed")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise SyncError("invalid_state")
    return value


def save_json(path: Path, value: dict) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


@contextmanager
def lock(path: Path):
    safe_path(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("busy") from None
        yield
    finally:
        os.close(fd)


class Synchronizer:
    def __init__(self, root: Path):
        self.root = root
        config = load_json(root / "config.json")
        self.source = Path(config["source"])
        self.target = Path(config["target"])
        if not self.source.is_absolute() or not self.target.is_absolute():
            raise SyncError("invalid_config")
        self.candidate: str | None = None

    def state(self) -> dict:
        value = load_json(self.root / "state.json")
        if value.get("source") != str(self.source) or value.get("target") != str(self.target):
            raise SyncError("state_binding_mismatch")
        if not isinstance(value.get("conflict"), bool) or "deployed_hash" not in value:
            raise SyncError("invalid_state")
        return value

    def record(self, state: dict, status: str) -> str:
        if state.get("status") != status:
            state["status"] = status
            state["status_at"] = now()
            save_json(self.root / "state.json", state)
        return status

    def step(self, *, overwrite: bool = False) -> str:
        with lock(self.root / "sync.lock"):
            state = self.state()
            try:
                return self._step(state, overwrite=overwrite)
            except (OSError, SyncError) as exc:
                self.candidate = None
                code = str(exc) if isinstance(exc, SyncError) else "io_error"
                return self.record(state, code)

    def _step(self, state: dict, *, overwrite: bool) -> str:
        target = read_package(self.target, source=False)
        target_hash = package_digest(target)
        if target_hash != state["deployed_hash"] and not state["conflict"]:
            state["conflict"] = True
            # 持久化锁存；即便下一次源/目标恰好相同也不静默清除。
            save_json(self.root / "state.json", state)
        if state["conflict"] and not overwrite:
            self.candidate = None
            return self.record(state, "target_conflict")
        source = read_package(self.source, source=True)
        if source is None:
            self.candidate = None
            return self.record(state, "source_unavailable")
        if not source:
            self.candidate = None
            return self.record(state, "source_empty")
        if "package.json" not in source:
            self.candidate = None
            return self.record(state, "source_manifest_missing")
        source_hash = package_digest(source)
        if source_hash != self.candidate:
            self.candidate = source_hash
            return self.record(state, "settling")
        if not state["conflict"] and source_hash == target_hash:
            return self.record(state, "in_sync")
        if overwrite and target is not None:
            backup_dir = self.root / "backups"
            safe_path(backup_dir)
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            backup = backup_dir / f"{time.time_ns()}-{uuid.uuid4().hex}"
            backup.mkdir(mode=0o700)
            write_files(backup, target)
            state["last_backup"] = str(backup)
        # 尽量缩短检查与替换间隔；不是与外部编辑器之间的文件系统 CAS。
        if read_package(self.target, source=False) != target or read_package(self.source, source=True) != source:
            self.candidate = None
            return self.record(state, "file_changing")
        replace_package(self.target, source)
        if read_package(self.target, source=False) != source:
            state["conflict"] = True
            save_json(self.root / "state.json", state)
            return self.record(state, "target_conflict")
        state.update(deployed_hash=source_hash, conflict=False, last_sync_at=now())
        state["status"] = "synced"
        state["status_at"] = now()
        save_json(self.root / "state.json", state)
        return "synced"


class Manager:
    def __init__(self, instance: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", instance):
            raise SyncError("invalid_instance")
        self.root = Path.home() / "Library/Application Support/WebhookNotifier/omp-hook-sync" / instance
        self.label = LABEL if instance == "default" else f"{LABEL}.{instance}"
        self.plist = Path.home() / "Library/LaunchAgents" / f"{self.label}.plist"
        self.service = f"gui/{os.getuid()}/{self.label}"
        self.script = self.root / "sync_omp_hook.py"
        self.instance = instance

    def launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=20)

    def loaded(self) -> bool:
        result = self.launchctl("print", self.service)
        if result.returncode not in (0, 113):
            raise SyncError("launchctl_status_failed")
        return result.returncode == 0

    def expected_plist(self, python: str) -> dict:
        return {
            "Label": self.label,
            "ProgramArguments": [python, str(self.script), "--instance", self.instance, "watch"],
            "WorkingDirectory": str(self.root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "Disabled": True,
            "ProcessType": "Background",
        }

    def check_plist(self) -> None:
        data = read_file(self.plist)
        if data is None:
            raise SyncError("not_installed")
        config = load_json(self.root / "config.json")
        if plistlib.loads(data) != self.expected_plist(config["python"]):
            raise SyncError("unmanaged_launchagent")

    def install(self, source: Path, target: Path) -> dict:
        safe_path(self.root)
        safe_path(source)
        safe_path(target)
        if source == target or (source.exists() and target.exists() and os.path.samefile(source, target)):
            raise SyncError("source_equals_target")
        if source in target.parents or target in source.parents:
            raise SyncError("source_target_overlap")
        if target.name.startswith("."):
            raise SyncError("hidden_target_refused")
        if self.root in source.parents or self.root in target.parents:
            raise SyncError("runtime_path_overlap")
        package = read_package(source, source=True)
        if not package:
            raise SyncError("source_unavailable_or_empty")
        if "package.json" not in package:
            raise SyncError("source_manifest_missing")
        if not target.parent.is_dir():
            raise SyncError("target_parent_missing")
        if self.loaded():
            raise SyncError("stop_before_install")
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        with lock(self.root / "daemon.lock"), lock(self.root / "sync.lock"):
            old_config = read_file(self.root / "config.json")
            existing = read_package(target, source=False)
            if old_config is not None:
                config = json.loads(old_config)
                if config["source"] != str(source) or config["target"] != str(target):
                    raise SyncError("instance_binding_mismatch")
                Synchronizer(self.root).state()
            elif existing is not None and existing != package:
                raise SyncError("initial_target_conflict")
            if read_file(self.plist) is not None:
                self.check_plist()
            python = str(Path(sys.executable).resolve())
            if python.startswith("/Volumes/"):
                raise SyncError("python_must_be_on_system_disk")
            if old_config is None:
                save_json(self.root / "state.json", {
                    "source": str(source), "target": str(target),
                    "deployed_hash": package_digest(existing), "conflict": False,
                    "status": "installed", "status_at": now(),
                })
            save_json(self.root / "config.json", {
                "source": str(source), "target": str(target), "python": python,
            })
            atomic_write(self.script, Path(__file__).read_bytes())
            safe_path(self.plist.parent)
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.plist, plistlib.dumps(self.expected_plist(python)))
        return {"status": "installed_stopped", "runtime": str(self.root), "plist": str(self.plist)}

    def start(self) -> dict:
        self.check_plist()
        if not read_file(self.script):
            raise SyncError("runtime_missing")
        if self.launchctl("enable", self.service).returncode:
            raise SyncError("launchctl_enable_failed")
        if not self.loaded():
            if self.launchctl("bootstrap", f"gui/{os.getuid()}", str(self.plist)).returncode:
                raise SyncError("launchctl_bootstrap_failed")
        return {"status": "started", "loaded": self.loaded()}

    def stop(self) -> dict:
        self.check_plist()
        if self.launchctl("disable", self.service).returncode:
            raise SyncError("launchctl_disable_failed")
        if self.loaded() and self.launchctl("bootout", self.service).returncode:
            raise SyncError("launchctl_bootout_failed")
        return {"status": "stopped", "loaded": self.loaded()}

    def uninstall(self) -> dict:
        self.stop()
        with lock(self.root / "daemon.lock"), lock(self.root / "sync.lock"):
            self.plist.unlink()
            safe_path(self.script)
            self.script.unlink(missing_ok=True)
        return {"status": "uninstalled", "retained": "target, config, state, backups", "runtime": str(self.root)}

    def status(self) -> dict:
        state = Synchronizer(self.root).state()
        installed = read_file(self.plist) is not None and read_file(self.script) is not None
        if installed:
            self.check_plist()
        result = {"installed": installed, "loaded": self.loaded(), "runtime": str(self.root), **state}
        try:
            source = read_package(Path(state["source"]), source=True)
            target = read_package(Path(state["target"]), source=False)
            result.update(source_available=source is not None, contents_equal=source is not None and source == target,
                          target_changed=package_digest(target) != state["deployed_hash"])
        except (OSError, SyncError):
            result["path_status"] = "unavailable_or_unsafe"
        return result

    def sync(self, overwrite: bool = False) -> dict:
        engine = Synchronizer(self.root)
        first = engine.step(overwrite=overwrite)
        if first == "settling":
            time.sleep(INTERVAL)
            first = engine.step(overwrite=overwrite)
        return {"status": first}

    def watch(self) -> dict:
        quit_event = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: quit_event.set())
        signal.signal(signal.SIGINT, lambda *_: quit_event.set())
        with lock(self.root / "daemon.lock"):
            engine = Synchronizer(self.root)
            while not quit_event.is_set():
                try:
                    engine.step()
                except SyncError as exc:
                    if str(exc) != "busy":
                        raise
                quit_event.wait(INTERVAL)
        return {"status": "stopped"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OMP 客户端包单向目录同步与 macOS LaunchAgent 管理")
    parser.add_argument("--instance", default="default", help="隔离实例名；默认 default")
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="安装或更新；不自动启动，不覆盖不同的已有目标")
    install.add_argument("--source", required=True, help="含 package.json 的客户端包目录")
    install.add_argument("--target", required=True, help="OMP 扩展目录下的包目录，如 ~/.omp/agent/extensions/webhook-notifier")
    for command in ("start", "stop", "status", "uninstall", "watch"):
        sub.add_parser(command)
    sync = sub.add_parser("sync", help="立即同步；稳定观察间隔 2 秒")
    sync.add_argument("--overwrite-target", action="store_true", help="备份冲突目标后以源覆盖")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise SyncError("macos_required")
        manager = Manager(args.instance)
        if args.command == "install":
            result = manager.install(absolute(args.source), absolute(args.target))
        elif args.command == "sync":
            result = manager.sync(args.overwrite_target)
        else:
            result = getattr(manager, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "sync" and result["status"] not in {"synced", "in_sync"}:
            return 2
        return 0
    except (SyncError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, SyncError) else "operation_failed"
        print(json.dumps({"error": code}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
