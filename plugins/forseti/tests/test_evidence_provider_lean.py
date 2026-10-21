"""Run Forseti evidence end to end through Glitnir's project runner.

Opt in like the other live Lean tests, by pointing ``FORSETI_LEAN_TEST_PROJECT``
(or ``FORSETI_LEAN_PROJECT``) at a built standalone Forseti Lean release checkout,
with ``lake`` on ``PATH``. Glitnir's runner also needs Lean 4.35.0 installed with
elan and a working sandbox. The notebook is written as a temporary module of the
checkout's ``Gimle.Forseti.Tests`` library, because the runner reads imports only
from a Lake workspace, and is removed with its results file afterwards.
"""

import hashlib
from pathlib import Path

import pytest
from glitnir.notebook.evidence_cards import checked_evidence

NOTEBOOK = """import Gimle.Asgard.Compile.RationalInterchange
import Gimle.Asgard.Model.Continuous
import Gimle.Asgard.Compile.Syntax
import Gimle.Asgard.Compile.Polynomial

/-! # Evidence through Glitnir's project runner -/

namespace GlitnirEvidenceLive
open Gimle.Asgard Model

def damping : ℚ := 1 / 2

def body : Body := {
  program := ⟨[⟨"state-x", "x", .state⟩, ⟨"param-a", "a", .parameter⟩],
    equations% { dx := -a*x; }⟩
  parameters := [⟨"param-a", damping⟩]
  observations := [⟨⟨"obs-x", "x", .output⟩, "state-x"⟩]
}

def evolution : Evolution := {
  states := [⟨"state-x", "dx", "initial-x"⟩]
  initialPorts := [⟨"initial-x", "x0", .initial⟩]
  initialValues := [⟨"initial-x", 1⟩]
  axis := ⟨"time", "t"⟩
  evolveAlong := "time"
  start := 0
}

def dissipative : ContinuousModel body evolution :=
  (compileContinuous body evolution).toOption.get (by decide +kernel)

def half : Polynomial.Expr 1 := .mul (.constant (1 / 2)) (.var 0)

def update : Circuit 1 1 := Polynomial.compileOutputs fun _ => half

-- @evidence simulate dissipative (h := 1/100) (steps := 40) (solver := euler)
-- @evidence iterate update (state := 1) (steps := 10)

end GlitnirEvidenceLive
"""


def _line(prefix: str) -> int:
    lines = NOTEBOOK.split("\n")
    return next(n for n, line in enumerate(lines, 1) if line.startswith(prefix))


SIMULATE_LINE = _line("-- @evidence simulate")
ITERATE_LINE = _line("-- @evidence iterate")


@pytest.fixture
def notebook(scratch_notebook) -> Path:
    """Write `NOTEBOOK` into the test project; it is removed with its results."""
    return scratch_notebook("GlitnirEvidenceLive", NOTEBOOK)


def _run(path: Path, line: int) -> dict:
    """Run the directive at `line` through the preview service; return its card."""
    from glitnir.notebook.source_preview import SourcePreviewService

    sha = hashlib.sha256(NOTEBOOK.encode()).hexdigest()
    service = SourcePreviewService(path)
    try:
        service.run_evidence(sha, line)
        service._evidence_worker.join(timeout=2400)
        assert not service._evidence_worker.is_alive(), "the run did not finish"
        return next(c for c in service.snapshot()["evidence"] if c["line"] == line)
    finally:
        service.close()


def _assert_current(card: dict, last: float) -> None:
    assert card["provider"] == "forseti", card
    assert card["run_error"] is None, card["run_error"]
    assert card["status"] == "current", card
    snapshot = card["snapshot"]
    assert checked_evidence(snapshot) == snapshot
    assert snapshot["finite"] is True
    assert snapshot["series"]["values"][-1][0] == pytest.approx(last)


def test_live_simulate_runs_through_the_project_runner(notebook) -> None:
    card = _run(notebook, SIMULATE_LINE)
    _assert_current(card, 0.995**40)
    assert card["snapshot"]["series"]["times"][-1] == pytest.approx(0.4)


@pytest.mark.simulation
def test_live_iterate_runs_through_the_project_runner(notebook) -> None:
    card = _run(notebook, ITERATE_LINE)
    _assert_current(card, 2.0**-10)
    assert card["snapshot"]["series"]["times"][-1] == 10
