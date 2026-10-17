"""Untrusted exact arithmetic for the two physical mild-stream bounds.

The producer never estimates a certificate from absolute coefficients of an
exponential-polynomial carrier. Lean checks each rational premise and the
complete circuit contract again. Carrier tables are separate report evidence.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import GoalError, check_rational_bits

from .goal import Modes
from .trig import SearchStopped

MAX_BAND_WINDOW = 8


@dataclass(frozen=True, slots=True)
class MildEvidence:
    """Rational theorem premises, never a validated mathematical conclusion."""

    kind: Literal["mild-scale", "mild-contraction"]
    size: int
    weight: Fraction
    window: int
    radius: Fraction | None = None


@dataclass(frozen=True, slots=True)
class _Budget:
    """Cooperative interruption and exact arithmetic limits."""

    limits: EntailmentLimits
    deadline: float | None
    cancel: threading.Event | None

    def check(self, *values: Fraction) -> None:
        """Check interruption before bounding each intermediate rational."""
        if self.cancel is not None and self.cancel.is_set():
            raise SearchStopped("cancelled", "mild bound search cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SearchStopped("timeout", "mild bound search deadline reached")
        try:
            for value in values:
                check_rational_bits(value, self.limits)
        except GoalError as error:
            raise SearchStopped("resource-limit", str(error)) from error


def admitted_start(start: Modes) -> bool:
    """Check aggregate evenness and zero mean; Lean checks them independently."""
    coefficients: dict[tuple[int, int], Fraction] = {}
    for k1, k2, value in start.terms:
        mode = (k1, k2)
        coefficients[mode] = coefficients.get(mode, Fraction(0)) + value
    return coefficients.get((0, 0), Fraction(0)) == 0 and all(
        coefficients.get((-k1, -k2), Fraction(0)) == value
        for (k1, k2), value in coefficients.items()
    )


def start_constants(start: Modes) -> tuple[int, Fraction]:
    """Use safe raw table bounds, except exact aggregate zero has zero mass."""
    coefficients: dict[tuple[int, int], Fraction] = {}
    for k1, k2, value in start.terms:
        mode = (k1, k2)
        coefficients[mode] = coefficients.get(mode, Fraction(0)) + value
    if not any(coefficients.values()):
        return 1, Fraction(0)
    return max(1, *(abs(k1) + abs(k2) for k1, k2, _ in start.terms)), sum(
        (abs(value) for _, _, value in start.terms), Fraction(0)
    )


def _heat_radius(target: Fraction, weight: Fraction, budget: _Budget) -> Fraction:
    """Find an upper rational square root below the strict contraction cutoff."""
    numerator = math.isqrt(target.numerator)
    denominator = math.isqrt(target.denominator)
    if numerator**2 == target.numerator and denominator**2 == target.denominator:
        return Fraction(numerator, denominator)
    lower, upper = Fraction(0), 1 / (4 * weight)
    for _ in range(24):
        middle = (lower + upper) / 2
        square = middle * middle
        budget.check(middle, square)
        if square >= target:
            upper = middle
        else:
            lower = middle
    return upper


def candidates(
    start: Modes,
    viscosity: Fraction,
    horizon: Fraction,
    window: int,
    limits: EntailmentLimits,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> tuple[MildEvidence, ...]:
    """Try both theorem routes, returning only exactly admissible premises."""
    budget = _Budget(limits, deadline, cancel)
    budget.check(viscosity, horizon)
    if viscosity < 0 or horizon < 0 or window < 1 or not admitted_start(start):
        return ()
    size, weight = start_constants(start)
    budget.check(weight)
    if weight == 0:
        return (MildEvidence("mild-scale", size, weight, window),)
    found: list[MildEvidence] = []
    limited: SearchStopped | None = None
    try:
        ratio = 72 * weight * size * horizon
        budget.check(ratio)
        if ratio < 1:
            found.append(MildEvidence("mild-scale", size, weight, window))
    except SearchStopped as error:
        if error.outcome != "resource-limit":
            raise
        limited = error
    try:
        if viscosity > 0:
            target = horizon / viscosity
            budget.check(target)
            if target < 1 / (16 * weight * weight):
                radius = _heat_radius(target, weight, budget)
                if radius * radius >= target and 4 * radius * weight < 1:
                    found.append(
                        MildEvidence("mild-contraction", size, weight, window, radius)
                    )
    except SearchStopped as error:
        if error.outcome != "resource-limit":
            raise
        limited = error
    if not found and limited is not None:
        raise limited
    return tuple(found)


def _catalan(index: int) -> int:
    """Compute the exact Catalan number in the proved scalar majorant."""
    return math.comb(2 * index, index) // (index + 1)


def tail_bound(evidence: MildEvidence, horizon: Fraction) -> Fraction:
    """Mirror the proved rational tail, retaining the Catalan prefactor."""
    if evidence.kind == "mild-scale":
        ratio = 72 * evidence.weight * evidence.size * horizon
        if not 0 <= ratio < 1:
            raise ValueError("Euler-scale ratio must lie in [0,1)")
        return 3 * evidence.weight * ratio**evidence.window / (1 - ratio)
    if evidence.radius is None:
        raise ValueError("contraction evidence needs a rational heat radius")
    ratio = 4 * evidence.radius * evidence.weight
    if not 0 <= ratio < 1:
        raise ValueError("contraction ratio must lie in [0,1)")
    coefficient = (
        evidence.weight * (evidence.radius * evidence.weight) ** evidence.window
    )
    return coefficient * _catalan(evidence.window) / (1 - ratio)


def band_bound(evidence: MildEvidence, horizon: Fraction) -> Fraction:
    """Mirror the finite physical majorant prefix plus its certified tail."""
    if evidence.kind == "mild-scale":
        ratio = 72 * evidence.weight * evidence.size * horizon
        prefix = evidence.weight + sum(
            (3 * evidence.weight * ratio**n for n in range(1, evidence.window)),
            Fraction(0),
        )
    else:
        if evidence.radius is None:
            raise ValueError("contraction evidence needs a rational heat radius")
        prefix = sum(
            (
                evidence.weight * (evidence.radius * evidence.weight) ** n * _catalan(n)
                for n in range(evidence.window)
            ),
            Fraction(0),
        )
    return prefix + tail_bound(evidence, horizon)


def choose_truncation(
    start: Modes,
    viscosity: Fraction,
    horizon: Fraction,
    window: int,
    epsilon: Fraction,
    limits: EntailmentLimits,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> MildEvidence | str:
    """Choose the smaller certified tail; failure is UNKNOWN, never refutation."""
    budget = _Budget(limits, deadline, cancel)
    found = candidates(
        start, viscosity, horizon, window, limits, deadline=deadline, cancel=cancel
    )
    ranked: list[tuple[Fraction, MildEvidence]] = []
    limited: SearchStopped | None = None
    for item in found:
        try:
            bound = tail_bound(item, horizon)
            budget.check(bound)
            ranked.append((bound, item))
        except SearchStopped as error:
            if error.outcome != "resource-limit":
                raise
            limited = error
    if not ranked and limited is not None:
        raise limited
    if not ranked:
        return "neither physical bound certifies this forward time and initial profile"
    bound, best = min(ranked, key=lambda entry: entry[0])
    if bound <= epsilon:
        return best
    return (
        f"requested error {epsilon} is below the best certified tail {bound}; no error"
        " lower bound is proved"
    )


def choose_band(
    start: Modes,
    viscosity: Fraction,
    horizon: Fraction,
    lower: Fraction | None,
    upper: Fraction | None,
    limits: EntailmentLimits,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> MildEvidence | str:
    """Try both bounds and finite prefixes, retaining the strongest admitted band."""
    budget = _Budget(limits, deadline, cancel)
    ranked: list[tuple[Fraction, MildEvidence]] = []
    limited: SearchStopped | None = None
    for window in range(1, MAX_BAND_WINDOW + 1):
        for item in candidates(
            start, viscosity, horizon, window, limits, deadline=deadline, cancel=cancel
        ):
            try:
                bound = band_bound(item, horizon)
                budget.check(bound)
                ranked.append((bound, item))
            except SearchStopped as error:
                if error.outcome != "resource-limit":
                    raise
                limited = error
    if not ranked and limited is not None:
        raise limited
    if not ranked:
        return "neither physical bound certifies this forward time and initial profile"
    bound, best = min(ranked, key=lambda entry: entry[0])
    if (lower is None or lower <= -bound) and (upper is None or bound <= upper):
        return best
    return (
        f"the best certified band is [{-bound}, {bound}]; the requested band remains"
        " undecided"
    )
