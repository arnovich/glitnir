"""Operator handoff sessions publish only usable, destination-bound profiles."""

import copy
import json
import threading

import pytest

from glitnir.compute.client import Client, Settings
from glitnir.compute.handoff_worker import capture_preflight, serve_handoff


def test_preflight_then_registration_then_profile(handoff_session):
    run, root, events, ready = handoff_session
    run()
    assert events == ["capture", "register", "ready", "claim"]
    profile = ready[0]["profile"]
    assert profile == {
        "format": "taskvalet.compute-profile/v1",
        "kind": "glitnir.check/v1",
        "server": "https://example.test",
        "project": "lab",
        "binding": ready[0]["handoff"]["binding"],
    }
    assert ready[0]["proof_authority"] is False
    assert "secret" not in json.dumps(ready)
    assert not (root / "ready.json").exists()
    assert not (root / "compute-profile.json").exists()


def test_preflight_failure_has_no_registration_or_profile(handoff_session, monkeypatch):
    run, root, events, _ = handoff_session

    def fail(*args):
        raise ValueError("unreadable snapshot ancestor")

    monkeypatch.setattr("glitnir.compute.handoff_worker.capture_preflight", fail)
    with pytest.raises(ValueError, match="ancestor"):
        run()
    assert events == []
    assert not (root / "compute-profile.json").exists()


def test_registration_failure_has_no_ready_profile(handoff_session, monkeypatch):
    run, root, events, _ = handoff_session

    def fail(self):
        raise OSError("offline")

    monkeypatch.setattr("glitnir.compute.handoff_worker.Worker.register", fail)
    with pytest.raises(OSError, match="offline"):
        run()
    assert events == ["capture"]
    assert not (root / "ready.json").exists()


def test_recovery_preserves_deadline_and_changed_inputs_fail(handoff_session):
    run, root, events, ready = handoff_session
    run()
    deadline = ready[0]["deadline"]
    run()
    assert ready[1]["deadline"] == deadline
    before = events.copy()
    with pytest.raises(ValueError, match="inputs changed"):
        run(lifetime_seconds=601)
    assert events == before
    assert json.loads((root / "session.json").read_text())["deadline"] == deadline


def test_expired_session_does_not_prepare_or_register(handoff_session, monkeypatch):
    run, root, events, ready = handoff_session
    run()
    before = events.copy()
    monkeypatch.setattr(
        "glitnir.compute.handoff_worker.time.time", lambda: ready[0]["deadline"] + 1
    )
    with pytest.raises(ValueError, match="deadline"):
        run()
    assert events == before
    assert not (root / "compute-profile.json").exists()


def test_destination_mismatch_fails_before_preparation(stub_handoff, tmp_path):
    base, bundle, source, builds = stub_handoff
    with pytest.raises(ValueError, match="destination"):
        serve_handoff(
            Client(Settings("https://other.test", "lab", "reuse", "secret")),
            base,
            bundle,
            source,
            tmp_path / "session",
            candidate_path="Next.lean",
            timeout_seconds=60,
            memory_mb=4096,
            max_bytes=10000000,
            lifetime_seconds=600,
            stop=threading.Event(),
            on_ready=lambda value: None,
        )
    assert builds == []


