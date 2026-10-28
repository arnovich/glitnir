"""The hedging lane's example goals over the at-the-money call.

Every goal is about a call struck at 100 on a spot of 100, re-hedged at each
of 20 steps, or a put at the same strike, over a band of ±2 % a step
(``down 1/50 up 1/50``). The claims are the ones a hedging notebook would
make: the band's own hedge covers the option from a premium, or the hedge
built for ±1 % a step, run on paths of ±2 %, ends at most so far below the
payoff. Each is written here as data, and no evidence is: the service finds
it or reports why not, and the project's Lean checks the block it proposes.

The binomial price of the call at the ±2 % band with 20 steps to go is
`PRICE` (≈ 3.5549), so a premium of 3.555 covers it and one of 3.5 does not;
at the money the put's price is the same. The ±1 % hedge, from its own price,
ends ≈ 4.1129 below the payoff on the ±2 % zigzag, the path that rises and
falls by turns, and that is its worst extreme path: a claim that it ends at
most 4 below is refuted there, and one of at most 5 is a gap, since no theorem
bounds its shortfall on the band's other paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .goal import HedgingGoal
from .render import IMPORTS

#: The strike, the spot and the number of steps of every example.
STRIKE = Fraction(100)
SPOT = Fraction(100)
STEPS = 20
#: The band's move each way: ±2 % a step, and ±1 % for the narrow hedge.
WIDE = Fraction(1, 50)
NARROW = Fraction(1, 100)
#: The call's binomial price at the ±2 % band with 20 steps to go, exactly.
PRICE = Fraction(
    177744620736998422379605070486628497811,
    50000000000000000000000000000000000000,
)


@dataclass(frozen=True, slots=True)
class Example:
    """One example goal and what it must end as.

    Attributes:
        name: The notebook claim the goal is written as, and its fixture's name.
        claim: What the goal says, in words.
        goal: The goal.
        expected: ``PROVEN``, ``REFUTED`` or ``UNKNOWN:<reason>``.
    """

    name: str
    claim: str
    goal: HedgingGoal
    expected: str


def covers_goal(
    premium: Fraction,
    *,
    payoff: str = "call",
    down: Fraction = WIDE,
    up: Fraction = WIDE,
    steps: int = STEPS,
) -> HedgingGoal:
    """Pose a goal that the band's own hedge covers the payoff from ``premium``.

    The payoff is the call, struck at 100 on a spot of 100, unless ``payoff``
    names another; the band is ±2 % a step over 20 steps unless ``down``,
    ``up`` or ``steps`` say otherwise.
    """
    return HedgingGoal(
        payoff=payoff,
        strike=STRIKE,
        spot=SPOT,
        up=up,
        down=down,
        steps=steps,
        claim="covers",
        premium=premium,
    )


def shortfall_goal(
    bound: Fraction, *, hedged: Fraction = NARROW, against: Fraction = WIDE
) -> HedgingGoal:
    """Pose a goal that the hedge ends at most ``bound`` below the call's payoff.

    The hedge is built for the band of ``±hedged`` a step and run on paths of
    ``±against``: by default the ±1 % hedge on paths of ±2 %, over 20 steps.
    """
    return HedgingGoal(
        payoff="call",
        strike=STRIKE,
        spot=SPOT,
        up=hedged,
        down=hedged,
        steps=STEPS,
        claim="shortfall",
        against_up=against,
        against_down=against,
        bound=bound,
    )


def replay_imports() -> tuple[tuple[str, ...], ...]:
    """Every import set a hedging block uses, for the replay pin."""
    return (IMPORTS,)


EXAMPLES = (
    Example(
        "covered_from_price",
        "the call struck at 100 on a spot of 100, re-hedged at each of 20 steps "
        "at the binomial ratio from a premium of 3.555, is covered at expiry on "
        "every path whose step returns stay within ±2 %: the binomial price is "
        "≈ 3.5549",
        covers_goal(Fraction(3555, 1000)),
        "PROVEN",
    ),
    Example(
        "covered_exactly",
        "the same hedge is covered from exactly the binomial price, the least "
        "premium any spot-feedback strategy covers from",
        covers_goal(PRICE),
        "PROVEN",
    ),
    Example(
        "not_from_seven_halves",
        "the call is covered from a premium of 7/2, below the price: refuted, "
        "since no spot-feedback strategy covers it from there; the band's own "
        "hedge ends ≈ 0.0549 below the payoff on every extreme path",
        covers_goal(Fraction(7, 2)),
        "REFUTED",
    ),
    Example(
        "narrow_hedge_loses_four",
        "the hedge built for ±1 % a step, from its own price, run on every path "
        "of ±2 % a step, ends at most 4 below the payoff: refuted by the zigzag, "
        "on which it ends ≈ 4.1129 below",
        shortfall_goal(Fraction(4)),
        "REFUTED",
    ),
    Example(
        "narrow_hedge_five_unknown",
        "the same hedge ends at most 5 below the payoff: its worst extreme path "
        "ends ≈ 4.1129 below, but no theorem bounds the shortfall over the "
        "band's other paths for a hedge built for a narrower band",
        shortfall_goal(Fraction(5)),
        "UNKNOWN:gap",
    ),
    Example(
        "same_band_no_shortfall",
        "the hedge built for ±2 % a step, run on the paths of its own band, "
        "ends at most 0 below the payoff: it covers it from its price",
        shortfall_goal(Fraction(0), hedged=WIDE),
        "PROVEN",
    ),
    Example(
        "put_covered",
        "the put at the same strike is covered from 3.555 too: at the money its "
        "binomial price is the call's",
        covers_goal(Fraction(3555, 1000), payoff="put"),
        "PROVEN",
    ),
    Example(
        "too_many_steps",
        "the call re-hedged at each of 251 steps: a covers claim is decided up "
        "to 250 steps, since its block has the kernel evaluate the price",
        covers_goal(Fraction(3555, 1000), steps=251),
        "UNKNOWN:resource-limit",
    ),
    Example(
        "band_too_wide",
        "a band whose down move is 1 would take the spot to zero, which the "
        "band's theorems exclude",
        covers_goal(Fraction(3555, 1000), down=Fraction(1)),
        "UNKNOWN:unsupported",
    ),
    Example(
        "against_narrower",
        "the hedge built for ±2 % a step run on paths of ±1 %: a shortfall is "
        "decided for a hedge run on its own band or a wider one",
        shortfall_goal(Fraction(4), hedged=WIDE, against=NARROW),
        "UNKNOWN:unsupported",
    ),
)
