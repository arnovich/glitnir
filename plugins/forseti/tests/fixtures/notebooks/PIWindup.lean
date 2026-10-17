import Mathlib
import Gimle.Forseti.Discrete
import Gimle.Asgard.Compile.Polynomial

/-!
# Can a fixed-point PI controller wind up?

An embedded PI controller keeps its integral term in a fixed-point register. If
a disturbance keeps pushing, the accumulator grows until it overflows and wraps
around, and the controller then drives the plant the wrong way: integrator
windup. Testing cannot rule it out, because it depends on the whole input
history. This notebook proves it cannot happen, for **every** admitted
reference and disturbance sequence, over the unit's whole lifetime.

## The loop

One clock tick updates the plant state `x` and the accumulator `q` together:

$$ x_{k+1} = ½\,x_k + ½\,(u_k + d_k + θ) $$
$$ q_{k+1} = ¾\,q_k + (r_k − x_k) $$
$$ u_k = ½\,(r_k − x_k) + ¼\,q_k $$

The reference `r` and the disturbance `d` may change every tick within
`[−1, 1]`; the actuator bias `θ` is fixed per unit within `[−¼, ¼]`.

**What is modelled.** The theorems are about this recurrence computed exactly,
over the reals. The coefficients are dyadic, so a fixed-point unit stores them
exactly, but it still rounds every product to its word length each tick, and
that rounding is not modelled here. The invariant below has no slack on the
`q` face, so a rounding implementation needs its own argument: the room between
the invariant and the registers is not proved to absorb it.
-/

namespace Notebook.PIWindup
open Gimle.Asgard Gimle.Asgard.Polynomial Gimle.Forseti.Discrete

/-! ## The model

The update reads its ports in the order `[x, q, r, d, θ]`: old state, this
tick's inputs, then the fixed parameter. It is compiled to an Asgard circuit
from the equations, with a theorem that the circuit computes them. -/

/-- The ports, by name. -/
def x : Expr 5 := .var 0
def q : Expr 5 := .var 1
def r : Expr 5 := .var 2
def d : Expr 5 := .var 3
def θ : Expr 5 := .var 4

/-- The control law `u = ½(r − x) + ¼q`. -/
def control : Expr 5 :=
  .add (.mul (.constant (1/2)) (.add r (.neg x))) (.mul (.constant (1/4)) q)

/-- The plant, `x⁺ = ½x + ½(u + d + θ)`. -/
def plant : Expr 5 :=
  .add (.mul (.constant (1/2)) x) (.mul (.constant (1/2)) (.add (.add control d) θ))

/-- The leaky accumulator, `q⁺ = ¾q + (r − x)`. -/
def integrator : Expr 5 :=
  .add (.mul (.constant (3/4)) q) (.add r (.neg x))

/-- One tick of the closed loop, as an Asgard circuit. -/
def update : Circuit (2 + (2 + 1)) 2 := compileOutputs ![plant, integrator]

-- @evidence iterate update (state := 1, 1) (inputs := 1, -1; 1, 1; 1, 1; 1, 1; -1, 1; -1, 1; -1, 1; -1, 1) (parameter := 1/4) (steps := 200) (names := x, q, r, d, θ)

/-! The run above is the harshest disturbance found by trying every bang-bang
pattern of reference and disturbance up to period 8: the accumulator still
peaks near `|q| ≈ 3.9`. It is evidence, not proof. A cleverer sequence might do
worse, and no simulation can try them all. The proof below covers every
sequence, and its box is wider than any run reaches because it must hold from
every point in it, not only the ones a run visits. -/

/-! ## The sets

Every set is a box. `I` is where the unit starts, `S` is what the fixed-point
registers can hold, and `K` is the invariant: the claim is that the state
never leaves `K`, and `K` fits inside `S`. -/

/-- Initial states: `x, q ∈ [−1, 1]`. -/
def initial : Box 2 := ⟨![-1, -1], ![1, 1], by intro i; fin_cases i <;> norm_num⟩
/-- The invariant: `x ∈ [−6, 6]`, `q ∈ [−28, 28]`. -/
def invariant : Box 2 := ⟨![-6, -28], ![6, 28], by intro i; fin_cases i <;> norm_num⟩
/-- The register ranges: `x ∈ [−8, 8]`, `q ∈ [−32, 32]`. -/
def registers : Box 2 := ⟨![-8, -32], ![8, 32], by intro i; fin_cases i <;> norm_num⟩
/-- Admitted inputs: `r, d ∈ [−1, 1]`, changing freely every tick. -/
def inputs : Box 2 := ⟨![-1, -1], ![1, 1], by intro i; fin_cases i <;> norm_num⟩
/-- Admitted bias: `θ ∈ [−¼, ¼]`. -/
def bias : Box 1 := ⟨![-1/4], ![1/4], by intro i; fin_cases i; norm_num⟩

/-! ## The proof

Three inclusions carry the whole argument. -/

/-- The circuit computes the substituted affine update. -/
theorem update_step (s w p : Point _) :
    step update p s w = ![s 0 / 4 + s 1 / 8 + w 0 / 4 + w 1 / 2 + p 0 / 2,
      3 / 4 * s 1 + w 0 - s 0] := by
  funext i
  fin_cases i <;>
    simp [step, update, plant, integrator, control, x, q, r, d, θ, Expr.eval,
      pointAppend] <;> ring

/-- Every initial state is inside the invariant. -/
theorem starts_inside : ∀ s, initial.Contains s → invariant.Contains s := by
  intro s h i
  have h0 := h 0
  have h1 := h 1
  fin_cases i <;> simp_all [initial, invariant, Box.Contains] <;> constructor <;> linarith

