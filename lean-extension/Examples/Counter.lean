import Glitnir

/-!
# Counter: an annotated induction step

This pilot adapts `Counter.ticks_eq_double` from the existing notebook example.
The first explanation names the local recurrence fact. The second applies the
induction hypothesis and arithmetic step. The theorem remains an ordinary
Lean theorem; Glitnir's independent checker decides its status.
-/

namespace Counter

def ticks : Nat → Nat
  | 0 => 0
  | n + 1 => ticks n + 2

theorem ticks_step (n : Nat) : ticks (n + 1) = ticks n + 2 := by
  rfl

theorem double_step : ∀ n : Nat, (n + 1) + (n + 1) = (n + n) + 2 := by
  intro n
  omega

theorem ticks_eq_double : ∀ n : Nat, ticks n = n + n := by
  intro n
  induction n with
  | zero => rfl
  | succ n ih =>
    explain "recurrence" "The recursive definition supplies the successor equation." =>
      have hStep : ticks (n + 1) = ticks n + 2 := ticks_step n
    explain "induction-step" "Rewrite with the recurrence, apply the induction hypothesis, then rearrange the sum." =>
      calc
        ticks (n + 1) = ticks n + 2 := hStep
        _ = (n + n) + 2 := congrArg (fun k => k + 2) ih
        _ = (n + 1) + (n + 1) := (double_step n).symm

end Counter
