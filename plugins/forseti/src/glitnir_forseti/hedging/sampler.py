"""Run a payoff's binomial hedge on random paths in a band, in float64.

The ``hedge`` evidence verb (`glitnir_forseti.notebook.evidence`) has Lean
evaluate the hedging library's binomial price of the plan's payoff at the
plan's band, and print it exactly with the parameters it was evaluated at.
`checked_premium` reads that print and checks it against `numbers.price`, the
same price in exact arithmetic. `sample` then starts the hedge the library
builds for the plan's band from that premium and runs it on ``plan.paths``
random paths of ``plan.steps`` simple returns each. At step ``k`` of ``N`` the
hedge holds the binomial hedge ratio with ``N - 1 - k`` steps to go at the
current spot (`numbers.delta`, here in float64), so a return ``r`` adds
``δ · S · r`` to its wealth and takes the spot from ``S`` to ``S (1 + r)``. A
path's profit and loss at expiry is its final wealth less the payoff.

Each return is drawn in the band the paths run in (`HedgePlan.path_band`),
``[-down, up]``, by the plan's law:

* ``uniform``: uniform on the band;
* ``twopoint``: ``up`` or ``-down``, with probability one half each;
* ``gaussian``: normal with mean 0 and standard deviation ``sigma``, drawn
  again while it falls outside the band, `MAX_GAUSSIAN_DRAWS` times at most,
  after which it is the band's end nearer the last draw.

Each is then clipped into the band, so that no path leaves it: the library's
theorems speak of paths in the band and of no other. One `random.Random`
seeded with the plan's seed draws every return, path by path and, within a
path, step by step, so the same plan always gives the same run.

This is the only part of the hedging lane in floating point: exact values
become float64 here and nowhere upstream, and nothing returned here reaches a
claim, a goal or a decision. The sampled profit and loss is evidence, shown on
a card by its quantiles and never as a proved number.
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import comb
from typing import Any

from glitnir_forseti.figures import figure
from glitnir_forseti.notebook.evidence import (
    MAX_GAUSSIAN_DRAWS,
    EvidenceError,
    HedgePlan,
    check_hedge_work,
    read_rational,
)

from . import numbers
from .payoffs import PAYOFFS

#: The keys Lean prints for a hedge.
KEYS = ("payoff", "strike", "spot", "up", "down", "steps", "premium")
#: Most digits in the numerator or denominator of a printed premium. A price
#: over many steps has far more digits than the other printed values, and this
#: stays below Python's default limit on reading digits as an integer (4300).
MAX_PREMIUM_DIGITS = 4000
#: A card shows the quantiles at the levels ``i / LEVELS``, ``0 ≤ i ≤ LEVELS``.
LEVELS = 40
#: How far below zero a path must end, as a fraction of the spot, to count as
#: below it: a path the hedge replicates exactly ends within float64's rounding
#: of zero, on either side.
BELOW = 1e-9
#: How far, in natural logarithms, a spot or a move's compounding may reach
#: from 1 over a run: float64 holds about ``e^±709``.
_LOG_LIMIT = math.log(1e300)
#: Draws one return from a generator.
_Draw = Callable[[random.Random], float]
#: One hedge-ratio row: ``(weight, up factor, down factor)`` per term.
_Row = list[tuple[float, float, float]]


def _call(strike: float, spot: float) -> float:
    """Return ``max(spot - strike, 0)``."""
    return max(spot - strike, 0.0)


def _put(strike: float, spot: float) -> float:
    """Return ``max(strike - spot, 0)``."""
    return max(strike - spot, 0.0)


def _straddle(strike: float, spot: float) -> float:
    """Return ``|spot - strike|``."""
    return abs(spot - strike)


#: Each registered payoff in float64, as `payoffs.PAYOFFS` holds it exactly.
FLOAT_PAYOFFS: dict[str, Callable[[float, float], float]] = {
    "call": _call,
    "put": _put,
    "straddle": _straddle,
}


@dataclass(frozen=True)
class HedgeResult:
    """A sampled run of a hedge, in float64: evidence, never a bound.

    Attributes:
        pnl: Each path's profit and loss at expiry, its final wealth less the
            payoff, sorted.
        below_zero: How many paths end more than `BELOW` times the spot below
            zero.
        finite: Whether every path stayed finite. A run stops at the first
            path that does not, and ``pnl`` holds the paths before it.
    """

    pnl: list[float]
    below_zero: int
    finite: bool


def _float(value: Fraction) -> float:
    """Return the float64 nearest an exact value.

    Raises:
        EvidenceError: If float64 cannot hold it.
    """
    try:
        return value.numerator / value.denominator
    except OverflowError:
        raise EvidenceError(f"{figure(value)} cannot be run in float64") from None


def checked_premium(emitted: Any, plan: HedgePlan) -> Fraction:
    """Read what Lean printed for a hedge and return its premium, exactly.

    The print must hold exactly `KEYS`, echo the plan's payoff, strike, spot,
    band and steps, and give as the premium the binomial price `numbers.price`
    computes for them: Lean's print is untrusted, and the card runs the hedge
    from exactly the price the library's ``priceQ`` evaluates.

    Raises:
        EvidenceError: If a key is missing or unknown, a value is malformed or
            not the plan's, the premium has more than `MAX_PREMIUM_DIGITS`
            digits, or it is not the binomial price, naming both.
    """
    if not isinstance(emitted, dict):
        raise EvidenceError("Lean printed a malformed hedge")
    missing = [key for key in KEYS if key not in emitted]
    if missing:
        raise EvidenceError(f"the printed hedge has no {missing[0]!r}")
    unknown = sorted(set(emitted) - set(KEYS))
    if unknown:
        raise EvidenceError(f"the printed hedge has an unknown key {unknown[0]!r}")
    printed = emitted["payoff"]
    if type(printed) is not str or printed not in PAYOFFS:
        raise EvidenceError("Lean printed the hedge of no registered payoff")
    if printed != plan.payoff:
        raise EvidenceError(
            f"Lean printed the hedge of the {printed}, not the {plan.payoff}"
        )
    for key, name, expected in (
        ("strike", "the strike", plan.strike),
        ("spot", "the spot", plan.spot),
        ("up", "the up move", plan.up),
        ("down", "the down move", plan.down),
    ):
        value = read_rational(emitted[key])
        if value != expected:
            raise EvidenceError(
                f"Lean printed {name} as {figure(value)}, not {figure(expected)}"
            )
    steps = emitted["steps"]
    if type(steps) is not int:
        raise EvidenceError("the steps are a count")
    if steps != plan.steps:
        raise EvidenceError(f"Lean printed the steps as {steps}, not {plan.steps}")
    premium = _premium(emitted["premium"])
    price = numbers.price(
        PAYOFFS[plan.payoff], plan.strike, plan.spot, plan.up, plan.down, plan.steps
    )
    if premium != price:
        printed, binomial = figure(premium), figure(price)
        apart = (
            f"; they differ by {figure(premium - price)}" if printed == binomial else ""
        )
        raise EvidenceError(
            f"Lean printed the premium {printed}, which is not the binomial price"
            f" {binomial}{apart}"
        )
    return premium


def _premium(value: Any) -> Fraction:
    """Read the printed premium, whose parts may be longer than other values'."""
    if (
        type(value) is list
        and len(value) == 2
        and all(type(part) is str for part in value)
        and max(len(part.removeprefix("-")) for part in value) > MAX_PREMIUM_DIGITS
    ):
        raise EvidenceError(
            f"the premium Lean printed has more than {MAX_PREMIUM_DIGITS} digits;"
            " use fewer steps or a band written with shorter numbers"
        )
    return read_rational(value, MAX_PREMIUM_DIGITS)


