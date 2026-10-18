"""Ordinary source preview never interprets project code or saved proof records."""

import hashlib
import os
import re
from pathlib import Path

import pytest

from glitnir.lean.project import describe_project, read_regular
from glitnir.notebook.source_preview import SourcePreviewService
from glitnir.providers import Providers


def test_nearest_project_and_loose_toolchain(tmp_path: Path) -> None:
    """A malformed nested project still owns its file, without parent fallback."""
    (tmp_path / "lakefile.toml").write_text('name = "outer"')
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.22.0\n")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "lakefile.toml").write_text("not valid [")
    result = describe_project(inner / "A.lean")
    assert result["root"] == str(inner)
    assert result["kind"] == "lake"
    assert result["files"]["lakefile.toml"]["error"]
    assert result["files"]["lean-toolchain"]["error"] == "missing"
    (inner / "lakefile.toml").unlink()
    assert describe_project(inner / "A.lean")["root"] == str(tmp_path)
    (tmp_path / "lakefile.toml").unlink()
    result = describe_project(inner / "A.lean")
    assert result["kind"] == "loose"
    assert result["toolchain"] == "leanprover/lean4:v4.22.0"


def test_nonregular_and_bounded_configuration(tmp_path: Path) -> None:
    """Symlinks, directories, FIFOs and oversized files are not followed/read."""
    config = tmp_path / "lakefile.lean"
    target = tmp_path / "target"
    target.write_text("dangerous code")
    config.symlink_to(target)
    assert describe_project(tmp_path / "A.lean")["files"][config.name]["error"]
    config.unlink()
    config.mkdir()
    assert describe_project(tmp_path / "A.lean")["files"][config.name]["error"]
    config.rmdir()
    os.mkfifo(config)
    assert describe_project(tmp_path / "A.lean")["files"][config.name]["error"]
    config.unlink()
    config.write_bytes(b"x" * (1024 * 1024 + 1))
    assert (
        "large" in describe_project(tmp_path / "A.lean")["files"][config.name]["error"]
    )
    with pytest.raises(ValueError, match="large"):
        read_regular(config, 12)


def test_open_descriptor_rejects_replaced_symlink(tmp_path: Path, monkeypatch) -> None:
    """The open itself refuses a configuration replaced after discovery."""
    config = tmp_path / "lakefile.lean"
    config.write_text("original")
    target = tmp_path / "target"
    target.write_text("replacement")
    original = os.open

    def replace(path, flags):
        config.unlink()
        config.symlink_to(target)
        return original(path, flags)

    monkeypatch.setattr(os, "open", replace)
    with pytest.raises(OSError):
        read_regular(config)


@pytest.mark.parametrize(
    "text",
    [
        "/-! # Prose -/\n\ntheorem t : True := by\n  trivial\n",
        "namespace N\n  theorem t : True := True.intro\nend N\n",
        'def broken := "unterminated\n',
        "",
    ],
)
def test_source_preserved_without_statuses(
    tmp_path: Path, text: str, monkeypatch
) -> None:
    """Fallback syntax remains readable, with no metadata or process access."""
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("source preview executed a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    path = tmp_path / "A.lean"
    path.write_bytes(text.replace("\n", "\r\n").encode())
    sidecar = path.with_suffix(".forseti.json")
    sidecar.write_text("malformed legacy metadata")
    before = path.read_bytes(), sidecar.read_bytes()
    snapshot = SourcePreviewService(path).snapshot()
    assert snapshot["preview"]["mode"] == "project"
    assert snapshot["claims"] == []
    assert snapshot["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert "".join(c["text"] for c in snapshot["cells"]) == text
    assert all(c["kind"] in ("source", "prose") for c in snapshot["cells"])
    assert (path.read_bytes(), sidecar.read_bytes()) == before


@pytest.mark.parametrize(
    "text, prose",
    [
        # Strings elsewhere in the file no longer turn its prose into source.
        (
            'def s : String := "time"\n\n/-!\n## Heading\n-/\n'
            "theorem t : True := by\n  trivial\n",
            ["## Heading"],
        ),
        ('/-! # Title -/\ndef p := ("a", "b")\n/-! More -/\n', ["# Title", "More"]),
        # A column-0 `/-!` inside a string is the string's text.
        ('def s : String := "\n/-! not prose -/\n"\n', []),
        # A string the scan cannot settle (it may be interpolated) confirms nothing.
        ('def s : String := s!"{1}"\n/-! unsure -/\n', []),
        # A block that closes early and is followed by code is not one comment.
        ("/-! a -/ def x := 1\n", []),
    ],
)
def test_prose_needs_lean_comment_boundaries(
    tmp_path: Path, text: str, prose: list[str]
) -> None:
    """Prose cells are kept where Lean's tokenizer reads the same comment."""
    path = tmp_path / "A.lean"
    path.write_text(text)
    cells = SourcePreviewService(path).snapshot()["cells"]
    assert "".join(c["text"] for c in cells) == text
    assert [c["markdown"] for c in cells if c["kind"] == "prose"] == prose


@pytest.mark.parametrize("separator", ["\u2028", "\r", "\v", "\x85"])
@pytest.mark.parametrize("proof", ["by\n  trivial", "True.intro"])
def test_only_lf_ends_source_lines(tmp_path: Path, separator: str, proof: str) -> None:
    """Prose and fallback ranges agree with Lean and editor navigation."""
    text = f"/-! hello{separator}world -/\ntheorem t : True := {proof}\n"
    path = tmp_path / "A.lean"
    path.write_bytes(text.encode())
    snapshot = SourcePreviewService(path).snapshot()
    lines = text.split("\n")[:-1]
    assert "".join(cell["text"] for cell in snapshot["cells"]) == text
    for cell in snapshot["cells"]:
        assert 1 <= cell["start"] <= cell["end"] <= len(lines)
        assert cell["text"] == "\n".join(lines[cell["start"] - 1 : cell["end"]]) + "\n"


@pytest.mark.parametrize("config", ["lakefile.toml", "lake-manifest.json"])
def test_deeply_nested_configuration_is_diagnostic(tmp_path: Path, config: str) -> None:
    """Malformed configuration cannot prevent a source preview from opening."""
    (tmp_path / "lakefile.lean").write_text("opaque configuration")
    (tmp_path / config).write_text(
        ("x = " if config.endswith(".toml") else "") + "[" * 5000 + "]" * 5000
    )
    result = describe_project(tmp_path / "A.lean")
    assert result["kind"] == "lake"
    assert result["files"][config]["error"]


def test_inspection_is_explicit_serialized_and_cancelled(tmp_path, monkeypatch):
    """Polling never launches inspection; close and concurrent requests are safe."""
    import threading

    from glitnir.notebook import source_preview
    from glitnir.notebook.document import NotebookError

    path = tmp_path / "A.lean"
    path.write_text("example : True := True.intro\n")
    service = SourcePreviewService(path)
    calls = []
    started, finish = threading.Event(), threading.Event()

    def inspect(source, *, expected_sha256, cancel, **options):
        # The check time limit bounds each Lake run: no shorter request bound.
        assert options == {}
        calls.append((source, expected_sha256, cancel.is_set()))
        started.set()
        assert finish.wait(5)
        return {
            "status": "unavailable",
            "message": "fixture",
            "setup": {"importArts": {"Big": ["x"]}},
            "resolution": {"kind": "prepared_local", "packages": [], "inputs": [1]},
        }

    monkeypatch.setattr(source_preview, "inspect_project", inspect)
    snapshot = service.snapshot()
    assert not calls and snapshot["preview"]["inspection"] is None
    state = service.inspect(snapshot["sha256"])
    assert state == {
        "status": "running",
        "source_sha256": snapshot["sha256"],
        "message": source_preview.INSPECTING,
    }
    assert started.wait(5)
    # Requests never wait for Lake; one inspection or check at a time.
    assert service.snapshot()["preview"]["inspection"]["status"] == "running"
    with pytest.raises(NotebookError, match="already running"):
        service.inspect(snapshot["sha256"])
    with pytest.raises(NotebookError, match="already running"):
        service.check(snapshot["sha256"])
    finish.set()
    service._inspection_worker.join(timeout=5)
    assert calls == [(path, snapshot["sha256"], False)]
    # The setup and the bytes Lake read stay out of every snapshot.
    assert service.snapshot()["preview"]["inspection"] == {
        "status": "unavailable",
        "source_sha256": snapshot["sha256"],
        "message": "fixture",
        "resolution": {"kind": "prepared_local", "packages": []},
    }
    service.close()
    with pytest.raises(NotebookError, match="closed"):
        service.inspect(snapshot["sha256"])


def test_a_failing_inspection_always_ends(tmp_path, monkeypatch):
    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("example : True := True.intro\n")
    service = SourcePreviewService(path)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(source_preview, "inspect_project", broken)
    sha = service.snapshot()["sha256"]
    service.inspect(sha)
    service._inspection_worker.join(timeout=5)
    found = service.snapshot()["preview"]["inspection"]
    assert found["status"] == "unavailable"
    assert found["message"] == "Project inspection failed: boom"
    service.inspect(sha)  # the gate is open again
    service.close()


def test_project_check_is_explicit_historical_and_cancelled(tmp_path, monkeypatch):
    """Checks run once on request, retain source binding, and stop on disposal."""
    import threading

    from glitnir.notebook import source_preview
    from glitnir.notebook.document import NotebookError

    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := True.intro\n")
    service = SourcePreviewService(path)
    started, finish = threading.Event(), threading.Event()
    calls = []

    def check(source, *, expected_sha256, cancel, progress, through_line, acquired):
        calls.append(source)
        assert through_line is None, "a check of the whole file by default"
        progress("Checking saved file")
        started.set()
        assert finish.wait(5)
        return {
            "status": "checked_snapshot",
            "source_sha256": expected_sha256,
            "theorems": [],
            "message": "Last checked snapshot",
        }

    monkeypatch.setattr(source_preview, "check_project", check)
    before = service.snapshot()
    assert not calls
    assert before["preview"]["check"] is None
    service.check(before["sha256"])
    assert started.wait(5)
    assert service.snapshot()["preview"]["check"]["status"] == "running"
    with pytest.raises(NotebookError, match="already running"):
        service.check(before["sha256"])
    finish.set()
    service._worker.join(5)
    path.write_text("theorem t : False := sorry\n")
    after = service.snapshot()
    assert after["preview"]["check"]["source_sha256"] == before["sha256"]
    assert after["sha256"] != before["sha256"]
    assert after["claims"] == []
    service.close()
    assert service._cancel.is_set()
    with pytest.raises(NotebookError, match="closed"):
        service.check(after["sha256"])


def test_a_check_saves_the_environment_it_compiled_in(tmp_path, monkeypatch):
    """The fingerprint is retaken once the check holds the Lean slot.

    The environment is A when the check is asked for, B while it compiles
    (it changed during the wait), then A again. Saved under A, the result
    would read as current although it was compiled in B.
    """
    from glitnir.notebook import source_preview
    from glitnir.notebook.check_records import load_records

    path = tmp_path / "A.lean"
    path.write_text("/-! # Title -/\ntheorem result : True := by\n  trivial\n")
    service = SourcePreviewService(path)
    environment = {"sha256": "A"}
    monkeypatch.setattr(
        source_preview, "environment_fingerprint", lambda *a: dict(environment)
    )

    def check(source, *, expected_sha256, cancel, progress, through_line, acquired):
        environment["sha256"] = "B"  # changed while the check waited
        acquired()
        result = _fake_result(expected_sha256)
        environment["sha256"] = "A"  # and back, after it compiled
        return result

    monkeypatch.setattr(source_preview, "check_project", check)
    sha = service.snapshot()["sha256"]
    service.check(sha)
    service._worker.join(5)
    saved = load_records(path)
    assert saved["environment"]["fingerprint"] == "B"
    [claim] = service.snapshot()["project_claims"]
    assert claim["current"] is False, "a result compiled in B is not current in A"
    service.close()


def test_stop_cancels_a_check_waiting_for_the_slot(tmp_path, monkeypatch):
    """A check waiting for another Lean run is stopped by Stop, not only close."""
    import threading

    from glitnir.lean.project_snapshot import SnapshotError, snapshot_slot
    from glitnir.notebook import source_preview

    monkeypatch.setattr(
        source_preview, "environment_fingerprint", lambda *a: {"sha256": "f"}
    )
    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := True.intro\n")
    service = SourcePreviewService(path)
    with pytest.raises(SnapshotError, match="No check is running"):
        service.cancel_check()
    sha = service.snapshot()["sha256"]
    held = snapshot_slot()
    held.__enter__()
    try:
        service.check(sha)
        for _ in range(100):
            if (
                "Waiting for another Lean run"
                in service.snapshot()["preview"]["check"]["message"]
            ):
                break
            threading.Event().wait(0.05)
        assert (
            "Waiting for another Lean run"
            in service.snapshot()["preview"]["check"]["message"]
        )
        service.cancel_check()
        service._worker.join(5)
        assert not service._worker.is_alive(), "stopped while the slot is still held"
    finally:
        held.__exit__(None, None, None)
    check = service.snapshot()["preview"]["check"]
    assert check["status"] == "unavailable"
    assert check["message"].endswith("cancelled.")
    assert not service._cancel.is_set(), "the preview stays open"
    service.check(sha)  # the gate is open again
    service.close()


def test_project_cli_reports_json_without_metadata(tmp_path, monkeypatch, capsys):
    """Agents can check ordinary files through the same snapshot service."""
    import json

    from glitnir.notebook import __main__ as cli

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := True.intro\n")
    calls = []
    result = {"status": "checked_snapshot", "theorems": [{"status": "checked"}]}
    monkeypatch.setattr(
        cli, "check_project", lambda path, **kw: calls.append((path, kw)) or result
    )
    assert cli.main(["check", str(source)]) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert (
        calls[0][1]["expected_sha256"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert not source.with_suffix(".forseti.json").exists()
    result["theorems"] = [{"status": "admitted"}]
    assert cli.main(["check", str(source)]) == 1
    source.unlink()
    assert cli.main(["check", str(source)]) == 2


def test_project_cli_flags_withheld_results(tmp_path, monkeypatch, capsys):
    """A partial check is not a complete success."""
    from glitnir.notebook import __main__ as cli

    path = tmp_path / "A.lean"
    path.write_text("theorem a : True := True.intro\n")
    monkeypatch.setattr(
        cli,
        "check_project",
        lambda *a, **k: {
            "status": "checked_snapshot",
            "theorems": [{"name": "a", "status": "checked"}],
            "withheld": {"from_line": 2, "message": "type mismatch"},
        },
    )
    assert cli.main(["check", str(path)]) == 1


def test_project_cli_has_no_write_flag(tmp_path):
    """Checking never writes, so there is no flag to ask it not to."""
    from glitnir.notebook import __main__ as cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["check", str(tmp_path / "A.lean"), "--no-write"])
    assert raised.value.code == 2


@pytest.mark.parametrize("failure", ["startup", "worker"])
def test_failed_project_worker_has_terminal_state(tmp_path, monkeypatch, failure):
    """Unexpected worker errors must not leave all controls disabled forever."""
    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := True.intro\n")
    service = SourcePreviewService(path)

    def fail(*args, **kwargs):
        raise RuntimeError("worker failed")

    if failure == "startup":
        monkeypatch.setattr(source_preview.threading.Thread, "start", fail)
    else:
        monkeypatch.setattr(source_preview, "check_project", fail)
    service.check(service.snapshot()["sha256"])
    if failure == "worker":
        service._worker.join(5)
    result = service.snapshot()["preview"]["check"]
    assert result["status"] == "unavailable"
    assert "worker failed" in result["message"]
    assert service._inspection_lock.acquire(blocking=False)
    service._inspection_lock.release()
    service.close()


def test_close_waits_for_worker_start(tmp_path, monkeypatch):
    """Closing a preview cannot join a worker before its start has completed."""
    import threading

    from glitnir.notebook import source_preview

    real_thread = threading.Thread
    starting, release, closing, closed = (threading.Event() for _ in range(4))
    errors = []

    class DelayedWorker:
        def __init__(self, **kwargs):
            self.started = False

        def start(self):
            starting.set()
            assert release.wait(5)
            self.started = True

        def join(self, **kwargs):
            if not self.started:
                raise RuntimeError("cannot join thread before it is started")

    service = SourcePreviewService(tmp_path / "A.lean")
    monkeypatch.setattr(source_preview.threading, "Thread", DelayedWorker)
    caller = real_thread(target=lambda: service.check("saved"))

    def close():
        closing.set()
        try:
            service.close()
        except RuntimeError as error:
            errors.append(error)
        finally:
            closed.set()

    closer = real_thread(target=close)
    caller.start()
    assert starting.wait(5)
    closer.start()
    assert closing.wait(5)
    try:
        assert not closed.wait(0.1)
    finally:
        release.set()
        caller.join(5)
        closer.join(5)
    assert not errors
    assert closed.is_set()


def _fake_result(sha):
    return {
        "status": "checked_snapshot",
        "source_sha256": sha,
        "environment_sha256": "e",
        "withheld": None,
        "message": "ok",
        "theorems": [
            {
                "name": "A.result",
                "status": "checked",
                "axioms": [],
                "start_line": 2,
                "end_line": 3,
                "proposition_sha256": "p",
            },
        ],
    }


def _check_and_wait(service, sha):
    service.check(sha)
    service._worker.join(timeout=10)


def test_check_saves_records_and_shows_claim_cells(tmp_path, monkeypatch):
    """After a check, the claim's lines become a claim cell with a current result."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text(
        "/-! # Title -/\ntheorem result : True := by\n  trivial\n-- after\n"
    )
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(sha)
    )
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert (tmp_path / "A.glitnir.json").is_file()
    assert not (tmp_path / "A.forseti.json").exists()
    [claim] = snapshot["project_claims"]
    assert claim["name"] == "A.result" and claim["current"] is True
    claim_cells = [c for c in snapshot["cells"] if c.get("claim")]
    assert [(c["start"], c["end"], c["claim"]) for c in claim_cells] == [
        (2, 3, "A.result")
    ]
    assert "".join(c["text"] for c in snapshot["cells"]) == path.read_text()


def test_checked_examples_show_their_statement_and_non_claims(tmp_path, monkeypatch):
    """An example claim carries its checked statement; others are marked."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("example : True = True := rfl\nexample : Nat := 3\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _fake_result(sha)
    result["theorems"] = [
        {
            **result["theorems"][0],
            "name": "_private.A.0._glitnir_example_0",
            "example": True,
            "type": "Eq True True",
            "statement": "True = True",
            "start_line": 1,
            "end_line": 1,
        }
    ]
    result["non_claims"] = [{"start_line": 2, "end_line": 2}]
    monkeypatch.setattr(source_preview, "check_project", lambda *a, **k: result)
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        snapshot = service.snapshot()
    finally:
        service.close()
    [claim] = snapshot["project_claims"]
    assert (claim["example"], claim["statement"], claim["start_line"]) == (
        True,
        "True = True",
        1,
    )
    assert claim["kernel_statement"] == "Eq True True"
    assert snapshot["project_non_claims"] == [{"start_line": 2, "end_line": 2}]


def test_preview_survives_a_corrupt_sidecar(tmp_path):
    """A broken .glitnir.json shows no claims and does not fail the preview."""
    from glitnir.notebook.source_preview import SourcePreviewService

    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := True.intro\n")
    (tmp_path / "A.glitnir.json").write_text("{broken")
    service = SourcePreviewService(path)
    try:
        snapshot = service.snapshot()
    finally:
        service.close()
    assert snapshot["project_claims"] == []
    assert snapshot["project_withheld"] is None


def test_edit_after_check_marks_the_claim_stale(tmp_path, monkeypatch):
    """Changing Lean above a saved claim keeps its card but marks it out of date."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(sha)
    )
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        path.write_text("def x : Nat := 2\ntheorem result : True := by\n  trivial\n")
        [claim] = service.snapshot()["project_claims"]
    finally:
        service.close()
    assert claim["current"] is False


def test_snapshot_skips_the_fingerprint_without_a_sidecar(tmp_path, monkeypatch):
    """Previews of unchecked files never pay for environment fingerprinting."""
    from glitnir.notebook import source_preview

    def boom(*args, **kwargs):
        raise AssertionError("fingerprint computed without records")

    monkeypatch.setattr(source_preview, "environment_fingerprint", boom)
    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := True.intro\n")
    service = source_preview.SourcePreviewService(path)
    try:
        snapshot = service.snapshot()
    finally:
        service.close()
    assert snapshot["project_claims"] == []


def test_snapshot_survives_a_fingerprint_failure(tmp_path, monkeypatch):
    """An unexpected error while judging saved claims yields no claims."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(sha)
    )
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)

        def boom(*args, **kwargs):
            raise RuntimeError("git exploded")

        monkeypatch.setattr(source_preview, "environment_fingerprint", boom)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert snapshot["project_claims"] == []
    assert snapshot["project_withheld"] is None


def test_failed_save_keeps_the_check_result(tmp_path, monkeypatch):
    """A read-only directory must not turn a successful check into unavailable."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(sha)
    )

    def deny(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(source_preview, "save_records", deny)
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        check = service.snapshot()["preview"]["check"]
    finally:
        service.close()
    assert check["status"] == "checked_snapshot"
    assert "Results could not be saved: read-only" in check["message"]


def test_a_check_too_large_to_save_with_saved_evidence_keeps_everything(
    tmp_path, monkeypatch
):
    """The records file is left as it was, and the check says why."""
    import hashlib

    from glitnir.notebook import check_records, source_preview
    from glitnir.notebook.check_records import (
        RESULTS_TOO_LARGE,
        _serialised,
        records_from_check,
        records_path,
        save_records,
        with_evidence,
    )

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    entry = {
        "prefix_sha256": "p",
        "fingerprint": "f",
        "provider": "demo",
        "created": "2026-10-14T00:00:00Z",
        "snapshot": {
            "format": "glitnir.evidence/v1",
            "series": {"times": [0.0], "values": [[1.0]], "names": ["x"]},
            "finite": True,
            "provenance": [],
        },
    }
    save_records(path, with_evidence(None, "k", entry, ["k"]))
    before = records_path(path).read_bytes()
    # Room for the claims (the fingerprint is a SHA-256), not for the evidence.
    claims = records_from_check(path.read_text(), _fake_result(sha), "0" * 64)
    room = len(_serialised(claims).encode()) + 10
    monkeypatch.setattr(check_records, "_MAX_RECORDS", room)
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(sha)
    )
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert snapshot["preview"]["check"]["message"] == (
        f"ok Results could not be saved: {RESULTS_TOO_LARGE}."
    )
    assert records_path(path).read_bytes() == before


def test_changed_file_after_check_saves_nothing(tmp_path, monkeypatch):
    """Records are written only when the file still matches the requested hash."""
    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    requested = "0" * 64
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(requested)
    )
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, requested)
    finally:
        service.close()
    assert not (tmp_path / "A.glitnir.json").exists()


def _saved_then(tmp_path, monkeypatch, later):
    """Save a checked claim, then run a second check returning ``later``."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(sha)
    )
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        [saved] = service.snapshot()["project_claims"]
        monkeypatch.setattr(source_preview, "check_project", lambda *a, **k: later)
        _check_and_wait(service, sha)
        [claim] = service.snapshot()["project_claims"]
    finally:
        service.close()
    return saved, claim


def test_unavailable_recheck_marks_saved_claims_out_of_date(tmp_path, monkeypatch):
    """A fresh check that could not run must not leave cards shown as current."""
    later = {"status": "unavailable", "message": "build failed", "theorems": []}
    saved, claim = _saved_then(tmp_path, monkeypatch, later)
    assert saved["current"] is True
    assert claim["current"] is False


def test_cancelled_recheck_keeps_saved_claims_current(tmp_path, monkeypatch):
    later = {
        "status": "unavailable",
        "message": "Project check cancelled.",
        "theorems": [],
    }
    _, claim = _saved_then(tmp_path, monkeypatch, later)
    assert claim["current"] is True


def test_busy_recheck_keeps_saved_claims_current(tmp_path, monkeypatch):
    """A check refused because another Lean run holds the slot says nothing."""
    from glitnir.lean.project_snapshot import SLOT_BUSY

    assert SLOT_BUSY == "Another Lean run is in progress; retry when it finishes."
    later = {"status": "unavailable", "message": SLOT_BUSY, "theorems": []}
    _, claim = _saved_then(tmp_path, monkeypatch, later)
    assert claim["current"] is True


def test_busy_slot_message_is_generic(tmp_path, monkeypatch):
    """The slot is shared by checks, views and evidence runs."""
    import fcntl
    import os

    from glitnir.lean import project_snapshot

    monkeypatch.setattr(project_snapshot.tempfile, "gettempdir", lambda: tmp_path)
    lock = tmp_path / f"glitnir-project-check-{os.getuid()}.lock"
    lock.touch(mode=0o600)
    with open(lock, "rb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        with pytest.raises(project_snapshot.SnapshotError) as raised:
            with project_snapshot.snapshot_slot():
                pass
    assert str(raised.value) == project_snapshot.SLOT_BUSY


def _withheld_result(sha):
    return {**_fake_result(sha), "withheld": {"from_line": 4, "message": "bad"}}


def test_withheld_from_an_older_file_is_marked_stale(tmp_path, monkeypatch):
    """A stop line recorded for other text still shows, flagged as from then."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _withheld_result(sha)
    )
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        fresh = service.snapshot()["project_withheld"]
        path.write_text(path.read_text() + "-- later\n")
        later = service.snapshot()["project_withheld"]
    finally:
        service.close()
    assert fresh == {"from_line": 4, "message": "bad"}
    assert later == {"from_line": 4, "message": "bad", "stale": True}


def test_project_snapshot_lists_sibling_notebooks(tmp_path):
    """Prose links open sibling notebooks; odd names and links are skipped."""
    import os

    from glitnir.notebook.source_preview import SourcePreviewService

    path = tmp_path / "Main.lean"
    path.write_text("theorem t : True := True.intro\n")
    (tmp_path / "Basics.lean").write_text("def x := 1\n")
    (tmp_path / "My Notes.lean").write_text("")
    (tmp_path / "1x.lean").write_text("")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Far.lean").write_text("")
    os.symlink(outside / "Far.lean", tmp_path / "Far.lean")
    service = SourcePreviewService(path)
    try:
        references = service.snapshot()["references"]
    finally:
        service.close()
    assert references == [
        {"module": "Basics", "file": "Basics.lean"},
        {"module": "Main", "file": "Main.lean"},
    ]


def test_unbuilt_sibling_import_is_explained(tmp_path, monkeypatch):

    path = tmp_path / "Main.lean"
    path.write_text("import Basics\nimport Mathlib\n\ntheorem t : True := True.intro\n")
    (tmp_path / "Basics.lean").write_text("def x := 1\n")
    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.0.0\n")
    message = _unavailable_message(tmp_path, path, monkeypatch, "setup failed")
    assert message.startswith("setup failed ")
    assert "If Basics are modules of this project" in message
    assert "`lake build Basics`" in message
    assert "Mathlib" not in message


def _unavailable_message(tmp_path, path, monkeypatch, text):
    from glitnir.notebook import source_preview

    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        source_preview,
        "check_project",
        lambda *a, **k: {"status": "unavailable", "message": text, "theorems": []},
    )
    service = source_preview.SourcePreviewService(path)
    try:
        service.check(sha)
        service._worker.join(timeout=10)
        return service.snapshot()["preview"]["check"]["message"]
    finally:
        service.close()


def test_loose_file_import_hint_asks_for_a_lake_project(tmp_path, monkeypatch):
    path = tmp_path / "Main.lean"
    path.write_text("import Basics\n\ntheorem t : True := True.intro\n")
    (tmp_path / "Basics.lean").write_text("def x := 1\n")
    message = _unavailable_message(tmp_path, path, monkeypatch, "")
    assert message == "Imports of other notebooks need a Lake project; see the README."
    assert "lake build" not in message


def test_no_hint_when_check_was_cut_short(tmp_path, monkeypatch):
    path = tmp_path / "Main.lean"
    path.write_text("import Basics\n\ntheorem t : True := True.intro\n")
    (tmp_path / "Basics.lean").write_text("def x := 1\n")
    from glitnir.lean.project_snapshot import SLOT_BUSY

    for text in (
        "Check cancelled.",
        "Saved source changed; refresh and retry.",
        SLOT_BUSY,
    ):
        assert _unavailable_message(tmp_path, path, monkeypatch, text) == text


def test_built_or_library_imports_get_no_hint(tmp_path):
    from glitnir.notebook.source_preview import unbuilt_sibling_imports

    folder = Path("/nonexistent-folder")
    assert (
        unbuilt_sibling_imports(folder / "Main.lean", {"Mathlib": None, "Init": None})
        == []
    )
    (tmp_path / "Basics.lean").write_text("def x := 1\n")
    assert unbuilt_sibling_imports(tmp_path / "Main.lean", {"Basics": "abc"}) == []


def test_hint_names_only_plain_sibling_notebooks(tmp_path):
    from glitnir.notebook.source_preview import unbuilt_sibling_imports

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Foo.lean").write_text("def x := 1\n")
    (tmp_path / "A;rm.lean").write_text("def x := 1\n")
    (tmp_path / "Real.lean").write_text("def x := 1\n")
    (tmp_path / "Link.lean").symlink_to(tmp_path / "Real.lean")
    imports = {"/abs": None, "sub/Foo": None, "A;rm": None, "Link": None}
    assert unbuilt_sibling_imports(tmp_path / "Main.lean", imports) == []
    assert unbuilt_sibling_imports(tmp_path / "Main.lean", {"Real": None}) == ["Real"]


VIEW_TEXT = (
    "import Demo.View\n\ndef c := 1\n-- @view circuit c using a, b\ndef d := 2\n"
)


def _demo_provider():
    from glitnir.providers import ViewProvider

    return ViewProvider(
        name="demo",
        kinds=frozenset({"circuit"}),
        imports=(),
        preamble="",
        marker="m:",
        command=lambda kind, target, using: "",
    )


def _drawn(service, cell: int, line: int) -> dict:
    """Ask for a view until its background drawing has ended; return the answer."""
    answer = service.view(cell, line)
    while answer.get("status") in ("drawing", "queued"):
        mine = answer["status"] == "drawing"
        service._view_worker.join(timeout=30)
        assert not service._view_worker.is_alive()
        # A queued view asks again once the drawing before it has ended.
        answer = service.view_job(answer["job"]) if mine else service.view(cell, line)
    return answer


def _view_service(tmp_path: Path, monkeypatch, text=VIEW_TEXT, errors=None):
    from glitnir.notebook import source_preview

    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({"circuit": _demo_provider()}, {}, {}, errors or {}),
    )
    path = tmp_path / "A.lean"
    path.write_text(text)
    return SourcePreviewService(path)


def test_view_directives_are_listed_without_processes(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("snapshot executed a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    service = _view_service(tmp_path, monkeypatch)
    snapshot = service.snapshot()
    (entry,) = snapshot["views"]
    cell = snapshot["cells"][entry["cell"]]
    assert cell["start"] <= 4 <= cell["end"]
    assert entry == {
        "cell": entry["cell"],
        "line": 4,
        "kind": "circuit",
        "name": "c",
        "using": ["a", "b"],
        "error": "",
    }


def test_no_directives_load_no_providers(tmp_path: Path, monkeypatch) -> None:
    from glitnir.notebook import source_preview

    def forbidden():
        pytest.fail("providers were loaded")

    monkeypatch.setattr(source_preview, "load_providers", forbidden)
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n")
    snapshot = SourcePreviewService(path).snapshot()
    assert snapshot["views"] == [] and snapshot["evidence"] == []
    assert snapshot["directive_providers"] == {"evidence": [], "decide": []}
    assert snapshot["decisions"] == [] and "simulations" not in snapshot


def test_unknown_view_kind_names_failed_providers(tmp_path: Path, monkeypatch) -> None:
    text = "-- @view graph g\n-- @view circuit c using " + ",".join("abcdefghijklmnopq")
    service = _view_service(tmp_path, monkeypatch, text, {"broken": "ImportError: x"})
    graph, circuit = service.snapshot()["views"]
    assert graph["error"] == (
        "No installed provider draws `graph` views."
        " (Provider broken failed to load: ImportError: x)"
    )
    assert circuit["error"] == "Too many `using` names (at most 16)."


def test_view_renders_once_per_source(tmp_path: Path, monkeypatch) -> None:
    from glitnir.notebook import source_preview

    calls = []

    def render(path, **options):
        calls.append(options)
        return {"svg": "<svg/>"}

    service = _view_service(tmp_path, monkeypatch)
    monkeypatch.setattr(source_preview, "render_project_view", render)
    cell = service.snapshot()["views"][0]["cell"]
    assert _drawn(service, cell, 4) == {"svg": "<svg/>"}
    assert service.view(cell, 4) == {"svg": "<svg/>"}
    assert len(calls) == 1
    assert calls[0]["using"] == ("a", "b") and calls[0]["line"] == 4
    assert calls[0]["expected_sha256"] == service.snapshot()["sha256"]
    with pytest.raises(KeyError):
        service.view(cell, 99)


def test_view_error_entries_do_not_render(tmp_path: Path, monkeypatch) -> None:
    from glitnir.notebook import source_preview

    def render(*args, **options):
        pytest.fail("rendered a view with an error")

    service = _view_service(tmp_path, monkeypatch, "-- @view graph g\n")
    monkeypatch.setattr(source_preview, "render_project_view", render)
    assert "graph" in service.view(0, 1)["error"]


def test_view_cache_key_includes_the_environment_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    from glitnir.notebook import source_preview

    calls = []
    monkeypatch.setattr(
        source_preview,
        "render_project_view",
        lambda path, **options: calls.append(options) or {"svg": "<svg/>"},
    )
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    fingerprint = {"sha256": "one"}
    monkeypatch.setattr(
        source_preview, "environment_fingerprint", lambda *a, **k: fingerprint
    )
    _drawn(service, cell, 4)
    _drawn(service, cell, 4)
    assert len(calls) == 1
    fingerprint["sha256"] = "two"
    _drawn(service, cell, 4)
    assert len(calls) == 2


def test_a_view_request_never_waits_for_lean(tmp_path: Path, monkeypatch) -> None:
    """Drawing runs in a worker; its job is polled until it ends."""
    import threading

    from glitnir.notebook import source_preview

    started, finish = threading.Event(), threading.Event()
    calls = []

    def render(path, *, progress, cancel, **options):
        calls.append(options["line"])
        progress("Waiting for another Lean run to finish.")
        started.set()
        assert finish.wait(10)
        return {"svg": f"<svg>{options['line']}</svg>"}

    monkeypatch.setattr(source_preview, "render_project_view", render)
    text = "def c := 1\n\n-- @view circuit c\n-- @view circuit c\n"
    service = _view_service(tmp_path, monkeypatch, text)
    cell = service.snapshot()["views"][0]["cell"]
    first = service.view(cell, 3)
    job = first["job"]
    assert first == {
        "status": "drawing",
        "message": source_preview.VIEW_STARTING,
        "job": job,
    }
    assert started.wait(10)
    waiting = {
        "status": "drawing",
        "message": "Waiting for another Lean run to finish.",
        "job": job,
    }
    assert service.view(cell, 3) == waiting
    assert service.view_job(job) == waiting
    # One view at a time: another waits, polling the job being drawn.
    assert service.view(cell, 4) == {
        "status": "queued",
        "message": source_preview.VIEW_QUEUED,
        "job": job,
    }
    finish.set()
    service._view_worker.join(timeout=10)
    assert service.view_job(job) == {"svg": "<svg>3</svg>"}
    assert service.view_job(job) == {"svg": "<svg>3</svg>"}, "as often as asked"
    assert service.view(cell, 3) == {"svg": "<svg>3</svg>"}
    assert _drawn(service, cell, 4) == {"svg": "<svg>4</svg>"}
    assert calls == [3, 4]
    assert service.view_job("0" * 32) == {
        "error": source_preview.UNKNOWN_JOB,
        "transient": True,
    }
    service.close()


def test_polling_a_job_reads_no_notebook(tmp_path: Path, monkeypatch) -> None:
    """A poll is cheap: no saved file, snapshot or fingerprint."""
    from glitnir.notebook import source_preview

    monkeypatch.setattr(
        source_preview, "render_project_view", lambda path, **o: {"svg": "<svg/>"}
    )
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    job = service.view(cell, 4)["job"]
    service._view_worker.join(timeout=10)

    def forbidden(*args, **kwargs):
        pytest.fail("a poll read the notebook")

    monkeypatch.setattr(service, "_text", forbidden)
    monkeypatch.setattr(source_preview, "environment_fingerprint", forbidden)
    assert service.view_job(job) == {"svg": "<svg/>"}


def test_an_unknown_environment_draws_once_and_says_so(
    tmp_path: Path, monkeypatch
) -> None:
    """A random unknown-state value must not make every poll a new drawing."""
    import secrets

    from glitnir.notebook import source_preview

    renders = []
    monkeypatch.setattr(
        source_preview,
        "render_project_view",
        lambda path, **o: renders.append(1) or {"svg": "<svg/>"},
    )
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *a: {
            "sha256": secrets.token_hex(32),  # matches nothing, ever
            "stable_sha256": "stable",
            "unknown": True,
        },
    )
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    answers = [service.view(cell, 4)]
    service._view_worker.join(timeout=10)
    answers += [service.view(cell, 4) for _ in range(4)]
    assert len(renders) == 1
    assert answers[-1] == {"svg": "<svg/>", "uncertain": True}
    assert service.view_job(answers[0]["job"]) == {"svg": "<svg/>", "uncertain": True}


def test_a_known_environment_needs_no_label(tmp_path: Path, monkeypatch) -> None:
    from glitnir.notebook import source_preview

    monkeypatch.setattr(
        source_preview, "render_project_view", lambda path, **o: {"svg": "<svg/>"}
    )
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    assert _drawn(service, cell, 4) == {"svg": "<svg/>"}


def test_a_view_is_keyed_by_the_providers_modules(tmp_path: Path, monkeypatch) -> None:
    """The provider's modules are part of the drawing's environment."""
    from glitnir.notebook import source_preview
    from glitnir.providers import ViewProvider

    provider = ViewProvider(
        "demo", frozenset({"circuit"}), ("Demo.View",), "", "m:", lambda *a: ""
    )
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({"circuit": provider}, {}, {}, {}),
    )
    asked = []

    def fingerprint(path, text, imports=(), packages=None):
        asked.append(imports)
        return {"sha256": f"env-{len(imports)}", "packages": []}

    monkeypatch.setattr(source_preview, "environment_fingerprint", fingerprint)
    keys = []
    monkeypatch.setattr(
        source_preview,
        "render_project_view",
        lambda path, **o: keys.append(service._view_job.key) or {"svg": "<svg/>"},
    )
    path = tmp_path / "A.lean"
    path.write_text(VIEW_TEXT)
    service = SourcePreviewService(path)
    cell = service.snapshot()["views"][0]["cell"]
    _drawn(service, cell, 4)
    assert ("Demo.View",) in asked
    assert keys[0][1] == "env-1"


def test_stop_cancels_only_that_cards_view(tmp_path: Path, monkeypatch) -> None:
    import threading

    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.notebook import source_preview

    started = threading.Event()
    calls = []

    def render(path, *, cancel, **options):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert cancel.wait(10)
            return {"error": "View drawing cancelled.", "transient": True}
        return {"svg": "<svg/>"}

    monkeypatch.setattr(source_preview, "render_project_view", render)
    service = _view_service(tmp_path, monkeypatch)
    snapshot = service.snapshot()
    sha, cell = snapshot["sha256"], snapshot["views"][0]["cell"]
    with pytest.raises(SnapshotError, match="not being drawn"):
        service.cancel_view(sha, 4)
    job = service.view(cell, 4)["job"]
    assert started.wait(10)
    for other in ((sha, 3), ("0" * 64, 4)):
        with pytest.raises(SnapshotError, match="not being drawn"):
            service.cancel_view(*other)
    assert service._view_job is not None and not service._view_job.cancel.is_set()
    service.cancel_view(sha, 4)
    service._view_worker.join(timeout=10)
    # Not transient: the reader asked for it, so nothing redraws it unasked.
    assert service.view_job(job) == {"error": source_preview.VIEW_CANCELLED}
    assert _drawn(service, cell, 4) == {"svg": "<svg/>"}
    assert len(calls) == 2


def test_a_view_of_a_replaced_source_is_cancelled(tmp_path: Path, monkeypatch) -> None:
    """Its drawing would be refused anyway: the saved source it read is gone."""
    import threading

    from glitnir.notebook import source_preview

    started = threading.Event()
    cancelled = []

    def render(path, *, cancel, **options):
        started.set()
        cancelled.append(cancel.wait(10))
        return {"error": "Saved source changed; refresh and retry."}

    monkeypatch.setattr(source_preview, "render_project_view", render)
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    service.view(cell, 4)
    assert started.wait(10)
    service.path.write_text(VIEW_TEXT + "-- edited\n")
    assert service.view(cell, 4)["status"] == "queued"
    service._view_worker.join(timeout=10)
    assert cancelled == [True]


def test_close_stops_a_view_being_drawn(tmp_path: Path, monkeypatch) -> None:
    import threading

    from glitnir.notebook import source_preview

    started = threading.Event()

    def render(path, *, cancel, **options):
        started.set()
        assert cancel.wait(10)
        return {"error": "View drawing cancelled.", "transient": True}

    monkeypatch.setattr(source_preview, "render_project_view", render)
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    job = service.view(cell, 4)["job"]
    assert started.wait(10)
    service.close()
    assert not service._view_worker.is_alive()
    assert service.view_job(job) == {"error": source_preview.CLOSED}
    assert service.view(cell, 4) == {"error": source_preview.CLOSED}


def test_a_failing_view_worker_always_ends(tmp_path: Path, monkeypatch) -> None:
    from glitnir.notebook import source_preview

    def render(path, **options):
        raise RuntimeError("boom")

    monkeypatch.setattr(source_preview, "render_project_view", render)
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    assert _drawn(service, cell, 4) == {"error": "View drawing failed: boom"}
    assert service._view_job is None


INCOMPLETE = (
    "This view directive is incomplete. Write it at the start of a line as "
    "`-- @view <kind> <name>`."
)


def test_incomplete_or_indented_view_directives_are_errors(
    tmp_path: Path, monkeypatch
) -> None:
    text = "-- @view\n-- @view circuit\n  -- @view circuit c\ndef x := 1 -- @view no\n"
    service = _view_service(tmp_path, monkeypatch, text)
    views = service.snapshot()["views"]
    assert [(v["line"], v["kind"], v["name"]) for v in views] == [
        (1, "", ""),
        (2, "circuit", ""),
        (3, "circuit", "c"),
    ]
    assert all(v["error"] == INCOMPLETE and v["using"] == [] for v in views)
    assert service.view(views[2]["cell"], 3) == {"error": INCOMPLETE}


@pytest.mark.parametrize(
    "text, lines",
    [
        ("def c := 1\n-- @view circuit c\n", [2]),
        ("/- a block comment\n-- @view circuit c\n-/\ndef c := 1\n", []),
        ("/-- a doc comment\n-- @view circuit c\n-/\ndef c := 1\n", []),
        ('def s := "a string\n-- @view circuit c\n"\n', []),
    ],
)
def test_view_directives_are_standalone_comment_lines(
    tmp_path: Path, monkeypatch, text, lines
) -> None:
    """Lean reads a view directive as a comment line, as evidence and decisions."""
    service = _view_service(tmp_path, monkeypatch, text)
    assert [view["line"] for view in service.snapshot()["views"]] == lines


def test_a_view_directive_the_scan_cannot_settle_is_still_listed(
    tmp_path: Path, monkeypatch
) -> None:
    # A string holding `{` may be interpolated, so the scan cannot settle. As
    # for evidence, a line is then a directive unless the text before it ends
    # inside a literal or comment.
    text = '/- c\n-- @view circuit d\n-/\ndef s := s!"{1}"\n-- @view circuit c\n'
    service = _view_service(tmp_path, monkeypatch, text)
    assert [view["line"] for view in service.snapshot()["views"]] == [5]


def test_view_names_must_be_lean_identifiers(tmp_path: Path, monkeypatch) -> None:
    text = (
        "-- @view circuit c!\n"
        "-- @view circuit c using a, b;c\n"
        "-- @view circuit N.c' using [h1, h_2]\n"
        "-- @view circuit c using [h1]\n"
    )
    service = _view_service(tmp_path, monkeypatch, text)
    bad_name, bad_using, brackets, single = service.snapshot()["views"]
    assert bad_name["error"] == "View names must be Lean identifiers."
    assert bad_using["error"] == "View names must be Lean identifiers."
    assert brackets["error"] == "" and brackets["using"] == ["h1", "h_2"]
    assert single["error"] == "" and single["using"] == ["h1"]


def test_providers_load_once_per_service(tmp_path: Path, monkeypatch) -> None:
    from glitnir.notebook import source_preview

    loads = []
    path = tmp_path / "A.lean"
    path.write_text(VIEW_TEXT)
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: loads.append(1) or Providers({"circuit": _demo_provider()}, {}, {}, {}),
    )
    monkeypatch.setattr(
        source_preview, "render_project_view", lambda path, **o: {"svg": "<svg/>"}
    )
    service = SourcePreviewService(path)
    assert loads == []
    cell = service.snapshot()["views"][0]["cell"]
    service.snapshot()
    _drawn(service, cell, 4)
    service.view(cell, 4)
    assert len(loads) == 1


def test_view_reads_and_fingerprints_the_file_once(tmp_path: Path, monkeypatch) -> None:
    from glitnir.notebook import source_preview

    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    reads, fingerprints = [], []
    read = source_preview.read_regular

    def counted_read(path, limit):
        if path == service.path:
            reads.append(path)
        return read(path, limit)

    monkeypatch.setattr(source_preview, "read_regular", counted_read)
    monkeypatch.setattr(source_preview, "load_records", lambda path: {"x": 1})
    monkeypatch.setattr(source_preview, "claim_views", lambda *a: [])
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *a: fingerprints.append(1) or {"sha256": "e"},
    )
    monkeypatch.setattr(
        source_preview, "render_project_view", lambda path, **o: {"svg": "<svg/>"}
    )
    service.view(cell, 4)  # starts the drawing
    service._view_worker.join(timeout=10)
    reads.clear()
    fingerprints.clear()
    assert service.view(cell, 4) == {"svg": "<svg/>"}
    assert len(reads) == 1 and len(fingerprints) == 1


def test_snapshot_has_no_leftover_runs_list(tmp_path):
    """Evidence and decision cards carry their own runs; nothing reads `runs`."""
    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := by\n  trivial\n")
    assert "runs" not in SourcePreviewService(path).snapshot()


def test_snapshot_reports_prose_images_without_touching_checking(tmp_path):
    """Images are illustrations: hashed for display, never part of the source."""
    path = tmp_path / "Pictured.lean"
    text = (
        "/-! # Pictured\n\n![Plot](assets/plot.png)\n\n![Gone](assets/gone.svg)\n"
        "\n![Escape](assets/../Pictured.lean) -/\n\ntheorem t : True := by\n  trivial\n"
    )
    path.write_text(text)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/plot.png").write_bytes(b"png")
    service = SourcePreviewService(path)
    snapshot = service.snapshot()
    ok, missing, invalid = snapshot["assets"]
    assert ok == {
        "path": "assets/plot.png",
        "status": "ok",
        "sha256": hashlib.sha256(b"png").hexdigest(),
    }
    assert (missing["status"], invalid["status"]) == ("missing", "invalid")
    assert snapshot["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    (tmp_path / "assets/plot.png").write_bytes(b"other")
    changed = service.snapshot()
    assert changed["assets"][0]["sha256"] == hashlib.sha256(b"other").hexdigest()
    assert changed["sha256"] == snapshot["sha256"]
    assert changed["cells"] == snapshot["cells"]
    assert service.asset("assets/plot.png", hashlib.sha256(b"other").hexdigest()) == (
        b"other",
        "image/png",
    )


def test_images_in_source_cells_are_not_listed(tmp_path):
    """Only prose renders images, so only prose references are served."""
    path = tmp_path / "Code.lean"
    path.write_text("-- ![Plot](assets/plot.png)\ntheorem t : True := by\n  trivial\n")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/plot.png").write_bytes(b"png")
    service = SourcePreviewService(path)
    assert service.snapshot()["assets"] == []
    with pytest.raises(ValueError, match="does not refer"):
        service.asset("assets/plot.png", hashlib.sha256(b"png").hexdigest())


def test_a_control_character_in_an_image_path_keeps_the_snapshot(tmp_path):
    """One unreadable reference becomes a note; the rest of the preview stays."""
    path = tmp_path / "Odd.lean"
    path.write_text("/-! # Odd\n\n![Bad](assets/x\x00.png) -/\n")
    snapshot = SourcePreviewService(path).snapshot()
    [entry] = snapshot["assets"]
    assert entry["status"] == "invalid"
    assert [c["kind"] for c in snapshot["cells"]] == ["prose"]


# ----- evidence runs -----------------------------------------------------------

EVIDENCE_TEXT = (
    "import Demo.Evidence\n\ndef M := 1\n"
    "-- @evidence simulate M (h := 1/100)\n-- @evidence iterate M\n"
)


def _evidence_body(**changes):
    body = {
        "format": "glitnir.evidence/v1",
        "series": {"times": [0, 1], "values": [[0], [1]], "names": ["x"]},
        "provenance": [["solver", "demo"]],
        "finite": True,
    }
    return {**body, **changes}


def _evidence_provider(calls=None, **changes):
    from glitnir.providers import EvidenceProvider, EvidenceRequest

    def compute(request, payload):
        if calls is not None:
            calls.append(("compute", request, payload))
        return _evidence_body()

    fields = {
        "name": "demo",
        "verbs": frozenset({"simulate"}),
        "imports": ("Demo.Evidence",),
        "preamble": "open Demo",
        "marker": "demoEvidence:",
        "parse": lambda verb, target, options: EvidenceRequest(
            verb, target, {"options": options}
        ),
        "command": lambda request: f"#demo_evidence {request.target}",
        "compute": compute,
        "unavailable": lambda: None,
        **changes,
    }
    return EvidenceProvider(**fields)


def _evidence_service(tmp_path, monkeypatch, provider=None, runner=None):
    """A notebook with two evidence directives and a stand-in Lean runner."""
    from glitnir.notebook import source_preview

    calls: list = []
    provider = provider or _evidence_provider(calls)

    def run(source, **options):
        calls.append(("lean", options))
        return {"payload": '{"x": 1}'}

    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({}, {"simulate": provider}, {}, {}),
    )
    monkeypatch.setattr(
        source_preview, "environment_fingerprint", lambda *a: {"sha256": "f"}
    )
    monkeypatch.setattr(source_preview, "run_provider_lean", runner or run)
    path = tmp_path / "A.lean"
    path.write_text(EVIDENCE_TEXT)
    return SourcePreviewService(path), calls


def _evidence_sha() -> str:
    return hashlib.sha256(EVIDENCE_TEXT.encode()).hexdigest()


def _run_evidence(service, line=4):
    key = service.run_evidence(_evidence_sha(), line)
    service._evidence_worker.join(timeout=10)
    assert not service._evidence_worker.is_alive()
    return key


def _card(service, line=4):
    return next(c for c in service.snapshot()["evidence"] if c["line"] == line)


def test_an_evidence_run_waits_for_the_slot_and_counts_its_modules(
    tmp_path, monkeypatch
):
    """The run counts its provider's modules and waits for a busy slot.

    A card saved under that environment reads as current.
    """
    from glitnir.notebook import source_preview

    service, calls = _evidence_service(tmp_path, monkeypatch)
    imports = service._providers().evidence["simulate"].imports
    assert imports, "the demo provider loads modules of its own"
    asked = []

    def fingerprint(path, text, extra=(), packages=None):
        asked.append(extra)
        return {"sha256": f"env-{'+'.join(extra)}", "packages": []}

    monkeypatch.setattr(source_preview, "environment_fingerprint", fingerprint)
    _run_evidence(service)
    [(_, options)] = [call for call in calls if call[0] == "lean"]
    assert options["wait"] is True
    assert imports in asked
    card = _card(service)
    assert card["status"] == "current", card
    service.close()


def test_an_evidence_run_saves_a_current_snapshot(tmp_path, monkeypatch):
    import json

    from glitnir.notebook.check_records import records_path
    from glitnir.notebook.evidence_cards import checked_evidence

    service, calls = _evidence_service(tmp_path, monkeypatch)
    try:
        key = _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["key"] == key
    assert (card["status"], card["run_error"]) == ("current", None)
    assert card["snapshot"] == checked_evidence(_evidence_body())
    [lean, compute] = calls
    assert lean[1]["needs"] == "evidence"
    assert lean[1]["expected_sha256"] == _evidence_sha()
    assert (lean[1]["line"], lean[1]["provider_name"]) == (4, "demo")
    assert (lean[1]["imports"], lean[1]["preamble"]) == (
        ("Demo.Evidence",),
        "open Demo",
    )
    assert (lean[1]["command"], lean[1]["marker"]) == (
        "#demo_evidence M",
        "demoEvidence:",
    )
    assert lean[1]["cancel"] is service._cancel
    assert compute[1].target == "M" and compute[2] == {"x": 1}
    saved = json.loads(records_path(service.path).read_text())["evidence"][key]
    assert saved["provider"] == "demo" and saved["fingerprint"] == "f"
    assert saved["created"].endswith("+00:00")
    assert service.path.read_text() == EVIDENCE_TEXT


class _ExpectedError(ValueError):
    """What a provider raises for a request it cannot serve."""


def _raises(error):
    def compute(request, payload):
        raise error

    return compute


@pytest.mark.parametrize(
    "changes, message",
    [
        (
            {"compute": lambda request, payload: 1 / 0},
            "Provider demo failed: ZeroDivisionError: division by zero",
        ),
        (
            {"compute": _raises(ValueError("The model has no states."))},
            "The model has no states.",
        ),
        (
            {"compute": _raises(_ExpectedError("Option h must be positive."))},
            "Option h must be positive.",
        ),
        (
            {"compute": _raises(ValueError())},
            "Provider demo failed: ValueError: ",
        ),
        (
            {"compute": _raises(KeyError("x"))},
            "Provider demo failed: KeyError: 'x'",
        ),
        (
            {"command": lambda request: [][0]},
            "Provider demo failed: IndexError: list index out of range",
        ),
        (
            {"compute": lambda request, payload: _evidence_body(format="other")},
            "Provider demo returned invalid evidence: "
            "Evidence format must be glitnir.evidence/v1.",
        ),
    ],
)
def test_provider_failures_become_the_card_run_error(
    tmp_path, monkeypatch, changes, message
):
    from glitnir.notebook.check_records import records_path

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, _evidence_provider(**changes)
    )
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("never", message)
    assert not records_path(service.path).exists()


def test_unreadable_payloads_and_runner_errors_become_run_errors(tmp_path, monkeypatch):
    results = iter([{"payload": "{not json"}, {"error": "Line 2: unknown constant"}])
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(results)
    )
    try:
        _run_evidence(service)
        first = _card(service)["run_error"]
        _run_evidence(service)
        second = _card(service)["run_error"]
    finally:
        service.close()
    assert first == "Provider demo printed evidence that is not JSON."
    assert second == "Line 2: unknown constant"


@pytest.mark.parametrize(
    "sha, line, message",
    [
        ("0" * 64, 4, "Saved source changed; refresh and retry."),
        (None, 1, "No evidence directive at line 1."),
        (None, 5, "No installed provider runs `@evidence iterate`."),
    ],
)
def test_runs_are_refused_without_lean(tmp_path, monkeypatch, sha, line, message):
    from glitnir.lean.project_snapshot import SnapshotError

    def forbidden(*args, **kwargs):
        pytest.fail("Lean ran")

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=forbidden)
    try:
        with pytest.raises(SnapshotError) as raised:
            service.run_evidence(sha or _evidence_sha(), line)
    finally:
        service.close()
    assert str(raised.value) == message
    assert service._evidence_worker is None


def _rejecting(verb, target, options):
    raise ValueError("Options are written (h := 1/100).")


@pytest.mark.parametrize(
    "changes, message",
    [
        (
            {"unavailable": lambda: "Install demo[simulation]."},
            "Install demo[simulation].",
        ),
        ({"parse": _rejecting}, "Options are written (h := 1/100)."),
    ],
)
def test_unavailable_or_rejected_directives_are_refused(
    tmp_path, monkeypatch, changes, message
):
    from glitnir.lean.project_snapshot import SnapshotError

    def forbidden(*args, **kwargs):
        pytest.fail("Lean ran")

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, _evidence_provider(**changes), runner=forbidden
    )
    try:
        with pytest.raises(SnapshotError, match=r"^" + re.escape(message) + r"$"):
            service.run_evidence(_evidence_sha(), 4)
    finally:
        service.close()


def test_one_run_at_a_time_shown_as_running(tmp_path, monkeypatch):
    import threading

    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.notebook.source_preview import RUN_BUSY

    started, release = threading.Event(), threading.Event()
    outcomes = iter([{"error": "first failed"}, {"payload": "{}"}])

    def run(source, **options):
        started.set()
        assert release.wait(10)
        return next(outcomes)

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        release.set()
        _run_evidence(service)
        assert _card(service)["run_error"] == "first failed"
        release.clear()
        started.clear()
        service.run_evidence(_evidence_sha(), 4)
        assert started.wait(10)
        running = _card(service)
        with pytest.raises(SnapshotError, match=RUN_BUSY):
            service.run_evidence(_evidence_sha(), 4)
        release.set()
        service._evidence_worker.join(timeout=10)
        finished = _card(service)
    finally:
        release.set()
        service.close()
    # The next run of a card clears its last error as it starts.
    assert (running["status"], running["run_error"]) == ("running", None)
    assert (finished["status"], finished["run_error"]) == ("current", None)


def test_source_saved_during_the_run_is_not_saved(tmp_path, monkeypatch):
    from glitnir.notebook.check_records import records_path
    from glitnir.notebook.source_preview import CHANGED_DURING_RUN

    path = tmp_path / "A.lean"

    def run(source, **options):
        path.write_text(EVIDENCE_TEXT + "-- edited\n")
        return {"payload": "{}"}

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        _run_evidence(service)
        [card] = [c for c in service.snapshot()["evidence"] if c["line"] == 4]
    finally:
        service.close()
    assert card["run_error"] == CHANGED_DURING_RUN
    assert not records_path(path).exists()


def test_an_unreadable_records_file_is_never_overwritten(tmp_path, monkeypatch):
    from glitnir.notebook.check_records import records_path

    service, _calls = _evidence_service(tmp_path, monkeypatch)
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == (
        "A.glitnir.json exists but cannot be read; fix or remove it "
        "before saving evidence."
    )
    assert sidecar.read_text() == "not json"


def test_evidence_saves_wait_for_other_records_writes(tmp_path, monkeypatch):
    """A check and an evidence run never interleave their records writes."""
    import threading

    from glitnir.notebook.check_records import records_path

    ran = threading.Event()

    def run(source, **options):
        ran.set()
        return {"payload": "{}"}

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        with service._records_lock:
            service.run_evidence(_evidence_sha(), 4)
            assert ran.wait(10)
            service._evidence_worker.join(timeout=0.2)
            assert service._evidence_worker.is_alive()
            assert not records_path(service.path).exists()
        service._evidence_worker.join(timeout=10)
        card = _card(service)
    finally:
        service.close()
    assert card["status"] == "current"


def test_a_check_keeps_evidence_saved_before_it(tmp_path, monkeypatch):
    from glitnir.notebook import source_preview

    service, _calls = _evidence_service(tmp_path, monkeypatch)
    sha = _evidence_sha()
    result = {**_fake_result(sha), "theorems": []}
    monkeypatch.setattr(source_preview, "check_project", lambda *a, **k: result)
    try:
        key = _run_evidence(service)
        _check_and_wait(service, sha)
        card = _card(service)
    finally:
        service.close()
    assert (card["key"], card["status"]) == (key, "current")


def test_missing_provider_import_is_a_run_error_without_lean(tmp_path, monkeypatch):
    """Review Focus 4: the real runner refuses before the sandbox starts."""
    from glitnir.lean import provider_lean
    from glitnir.notebook import source_preview

    service, _calls = _evidence_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_preview, "run_provider_lean", provider_lean.run_provider_lean
    )
    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setattr(provider_lean, "_toolchain", lambda: toolchain)
    monkeypatch.setattr(
        provider_lean,
        "inspect_project",
        lambda path, *, expected_sha256, cancel, imports=(): {
            "status": "resolved",
            "setup": {
                "name": "A",
                "isModule": False,
                "importArts": {},
                "plugins": [],
                "dynlibs": [],
                "options": {},
            },
            "resolution": {"inputs": []},
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("run_observer was called")

    monkeypatch.setattr(provider_lean, "run_observer", forbidden)
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"].startswith(
        "This evidence needs the module `Demo.Evidence`, which no package"
    )


def test_close_cancels_the_run_and_refuses_new_ones(tmp_path, monkeypatch):
    import threading

    from glitnir.lean.project_snapshot import SnapshotError

    started = threading.Event()

    def run(source, **options):
        started.set()
        assert options["cancel"].wait(10)
        return {"error": "Evidence run cancelled.", "transient": True}

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    service.run_evidence(_evidence_sha(), 4)
    assert started.wait(10)
    service.close()
    assert not service._evidence_worker.is_alive()
    with pytest.raises(SnapshotError, match="closed"):
        service.run_evidence(_evidence_sha(), 4)


_UNKNOWN_GIT = {
    "sha256": "f",
    "packages": [{"name": "mathlib", "reason": "fatal: detected dubious ownership"}],
}
_UNKNOWN_NOTE = (
    "Could not read the git state of mathlib: fatal: detected dubious "
    "ownership; results are saved as out of date."
)


def test_check_says_when_dependency_git_state_is_unknown(tmp_path, monkeypatch):
    """Results saved under an unknown git state say why they read out of date."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    path.write_text("theorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    phases: list = []
    service = source_preview.SourcePreviewService(path)

    def fingerprint(*args):
        phases.append(service._check["message"])
        return _UNKNOWN_GIT

    monkeypatch.setattr(source_preview, "environment_fingerprint", fingerprint)
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _fake_result(sha)
    )
    try:
        _check_and_wait(service, sha)
        check = service.snapshot()["preview"]["check"]
    finally:
        service.close()
    assert phases[0] == "Reading dependency state"
    assert check["message"] == f"ok {_UNKNOWN_NOTE}"
    assert (tmp_path / "A.glitnir.json").is_file()


def test_evidence_saved_under_unknown_git_state_explains_it(tmp_path, monkeypatch):
    """The evidence is saved; the card's run error says why it is out of date."""
    from glitnir.notebook import source_preview
    from glitnir.notebook.check_records import load_records

    service, _ = _evidence_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_preview, "environment_fingerprint", lambda *a: _UNKNOWN_GIT
    )
    try:
        key = _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _UNKNOWN_NOTE
    assert key in (load_records(tmp_path / "A.lean") or {}).get("evidence", {})


