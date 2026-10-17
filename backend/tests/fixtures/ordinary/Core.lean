def f : Nat → Nat
  | 0 => 0
  | n + 1 => f n + 2

def g (n : Nat) : Nat := if n = 0 then 0 else g (n - 1) + 1
termination_by n

def h (x : Nat) : Nat := match x with
  | 0 => 1
  | _ + 1 => 2

theorem c_simp : f 1 = 2 := by
  simp [f]

theorem c_unfold : g 1 = 1 := by
  unfold g
  simp [g]

theorem c_funind (n : Nat) : f n = 2 * n := by
  fun_induction f n <;> omega

theorem c_split (x : Nat) : h x ≥ 1 := by
  unfold h
  split <;> decide

theorem c_decide : (List.range 10).length = 10 := by
  decide

theorem c_kernel : 2 ^ 10 = 1024 := by
  decide +kernel

theorem c_induct (n : Nat) : g n = n := by
  induction n using g.induct with
  | _ => grind [g]

theorem c_rec (n : Nat) : f n ≥ n := by
  induction n with
  | zero => simp [f]
  | succ k ih => simp only [f]; omega
