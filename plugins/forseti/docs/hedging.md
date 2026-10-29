# The hedging lane

`hedging` decides closed goals about hedging a convex payoff over a band of
per-step moves, in exact rationals, and `-- @evidence hedge` shows how that
hedge fares on seeded random paths. The decider runs no Lean and no solver: it
picks a proof template and its constants, and the project's Lean checks the
block against `Gimle.Forseti.Hedging`. Only that check makes a decision a
proof. The lane needs a Forseti Lean build that contains
`Gimle.Forseti.Hedging`, as the pinned release, Forseti Lean v0.6.0, does; an
older build reports the missing module.

## Goals

```lean
-- @decide covered : hedging call strike 100 spot 100 band down 1/50 up 1/50
--   steps 20 premium 3555/1000 covers
-- @decide narrow_hedge : hedging call strike 100 spot 100 band down 1/100
--   up 1/100 steps 20 against down 1/50 up 1/50 shortfall <= 4
```

A goal names a payoff (`call`, `put` or `straddle`), a strike (not negative), a
spot (positive), the band `down d up u` within which the spot moves at each of
`steps` steps (a simple return between `-d` and `u`; `u` positive, `d` between 0
and 1) and one claim. Numbers are plain decimals or fractions and stay exact.
The hedge is the binomial hedge for the band: it starts from the payoff's
binomial price at the band and holds the binomial hedge ratio at each step, a
function of the step and the spot.

The band is the user's assumption. A path that leaves it is outside every
guarantee here, and which band real moves keep to is not addressed.

## Rules

* `premium P covers`: the hedge started from `P` ends at least at the payoff on
  every path whose returns stay in the band. **PROVEN** when `P` is at least the
  binomial price at the band's ends. **REFUTED** when `P` is below it, and then
  no spot-feedback strategy at all covers the payoff from `P`, not only the
  band's own hedge. The reason says how far short the band's own hedge ends. The
  worked call at plus or minus 2 % over 20 steps has the price 3.5549, so a
  premium of 3.555 is proven and one of 7/2 is refuted.
* `against down d' up u' shortfall <= X`: the hedge built for the goal's band,
  from its own price, is run on every path of the wider `against` band and ends
  at most `X` below the payoff. It is **PROVEN** only when the against band
  equals the hedge's band and `X` is not negative (that is `covers` from the
  price). It is **REFUTED** when the worst extreme path of the against band, one
  that takes only the band's ends, leaves the hedge more than `X` below the
  payoff: that path is the witness. Otherwise it is **UNKNOWN: gap**, because no
  theorem bounds the shortfall over the band's other paths for a hedge built for
  a narrower band. The plus or minus 1 % hedge ends 4.1129 below the payoff on
  the zigzag path of the plus or minus 2 % band, its worst extreme path, so
  `<= 4` is refuted and `<= 5` is a gap.

Some inputs cannot be written in a notebook, and the directive is a parse
error: a payoff other than `call`, `put` or `straddle`, a spot or step count
that is not positive, and a move, in either band, that is not positive. Two
goals reach the decider and end `UNKNOWN: unsupported`: a down move of 1 or
more, in either band (the spot would reach zero), and an `against` band that
does not contain the hedge's band. Past the caps below, or with a number longer
than the coefficient limit, a goal is `UNKNOWN: resource-limit`; the path search
stops at its time limit (`timeout`) or when cancelled (`cancelled`). The lane
keeps no artifact and has no pinned replay, so a request for one ends
`UNKNOWN: lean-unavailable`.

| Claim | Most steps |
|-------|-----------:|
| `covers` | 250 (its block has the kernel evaluate one price) |
| `shortfall` | 60 (the path search is about cubic in the steps, and a refutation has the kernel evaluate the hedge along the path) |

The block imports `Gimle.Forseti.Hedging`, which the outcome lists as its
`imports` stage; the notebook must import it. The card states the claim in
words and in the library's terms.

## The `hedge` evidence verb

```lean
-- @evidence hedge Gimle.Forseti.Hedging.call (strike := 100) (spot := 100) (down := 1/50) (up := 1/50) (steps := 20) (law := uniform) (paths := 4000) (seed := 7)
```

The target is a payoff as the library names it: `Gimle.Forseti.Hedging.call`,
`.put` or `.straddle`. The notebook must import `Gimle.Forseti.Hedging`. Lean
evaluates the library's own binomial price at the band and prints it exactly;
the provider checks it against the same price in exact arithmetic, runs the
hedge built for the band from that premium on `paths` seeded random paths of
`steps` returns, and draws the returns by `law`, each kept inside the band:

* `uniform` (the default): uniform on the band;
* `twopoint`: `up` or `-down`, one half each;
* `gaussian`: normal with mean 0 and standard deviation `(sigma := …)`, drawn
  again while it falls outside the band, and after 100 draws it is the band's
  end nearer the last draw. `sigma` is required with `gaussian` and refused with
  the other laws.

`strike`, `spot`, `down`, `up` and `steps` are required. `paths` defaults to
4000 (at most 100000) and `seed` to 7. `(against_down := …) (against_up := …)`,
given together, draw the paths in another band than the hedge's own, usually a
wider one: the sampled counterpart of a `shortfall` claim. A run that would take
more than 20 million payoff evaluations and draws is refused on the card before
anything runs, as are steps above 250. A path evaluates `steps × (steps + 1)`
payoffs and draws at most `draws` numbers for each of its `steps` returns, where
`draws` is 1, or 100 for `gaussian`, so `paths × steps × (steps + 1 + draws)`
must stay within the 20 000 000 cap. With the default 4000 paths that allows at
most 69 steps for `uniform` and `twopoint` and 36 for `gaussian`; fewer paths
allow more steps.

## What a card never shows

The `hedge` card is float64 evidence, never proof. Its chart is the profit and
loss at expiry by its quantiles, and its provenance holds the premium, the
least, median and greatest outcome and the number of paths that end more than
a billionth of the spot below zero (a path the hedge replicates exactly ends
within rounding of zero, on either side). It shows no proved number and no
status for any claim: the premium is the binomial price Lean printed and the
provider recomputed, and the card asserts no bound. A sampled run cannot
establish `covers` or `shortfall`, however many paths end above zero, and a
claim's status comes from the project's Lean check alone.
