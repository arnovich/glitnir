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
if asked), provenance and a status: Never run, Running, Current, Failed or Out of
date.
Nothing runs when the preview opens. The user presses **Run** on the card: one run
at a time. Lean runs in the sandbox; the provider computes from its output. The
result is saved in `<Notebook>.glitnir.json` under `evidence`, kept when checking,
and goes out of date when anything above the directive or the environment changes.
A run that fails in Lean, in the provider, with output that is not evidence, or at
its time limit is saved too, under `evidence_failures`; a later successful run
replaces it. A provider crash is saved as its exception type only. Setup failures
(an unbuilt module, Lake, the sandbox or a missing Python dependency) are only shown.
If the file would exceed 16 MiB or the existing file cannot be read, the run still
happens but its result is not saved; the card shows why. Never hand-edit it.
While the preview stays open, a rerun after editing only the directive's options
reuses Lean's earlier output (same source above, same environment) and takes
seconds; the first run of a directive always runs Lean.

`glitnir-forseti` provides four verbs: `simulate`, `iterate`, `sample` and
`hedge`. They load `Gimle.Asgard.Compile.RationalInterchange` and
`Gimle.Asgard.Model.Continuous` from the project's build; the notebook need not
import them. `sample` also needs the notebook to import
`Gimle.Forseti.Examples.HedgedSpreadStepped`, which defines the step it
samples, and `hedge` the notebook to import `Gimle.Forseti.Hedging`. Without
Python Asgard, `iterate` cards say it is unavailable and cannot run.

```lean
-- @evidence simulate dissipative (h := 1/100) (steps := 400) (solver := rk4) (plane := x, z)
-- @evidence iterate update (state := 1, 1) (inputs := 1, -1; 1, 1) (parameter := 1/4) (steps := 200)
-- @evidence hedge Gimle.Forseti.Hedging.call (strike := 100) (spot := 100) (down := 1/50) (up := 1/50) (steps := 20) (law := uniform) (paths := 4000) (seed := 7)
```

`simulate` takes `h`, `steps`, `solver` (`euler` or `rk4`) and `plane`; it needs no
extra package. `iterate` takes `state`, `inputs`, `parameter`, `steps` and `names`
(one per port: state, then inputs, then parameters; the state names distinct) and needs Python
Asgard (`gimle-asgard`, installed separately into the backend).
`sample` takes `law` (`gaussian`, `twopoint` or `threepoint`), `variance` and
`observe`, plus `paths` (at most 100000), `steps`, `seed`, `barrier` and `show`,
and needs no extra package. `hedge` targets `Gimle.Forseti.Hedging.call`,
`.put` or `.straddle`, requires `strike`, `spot`, `down`, `up` and `steps` (at
most 250), and takes `law` (`uniform`, the default, `twopoint` or `gaussian`,
which requires `sigma`), `paths` (4000, at most 100000), `seed` (7) and, given
together, `against_down` and `against_up`, which draw the paths in another band
than the hedge's, usually a wider one; a run of more than 20 million payoff
evaluations and draws is refused, which with the default 4000 paths allows at
most 69 steps for `uniform` and `twopoint` and 36 for `gaussian`. It needs no
extra package. Moves are simple returns as fractions of the spot: ±2 % a step
is `(down := 1/50) (up := 1/50)` or `0.02`, not `2`. `sigma` is the per-step
standard deviation of the simple return, in the units of `down` and `up` (1/100
is 1 % a step), not an annualised volatility. Its card is the seller's profit
and loss at expiry by quantile (the x axis is the quantile level, not time),
with the premium and the paths that end below zero, and shows no proved number.
Numbers are plain decimals or fractions of at most 40 digits per part; write
`1/100` or `0.01`, not `1e-2`. Without a provider the line shows why, and saved
results stay viewable. A bad option is reported on the card. CI's `plugin-live`
job runs the plugin's live Lean tests in shards against the pinned Forseti Lean
release, so Forseti evidence is exercised end to end there by
`plugins/forseti/tests/test_evidence_provider_lean.py` (and `hedge` by
`test_hedging_lean.py`); locally they skip unless `FORSETI_LEAN_TEST_PROJECT`
names a built Forseti Lean checkout.

Evidence is never proof. It changes no claim's status and not the check exit code,
so state what a claim proves from Lean alone and describe a run as a numerical
observation. Add a directive only on request; leave existing ones and saved
results untouched.

## Decisions

