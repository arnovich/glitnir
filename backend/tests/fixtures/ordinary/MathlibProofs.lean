import Mathlib

def s : ℕ → ℕ
  | 0 => 1
  | n + 1 => 2 * s n

theorem c_ring (x y : ℝ) : (x + y) ^ 2 = x ^ 2 + 2 * x * y + y ^ 2 := by
  ring

theorem c_linarith (x : ℝ) (h : 0 < x) : 0 < 2 * x + 1 := by
  linarith

theorem c_normnum : s 3 = 8 := by
  norm_num [s]

theorem c_aesop (p q : Prop) (hp : p) (hq : q) : p ∧ q := by
  aesop

theorem c_ext : (fun x : ℕ => x + 0) = id := by
  ext x
  simp

theorem c_positivity (x : ℝ) : 0 ≤ x ^ 2 + 1 := by
  positivity

theorem c_induction (n : ℕ) : 0 < s n := by
  induction n with
  | zero => simp [s]
  | succ k ih => simp [s, ih]
