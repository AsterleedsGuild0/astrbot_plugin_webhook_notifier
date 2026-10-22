from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync_omp_hook.py"
spec = importlib.util.spec_from_file_location("sync_omp_hook", SCRIPT)
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path.resolve() / "state"
    root.mkdir()
    source = root.parent / "source" / "hook.ts"
    source.parent.mkdir()
    source.write_bytes(b"export const version = 1;\n")
    target = root.parent / "target" / "hook.ts"
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
    assert target.read_bytes() == source.read_bytes()
    return engine


def test_stable_copy_is_atomic_private_and_noop_when_unchanged(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    assert target.stat().st_mode & 0o777 == 0o600
    assert source.stat().st_ino != target.stat().st_ino
    before = target.stat()
    assert engine.step() == "in_sync"
    assert target.stat().st_ino == before.st_ino
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert list(target.parent.iterdir()) == [target]


def test_atomic_source_save_and_rapid_changes_wait_for_stability(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    initial = target.read_bytes()
    temporary = source.with_suffix(".save")
    temporary.write_bytes(b"intermediate")
    temporary.replace(source)
    assert engine.step() == "settling"
    source.write_bytes(b"final")
    assert engine.step() == "settling"
    assert target.read_bytes() == initial
    assert engine.step() == "synced"
    assert target.read_bytes() == b"final"


def test_missing_source_directory_recovers_without_deleting_target(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    initial = target.read_bytes()
    unavailable = source.parent.with_name("detached")
    source.parent.rename(unavailable)
    assert engine.step() == "source_unavailable"
    assert target.read_bytes() == initial
    (unavailable / source.name).write_bytes(b"after remount")
    unavailable.rename(source.parent)
    assert engine.step() == "settling"
    assert engine.step() == "synced"
    assert target.read_bytes() == b"after remount"


@pytest.mark.parametrize("change", ["edit", "delete", "replace"])
def test_target_conflict_survives_restart_and_requires_explicit_resolution(deployment, change):
    engine = deploy(deployment)
    root, source, target = deployment
    if change == "delete":
        target.unlink()
    elif change == "replace":
        other = target.with_suffix(".new")
        other.write_bytes(b"manual replacement")
        other.replace(target)
    else:
        target.write_bytes(b"manual edit")
    previous = target.read_bytes() if target.exists() else None
    source.write_bytes(b"new source")
    assert engine.step() == "target_conflict"
    restarted = sync.Synchronizer(root)
    assert restarted.step() == "target_conflict"
    assert (target.read_bytes() if target.exists() else None) == previous
    assert restarted.step(overwrite=True) == "settling"
    assert restarted.step(overwrite=True) == "synced"
    assert target.read_bytes() == b"new source"
    backups = list((root / "backups").glob("*.bak")) if (root / "backups").exists() else []
    assert [p.read_bytes() for p in backups] == ([] if previous is None else [previous])
    assert not restarted.state()["conflict"]


def test_conflict_does_not_clear_when_contents_happen_to_match(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    target.write_bytes(b"manual")
    assert engine.step() == "target_conflict"
    source.write_bytes(b"manual")
    assert engine.step() == "target_conflict"
    assert engine.step(overwrite=True) == "settling"
    assert engine.step(overwrite=True) == "synced"


def test_conflict_resolution_never_backs_up_or_overwrites_unstable_source(deployment):
    engine = deploy(deployment)
    root, source, target = deployment
    target.write_bytes(b"manual")
    source.write_bytes(b"first pending source")
    assert engine.step(overwrite=True) == "settling"
    source.write_bytes(b"changing")
    assert engine.step(overwrite=True) == "settling"
    assert target.read_bytes() == b"manual"
    assert not (root / "backups").exists()


@pytest.mark.parametrize("side", ["source", "target", "source_parent", "target_parent"])
def test_symlinks_never_followed(deployment, side):
    engine = deploy(deployment)
    _, source, target = deployment
    chosen = source if side.startswith("source") else target
    if side.endswith("parent"):
        moved = chosen.parent.with_name(chosen.parent.name + "-real")
        chosen.parent.rename(moved)
        chosen.parent.symlink_to(moved, target_is_directory=True)
    else:
        moved = chosen.with_suffix(".real")
        chosen.rename(moved)
        chosen.symlink_to(moved)
    original = moved.read_bytes() if moved.is_file() else (moved / chosen.name).read_bytes()
    assert engine.step() == "symlink_refused"
    assert (moved.read_bytes() if moved.is_file() else (moved / chosen.name).read_bytes()) == original


def test_empty_source_preserves_previous_copy(deployment):
    engine = deploy(deployment)
    _, source, target = deployment
    initial = target.read_bytes()
    source.write_bytes(b"")
    assert engine.step() == "source_empty"
    assert target.read_bytes() == initial


def test_missing_target_parent_is_a_conflict_not_automatically_recreated(deployment):
    engine = deploy(deployment)
    _, _, target = deployment
    target.unlink()
    target.parent.rmdir()
    assert engine.step() == "target_conflict"
    assert not target.parent.exists()


def test_restart_after_target_write_before_state_save_fails_closed(deployment, monkeypatch):
    engine = deploy(deployment)
    root, source, target = deployment
    source.write_bytes(b"new source")
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
    assert target.read_bytes() == b"new source"
    assert sync.Synchronizer(root).step() == "target_conflict"


def test_target_modified_during_preparation_is_not_overwritten(deployment, monkeypatch):
    engine = deploy(deployment)
    _, source, target = deployment
    source.write_bytes(b"new source")
    assert engine.step() == "settling"
    original_read = sync.read_file
    calls = 0

    def edit_before_recheck(path):
        nonlocal calls
        if path == target:
            calls += 1
            if calls == 2:
                target.write_bytes(b"concurrent edit")
        return original_read(path)

    monkeypatch.setattr(sync, "read_file", edit_before_recheck)
    assert engine.step() == "file_changing"
    assert target.read_bytes() == b"concurrent edit"
    assert engine.step() == "target_conflict"


def test_state_binding_change_fails_closed(deployment):
    root, _, target = deployment
    config = sync.load_json(root / "config.json")
    config["target"] = str(target.with_name("other.ts"))
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
    initial = target.read_bytes()
    (root / "state.json").write_text("broken json")
    source.write_bytes(b"changed")
    with pytest.raises(json.JSONDecodeError):
        engine.step()
    assert target.read_bytes() == initial


@pytest.fixture
def manager(deployment, monkeypatch):
    root, _, _ = deployment
    monkeypatch.setattr(Path, "home", lambda: root.parent)
    monkeypatch.setattr(sync.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sync.Manager, "loaded", lambda self: False)
    return sync.Manager("test")


def test_install_refuses_existing_different_target(manager, deployment):
    _, source, target = deployment
    target.write_bytes(b"local installation")
    with pytest.raises(sync.SyncError, match="initial_target_conflict"):
        manager.install(source, target)
    assert target.read_bytes() == b"local installation"
    assert not manager.plist.exists()


def test_install_adopts_identical_copy_without_changing_it(manager, deployment):
    _, source, target = deployment
    target.write_bytes(source.read_bytes())
    before = target.stat()
    manager.install(source, target)
    engine = sync.Synchronizer(manager.root)
    assert engine.step() == "settling"
    assert engine.step() == "in_sync"
    assert target.stat().st_ino == before.st_ino
    assert target.stat().st_mtime_ns == before.st_mtime_ns


def test_reinstall_preserves_conflict_and_original_binding(manager, deployment):
    _, source, target = deployment
    manager.install(source, target)
    engine = sync.Synchronizer(manager.root)
    engine.step()
    assert engine.step() == "synced"
    target.write_bytes(b"local change")
    assert engine.step() == "target_conflict"
    manager.install(source, target)
    assert sync.Synchronizer(manager.root).step() == "target_conflict"
    other = target.with_name("other.ts")
    with pytest.raises(sync.SyncError, match="instance_binding_mismatch"):
        manager.install(source, other)
    assert target.read_bytes() == b"local change"
    assert not other.exists()


def test_install_rejects_source_target_alias(manager, deployment):
    _, source, target = deployment
    target.hardlink_to(source)
    with pytest.raises(sync.SyncError, match="source_equals_target"):
        manager.install(source, target)
    assert not manager.plist.exists()


def test_missing_source_refuses_install_without_launchagent(manager, deployment):
    _, source, target = deployment
    source.unlink()
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
    target.write_bytes(b"manual")
    engine.step()
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    assert sync.main(["--instance", "test", "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "target_conflict"
    assert report["target_changed"] is True
    assert report["contents_equal"] is False
