"""The Euler radius lane: certified truncation and bands for trigonometric streams.

asgard-lean v1.14.0's ``NS.euler_l1_geometric`` says that the vorticity
stream of the two-dimensional Euler equation (``ν = 0``) from a start whose
modes have size at most ``K ≥ 1`` and ℓ¹ norm at most ``L`` has
``l1 ω_n ≤ 3L (72LK)ⁿ``; ``TrigStream.truncationBound_rat`` turns that into the
one-axis bound ``tailBound M ρ r N = M (ρr)^N / (1 − ρr)`` on ``|t| ≤ r`` when
``ρr < 1``, and ``TrigStream.abs_analyticField_le`` bounds the field's sum by
the window's ℓ¹ spread plus that error. Every premise is decided by the kernel:
the size and the ℓ¹ norm of the start through ``Table.sizeLE_toTrig`` and
``Table.l1_le_absSum``, and the ℓ¹ norms of the later coefficients through the
table mirror ``NS.picard`` of the Picard iteration (asgard-lean task 066).

This module mirrors those quantities exactly so the producer can choose the
window and say, when no proof is possible, what the certified bound is. The
mirror only chooses: Lean recomputes every side condition, and a band can be
proved from the bound but never refuted here — the field at a point is a cosine
sum, not a rational, so an uncontained band is ``UNKNOWN``. Positive viscosity
is ``UNKNOWN`` too: asgard-lean certifies no radius for it (``Streams.Gevrey``).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import GoalError, check_rational_bits

from .goal import Modes, TrigBandClaim, TrigTruncationClaim, band_of
from .tail import approximately, describe_bound

#: The largest window a band is tried at; each degree is a kernel-computed
#: table whose size grows quadratically in the degree.
MAX_BAND_WINDOW = 4

Mode = tuple[int, int]
Poly = dict[Mode, Fraction]


class SearchStopped(RuntimeError):
    """A producer hit its deadline, cancellation or exact-arithmetic limit."""

    def __init__(self, outcome: str, detail: str) -> None:
        """Keep the reason the service reports as a structured UNKNOWN."""
        super().__init__(detail)
        self.outcome = outcome


@dataclass(frozen=True)
class _Budget:
    """Cooperative limits checked throughout the Picard mirror."""

    deadline: float | None = None
    cancel: threading.Event | None = None
    limits: EntailmentLimits | None = None

    def check(self) -> None:
        """Stop before the next arithmetic operation if interrupted."""
        if self.cancel is not None and self.cancel.is_set():
            raise SearchStopped("cancelled", "Euler band search cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SearchStopped("timeout", "Euler band search deadline reached")

    def coefficient(self, value: Fraction) -> Fraction:
        """Bound intermediate exact arithmetic, not only the final norm."""
        self.check()
        if self.limits is not None:
            try:
                check_rational_bits(value, self.limits)
            except GoalError as error:
                raise SearchStopped("resource-limit", str(error)) from error
        return value

    def table(self, poly: Poly) -> None:
        """Bound the number of modes retained by a polynomial expansion."""
        self.check()
        if self.limits is not None and len(poly) > self.limits.max_monomials:
            raise SearchStopped(
                "resource-limit", "Euler mode table exceeds max_monomials"
            )


@dataclass(frozen=True, slots=True)
class RadiusEvidence:
    """The constants a truncation proof uses: the start's size and ℓ¹ bounds.

    Attributes:
        size: ``K``, the largest ``|k₁| + |k₂|`` over the start's modes.
        weight: ``L``, the sum of the absolute coefficients of the start.
    """

    size: int
    weight: Fraction


@dataclass(frozen=True, slots=True)
class BandEvidence:
    """The constants a band proof uses, and the window it sums.

    Attributes:
        size: ``K`` as in `RadiusEvidence`.
        weight: ``L`` as in `RadiusEvidence`.
        window: ``N``, the ``t``-degrees summed before the tail.
        norms: The ℓ¹ norms of ``ω_0, …, ω_{N−1}`` as the Picard tables give
            them, computed here and checked by the kernel.
    """

    size: int
    weight: Fraction
    window: int
    norms: tuple[Fraction, ...]

    def __post_init__(self) -> None:
        """Tie the norms to the window."""
        if len(self.norms) != self.window:
            raise ValueError("one norm per degree of the window")


# --- the start ----------------------------------------------------------------


def size(mode: Mode) -> int:
    """Mirror ``Torus.size``: ``|k₁| + |k₂|``."""
    return abs(mode[0]) + abs(mode[1])


def start_table(start: Modes) -> list[tuple[Mode, Fraction]]:
    """Return the start as the table Lean reads, in the goal's order."""
    return [((k1, k2), c) for k1, k2, c in start.terms]


def largest_size(start: Modes) -> int:
    """``K``: the largest size over the start's modes."""
    return max(size((k1, k2)) for k1, k2, _ in start.terms)