def test_a_check_through_a_claim_shows_later_claims_not_rechecked(
    tmp_path, monkeypatch
):
    """The preview asks for lines 1..N; later saved results stay, marked."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    text = "theorem a : True := trivial\ntheorem b : True := trivial\n"
    path.write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    asked = []

    def theorem(name, line):
        return {
            "name": name,
            "status": "checked",
            "axioms": [],
            "start_line": line,
            "end_line": line,
            "proposition_sha256": name,
            "uses": [],
        }

    def check(source, *, through_line=None, **_):
        asked.append(through_line)
        result = {**_fake_result(sha), "theorems": [theorem("a", 1)]}
        if through_line is None:
            result["theorems"].append(theorem("b", 2))
        else:
            result["withheld"] = {
                "from_line": through_line + 1,
                "message": f"Checked through line {through_line} on request",
                "through_line": through_line,
            }
        return result

    monkeypatch.setattr(source_preview, "check_project", check)
    service = source_preview.SourcePreviewService(path)
    try:
        _check_and_wait(service, sha)
        service.check(sha, through_line=1)
        assert service.snapshot()["preview"]["check"]["through_line"] == 1
        service._worker.join(timeout=10)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert asked == [None, 1]
    assert snapshot["preview"]["check"]["through_line"] == 1
    assert snapshot["project_withheld"] == {
        "from_line": 2,
        "message": "Checked through line 1 on request",
        "through_line": 1,
    }
    a, b = snapshot["project_claims"]
    assert a["current"] and "not_rechecked" not in a
    assert b["current"] and b["not_rechecked"] is True


def test_a_check_through_the_last_line_is_a_check_of_the_whole_file(
    tmp_path, monkeypatch
):
    """Nothing after the line: the request is dropped, not shown as partial."""
    import hashlib

    from glitnir.notebook import source_preview

    path = tmp_path / "A.lean"
    text = "theorem a : True := trivial\ntheorem b : True := trivial\n"
    path.write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    asked = []

    def check(source, *, through_line=None, **_):
        asked.append(through_line)
        return _fake_result(sha)

    monkeypatch.setattr(source_preview, "check_project", check)
    service = source_preview.SourcePreviewService(path)
    try:
        for line in (2, 3, 1):
            service.check(sha, through_line=line)
            service._worker.join(timeout=10)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert asked == [None, None, 1]
    # The result reports no cut, so neither does the observation.
    assert snapshot["preview"]["check"]["through_line"] is None


def test_a_claim_cell_takes_in_a_continuation_of_its_command(tmp_path):
    """C1: a line continuing a saved claim's command joins its cell."""
    from glitnir.notebook.check_records import records_from_check, save_records
    from glitnir.notebook.source_preview import SourcePreviewService

    path = tmp_path / "A.lean"
    text = "def h : 1 = 1 := rfl\ntheorem foo : 1 = 1 := h\n"
    path.write_text(text)
    result = {
        **_fake_result("s"),
        "theorems": [
            {
                "name": "foo",
                "status": "checked",
                "axioms": [],
                "start_line": 2,
                "end_line": 2,
                "proposition_sha256": "p",
            }
        ],
    }
    service = SourcePreviewService(path)
    try:
        save_records(
            path, records_from_check(text, result, service._fingerprint(text)())
        )
        assert service.snapshot()["project_claims"][0]["current"] is True
        path.write_text(text + "  |>.trans sorry\n")
        snapshot = service.snapshot()
    finally:
        service.close()
    [claim] = snapshot["project_claims"]
    assert (claim["current"], claim["end_line"]) == (False, 3)
    [cell] = [c for c in snapshot["cells"] if c.get("claim") == "foo"]
    assert (cell["start"], cell["end"]) == (2, 3)
    assert "|>.trans sorry" in cell["text"]


