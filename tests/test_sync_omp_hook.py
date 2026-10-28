from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync_omp_hook.py"
spec = importlib.util.spec_from_file_location("sync_omp_hook", SCRIPT)
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

MANIFEST = b'{"omp": {"extensions": ["./index.ts"]}}\n'


def put(directory: Path, data: bytes) -> None:
    (directory / "index.ts").write_bytes(data)


def entry(directory: Path) -> bytes | None:
    path = directory / "index.ts"
    return path.read_bytes() if path.exists() else None


def snapshot(directory: Path) -> dict[str, bytes] | None:
    if not directory.exists():
        return None
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path.resolve() / "state"
    root.mkdir()
    # 源包位于模拟外置卷下；重命名 volume 即模拟卸载。
    source = root.parent / "volume" / "omp"
    source.mkdir(parents=True)
    (source / "package.json").write_bytes(MANIFEST)
    put(source, b"export const version = 1;\n")
    (source / "index.test.ts").write_bytes(b"test only\n")
    (source / ".DS_Store").write_bytes(b"finder\n")
    (source / "node_modules").mkdir()
    target = root.parent / "extensions" / "webhook-notifier"
    target.parent.mkdir()
    sync.save_json(root / "config.json", {"source": str(source), "target": str(target)})
    sync.save_json(root / "state.json", {
        "source": str(source), "target": str(target), "deployed_hash": None,
        "conflict": False, "status": "installed",
    })
    return root, source, target


def deploy(deployment):
    root, source, target = deployment
    engine = sync.Synchronizer(root)
    assert engine.step() == "settling"
    assert not target.exists()
    assert engine.step() == "synced"
    assert entry(target) == entry(source)
    return engine


def test_stable_copy_ships_runtime_files_only_privately_and_noop_when_unchanged(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    assert sorted(path.name for path in target.iterdir()) == ["index.ts", "package.json"]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in target.iterdir())
    assert (source / "index.ts").stat().st_ino != (target / "index.ts").stat().st_ino
    before = target.stat()
    mtime = (target / "index.ts").stat().st_mtime_ns
    assert engine.step() == "in_sync"
    assert target.stat().st_ino == before.st_ino
    assert (target / "index.ts").stat().st_mtime_ns == mtime
    assert list(target.parent.iterdir()) == [target]


