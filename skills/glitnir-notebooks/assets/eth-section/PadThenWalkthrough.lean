import PiecesCounted.PadThen

/-!
# From padding to counted steps

This companion to the ETH walkthrough explains `PiecesCounted.padThen_runs`
from Anthropic's formal-math project at commit
`16a5ce41304fcdaea4e0be7c132e1c361dc26da5` (Apache-2.0),
`eth/proof/rates/PiecesCounted/PadThen.lean`, lines 68–72.

The machine receives an input `a`, writes `N a` units, then applies `f` to
that input and the padding. It therefore halts within `N a + 1` transitions. This
counts machine transitions, not constant-cost Turing-machine steps: evaluating
`f` can perform substantial work. The state-size bound `S` and the separate
polynomial-time implementation results are essential to the later simulation.

The first hypothesis bounds the input, padding length, and padding state;
the second bounds the final result. The source proves a stronger invariant
`padThen_reaches`: starting with `j` units and `j + k = N a`, the machine
finishes within `k + 1` more transitions. At `k = 0`, it applies `f`. At a
successor, it writes one unit, keeping the total `j + k` fixed, and uses the
induction hypothesis. Setting `j = 0` and `k = N a` gives the theorem below.

The types `α` and `β` are section variables in the source. We copy their
implicit declarations, the original open namespace, and the theorem header.
Lean selects the same type parameters; no hypothesis is added or dropped.
This notebook targets the source's Lean 4.33.1. A successful build is not a
Glitnir 4.35 independent-check observation; no saved status is supplied here.
-/

namespace PadThenWalkthrough
section
open Groundwork
variable {α β : Type}

/-- Padding takes one transition per unit, then one transition for the result. -/
theorem padding_runs [HasSize α] [HasSize β] (N : α → ℕ) (f : α × List Unit → β) (a : α) {S : ℕ} (h1 : size a + size (N a) + (2 * N a + 1) + 1 ≤ S)
    (h2 : size (f (a, List.replicate (N a) ())) + 1 ≤ S) : (PiecesCounted.padThen N f).Runs a (N a + 1) S (f (a, List.replicate (N a) ())) := by
  exact PiecesCounted.padThen_runs (N := N) (f := f) (a := a) (h1 := h1) (h2 := h2)

end
end PadThenWalkthrough
