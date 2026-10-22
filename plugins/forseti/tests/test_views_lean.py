"""Draw Forseti views end to end through Glitnir's project runner.

Opt in like the other live Lean tests, by pointing ``FORSETI_LEAN_TEST_PROJECT``
(or ``FORSETI_LEAN_PROJECT``) at a built Forseti Lean checkout whose view
provider modules are built too, with ``lake`` on ``PATH``, Lean 4.35.0 installed
with elan and a working sandbox. The view provider's preamble
(``ViewDiagram.lean``) is elaborated with the notebook, as the editor does; the
notebook is a temporary module of the checkout's ``Gimle.Forseti.Tests`` library
and is removed with its results file afterwards.
"""

import hashlib
import threading

from glitnir.lean.project_views import render_project_views
from glitnir.providers import ViewProvider

from glitnir_forseti import providers

NOTEBOOK = """import Gimle.Asgard.Model.Continuous
import Gimle.Asgard.Compile.Syntax
import Gimle.Asgard.Compile.Polynomial

/-! # Views through Glitnir's project runner -/

namespace GlitnirViewLive
open Gimle.Asgard Model

def body : Body := {
  program := ⟨[⟨"state-x", "x", .state⟩, ⟨"param-a", "a", .parameter⟩],
    equations% { dx := -a*x; }⟩
  parameters := [⟨"param-a", 1 / 2⟩]
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

-- @view circuit update
-- @view model dissipative

end GlitnirViewLive
"""


def _line(prefix: str) -> int:
    lines = NOTEBOOK.split("\n")
    return next(n for n, line in enumerate(lines, 1) if line.startswith(prefix))


def test_live_views_draw_a_circuit_and_a_model(scratch_notebook) -> None:
    """The provider's preamble elaborates and draws both views as SVG."""
    path = scratch_notebook("GlitnirViewLive", NOTEBOOK)
    [provider] = [p for p in providers() if isinstance(p, ViewProvider)]
    result = render_project_views(
        path,
        expected_sha256=hashlib.sha256(NOTEBOOK.encode()).hexdigest(),
        provider=provider,
        views=[
            (_line("-- @view circuit"), "circuit", "update", ()),
            (_line("-- @view model"), "model", "dissipative", ()),
        ],
        cancel=threading.Event(),
    )
    assert "views" in result, result
    circuit, model = result["views"]
    assert "error" not in circuit, circuit
    assert circuit["svg"].lstrip().startswith("<svg")
    assert circuit["graph"]["nodes"] and circuit["graph"]["outputs"]
    assert "error" not in model, model
    assert model["svg"].lstrip().startswith("<svg")
    assert [row["name"] for row in model["model"]["states"]] == ["x"]