def abs_sum(table: list[tuple[Mode, Fraction]]) -> Fraction:
    """Mirror ``Table.absSum``: the sum of the absolute entries."""
    return sum((abs(c) for _, c in table), Fraction(0))


def weight(start: Modes) -> Fraction:
    """``L``: ``Table.absSum`` of the start's table."""
    return abs_sum(start_table(start))


def classical_start_reason(start: Modes) -> str | None:
    """Check physical initial-data premises; Lean checks them again in the proof."""
    coefficients = dict(start_table(start))
    if coefficients.get((0, 0), 0) != 0:
        return "a classical Euler start must be mean-zero"
    if any(
        coefficients.get((-k1, -k2), 0) != c for (k1, k2), c in coefficients.items()
    ):
        return (
            "a classical Euler start must be even: "
            "opposite modes have equal coefficients"
        )
    return None


def geometric(start: Modes) -> tuple[Fraction, Fraction]:
    """``(M, ρ) = (3L, 72LK)``: the geometric bound ``l1 ω_n ≤ M ρⁿ``."""
    big_l, big_k = weight(start), largest_size(start)
    return 3 * big_l, 72 * big_l * big_k


def tail_bound(
    bound: Fraction, ratio: Fraction, radius: Fraction, window: int
) -> Fraction:
    """Mirror ``TrigStream.tailBound M ρ r N = M (ρr)^N / (1 − ρr)``.

    Raises:
        ValueError: If ``ρ r ≥ 1``; no theorem certifies anything there.
    """
    q = ratio * radius
    if q >= 1:
        raise ValueError("no radius is certified at or beyond 1/(72·L·K)")
    return bound * q**window / (1 - q)


# --- the Picard tables (mirror of NS.picard, normalised) ----------------------


def _cross(p: Mode, q: Mode) -> int:
    """Mirror ``Torus.cross``: the two-dimensional cross product."""
    return p[0] * q[1] - p[1] * q[0]


def _lam(p: Mode) -> int:
    """Mirror ``Torus.lam``: ``|p|²``."""
    return p[0] ** 2 + p[1] ** 2


def _add(polys: list[Poly], budget: _Budget) -> Poly:
    """Sum trigonometric polynomials, keeping entries that cancel to zero."""
    out: defaultdict[Mode, Fraction] = defaultdict(Fraction)
    for poly in polys:
        for k, a in poly.items():
            out[k] = budget.coefficient(out[k] + a)
            budget.table(out)
    return dict(out)


def _smul(c: Fraction, poly: Poly, budget: _Budget) -> Poly:
    """Scale a trigonometric polynomial."""
    return {k: budget.coefficient(c * a) for k, a in poly.items()}


def _transport(p_poly: Poly, q_poly: Poly, budget: _Budget) -> Poly:
    """Mirror ``Torus.transport``: ``Σ coupling p q · a_p · b_q`` at ``p + q``."""
    out: defaultdict[Mode, Fraction] = defaultdict(Fraction)
    for p, a in p_poly.items():
        budget.check()
        if _lam(p) == 0:
            continue  # ``coupling 0 q``: Lean's ``cross 0 q / lam 0`` is ``0 / 0 = 0``
        for q, b in q_poly.items():
            budget.check()
            coupling = budget.coefficient(Fraction(_cross(p, q), _lam(p)))
            term = budget.coefficient(budget.coefficient(coupling * a) * b)
            k = (p[0] + q[0], p[1] + q[1])
            out[k] = budget.coefficient(out[k] + term)
            budget.table(out)
    return dict(out)


def picard_norms(start: Modes, window: int) -> tuple[Fraction, ...]:
    """Mirror ``Table.absSum ((NS.picard 0 table n).get n)`` for ``n < window``.

    A mirror at ``ν = 0`` only: the ``ν Δ`` term of ``NS.rhsSeq`` is left out,
    which `replay.supported` keeps honest by refusing every other viscosity.
    The tables Lean computes are normalised (duplicate modes merged, zero
    entries kept), so their absolute sums are the ℓ¹ norms of the
    coefficients, which is what this returns: entries that cancel to zero
    contribute nothing either way.
    """
    return tuple(_norms(start, window, _Budget()))


def _norms(start: Modes, window: int, budget: _Budget) -> Iterator[Fraction]:
    """Yield each norm before calculating the next Picard coefficient."""
    omega: list[Poly] = [_add([{(k1, k2): c} for k1, k2, c in start.terms], budget)]
    for degree in range(window):
        norm = Fraction(0)
        for a in omega[-1].values():
            norm = budget.coefficient(norm + abs(a))
        yield norm
        if degree == window - 1:
            return
        n = degree
        rhs = [
            _smul(Fraction(-1), _transport(omega[m], omega[n - m], budget), budget)
            for m in range(n + 1)
        ]
        omega.append(_smul(Fraction(1, n + 1), _add(rhs, budget), budget))


