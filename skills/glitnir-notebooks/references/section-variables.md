# Section type variables in walkthroughs

**Use when:** a cited theorem gets implicit type parameters from surrounding
`variable` commands. **Don't use:** to infer parameters from proofs, support
section term or instance assumptions, or change the original formalization.
Use the main walkthrough recipe for claims with only declaration-local binders.

## Preserve context rather than infer a telescope

The helper supports section commands of these forms:

```lean
universe u v
variable {α β : Type u}
variable {γ : Sort v}
open Groundwork
```

Each `variable` command must consist solely of implicit type-variable groups:
`Type` or `Sort`, optionally with one named universe. Copy all active supported
commands in their original order, even unused type variables. Put each command
on one line in a dedicated `section … end` around the claim. Copy the original
header, qualifying project names as usual, and forward declaration-local explicit
binders by name. Do not prepend a guessed list of parameters to the header.

The helper compares the active contexts and checks scope restoration at `end`.
Lean then generalizes the theorem in that copied context, including only the
parameters it takes. Adding an unused explicit parameter manually would change
this contract. Names that shadow another context or header binder are refused.
Only a simple `open Namespace` is supported, and its namespace must not depend
on relative resolution inside the original namespace. Opens must match the
source; a notebook cannot introduce an unrelated one.

Section term variables, section instance variables, strict-implicit section
binders, `include`, `omit`, `open scoped`, renamed/hidden opens, and complex
universe expressions remain unsupported. Do not silently discard them. Explain
that theorem in prose, or request support for the concrete case. The helper
remains textual validation: elaborate every wrapper with Lean and independently
check it when its toolchain and command forms are supported. The current
independent reader does not accept explicit `universe` commands: validate those
with ordinary Lean and report independent checking as unavailable.

## Real case: ETH's padding machine

[PadThenWalkthrough.lean](../assets/eth-section/PadThenWalkthrough.lean) and its
[outline](../assets/eth-section/outline.json) explain a support theorem already
cited by the ETH walkthrough's Appendix B route: `PiecesCounted.padThen_runs`.
The source is Anthropic's Apache-2.0 `formal-math` repository at
`16a5ce41304fcdaea4e0be7c132e1c361dc26da5`,
`eth/proof/rates/PiecesCounted/PadThen.lean`, lines 68–72.
The source has `open Groundwork` and `variable {α β : Type}` before the theorem.
The companion copies those commands and preserves both state-size hypotheses.

The proof writes one padding unit per transition and then applies `f`. Its
`N a + 1` bound counts transitions, not constant-cost Turing-machine steps.
The notebook explains the stronger remaining-padding invariant and how the
initial-state theorem follows. It does not promote this local observation into
a new proof of the full ETH result.

To reproduce, use an isolated copy of the existing ETH walkthrough environment
with its prepared dependencies and original Lean 4.33.1 pin. Copy the companion
notebook and outline there; set only `project.root` to that environment's
unchanged `formal-math/eth` source directory. The default relative root is a
placeholder layout for a sibling `formal-math` checkout, not bundled source.
With the skill path and matching backend Python selected:

```sh
python -B scripts/walkthrough.py check OUTLINE --notebook NOTEBOOK --require-provenance
```

From the prepared ETH walkthrough environment, after build authorization:

```sh
lake --keep-toolchain build +PiecesCounted.PadThen
lake --keep-toolchain env lean -DautoImplicit=false -DrelaxedAutoImplicit=false PadThenWalkthrough.lean
```

Keep the original dependency revisions. This example's Lean 4.33.1 differs from
Glitnir's supported 4.35.0: report ordinary Lean elaboration separately, and do
not create independent-check records for it. A successful text or Lean check
says nothing about the prose beyond the claim's formal statement.

The backend regression contains an exact, attributed copy of `PadThen.lean`,
with its Apache license and source digest. That excerpt tests the helper offline;
it is not a substitute for building the actual imported theorem. Separate live
4.35 tests cover scope exit and unused section types through Glitnir's
independent checker; explicit universe commands are tested by Lean elaboration.
