# glitnir-forseti

An optional provider package for Glitnir. It is found through the
`glitnir.providers` entry point, so install it into the same Python environment
as the Glitnir backend. It is not on PyPI: each Glitnir release has its wheel,
and the setup guide's
[Forseti plugin](https://github.com/arnovich/glitnir/blob/main/extension/media/setup.md#the-forseti-plugin) section
explains verifying and installing it. To build it from a checkout, see
[DEVELOPMENT.md](https://github.com/arnovich/glitnir/blob/main/DEVELOPMENT.md#build-and-test).

**What it provides now:** Forseti circuit, model and dynamics views for
`-- @view circuit|model|dynamics <name>` directives. The notebook must sit in a
Lake project whose packages build `Gimle.Forseti` and
`Gimle.Asgard.Visualization.Model`; Glitnir loads both from the project's build
for the view, so the notebook need not import them. An unbuilt or out-of-date one
is named on the card with the `lake build` command to run.

It also provides evidence for `-- @evidence simulate|iterate|sample|hedge <target>`
directives, shown as cards with a **Run** button; a run only happens when
clicked. The notebook loads `Gimle.Asgard.Compile.RationalInterchange` and
`Gimle.Asgard.Model.Continuous` from the project's build, as views load theirs.

```lean
-- @evidence simulate dissipative (h := 1/100) (steps := 400) (solver := rk4)
-- @evidence iterate update (state := 1, 1) (inputs := 1, -1; 1, 1) (parameter := 1/4) (steps := 200)
-- @evidence sample Gimle.Forseti.Examples.HedgedSpread.compiled (law := threepoint) (variance := 64) (observe := s)
-- @evidence hedge Gimle.Forseti.Hedging.call (strike := 100) (spot := 100) (down := 1/50) (up := 1/50) (steps := 20) (law := uniform) (paths := 4000) (seed := 7)
```

`simulate` options are `h`, `steps`, `solver` (`euler` or `rk4`) and `plane`;
`iterate` options are `state`, `inputs`, `parameter`, `steps` and `names`. When
`names` is given it must name every port of the circuit, state first, then the
inputs, then the parameters, not just the states. For a circuit with two states,
one input and one parameter, `x+ = a*x + u`, `y+ = y + x`:

```lean
-- @evidence iterate driven (state := 1, 0) (inputs := 1; -1) (parameter := 1/2) (steps := 40) (names := x, y, u, a)
```

Naming only `x, y` is refused ("Names must name all 4 ports, state first."), and
so are two states of the same name and a state name longer than 200 characters,
on the card before anything runs. Whether the counts match the circuit is known
only from what Lean prints, so a mismatch is reported at the end of the first
run: `(state := 1, 1)` on a circuit with one port, `x+ = 2*x`, is refused with
"The circuit maps 1 port to 1, not 2 (state, inputs, parameters) to 2." Running
again after correcting only the options reuses that output and answers in
seconds.
Numbers are plain decimals or fractions (at most 40 digits per part). `simulate` needs
nothing beyond this package. `iterate` needs Python Asgard (`gimle-asgard`),
which is not on PyPI: install it into the backend's environment from its own
wheel or checkout. Without it the card says so before anything runs.
`sample` draws seeded random shock paths in float64 through a stepped model the
plugin knows (`hedged-spread`, named by its compiled model as above, with
`Gimle.Forseti.Examples.HedgedSpreadStepped` imported) and needs nothing beyond
this package; `law` (`gaussian`, `twopoint` or `threepoint`), `variance` and
`observe` are required, and `paths` (4000, at most 100000), `steps` (256),
`seed` (7), `show` (8, or `paths` if fewer) and `barrier` (1) are optional.
Its card shows a few paths and the fraction that has left the band
`|observe| ≤ barrier`: sampled evidence to read beside a proved bound, never
the bound. Evidence is never proof and changes no claim's status.

`hedge` runs the binomial hedge of a convex payoff on seeded random paths in
float64 and needs nothing beyond this package. Its target is a payoff as the
hedging library names it, `Gimle.Forseti.Hedging.call`, `.put` or `.straddle`,
and the notebook must import `Gimle.Forseti.Hedging`, which defines the price
Lean prints. `strike`, `spot`, `down`, `up` and `steps` (at most 250) are
required; `law` (`uniform`, the default, `twopoint` or `gaussian`), `paths`
(4000, at most 100000) and `seed` (7) are optional, as are `against_down` and
`against_up`, which are given together and draw the paths in another band than
the hedge's, usually a wider one, and `sigma`, which `gaussian` requires and the
other laws refuse. `sigma` is the per-step standard deviation of the simple
return, in the units of `down` and `up` (1/100 is 1 % a step), not an
annualised volatility. A run of more than 20 million payoff evaluations and
draws is refused on the card: with the default 4000 paths, at most 69 steps for
`uniform` and `twopoint` and 36 for `gaussian` (fewer paths allow more steps).
Lean prints the library's binomial price at the band, the provider checks it
against the same price in exact arithmetic, and the hedge built for the band is
run from that premium. Its card shows the seller's profit and loss at expiry,
the hedge's wealth less the payoff, by its quantiles (the x axis is the quantile
level, 0 the worst sampled path and 1 the best, not time), with the premium, the
least, median and greatest outcome and the paths that end below zero, with a
95 % Wilson interval for their fraction, in the provenance. It shows no proved
number: it is sampled evidence beside a `hedging` decision, never the decision.
Both are described in
[the hedging lane](https://github.com/arnovich/glitnir/blob/main/plugins/forseti/docs/hedging.md).

It also decides `-- @decide <name> : <goal>` directives whose goal starts with
`entailment`, `trajectory`, `stream`, `stepped` or `hedging`, in the goal
syntax of `glitnir_forseti.claims.text`:

```lean
-- @decide disk : entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1
-- @decide energy_band : trajectory three-state from (1, 2, -1) at 2
--   observe V always : V >= 0 and V <= 6
-- @decide breach_within_year : stepped hedged-spread from (0, 0) at 0 step 1/256
--   shocks variance 64 within 256 observe s breach 1 probability <= 1/4
-- @decide covered : hedging call strike 100 spot 100 band down 1/50 up 1/50
--   steps 20 premium 3555/1000 covers
-- @decide narrow_hedge : hedging call strike 100 spot 100 band down 1/100
--   up 1/100 steps 20 against down 1/50 up 1/50 shortfall <= 4
```

A `stepped` goal's law class goes with its claim: a breach within a horizon
(`within N`) and an expectation (`expected <= c at N`) take the two-moment
class (`shocks variance v`), and a breach at a fixed date, at every mark taken
separately (`at every mark within N`), takes the sub-Gaussian class
(`shocks subgaussian v`). A breach within a horizon is decided up to 4096 marks,
sixteen years of daily marks; a breach at a fixed date and an expectation, whose
proofs may unroll a recursion over every mark, only up to 1024 marks, four
years. A longer claim is `UNKNOWN: unsupported`. `stepped` decisions and
`sample` evidence need a Forseti Lean build that contains
`Gimle.Forseti.Stepped` and `Gimle.Forseti.Examples.HedgedSpreadStepped`, as the
pinned release, Forseti Lean v0.6.0, does; an older build reports the missing
module.

A `hedging` goal names a payoff (`call`, `put` or `straddle`), a strike, a spot,
a band of per-step moves (`down` and `up`, simple returns between `-down` and
`up`), a number of steps and one claim, in exact rationals. Moves are simple
returns as fractions of the spot: ±2 % a step is `down 1/50 up 1/50` or
`down 0.02 up 0.02`; `2`, `2%` and `1e-2` do not mean 2 %. `premium P covers`
is PROVEN when the premium is at least the binomial price at the band's ends, so
the hedge from it covers the payoff on every path whose simple returns stay in
the band, and REFUTED when it is below, since then no spot-feedback strategy
covers the payoff from it. `against down d up u shortfall <= X` compares a hedge
built for one band with paths of a band that contains the hedge's, equal or
wider: it is PROVEN only for equal bands with a nonnegative bound, REFUTED by
one exhibited extreme path of the wider band that leaves the hedge more than `X`
below the payoff, and otherwise `UNKNOWN: gap`, since no theorem bounds the
shortfall over the band's other paths. PROVEN and REFUTED are the decider's
proposals: it runs no Lean, and the notebook's Lean check of the proposed block
decides. A payoff other than `call`, `put` or `straddle`, a spot or step count
that is not positive and a move that is not positive cannot be written in a
notebook (the directive is a parse error). A down move of 1 or more, and an
`against` band that does not contain the hedge's band, are
`UNKNOWN: unsupported`. A `covers` claim is decided up to 250 steps and a
`shortfall` claim up to 60; a longer one is `UNKNOWN: resource-limit`.
`hedging` decisions and `hedge` evidence need a Forseti Lean build that
contains `Gimle.Forseti.Hedging`, as the pinned release, Forseti Lean v0.6.0,
does; an older build reports the missing module.

Read as a sale: the seller takes in the premium and holds the hedge, borrowing
or lending at zero interest with no transaction costs, so wealth is the premium
plus the trading gains; the account is self-financing and rebalanced only at the
steps, and the payoff is European, paid on the spot after the last step. The
binomial price at the band's ends is the Cox–Ross–Rubinstein price with up
factor `1 + u`, down factor `1 - d` and weight `q = d/(u + d)`. By the two
`covers` rules (`covers_hedge` and `not_covers_of_lt`) it is the least premium
from which any spot-feedback strategy covers the payoff on every path in the
band: the band's super-hedging price, the discrete, bounded-move form of
uncertain-volatility pricing. A spot-feedback strategy holds, over each step, a
number of units set by the step and the current spot; a strategy that depends
on the path so far is outside the refutation theorem.

Deciding needs the `z3` extra (`glitnir-forseti[z3]`) for every kind, although
the stepped and hedging deciders use none; without it the card says so before
anything runs. **Run** searches for a certificate or a counterexample and proposes a self-contained Lean block: a `Decided_<name>`
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

**Verified on 2026-10-28** against Forseti Lean v0.5.1 with Asgard Lean v0.4.1
(Lean 4.35.0), the release pinned then, by CI's `plugin-live` shards on Linux
(run 40654470418), every one green:
`streams` 88 passed (`tests/test_streams_lean.py` and
`tests/test_mild_stream_lean.py`, kernel replay of the stream roots against
the release pinned then); `trajectory` 81 passed (`tests/test_trajectory_lean.py`,
every example goal and both driven models, and
`tests/test_classical_euler_lean.py`); `decisions` 56 passed and one skipped
(`tests/test_decision_provider_lean.py`, proven and refuted `entailment` and
`trajectory` checked through Glitnir's project runner, including a driven
claim; `tests/test_entailment_lean.py`; `tests/test_evidence_provider_lean.py`
`simulate`; the skip is `iterate`, which needs Python Asgard); `stepped` 27
passed (`tests/test_stepped_lean.py`: the lane's ten examples, its template
variants and caps, a rejected false block and the `sample` card, each block
checked in 71–76 s, the first after a one-time import replay); and `rest`
1559 passed with 14 skipped, all Python Asgard simulations or macOS-only
sandbox probes (`tests/test_lean_release.py`,
`tests/test_live_lean_gate.py`, `tests/test_lean_independent.py`,
`tests/test_lean_sandbox_attacks.py` and the unit tests). The pin is now
Forseti Lean v0.6.0, which this release's pull request runs the same shards
against, the `hedging` shard included. The view preamble
drew a circuit and a model in `tests/test_views_lean.py` on macOS against
Forseti Lean v0.4.0 on 2026-10-22; CI runs it from then on. `stream` decisions and
`iterate` evidence were verified on 2026-10-23, locally on macOS with Glitnir
0.1.5 and Lean 4.35.0: six stream goals were decided, accepted and checked by
the project runner, and `iterate` evidence matched exact and float64 references.
CI covers `stream` decisions with `tests/test_stream_decision_lean.py` (the
`streams` shard: a proven and a refuted heat-field bound, accepted and
project-checked); `iterate` needs Python Asgard and stays a local check.

Development: from the repository root, build it with
`uv build plugins/forseti --wheel --out-dir plugins/forseti/dist`, install the
wheel with `--no-deps` after installing `lark` and `z3-solver==5.1.0.0` (the
`z3` extra's pin), and run its tests
from `plugins/forseti`. See the repository's
[DEVELOPMENT.md](https://github.com/arnovich/glitnir/blob/main/DEVELOPMENT.md#build-and-test).

The live evidence tests (`tests/test_evidence_provider_lean.py`) run `simulate`,
and `iterate` when `gimle.asgard` is installed, end to end through Glitnir's
project runner. They need a built Forseti Lean release checkout, `lake` on
`PATH`, Lean 4.35.0 installed with elan and a working sandbox. They write a
temporary module into the checkout's `Gimle/Forseti/Tests/` and remove it and its
results file afterwards. CI's `plugin-live` job runs them against the pinned
release; without a project they skip. To run them:

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
afterwards; CI's `plugin-live` job runs them, and without a project they skip.
To run them:

```sh
cd plugins/forseti
FORSETI_LEAN_TEST_PROJECT=/path/to/forseti-lean \
  ../../.venv/bin/python -m pytest -q -rs tests/test_decision_provider_lean.py
```

The live hedging tests (`tests/test_hedging_lean.py`) take each example goal of
the lane through the decision provider and have Glitnir's project check accept
every block it proposes, check variants at the step caps and for the straddle,
show that a block whose premium was lowered by hand is rejected by the kernel,
and run the `hedge` verb end to end, comparing the premium Lean prints with the
exact binomial price. They need the same project, `lake`, Lean and sandbox as
the live evidence tests, a checkout that holds `Gimle.Forseti.Hedging`, and
create and remove their scratch modules the same way. CI's `plugin-live` job
runs them in its `hedging` shard; without a project they skip. To run them:

```sh
cd plugins/forseti
FORSETI_LEAN_TEST_PROJECT=/path/to/forseti-lean \
  ../../.venv/bin/python -m pytest -q -rs tests/test_hedging_lean.py
```
