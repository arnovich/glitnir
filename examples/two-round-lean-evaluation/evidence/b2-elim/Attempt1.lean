import Mathlib

namespace TwoRoundTrial

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
/- BEGIN PROOF target -/
  have hc : c = -(a + b) := by
    exact add_left_cancel (h.trans (add_neg_cancel (a + b)).symm)
  subst c
  ring
/- END PROOF target -/

#print axioms target

end TwoRoundTrial
