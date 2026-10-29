import Mathlib

theorem target (a b c : ℝ) (h : a + b + c = 0) : a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  calc
    a ^ 3 + b ^ 3 + c ^ 3 =
        3 * a * b * c + (a + b + c) *
          (a ^ 2 + b ^ 2 + c ^ 2 - a * b - b * c - c * a) := by ring
    _ = 3 * a * b * c := by rw [h, zero_mul, add_zero]

#print axioms target
