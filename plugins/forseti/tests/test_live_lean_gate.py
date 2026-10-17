"""The live gate refuses missing prerequisites and mismatched replay closures."""

import importlib.util
from pathlib import Path

import pytest

from glitnir_forseti.lean.independent import IndependentCheck, IndependentStatus

_TOOL = Path(__file__).resolve().parents[1] / "tools/check_live_lean.py"
_SPEC = importlib.util.spec_from_file_location("check_live_lean", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def test_missing_repl_is_a_gate_failure(lean_release_project, tmp_path) -> None:
    """A configured project alone cannot make persistent tests mandatory."""
    with pytest.raises(SystemExit, match="REPL executable"):
        gate.check(lean_release_project, tmp_path / "missing")


def test_the_gate_checks_every_pinned_import_set(
    monkeypatch, lean_release_project
) -> None:
    """One unchecked closure may not hide behind another's successful replay."""
    repl = lean_release_project / "repl"
    repl.write_text("test executable")
    repl.chmod(0o700)
    imports = [("First",), ("Second", "Third")]
    checked = []
    monkeypatch.setattr(gate, "sandbox_available", lambda: True)
    monkeypatch.setattr(
        gate,
        "replay_pin",
        lambda: {"closures": [{"imports": list(i)} for i in imports]},
    )

    def verify(names, limits, project):
        checked.append(names)
        assert project == lean_release_project
        return IndependentCheck(IndependentStatus.CONFIRMED)

    monkeypatch.setattr(gate, "verify_closure", verify)
    gate.check(lean_release_project, repl)
    assert checked == imports


@pytest.mark.parametrize("pin", [{}, {"closures": []}])
def test_an_empty_replay_pin_cannot_skip_verification(
    monkeypatch, lean_release_project, pin
) -> None:
    """A broken inventory cannot turn the closure gate into a no-op."""
    repl = lean_release_project / "repl"
    repl.write_text("test executable")
    repl.chmod(0o700)
    monkeypatch.setattr(gate, "sandbox_available", lambda: True)
    monkeypatch.setattr(gate, "replay_pin", lambda: pin)
    with pytest.raises(SystemExit, match="no import closures"):
        gate.check(lean_release_project, repl)


@pytest.mark.parametrize("available", [True, False])
def test_unavailable_sandbox_or_closure_fails_closed(
    monkeypatch, lean_release_project, available
) -> None:
    """An unconfined or differently built environment never passes preflight."""
    repl = lean_release_project / "repl"
    repl.write_text("test executable")
    repl.chmod(0o700)
    monkeypatch.setattr(gate, "sandbox_available", lambda: available)
    monkeypatch.setattr(
        gate, "replay_pin", lambda: {"closures": [{"imports": ["First"]}]}
    )
    monkeypatch.setattr(
        gate,
        "verify_closure",
        lambda *args: IndependentCheck(IndependentStatus.UNKNOWN),
    )
    with pytest.raises(SystemExit, match="sandbox|closure"):
        gate.check(lean_release_project, repl)
