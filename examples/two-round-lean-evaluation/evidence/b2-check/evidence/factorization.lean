import Mathlib.Data.Real.Basic
import Mathlib.Tactic.Ring

namespace TwoRoundTrial

theorem cubic_factorization {R : Type*} [CommRing R] (a b c : R) :
    a ^ 3 + b ^ 3 + c ^ 3 =
      3 * a * b * c + (a + b + c) *
        (a ^ 2 + b ^ 2 + c ^ 2 - a * b - b * c - c * a) := by
  ring

theorem target (a b c : ℝ) (h : a + b + c = 0) : a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  calc
    a ^ 3 + b ^ 3 + c ^ 3 =
        3 * a * b * c + (a + b + c) *
          (a ^ 2 + b ^ 2 + c ^ 2 - a * b - b * c - c * a) :=
      cubic_factorization a b c
    _ = 3 * a * b * c := by rw [h, zero_mul, add_zero]

#print axioms cubic_factorization
#print axioms target

end TwoRoundTrial
