"""Ordinary source preview never interprets project code or saved proof records."""

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import pytest

from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_inspection import TOOLCHAIN, supported_version
from glitnir.preview import snapshot as preview_snapshot
from glitnir.providers import Providers
from glitnir.service import preview as source_preview
from glitnir.service.preview import SourcePreviewService

# Environment fingerprints are SHA-256 digests; any other string matches nothing.
FP = "f" * 64
#: Another Lean version, which Glitnir refuses.
OTHER = "4.32.1"


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

    from glitnir.notebook.document import NotebookError
    from glitnir.service import preview as source_preview

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
            "setup": {"importArts": {"Big": [["x"]]}},
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
    from glitnir.service import preview as source_preview

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

    from glitnir.notebook.document import NotebookError
    from glitnir.service import preview as source_preview

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


def test_a_check_saves_the_environment_it_compiled_in(
    tmp_path, monkeypatch, patch_fingerprint
):
    """The fingerprint is retaken once the check holds the Lean slot.

    The environment is A when the check is asked for, B while it compiles
    (it changed during the wait), then A again. Saved under A, the result
    would read as current although it was compiled in B.
    """
    from glitnir.notebook.check_records import load_records
    from glitnir.service import preview as source_preview

    path = tmp_path / "A.lean"
    path.write_text("/-! # Title -/\ntheorem result : True := by\n  trivial\n")
    service = SourcePreviewService(path)
    first, second = "a" * 64, "b" * 64  # environments A and B
    environment = {"sha256": first}
    patch_fingerprint(lambda *a: dict(environment))

    def check(source, *, expected_sha256, cancel, progress, through_line, acquired):
        environment["sha256"] = second  # changed while the check waited
        acquired()
        result = _fake_result(expected_sha256)
        environment["sha256"] = first  # and back, after it compiled
        return result

    monkeypatch.setattr(source_preview, "check_project", check)
    sha = service.snapshot()["sha256"]
    service.check(sha)
    service._worker.join(5)
    saved = load_records(path)
    assert saved["environment"]["fingerprint"] == second
    [claim] = service.snapshot()["project_claims"]
    assert claim["current"] is False, "a result compiled in B is not current in A"
    service.close()


def test_stop_cancels_a_check_waiting_for_the_slot(
    tmp_path, monkeypatch, patch_fingerprint
):
    """A check waiting for another Lean run is stopped by Stop, not only close."""
    import threading

    from glitnir.lean.project_snapshot import SnapshotError, snapshot_slot

    patch_fingerprint(lambda *a: {"sha256": FP})
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

    from glitnir.service import cli

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
    from glitnir.service import cli

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
    from glitnir.service import cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["check", str(tmp_path / "A.lean"), "--no-write"])
    assert raised.value.code == 2


@pytest.mark.parametrize("failure", ["startup", "worker"])
def test_failed_project_worker_has_terminal_state(tmp_path, monkeypatch, failure):
    """Unexpected worker errors must not leave all controls disabled forever."""
    from glitnir.service import preview as source_preview

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

    from glitnir.service import preview as source_preview

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

    from glitnir.service import preview as source_preview

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

    from glitnir.service import preview as source_preview

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
    from glitnir.service.preview import SourcePreviewService

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

    from glitnir.service import preview as source_preview

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


def test_snapshot_skips_the_fingerprint_without_a_sidecar(
    tmp_path, monkeypatch, patch_fingerprint
):
    """Previews of unchecked files never pay for environment fingerprinting."""
    from glitnir.service import preview as source_preview

    def boom(*args, **kwargs):
        raise AssertionError("fingerprint computed without records")

    patch_fingerprint(boom)
    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := True.intro\n")
    service = source_preview.SourcePreviewService(path)
    try:
        snapshot = service.snapshot()
    finally:
        service.close()
    assert snapshot["project_claims"] == []


def test_snapshot_survives_a_fingerprint_failure(
    tmp_path, monkeypatch, patch_fingerprint
):
    """A failed fingerprint matches nothing: saved claims show, out of date."""
    import hashlib

    from glitnir.service import preview as source_preview

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

        patch_fingerprint(boom)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert [claim["current"] for claim in snapshot["project_claims"]] == [False]
    assert snapshot["project_withheld"] is None


