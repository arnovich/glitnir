# glitnir-forseti

An optional provider package for Glitnir. It is found through the
`glitnir.providers` entry point, so install it into the same Python environment
as the Glitnir backend. It is not on PyPI: each Glitnir release has its wheel,
and the repository README's Extensions section explains verifying and installing
it, or building it from a checkout.

**What it provides now:** Forseti circuit, model and dynamics views for
`-- @view circuit|model|dynamics <name>` directives. The notebook must import
`Gimle.Forseti` and `Gimle.Asgard.Visualization.Model`, and sit in a Lake project
with built imports.

It also provides evidence for `-- @evidence simulate|iterate <target>` directives,
shown as cards with a **Run** button; a run only happens when clicked. The notebook
must import `Gimle.Asgard.Compile.RationalInterchange` and
`Gimle.Asgard.Model.Continuous`.

```lean
-- @evidence simulate dissipative (h := 1/100) (steps := 400) (solver := rk4)
-- @evidence iterate update (state := 1, 1) (inputs := 1, -1; 1, 1) (parameter := 1/4) (steps := 200)
```

`simulate` options are `h`, `steps`, `solver` (`euler` or `rk4`) and `plane`;
`iterate` options are `state`, `inputs`, `parameter`, `steps` and `names`. Numbers
are plain decimals or fractions (at most 40 digits per part). `simulate` needs
nothing beyond this package. `iterate` needs Python Asgard (`gimle-asgard`),
which is not on PyPI: install it into the backend's environment from its own
wheel or checkout. Without it the card says so before anything runs. Evidence is never proof and changes no claim's status.

It also decides `-- @decide <name> : <goal>` directives whose goal starts with
`entailment`, `trajectory` or `stream`, in the goal syntax of
`glitnir_forseti.claims.text`:

```lean
-- @decide disk : entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1
-- @decide energy_band : trajectory three-state from (1, 2, -1) at 2
--   observe V always : V >= 0 and V <= 6
```

Deciding needs the `z3` extra (`glitnir-forseti[z3]`); without it the card says
so before anything runs. **Run** searches for a certificate or a counterexample and proposes a self-contained Lean block: a `Decided_<name>`
namespace with the definitions it uses and the theorem `<name>` (a proof) or
`<name>_refuted` (a refutation, stating `¬ (claim)`). The search runs no Lean;
Glitnir checks the block with the project's Lean, and only that check makes it
a proof. The modules a block needs depend on the goal, so the outcome lists
them as its `imports` stage; the notebook must import them (an entailment needs
`Gimle.Forseti.Syntax.Certificate`, or `Gimle.Forseti.Syntax.AlgebraicWitness`
for an irrational counterexample); a missing one shows as the check's
unknown-identifier rejection. The package also contains the deciders' replay
against a pinned Forseti Lean release and its command lines, which are not
reachable from the editor.

**Verified on 2026-10-16** against forseti-lean `46d3a0c` (Lean 4.32.1), the
checkout the release and replay pins are cut from:
`tests/test_trajectory_lean.py` (every example goal, both driven models) and
`tests/test_decision_provider_lean.py` (proven and refuted `entailment` and
`trajectory`, including a driven claim read through the project runner) passed
with `FORSETI_LEAN_TEST_PROJECT` set. `tests/test_evidence_provider_lean.py`
(`simulate`) last passed on 2026-10-15 against `c65dada`. `stream` decisions and
`iterate` evidence are not yet verified; the unit tests run no Lean and CI skips
these live tests, so re-run them after changes.

Development: from the repository root, build it with
`uv build plugins/forseti --wheel --out-dir plugins/forseti/dist`, install the
wheel with `--no-deps` after installing `lark` and `z3-solver==5.1.0.0` (the
`z3` extra's pin), and run its tests
from `plugins/forseti`. See the repository README's Development section.

The live evidence tests (`tests/test_evidence_provider_lean.py`) run `simulate`,
and `iterate` when `gimle.asgard` is installed, end to end through Glitnir's
project runner. They need a built Forseti Lean release checkout, `lake` on
`PATH`, Lean 4.32.1 installed with elan and a working sandbox. They write a
temporary module into the checkout's `Gimle/Forseti/Tests/` and remove it and its
results file afterwards. CI sets no project, so they skip there; `-k "not live"`
deselects them. To run them:

```sh
cd plugins/forseti
FORSETI_LEAN_TEST_PROJECT=/path/to/forseti-lean \
  ../../.venv/bin/python -m pytest -q -rs tests/test_evidence_provider_lean.py
```

The live decision tests (`tests/test_decision_provider_lean.py`) decide a proven
and a refuted entailment and trajectory goal through Glitnir's project runner and
assert that its check accepts each proposed block. They need the same project, `lake`, Lean and
sandbox as the live evidence tests, and Z3. They create a new temporary module
in the checkout's `Gimle/Forseti/Tests/` and remove it and its results file
afterwards; CI sets no project, so they skip there. To run them:

```sh
cd plugins/forseti
FORSETI_LEAN_TEST_PROJECT=/path/to/forseti-lean \
  ../../.venv/bin/python -m pytest -q -rs tests/test_decision_provider_lean.py
```
