import Mathlib

namespace TwoRoundTrial

theorem eliminate_first {R : Type*} [CommRing R] (a b c : R)
    (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  have hs : a + (b + c) = 0 := (add_assoc a b c).symm.trans h
  have ha : a = -(b + c) :=
    add_right_cancel (hs.trans (neg_add_cancel (b + c)).symm)
  rw [ha]
  ring

#print axioms eliminate_first

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
/- BEGIN PROOF target -/
  exact eliminate_first a b c h
/- END PROOF target -/

#print axioms target

end TwoRoundTrial
