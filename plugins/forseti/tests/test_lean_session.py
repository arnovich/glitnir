"""Tests for the bounded Lean elaboration session."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from glitnir.lean.artifacts import artifact_files
from glitnir.lean.environment import (
    MAX_HEARTBEATS,
    MAX_OUTPUT_BYTES,
    MAX_TIMEOUT_MS,
)

from glitnir_forseti.lean.messages import MessageStream
from glitnir_forseti.lean.release_environment import (
    lean_module_setup,
    lean_process_environment,
)
from glitnir_forseti.lean.session import (
    NO_EXIT_CODE,
    Elaboration,
    RecordedLeanSession,
    SessionLimits,
    SessionStatus,
    SubprocessLeanSession,
)

UNSOLVED = json.dumps(
    {
        "data": "unsolved goals\nx : Nat\n⊢ x + 0 = x",
        "endPos": {"column": 6, "line": 2},
        "kind": "Tactic.unsolvedGoals",
        "pos": {"column": 33, "line": 1},
        "severity": "error",
    }
).encode()
SORRY = json.dumps(
    {
        "data": "declaration uses `sorry`",
        "endPos": {"column": 10, "line": 1},
        "kind": "hasSorry",
        "pos": {"column": 8, "line": 1},
        "severity": "warning",
    }
).encode()
AXIOMS = json.dumps(
    {
        "data": "'t1' depends on axioms: [propext, sorryAx]",
        "endPos": {"column": 0, "line": 7},
        "kind": "[anonymous]",
        "pos": {"column": 0, "line": 7},
        "severity": "information",
    }
).encode()


def recorded(*raw: bytes, status=SessionStatus.COMPLETED, exit_code: int = 0):
    return RecordedLeanSession(((status, b"\n".join(raw), exit_code),))


def test_limits_reject_non_positive_and_over_cap_values() -> None:
    SessionLimits()
    for field, cap in (
        ("timeout_ms", MAX_TIMEOUT_MS),
        ("max_heartbeats", MAX_HEARTBEATS),
        ("max_output_bytes", MAX_OUTPUT_BYTES),
    ):
        with pytest.raises(ValueError):
            SessionLimits(**{field: 0})
        with pytest.raises(ValueError):
            SessionLimits(**{field: cap + 1})
        with pytest.raises(TypeError):
            SessionLimits(**{field: True})


def test_clean_elaboration_succeeds_with_no_open_goals() -> None:
    result = recorded(b"").elaborate("example : True := trivial", SessionLimits())
    assert result.status is SessionStatus.COMPLETED
    assert result.succeeded()
    assert result.open_goals().is_closed()
    assert result.forbidden_axioms() == ()


def test_unsolved_goals_are_reported_as_search_state() -> None:
    result = recorded(UNSOLVED).elaborate("example : True := by skip", SessionLimits())
    assert not result.succeeded()
    goals = result.open_goals()
    assert not goals.is_closed()
    assert goals.goals[0].target == "x + 0 = x"


def test_a_sorry_can_never_be_reported_as_success() -> None:
    result = recorded(SORRY).elaborate("theorem t : True := by sorry", SessionLimits())
    assert result.status is SessionStatus.COMPLETED
    assert result.stream.errors() == ()
    assert result.stream.has_sorry()
    assert not result.succeeded()


def test_axioms_outside_the_allowlist_are_named() -> None:
    result = recorded(SORRY, AXIOMS).elaborate("theorem t : True", SessionLimits())
    assert "propext" in result.axioms
    assert result.forbidden_axioms() == ("sorryAx",)


def test_non_completed_statuses_never_succeed() -> None:
    for status in (
        SessionStatus.TIMEOUT,
        SessionStatus.INTERRUPTED,
        SessionStatus.UNAVAILABLE,
        SessionStatus.OUTPUT_LIMIT,
    ):
        session = RecordedLeanSession(((status, b"", NO_EXIT_CODE),))
        result = session.elaborate("example : True := trivial", SessionLimits())
        assert result.status is status
        assert not result.succeeded()


def test_recorded_session_reports_source_digest_and_exhaustion() -> None:
    session = recorded(b"")
    first = session.elaborate("example : True := trivial", SessionLimits())
    expected = hashlib.sha256(b"example : True := trivial").hexdigest()
    assert first.source_sha256 == expected
    second = session.elaborate("example : True := trivial", SessionLimits())
    assert second.status is SessionStatus.UNAVAILABLE
    assert session.requests == ["example : True := trivial"] * 2


def test_session_values_reject_hostile_construction() -> None:
    stream = MessageStream((), 0, False)
    with pytest.raises(TypeError):
        Elaboration("completed", "", stream, (), ())  # type: ignore[arg-type]
    done = SessionStatus.COMPLETED
    axioms = ("a", 1)
    with pytest.raises(TypeError):
        Elaboration(done, "", stream, (), axioms)  # type: ignore[arg-type]
    session = recorded(b"")
    with pytest.raises(TypeError):
        session.elaborate(b"bytes", SessionLimits())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        session.elaborate("example : True := trivial", None)  # type: ignore[arg-type]


def test_unconfigured_subprocess_session_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("FORSETI_LEAN_PROJECT", raising=False)
    monkeypatch.delenv("FORSETI_LEAN_TEST_PROJECT", raising=False)
    session = SubprocessLeanSession()
    result = session.elaborate("example : True := trivial", SessionLimits())
    assert result.status is SessionStatus.UNAVAILABLE
    assert not result.succeeded()


def _built(library: Path, stem: str, *suffixes: str) -> str:
    base = library / stem
    base.parent.mkdir(parents=True, exist_ok=True)
    for suffix in suffixes:
        Path(f"{base}.{suffix}").write_bytes(b"")
    return str(base)


def test_module_setup_lists_artifacts_in_the_nested_shape(tmp_path) -> None:
    forseti = tmp_path / ".lake/build/lib/lean"
    asgard = tmp_path / ".lake/packages/asgard/.lake/build/lib/lean"
    every = ("olean", "olean.server", "olean.private", "ir.sig", "ir")
    full = _built(forseti, "Gimle/Forseti/Full", *every)
    plain = _built(asgard, "Gimle/Asgard/Plain", "olean")
    gaps = _built(asgard, "Gimle/Asgard/Gaps", "olean", "olean.private", "ir")
    public = _built(asgard, "Gimle/Asgard/Public", "olean", "olean.server", "ir.sig")

    setup = json.loads(lean_module_setup(tmp_path, "Unit"))
    arts = setup["importArts"]

    assert arts["Gimle.Forseti.Full"] == [
        [f"{full}.olean", f"{full}.olean.server", f"{full}.olean.private"],
        [f"{full}.ir.sig", f"{full}.ir"],
    ]
    # Lean reads each file by position, so a file after a missing one is left out.
    assert arts["Gimle.Asgard.Plain"] == [[f"{plain}.olean"]]
    assert arts["Gimle.Asgard.Gaps"] == [[f"{gaps}.olean"]]
    assert arts["Gimle.Asgard.Public"] == [
        [f"{public}.olean", f"{public}.olean.server"],
        [f"{public}.ir.sig"],
    ]
    # Glitnir's reader of the shape gives every path the suffix it carries.
    for name in ("Forseti.Full", "Asgard.Plain", "Asgard.Gaps", "Asgard.Public"):
        files = artifact_files(arts[f"Gimle.{name}"])
        assert all(path.endswith(suffix) for path, suffix in files)


def test_child_environment_inherits_no_lean_override(lean_test_project, monkeypatch):
    monkeypatch.setenv("LEAN_PATH", "/tmp/hostile-lean-path")
    monkeypatch.setenv("LEAN_SYSROOT", "/tmp/hostile-sysroot")
    elan = os.path.join(os.path.expanduser("~"), ".elan/bin/elan")
    environment = lean_process_environment(lean_test_project, elan)
    assert "/tmp/hostile-lean-path" not in environment["LEAN_PATH"]
    assert "LEAN_SYSROOT" not in environment
    assert set(environment).issubset({"PATH", "LEAN_PATH", "HOME", "ELAN_HOME"})


def test_live_session_returns_a_real_forseti_goal_state(lean_test_project):
    session = SubprocessLeanSession(lean_test_project)
    source = (
        "import Gimle.Forseti\n\n"
        "open Gimle.Forseti in\n"
        "example (p : Predicate 1) : PredicateEquality p p := by\n"
        "  skip\n"
    )
    result = session.elaborate(source, SessionLimits(timeout_ms=180_000))
    assert result.status is SessionStatus.COMPLETED
    assert not result.succeeded()
    (goal,) = result.open_goals().goals
    assert goal.target == "PredicateEquality p p"
    assert goal.hypotheses[-1].names == ("p",)
    assert goal.hypotheses[-1].type_text == "Predicate 1"


def test_live_session_accepts_a_closed_proof(lean_test_project):
    session = SubprocessLeanSession(lean_test_project)
    source = (
        "import Gimle.Forseti\n\n"
        "open Gimle.Forseti in\n"
        "example (p : Predicate 1) : PredicateEquality p p :=\n"
        "  predicateReflexivity p\n"
    )
    result = session.elaborate(source, SessionLimits(timeout_ms=180_000))
    assert result.succeeded()
    assert result.open_goals().is_closed()


def test_live_timeout_leaves_no_surviving_child(lean_test_project, monkeypatch):
    """A timed-out elaboration leaves nothing of its own running.

    Only this session's processes count: its process group, which it leads,
    and anything still naming its private scratch directory. Every Lean session
    uses the same ``glitnir-forseti-session-`` prefix, so a machine-wide search
    for it also found other agents' live sessions and failed under load.
    """
    from glitnir_forseti.lean import session as module

    launched = []
    real = module.subprocess.Popen

    def popen(*args, **kwargs):
        process = real(*args, **kwargs)
        cwd = kwargs.get("cwd")
        if cwd is not None and Path(cwd).parent.name.startswith(
            "glitnir-forseti-session-"
        ):
            launched.append((process, Path(cwd)))
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    session = SubprocessLeanSession(lean_test_project)
    source = "import Gimle.Forseti\nexample : True := trivial\n"
    result = session.elaborate(source, SessionLimits(timeout_ms=60))
    assert result.status in (SessionStatus.TIMEOUT, SessionStatus.COMPLETED)
    if result.status is SessionStatus.TIMEOUT:
        ((process, scratch),) = launched
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)  # the session's group is empty
        survivors = subprocess.run(["pgrep", "-f", str(scratch)], capture_output=True)
        assert survivors.stdout.strip() == b""


def test_a_failed_launch_with_no_output_is_never_success() -> None:
    session = recorded(b"", exit_code=1)
    result = session.elaborate("import Missing.Module\n", SessionLimits())
    assert result.status is SessionStatus.COMPLETED
    assert result.stream.messages == ()
    assert result.exit_code == 1
    assert not result.succeeded()


def test_exit_code_is_validated() -> None:
    stream = MessageStream((), 0, False)
    done = SessionStatus.COMPLETED
    with pytest.raises(TypeError):
        Elaboration(done, "", stream, (), (), True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Elaboration(done, "", stream, (), (), -2)
    assert Elaboration(done, "", stream, (), ()).exit_code == NO_EXIT_CODE