/-- One tick keeps the state inside the invariant, for every admitted input
and bias. -/
theorem stays_inside : ∀ s w p, invariant.Contains s → inputs.Contains w →
    bias.Contains p → invariant.Contains (step update p s w) := by
  intro s w p hs hw hp i
  have s0 := hs 0
  have s1 := hs 1
  have w0 := hw 0
  have w1 := hw 1
  have p0 := hp 0
  simp only [invariant, inputs, bias] at s0 s1 w0 w1 p0
  norm_num at s0 s1 w0 w1 p0
  rw [update_step]
  fin_cases i <;> simp [invariant] <;> constructor <;> linarith

/-- The invariant fits inside the registers. -/
theorem within_registers : ∀ s, invariant.Contains s → registers.Contains s := by
  intro s h i
  have h0 := h 0
  have h1 := h 1
  fin_cases i <;> simp_all [invariant, registers, Box.Contains] <;> constructor <;> linarith

/-- **The accumulator never overflows.** For every bias in range, every start
in `I` and every admitted input sequence, the exact loop has exactly one run,
and every state of it fits the registers. -/
theorem never_overflows :
    AllStepSafety update initial.Contains registers.Contains inputs.Contains
      bias.Contains := by
  exact certificate_sound update _ invariant.Contains _ _ _ inputs.nonempty
    starts_inside stays_inside within_registers

/-!
## Gain tolerance

A real unit's proportional gain is not exactly ½: component tolerance puts it
anywhere in `[0.45, 0.55]`. The gain multiplies the state, so the update is no
longer affine in what is uncertain. Forseti's earlier certificate checker only
accepted affine combinations of the box bounds, so it could not state this
case. A Lean proof has no such restriction: `nlinarith` multiplies the bounds
itself.

The same box `K` still works, with no margin left: the worst case,
`x = 6, r = 1` at gain `0.45`, lands exactly on `x⁺ = 6`.
-/

namespace Gain

/-- The ports, now with the gain `k` as a second fixed parameter. -/
def x : Expr 6 := .var 0
def q : Expr 6 := .var 1
def r : Expr 6 := .var 2
def d : Expr 6 := .var 3
def θ : Expr 6 := .var 4
def k : Expr 6 := .var 5

/-- The control law `u = k(r − x) + ¼q`. -/
def control : Expr 6 :=
  .add (.mul k (.add r (.neg x))) (.mul (.constant (1/4)) q)

def plant : Expr 6 :=
  .add (.mul (.constant (1/2)) x) (.mul (.constant (1/2)) (.add (.add control d) θ))

def integrator : Expr 6 :=
  .add (.mul (.constant (3/4)) q) (.add r (.neg x))

/-- One tick with an uncertain gain. -/
def update : Circuit (2 + (2 + 2)) 2 := compileOutputs ![plant, integrator]

/-- Admitted parameters: bias `θ ∈ [−¼, ¼]` and gain `k ∈ [0.45, 0.55]`. -/
def parameters : Box 2 :=
  ⟨![-1/4, 9/20], ![1/4, 11/20], by intro i; fin_cases i <;> norm_num⟩

end Gain

/-- The circuit computes the update, now bilinear in the gain. -/
theorem gain_step (s w p : Point _) :
    step Gain.update p s w = ![s 0 / 2 + (p 1 * (w 0 - s 0) + s 1 / 4 + w 1 + p 0) / 2,
      3 / 4 * s 1 + w 0 - s 0] := by
  funext i
  fin_cases i <;>
    simp [step, Gain.update, Gain.plant, Gain.integrator, Gain.control, Gain.x, Gain.q,
      Gain.r, Gain.d, Gain.θ, Gain.k, Expr.eval, pointAppend] <;> ring

/-- One tick keeps the state inside the same invariant, for every gain in
tolerance. -/
theorem stays_inside_with_gain : ∀ s w p, invariant.Contains s →
    inputs.Contains w → Gain.parameters.Contains p →
    invariant.Contains (step Gain.update p s w) := by
  intro s w p hs hw hp i
  have s0 := hs 0
  have s1 := hs 1
  have w0 := hw 0
  have w1 := hw 1
  have p0 := hp 0
  have p1 := hp 1
  simp only [invariant, inputs, Gain.parameters] at s0 s1 w0 w1 p0 p1
  norm_num at s0 s1 w0 w1 p0 p1
  rw [gain_step]
  fin_cases i <;> simp [invariant] <;> constructor <;>
    nlinarith [mul_nonneg (sub_nonneg.2 p1.1) (sub_nonneg.2 s0.2),
      mul_nonneg (sub_nonneg.2 p1.1) (neg_le_iff_add_nonneg.1 s0.1),
      mul_nonneg (sub_nonneg.2 p1.2) (sub_nonneg.2 s0.2),
      mul_nonneg (sub_nonneg.2 p1.2) (neg_le_iff_add_nonneg.1 s0.1),
      mul_nonneg (sub_nonneg.2 p1.1) (sub_nonneg.2 w0.2),
      mul_nonneg (sub_nonneg.2 p1.1) (neg_le_iff_add_nonneg.1 w0.1),
      mul_nonneg (sub_nonneg.2 p1.2) (sub_nonneg.2 w0.2),
      mul_nonneg (sub_nonneg.2 p1.2) (neg_le_iff_add_nonneg.1 w0.1)]

/-- **Windup is ruled out across the gain tolerance too.** -/
theorem never_overflows_with_gain :
    AllStepSafety Gain.update initial.Contains registers.Contains inputs.Contains
      Gain.parameters.Contains := by
  exact certificate_sound Gain.update _ invariant.Contains _ _ _ inputs.nonempty
    starts_inside stays_inside_with_gain within_registers

end Notebook.PIWindup
