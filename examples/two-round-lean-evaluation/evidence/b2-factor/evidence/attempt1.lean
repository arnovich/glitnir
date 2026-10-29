import Mathlib

namespace TwoRoundTrial

theorem target (a b c : ℝ) (h : a + b + c = 0) : a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  apply sub_eq_zero.mp
  calc
    a ^ 3 + b ^ 3 + c ^ 3 - 3 * a * b * c =
        (a + b + c) * (a ^ 2 + b ^ 2 + c ^ 2 - a * b - b * c - c * a) := by ring
    _ = 0 := by rw [h, zero_mul]

#print axioms target

end TwoRoundTrial
