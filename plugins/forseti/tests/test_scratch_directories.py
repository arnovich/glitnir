"""The plugin's scratch directories are owner-marked, so a killed run's are swept."""

import tempfile
import time
from pathlib import Path

import pytest
from glitnir.lean.limits import SessionLimits
from glitnir.lean.scratch import MARKER

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "glitnir_forseti"


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Point the system temp directory at a private test directory."""
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def test_no_unmarked_temporary_directories_remain() -> None:
    offenders = [
        path.relative_to(PACKAGE)
        for path in PACKAGE.rglob("*.py")
        if "TemporaryDirectory(" in path.read_text() or "mkdtemp(" in path.read_text()
    ]
    assert offenders == []


def test_a_lean_session_writes_beside_its_owner_marker(temp_root, monkeypatch) -> None:
    from glitnir_forseti.lean import session as module

    seen = {}

    def elaborate(self, limits, digest, project, elan, source_path, setup_path):
        work = source_path.parent
        seen["work"] = work
        seen["owned"] = sorted(p.name for p in work.parent.iterdir())
        seen["visible"] = sorted(p.name for p in work.iterdir())
        return "elaborated"

    monkeypatch.setattr(module, "lean_module_setup", lambda project, stem: "{}")
    monkeypatch.setattr(module.SubprocessLeanSession, "_elaborate", elaborate)
    session = module.SubprocessLeanSession.__new__(module.SubprocessLeanSession)
    answer = session._run(
        "example : True := trivial\n", SessionLimits(), "d", temp_root, "elan"
    )
    assert answer == "elaborated"
    work = seen["work"]
    assert work.parent.parent == temp_root
    assert work.parent.name.startswith("glitnir-forseti-session-")
    assert seen["owned"] == sorted([MARKER, "work"])
    # The confined process's directory holds only the unit, not the marker.
    assert MARKER not in seen["visible"]
    assert not work.parent.exists()


def test_the_solver_worker_runs_in_an_empty_marked_directory(
    tmp_path, temp_root, monkeypatch
) -> None:
    from glitnir_forseti.entailment import solver

    record = tmp_path / "record"
    script = tmp_path / "python"
    script.write_text(
        "#!/bin/sh\n"
        f'{{ pwd; ls -A; echo --; ls -A ..; }} > "{record}"\n'
        "echo not json\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(solver.sys, "executable", str(script))
    solver.run_worker({"op": "refute"}, time.monotonic() + 60)
    cwd, *rest = record.read_text().splitlines()
    separator = rest.index("--")
    assert rest[:separator] == []
    assert sorted(rest[separator + 1 :]) == sorted([MARKER, "work"])
    work = Path(cwd)
    assert work.name == "work"
    assert work.parent.name.startswith("glitnir-forseti-z3-")
    assert not work.parent.exists()
