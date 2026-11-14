module

public section

namespace Counter

/-- A counter starting at zero and adding two ticks each round. -/
def ticks : Nat → Nat
  | 0 => 0
  | n + 1 => ticks n + 2

/-- One additional round contributes exactly two ticks. -/
theorem ticks_step (n : Nat) : ticks (n + 1) = ticks n + 2 := by
  rfl

/-- Increasing both copies of n contributes exactly two. -/
theorem double_step : ∀ n : Nat, (n + 1) + (n + 1) = (n + n) + 2 := by
  intro n
  omega

/-- The recursive counter is twice the number of rounds. -/
theorem ticks_eq_double : ∀ n : Nat, ticks n = n + n := by
  intro n
  induction n with
  | zero => rfl
  | succ n ih =>
    calc
      ticks (n + 1) = ticks n + 2 := ticks_step n
      _ = (n + n) + 2 := congrArg (fun k => k + 2) ih
      _ = (n + 1) + (n + 1) := (double_step n).symm

end Counter
