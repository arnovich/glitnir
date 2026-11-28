# Lean extension pilot

**Baseline:** Glitnir `main` at `bb2088438f6265bcada92ced9323bed8dc8848c3`
(the same source tree as this branch's `efa8f6c`), Lean 4.35.0.

## Purpose

Give Glitnir an optional Lean package for explanations attached to proof steps.
The first result should help a reader find an intermediate fact, its assumptions,
and the associated proof source in one nontrivial notebook. Lean elaboration
supplies semantic links; the existing sandboxed project check remains the only
authority for proof status.

This branch covers the feasibility spike and a narrow reader pilot. Portable
saved observations, nested groups, static export of goals, and a general CLI
contract need a separate release decision after the pilot.

## Existing seams

- `backend/src/glitnir/lean/ProofTrace.lean` gathers tactic information trees.
  `proof_trace.py` runs it on explicit request against read-only live project
  dependencies, and `service/proof_steps.py` keeps one result in memory.
- `observer.py` compiles helpers with `LEAN_PATH=""`; the target project's
  modules are loaded later by each helper. A custom payload type cannot simply
  be imported at the top of `ProofTrace.lean`.
- `notebook/document.py` supplies fast source cells. `preview/snapshot.py` and
  `preview/app` supply the canonical renderer. Existing notebooks need no import.
- `notebook/check_records.py` owns `.glitnir.json`; its check results must never
  be inferred from explanation metadata.

## Milestones and gates

### 1. Package and transport spike

Create a small companion Lean package in this repository and a clean Lake
fixture that imports it. Write a failing live test first for the two-implication
proof: a fact introduced in one annotated block remains available in the next.
Prove how `ProofTrace.lean` can decode a typed annotation payload from that same
package under the existing helper sandbox and packaging checks. Check an
annotated theorem through the normal project checker. Keep the prototype's
metadata out of saved records.

**Gate:** normal Lake build, trace extraction, and independent check all load
the intended package. The helper must not re-execute a tactic to collect its
metadata, grant a proof status, or relax the sandbox. If the typed transport
cannot be made reliable, revise the transport before UI work.

### 2. Reader pilot

Expose a deliberately constrained, source-readable annotation form with a
stable ID and Markdown string. Extend the fast notebook projection to show
unambiguous authored text immediately, without claiming Lean semantics. Extend
the trace contract and one canonical renderer path for flat groups, a simple
named local `have`, before/after goals, and source navigation. Request semantic
inspection explicitly; keep its result in preview memory for this pilot.

**Gate:** a representative existing proof is clearer to read: a reader can
identify its key intermediate fact, assumptions, and source step from the
explanation view. A failed or unavailable trace leaves authored prose readable.
Proof status still comes only from the independent check. Existing notebooks
render without importing the new package.

### 3. Release decision

Review the compiled spike, reader comparison, authoring friction, runtime cost,
and compatibility. Continue only with evidence that the semantic layer improves
more than one notebook and is viable as a reusable extension point.

If continued, plan separate changes for a portable observation identity,
bounded saved records and cross-writer retention, nesting and repeated
executions, static and linked rendering, package distribution, and
machine-readable access. The observation's source, environment, and producer
identity must be distinct from the existing check verdict and fingerprint.

## Validation

- Follow the repository's live Lean test gate for the package, helper, trace,
  and independent checker. Verify the helper produces no Lean output.
- Run relevant backend tests and its black, isort, flake8, and mypy checks for
  changed Python. Run renderer and extension packaging checks for UI changes.
- Compare a real annotated notebook with its current Glitnir presentation.
- Review the completed pilot with Lean-correctness, integration, and reader-UX
  judges before a release decision.

## Progress

- [x] Confirmed the current repository seams and Lean 4.35.0 baseline.
- [x] Package and transport spike: a clean local Lake dependency builds; the
  helper decodes a version-tagged `Json` held in `CustomInfo` without changing
  its toolchain-only import path; the existing sandboxed trace and independent
  checker pass on the two-implication proof. Live test:
  `backend/tests/test_lean_extension.py`.
- [x] Reader pilot: Counter example, source projection, typed local proposition,
  current goal observation, and static export boundary.
- [ ] Broader release decision: another notebook and distribution/provenance
  work are needed before calling this a general extension setup.

## Pilot evidence and decision boundary

The reader pilot is implemented on this branch. `Examples/Counter.lean` adapts
the existing `Counter.ticks_eq_double` induction proof. The original reading
view presents the theorem and its proof source fold; finding the recurrence
equation and where the induction hypothesis applies requires reading the `calc`
block. The annotated version adds two authored explanation cards from the saved
source. A current explicit Lean observation attaches the typed local proposition
`hStep : ticks (n + 1) = ticks n + 2`, the before/after goals, and source links.
The check status remains separate. This costs two annotation wrappers and an
explicit `have` in the proof.

The normal Lake build, trace extraction, and independent check passed for the
two-implication scope fixture and the adapted Counter proof. Counter's three
theorems were independently checked. The live integration test covers both;
the snapshot test confirms the two cards appear before a Lean run; the renderer
test covers the initial, observed, linked, and static-export states. The cold
live integration test took about 203 seconds on the local macOS toolchain,
including package build, two traces, and two independent checks; it is not an
isolated UI or trace timing measurement.

This is evidence of readability in one representative proof, not yet of value
across more than one notebook. The broader release gate remains open. Before
shipping the extension as a general authoring setup, repeat the comparison on
another proof and settle package distribution, portable observation identity,
bounded persistence, and behavior for nested or repeated annotations.