def test_new_module_and_removed_module_replace_whole_package(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    (source / "extra.ts").write_bytes(b"export {};\n")
    assert engine.step() == "settling"
    assert engine.step() == "synced"
    assert (target / "extra.ts").read_bytes() == b"export {};\n"
    (source / "extra.ts").unlink()
    assert engine.step() == "settling"
    assert engine.step() == "synced"
    assert sorted(path.name for path in target.iterdir()) == ["index.ts", "package.json"]
    assert list(target.parent.iterdir()) == [target]


def test_atomic_source_save_and_rapid_changes_wait_for_stability(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    initial = entry(target)
    temporary = source / ".index.ts.save"
    temporary.write_bytes(b"intermediate")
    temporary.replace(source / "index.ts")
    assert engine.step() == "settling"
    put(source, b"final")
    assert engine.step() == "settling"
    assert entry(target) == initial
    assert engine.step() == "synced"
    assert entry(target) == b"final"


def test_unmounted_source_volume_recovers_without_deleting_target(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    initial = snapshot(target)
    volume = source.parent
    detached = volume.with_name("detached")
    volume.rename(detached)
    assert engine.step() == "source_unavailable"
    assert snapshot(target) == initial
    put(detached / "omp", b"after remount")
    detached.rename(volume)
    assert engine.step() == "settling"
    assert engine.step() == "synced"
    assert entry(target) == b"after remount"


@pytest.mark.parametrize("change", ["edit", "add", "delete", "replace"])
def test_target_conflict_survives_restart_and_requires_explicit_resolution(deployment, change):
    engine = deploy(deployment)
    root, source, target = deployment
    if change == "delete":
        shutil.rmtree(target)
    elif change == "replace":
        other = target.with_name("manual")
        other.mkdir()
        (other / "index.ts").write_bytes(b"manual replacement")
        target.rename(target.with_name("old"))
        other.rename(target)
        shutil.rmtree(target.with_name("old"))
    elif change == "add":
        (target / "onebot.ts").write_bytes(b"second entry")
    else:
        put(target, b"manual edit")
    previous = snapshot(target)
    put(source, b"new source")
    assert engine.step() == "target_conflict"
    restarted = sync.Synchronizer(root)
    assert restarted.step() == "target_conflict"
    assert snapshot(target) == previous
    assert restarted.step(overwrite=True) == "settling"
    assert restarted.step(overwrite=True) == "synced"
    assert entry(target) == b"new source"
    assert sorted(path.name for path in target.iterdir()) == ["index.ts", "package.json"]
    backups = sorted((root / "backups").iterdir()) if (root / "backups").exists() else []
    assert [snapshot(path) for path in backups] == ([] if previous is None else [previous])
    assert not restarted.state()["conflict"]


def test_conflict_does_not_clear_when_contents_happen_to_match(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    put(target, b"manual")
    assert engine.step() == "target_conflict"
    put(source, b"manual")
    assert engine.step() == "target_conflict"
    assert engine.step(overwrite=True) == "settling"
    assert engine.step(overwrite=True) == "synced"


def test_conflict_resolution_never_backs_up_or_overwrites_unstable_source(deployment):
    engine = deploy(deployment)
    root, source, target = deployment
    put(target, b"manual")
    put(source, b"first pending source")
    assert engine.step(overwrite=True) == "settling"
    put(source, b"changing")
    assert engine.step(overwrite=True) == "settling"
    assert entry(target) == b"manual"
    assert not (root / "backups").exists()


@pytest.mark.parametrize("side", ["source", "target", "source_file", "target_parent"])
def test_symlinks_never_followed(deployment, side):
    engine = deploy(deployment)
    _, source, target = deployment
    chosen = {"source": source, "target": target, "source_file": source / "index.ts", "target_parent": target.parent}[side]
    moved = chosen.with_name(chosen.name + "-real")
    chosen.rename(moved)
    chosen.symlink_to(moved, target_is_directory=moved.is_dir())
    before = {path: path.read_bytes() for path in moved.rglob("*") if path.is_file()} if moved.is_dir() else moved.read_bytes()
    assert engine.step() == "symlink_refused"
    after = {path: path.read_bytes() for path in moved.rglob("*") if path.is_file()} if moved.is_dir() else moved.read_bytes()
    assert after == before


def test_empty_or_manifestless_source_preserves_previous_copy(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    initial = snapshot(target)
    (source / "package.json").unlink()
    assert engine.step() == "source_manifest_missing"
    (source / "index.ts").unlink()
    assert engine.step() == "source_empty"
    assert snapshot(target) == initial


def test_missing_target_parent_is_a_conflict_not_automatically_recreated(deployment):
    engine = deploy(deployment)
    _, _, target = deployment
    shutil.rmtree(target.parent)
    assert engine.step() == "target_conflict"
    assert not target.parent.exists()


def test_restart_after_target_write_before_state_save_fails_closed(deployment, monkeypatch):
    engine = deploy(deployment)
    root, source, target = deployment
    put(source, b"new source")
    assert engine.step() == "settling"
    original_save = sync.save_json

    def crash(path, value):
        if value.get("status") == "synced":
            raise RuntimeError("simulated crash")
        original_save(path, value)

    monkeypatch.setattr(sync, "save_json", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.step()
    monkeypatch.setattr(sync, "save_json", original_save)
    assert entry(target) == b"new source"
    assert sync.Synchronizer(root).step() == "target_conflict"


def test_target_modified_during_preparation_is_not_overwritten(deployment, monkeypatch):
    engine = deploy(deployment)
    _, source, target = deployment
    put(source, b"new source")
    assert engine.step() == "settling"
    original_read = sync.read_package
    calls = 0

    def edit_before_recheck(path, *, source):
        nonlocal calls
        if path == target:
            calls += 1
            if calls == 2:
                put(target, b"concurrent edit")
        return original_read(path, source=source)

    monkeypatch.setattr(sync, "read_package", edit_before_recheck)
    assert engine.step() == "file_changing"
    assert entry(target) == b"concurrent edit"
    assert engine.step() == "target_conflict"


def test_state_binding_change_fails_closed(deployment):
    root, _, target = deployment
    config = sync.load_json(root / "config.json")
    config["target"] = str(target.with_name("other"))
    sync.save_json(root / "config.json", config)
    with pytest.raises(sync.SyncError, match="state_binding_mismatch"):
        sync.Synchronizer(root).step()
    assert not target.exists()


def test_single_instance_lock_is_enforced_across_processes(deployment):
    root, _, _ = deployment
    code = """
import fcntl, sys
with open(sys.argv[1], 'a') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(7)
"""
    with sync.lock(root / "daemon.lock"):
        result = subprocess.run([sys.executable, "-c", code, str(root / "daemon.lock")])
        assert result.returncode == 7
    result = subprocess.run([sys.executable, "-c", code, str(root / "daemon.lock")])
    assert result.returncode == 0


def test_invalid_state_does_not_replace_target(deployment):
    engine = deploy(deployment)
    root, source, target = deployment
    initial = snapshot(target)
    (root / "state.json").write_text("broken json")
    put(source, b"changed")
    with pytest.raises(json.JSONDecodeError):
        engine.step()
    assert snapshot(target) == initial


@pytest.fixture
def manager(deployment, monkeypatch):
    root, _, _ = deployment
    monkeypatch.setattr(Path, "home", lambda: root.parent)
    monkeypatch.setattr(sync.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sync.Manager, "loaded", lambda self: False)
    return sync.Manager("test")


def test_install_refuses_existing_different_target(manager, deployment):
    _, source, target = deployment
    target.mkdir()
    put(target, b"local installation")
    with pytest.raises(sync.SyncError, match="initial_target_conflict"):
        manager.install(source, target)
    assert snapshot(target) == {"index.ts": b"local installation"}
    assert not manager.plist.exists()


def test_install_adopts_identical_copy_without_changing_it(manager, deployment):
    _, source, target = deployment
    target.mkdir()
    for name in ("package.json", "index.ts"):
        (target / name).write_bytes((source / name).read_bytes())
    before = target.stat()
    manager.install(source, target)
    engine = sync.Synchronizer(manager.root)
    assert engine.step() == "settling"
    assert engine.step() == "in_sync"
    assert target.stat().st_ino == before.st_ino


def test_reinstall_preserves_conflict_and_original_binding(manager, deployment):
    _, source, target = deployment
    manager.install(source, target)
    engine = sync.Synchronizer(manager.root)
    engine.step()
    assert engine.step() == "synced"
    put(target, b"local change")
    assert engine.step() == "target_conflict"
    manager.install(source, target)
    assert sync.Synchronizer(manager.root).step() == "target_conflict"
    other = target.with_name("other")
    with pytest.raises(sync.SyncError, match="instance_binding_mismatch"):
        manager.install(source, other)
    assert entry(target) == b"local change"
    assert not other.exists()


@pytest.mark.parametrize("relation", ["same", "inside_source", "hidden"])
def test_install_rejects_unsafe_targets(manager, deployment, relation):
    _, source, target = deployment
    chosen = {"same": source, "inside_source": source / "deployed", "hidden": target.with_name(".webhook-notifier")}[relation]
    with pytest.raises(sync.SyncError):
        manager.install(source, chosen)
    assert not manager.plist.exists()
    assert not (source / "deployed").exists()


def test_missing_source_refuses_install_without_launchagent(manager, deployment):
    _, source, target = deployment
    shutil.rmtree(source)
    with pytest.raises(sync.SyncError, match="source_unavailable_or_empty"):
        manager.install(source, target)
    assert not manager.plist.exists()
    assert not target.exists()


def test_unmanaged_launchagent_is_not_overwritten(manager, deployment):
    _, source, target = deployment
    manager.plist.parent.mkdir(parents=True)
    manager.plist.write_bytes(b"unmanaged file")
    with pytest.raises(sync.SyncError):
        manager.install(source, target)
    assert manager.plist.read_bytes() == b"unmanaged file"
    assert not target.exists()


def test_status_reports_conflict_without_failing_query(manager, deployment, monkeypatch, capsys):
    _, source, target = deployment
    manager.install(source, target)
    engine = sync.Synchronizer(manager.root)
    engine.step()
    engine.step()
    put(target, b"manual")
    engine.step()
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    assert sync.main(["--instance", "test", "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "target_conflict"
    assert report["target_changed"] is True
    assert report["contents_equal"] is False


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def set_version(source: Path, version: str) -> None:
    (source / "package.json").write_text(json.dumps({"version": version, "omp": {"extensions": ["./index.ts"]}}))


@pytest.fixture
def committing(deployment):
    """目标父目录是 Git 仓库（模拟 ~/.omp），仓库里另有一个已暂存的无关文件。"""
    root, source, target = deployment
    repo = target.parent
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "unrelated.yml").write_text("staged by the user\n")
    git(repo, "add", "unrelated.yml")
    config = sync.load_json(root / "config.json")
    sync.save_json(root / "config.json", {**config, "git_commit": True})
    set_version(source, "1.0.0")
    return root, source, target, repo


def sync_once(engine) -> str:
    assert engine.step() == "settling"
    return engine.step()


def test_version_change_commits_only_target_with_formatted_message(committing):
    root, source, target, repo = committing
    engine = sync.Synchronizer(root)
    assert sync_once(engine) == "synced"
    assert git(repo, "log", "--format=%s") == "config🔧: webhook-notifier install 1.0.0\n"

    put(source, b"export const version = 2;\n")
    set_version(source, "1.1.0")
    assert sync_once(engine) == "synced"
    assert git(repo, "log", "-1", "--format=%s") == "config🔧: webhook-notifier update 1.0.0 -> 1.1.0\n"
    assert sorted(git(repo, "show", "--name-only", "--format=", "HEAD").split()) == [
        "webhook-notifier/index.ts", "webhook-notifier/package.json",
    ]
    # 用户原本暂存的无关文件仍在暂存区，未被带入提交。
    assert git(repo, "diff", "--cached", "--name-only") == "unrelated.yml\n"
    assert sync.load_json(root / "state.json")["commit_status"] == "committed"


def test_sync_without_version_change_leaves_target_uncommitted(committing):
    root, source, target, repo = committing
    engine = sync.Synchronizer(root)
    sync_once(engine)
    put(source, b"export const version = 2;\n")
    assert sync_once(engine) == "synced"
    assert git(repo, "rev-list", "--count", "HEAD") == "1\n"
    assert "webhook-notifier/index.ts" in git(repo, "status", "--porcelain")


def test_commit_is_opt_in(committing):
    root, source, target, repo = committing
    config = sync.load_json(root / "config.json")
    sync.save_json(root / "config.json", {**config, "git_commit": False})
    assert sync_once(sync.Synchronizer(root)) == "synced"
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True).returncode != 0


def test_commit_failure_keeps_deployment(committing):
    root, source, target, repo = committing
    shutil.rmtree(repo / ".git")
    assert sync_once(sync.Synchronizer(root)) == "synced"
    assert entry(target) == entry(source)
    assert sync.load_json(root / "state.json")["commit_status"] == "commit_not_repository"