def returns(plan: HedgePlan, rng: random.Random) -> list[float]:
    """Draw one path's ``plan.steps`` returns from ``rng``, in order.

    Each is drawn by the plan's law in the band of `HedgePlan.path_band`, as
    the module describes, and clipped into it in float64.

    Raises:
        EvidenceError: If a value cannot be run in float64, or the law is
            unknown or a Gaussian has no ``sigma``.
    """
    down, up = plan.path_band
    low, high = -_float(down), _float(up)
    draw = _drawer(plan, low, high)
    return [min(max(draw(rng), low), high) for _ in range(plan.steps)]


def _drawer(plan: HedgePlan, low: float, high: float) -> _Draw:
    """Return a function that draws one return of the plan's law."""
    if plan.law == "uniform":

        def uniform(rng: random.Random) -> float:
            return rng.uniform(low, high)

        return uniform
    if plan.law == "twopoint":

        def twopoint(rng: random.Random) -> float:
            return high if rng.random() < 0.5 else low

        return twopoint
    if plan.law == "gaussian":
        if plan.sigma is None:
            raise EvidenceError("a gaussian law needs its sigma")
        sigma = _float(plan.sigma)

        def gaussian(rng: random.Random) -> float:
            drawn = 0.0
            for _ in range(MAX_GAUSSIAN_DRAWS):
                drawn = rng.gauss(0.0, sigma)
                if low <= drawn <= high:
                    return drawn
            return low if drawn < low else high

        return gaussian
    raise EvidenceError(f"unknown law {plan.law!r}")


def _check_range(plan: HedgePlan) -> None:
    """Refuse a run on which a spot could leave the range float64 holds.

    A path's spot, and the spots its hedge ratios are priced at, lie between
    ``S₀ fall^N`` and ``S₀ rise^N`` for the widest moves of the two bands, as
    float64 takes them; a spot of zero would leave the hedge ratio undefined.

    Raises:
        EvidenceError: If a down move rounds to 1 in float64, or those spots
            could reach past ``10^±300``.
    """
    path_down, path_up = plan.path_band
    down, up = max(plan.down, path_down), max(plan.up, path_up)
    fall, rise = 1.0 - _float(down), 1.0 + _float(up)
    if fall <= 0:
        raise EvidenceError(
            f"the down move {figure(down)} rounds to 1 in float64, so a path"
            " would take the spot to zero; use a smaller down move"
        )
    reach = abs(math.log(_float(plan.spot))) + plan.steps * max(
        math.log(rise), -math.log(fall)
    )
    if reach > _LOG_LIMIT:
        raise EvidenceError(
            f"a path of {plan.steps} steps could take the spot past what float64"
            " holds; use a narrower band or fewer steps"
        )