def test_indented_claims_keep_their_own_cells(tmp_path):
    """N1: claims indented in a namespace are cells of their own, current."""
    from glitnir.notebook.check_records import records_from_check, save_records
    from glitnir.notebook.source_preview import SourcePreviewService

    path = tmp_path / "A.lean"
    text = (
        "namespace Foo\n"
        "  theorem a : True := trivial\n"
        "  theorem b : True := trivial\n"
        "end Foo\n"
    )
    path.write_text(text)

    def theorem(name, line):
        return {
            "name": name,
            "status": "checked",
            "axioms": [],
            "start_line": line,
            "end_line": line,
            "proposition_sha256": name,
        }

    result = {
        **_fake_result("s"),
        "theorems": [theorem("Foo.a", 2), theorem("Foo.b", 3)],
    }
    service = SourcePreviewService(path)
    try:
        save_records(
            path, records_from_check(text, result, service._fingerprint(text)())
        )
        snapshot = service.snapshot()
    finally:
        service.close()
    assert [c["current"] for c in snapshot["project_claims"]] == [True, True]
    cells = {
        c["claim"]: (c["start"], c["end"]) for c in snapshot["cells"] if c.get("claim")
    }
    assert cells == {"Foo.a": (2, 2), "Foo.b": (3, 3)}
