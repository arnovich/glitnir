import Counter

/-!
# Two ticks per round

For every natural number $n$, a counter starting at zero and adding two each
round has $n+n$ ticks after $n$ rounds. Zero rounds is included. The reader is
assumed to know induction on natural numbers, but no Lean syntax.

This is a source-only walkthrough of the bundled Counter example by the
[Glitnir contributors](../counter/LICENSE), under the MIT licence. There is
no accompanying paper. The final statement is `Counter.ticks_eq_double` in
`Counter.lean`; no separate trusted statement file exists. The reading route
and support links are curated from that proof. Explore shows the same route.

The strategy is to show that the recursive counter and $n+n$ start at the
same value and gain the same amount each round. Two statements below have
claim cards. The recurrence lemma has an explicit argument and is cited in
prose because the walkthrough helper cannot restate that form. The definition
is also prose-only. These coverage limits do not hide a missing proof.

Opening this file does not check it. Run Glitnir's check to obtain observations
for this copy; the bundled example includes no saved status records. Source
comparison, Lean elaboration and independent checking are separate validations.
-/

namespace CounterWalkthrough

/-!
## What the counter means {fold}

$T(0)=0$ and $T(n+1)=T(n)+2$: the state is a natural-number count, not elapsed
time or a physical measurement.

`Counter.ticks` defines $T$ by these two equations. For example, $T(3)=6$ by
three applications of the recurrence. This calculation illustrates the
model; the theorem quantifies over every natural number, not just this sample.

`Counter.ticks_step (n : Nat)` states the second equation. Its proof is `rfl`:
Lean unfolds the successor clause of the definition, leaving identical sides.
The lemma is public, but its explicit binder is outside the text helper's
supported claim shape. We therefore explain it here without a claim card.

## Why the proposed answer gains two {fold}

Replacing $n$ by $n+1$ in both summands adds two in total:
$(n+1)+(n+1)=(n+n)+2$.

Regroup the sum as $n+1+n+1=n+n+1+1$. This uses associativity and
commutativity of natural-number addition. The source's `Counter.double_step`
uses `omega` to construct a proof of this arithmetic identity. Its tactic
name does not explain the regrouping, and the prose is not a tactic trace.
This elementary arithmetic identity is the boundary of our explanation; we
do not describe the implementation of `omega`.
-/

/-- Both copies of the round count increase by one. -/
theorem arithmetic_bridge : ∀ n : Nat, (n + 1) + (n + 1) = (n + n) + 2 := by
  exact Counter.double_step

/-!
## Close the induction step {fold}

Assume $T(n)=n+n$. The recurrence adds two to the left-hand side; adding two
to the right-hand side gives precisely $(n+1)+(n+1)$ by the arithmetic bridge.

The base case is $T(0)=0=0+0$, directly from the definition. In the successor
case the three equalities are

$$T(n+1)=T(n)+2=(n+n)+2=(n+1)+(n+1).$$

The first equality is `Counter.ticks_step`. The second substitutes the
induction hypothesis inside the function $k\mapsto k+2$; the source expresses
this with `congrArg`. The last uses `Counter.double_step` backwards, which
is why the source writes `.symm`. The direction matters: the intermediate
expression is $(n+n)+2$, while the target is the doubled successor.

Induction now covers every natural number. No positivity assumption is needed,
and no subtraction or division introduces a special case at zero. The claim
below restates `Counter.ticks_eq_double` exactly, with its definition name
fully qualified. It invokes the original theorem; it does not re-prove the
induction inside this notebook or certify the explanatory prose.
-/

/-- The counter equals the sum of two copies of the number of rounds. -/
theorem result : ∀ n : Nat, Counter.ticks n = n + n := by
  exact Counter.ticks_eq_double

end CounterWalkthrough
