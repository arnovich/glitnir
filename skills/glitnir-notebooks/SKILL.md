---
name: glitnir-notebooks
description: Write or revise Lean notebooks for Glitnir, combining mathematical prose, definitions, and checked mathematical claims, or write a guided walkthrough of an existing Lean formalization and its paper. Use for notebook authoring, converting a Lean file for Glitnir, or writing or extending a walkthrough of an existing project; not for extension development or unrelated Lean proof work.
---

# Author Glitnir notebooks

**Use when:** the user wants a mathematical notebook displayed by Glitnir,
or wants to edit or convert an existing one (the sections below); or wants a
guided walkthrough of an existing Lean formalization and its paper, or to
extend one (see "Walk through an existing formalization"). Converting changes
the user's file into a notebook; a walkthrough leaves the original untouched
and restates its theorems in a separate notebook.
**Don't use:** for ordinary Lean changes with no notebook requirement, VS Code
extension implementation, annotating or rewriting an existing project's files,
or creating skills (use skill-creator for that).

Write a readable mathematical argument in a normal `.lean` file. The source is
authoritative; Glitnir renders it and reports checking observations.
Keep the user's mathematical claims and assumptions intact when converting code.

## Establish the environment

Inspect the file's imports, nearby notebooks, `lean-toolchain` and Lake files.
Reuse the project's existing dependencies and conventions. Do not add Mathlib,
Asgard or Forseti imports merely to make a file look like a notebook.

Glitnir checks a file with its prepared Lake modules, or the supported
standard library for a loose file. The checker supports Lean 4.35.0.
Checks are explicit, whole-file operations; compiled propositions and assumptions
appear separately from source. Results are saved beside the notebook; any edit
above a claim marks its result out of date, and if a command fails only the
claims from it onward are withheld. Term proofs and fully qualified theorem names are
supported. An `example` whose type is a proposition is checked as a private
theorem and shown as "example (line N)" with the statement that was checked; it
may have a docstring but no attributes or other modifiers, must not use
`decl_name%`, and its section variables are included as for a theorem (only those
its statement mentions, plus instances), so state any hypothesis its proof uses.
An example without a type, or whose type is not a proposition, claims nothing.
Prefer named theorems in files with interpolated strings, where examples cannot
be located exactly. Do not leave stray carriage returns in a notebook.
`#print axioms name` lines are accepted and only print; the checker's own axiom
lists decide statuses. Do not print a lemma Lean generates on demand (such as
`f.eq_1`). Other `#` commands (`#check`, `#eval`, other `#print` forms) and
unfamiliar command forms are currently unsupported by independent checking: the
check stops at them, though they remain visible as source.

For incompatible imports, toolchains or configuration, report the limitation
instead of changing mathematics, pins or dependencies to obtain a green status. Prepared Git dependencies use their actual local contents;
Glitnir does not verify their declared Git revision or working-tree cleanliness.
Configuration that needs subprocesses remains unavailable on macOS.

## Write the notebook

- Put prose in column-zero `/-! … -/` blocks. Use Markdown headings and `$…$`
  or `$$…$$` for mathematics. Explain assumptions and distinguish proved facts
  from intuition and future goals.
- Keep imports at the start. Use ordinary Lean definitions and namespaces;
  `/-- … -/` docstrings explain definitions and claims.
- Preserve ordinary Lean theorem syntax and namespaces. Use named theorems
  for independently reported observations.
- Place a claim's docstring immediately above its declaration. Keep longer
  explanations in separate prose blocks. Definitions before a claim form its
  context; avoid depending on declarations introduced later.
- Preserve deliberate open or refuted examples. `sorry` means unfinished work,
  never success. Do not weaken a statement, add an axiom or conceal a missing
  proof merely to improve its displayed status.

This minimal notebook uses only Lean's standard environment:

```lean
/-!
# Adding zero

The claim below applies to every natural number.
-/

namespace Notebook.AddZero

/-- Adding zero on the right preserves a natural number. -/
theorem add_zero (n : Nat) : n + 0 = n := by
  rfl

end Notebook.AddZero
```

Any syntax that cannot be represented safely should remain visible as source.
Do not silently drop declarations or rewrite complex Lean syntax just to render
it. Report parser limitations separately from Lean errors.

## Write an expandable argument

For longer arguments, lead with the result, its scope and a short proof strategy.
Organize the explanation around meaningful intermediate conclusions. For each
step, explain the assumptions it uses, what it establishes, and how that advances
the enclosing argument. Extract named lemmas when they clarify the mathematics;
avoid splitting a proof merely to create more sections. Preserve theorem
statements and check any refactoring in Lean.

Glitnir supports opt-in collapsible sections in prose comments:

```lean
/-!
## Establish the bound {fold}

Damping gives the upper bound; a square gives the lower bound.

### Show the energy cannot increase {fold}

For every state, the energy derivative is nonpositive because the damping
coefficient is positive. This supplies the upper-bound argument.

Explain the calculation here, followed by its supporting Lean declarations.
-/
```

Use heading levels 2–6; deeper headings nest within the enclosing section. A
section includes subsequent prose and Lean cells until a heading of equal or
lower level, or the end of the file. Put each heading on its own line. Only a
trailing ` {fold}` opts into collapse; ordinary headings stay expanded. The first
paragraph immediately after the heading remains visible as the summary. Keep it
short and self-contained, with important assumptions or limitations visible.
Place detailed reasoning in later paragraphs and the supporting declarations
below them. No closing marker, HTML or change to Lean syntax is needed.

The renderer counts descendant claim observations on the closed section; these
counts are not a verification of the explanation or a combined proof verdict.
The Lean source/proof within each card remains separately expandable. Static
exports retain expandable sections but start them open. Older renderers show the
marker literally and all content expanded; folding requires a supporting Glitnir
runtime. Inspect both the closed overview and expanded reasoning, and check that
outline/claim navigation reveals the intended step. Do not describe an informal
bridge as checked merely because nearby lemmas are checked.

## Metadata, links and optional features

Do not manufacture or hand-edit check records, status flags, hashes or kept
decision artifacts. When moving or copying a notebook, bring its assets along;
its `<Notebook>.glitnir.json` results are tied to the file and may simply be
regenerated by checking again.

Importing another notebook is an ordinary Lean import: the imported notebook
must be a module of the same Lake project and built before checking; if it has
unfinished proofs, the importing claim is reported as **admitted** (Contains
sorry). Use prose links for navigation without adding assumptions. Check
dependencies before interpreting downstream statuses. Glitnir never converts
earlier claims into axioms.

Read [references/optional-features.md](references/optional-features.md) only
when the user needs images, circuit/model/dynamics views, simulation evidence or
decisions. Views, `-- @evidence` and `-- @decide` need the optional
`glitnir-forseti` package (or another provider). Ordinary notebooks need none of
these directives.

## Walk through an existing formalization

A walkthrough explains a finished Lean project, often too large to read file by
file, through its paper's argument: a map of the paper's statements and what
each proof uses, and a notebook read from the top in which each key statement is
restated as a claim proved by the project's own theorem. It lives in a folder of
its own beside the repository that contains the original, never inside it, and
never changes the original. Its source of truth is an `outline.json`, which the
bundled helper checks against the project and Glitnir reads directly for the
Graph view and static export. Run it from this skill's folder with Glitnir's
backend Python, the interpreter `glitnir.pythonPath` points to, as
`python -B scripts/walkthrough.py check …` (`-B` writes no bytecode, so the
folder stays unchanged). With no Glitnir backend installed, run
`uv run --no-project --python ">=3.10" python -B scripts/walkthrough.py check …`
instead. The helper needs Python 3.10 or newer, so never use the system Python:
macOS ships 3.9.

Follow [references/walkthroughs.md](references/walkthroughs.md) step by step,
both to write a walkthrough and to extend one, and write the outline as
[references/walkthrough-outline.md](references/walkthrough-outline.md)
describes. Ask before building the original or downloading a paper. Never
describe a claim as checked unless a Glitnir check of the walkthrough reported
it.

## Validate before finishing

Steps 1 and 2 need the notebook's imports built (a loose file needs only Lean),
and step 2 needs the Lean version Glitnir supports. When either is missing, as is usual for a walkthrough
(follow step 6 of [references/walkthroughs.md](references/walkthroughs.md)),
report that step as not run and why.

1. Save the source. Run the project's existing Lean validation, usually
   `lake env lean path/to/Notebook.lean` from its Lake root. Read diagnostics:
   successful elaboration can still include `sorry` or declared axioms.
2. Use **Check saved file** and inspect the full result, compiled
   propositions and assumptions. Checks save results to
   `<Notebook>.glitnir.json`; do not hand-edit it. For headless validation see
   [references/current-runtime.md](references/current-runtime.md).
   A build alone does not refresh notebook check records.
3. Inspect the rendered notebook when preview access is available: prose,
   statement/proof boundaries, source navigation and any requested images and
   views.
4. Report what was checked and any open, conditional, failed, stale or unknown
   results. If the runtime, project or preview is unavailable, state which
   validation could not run; never claim a checked result from source inspection.

Simulation output, including `-- @evidence` cards, is evidence, not proof; it
changes no claim status. A `-- @decide` card never replaces checking: only
the Lean check of the saved file grants a theorem its status. Notebook status records describe
checking observations; they do not themselves grant mathematical authority.
