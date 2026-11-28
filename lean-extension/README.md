# Glitnir Lean extension pilot

This optional Lean 4.35.0 package adds one tactic form:

```lean
import Glitnir

theorem example (P Q : Prop) (p : P) (h : P → Q) : Q := by
  explain "derive-q" "Apply the implication to obtain Q." =>
    have q : Q := h p
  exact q
```

`explain ID MARKDOWN =>` runs its body as an ordinary tactic sequence, once.
The first string is a stable ID within the theorem. The second is authored
Markdown. The syntax emits trace metadata; it does not check, save, or certify
the theorem. The Glitnir backend's independent project check remains the only
source of a proof status.

For this pilot, put the package inside a Lake project's root (for example in a
`glitnir_extension/` subdirectory) and add a local dependency:

```toml
[[require]]
name = "glitnir_extension"
path = "glitnir_extension"
```

Then run `lake build` and import `Glitnir` in the annotated module. The
checked-in [`Examples/Counter.lean`](Examples/Counter.lean) adapts an existing
notebook proof: its two explanations make the recurrence fact and the use of
the induction hypothesis visible in Glitnir's reading view.

The preview shows unambiguous authored prose from saved source immediately.
An explicit proof-step recording can add the exact Lean goal context and, in
the one-goal case, one newly introduced named local proposition. These are
observations, not proof results. The pilot keeps observations in preview memory;
static exports keep the prose but omit goal observations. Package distribution,
saved semantic records, nested groups, and cross-machine identity remain later
release decisions.
