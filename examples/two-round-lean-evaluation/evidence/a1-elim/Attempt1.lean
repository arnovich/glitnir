import Mathlib

namespace TwoRoundTrial

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  have hc : c = -(a + b) := by linarith
  rw [hc]
  ring

#print axioms target

end TwoRoundTrial
