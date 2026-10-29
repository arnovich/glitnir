import Mathlib.Basic.Real.Basic
import Mathlib.Tactic.Ring

namespace TwoRoundTrial

theorem generic_elimination {R : Type*} [CommRing R]
    (a b c : R) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  have hc : c = -(a + b) := by
    calc
      c = (a + b + c) - (a + b) := by ring
      _ = -(a + b) := by rw [h]; ring
  rw [hc]
  ring

def required_type : Prop :=
  ∀ (a b c : ℝ), a + b + c = 0 →
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  exact generic_elimination a b c h

#print axioms generic_elimination
#print axioms required_type
#print axioms Real.commRing
#print axioms Real.ofCauchy_add
#print axioms Real.cauchy_mul
#print axioms Real.ext_cauchy
#print axioms target

end TwoRoundTrial
