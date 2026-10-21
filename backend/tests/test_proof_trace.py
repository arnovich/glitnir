"""Saved proof observations preserve source spans and local tactic scopes."""

import hashlib
from pathlib import Path

import pytest

from glitnir.lean.proof_trace import validate_trace


def test_trace_keeps_each_tactics_own_goals_and_unicode_ranges():
    """A closed branch never replaces another node's before state."""
    source = (
        "-- λ\ntheorem t : True ∧ True := by\n  constructor\n  · trivial\n  · trivial\n"
    )
    data = source.encode()
    start = data.index(b"constructor")
    answer = validate_trace(
        {
            "nodes": [
                {
                    "start": start,
                    "end": start + 11,
                    "parent": None,
                    "before": ["⊢ True ∧ True"],
                    "after": ["left", "right"],
                    "declaration": "t",
                    "kind": "constructor",
                }
            ],
            "errors": [],
            "warnings": [],
        },
        source,
    )
    assert answer["nodes"][0]["text"] == "constructor"
    assert answer["nodes"][0]["range"]["start"] == {"line": 3, "character": 2}
    assert answer["nodes"][0]["after"] == ["left", "right"]


@pytest.mark.parametrize("start,end,parent", [(1, 2, None), (0, 999, None), (0, 2, 0)])
def test_trace_rejects_invalid_byte_spans_and_parent_cycles(start, end, parent):
    """Only UTF-8 boundaries and earlier parent nodes are accepted."""
    with pytest.raises(ValueError):
        validate_trace(
            {
                "nodes": [
                    {
                        "start": start,
                        "end": end,
                        "parent": parent,
                        "before": [],
                        "after": [],
                        "declaration": "t",
                        "kind": "exact",
                    }
                ],
                "errors": [],
                "warnings": [],
            },
            "λ test",
        )


def test_trace_rejects_oversized_or_nontext_goals():
    """Display-only Lean output still crosses a bounded API boundary."""
    with pytest.raises(ValueError):
        validate_trace(
            {
                "nodes": [
                    {
                        "start": 0,
                        "end": 1,
                        "parent": None,
                        "before": [{}],
                        "after": [],
                        "declaration": "t",
                        "kind": "exact",
                    }
                ],
                "errors": [],
                "warnings": [],
            },
            "x",
        )


def test_changed_saved_source_refuses_to_launch(tmp_path):
    """Reject stale commands before inspecting or executing a project."""
    from glitnir.lean.proof_trace import trace_file

    source = tmp_path / "Example.lean"
    source.write_text("theorem t : True := by trivial\n")
    result = trace_file(source, expected_sha256=hashlib.sha256(b"old").hexdigest())
    assert result["status"] == "unavailable"
    assert "changed" in result["message"]
    assert not source.with_suffix(".forseti.json").exists()


