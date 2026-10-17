import Gimle.Forseti.Syntax.Certificate

/-! # Prototype: degree-2 Positivstellensatz certificates (task 034)

Task 111's certificate proves `P ⊨ Q` from an identity
`−q = σ₀ + Σᵢ σᵢ · (−pᵢ)` in the *quadratic module* of the antecedent: every
constraint is used alone. `x ≥ 0 ∧ y ≥ 0 ⊨ x·y ≥ 0` has no such identity at any
degree, because `x·y` needs the *product* of two constraints.

This prototype admits pairwise products. A `PsatzCertificate` names pairs
`(i, j)` of antecedent constraints and gives an ordinary `EntailmentCertificate`
against the antecedent *augmented* with `−(pᵢ · pⱼ) ≤ 0`. That constraint holds
wherever `pᵢ ≤ 0` and `pⱼ ≤ 0` do, since `(−pᵢ)(−pⱼ) ≥ 0`. Soundness therefore
reuses `Row.check_sound` unchanged: nothing new is trusted beyond one lemma
about products of nonpositive reals.

It is a prototype for the comparison in `README.md`, checked with
`lake env lean` against the pinned release. The checker the comparison
recommends is filed separately in forseti-lean.
-/

namespace Gimle.Forseti.Prototype.Psatz

open Gimle.Forseti.Syntax

variable {n : Nat}

/-- The derived constraint `−(pᵢ · pⱼ) ≤ 0`; an index out of range reads `0`,
which only weakens the certificate. -/
def product (constraints : List (Gimle.Asgard.Polynomial.Expr n)) (pair : ℕ × ℕ) : Gimle.Asgard.Polynomial.Expr n :=
  .neg (.mul (constraints.getD pair.1 (.constant 0)) (constraints.getD pair.2 (.constant 0)))

/-- The antecedent's constraints followed by the named products. -/
def augment (constraints : List (Gimle.Asgard.Polynomial.Expr n)) (pairs : List (ℕ × ℕ)) :
    List (Gimle.Asgard.Polynomial.Expr n) :=
  constraints ++ pairs.map (product constraints)

theorem getD_nonpositive (constraints : List (Gimle.Asgard.Polynomial.Expr n)) (point : Point n)
    (below : ∀ p ∈ constraints, p.eval point ≤ 0) (i : ℕ) :
    (constraints.getD i (.constant 0)).eval point ≤ 0 := by
  rw [List.getD_eq_getElem?_getD]
  cases h : constraints[i]? with
  | none => simp [Gimle.Asgard.Polynomial.Expr.eval]
  | some p => exact below p (List.mem_of_getElem? h)

theorem augment_nonpositive (constraints : List (Gimle.Asgard.Polynomial.Expr n)) (pairs : List (ℕ × ℕ))
    (point : Point n) (below : ∀ p ∈ constraints, p.eval point ≤ 0) :
    ∀ p ∈ augment constraints pairs, p.eval point ≤ 0 := by
  intro p member
  rcases List.mem_append.mp member with original | derived
  · exact below p original
  · obtain ⟨pair, -, rfl⟩ := List.mem_map.mp derived
    have hi := getD_nonpositive constraints point below pair.1
    have hj := getD_nonpositive constraints point below pair.2
    simp only [product, Gimle.Asgard.Polynomial.Expr.eval]
    nlinarith

/-- Evidence: the products used, and one row per consequent constraint against
the augmented antecedent. -/
structure PsatzCertificate (n : Nat) where
  /-- Pairs `(i, j)` of antecedent constraint indices. -/
  pairs : List (ℕ × ℕ)
  /-- An ordinary certificate over the augmented antecedent. -/
  rows : List (Row n)

def PsatzCertificate.check (goal : Goal)
    (certificate : PsatzCertificate goal.context.dimension) : Bool :=
  match goal.antecedent.nonstrict?, goal.consequent.nonstrict? with
  | some constraints, some consequents =>
      certificate.rows.length == consequents.length &&
        (certificate.rows.zip consequents).all fun pair =>
          pair.1.check (augment constraints certificate.pairs) pair.2
  | _, _ => false

