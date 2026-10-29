import Mathlib.Basic.Real.Basic

namespace TwoRoundTrial

theorem balanced_factorization {R : Type*} [CommSemiring R] (a b c : R) :
    a ^ 3 + b ^ 3 + c ^ 3 + (a + b + c) * (a * b + b * c + c * a) =
      (a + b + c) * (a ^ 2 + b ^ 2 + c ^ 2) + 3 * a * b * c := by
  rw [← (two_add_one_eq_three : (2 : R) + 1 = 3),
    ← (one_add_one_eq_two : (1 : R) + 1 = 2)]
  simp only [pow_succ, pow_zero, mul_one, one_mul, mul_add, add_mul]
  ac_rfl

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
/- BEGIN PROOF target -/
  have balanced := balanced_factorization a b c
  rw [h, zero_mul, zero_mul, add_zero, zero_add] at balanced
  exact balanced
/- END PROOF target -/

#print axioms balanced_factorization
#print axioms Real.commRing
#print axioms Real.semiring
#print axioms two_add_one_eq_three
#print axioms one_add_one_eq_two
#print axioms target

end TwoRoundTrial
