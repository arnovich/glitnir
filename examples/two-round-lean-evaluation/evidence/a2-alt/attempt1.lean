import Mathlib.Basic.Real.Basic
import Mathlib.Tactic.Ring

namespace TwoRoundTrial

theorem factorization {R : Type*} [CommRing R] (a b c : R) :
    a ^ 3 + b ^ 3 + c ^ 3 =
      (a + b + c) * (a ^ 2 + b ^ 2 + c ^ 2 - a * b - b * c - c * a) +
        3 * a * b * c := by
  ring

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
/- BEGIN PROOF target -/
  exact (factorization a b c).trans
    ((congrArg (fun s : ℝ =>
        s * (a ^ 2 + b ^ 2 + c ^ 2 - a * b - b * c - c * a) + 3 * a * b * c) h).trans
      ((congrArg (fun s : ℝ => s + 3 * a * b * c) (zero_mul _)).trans (zero_add _)))
/- END PROOF target -/

#print axioms factorization
#print axioms Real.commRing
#print axioms congrArg
#print axioms zero_mul
#print axioms zero_add
#print axioms target

end TwoRoundTrial
