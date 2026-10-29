import Mathlib.Basic.Real.Basic
import Mathlib.Tactic.Ring

namespace TwoRoundTrial

theorem cauchy_elimination
    (a b c : CauSeq.Completion.Cauchy (abs : ℚ → ℚ))
    (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  have hc : c = -(a + b) := by
    calc
      c = (a + b + c) - (a + b) := by ring
      _ = -(a + b) := by rw [h]; ring
  rw [hc]
  ring

theorem target (a b c : ℝ) (h : a + b + c = 0) :
    a ^ 3 + b ^ 3 + c ^ 3 = 3 * a * b * c := by
  let φ := Real.ringEquivCauchy
  have mapped_h : φ a + φ b + φ c = 0 := by
    simpa only [map_add, map_zero] using congrArg φ h
  have mapped_result := cauchy_elimination (φ a) (φ b) (φ c) mapped_h
  apply φ.injective
  simpa only [map_add, map_mul, map_pow, map_ofNat] using mapped_result

#print axioms cauchy_elimination
#print axioms Real.ringEquivCauchy
#print axioms target

end TwoRoundTrial
