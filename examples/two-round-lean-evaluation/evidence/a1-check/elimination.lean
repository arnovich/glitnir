import Mathlib.Data.Real.Basic
import Mathlib.Tactic.Ring

namespace TwoRoundTrial

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  have hc : c = -(a + b) := by
    calc
      c = (a + b + c) - (a + b) := by ring
      _ = -(a + b) := by rw [h]; ring
  rw [hc]
  ring

#print axioms target

end TwoRoundTrial