# --- choosing -----------------------------------------------------------------


def _bits(
    values: tuple[Fraction, ...], limits: EntailmentLimits, what: str
) -> str | None:
    """Say which value is over the coefficient bit limit, if any."""
    try:
        for value in values:
            check_rational_bits(value, limits)
    except GoalError:
        return f"{what} is over the coefficient bit limit"
    return None


def _certified(
    start: Modes, radius: Fraction, *, allow_zero: bool = False
) -> tuple[Fraction, Fraction, int] | str:
    """Return ``(M, ρ, K)`` when the theorems certify ``radius``, else why not.

    ``K`` and ``L`` are the start's; the radius theorem needs ``1 ≤ K`` and
    ``ρ r < 1``. These are the producer's own readings of the hypotheses the
    kernel decides again in the root.
    """
    big_m, rho = geometric(start)
    big_k = largest_size(start)
    if big_k == 0 and not (allow_zero and big_m == 0):
        return "the start has only the zero mode; its stream is constant"
    if rho * radius >= 1:
        return (
            f"the radius r = {radius} is not below the certified radius "
            f"1/(72·L·K) = {1 / rho} (L = {weight(start)}, K = {big_k}); nothing is "
            "certified beyond it"
        )
    return big_m, rho, big_k


def choose_truncation(
    start: Modes,
    claim: TrigTruncationClaim,
    limits: EntailmentLimits,
    *,
    allow_zero: bool = False,
) -> RadiusEvidence | str:
    """Choose the constants of a truncation proof, or say why none exists here.

    Nothing is free to choose: ``K`` and ``L`` are the start's, ``ρ r < 1`` is
    required by the theorem, and the certified bound is ``tailBound`` at the
    claim's window. An ``ε`` below it is ``UNKNOWN``, never ``REFUTED``.
    """
    certified = _certified(start, claim.radius, allow_zero=allow_zero)
    if isinstance(certified, str):
        return certified
    big_m, rho, big_k = certified
    bound = tail_bound(big_m, rho, claim.radius, claim.window)
    over = _bits((bound,), limits, "the certified bound")
    if over is not None:
        return over
    if bound <= claim.epsilon:
        return RadiusEvidence(big_k, weight(start))
    return (
        f"ε = {claim.epsilon} is below the certified bound {describe_bound(bound)} "
        f"({approximately(bound)}) at window {claim.window}; a larger window or a "
        "smaller radius would be needed, and no lower bound on the true error is "
        "checked"
    )


def choose_band(
    start: Modes,
    claim: TrigBandClaim,
    limits: EntailmentLimits,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
    allow_zero: bool = False,
) -> BandEvidence | str:
    """Find a window whose bound proves the band, or say why none here.

    The sum is bounded in absolute value by the window's ℓ¹ spread
    ``Σ_{n<N} l1 ω_n rⁿ`` plus the tail; the smallest window whose bound ``B``
    has ``lo ≤ −B`` and ``B ≤ hi`` is taken. A band the bound does not contain
    is ``UNKNOWN``: the field at a point is a cosine sum, which this lane
    cannot evaluate exactly, so it never refutes.
    """
    bounds = band_of(claim.post)
    if bounds is None:
        return "the postcondition is not a band lo ≤ u ≤ hi"
    lo, hi = bounds
    certified = _certified(start, claim.radius, allow_zero=allow_zero)
    if isinstance(certified, str):
        return certified
    big_m, rho, big_k = certified
    budget = _Budget(deadline, cancel, limits)
    budget.check()
    norms: tuple[Fraction, ...] = ()
    best: tuple[Fraction, int] | None = None
    for window, norm in enumerate(_norms(start, MAX_BAND_WINDOW, budget), 1):
        norms += (norm,)
        spread = sum((a * claim.radius**n for n, a in enumerate(norms)), Fraction(0))
        total = spread + tail_bound(big_m, rho, claim.radius, window)
        over = _bits((*norms, total), limits, "a window norm")
        if over is not None:
            break
        if (lo is None or lo <= -total) and (hi is None or total <= hi):
            return BandEvidence(big_k, weight(start), window, norms)
        if best is None or total < best[0]:
            best = (total, window)
    asked = f"[{lo if lo is not None else '−∞'}, {hi if hi is not None else '∞'}]"
    if best is None:
        return "no window within the coefficient bit limit bounds the field"
    total, window = best
    return (
        f"the window at degree {window} bounds u within "
        f"[{approximately(-total)}, {approximately(total)}] on |t| ≤ {claim.radius}; "
        f"the claim asks {asked}, and this lane evaluates the field at no point, so "
        "no proof and no refutation; a smaller radius would tighten the bound"
    )
