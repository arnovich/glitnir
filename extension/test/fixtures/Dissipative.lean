import Mathlib

/-!
# A dissipative system

## The system

Start with a single state and negative feedback. Can we show that its energy
never increases?

$$ ẋ = −a·x $$
-/

namespace Notebook.Dissipative

/-- The damping rate `a`. -/
noncomputable def rate : ℝ := 1 / 2

/-- The vector field of `ẋ = −a·x`. -/
noncomputable def field (x : ℝ) : ℝ := -rate * x

/-- The energy `E(x) = x²/2`. -/
noncomputable def energy (x : ℝ) : ℝ := x ^ 2 / 2

-- @view circuit field (x0 := 1)

/-!
## An energy argument

Choose E(x) = x²/2. The argument has two ingredients: an algebraic sign fact,
and the derivative of E along a solution. Together they make E non-increasing
in time.
-/

/-- −a x² ≤ 0, for every x ∈ ℝ. -/
theorem energy_falls (x : ℝ) :
    -rate * x ^ 2 ≤ 0 := by
  unfold rate
  nlinarith [sq_nonneg x]

/-- d⁄dt E(x(t)) = −a x(t)², for every solution x and time t. -/
theorem energy_deriv (x : ℝ → ℝ) (hx : ∀ t, HasDerivAt x (field (x t)) t) (t : ℝ) :
    HasDerivAt (fun s => energy (x s)) (-rate * x t ^ 2) t := by
  have h := ((hx t).pow 2).div_const 2
  refine h.congr_deriv ?_
  simp only [field]
  ring

-- @evidence simulate field (x0 := 1) (h := 0.01) (steps := 400)

/-!
## Lifting to trajectories

A function whose derivative is never positive is antitone. The two lemmas above
supply exactly that, for every solution of the system.
-/

/-- t ↦ E(x(t)) is non-increasing, for every solution x. -/
theorem energy_antitone (x : ℝ → ℝ) (hx : ∀ t, HasDerivAt x (field (x t)) t) :
    Antitone (fun t => energy (x t)) := by
  apply antitone_of_deriv_nonpos
  · exact fun t => (energy_deriv x hx t).differentiableAt
  · intro t
    rw [(energy_deriv x hx t).deriv]
    exact energy_falls (x t)

end Notebook.Dissipative
