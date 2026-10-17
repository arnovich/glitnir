import Mathlib
import Gimle.Asgard.Model.Continuous
import Gimle.Asgard.Compile.Syntax

/-!
# A dissipative system, as an Asgard model

The same system as `Dissipative.lean`, stated as an Asgard continuous model so
the workbench can draw it from the definition.

![A hand sketch of the feedback loop](assets/feedback-sketch.svg)
-/

namespace Notebook.DissipativeModel
open Gimle.Asgard Model

/-- The damping rate `a`, exact. -/
def damping : ℚ := 1 / 2

/-- ẋ = −a·x with one observed state. -/
def body : Body := {
  program := ⟨[⟨"state-x", "x", .state⟩, ⟨"param-a", "a", .parameter⟩],
    equations% { dx := -a*x; }⟩
  parameters := [⟨"param-a", damping⟩]
  observations := [⟨⟨"obs-x", "x", .output⟩, "state-x"⟩]
}

/-- Integrate along time from x(0) = 1. -/
def evolution : Evolution := {
  states := [⟨"state-x", "dx", "initial-x"⟩]
  initialPorts := [⟨"initial-x", "x0", .initial⟩]
  initialValues := [⟨"initial-x", 1⟩]
  axis := ⟨"time", "t"⟩
  evolveAlong := "time"
  start := 0
}

/-- The compiled model. -/
def dissipative : ContinuousModel body evolution :=
  (compileContinuous body evolution).toOption.get (by decide +kernel)

-- @view model dissipative
-- @evidence simulate dissipative (h := 1/100) (steps := 400)

/-- The damping is positive. -/
theorem damping_pos : 0 < damping := by
  norm_num [damping]

end Notebook.DissipativeModel