def test_missing_sandbox_is_reported_as_unavailable(tmp_path, monkeypatch):
    """A machine without a sandbox gets an answer, not an exception."""
    from glitnir.lean import proof_trace
    from glitnir.lean.sandbox import SandboxUnavailable

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := by trivial\n")
    monkeypatch.setattr(proof_trace, "_toolchain", lambda: tmp_path)

    def run(*_arguments, **_options):
        raise SandboxUnavailable("No Lean sandbox is available here.")

    monkeypatch.setattr(proof_trace, "run_observer", run)
    result = proof_trace.trace_file(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "unavailable"
    assert result["message"] == "No Lean sandbox is available here."
    assert result["nodes"] == []


def test_live_scoped_tactics_errors_and_admissions(tmp_path):
    """Lean reports real branch states, same-line tactics and partial failures."""
    import json
    import os
    import threading
    import time

    from glitnir.lean.project_checking import _run
    from glitnir.lean.project_inspection import _toolchain
    from glitnir.lean.project_snapshot import ProjectSnapshot
    from glitnir.lean.proof_trace import PROGRAM

    if os.environ.get("GLITNIR_TEST_PROOF_TRACE") != "1":
        pytest.skip("set GLITNIR_TEST_PROOF_TRACE=1 for the confined Lean trace check")
    toolchain = _toolchain()
    inputs = tmp_path / "inputs"
    output = tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    source = inputs / "Example.lean"
    text = (
        "theorem pair : True ∧ True := by\n  constructor\n  · trivial\n  · trivial\n"
        "theorem same (P : Prop) (h : P) : P ∧ P := by constructor <;> exact h\n"
        "theorem term : True := True.intro\n"
        "theorem admitted : False := by sorry\n"
        "theorem bad : False := by exact True.intro\n"
    )
    source.write_text(text)
    setup = {
        "name": "Example",
        "isModule": False,
        "importArts": {},
        "plugins": [],
        "dynlibs": [],
        "options": {},
    }
    setup_path = inputs / "setup.json"
    setup_path.write_text(json.dumps(setup))
    helper = inputs / "ProofTrace.lean"
    helper.write_bytes(PROGRAM.read_bytes())
    report = output / "trace.json"
    snapshot = ProjectSnapshot(inputs, toolchain, inputs, source, setup, "test", 0, 0)
    _run(
        snapshot,
        [
            "lean",
            "--run",
            str(helper),
            str(source),
            str(setup_path),
            "[]",
            str(report),
        ],
        cwd=output,
        readable=[toolchain],
        writable=True,
        cancel=threading.Event(),
        deadline=time.monotonic() + 90,
    )
    result = validate_trace(json.loads(report.read_text()), text)
    pair = [n for n in result["nodes"] if n["declaration"] == "pair"]
    assert any(n["text"] == "constructor" and len(n["after"]) == 2 for n in pair)
    assert any(
        n["text"] == "trivial" and len(n["before"]) == 1 and not n["after"]
        for n in pair
    )
    assert any(n["parent"] is not None for n in pair)
    assert any(n["declaration"] == "same" for n in result["nodes"])
    assert not any(n["declaration"] == "term" for n in result["nodes"])
    assert result["errors"]
    assert any("sorry" in warning for warning in result["warnings"])
    assert source.read_text() == text


def test_trace_bounds_overlapping_expanded_text(monkeypatch):
    """A small report cannot duplicate source into an unbounded response."""
    from glitnir.lean import proof_trace

    monkeypatch.setattr(proof_trace, "MAX_TRACE_TEXT_BYTES", 1024)
    node = {
        "start": 0,
        "end": 600,
        "parent": None,
        "declaration": "t",
        "kind": "tactic",
        "before": [],
        "after": [],
    }
    with pytest.raises(ValueError, match="expanded text limit"):
        validate_trace({"nodes": [node, node], "errors": [], "warnings": []}, "x" * 600)


def test_trace_maps_only_saved_source_and_keeps_checking_read_only(
    tmp_path, monkeypatch
):
    """Copied saved source is observed without changing checking metadata."""
    import json
    import time

    from glitnir.lean import proof_trace

    text = "-- λ\ntheorem t : True := by trivial\n"
    source = tmp_path / "A.lean"
    source.write_text(text)
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", "42000")
    monkeypatch.setattr(proof_trace, "_toolchain", lambda: toolchain)

    def run(runtime, arguments, **options):
        assert 40 < options["deadline"] - time.monotonic() <= 42
        assert runtime == toolchain
        assert toolchain in options["readable"]
        assert source.parent not in options["readable"]
        assert Path(arguments[-4]) != source
        assert arguments[-2] == "[]"
        data = Path(arguments[-4]).read_bytes()
        assert data == text.encode()
        assert options["writable"]
        start = data.index(b"trivial")
        node = {
            "start": start,
            "end": start + 7,
            "parent": None,
            "declaration": "t",
            "kind": "tactic",
            "before": ["⊢ True"],
            "after": [],
        }
        Path(arguments[-1]).write_text(
            json.dumps({"nodes": [node], "errors": [], "warnings": []})
        )

    monkeypatch.setattr(proof_trace, "run_observer", run)
    result = proof_trace.trace_file(
        source, expected_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    assert result["status"] == "ready"
    assert result["nodes"][0]["start"] == text.encode().index(b"trivial")
    assert result["nodes"][0]["range"]["start"] == {"line": 2, "character": 23}
    assert result["environment"]["kind"] == "read_only_live_dependencies"
    assert "environment_sha256" not in result
    assert source.read_text() == text
    assert not source.with_suffix(".forseti.json").exists()


def test_observation_absolutizes_validated_project_artifacts(tmp_path):
    """Private execution does not resolve relative Lake paths against scratch."""
    from glitnir.lean.proof_trace import _absolute_setup

    setup = {
        "importArts": {"Example": ["build/Example.olean"]},
        "plugins": [{"path": "build/plugin.so", "initFn": "init"}],
        "dynlibs": ["build/library.so"],
    }
    result = _absolute_setup(setup, tmp_path)
    assert result["importArts"]["Example"] == [str(tmp_path / "build/Example.olean")]
    assert result["plugins"] == [
        {"path": str(tmp_path / "build/plugin.so"), "initFn": "init"}
    ]
    assert result["dynlibs"] == [str(tmp_path / "build/library.so")]
    assert setup["importArts"]["Example"] == ["build/Example.olean"]
    with pytest.raises(ValueError, match="without a project"):
        _absolute_setup(setup, None)


def test_observation_rejects_changed_package_configuration(tmp_path):
    """The inspection description cannot silently outlive its configuration."""
    from glitnir.lean.proof_trace import _check_configuration

    config = tmp_path / "lakefile.toml"
    config.write_bytes(b"original")
    resolution = {
        "inputs": [
            {"path": config.name, "sha256": hashlib.sha256(b"original").hexdigest()},
            {"path": "missing", "sha256": None},
        ]
    }
    _check_configuration(tmp_path, resolution)
    config.write_bytes(b"changed")
    with pytest.raises(ValueError, match="configuration changed"):
        _check_configuration(tmp_path, resolution)


@pytest.mark.parametrize("configured, expected", [("3600000", 1800), ("", 600)])
def test_observation_timeout_uses_bounded_shared_setting(
    tmp_path, monkeypatch, configured, expected
):
    """The shared setting controls observation lifetime without unbounded runs."""
    import time

    from glitnir.lean import proof_trace

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := by trivial\n")
    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", configured)
    monkeypatch.setattr(proof_trace, "_toolchain", lambda: tmp_path)

    def run(_toolchain, _arguments, **options):
        assert expected - 2 < options["deadline"] - time.monotonic() <= expected
        raise ValueError("observed bounded deadline")

    monkeypatch.setattr(proof_trace, "run_observer", run)
    answer = proof_trace.trace_file(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert answer["message"] == "observed bounded deadline"


@pytest.mark.parametrize(
    "configured, expected", [("", "-M16384"), ("20480", "-M20480")]
)
def test_observation_uses_the_configured_memory_limit(
    tmp_path, monkeypatch, configured, expected
):
    """Proof-step recording passes `GLITNIR_MEMORY_LIMIT_MB` to Lean as `-M`."""
    from glitnir.lean import limits, proof_trace

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := by trivial\n")
    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 65536)
    monkeypatch.setenv("GLITNIR_MEMORY_LIMIT_MB", configured)
    monkeypatch.setattr(proof_trace, "_toolchain", lambda: tmp_path)
    seen: list[str] = []

    def run(_toolchain, arguments, **_options):
        seen.extend(a for a in arguments if a.startswith("-M"))
        raise ValueError("observed memory limit")

    monkeypatch.setattr(proof_trace, "run_observer", run)
    answer = proof_trace.trace_file(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert answer["message"] == "observed memory limit"
    assert seen == [expected]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("import Missing.Module\ntheorem t : True := True.intro\n", "unknown module"),
        ("theorem b : True := by exact (\ntheorem c : True := trivial\n", "expected"),
    ],
)
def test_live_trace_keeps_header_and_parse_errors(tmp_path, text, expected):
    """Import and parse failures are reported, not lost when commands elaborate."""
    import json
    import os
    import threading
    import time

    from glitnir.lean.project_checking import run_observer
    from glitnir.lean.project_inspection import _toolchain
    from glitnir.lean.proof_trace import PROGRAM

    if os.environ.get("GLITNIR_TEST_PROOF_TRACE") != "1":
        pytest.skip("set GLITNIR_TEST_PROOF_TRACE=1 for the confined Lean trace check")
    toolchain = _toolchain()
    inputs, output = tmp_path / "inputs", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    source = inputs / "Example.lean"
    source.write_text(text)
    setup = {
        "name": "Example",
        "isModule": False,
        "importArts": {},
        "plugins": [],
        "dynlibs": [],
        "options": {},
    }
    (inputs / "setup.json").write_text(json.dumps(setup))
    helper = inputs / "ProofTrace.lean"
    helper.write_bytes(PROGRAM.read_bytes())
    report = output / "trace.json"
    run_observer(
        toolchain,
        ["lean", "--run", str(helper), str(source), str(inputs / "setup.json")]
        + ["[]", str(report)],
        cwd=output,
        readable=[inputs, toolchain],
        writable=True,
        cancel=threading.Event(),
        deadline=time.monotonic() + 90,
    )
    result = validate_trace(json.loads(report.read_text()), text)
    assert any(expected in error for error in result["errors"]), result["errors"]