`-- @decide <name> : <goal>` on a line of its own, with continuation lines `--`
plus at least two spaces, asks a decision provider for a Lean proof or refutation
of the goal. `glitnir-forseti` decides kinds `entailment`, `trajectory`,
`stream`, `stepped` and `hedging`; it needs `glitnir-forseti[z3]`, and the
notebook must import the Forseti/Asgard modules the goal uses (a missing one
shows as an unknown-identifier rejection on the card). Forseti `entailment`,
`trajectory`, `stepped` and `hedging` decision blocks and the `simulate`,
`sample` and `hedge` cards passed Glitnir's check in the plugin's live tests
against Forseti Lean v0.6.0, the pinned release, on Lean 4.35.0 (Linux CI,
2026-10-29, run 40847259729). CI also covers `stream` decisions
(`tests/test_stream_decision_lean.py`). `stepped` decisions and `sample` need a
Forseti Lean build that contains `Gimle.Forseti.Stepped` and
`Gimle.Forseti.Examples.HedgedSpreadStepped`, as Forseti Lean v0.6.0, the
pinned release, does. The first check of a Forseti project takes about 18
minutes (a one-time import replay); later checks take a few minutes.

```lean
-- @decide disk : entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1
-- @decide energy_band : trajectory three-state from (1, 2, -1) at 2
--   observe V always : V >= 0 and V <= 6
-- @decide covered : hedging call strike 100 spot 100 band down 1/50 up 1/50
--   steps 20 premium 3555/1000 covers
```

A `hedging` goal names a `call`, `put` or `straddle` with a strike, a spot, a
band of per-step moves (`down`, `up`) and a step count, and claims one of two
things, in exact rationals. Moves are simple returns as fractions of the spot:
±2 % a step is `down 1/50 up 1/50` or `down 0.02 up 0.02`; `2`, `2%` and `1e-2`
do not mean 2 %. `premium P covers` is proven when the premium is at least the
binomial price at the band's ends (the hedge from it covers the payoff on every
path whose simple returns stay in the band) and refuted when it is below (no
spot-feedback strategy covers the payoff from it).
`against down d up u shortfall <= X` compares a hedge built for one band with
paths of a band that contains the hedge's, equal or wider: it is proven only
for equal bands with a nonnegative bound, refuted by one exhibited extreme path
of the wider band, and otherwise Unknown, since no theorem bounds the shortfall
over the band's other paths. Read as a sale: the seller takes in the premium
and holds the hedge, borrowing or lending at zero interest with no transaction
costs, so wealth is the premium plus the trading gains; the payoff is European,
paid on the spot after the last step. A payoff other than `call`, `put` or
`straddle`, a spot or step count that is not positive and a move that is not
positive cannot be written (the directive is a parse error). A down move of 1
or more, and an `against` band that does not contain the hedge's band, are
Unknown (unsupported). A `covers` claim is decided up to 250 steps and a
`shortfall` claim up to 60. The notebook must import `Gimle.Forseti.Hedging`,
and `hedging` decisions and `hedge` evidence need a Forseti Lean build that
contains it, as the pinned release, Forseti Lean v0.6.0, does; an older build
reports the missing module.

Nothing runs when the preview opens. **Run** asks the provider (one provider run
at a time, evidence included); Glitnir then checks the saved text up to the
directive plus the proposed block with its own sandboxed kernel check, the only
proof authority. Status is Never run, Running, Unknown, Rejected, Candidate,
Accepted, Failed or Out of date; the card shows the stages, the kernel statement
(Lean's form, e.g. `Eq (instHAdd.hAdd 2 2) 4`), axioms and the full block. A proof
declares `theorem <name>`; a refutation `theorem <name>_refuted : ¬ (...)`. The
provider is trusted only for matching the theorem to the goal, so read the
statement before accepting.

**Accept** (VS Code preview only, for a checked candidate) inserts the block right
after the directive through the editor and saves; the backend never writes the
`.lean` file. It is refused while the notebook has unsaved edits or the editor's
text is not the saved file the backend reads. Saved edits below the directive keep
the candidate acceptable; an edit above it, or an environment change, makes it Out
of date until it is run again. **Discard** removes the saved result and any saved
failure; **Stop** cancels a run. Results are saved in `<Notebook>.glitnir.json`
under `decisions` (a block the kernel rejects is a saved Rejected result). A run
that fails before a result, because the provider crashed or refused the goal,
returned an outcome that is not a decision of the directive, or the candidate
check reached its time limit, is saved too, under `decision_failures`; a later
result replaces it. A provider crash is saved as its exception type only. Setup
failures, a busy checker and Stop are only shown. Never hand-edit the file.

As an agent you may ask the user to Run and Accept, or write the proof directly
in Lean; either way a decision never replaces checking, so run the project's Lean
validation and **Check saved file** afterwards. Add a directive only on request;
leave existing ones, accepted blocks and saved results untouched. Never invent an
artifact or paste a status into metadata.
