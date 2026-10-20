"""The stepped lane's example goals over the hedged spread.

Every goal is about the hedged spread advanced in steps of 1/256 from the flat
book (0, 0), under independent shocks of variance (or sub-Gaussian proxy) 64,
over a year of 256 marks. The claims are the ones a risk notebook would make:
the exposure ``s`` breaches the barrier 1 within the year, or on any one date,
or the expected energy ``V`` at the year's end. Each is written here as data,
and no evidence is: the service finds it or reports why not, and the project's
Lean checks the block it proposes.

The horizon bound ``N·Δ²·v/B²`` is 256 · (1/256)² · 64 = 1/4, so a claim at 1/4
holds exactly, and the three-point law of variance 64 with jumps ±257 breaches
within the year with probability at least ≈ 0.2198: a claim below that is
refuted, by Bernoulli's cheaper bound (≈ 0.1988) or, between the two, by the
exact one. The expected energy at the year's end is exactly ≈ 0.1085.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from .goal import SteppedGoal
from .models import HEDGED_SPREAD_STEPPED, SteppedEntry

#: The shock variance (or proxy), the number of marks and the barrier of every
#: example: the notebook's year.
VARIANCE = Fraction(64)
YEAR = 256
BARRIER = Fraction(1)


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
    goal: SteppedGoal
    expected: str


def breach_goal(
    bound: Fraction,
    *,
    claim: Literal["horizon", "fixed-date"] = "horizon",
    law: Literal["two-moment", "sub-gaussian"] = "two-moment",
    entry: SteppedEntry = HEDGED_SPREAD_STEPPED,
    horizon: int = YEAR,
    observation: str = "s",
) -> SteppedGoal:
    """Pose a goal that an exposure's breach of 1 has probability at most ``bound``.

    ``claim`` is ``horizon`` (within ``horizon`` marks) or ``fixed-date`` (at
    every one of them, taken separately). The model is the hedged spread's, and
    its exposure ``s``, unless ``entry`` and ``observation`` name another.
    """
    return SteppedGoal(
        model=entry.key,
        initial=entry.initial,
        start=entry.start,
        step=entry.delta,
        law=law,
        variance=VARIANCE,
        claim=claim,
        horizon=horizon,
        observation=observation,
        barrier=BARRIER,
        bound=bound,
    )


def gauge_goal(bound: Fraction) -> SteppedGoal:
    """Pose a goal that the expected energy at the year's end is at most ``bound``."""
    entry = HEDGED_SPREAD_STEPPED
    return SteppedGoal(
        model=entry.key,
        initial=entry.initial,
        start=entry.start,
        step=entry.delta,
        law="two-moment",
        variance=VARIANCE,
        claim="expected",
        horizon=YEAR,
        observation="V",
        barrier=None,
        bound=bound,
    )


EXAMPLES = (
    Example(
        "breach_within_year",
        "hedged spread from (0, 0) in steps of 1/256, under independent shocks "
        "of a centred law with variance 64: the exposure s breaches 1 within "
        "256 marks with probability at most 1/4, which is the horizon bound "
        "N·Δ²·v/B² exactly",
        breach_goal(Fraction(1, 4)),
        "PROVEN",
    ),
    Example(
        "mark_on_any_date",
        "under independent sub-Gaussian shocks with proxy 64: on each of the "
        "256 marks, taken separately, the exposure s breaches 1 with "
        "probability at most 1/100 (the bound at a mark is ≈ 0.0084)",
        breach_goal(Fraction(1, 100), claim="fixed-date", law="sub-gaussian"),
        "PROVEN",
    ),
    Example(
        "expected_gauge",
        "the expected energy V at mark 256 is at most 33/256, above its exact "
        "value ≈ 0.1085 for every centred law with variance 64",
        gauge_goal(Fraction(33, 256)),
        "PROVEN",
    ),
    Example(
        "tight_limit",
        "the exposure breaches 1 within 256 marks with probability at most "
        "1/10: refuted by the three-point law with jumps ±257, which breaches "
        "with probability ≈ 0.2198, and already by Bernoulli's ≈ 0.1988",
        breach_goal(Fraction(1, 10)),
        "REFUTED",
    ),
    Example(
        "breach_within_year_subgaussian",
        "the same year-long claim under sub-Gaussian shocks, at 1/50: this "
        "version has no horizon theorem for that class",
        breach_goal(Fraction(1, 50), law="sub-gaussian"),
        "UNKNOWN:unsupported",
    ),
    Example(
        "trivial_bound",
        "a probability is at most 1: the year-long claim at 1 holds for every law",
        breach_goal(Fraction(1)),
        "PROVEN",
    ),
    Example(
        "zero_bound",
        "the exposure never breaches 1 within 256 marks: refuted, since the "
        "three-point law breaches with positive probability",
        breach_goal(Fraction(0)),
        "REFUTED",
    ),
    Example(
        "fifth_refuted",
        "the exposure breaches 1 within 256 marks with probability at most "
        "1/5: above Bernoulli's ≈ 0.1988, so refuted by the exact threshold "
        "1 − (1 − 2ε)^256 ≈ 0.2198 that the kernel evaluates",
        breach_goal(Fraction(1, 5)),
        "REFUTED",
    ),
    Example(
        "tight_gauge",
        "the expected energy V at mark 256 is at most 1/10: refuted by the "
        "three-point law of variance 64, whose expected energy is ≈ 0.1085",
        gauge_goal(Fraction(1, 10)),
        "REFUTED",
    ),
    Example(
        "trivial_marks",
        "on each of the 256 marks, taken separately, the exposure breaches 1 "
        "with probability at most 1: true for every law, either class",
        breach_goal(Fraction(1), claim="fixed-date"),
        "PROVEN",
    ),
)