def test_failed_save_keeps_the_check_result(tmp_path, monkeypatch):
    """A read-only directory must not turn a successful check into unavailable."""
    import hashlib

    from glitnir.service import preview as source_preview

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

    from glitnir.notebook import check_records
    from glitnir.notebook.check_records import (
        RESULTS_TOO_LARGE,
        _serialised,
        records_from_check,
        records_path,
        save_records,
        with_evidence,
    )
    from glitnir.service import preview as source_preview

    path = tmp_path / "A.lean"
    path.write_text("def x : Nat := 1\ntheorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    entry = {
        "prefix_sha256": "p",
        "fingerprint": FP,
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
    # Room for the claims (the fingerprint is a SHA-256) or for the saved
    # evidence, so both load, but not for both together.
    claims = records_from_check(path.read_text(), _fake_result(sha), "0" * 64)
    room = max(len(_serialised(claims).encode()), len(before)) + 10
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
    from glitnir.service import preview as source_preview

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

    from glitnir.service import preview as source_preview

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

    from glitnir.service import preview as source_preview

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

    from glitnir.service.preview import SourcePreviewService

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
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    message = _unavailable_message(tmp_path, path, monkeypatch, "setup failed")
    assert message.startswith("setup failed ")
    assert "If Basics are modules of this project" in message
    assert "`lake build Basics`" in message
    assert "Mathlib" not in message


def _unavailable_message(tmp_path, path, monkeypatch, text):
    from glitnir.service import preview as source_preview

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
    from glitnir.preview.linked import unbuilt_sibling_imports

    folder = Path("/nonexistent-folder")
    assert (
        unbuilt_sibling_imports(folder / "Main.lean", {"Mathlib": None, "Init": None})
        == []
    )
    (tmp_path / "Basics.lean").write_text("def x := 1\n")
    assert unbuilt_sibling_imports(tmp_path / "Main.lean", {"Basics": "abc"}) == []


def test_hint_names_only_plain_sibling_notebooks(tmp_path):
    from glitnir.preview.linked import unbuilt_sibling_imports

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
    from glitnir.service import preview as source_preview

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
    from glitnir.service import preview as source_preview

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


def test_view_renders_once_per_source(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    calls = []

    def render(path, **options):
        calls.append(options)
        return {"svg": "<svg/>"}

    service = _view_service(tmp_path, monkeypatch)
    draw_each_alone(render)
    cell = service.snapshot()["views"][0]["cell"]
    assert _drawn(service, cell, 4) == {"svg": "<svg/>"}
    assert service.view(cell, 4) == {"svg": "<svg/>"}
    assert len(calls) == 1
    assert calls[0]["using"] == ("a", "b") and calls[0]["line"] == 4
    assert calls[0]["expected_sha256"] == service.snapshot()["sha256"]
    with pytest.raises(KeyError):
        service.view(cell, 99)


def test_view_error_entries_do_not_render(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    def render(*args, **options):
        pytest.fail("rendered a view with an error")

    service = _view_service(tmp_path, monkeypatch, "-- @view graph g\n")
    draw_each_alone(render)
    assert "graph" in service.view(0, 1)["error"]


def test_view_cache_key_includes_the_environment_fingerprint(
    tmp_path: Path,
    monkeypatch,
    draw_each_alone,
    patch_fingerprint,
) -> None:

    calls = []
    draw_each_alone(lambda path, **options: calls.append(options) or {"svg": "<svg/>"})
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    fingerprint = {"sha256": "one"}
    patch_fingerprint(lambda *a, **k: fingerprint)
    _drawn(service, cell, 4)
    _drawn(service, cell, 4)
    assert len(calls) == 1
    fingerprint["sha256"] = "two"
    _drawn(service, cell, 4)
    assert len(calls) == 2


def test_a_view_request_never_waits_for_lean(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    """Drawing runs in a worker; its job is polled until it ends."""
    import threading

    from glitnir.service import preview as source_preview

    started, finish = threading.Event(), threading.Event()
    calls = []

    def render(path, *, progress, cancel, **options):
        calls.append(options["line"])
        progress("Waiting for another Lean run to finish.")
        started.set()
        assert finish.wait(10)
        return {"svg": f"<svg>{options['line']}</svg>"}

    draw_each_alone(render)
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
    # The other view is drawn in the same run, as a job of its own.
    other = service.view(cell, 4)
    assert other == {**waiting, "job": other["job"]} and other["job"] != job
    finish.set()
    service._view_worker.join(timeout=10)
    assert service.view_job(job) == {"svg": "<svg>3</svg>"}
    assert service.view_job(job) == {"svg": "<svg>3</svg>"}, "as often as asked"
    assert service.view(cell, 3) == {"svg": "<svg>3</svg>"}
    assert service.view_job(other["job"]) == {"svg": "<svg>4</svg>"}
    assert service.view(cell, 4) == {"svg": "<svg>4</svg>"}
    assert calls == [3, 4]
    assert service.view_job("0" * 32) == {
        "error": source_preview.UNKNOWN_JOB,
        "transient": True,
    }
    service.close()


def test_polling_a_job_reads_no_notebook(
    tmp_path: Path, monkeypatch, draw_each_alone, patch_fingerprint
) -> None:
    """A poll is cheap: no saved file, snapshot or fingerprint."""
    draw_each_alone(lambda path, **o: {"svg": "<svg/>"})
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    job = service.view(cell, 4)["job"]
    service._view_worker.join(timeout=10)

    def forbidden(*args, **kwargs):
        pytest.fail("a poll read the notebook")

    monkeypatch.setattr(service, "_text", forbidden)
    patch_fingerprint(forbidden)
    assert service.view_job(job) == {"svg": "<svg/>"}


def test_an_unknown_environment_draws_once_and_says_so(
    tmp_path: Path,
    monkeypatch,
    draw_each_alone,
    patch_fingerprint,
) -> None:
    """A random unknown-state value must not make every poll a new drawing."""
    import secrets

    renders = []
    draw_each_alone(lambda path, **o: renders.append(1) or {"svg": "<svg/>"})
    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(
            module,
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


def test_a_known_environment_needs_no_label(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    draw_each_alone(lambda path, **o: {"svg": "<svg/>"})
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    assert _drawn(service, cell, 4) == {"svg": "<svg/>"}


def test_a_view_is_keyed_by_the_providers_modules(
    tmp_path: Path, monkeypatch, draw_each_alone, patch_fingerprint
) -> None:
    """The provider's modules are part of the drawing's environment."""
    from glitnir.providers import ViewProvider
    from glitnir.service import preview as source_preview

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

    patch_fingerprint(fingerprint)
    keys = []
    draw_each_alone(
        lambda path, **o: keys.extend(j.key for j in service._view_run.jobs.values())
        or {"svg": "<svg/>"}
    )
    path = tmp_path / "A.lean"
    path.write_text(VIEW_TEXT)
    service = SourcePreviewService(path)
    cell = service.snapshot()["views"][0]["cell"]
    _drawn(service, cell, 4)
    assert ("Demo.View",) in asked
    assert keys[0][1] == "env-1"


def test_stop_cancels_only_that_cards_view(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    import threading

    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.service import preview as source_preview

    started = threading.Event()
    calls = []

    def render(path, *, cancel, **options):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert cancel.wait(10)
            return {"error": "View drawing cancelled.", "transient": True}
        return {"svg": "<svg/>"}

    draw_each_alone(render)
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
    run = service._view_run
    assert run is not None and not run.jobs[4].cancel.is_set()
    service.cancel_view(sha, 4)
    service._view_worker.join(timeout=10)
    # Not transient: the reader asked for it, so nothing redraws it unasked.
    assert service.view_job(job) == {"error": source_preview.VIEW_CANCELLED}
    assert _drawn(service, cell, 4) == {"svg": "<svg/>"}
    assert len(calls) == 2


def test_a_view_of_a_replaced_source_is_cancelled(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    """Its drawing would be refused anyway: the saved source it read is gone."""
    import threading

    started = threading.Event()
    cancelled = []

    def render(path, *, cancel, **options):
        started.set()
        cancelled.append(cancel.wait(10))
        return {"error": "Saved source changed; refresh and retry."}

    draw_each_alone(render)
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    service.view(cell, 4)
    assert started.wait(10)
    service.path.write_text(VIEW_TEXT + "-- edited\n")
    assert service.view(cell, 4)["status"] == "queued"
    service._view_worker.join(timeout=10)
    assert cancelled == [True]


def test_close_stops_a_view_being_drawn(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    import threading

    from glitnir.service import preview as source_preview

    started = threading.Event()

    def render(path, *, cancel, **options):
        started.set()
        assert cancel.wait(10)
        return {"error": "View drawing cancelled.", "transient": True}

    draw_each_alone(render)
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    job = service.view(cell, 4)["job"]
    assert started.wait(10)
    service.close()
    assert not service._view_worker.is_alive()
    assert service.view_job(job) == {"error": source_preview.CLOSED}
    assert service.view(cell, 4) == {"error": source_preview.CLOSED}


def test_a_failing_view_worker_always_ends(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    def render(path, **options):
        raise RuntimeError("boom")

    draw_each_alone(render)
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    assert _drawn(service, cell, 4) == {"error": "View drawing failed: boom"}
    assert service._view_run is None


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


def test_providers_load_once_per_service(
    tmp_path: Path, monkeypatch, draw_each_alone
) -> None:
    from glitnir.service import preview as source_preview

    loads = []
    path = tmp_path / "A.lean"
    path.write_text(VIEW_TEXT)
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: loads.append(1) or Providers({"circuit": _demo_provider()}, {}, {}, {}),
    )
    draw_each_alone(lambda path, **o: {"svg": "<svg/>"})
    service = SourcePreviewService(path)
    assert loads == []
    cell = service.snapshot()["views"][0]["cell"]
    service.snapshot()
    _drawn(service, cell, 4)
    service.view(cell, 4)
    assert len(loads) == 1


def test_view_reads_and_fingerprints_the_file_once(
    tmp_path: Path, monkeypatch, draw_each_alone, patch_fingerprint
) -> None:
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    reads, fingerprints = [], []
    read = preview_snapshot.read_regular

    def counted_read(path, limit):
        if path == service.path:
            reads.append(path)
        return read(path, limit)

    monkeypatch.setattr(preview_snapshot, "read_regular", counted_read)
    monkeypatch.setattr(preview_snapshot, "load_records", lambda path: {"x": 1})
    monkeypatch.setattr(preview_snapshot, "claim_views", lambda *a: [])
    patch_fingerprint(lambda *a: fingerprints.append(1) or {"sha256": "e"})
    draw_each_alone(lambda path, **o: {"svg": "<svg/>"})
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
    from glitnir.service import preview as source_preview

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
    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(
            module, "environment_fingerprint", lambda *a: {"sha256": FP}
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


def _saved_failures(service):
    """The failed runs saved in the service's records, as a reader reads them."""
    from glitnir.notebook.check_records import load_records, saved_evidence_failures

    return saved_evidence_failures(load_records(service.path))


def _reopened(service, line=4):
    """The card a new preview of the same notebook shows, before any run."""
    reopened = SourcePreviewService(service.path)
    try:
        return _card(reopened, line)
    finally:
        reopened.close()


def test_an_evidence_run_waits_for_the_slot_and_counts_its_modules(
    tmp_path, monkeypatch, patch_fingerprint
):
    """The run counts its provider's modules and waits for a busy slot.

    A card saved under that environment reads as current.
    """
    service, calls = _evidence_service(tmp_path, monkeypatch)
    imports = service._providers().evidence["simulate"].imports
    assert imports, "the demo provider loads modules of its own"
    asked = []

    def fingerprint(path, text, extra=(), packages=None):
        asked.append(extra)
        named = f"env-{'+'.join(extra)}".encode()
        return {"sha256": hashlib.sha256(named).hexdigest(), "packages": []}

    patch_fingerprint(fingerprint)
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
    from glitnir.providers.outcomes import checked_evidence

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
    assert saved["provider"] == "demo" and saved["fingerprint"] == FP
    assert saved["created"].endswith("+00:00")
    assert service.path.read_text() == EVIDENCE_TEXT


class _ExpectedError(ValueError):
    """What a provider raises for a request it cannot serve."""


def _raises(error):
    def compute(request, payload):
        raise error

    return compute


@pytest.mark.parametrize(
    "changes, message, kind",
    [
        (
            {"compute": lambda request, payload: 1 / 0},
            "Provider demo failed: ZeroDivisionError: division by zero",
            "provider",
        ),
        (
            {"compute": _raises(ValueError("The model has no states."))},
            "The model has no states.",
            "refused",
        ),
        (
            {"compute": _raises(_ExpectedError("Option h must be positive."))},
            "Option h must be positive.",
            "refused",
        ),
        (
            {"compute": _raises(ValueError())},
            "Provider demo failed: ValueError: ",
            "refused",
        ),
        (
            {"compute": _raises(KeyError("x"))},
            "Provider demo failed: KeyError: 'x'",
            "provider",
        ),
        (
            {"command": lambda request: [][0]},
            "Provider demo failed: IndexError: list index out of range",
            "provider",
        ),
        (
            {"compute": lambda request, payload: _evidence_body(format="other")},
            "Provider demo returned invalid evidence: "
            "Evidence format must be glitnir.evidence/v1.",
            "invalid",
        ),
    ],
)
def test_provider_failures_become_the_card_run_error(
    tmp_path, monkeypatch, changes, message, kind
):
    """Each is shown at once and saved as a failure of its kind."""
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, _evidence_provider(**changes)
    )
    try:
        key = _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("failed", message)
    [(saved_key, saved)] = _saved_failures(service).items()
    # A crash's text is not written for the reader: only its type is saved.
    if kind == "provider":
        message = message.rsplit(": ", 1)[0]
    assert (saved_key, saved["kind"], saved["message"]) == (key, kind, message)


def _asking_lean_every_time(monkeypatch):
    """Run Lean for every run, as separate previews would, keeping no output.

    For tests whose stand-in Lean answers the same input differently: they
    stand for runs in previews opened one after another, or for a project
    rebuilt between runs, where Lean's output is not kept from one to the next.
    """
    monkeypatch.setattr(source_preview, "_emission_key", lambda *args: None)


def test_unreadable_payloads_and_runner_errors_become_run_errors(tmp_path, monkeypatch):
    results = iter([{"payload": "{not json"}, {"error": "Line 2: unknown constant"}])
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(results)
    )
    _asking_lean_every_time(monkeypatch)
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
    assert _saved_failures(service) == {}


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
    from glitnir.service.preview import RUN_BUSY

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
    from glitnir.service.preview import CHANGED_DURING_RUN

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
    from glitnir.service import preview as source_preview

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
    from glitnir.service import preview as source_preview

    service, _calls = _evidence_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_preview, "run_provider_lean", provider_lean.run_provider_lean
    )
    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\n")
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
    "sha256": FP,
    "packages": [{"name": "mathlib", "reason": "fatal: detected dubious ownership"}],
}
_UNKNOWN_NOTE = (
    "Could not read the git state of mathlib: fatal: detected dubious "
    "ownership; results are saved as out of date."
)


def test_check_says_when_dependency_git_state_is_unknown(
    tmp_path, monkeypatch, patch_fingerprint
):
    """Results saved under an unknown git state say why they read out of date."""
    import hashlib

    from glitnir.service import preview as source_preview

    path = tmp_path / "A.lean"
    path.write_text("theorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    phases: list = []
    service = source_preview.SourcePreviewService(path)

    def fingerprint(*args):
        phases.append(service._check["message"])
        return _UNKNOWN_GIT

    patch_fingerprint(fingerprint)
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


def test_evidence_saved_under_unknown_git_state_explains_it(
    tmp_path, monkeypatch, patch_fingerprint
):
    """The evidence is saved; the card's run error says why it is out of date."""
    from glitnir.notebook.check_records import load_records

    service, _ = _evidence_service(tmp_path, monkeypatch)
    patch_fingerprint(lambda *a: _UNKNOWN_GIT)
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

    from glitnir.service import preview as source_preview

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

    from glitnir.service import preview as source_preview

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
    from glitnir.service.preview import SourcePreviewService

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
    from glitnir.service.preview import SourcePreviewService

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


@pytest.mark.parametrize("unknown, expected", [(False, "d" * 64), (True, None)])
def test_a_view_passes_its_environment_digest_to_the_renderer(
    tmp_path: Path,
    monkeypatch,
    unknown,
    expected,
    draw_each_alone,
    patch_fingerprint,
) -> None:

    calls = []
    draw_each_alone(lambda path, **options: calls.append(options) or {"svg": "<svg/>"})
    service = _view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(
            module,
            "environment_fingerprint",
            lambda *a, **k: {"sha256": "d" * 64, "unknown": unknown},
        )
    _drawn(service, cell, 4)
    assert calls[0]["environment_sha256"] == expected


@pytest.mark.parametrize("unknown, expected", [(False, FP), (True, None)])
def test_an_evidence_run_passes_its_environment_digest(
    tmp_path, monkeypatch, unknown, expected, patch_fingerprint
):

    seen = []

    def run(source, **options):
        seen.append(options["environment_sha256"])
        return {"payload": '{"x": 1}'}

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    patch_fingerprint(lambda *a: {"sha256": FP, "unknown": unknown})
    _run_evidence(service)
    assert seen == [expected]


# ----- saved drawings ----------------------------------------------------------


def _saving_view_service(
    tmp_path: Path,
    monkeypatch,
    draw_each_alone,
    drawing=None,
    unknown=False,
    text=VIEW_TEXT,
):
    """A view service with a fixed fingerprint and a counted, faked renderer."""
    renders: list[dict] = []
    answer = {"svg": "<svg/>"} if drawing is None else drawing
    draw_each_alone(lambda path, **options: renders.append(options) or dict(answer))
    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(
            module,
            "environment_fingerprint",
            lambda *a, **k: {
                "sha256": FP,
                "stable_sha256": FP,
                "unknown": unknown,
                "packages": [],
            },
        )
    return _view_service(tmp_path, monkeypatch, text), renders


def _view_cell(service, line: int = 4) -> int:
    return next(v["cell"] for v in service.snapshot()["views"] if v["line"] == line)


def _saved_drawings(path: Path) -> dict:
    from glitnir.notebook.check_records import load_records, saved_views

    return saved_views(load_records(path))


def test_drawing_is_saved_and_reused_without_lean(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.lean.project_views import view_provider_sha256
    from glitnir.notebook.check_records import evidence_prefix_sha256
    from glitnir.notebook.directives import evidence_key

    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        assert _drawn(service, _view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert len(renders) == 1
    [(key, entry)] = _saved_drawings(service.path).items()
    assert key == evidence_key("-- @view circuit c using a, b", 0)
    assert entry["prefix_sha256"] == evidence_prefix_sha256(VIEW_TEXT, 4)
    assert entry["fingerprint"] == FP
    assert entry["provider"] == "demo"
    assert entry["provider_sha256"] == view_provider_sha256(
        _demo_provider(), "circuit", "c", ("a", "b")
    )
    assert entry["drawing"] == {"svg": "<svg/>"}
    again = SourcePreviewService(service.path)
    try:
        assert again.view(_view_cell(again), 4) == {"svg": "<svg/>"}
        assert again.view(_view_cell(again), 4) == {"svg": "<svg/>"}
    finally:
        again.close()
    assert len(renders) == 1


def test_uncertain_drawing_is_not_saved(tmp_path, monkeypatch, draw_each_alone):
    from glitnir.notebook.check_records import records_path

    service, renders = _saving_view_service(
        tmp_path, monkeypatch, draw_each_alone, unknown=True
    )
    try:
        answer = _drawn(service, _view_cell(service), 4)
    finally:
        service.close()
    assert answer == {"svg": "<svg/>", "uncertain": True}
    assert not records_path(service.path).exists()


def test_a_drawing_with_an_unhashable_provider_is_not_saved(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.notebook.check_records import records_path
    from glitnir.providers import ViewProvider

    def command(kind, name, using):
        raise RuntimeError("no command")

    _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    provider = ViewProvider("demo", frozenset({"circuit"}), (), "", "m:", command)
    service = SourcePreviewService(
        tmp_path / "A.lean",
        providers=lambda: Providers({"circuit": provider}, {}, {}, {}),
    )
    try:
        assert _drawn(service, _view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert not records_path(service.path).exists()


def test_saved_drawing_redrawn_after_edit_above(tmp_path, monkeypatch, draw_each_alone):
    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        _drawn(service, _view_cell(service), 4)
    finally:
        service.close()
    # An edit below the directive keeps the saved drawing current.
    service.path.write_text(VIEW_TEXT + "def e := 3\n")
    below = SourcePreviewService(service.path)
    try:
        assert below.view(_view_cell(below), 4) == {"svg": "<svg/>"}
    finally:
        below.close()
    assert len(renders) == 1
    service.path.write_text(VIEW_TEXT.replace("def c := 1", "def c := 2"))
    above = SourcePreviewService(service.path)
    try:
        assert _drawn(above, _view_cell(above), 4) == {"svg": "<svg/>"}
    finally:
        above.close()
    assert len(renders) == 2


def test_saved_drawing_redrawn_when_the_provider_changes(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.providers import ViewProvider

    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        _drawn(service, _view_cell(service), 4)
    finally:
        service.close()
    provider = ViewProvider(
        "demo", frozenset({"circuit"}), (), "", "m:", lambda *a: "#draw"
    )
    changed = SourcePreviewService(
        service.path, providers=lambda: Providers({"circuit": provider}, {}, {}, {})
    )
    try:
        assert _drawn(changed, _view_cell(changed), 4) == {"svg": "<svg/>"}
    finally:
        changed.close()
    assert len(renders) == 2


def test_drawing_shown_when_saving_fails(tmp_path, monkeypatch, draw_each_alone):
    from glitnir.notebook.check_records import records_path

    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    try:
        cell = _view_cell(service)
        assert _drawn(service, cell, 4) == {"svg": "<svg/>"}
        # Kept in memory all the same: asking again draws nothing.
        assert service.view(cell, 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert len(renders) == 1
    assert sidecar.read_text() == "not json"


def test_a_drawing_too_large_to_save_is_still_shown(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.notebook import check_records

    monkeypatch.setattr(check_records, "MAX_VIEWS_BYTES", 10)
    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        assert _drawn(service, _view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert _saved_drawings(service.path) == {}


def test_a_drawing_is_not_saved_when_the_source_changed_during_it(
    tmp_path,
    monkeypatch,
    draw_each_alone,
):
    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)

    def render(path, **options):
        path.write_text(VIEW_TEXT + "def e := 3\n")
        return {"svg": "<svg/>"}

    draw_each_alone(render)
    try:
        assert _drawn(service, _view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert _saved_drawings(service.path) == {}


def test_saved_drawing_has_no_transient_fields(tmp_path, monkeypatch, draw_each_alone):
    drawn = {"svg": "<svg/>", "proofs_skipped": True}
    service, renders = _saving_view_service(
        tmp_path, monkeypatch, draw_each_alone, drawing=drawn
    )
    try:
        assert _drawn(service, _view_cell(service), 4) == drawn
    finally:
        service.close()
    [entry] = _saved_drawings(service.path).values()
    assert entry["drawing"] == {"svg": "<svg/>"}
    assert "uncertain" not in entry["drawing"]
    assert "proofs_skipped" not in entry["drawing"]


def test_saved_drawings_of_identical_directives_keep_their_own_keys(
    tmp_path,
    monkeypatch,
    draw_each_alone,
):
    from glitnir.notebook.directives import evidence_key

    text = "def c := 1\n-- @view circuit c\ndef d := 2\n-- @view circuit c\n"
    service, renders = _saving_view_service(
        tmp_path, monkeypatch, draw_each_alone, text=text
    )
    try:
        _drawn(service, _view_cell(service, 2), 2)
        _drawn(service, _view_cell(service, 4), 4)
    finally:
        service.close()
    assert set(_saved_drawings(service.path)) == {
        evidence_key("-- @view circuit c", 0),
        evidence_key("-- @view circuit c", 1),
    }
    # Removing the second directive drops its drawing at the next save.
    service.path.write_text("def c := 2\n-- @view circuit c\n")
    again = SourcePreviewService(service.path)
    try:
        _drawn(again, _view_cell(again, 2), 2)
    finally:
        again.close()
    assert set(_saved_drawings(service.path)) == {evidence_key("-- @view circuit c", 0)}


# ----- one run for a notebook's views -------------------------------------------

THREE_VIEWS = (
    "def a := 1\n-- @view circuit a\n"
    "def b := 2\n-- @view circuit b\n"
    "def c := 3\n-- @view circuit c\n"
)
MEMORY = (
    "Lean reached its memory limit of 16384 MB. The limit includes the library "
    "files Lean loads (about 4–5 GB with Mathlib) as well as its working memory. "
    "Raise the glitnir.memoryLimitMb setting and try again."
)


class _HeldRuns:
    """A fake `render_project_views` whose runs end only when released.

    Each call's lines, process cancel and other options are recorded. A run
    tells its views that Lean is drawing them, then waits for `release`, or
    for its cancel, which makes it a cancelled run. It answers the next of
    `answers`, if any, else a drawing per view naming its line.
    """

    def __init__(self, *answers: dict) -> None:
        import threading

        self.calls: list[tuple[list[int], Any, dict]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.answers = list(answers)

    def __call__(self, path, *, views, cancel, progress, **options):
        import time

        self.calls.append(([view[0] for view in views], cancel, options))
        progress("Lean is drawing it.")
        self.started.set()
        deadline = time.monotonic() + 10
        while not (self.release.is_set() or cancel.is_set()):
            assert time.monotonic() < deadline, "never released"
            time.sleep(0.005)
        if cancel.is_set():
            return {"error": "View drawing cancelled.", "transient": True}
        if self.answers:
            return self.answers.pop(0)
        return {"views": [{"svg": f"<svg>{view[0]}</svg>"} for view in views]}

    def lines(self) -> list[list[int]]:
        return [lines for lines, _cancel, _options in self.calls]


def _run_service(tmp_path, monkeypatch, runs, text=THREE_VIEWS, **options):
    """A view service drawing with `runs`, in a known environment."""
    from glitnir.service import preview as source_preview

    monkeypatch.setattr(source_preview, "render_project_views", runs)
    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(
            module,
            "environment_fingerprint",
            lambda *a, **k: {"sha256": FP, "stable_sha256": FP, "packages": []},
        )
    _view_service(tmp_path, monkeypatch, text)
    return SourcePreviewService(tmp_path / "A.lean", **options)


def _ask(service, line: int) -> dict:
    return service.view(_view_cell(service, line), line)


def _key(text: str, line: int) -> str:
    from glitnir.notebook.directives import evidence_key

    return evidence_key(text.split("\n")[line - 1], 0)


def test_views_of_one_provider_draw_in_one_run(tmp_path, monkeypatch):
    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        first = _ask(service, 2)
        assert runs.started.wait(10)
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        assert runs.lines() == [[2, 4, 6]]
        assert runs.calls[0][2]["environment_sha256"] == FP
        assert answers[2]["job"] == first["job"]
        assert len({answer["job"] for answer in answers.values()}) == 3
        for answer in answers.values():
            assert answer == {
                "status": "drawing",
                "message": "Lean is drawing it.",
                "job": answer["job"],
            }
            assert service.view_job(answer["job"]) == answer
        runs.release.set()
        service._view_worker.join(timeout=10)
        for line, answer in answers.items():
            drawn = {"svg": f"<svg>{line}</svg>"}
            assert service.view_job(answer["job"]) == drawn
            assert _ask(service, line) == drawn
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6]]
    assert set(_saved_drawings(service.path)) == {
        _key(THREE_VIEWS, line) for line in (2, 4, 6)
    }


def test_saved_or_cached_views_are_not_redrawn_in_a_run(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SnapshotError

    runs = _HeldRuns()
    runs.release.set()
    alone = THREE_VIEWS.split("def b")[0]
    first = _run_service(tmp_path, monkeypatch, runs, alone)
    try:
        assert _drawn(first, _view_cell(first, 2), 2) == {"svg": "<svg>2</svg>"}
    finally:
        first.close()
    first.path.write_text(THREE_VIEWS)
    # Drawings of this service are kept in memory only.
    service = SourcePreviewService(first.path, save_drawings=False)
    try:
        runs.release.clear()
        runs.started.clear()
        _ask(service, 4)
        assert runs.started.wait(10)
        sha = service.snapshot()["sha256"]
        service.cancel_view(sha, 6)
        runs.release.set()
        service._view_worker.join(timeout=10)
        with pytest.raises(SnapshotError, match="not being drawn"):
            service.cancel_view(sha, 6)
        # Line 2's drawing is saved, line 4's is in memory: only 6 is drawn.
        assert _drawn(service, _view_cell(service, 6), 6) == {"svg": "<svg>6</svg>"}
        assert _ask(service, 2) == {"svg": "<svg>2</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2], [4, 6], [6]]
    assert set(_saved_drawings(service.path)) == {_key(THREE_VIEWS, 2)}


def test_directive_inside_a_command_is_drawn_alone(tmp_path, monkeypatch):
    """Only directives at a command boundary of the trusted lexer join a run.

    The command before the directive must end above it
    (`command_boundaries(text).ends_at(line - 1)`), or only comments may
    precede it. A directive inside the `theorem … := by` block is drawn
    alone, when it is the view asked for, and never in another view's run.
    """
    text = (
        "-- Notes.\n"
        "-- @view circuit a\n"  # only comments before it
        "#check 1\n"
        "def a := 1\n"
        "-- @view circuit a\n"
        "theorem t : True := by\n"
        "-- @view circuit t\n"  # inside the theorem
        "  trivial\n"
        "\n"
        "-- @view circuit t\n"
    )
    runs = _HeldRuns()
    runs.release.set()
    service = _run_service(tmp_path, monkeypatch, runs, text, save_drawings=False)
    try:
        assert _drawn(service, _view_cell(service, 5), 5) == {"svg": "<svg>5</svg>"}
        assert _drawn(service, _view_cell(service, 7), 7) == {"svg": "<svg>7</svg>"}
    finally:
        service.close()
    other = SourcePreviewService(service.path, save_drawings=False)
    try:
        assert _drawn(other, _view_cell(other, 7), 7) == {"svg": "<svg>7</svg>"}
    finally:
        other.close()
    assert runs.lines() == [[2, 5, 10], [7], [7]]


def test_stop_drops_only_that_view(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.service import preview as source_preview

    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        assert runs.started.wait(10)
        sha = service.snapshot()["sha256"]
        service.cancel_view(sha, 4)
        cancelled = {"error": source_preview.VIEW_CANCELLED}
        # Answered at once; the run goes on for the others.
        assert service.view_job(answers[4]["job"]) == cancelled
        assert service.view_job(answers[2]["job"])["status"] == "drawing"
        with pytest.raises(SnapshotError, match="not being drawn"):
            service.cancel_view(sha, 4)
        runs.release.set()
        service._view_worker.join(timeout=10)
        assert service.view_job(answers[2]["job"]) == {"svg": "<svg>2</svg>"}
        assert service.view_job(answers[4]["job"]) == cancelled
        assert service.view_job(answers[6]["job"]) == {"svg": "<svg>6</svg>"}
    finally:
        service.close()
    [(_lines, cancel, _options)] = runs.calls
    assert not cancel.is_set()
    assert set(_saved_drawings(service.path)) == {
        _key(THREE_VIEWS, 2),
        _key(THREE_VIEWS, 6),
    }


def test_stopping_every_view_stops_the_run(tmp_path, monkeypatch):
    from glitnir.service import preview as source_preview

    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        assert runs.started.wait(10)
        sha = service.snapshot()["sha256"]
        [(_lines, cancel, _options)] = runs.calls
        service.cancel_view(sha, 2)
        service.cancel_view(sha, 6)
        assert not cancel.is_set()
        service.cancel_view(sha, 4)
        assert cancel.is_set()
        service._view_worker.join(timeout=10)
        for answer in answers.values():
            assert service.view_job(answer["job"]) == {
                "error": source_preview.VIEW_CANCELLED
            }
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6]]
    assert _saved_drawings(service.path) == {}


def test_failed_run_of_several_views_retries_one_per_run(tmp_path, monkeypatch):
    runs = _HeldRuns({"error": MEMORY})
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        runs.release.set()
        service._view_worker.join(timeout=10)
        for line, answer in answers.items():
            assert service.view_job(answer["job"]) == {"svg": f"<svg>{line}</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6], [2], [4], [6]]
    # Each view drawn alone can be stopped alone.
    cancels = [cancel for _lines, cancel, _options in runs.calls]
    assert len({id(cancel) for cancel in cancels}) == 4
    assert len(_saved_drawings(service.path)) == 3


@pytest.mark.parametrize(
    "message", ["Project check timed out.", "View drawing cancelled."]
)
def test_timed_out_run_is_not_retried(tmp_path, monkeypatch, message):
    failure = {"error": message, "transient": True}
    runs = _HeldRuns(failure)
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        runs.release.set()
        service._view_worker.join(timeout=10)
        for answer in answers.values():
            assert service.view_job(answer["job"]) == failure
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6]]


def test_views_of_a_timed_out_run_are_drawn_alone_when_asked_again(
    tmp_path, monkeypatch
):
    """The same views together would time out again: each is drawn alone."""
    runs = _HeldRuns({"error": "Project check timed out.", "transient": True})
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        _ask(service, 2)
        runs.release.set()
        service._view_worker.join(timeout=10)
        assert _drawn(service, _view_cell(service, 4), 4) == {"svg": "<svg>4</svg>"}
        assert _drawn(service, _view_cell(service, 2), 2) == {"svg": "<svg>2</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6], [4], [2]]


def test_views_of_a_cancelled_run_are_drawn_together_again(tmp_path, monkeypatch):
    failure = {"error": "View drawing cancelled.", "transient": True}
    runs = _HeldRuns(failure)
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        _ask(service, 2)
        runs.release.set()
        service._view_worker.join(timeout=10)
        assert _drawn(service, _view_cell(service, 4), 4) == {"svg": "<svg>4</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6], [2, 4, 6]]


def test_a_failed_run_of_one_view_is_not_retried(tmp_path, monkeypatch):
    runs = _HeldRuns({"error": MEMORY})
    runs.release.set()
    text = THREE_VIEWS.split("def b")[0]
    service = _run_service(tmp_path, monkeypatch, runs, text)
    try:
        assert _drawn(service, _view_cell(service, 2), 2) == {"error": MEMORY}
    finally:
        service.close()
    assert runs.lines() == [[2]]


def test_views_of_another_provider_wait(tmp_path, monkeypatch):
    from glitnir.providers import ViewProvider
    from glitnir.service import preview as source_preview

    graph = ViewProvider("graph", frozenset({"graph"}), (), "", "g:", lambda *a: "")
    runs = _HeldRuns()
    text = "def a := 1\n-- @view circuit a\n-- @view graph a\n-- @view circuit a\n"
    both = Providers({"circuit": _demo_provider(), "graph": graph}, {}, {}, {})
    service = _run_service(tmp_path, monkeypatch, runs, text, providers=lambda: both)
    try:
        first = _ask(service, 2)
        assert runs.started.wait(10)
        assert _ask(service, 3) == {
            "status": "queued",
            "message": source_preview.VIEW_QUEUED,
            "job": first["job"],
        }
        runs.release.set()
        assert _drawn(service, _view_cell(service, 3), 3) == {"svg": "<svg>3</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4], [3]]
    assert [options["provider"].name for *_, options in runs.calls] == [
        "demo",
        "graph",
    ]


def test_close_ends_every_view_of_the_run(tmp_path, monkeypatch):
    from glitnir.service import preview as source_preview

    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    answers = {line: _ask(service, line) for line in (2, 4, 6)}
    assert runs.started.wait(10)
    service.close()
    assert not service._view_worker.is_alive()
    assert runs.calls[0][1].is_set()
    for answer in answers.values():
        assert service.view_job(answer["job"]) == {"error": source_preview.CLOSED}


def test_a_failing_run_ends_every_view(tmp_path, monkeypatch):
    import threading

    release = threading.Event()

    def runs(path, **options):
        assert release.wait(10)
        raise RuntimeError("boom")

    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        release.set()
        service._view_worker.join(timeout=10)
        for answer in answers.values():
            assert service.view_job(answer["job"]) == {
                "error": "View drawing failed: boom"
            }
        assert service._view_run is None
    finally:
        service.close()


def test_a_run_draws_no_more_views_than_are_kept(tmp_path, monkeypatch):
    """Every answer of a run must outlast it, so a run is at most the cache size."""
    from glitnir.service import preview as source_preview

    monkeypatch.setattr(source_preview, "MAX_CACHED_VIEWS", 2)
    runs = _HeldRuns()
    runs.release.set()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        assert _drawn(service, _view_cell(service, 4), 4) == {"svg": "<svg>4</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4]]


#: The words every refusal shares after the version sentence.
LEAN_OFF = (
    f"Glitnir checks Lean {supported_version()}, so checking, proof steps, views, "
    "evidence and decisions are off; reading works as usual."
)
UNSUPPORTED_TEXT = (
    "import Demo.View\n\ndef c := 1\n-- @view circuit c\n"
    "-- @evidence simulate c\n-- @decide band : entailment c : c > 0\n--   |- c >= 0\n"
)


def _unsupported_service(tmp_path: Path, monkeypatch, toolchain: str | None):
    """A Lake notebook with every directive; each Lean runner fails the test."""
    import threading

    from glitnir.providers import (
        DecisionProvider,
        DecisionRequest,
        EvidenceProvider,
        EvidenceRequest,
    )
    from glitnir.service import preview as source_preview
    from glitnir.service import proof_steps

    def forbidden(*args, **kwargs):
        pytest.fail("a refused request reached Lean, Lake or a provider")

    evidence = EvidenceProvider(
        name="demo",
        verbs=frozenset({"simulate"}),
        imports=(),
        preamble="",
        marker="m:",
        parse=lambda verb, target, options: EvidenceRequest(verb, target, {}),
        command=forbidden,
        compute=forbidden,
        unavailable=lambda: None,
    )
    decision = DecisionProvider(
        name="demo",
        kinds=frozenset({"entailment"}),
        imports=(),
        parse=lambda name, kind, goal: DecisionRequest(name, kind, goal, {}),
        decide=forbidden,
        unavailable=lambda: None,
    )
    providers = Providers(
        {"circuit": _demo_provider()},
        {"simulate": evidence},
        {"entailment": decision},
        {},
    )
    monkeypatch.setattr(source_preview, "load_providers", lambda: providers)
    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(
            module, "environment_fingerprint", lambda *a: {"sha256": FP}
        )
    for name in (
        "check_project",
        "check_candidate",
        "inspect_project",
        "render_project_views",
        "run_provider_lean",
        "provider_imports_error",
    ):
        monkeypatch.setattr(source_preview, name, forbidden)
    monkeypatch.setattr(proof_steps, "trace_file", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    (tmp_path / "lakefile.toml").write_text('name = "demo"')
    (tmp_path / "lake-manifest.json").write_text("{}")
    if toolchain is not None:
        (tmp_path / "lean-toolchain").write_text(toolchain)
    path = tmp_path / "A.lean"
    path.write_text(UNSUPPORTED_TEXT)
    return SourcePreviewService(path)


def test_the_snapshot_says_whether_the_lean_version_is_supported(
    tmp_path: Path,
) -> None:
    (tmp_path / "lakefile.toml").write_text('name = "demo"')
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\r\n")
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n")
    assert SourcePreviewService(path).snapshot()["preview"]["lean"] == {
        "supported": True,
        "declared": TOOLCHAIN,
        "toolchain": TOOLCHAIN,
        "message": None,
    }
    (tmp_path / "lean-toolchain").write_text(f"leanprover/lean4:v{OTHER}\n")
    assert SourcePreviewService(path).snapshot()["preview"]["lean"] == {
        "supported": False,
        "declared": f"leanprover/lean4:v{OTHER}",
        "toolchain": TOOLCHAIN,
        "message": f"This project uses Lean {OTHER}. {LEAN_OFF}",
    }


@pytest.mark.parametrize(
    "toolchain, message",
    [
        (f"leanprover/lean4:v{OTHER}\n", f"This project uses Lean {OTHER}. {LEAN_OFF}"),
        (None, f"This project declares no Lean version. {LEAN_OFF}"),
        (
            f"v{OTHER}\n{TOOLCHAIN}\n",
            "This project's lean-toolchain could not be read: expected one "
            f"toolchain declaration. {LEAN_OFF}",
        ),
    ],
)
def test_every_lean_request_is_refused_up_front_on_another_version(
    tmp_path: Path, monkeypatch, toolchain, message
) -> None:
    """Nothing starts a worker, takes a slot or reaches Lake, Lean or a provider."""
    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.notebook.document import NotebookError

    service = _unsupported_service(tmp_path, monkeypatch, toolchain)
    snapshot = service.snapshot()
    assert snapshot["preview"]["lean"]["message"] == message
    sha = snapshot["sha256"]
    (view,) = snapshot["views"]
    assert not view["error"]
    assert [card["line"] for card in snapshot["evidence"]] == [5]
    assert [card["line"] for card in snapshot["decisions"]] == [6]
    with pytest.raises(NotebookError) as refused:
        service.inspect(sha)
    assert str(refused.value) == message
    with pytest.raises(NotebookError) as refused:
        service.check(sha)
    assert str(refused.value) == message
    with pytest.raises(NotebookError) as refused:
        service.check(sha, through_line=3)
    assert str(refused.value) == message
    with pytest.raises(ValueError) as refused:
        service.proof_steps.start(sha)
    assert str(refused.value) == message
    assert service.view(view["cell"], view["line"]) == {"error": message}
    with pytest.raises(SnapshotError) as refused:
        service.run_evidence(sha, 5)
    assert str(refused.value) == message
    with pytest.raises(SnapshotError) as refused:
        service.run_decision(sha, 6)
    assert str(refused.value) == message
    # Each refusal leaves every gate open and nothing reported as running.
    after = service.snapshot()
    assert after["preview"]["check"] is None
    assert after["preview"]["inspection"] is None
    assert service.proof_steps.snapshot() == {"status": "idle", "nodes": []}
    assert service._inspection_lock.acquire(blocking=False)
    service._inspection_lock.release()
    assert service._run_lock.acquire(blocking=False)
    service._run_lock.release()
    assert service._running is None and service._run_errors == {}
    service.close()


# ----- saved evidence failures --------------------------------------------------

_LEAN_ERROR = {"error": "Line 2: unknown constant 'x'", "lean": True}
_TIMED_OUT = {"error": "Project check timed out.", "transient": True, "timed_out": True}


@pytest.mark.parametrize(
    "result, kind, message",
    [
        (_LEAN_ERROR, "lean", "Line 2: unknown constant 'x'"),
        (
            {"error": "The provider printed no evidence.", "lean": True},
            "lean",
            "The provider printed no evidence.",
        ),
        (
            {"payload": "{not json"},
            "invalid",
            "Provider demo printed evidence that is not JSON.",
        ),
        (_TIMED_OUT, "timeout", "The run reached its time limit of 90 s."),
    ],
)
def test_each_lean_side_failure_is_saved_with_its_kind(
    tmp_path, monkeypatch, result, kind, message
):
    """Saved beside the notebook; a new preview shows it, not "never"."""
    from glitnir.notebook.check_records import evidence_prefix_sha256

    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", "90000")
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(result)
    )
    try:
        key = _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    # This session's own error is shown once, as the run error.
    assert (card["status"], card["run_error"], card["failure"]) == (
        "failed",
        message,
        None,
    )
    [(saved_key, saved)] = _saved_failures(service).items()
    assert saved_key == key
    assert {k: v for k, v in saved.items() if k != "created"} == {
        "prefix_sha256": evidence_prefix_sha256(EVIDENCE_TEXT, 4),
        "fingerprint": FP,
        "provider": "demo",
        "kind": kind,
        "message": message,
    }
    assert saved["created"].endswith("+00:00")
    reopened = _reopened(service)
    assert (reopened["status"], reopened["run_error"], reopened["snapshot"]) == (
        "failed",
        None,
        None,
    )
    assert reopened["failure"] == {
        "kind": kind,
        "message": message,
        "created": saved["created"],
        "status": "current",
    }


def test_a_long_failure_message_is_saved_cut_to_the_limit(tmp_path, monkeypatch):
    from glitnir.notebook.check_records import MAX_MESSAGE

    long = {"error": "Line 2: " + "x" * 3000, "lean": True}
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(long)
    )
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == long["error"]
    [saved] = _saved_failures(service).values()
    assert len(saved["message"]) == MAX_MESSAGE and saved["message"].endswith("…")


@pytest.mark.parametrize(
    "result",
    [
        {"error": "Evidence run cancelled.", "transient": True},
        {"error": "Another Lean run is in progress; retry when it finishes."},
        {"error": "Waiting for another Lean run timed out.", "transient": True},
        {"error": "Evidence needs a Lake project; see the README."},
        {"error": "Project inspection timed out."},
        {"error": "Lake could not read the project configuration."},
        {"error": "The sandbox is not available on this system."},
        {"error": "This evidence needs `import Demo.Evidence` in the notebook."},
        {"error": "Project check exceeded its output limit."},
    ],
)
def test_failures_that_say_nothing_about_the_directive_are_not_saved(
    tmp_path, monkeypatch, result
):
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(result)
    )
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("never", result["error"])
    assert _saved_failures(service) == {}
    assert _reopened(service)["status"] == "never"


def test_a_setup_error_naming_a_local_path_is_never_saved(tmp_path, monkeypatch):
    """Lake's "build these modules" advice names the project root: not saved."""
    from glitnir.lean import provider_lean
    from glitnir.notebook.check_records import records_path
    from glitnir.service import preview as source_preview

    service, _calls = _evidence_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_preview, "run_provider_lean", provider_lean.run_provider_lean
    )
    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setattr(provider_lean, "_toolchain", lambda: toolchain)
    own = {
        "name": "A",
        "isModule": False,
        "importArts": {},
        "plugins": [],
        "dynlibs": [],
        "options": {},
    }

    def inspect(path, *, expected_sha256, cancel, imports=()):
        if imports:  # the provider's module is not built
            return {"status": "unresolved", "needs_build": True, "message": "x"}
        return {"status": "resolved", "setup": own, "resolution": {"inputs": []}}

    monkeypatch.setattr(provider_lean, "inspect_project", inspect)
    monkeypatch.setattr(
        provider_lean, "run_observer", lambda *a, **k: pytest.fail("Lean ran")
    )
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    root = str(tmp_path.resolve())
    assert root in card["run_error"] and "lake build Demo.Evidence" in card["run_error"]
    assert not records_path(service.path).exists()


@pytest.mark.parametrize(
    "where",
    [
        lambda tmp_path: str(tmp_path / "data.csv"),
        lambda tmp_path: str(tmp_path.resolve() / "data.csv"),
        lambda tmp_path: str(Path.home() / "secret" / "data.csv"),
    ],
)
def test_a_failure_naming_a_local_path_is_shown_but_never_saved(
    tmp_path, monkeypatch, where
):
    """A provider's message is saved into committed records, so no path is."""
    path = where(tmp_path)
    provider = _evidence_provider(compute=_raises(ValueError(f"Cannot read {path}.")))
    service, _calls = _evidence_service(tmp_path, monkeypatch, provider)
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == f"Cannot read {path}."
    assert _saved_failures(service) == {}


@pytest.mark.parametrize(
    "environment",
    [
        {"sha256": FP, "unknown": True, "packages": []},
        {"sha256": "", "packages": []},
    ],
)
def test_a_failure_in_an_unknown_environment_is_not_saved(
    tmp_path, monkeypatch, environment, patch_fingerprint
):

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    patch_fingerprint(lambda *a: environment)
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _LEAN_ERROR["error"]
    assert _saved_failures(service) == {}


def test_a_failure_while_the_preview_closes_is_not_saved(tmp_path, monkeypatch):
    def run(source, **options):
        options["cancel"].set()
        return dict(_LEAN_ERROR)

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        _run_evidence(service)
    finally:
        service.close()
    assert _saved_failures(service) == {}


def test_a_refused_start_saves_nothing(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.notebook.check_records import records_path
    from glitnir.service.preview import RUN_BUSY

    service, _calls = _evidence_service(tmp_path, monkeypatch)
    try:
        with service._run_lock:
            with pytest.raises(SnapshotError, match=RUN_BUSY):
                service.run_evidence(_evidence_sha(), 4)
    finally:
        service.close()
    assert not records_path(service.path).exists()


def test_a_failure_whose_source_changed_during_the_run_is_not_saved(
    tmp_path, monkeypatch
):
    path = tmp_path / "A.lean"

    def run(source, **options):
        path.write_text(EVIDENCE_TEXT + "-- edited\n")
        return dict(_LEAN_ERROR)

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _LEAN_ERROR["error"]
    assert _saved_failures(service) == {}


def test_a_failure_that_cannot_be_saved_still_shows_its_own_error(
    tmp_path, monkeypatch
):
    from glitnir.notebook.check_records import records_path

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _LEAN_ERROR["error"]
    assert sidecar.read_text() == "not json"


def test_a_saved_failure_goes_out_of_date_with_the_text_above_or_the_environment(
    tmp_path, monkeypatch, patch_fingerprint
):

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    try:
        _run_evidence(service)
    finally:
        service.close()
    assert _reopened(service)["status"] == "failed"
    service.path.write_text(EVIDENCE_TEXT + "def later := 2\n")
    assert _reopened(service)["status"] == "failed", "edits below change nothing"
    service.path.write_text(EVIDENCE_TEXT.replace("def M := 1", "def M := 2"))
    card = _reopened(service)
    assert (card["status"], card["failure"]["status"]) == ("stale", "stale")
    service.path.write_text(EVIDENCE_TEXT)
    patch_fingerprint(lambda *a: {"sha256": "9" * 64})
    card = _reopened(service)
    assert (card["status"], card["failure"]["status"]) == ("stale", "stale")


def test_a_failure_after_a_success_keeps_the_snapshot_and_a_success_clears_it(
    tmp_path, monkeypatch
):
    from glitnir.notebook.check_records import load_records, saved_evidence

    outcomes = iter([{"payload": "{}"}, dict(_LEAN_ERROR), {"payload": "{}"}])
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(outcomes)
    )
    _asking_lean_every_time(monkeypatch)
    try:
        key = _run_evidence(service)
        _run_evidence(service)
        failed = _reopened(service)
        _run_evidence(service)
        cleared = _reopened(service)
    finally:
        service.close()
    assert (failed["status"], failed["snapshot"] is not None) == ("current", True)
    assert failed["failure"]["status"] == "current"
    assert failed["failure"]["message"] == _LEAN_ERROR["error"]
    assert (cleared["status"], cleared["failure"]) == ("current", None)
    assert key in saved_evidence(load_records(service.path))
    assert _saved_failures(service) == {}


def test_an_out_of_date_failure_is_hidden_behind_a_current_snapshot(
    tmp_path, monkeypatch, patch_fingerprint
):

    outcomes = iter([{"payload": "{}"}, dict(_LEAN_ERROR)])
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(outcomes)
    )
    other = {"sha256": "9" * 64}
    try:
        _run_evidence(service)  # current in FP
        patch_fingerprint(lambda *a: other)
        _run_evidence(service)  # fails in another environment
        in_other = _reopened(service)
        patch_fingerprint(lambda *a: {"sha256": FP})
        in_first = _reopened(service)
    finally:
        service.close()
    assert (in_other["status"], in_other["failure"]["status"]) == ("stale", "current")
    assert (in_first["status"], in_first["failure"]) == ("current", None)


def test_uncompared_results_mark_saved_failures_uncompared():
    from glitnir.preview.snapshot import uncompared as _uncompared

    failure = {"kind": "lean", "message": "m", "created": "c", "status": "stale"}
    card = {"status": "stale", "snapshot": None, "failure": dict(failure)}
    running = {"status": "running", "snapshot": None, "failure": dict(failure)}
    snapshot = _uncompared(
        {"project_claims": [], "evidence": [card, running], "decisions": []}
    )
    assert [c["status"] for c in snapshot["evidence"]] == ["uncompared", "running"]
    assert [c["failure"]["status"] for c in snapshot["evidence"]] == ["uncompared"] * 2


@pytest.mark.parametrize(
    "error",
    [
        OSError("cannot open /opt/x/data.csv"),
        FileNotFoundError(2, "No such file or directory", "/Volumes/y/model.json"),
        RuntimeError("GET https://api.example.test/v1/runs?token=s3cr3t failed"),
    ],
)
@pytest.mark.parametrize("where", ["command", "compute"])
def test_a_provider_crash_saves_only_its_exception_type(
    tmp_path, monkeypatch, error, where
):
    """The crash's text, which may name any path or URL, is never written."""
    from glitnir.notebook.check_records import records_path

    def crash(*_arguments):
        raise error

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, _evidence_provider(**{where: crash})
    )
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    kind = type(error).__name__
    assert card["run_error"] == f"Provider demo failed: {kind}: {error}"
    [saved] = _saved_failures(service).values()
    assert (saved["kind"], saved["message"]) == (
        "provider",
        f"Provider demo failed: {kind}",
    )
    written = records_path(service.path).read_text()
    for secret in ("/opt/x", "/Volumes/y", "s3cr3t", "api.example.test"):
        assert secret not in written


@pytest.mark.parametrize("where", ["command", "compute"])
def test_a_missing_or_broken_dependency_is_shown_but_not_saved(
    tmp_path, monkeypatch, where
):
    """An ImportError is a setup failure: it says nothing about the directive."""

    def missing(*_arguments):
        raise ModuleNotFoundError("No module named 'gimle.asgard'")

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, _evidence_provider(**{where: missing})
    )
    try:
        _run_evidence(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == (
        "Provider demo failed: ModuleNotFoundError: No module named 'gimle.asgard'"
    )
    assert (card["status"], _saved_failures(service)) == ("never", {})


def test_an_unsaved_failure_is_logged_without_its_message(
    tmp_path, monkeypatch, caplog
):
    import logging

    from glitnir.notebook.check_records import records_path

    secret = str(tmp_path / "private.csv")
    provider = _evidence_provider(compute=_raises(ValueError(f"Cannot read {secret}")))
    service, _calls = _evidence_service(tmp_path, monkeypatch, provider)
    caplog.set_level(logging.DEBUG, logger="glitnir.service.preview")
    try:
        _run_evidence(service)
    finally:
        service.close()
    assert not records_path(service.path).exists()
    [record] = [r for r in caplog.records if "not saved" in r.getMessage()]
    assert "local path" in record.getMessage()
    assert all(secret not in r.getMessage() for r in caplog.records)


def test_a_failure_that_cannot_be_written_is_logged_by_its_error_type(
    tmp_path, monkeypatch, caplog
):
    import logging

    from glitnir.notebook.check_records import records_path

    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    records_path(service.path).write_text("not json")
    caplog.set_level(logging.DEBUG, logger="glitnir.service.preview")
    try:
        _run_evidence(service)
    finally:
        service.close()
    [record] = [r for r in caplog.records if "not saved" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "ValueError" in record.getMessage()
    assert "A.glitnir.json" not in record.getMessage()


def test_an_unsaved_failure_keeps_the_saved_one_readable_as_earlier(
    tmp_path, monkeypatch
):
    """A busy slot after a saved failure says nothing new about the directive."""
    busy = {"error": "Another Lean run is in progress; retry when it finishes."}
    outcomes = iter([dict(_LEAN_ERROR), busy, dict(_LEAN_ERROR), busy])
    service, _calls = _evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(outcomes)
    )
    try:
        _run_evidence(service)
        saved_now = _card(service)
        _run_evidence(service)
        after_busy = _card(service)
    finally:
        service.close()
    # Saved by this session's run: shown once, as the run error.
    assert (saved_now["run_error"], saved_now["failure"]) == (
        _LEAN_ERROR["error"],
        None,
    )
    # Not saved: the session's error is the latest, the saved one is earlier.
    assert (after_busy["status"], after_busy["run_error"]) == ("failed", busy["error"])
    assert after_busy["failure"]["message"] == _LEAN_ERROR["error"]
    assert after_busy["failure"]["earlier"] is True
    reopened = SourcePreviewService(service.path)
    try:
        _run_evidence(reopened)  # saves the same failure again
        assert _card(reopened)["failure"] is None
        _run_evidence(reopened)  # busy
        assert _card(reopened)["failure"]["earlier"] is True
    finally:
        reopened.close()


# ----- Lean output remembered for a rerun --------------------------------------


def _refusing_bad_options(calls):
    """A demo provider whose `compute` refuses options that name `bad`."""

    def compute(request, payload):
        calls.append(("compute", request, payload))
        if "bad" in request.options["options"]:
            raise ValueError("The circuit maps 1 port to 1, not 2.")
        return _evidence_body()

    return _evidence_provider(compute=compute)


def _rerun(service, text, line=4):
    """Save `text` as the notebook, run the directive at `line`; return its card."""
    service.path.write_text(text)
    service.run_evidence(hashlib.sha256(text.encode()).hexdigest(), line)
    service._evidence_worker.join(timeout=10)
    assert not service._evidence_worker.is_alive()
    return next(c for c in service.snapshot()["evidence"] if c["line"] == line)


def _lean_calls(calls):
    return [call for call in calls if call[0] == "lean"]


def _saved_snapshots(service):
    from glitnir.notebook.check_records import load_records

    return load_records(service.path)["evidence"]


BAD_TEXT = EVIDENCE_TEXT.replace("(h := 1/100)", "(h := bad)")


def test_a_first_run_still_runs_lean_and_saves_the_late_refusal(tmp_path, monkeypatch):
    """Nothing was remembered: Lean runs, and `compute`'s refusal is saved."""
    calls: list = []
    service, lean = _evidence_service(
        tmp_path, monkeypatch, _refusing_bad_options(calls)
    )
    try:
        card = _rerun(service, BAD_TEXT)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 1
    assert (card["status"], card["run_error"]) == (
        "failed",
        "The circuit maps 1 port to 1, not 2.",
    )
    [saved] = _saved_failures(service).values()
    assert (saved["kind"], saved["message"]) == (
        "refused",
        "The circuit maps 1 port to 1, not 2.",
    )


def test_a_rerun_after_an_options_edit_is_refused_without_lean(tmp_path, monkeypatch):
    """Lean's output for the unchanged text above is remembered and reused.

    The refusal is the same late check, on the same payload, saved as
    `refused` like any other.
    """
    calls: list = []
    service, lean = _evidence_service(
        tmp_path, monkeypatch, _refusing_bad_options(calls)
    )
    try:
        first = _rerun(service, EVIDENCE_TEXT)
        second = _rerun(service, BAD_TEXT)
        third = _rerun(service, EVIDENCE_TEXT.replace("1/100", "1/50"))
    finally:
        service.close()
    assert first["status"] == "current"
    assert len(_lean_calls(lean)) == 1
    assert [call[2] for call in calls] == [{"x": 1}] * 3
    assert (second["status"], second["run_error"]) == (
        "failed",
        "The circuit maps 1 port to 1, not 2.",
    )
    assert third["status"] == "current" and third["run_error"] is None
    [saved] = _saved_snapshots(service).values()
    assert saved["fingerprint"] == FP


def test_lines_below_the_directive_do_not_need_lean_again(tmp_path, monkeypatch):
    service, lean = _evidence_service(tmp_path, monkeypatch)
    try:
        _rerun(service, EVIDENCE_TEXT)
        card = _rerun(service, EVIDENCE_TEXT + "\ndef Later := 2\n")
    finally:
        service.close()
    assert card["status"] == "current"
    assert len(_lean_calls(lean)) == 1


@pytest.mark.parametrize(
    "edited",
    [
        EVIDENCE_TEXT.replace("def M := 1", "def M := 2"),  # the text above
        "\n" + EVIDENCE_TEXT,  # a line inserted above
    ],
)
def test_an_edit_above_the_directive_runs_lean_again(tmp_path, monkeypatch, edited):
    service, lean = _evidence_service(tmp_path, monkeypatch)
    line = edited.split("\n").index("-- @evidence simulate M (h := 1/100)") + 1
    try:
        _rerun(service, EVIDENCE_TEXT)
        _rerun(service, edited, line)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 2


def test_another_command_runs_lean_again(tmp_path, monkeypatch):
    """The command is part of what Lean saw: another target is another run."""
    service, lean = _evidence_service(tmp_path, monkeypatch)
    try:
        _rerun(service, EVIDENCE_TEXT)
        _rerun(service, EVIDENCE_TEXT.replace("simulate M", "simulate N"))
    finally:
        service.close()
    assert [call[1]["command"] for call in _lean_calls(lean)] == [
        "#demo_evidence M",
        "#demo_evidence N",
    ]


@pytest.mark.parametrize(
    "environments",
    [
        # The environment changed between the runs.
        [{"sha256": FP, "packages": []}, {"sha256": "e" * 64, "packages": []}],
        # An unknown environment's output is never remembered or reused.
        [{"sha256": FP, "unknown": True, "packages": []}] * 2,
    ],
)
def test_another_or_an_unknown_environment_runs_lean_again(
    tmp_path, monkeypatch, patch_fingerprint, environments
):
    service, lean = _evidence_service(tmp_path, monkeypatch)
    given = iter(environments)
    current: dict = {}

    def fingerprint(*args, **kwargs):
        return current

    patch_fingerprint(fingerprint)
    try:
        for environment in given:
            current.clear()
            current.update(environment)
            _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 2


@pytest.mark.parametrize(
    "failed",
    [
        {"error": "Line 2: unknown constant 'x'", "lean": True},
        {"error": "timed out.", "timed_out": True},
        {"error": "Lake failed."},
    ],
)
def test_only_printed_output_is_remembered(tmp_path, monkeypatch, failed):
    """A Lean error, time-out or setup failure is tried again on the next run."""
    results = iter([dict(failed), {"payload": '{"x": 1}'}, {"payload": "{}"}])
    asked: list = []

    def run(source, **options):
        asked.append(options)
        return next(results)

    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        _rerun(service, EVIDENCE_TEXT)
        _rerun(service, EVIDENCE_TEXT)
        card = _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert len(asked) == 2
    assert card["status"] == "current"


def test_remembered_output_is_bounded_by_count_and_size():
    from glitnir.service.preview import _Emissions

    kept = _Emissions(entries=2, size=10)
    kept.keep(("a",), "1234")
    kept.keep(("b",), "5678")
    kept.keep(("c",), "90")
    assert (kept.get(("a",)), kept.get(("b",)), kept.get(("c",))) == (
        None,
        "5678",
        "90",
    )
    kept.keep(("d",), "123456789")  # over the size with "90": the oldest go first
    assert (kept.get(("b",)), kept.get(("c",)), kept.get(("d",))) == (
        None,
        None,
        "123456789",
    )
    kept.keep(("e",), "12345678901")  # larger than the whole bound: not kept
    assert kept.get(("e",)) is None and kept.get(("d",)) == "123456789"


def test_output_is_not_kept_when_the_environment_changed_during_the_run(
    tmp_path, monkeypatch, patch_fingerprint
):
    """Lean may have loaded the other build: its output describes neither for sure.

    The run's own result is handled as before; only the next run asks Lean again.
    """
    current = {"sha256": FP, "packages": []}

    def run(source, **options):
        lean.append(options)
        current["sha256"] = "e" * 64  # rebuilt while Lean ran
        return {"payload": '{"x": 1}'}

    lean: list = []
    service, _calls = _evidence_service(tmp_path, monkeypatch, runner=run)
    patch_fingerprint(lambda *args: dict(current))
    try:
        first = _rerun(service, EVIDENCE_TEXT)
        current["sha256"] = FP  # and back
        _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert first["run_error"] is None
    assert len(lean) == 2


def test_output_kept_before_an_environment_change_is_used_after_it_is_undone(
    tmp_path, monkeypatch, patch_fingerprint
):
    current = {"sha256": FP, "packages": []}
    service, lean = _evidence_service(tmp_path, monkeypatch)
    patch_fingerprint(lambda *args: dict(current))
    try:
        _rerun(service, EVIDENCE_TEXT)
        current["sha256"] = "e" * 64
        _rerun(service, EVIDENCE_TEXT)
        current["sha256"] = FP
        card = _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 2
    assert card["status"] == "current"


def _key_parts(**changes):
    """The arguments of `_emission_key` for a demo job, with `changes`."""
    from glitnir.service.preview import _EvidenceJob

    parts = {
        "text": EVIDENCE_TEXT,
        "line": 4,
        "imports": ("Demo.Evidence",),
        "preamble": "open Demo",
        "marker": "demoEvidence:",
        "command": "#demo_evidence M",
        "environment": {"sha256": FP},
        **changes,
    }
    job = _EvidenceJob(
        text=parts["text"],
        sha256=hashlib.sha256(parts["text"].encode()).hexdigest(),
        line=parts["line"],
        key="k",
        keep=frozenset({"k"}),
        provider=_evidence_provider(),
        request=None,
    )
    return (
        job,
        parts["imports"],
        parts["preamble"],
        parts["marker"],
        parts["command"],
        parts["environment"],
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"imports": ("Demo.Evidence", "Demo.More")},
        {"preamble": "open Demo Other"},
        {"marker": "otherEvidence:"},
        {"command": "#demo_evidence N"},
        {"line": 5},
        {"text": EVIDENCE_TEXT.replace("def M := 1", "def M := 2")},
        {"text": "module\n" + EVIDENCE_TEXT, "line": 5},
        {"environment": {"sha256": "e" * 64}},
    ],
)
def test_everything_lean_is_given_is_part_of_the_key(changes):
    from glitnir.service.preview import _emission_key

    assert _emission_key(*_key_parts(**changes)) != _emission_key(*_key_parts())


def test_the_header_below_a_directive_is_part_of_the_key():
    """Lake's setup reads the whole header, even lines after the directive."""
    from glitnir.service.preview import _emission_key

    text = "-- @evidence simulate M (h := 1/100)\nimport Demo.Evidence\n"
    other = "-- @evidence simulate M (h := 1/100)\nmodule\nimport Demo.Evidence\n"
    first = _emission_key(*_key_parts(text=text, line=1))
    assert first is not None
    assert first != _emission_key(*_key_parts(text=other, line=1))


@pytest.mark.parametrize(
    "environment",
    [
        {"sha256": FP, "unknown": True},
        {"sha256": ""},
        {"sha256": "not a digest"},
        {},
    ],
)
def test_no_key_for_an_unknown_or_malformed_environment(environment):
    from glitnir.service.preview import _emission_key

    assert _emission_key(*_key_parts(environment=environment)) is None