/-- **A Positivstellensatz certificate that checks proves the goal.** -/
theorem PsatzCertificate.sound (goal : Goal)
    (certificate : PsatzCertificate goal.context.dimension)
    (valid : certificate.check goal = true) : goal.Entailment := by
  unfold check at valid
  split at valid
  · rename_i constraints consequents antecedentRead consequentRead
    simp only [Bool.and_eq_true, beq_iff_eq, List.all_eq_true] at valid
    obtain ⟨lengths, rows⟩ := valid
    intro point holds
    rw [Formula.holds_of_nonstrict antecedentRead] at holds
    rw [Formula.holds_of_nonstrict consequentRead]
    intro q member
    obtain ⟨index, bound, rfl⟩ := List.getElem_of_mem member
    have paired : (certificate.rows[index]'(by omega), consequents[index]) ∈
        certificate.rows.zip consequents := by
      rw [List.mem_iff_getElem]
      exact ⟨index, by simp [bound, lengths], by simp⟩
    exact Row.check_sound (rows _ paired) point
      (augment_nonpositive constraints certificate.pairs point holds)
  · cases valid

/-! ## Examples -/

section Examples

open Gimle.Forseti.Syntax.ExprNotation

def plane : Context := ⟨["x", "y"], by decide, by decide, by decide⟩

def x : Gimle.Asgard.Polynomial.Expr 2 := .var ⟨0, by decide⟩
def y : Gimle.Asgard.Polynomial.Expr 2 := .var ⟨1, by decide⟩

/-- The unit disk, 017's first goal, needs no product: `σ₀ = y²`, `σ₁ = 1`. -/
def disk : Goal where
  context := plane
  antecedent := .atom .le (.add (.add (.mul x x) (.mul y y)) (.neg (.constant 1)))
  consequent := .atom .le (.add (.mul x x) (.neg (.constant 1)))

theorem disk_proved : disk.Entailment :=
  PsatzCertificate.sound disk
    ⟨[], [⟨⟨[(1, y)]⟩, [⟨[(1, .constant 1)]⟩]⟩]⟩ (by decide +kernel)

/-- `x ≥ 0 ∧ y ≥ 0 ⊨ x·y ≥ 0`: outside every quadratic-module certificate, and
exactly the product `(−p₀)(−p₁) = x·y`. -/
def productNonneg : Goal where
  context := plane
  antecedent := .and (.atom .ge x) (.atom .ge y)
  consequent := .atom .ge (.mul x y)

theorem productNonneg_proved : productNonneg.Entailment :=
  PsatzCertificate.sound productNonneg
    ⟨[(0, 1)], [⟨⟨[]⟩, [⟨[]⟩, ⟨[]⟩, ⟨[(1, .constant 1)]⟩]⟩]⟩ (by decide +kernel)

/-- A higher-degree certificate: `x ≥ 1 ∧ y ≥ 1 ⊨ x·y ≥ 1`, by
`x·y − 1 = (x−1)(y−1) + (x−1) + (y−1)`. -/
def boxProduct : Goal where
  context := plane
  antecedent := .and (.atom .ge (.add x (.neg (.constant 1))))
    (.atom .ge (.add y (.neg (.constant 1))))
  consequent := .atom .ge (.add (.mul x y) (.neg (.constant 1)))

theorem boxProduct_proved : boxProduct.Entailment :=
  PsatzCertificate.sound boxProduct
    ⟨[(0, 1)], [⟨⟨[]⟩, [⟨[(1, .constant 1)]⟩, ⟨[(1, .constant 1)]⟩,
      ⟨[(1, .constant 1)]⟩]⟩]⟩ (by decide +kernel)

/-- `x² ≤ 1 ∧ y² ≤ 1 ⊨ x·y ≤ 1` needs the off-diagonal square `(x − y)²`:
`1 − x·y = ½(x − y)² + ½(1 − x²) + ½(1 − y²)`. The certificate format already
admits it; 111's diagonal dictionary search does not find it. -/
def unitProduct : Goal where
  context := plane
  antecedent := .and (.atom .le (.add (.mul x x) (.neg (.constant 1))))
    (.atom .le (.add (.mul y y) (.neg (.constant 1))))
  consequent := .atom .le (.add (.mul x y) (.neg (.constant 1)))

theorem unitProduct_proved : unitProduct.Entailment :=
  PsatzCertificate.sound unitProduct
    ⟨[], [⟨⟨[(1 / 2, .add x (.neg y))]⟩, [⟨[(1 / 2, .constant 1)]⟩,
      ⟨[(1 / 2, .constant 1)]⟩]⟩]⟩ (by decide +kernel)

/-- A wrong product certificate is rejected: dropping the product leaves no
identity. -/
example : (PsatzCertificate.check productNonneg
    ⟨[], [⟨⟨[]⟩, [⟨[]⟩, ⟨[(1, .constant 1)]⟩]⟩]⟩) = false := by decide +kernel

end Examples

/--
info: 'Gimle.Forseti.Prototype.Psatz.PsatzCertificate.sound' depends on axioms: [propext, Classical.choice, Quot.sound]
-/
#guard_msgs in #print axioms PsatzCertificate.sound

end Gimle.Forseti.Prototype.Psatz
