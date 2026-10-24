"""Tests for owner-marked scratch directories and the stale-scratch sweep."""

import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from glitnir.lean import scratch
from glitnir.lean.scratch import MARKER, scratch_directory, sweep_stale_scratch

_OLD = time.time() - 7 * 3600


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Point the system temp directory at a private test directory."""
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _read_only_tree(root: Path) -> None:
    """Mimic a captured snapshot: read-only files inside read-only directories."""
    nested = root / "snapshot" / "lib"
    nested.mkdir(parents=True)
    (nested / "Mathlib.olean").write_bytes(b"x" * 64)
    (nested / "Mathlib.olean").chmod(0o400)
    (root / "snapshot" / "tool").write_bytes(b"#!")
    (root / "snapshot" / "tool").chmod(0o500)
    nested.chmod(0o500)
    (root / "snapshot").chmod(0o500)


def _dead_scratch(temp_root: Path, prefix: str) -> Path:
    """Create a scratch directory in a process that is then killed."""
    code = (
        "import sys, time\n"
        "from glitnir.lean.scratch import scratch_directory\n"
        f"with scratch_directory({prefix!r}) as path:\n"
        "    print(path, flush=True)\n"
        "    time.sleep(60)\n"
    )
    env = dict(os.environ, TMPDIR=str(temp_root))
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 30)
        assert ready, "the scratch owner did not start"
        path = Path(process.stdout.readline().strip())
    finally:
        process.send_signal(signal.SIGKILL)
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
    assert path.parent.resolve() == temp_root.resolve()
    return path


def test_scratch_directory_is_marked_and_removed_with_read_only_contents(temp_root):
    with scratch_directory("glitnir-check-") as path:
        assert isinstance(path, Path)
        assert path.parent == temp_root
        assert path.name.startswith("glitnir-check-")
        assert (path / MARKER).is_file()
        assert str(os.getpid()) in (path / MARKER).read_text()
        assert os.stat(path).st_mode & 0o777 == 0o700
        _read_only_tree(path)
    assert not path.exists()
    assert list(temp_root.iterdir()) == []


def test_scratch_directory_is_removed_after_an_error(temp_root):
    with pytest.raises(RuntimeError, match="boom"):
        with scratch_directory("glitnir-view-") as path:
            _read_only_tree(path)
            raise RuntimeError("boom")
    assert not path.exists()


def test_scratch_removal_does_not_follow_symlinks(temp_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    outside.chmod(0o500)
    try:
        with scratch_directory("glitnir-trace-") as path:
            (path / "link").symlink_to(outside, target_is_directory=True)
        assert not path.exists()
        assert (outside / "keep").read_text() == "keep"
        assert os.stat(outside).st_mode & 0o777 == 0o500
    finally:
        outside.chmod(0o700)


@pytest.mark.parametrize(
    "prefix", ["other-", "glitnir-check", "glitnir-../x-", "glitnir-Check-"]
)
def test_scratch_directory_refuses_prefixes_outside_the_namespace(temp_root, prefix):
    with pytest.raises(ValueError, match="prefix"):
        with scratch_directory(prefix):
            pass


def test_sweep_removes_a_killed_plugin_owners_directory(temp_root):
    path = _dead_scratch(temp_root, "glitnir-forseti-session-")
    sweep_stale_scratch()
    assert not path.exists()


def test_sweep_keeps_small_unmarked_directories_outside_the_legacy_rule(temp_root):
    kept = []
    for name in ("glitnir-forseti-z3-old", "glitnir-seccomp-old", "glitnir-x-old"):
        path = temp_root / name
        path.mkdir()
        os.utime(path, (_OLD, _OLD))
        kept.append(path)
    sweep_stale_scratch()
    assert all(path.is_dir() for path in kept)


def test_sweep_removes_a_killed_owners_snapshot(temp_root):
    path = _dead_scratch(temp_root, "glitnir-check-")
    assert (path / MARKER).is_file()
    _read_only_tree(path)
    sweep_stale_scratch()
    assert not path.exists()


def test_sweep_keeps_a_running_owners_directory(temp_root):
    with scratch_directory("glitnir-view-") as path:
        os.utime(path, (_OLD, _OLD))
        sweep_stale_scratch()
        assert (path / MARKER).is_file()
    assert not path.exists()


def test_sweep_keeps_unrelated_entries_and_the_slot_lock(temp_root):
    lock = temp_root / f"glitnir-project-check-{os.getuid()}.lock"
    lock.write_text("")
    unrelated = temp_root / "glitnir-other-abc"
    unrelated.mkdir()
    other = temp_root / "something-else"
    other.mkdir()
    named_file = temp_root / "glitnir-check-file"
    named_file.write_text("not a directory")
    for entry in (lock, unrelated, other, named_file):
        os.utime(entry, (_OLD, _OLD))
    sweep_stale_scratch()
    assert lock.is_file() and unrelated.is_dir() and other.is_dir()
    assert named_file.is_file()


def test_sweep_does_not_follow_a_planted_symlink(temp_root, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data").write_text("precious")
    (victim / MARKER).write_text("1\n")  # unlocked: would look dead
    os.utime(victim, (_OLD, _OLD))
    planted = temp_root / "glitnir-check-planted"
    planted.symlink_to(victim, target_is_directory=True)
    sweep_stale_scratch()
    assert planted.is_symlink()
    assert (victim / "data").read_text() == "precious"


def test_sweep_does_not_follow_a_planted_marker_symlink(temp_root, tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    path = temp_root / "glitnir-trace-planted"
    path.mkdir()
    (path / MARKER).symlink_to(target)
    os.utime(path, (_OLD, _OLD))
    sweep_stale_scratch()
    assert target.read_text() == "keep"
    assert (path / MARKER).is_symlink()


def test_sweep_removes_old_unmarked_legacy_leaks(temp_root):
    legacy = temp_root / "glitnir-view-legacy"
    legacy.mkdir()
    _read_only_tree(legacy)
    os.utime(legacy, (_OLD, _OLD))
    seccomp = temp_root / "glitnir-seccomp-legacy"
    seccomp.mkdir()
    (seccomp / "keyring.bpf").write_bytes(b"\0")
    os.utime(seccomp, (_OLD, _OLD))
    sweep_stale_scratch()
    assert not legacy.exists()
    # An old backend may still be using its unmarked seccomp filter.
    assert (seccomp / "keyring.bpf").is_file()


def test_removal_chmods_directories_without_following_symlinks(
    temp_root, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o500)
    chmods = []
    try:
        with scratch_directory("glitnir-check-") as path:
            _read_only_tree(path)
            (path / "snapshot-link").symlink_to(outside, target_is_directory=True)
            monkeypatch.setattr(
                scratch.os, "chmod", lambda *a, **k: chmods.append(a)  # by path
            )
        assert not path.exists()
        assert chmods == []
        assert os.stat(outside).st_mode & 0o777 == 0o500
    finally:
        monkeypatch.undo()
        outside.chmod(0o700)


def test_sweep_keeps_recent_unmarked_directories(temp_root):
    recent = temp_root / "glitnir-check-recent"
    recent.mkdir()
    (recent / "file").write_text("x")
    sweep_stale_scratch()
    assert (recent / "file").is_file()


def test_sweep_skips_directories_of_other_users(temp_root, monkeypatch):
    path = _dead_scratch(temp_root, "glitnir-trace-")
    monkeypatch.setattr(scratch.os, "getuid", lambda: os.geteuid() + 1)
    sweep_stale_scratch()
    assert path.exists()


def test_sweep_swallows_errors_per_directory(temp_root, monkeypatch):
    first = _dead_scratch(temp_root, "glitnir-check-")
    second = _dead_scratch(temp_root, "glitnir-view-")
    real = scratch._remove
    calls = []

    def flaky(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("disk trouble")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(scratch, "_remove", flaky)
    sweep_stale_scratch()
    assert len(calls) == 2
    assert first.exists() != second.exists()
    monkeypatch.setattr(scratch, "_remove", real)
    sweep_stale_scratch()  # the marker survived the failed attempt
    assert not first.exists() and not second.exists()


def test_sweep_never_raises_when_the_temp_directory_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "missing"))
    sweep_stale_scratch()


def test_persistent_scratch_is_marked_and_registered_for_exit(temp_root, monkeypatch):
    registered = []
    monkeypatch.setattr(
        scratch.atexit, "register", lambda *call: registered.append(call)
    )
    path = scratch.persistent_scratch_directory("glitnir-seccomp-")
    assert (path / MARKER).is_file()
    sweep_stale_scratch()
    assert path.exists()
    ((function, *arguments),) = registered
    function(*arguments)
    assert not path.exists()


def test_check_command_sweeps_before_checking(tmp_path, monkeypatch):
    from glitnir.service import cli

    events = []
    monkeypatch.setattr(cli, "sweep_stale_scratch", lambda: events.append("sweep"))
    monkeypatch.setattr(
        cli,
        "check_project",
        lambda *a, **k: events.append("check") or {"status": "failed"},
    )
    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := True.intro\n")
    cli.main(["check", str(source)])
    assert events == ["sweep", "check"]


def test_serve_sweeps_in_the_background(tmp_path, monkeypatch):
    import threading

    from glitnir.service import server as module

    swept = threading.Event()
    monkeypatch.setenv("GLITNIR_SERVER_TOKEN", "a" * 64)
    monkeypatch.setattr(module, "sweep_stale_scratch", swept.set)

    def construct(path):
        raise RuntimeError("construction reached")

    monkeypatch.setattr(module, "SourcePreviewService", construct)
    with pytest.raises(RuntimeError, match="construction reached"):
        module.serve(tmp_path / "A.lean", 0)
    assert swept.wait(5)
