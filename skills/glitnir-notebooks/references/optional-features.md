# Optional notebook features

Inspect a working example using the same project and runtime before adding one
of these features. Preserve existing directives.

## Images and links

Put local images under `assets/` beside the notebook and refer to them from prose
as `![caption](assets/figure.png)`, on a line of its own. Only PNG, JPEG, GIF,
WebP and SVG files of at most 10 MB inside `assets/` are shown; check that the
preview shows each image rather than a missing, invalid or changed note. Images
illustrate and never change a check, so do not rely on a figure to carry an
argument.
`[Another notebook](Another.lean)` is a prose link for navigation, not a Lean
import. To use another notebook's results, `import` it as an ordinary Lean module
of the same Lake project and build it first.

## Views

Standalone Lean comment lines can request views. Views come from optional
provider packages found through the `glitnir.providers` entry point.
`glitnir-forseti` draws `circuit`, `model` and `dynamics` views when the package
is installed in the same Python environment as the Glitnir backend and the
notebook is in a Lake project whose packages build `Gimle.Forseti` and
`Gimle.Asgard.Visualization.Model`. The notebook need not import the
visualization module: the view loads it from the project's build. Without these,
the view reports an error instead of drawing, naming any `lake build` to run.

```lean
-- @view model myModel
-- @view circuit myCircuit
```

The named object must exist in the notebook's preceding Lean context and have
the supported type; do not infer that any function can be drawn as a circuit.
`using first, second, whole` after a circuit view attaches supported closed
Hoare declarations. Use existing checked declarations rather than invented labels.
A view is a picture of an object, never evidence that a claim holds.

## Simulation evidence

`-- @evidence <verb> <target> (options)` on a line of its own requests a simulation
of an object defined earlier. It shows as a card with a chart (and a phase plane
if asked), provenance and a status: Never run, Running, Current or Out of date.
Nothing runs when the preview opens. The user presses **Run** on the card: one run
at a time. Lean runs in the sandbox; the provider computes from its output. The
result is saved in `<Notebook>.glitnir.json` under `evidence`, kept when checking,
and goes out of date when anything above the directive or the environment changes.
If the file would exceed 16 MiB or the existing file cannot be read, the run still
happens but its result is not saved; the card shows why. Never hand-edit it.

`glitnir-forseti` provides three verbs: `simulate`, `iterate` and `sample`. They
load `Gimle.Asgard.Compile.RationalInterchange` and `Gimle.Asgard.Model.Continuous`
from the project's build; the notebook need not import them. `sample` also needs
the notebook to import `Gimle.Forseti.Examples.HedgedSpreadStepped`, which
defines the step it samples. Without Python Asgard, `iterate` cards say it is
unavailable and cannot run.

```lean
-- @evidence simulate dissipative (h := 1/100) (steps := 400) (solver := rk4) (plane := x, z)
-- @evidence iterate update (state := 1, 1) (inputs := 1, -1; 1, 1) (parameter := 1/4) (steps := 200)
```

`simulate` takes `h`, `steps`, `solver` (`euler` or `rk4`) and `plane`; it needs no
extra package. `iterate` takes `state`, `inputs`, `parameter`, `steps` and `names`
and needs Python Asgard (`gimle-asgard`, installed separately into the backend).
`sample` takes `law` (`gaussian`, `twopoint` or `threepoint`), `variance` and
`observe`, plus `paths` (at most 100000), `steps`, `seed`, `barrier` and `show`,
and needs no extra package. Numbers are plain decimals or
fractions of at most 40 digits per part; write `1/100` or `0.01`, not `1e-2`.
Without a provider the line shows why, and saved results stay viewable. A bad
option is reported on the card. CI skips the plugin's live Lean tests, so Forseti
evidence is exercised end to end only by the opt-in
`plugins/forseti/tests/test_evidence_provider_lean.py`.

Evidence is never proof. It changes no claim's status and not the check exit code,
so state what a claim proves from Lean alone and describe a run as a numerical
observation. Add a directive only on request; leave existing ones and saved
results untouched.

## Decisions

`-- @decide <name> : <goal>` on a line of its own, with continuation lines `--`
plus at least two spaces, asks a decision provider for a Lean proof or refutation
of the goal. `glitnir-forseti` decides kinds `entailment`, `trajectory`, `stream`
and `stepped`; it needs `glitnir-forseti[z3]`, and the notebook must import the
Forseti/Asgard modules the goal uses (a missing one shows as an unknown-identifier
rejection on the card). Forseti `entailment`, `trajectory` and `stepped` decision
blocks and the `simulate` and `sample` cards pass Glitnir's check in the plugin's
live tests against Forseti Lean v0.4.0 on Lean 4.35.0, the pinned release (Linux
CI, 2026-10-22); `stream` decisions have not been checked through Glitnir yet,
so do not present them as working. `stepped` decisions and `sample` need a
Forseti Lean build that contains `Gimle.Forseti.Stepped` and
`Gimle.Forseti.Examples.HedgedSpreadStepped`, as Forseti Lean v0.4.0 does. The first check of a Forseti
project takes about 18 minutes (a one-time import replay); later checks take a
few minutes.

```lean
-- @decide disk : entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1
-- @decide energy_band : trajectory three-state from (1, 2, -1) at 2
--   observe V always : V >= 0 and V <= 6
```

Nothing runs when the preview opens. **Run** asks the provider (one provider run
at a time, evidence included); Glitnir then checks the saved text up to the
directive plus the proposed block with its own sandboxed kernel check, the only
proof authority. Status is Never run, Running, Unknown, Rejected, Candidate,
Accepted or Out of date; the card shows the stages, the kernel statement
(Lean's form, e.g. `Eq (instHAdd.hAdd 2 2) 4`), axioms and the full block. A proof
declares `theorem <name>`; a refutation `theorem <name>_refuted : ¬ (...)`. The
provider is trusted only for matching the theorem to the goal, so read the
statement before accepting.

**Accept** (VS Code preview only, for a checked candidate) inserts the block right
after the directive through the editor and saves; the backend never writes the
`.lean` file. It is refused while the notebook has unsaved edits or the editor's
text is not the saved file the backend reads. Saved edits below the directive keep
the candidate acceptable; an edit above it, or an environment change, makes it Out
of date until it is run again. **Discard** removes the saved result; **Stop**
cancels a run. Results are saved in `<Notebook>.glitnir.json` under `decisions`;
never hand-edit it.

As an agent you may ask the user to Run and Accept, or write the proof directly
in Lean; either way a decision never replaces checking, so run the project's Lean
validation and **Check saved file** afterwards. Add a directive only on request;
leave existing ones, accepted blocks and saved results untouched. Never invent an
artifact or paste a status into metadata.