def test_session_lock_prevents_parallel_workers(handoff_session):
    import fcntl

    run, root, events, _ = handoff_session
    root.mkdir(mode=0o700)
    with (root / "session.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="Another"):
            run()
    assert events == []


def test_profile_matches_taskvalet_wire_fixture(handoff_session):
    """Do not add paths, credentials or worker-only policy to participant input."""
    run, root, _, ready = handoff_session
    run()
    binding = ready[0]["handoff"]["binding"]
    assert set(binding) == {
        "environment",
        "checker_sha256",
        "context_sha256",
        "path",
        "timeout_seconds",
        "memory_mb",
    }
    assert len(binding["checker_sha256"]) == len(binding["context_sha256"]) == 64


def test_altered_bundle_rejected_before_worker(handoff_session, stub_handoff):
    run, _, events, _ = handoff_session
    bundle = copy.deepcopy(stub_handoff[1])
    bundle["origin"]["project"] = "other"
    with pytest.raises(ValueError):
        run(bundle=bundle)
    assert events == []


def test_deadline_cancels_an_active_worker(handoff_session, monkeypatch):
    """A blocked check observes cancellation even without returning to polling."""
    import time

    run, root, _, _ = handoff_session
    observed = []

    def work(self, stop):
        observed.append(stop.wait(3))
        return True

    monkeypatch.setattr("glitnir.compute.handoff_worker.Worker.run_once", work)
    start = time.monotonic()
    run(timeout_seconds=1, lifetime_seconds=1)
    assert observed == [True]
    assert time.monotonic() - start < 3
    assert not (root / "ready.json").exists()


def test_disk_budget_failure_cancels_worker(handoff_session, monkeypatch):
    run, root, _, _ = handoff_session

    def work(self, stop):
        (root / "large").write_bytes(b"x" * 10000001)
        assert stop.wait(3)
        return True

    monkeypatch.setattr("glitnir.compute.handoff_worker.Worker.run_once", work)
    with pytest.raises(ValueError, match="disk budget"):
        run()
    assert not (root / "ready.json").exists()


def test_runtime_cache_and_temp_are_private_and_restored(handoff_session, monkeypatch):
    import os
    import tempfile

    run, root, _, _ = handoff_session
    original = {key: os.environ.get(key) for key in ("HOME", "TMPDIR", "ELAN_HOME")}
    original_tmp = tempfile.tempdir

    def work(self, stop):
        assert os.environ["HOME"] == str(root / "home")
        assert tempfile.gettempdir() == str(root / "tmp")
        assert os.environ["ELAN_HOME"] == (
            original["ELAN_HOME"] or original["HOME"] + "/.elan"
        )
        stop.set()
        return True

    monkeypatch.setattr("glitnir.compute.handoff_worker.Worker.run_once", work)
    run()
    assert {key: os.environ.get(key) for key in original} == original
    assert tempfile.tempdir == original_tmp


def test_existing_worker_completion_is_retained(handoff_session):
    run, root, _, _ = handoff_session
    run()
    pending = root / "worker/original"
    pending.mkdir()
    (pending / "completion.json").write_text('{"original":"lease"}')
    run()
    assert (pending / "completion.json").read_text() == '{"original":"lease"}'


@pytest.mark.handoff_build
def test_live_handoff_worker_captures_before_serving(
    handoff_project, tmp_path, monkeypatch
):
    """Real bounded Lake and snapshot capture; transport is intentionally local."""
    from glitnir.compute.worker import Worker

    base, bundle, source = handoff_project
    ready = []
    monkeypatch.setattr(Worker, "register", lambda self: None)

    def work(self, stop):
        stop.set()
        return False

    monkeypatch.setattr(Worker, "run_once", work)
    serve_handoff(
        Client(Settings("https://example.test", "lab", "reuse", "secret")),
        base,
        bundle,
        source,
        tmp_path / "session",
        candidate_path="Example/Next.lean",
        timeout_seconds=180,
        memory_mb=4096,
        max_bytes=20 * 1024**3,
        lifetime_seconds=600,
        stop=threading.Event(),
        on_ready=ready.append,
    )
    assert len(ready) == 1
    assert ready[0]["handoff"]["preflight"]["import"] == "Example.Checked"
    assert ready[0]["profile"]["binding"]["path"] == "Example/Next.lean"
    assert not (tmp_path / "session/capture").exists()


@pytest.mark.parametrize("change", ["corrupt", "boolean", "future", "duplicate"])
def test_corrupt_session_deadline_cannot_reset_budget(handoff_session, change):
    run, root, events, _ = handoff_session
    run()
    state_path = root / "session.json"
    state = json.loads(state_path.read_text())
    if change == "corrupt":
        state_path.write_text("null")
    elif change == "duplicate":
        state_path.write_text('{"deadline":1,"deadline":2}')
    else:
        state["deadline"] = True if change == "boolean" else state["deadline"] + 600
        state_path.write_text(json.dumps(state))
    before = events.copy()
    with pytest.raises(ValueError):
        run()
    assert events == before


def test_symlink_session_root_is_refused(handoff_session, tmp_path):
    run, root, events, _ = handoff_session
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        run()
    assert events == []


def test_capture_preflight_uses_real_checker_snapshot_path(tmp_path, monkeypatch):
    """Snapshot, not metadata, must decide whether the OS user can read inputs."""
    from glitnir.compute import handoff_worker as module

    root = tmp_path / "session"
    source = root / "preparation/preflight/project/Example/Next.lean"
    inspection = {
        "status": "resolved",
        "setup": {"name": "Example.Next", "importArts": {"Example.Helper": []}},
        "toolchain_root": str(tmp_path / "toolchain"),
        "resolution": {"inputs": [{"path": "lakefile.toml", "sha256": "a" * 64}]},
    }
    seen = []
    monkeypatch.setattr(module, "inspect_project", lambda *args, **kwargs: inspection)

    def capture(destination, actual_source, setup, toolchain, project, stop, **kwargs):
        seen.append((destination, actual_source, project, kwargs))
        raise ValueError("descriptor denied")

    monkeypatch.setattr(module, "capture", capture)
    receipt = {
        "binding": {"path": "Example/Next.lean"},
        "source_sha256": "b" * 64,
        "preflight": {"module": "Example.Next", "import": "Example.Helper"},
    }
    with pytest.raises(ValueError, match="descriptor denied"):
        module.capture_preflight(receipt, root, threading.Event(), 10**10, 1000000)
    assert seen == [
        (
            root / "capture",
            source,
            source.parents[1],
            {"configuration": {"lakefile.toml": "a" * 64}},
        )
    ]


@pytest.mark.parametrize("dangling", [False, True])
def test_missing_journal_cannot_reset_existing_session(handoff_session, dangling):
    run, root, events, _ = handoff_session
    run()
    (root / "session.json").unlink()
    if dangling:
        (root / "session.json").symlink_to(root / "missing")
    before = events.copy()
    with pytest.raises(ValueError, match="Missing session journal"):
        run()
    assert events == before


def test_preparation_and_inspection_launch_without_monitor_thread(
    handoff_session, monkeypatch
):
    """Do not fork Python preexec callbacks after starting the watcher thread."""
    from glitnir.compute import handoff_worker as module

    run, _, _, _ = handoff_session
    prepare = module.prepare_handoff

    def verify_prepare(*args, **kwargs):
        assert not any(
            t.name == "glitnir-handoff-monitor" for t in threading.enumerate()
        )
        return prepare(*args, **kwargs)

    def verify_capture(*args):
        assert not any(
            t.name == "glitnir-handoff-monitor" for t in threading.enumerate()
        )

    monkeypatch.setattr(module, "prepare_handoff", verify_prepare)
    monkeypatch.setattr(module, "capture_preflight", verify_capture)
    run()


def test_original_environment_name_is_refused(handoff_session, stub_handoff):
    run, _, events, _ = handoff_session
    original = stub_handoff[1]["job"]["request"]["environment"]
    client = Client(Settings("https://example.test", "lab", original, "secret"))
    with pytest.raises(ValueError, match="distinct environment"):
        run(client=client)
    assert events == []


def test_capture_peak_budget_checked_before_discard(handoff_session, monkeypatch):
    """A successful but oversized temporary snapshot cannot publish readiness."""
    from glitnir.compute import handoff_worker as module

    run, root, events, _ = handoff_session
    monkeypatch.setattr(module, "capture_preflight", capture_preflight)
    monkeypatch.setattr(
        module,
        "inspect_project",
        lambda *args, **kwargs: {
            "status": "resolved",
            "setup": {"name": "Next", "importArts": {"Candidate": []}},
            "toolchain_root": str(root / "toolchain"),
            "resolution": {"inputs": []},
        },
    )

    def capture(destination, *args, **kwargs):
        destination.mkdir()
        (destination / "large").write_bytes(b"x" * 10000001)

    monkeypatch.setattr(module, "capture", capture)
    with pytest.raises(ValueError, match="capture.*disk budget"):
        run()
    assert events == []
    assert not (root / "capture").exists()
    assert not (root / "ready.json").exists()