def _rows(plan: HedgePlan) -> list[_Row]:
    """Return, for each count ``n`` of steps to go, the hedge ratio's terms.

    The price with ``n`` steps to go at ``X`` is ``Σ_j w_j g(X f_j)``, with
    ``w_j = C(n, j) q^j (1 - q)^(n-j)`` and ``f_j = (1 + up)^j (1 - down)^(n-j)``
    for the hedge's own band (`numbers.price`). The ratio at ``S`` is the price
    at ``S (1 + up)`` less the price at ``S (1 - down)``, over ``S (up +
    down)``, so row ``n`` holds ``(w_j, (1 + up) f_j, (1 - down) f_j)``.
    """
    q = _float(numbers.fair_weight(plan.up, plan.down))
    rise, fall = _float(1 + plan.up), _float(1 - plan.down)
    rows: list[_Row] = []
    for n in range(plan.steps):
        row: _Row = []
        for j in range(n + 1):
            weight = comb(n, j) * q**j * (1 - q) ** (n - j)
            factor = rise**j * fall ** (n - j)
            row.append((weight, rise * factor, fall * factor))
        rows.append(row)
    return rows


def sample(plan: HedgePlan, premium: Fraction) -> HedgeResult:
    """Run the hedge built for the plan's band, from ``premium``, on its paths.

    Every path starts at the plan's spot with ``premium`` as its wealth and
    takes ``plan.steps`` returns drawn by `returns`, path after path from one
    generator seeded with the plan's seed.

    Raises:
        EvidenceError: If the run would take more than `MAX_WORK` payoff
            evaluations and draws, a value cannot be run in float64, a spot
            could leave float64's range (`_check_range`), or the payoff or
            law is unknown.
    """
    draws = MAX_GAUSSIAN_DRAWS if plan.law == "gaussian" else 1
    check_hedge_work(plan.paths, plan.steps, draws)
    _check_range(plan)
    payoff = FLOAT_PAYOFFS.get(plan.payoff)
    if payoff is None:
        raise EvidenceError(f"no payoff {plan.payoff!r} is registered")
    strike, spot = _float(plan.strike), _float(plan.spot)
    start, width = _float(premium), _float(plan.up + plan.down)
    rows = _rows(plan)
    rng = random.Random(plan.seed)
    pnl: list[float] = []
    finite = True
    for _ in range(plan.paths):
        path = returns(plan, rng)
        value = _run(rows, payoff, strike, spot, start, width, path)
        if not math.isfinite(value):
            finite = False
            break
        pnl.append(value)
    pnl.sort()
    below = bisect.bisect_left(pnl, -below_threshold(plan))
    return HedgeResult(pnl=pnl, below_zero=below, finite=finite)


def below_threshold(plan: HedgePlan) -> float:
    """Return how far below zero a path must end to count as below it.

    It is `BELOW` times the spot: a path the hedge replicates exactly ends
    within float64's rounding of zero, on either side, and is not counted.
    """
    return BELOW * _float(plan.spot)


def _run(
    rows: list[_Row],
    payoff: Callable[[float, float], float],
    strike: float,
    spot: float,
    wealth: float,
    width: float,
    path: list[float],
) -> float:
    """Run the hedge along one path; return its wealth less the payoff."""
    steps = len(path)
    for k, step_return in enumerate(path):
        gap = sum(
            weight * (payoff(strike, spot * up) - payoff(strike, spot * down))
            for weight, up, down in rows[steps - 1 - k]
        )
        ratio = gap / (spot * width)
        wealth += ratio * spot * step_return
        spot *= 1 + step_return
    return wealth - payoff(strike, spot)


def levels() -> list[float]:
    """Return the quantile levels a card shows: ``0, 1/40, …, 1``."""
    return [i / LEVELS for i in range(LEVELS + 1)]


def quantiles(ordered: Sequence[float]) -> list[float]:
    """Return the quantiles of sorted values at `levels`.

    The quantile at level ``p`` of ``n`` sorted values lies at position
    ``p (n - 1)``: the value there, or between the two either side in
    proportion. The levels 0 and 1 are the least and greatest values exactly.

    Raises:
        EvidenceError: If there are no values.
    """
    if not ordered:
        raise EvidenceError("no path of the hedge was run")
    last = len(ordered) - 1
    shown = []
    for i in range(LEVELS + 1):
        position = Fraction(i * last, LEVELS)
        below = position.numerator // position.denominator
        weight = float(position - below)
        above = min(below + 1, last)
        shown.append(ordered[below] + weight * (ordered[above] - ordered[below]))
    return shown
