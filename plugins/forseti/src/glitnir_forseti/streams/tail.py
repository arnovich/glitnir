"""Certified truncation of an analytic tail: the heat boundary and the front.

For an exponential-sum profile ``Σᵢ cᵢ·e^(aᵢx)``, asgard-lean's
``AnalyticHeat.expSum_truncation`` proves that every stream the heat circuit
reconstructs from that boundary is within ``ε`` of its window ``N`` on a box,
given ``ρ > 0`` with every ``|aᵢ|·ρ ≤ 1``, a box strictly inside the radii
``[ρ², ρ]``, and ``tailBound ≤ ε`` for the majorant ``Σᵢ |cᵢ|``. Each premise is
decided by the kernel. The evidence is ``ρ`` alone.

For the viscous Burgers circuit at ``ν > 0``, ``ColeHopf.front_truncation``
proves the same of every stream it reconstructs from the ``t = 0`` slice of the
front ``−2ν φ_x/φ``, where ``φ`` is the heat stream of the exponential-sum
profile: given ``ρ`` as above, radii ``r`` strictly inside the heat radii
``[ρ²/ν, ρ]`` at which the inverse is majorized (``smallEnough``), a box
strictly inside ``r/2``, and ``tailBound ≤ ε`` for the front's majorant. The
evidence is ``ρ`` and ``r``.

This module mirrors both bounds exactly so the producer can choose the
evidence and say, when no proof is possible, what the certified bound is. The
mirror only chooses: Lean recomputes every side condition. It makes no claim
about the true error, so an ``ε`` below the certified bound is ``UNKNOWN``,
never ``REFUTED``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import GoalError, check_rational_bits

from .goal import ExpSum, FieldClaim, TruncationClaim, band_of

#: How many times ``ρ`` may be doubled for a constant profile (every ``aᵢ = 0``).
_MAX_DOUBLINGS = 32
#: The window a field band is certified at: the certificate's window and the
#: size of the checked inverse table. Larger shrinks ``ε`` but not the window's
#: spread, which low-order terms dominate, at quadratic kernel cost.
BAND_WINDOW = (16, 16)
#: The front's radii are tried on the grid ``(k/K) · heatRadii``, ``0 < k < K``.
#: ``K = 12`` is the smallest grid holding asgard-lean's ``BurgersFront`` radii
#: ``(2/3, 1/2) = (8/12, 6/12)`` of the heat radii ``(2, 1)``, so the decider
#: reproduces that example; each ``ρ`` costs ``(K − 1)²`` exact bound
#: evaluations, times the doublings for a constant potential.
_GRID = 12


@dataclass(frozen=True, slots=True)
class TailEvidence:
    """The radius parameter ``ρ`` a heat truncation proof uses."""

    rho: Fraction


@dataclass(frozen=True, slots=True)
class WindowEvidence:
    """A checked window of the front, for a field band.

    Attributes:
        rho: The profile radius ``ρ``.
        radii: ``(r_t, r_x)``, the inverse's majorant radii.
        window: ``(N_t, N_x)``, the certificate's window and the table's size.
        epsilon: The certified truncation error at that window.
        table: The window of ``φ⁻¹``, ``N_t`` rows of ``N_x`` rationals,
            computed here and checked by Lean's ``WindowIdentity``.
        point: For a refutation, the rational point of the box where the
            window exceeds the band by more than ``ε``; ``None`` for a proof.
        side: ``above`` (``W(p) − ε > hi``) or ``below`` (``W(p) + ε < lo``)
            for a refutation, empty for a proof.
    """

    rho: Fraction
    radii: tuple[Fraction, Fraction]
    window: tuple[int, int]
    epsilon: Fraction
    table: tuple[tuple[Fraction, ...], ...]
    point: tuple[Fraction, Fraction] | None = None
    side: str = ""

    def __post_init__(self) -> None:
        """Tie the table to the window and the side to the point."""
        n_t, n_x = self.window
        if len(self.table) != n_t or any(len(row) != n_x for row in self.table):
            raise ValueError("the table has the window's shape")
        if self.side not in ("", "above", "below"):
            raise ValueError("the side is above, below or empty")
        if (self.point is None) != (self.side == ""):
            raise ValueError("a refutation has a point and a side; a proof neither")

    @property
    def refutes(self) -> bool:
        """Whether this evidence refutes the band rather than proving it."""
        return self.point is not None


@dataclass(frozen=True, slots=True)
class FrontEvidence:
    """The parameters a Cole–Hopf truncation proof uses.

    Attributes:
        rho: The profile radius ``ρ``.
        radii: ``(r_t, r_x)``, the radii at which the inverse is majorized;
            the front's majorant has radii ``r/2``.
    """

    rho: Fraction
    radii: tuple[Fraction, Fraction]


# --- the heat boundary (Gimle.Asgard.Streams.AnalyticHeat) -------------------


def weight(profile: ExpSum) -> Fraction:
    """Return the majorant bound ``M = Σᵢ |cᵢ|`` (``expSumWeight``)."""
    return sum((abs(c) for c, _ in profile.terms), Fraction(0))


def fits(profile: ExpSum, rho: Fraction) -> bool:
    """Mirror ``expSumFits``: every ``|aᵢ|·ρ ≤ 1``."""
    return all(abs(a) * rho <= 1 for _, a in profile.terms)


def inside(claim: TruncationClaim, rho: Fraction) -> bool:
    """Mirror the box condition: ``r_t < ρ²`` and ``r_x < ρ``."""
    return claim.radius[0] < rho * rho and claim.radius[1] < rho


def _tail_bound(
    bound: Fraction, radii: tuple[Fraction, Fraction], claim: TruncationClaim
) -> Fraction:
    """Mirror ``tailBound m box N`` for the majorant ``⟨bound, radii⟩``.

    ``M · (Σᵢ qᵢ^Nᵢ) · ∏ᵢ (1 − qᵢ)⁻¹`` with ``qᵢ = boxᵢ/radiiᵢ``. Only
    meaningful when the box is inside the radii.
    """
    ratios = tuple(b / r for b, r in zip(claim.radius, radii))
    total = sum((q**n for q, n in zip(ratios, claim.window)), Fraction(0))
    product = Fraction(1)
    for q in ratios:
        product /= 1 - q
    return bound * total * product


def tail_bound(profile: ExpSum, claim: TruncationClaim, rho: Fraction) -> Fraction:
    """Mirror ``tailBound (heatMajorant M ρ) box N``, exactly."""
    return _tail_bound(weight(profile), (rho * rho, rho), claim)


def _candidate_rho(
    profile: ExpSum, claim: TruncationClaim, limits: EntailmentLimits
) -> Iterator[Fraction]:
    """Yield the candidate ``ρ``: ``1/max|aᵢ|``, or doublings for a constant."""
    rates = [abs(a) for _, a in profile.terms if a]
    if rates:
        candidates = [1 / max(rates)]
    else:
        start = max(Fraction(1), 2 * claim.radius[1], 2 * claim.radius[0] + 1)
        candidates = [start * 2**k for k in range(_MAX_DOUBLINGS)]
    for rho in candidates:
        try:
            check_rational_bits(rho, limits)
        except GoalError:
            return
        yield rho


def choose(
    profile: ExpSum, claim: TruncationClaim, limits: EntailmentLimits
) -> TailEvidence | str:
    """Choose ``ρ`` for a heat truncation proof, or say why none exists here.

    The certified bound decreases as ``ρ`` grows, so the largest ``ρ`` the
    profile fits is best: ``1/max|aᵢ|``. A constant profile fits every ``ρ``,
    which is doubled until the bound is small enough.
    """
    bound = None
    for rho in _candidate_rho(profile, claim, limits):
        if not inside(claim, rho):
            return (
                f"the box (r_t, r_x) = ({claim.radius[0]}, {claim.radius[1]}) is not "
                f"strictly inside the radii (ρ², ρ) = ({rho * rho}, {rho})"
            )
        bound = tail_bound(profile, claim, rho)
        if bound <= claim.epsilon:
            return TailEvidence(rho)
    if bound is None:
        return "no radius ρ within the coefficient bit limit fits the box"
    return _below(claim, bound)


def _below(claim: TruncationClaim, bound: Fraction, where: str = "") -> str:
    return (
        f"ε = {claim.epsilon} is below the certified bound "
        f"{describe_bound(bound)}{where}; a tighter window or a smaller box would be "
        "needed, and no lower bound on the "
        "true error is checked"
    )


def approximately(value: Fraction) -> str:
    """Show an exact rational to four decimals, for a reason a reader scans.

    The decimal is description only; the exact value stays in the evidence.
    """
    scaled = math.floor(abs(value) * 10_000 + Fraction(1, 2))  # half away from zero
    sign = "-" if value < 0 else ""
    whole, part = divmod(scaled, 10_000)
    return f"≈ {sign}{whole}.{part:04d}"


def describe_bound(value: Fraction) -> str:
    """Show an exact bound, or its size when it is too long to read."""
    if max(value.numerator.bit_length(), value.denominator.bit_length()) <= 200:
        return str(value)
    return f"≈ 2^{value.numerator.bit_length() - value.denominator.bit_length()}"


# --- the Cole–Hopf front (asgard-lean ColeHopf) -------------------------------


def constant_term(profile: ExpSum) -> Fraction:
    """Mirror ``constantTerm``: ``Σᵢ cᵢ``, the potential's value at ``x = 0``."""
    return sum((c for c, _ in profile.terms), Fraction(0))


def rest_weight(profile: ExpSum) -> Fraction:
    """Mirror ``restWeight``: ``Σ |cᵢ|`` over the terms with ``aᵢ ≠ 0``."""
    return sum((abs(c) for c, a in profile.terms if a != 0), Fraction(0))


def heat_radii(viscosity: Fraction, rho: Fraction) -> tuple[Fraction, Fraction]:
    """Mirror ``heatRadii ν ρ = [ρ²/ν, ρ]``, the potential's majorant radii."""
    return (rho * rho / viscosity, rho)


def front_bound(viscosity: Fraction, profile: ExpSum) -> Fraction:
    """Mirror ``frontBound``: ``|2ν| · (Σᵢ |cᵢ aᵢ|) / |Σᵢ cᵢ|``."""
    shifted = sum((abs(c * a) for c, a in profile.terms), Fraction(0))
    return abs(-2 * viscosity) * (shifted / abs(constant_term(profile)))


def small_enough(
    viscosity: Fraction,
    rho: Fraction,
    profile: ExpSum,
    radii: tuple[Fraction, Fraction],
) -> bool:
    """Mirror ``smallEnough``: ``(W/|c|) · (∏ᵢ (1 − rᵢ/Rᵢ)⁻¹ − 1) ≤ 1``.

    ``W`` is the rest weight, ``c`` the constant term and ``R`` the heat radii;
    it is the condition under which the inverse ``φ⁻¹`` is majorized at ``r``.
    Only meaningful when ``r`` is strictly inside ``R``.
    """
    product = Fraction(1)
    for r, big in zip(radii, heat_radii(viscosity, rho)):
        product /= 1 - r / big
    return rest_weight(profile) / abs(constant_term(profile)) * (product - 1) <= 1


def front_tail_bound(
    viscosity: Fraction,
    profile: ExpSum,
    claim: TruncationClaim,
    radii: tuple[Fraction, Fraction],
) -> Fraction:
    """Mirror ``tailBound (frontMajorant ν terms r _) box N``: radii ``r/2``."""
    half = (radii[0] / 2, radii[1] / 2)
    return _tail_bound(front_bound(viscosity, profile), half, claim)


def _front_radii(
    viscosity: Fraction,
    profile: ExpSum,
    claim: TruncationClaim,
    limits: EntailmentLimits,
) -> tuple[Fraction, tuple[Fraction, Fraction], Fraction] | str:
    """Choose ``ρ`` and ``r`` with the least certified bound, or say why none.

    Returns ``(ρ, r, bound)``; the claim's ``ε`` is not consulted.
    """
    if constant_term(profile) == 0:
        return "the potential's constant term Σ cᵢ is zero: no front"
    reason = "no radius ρ within the coefficient bit limit fits the box"
    for rho in _candidate_rho(profile, claim, limits):
        found = _best_radii(viscosity, profile, claim, rho, limits)
        if isinstance(found, tuple):
            bound, radii = found
            return rho, radii, bound
        if rest_weight(profile) != 0:
            return found  # a larger ρ would not fit the profile
        reason = found  # a constant potential fits every ρ: double it
    return reason


def choose_front(
    viscosity: Fraction,
    profile: ExpSum,
    claim: TruncationClaim,
    limits: EntailmentLimits,
) -> FrontEvidence | str:
    """Choose ``ρ`` and ``r`` for a front truncation proof, or say why none here.

    ``ρ`` is chosen as for the heat boundary. The radii ``r`` trade off: the
    bound falls as they grow, and ``smallEnough`` caps them below the heat
    radii. They are tried on a fixed rational grid inside the heat radii, and
    the pair with the least certified bound among those that satisfy
    ``smallEnough`` and keep the box strictly inside ``r/2`` is taken.

    The caller has checked ``ν > 0``; a potential with ``Σᵢ cᵢ = 0`` has no
    front and is refused here too, since the mirror would divide by it.
    """
    found = _front_radii(viscosity, profile, claim, limits)
    if isinstance(found, str):
        return found
    rho, radii, bound = found
    if bound <= claim.epsilon:
        return FrontEvidence(rho, radii)
    return _below(claim, bound, " (the least on the radii grid)")


def _best_radii(
    viscosity: Fraction,
    profile: ExpSum,
    claim: TruncationClaim,
    rho: Fraction,
    limits: EntailmentLimits,
) -> tuple[Fraction, tuple[Fraction, Fraction]] | str:
    """Return the least certified bound and its radii on the grid at ``ρ``.

    Or say why no grid point qualifies: the box reaches half of every radii
    below the heat radii, or the inverse is majorized at none that hold it.
    Radii over the coefficient bit limit are skipped, since the artifact could
    not carry them.
    """
    big = heat_radii(viscosity, rho)
    best: tuple[Fraction, tuple[Fraction, Fraction]] | None = None
    holds_box = False
    for k_t in range(1, _GRID):
        for k_x in range(1, _GRID):
            radii = (big[0] * k_t / _GRID, big[1] * k_x / _GRID)
            try:
                for r in radii:
                    check_rational_bits(r, limits)
            except GoalError:
                continue
            if not all(b < r / 2 for b, r in zip(claim.radius, radii)):
                continue
            holds_box = True
            if not small_enough(viscosity, rho, profile, radii):
                continue
            bound = front_tail_bound(viscosity, profile, claim, radii)
            if best is None or bound < best[0]:
                best = (bound, radii)
    if best is not None:
        return best
    where = f"below the heat radii (ρ²/ν, ρ) = ({big[0]}, {big[1]})"
    if not holds_box:
        return (
            f"the box (r_t, r_x) = ({claim.radius[0]}, {claim.radius[1]}) is not "
            f"strictly inside half of any radii on the grid {where}"
        )
    return (
        f"the inverse of the potential is majorized at no radii on the grid {where} "
        f"that hold the box: its rest weight Σ |cᵢ| over aᵢ ≠ 0 is "
        f"{rest_weight(profile)} against the constant term {constant_term(profile)}"
    )


# --- the front's window, for field bands ---------------------------


def exp_sum(profile: ExpSum, j: int) -> Fraction:
    """Mirror ``expSum``: the profile's raw EGF coefficient ``Σᵢ cᵢ aᵢ^j``."""
    return sum((c * a**j for c, a in profile.terms), Fraction(0))


def shifted(profile: ExpSum) -> ExpSum:
    """Mirror ``shifted``: the profile of ``D_x φ``, ``(cᵢ aᵢ, aᵢ)``."""
    return ExpSum(tuple((c * a, a) for c, a in profile.terms))


def heat_coeff(viscosity: Fraction, profile: ExpSum, n: int, k: int) -> Fraction:
    """Mirror ``heatCoeff``: ``ν^n g_(k+2n) / (n! k!)`` for the profile ``g``."""
    return (
        viscosity**n
        * exp_sum(profile, k + 2 * n)
        / (math.factorial(n) * math.factorial(k))
    )


def inverse_table(
    viscosity: Fraction, profile: ExpSum, window: tuple[int, int]
) -> tuple[tuple[Fraction, ...], ...]:
    """Compute the window of ``φ⁻¹`` by the triangular recurrence, ``Σ cᵢ ≠ 0``.

    Lean checks it through ``WindowIdentity``; a wrong table loses the proof.
    """
    n_t, n_x = window
    c = heat_coeff(viscosity, profile, 0, 0)
    table = [[Fraction(0)] * n_x for _ in range(n_t)]
    for n in range(n_t):
        for k in range(n_x):
            if (n, k) == (0, 0):
                table[0][0] = 1 / c
                continue
            total = Fraction(0)
            for i in range(n + 1):
                for j in range(k + 1):
                    if (i, j) != (0, 0):
                        total += (
                            heat_coeff(viscosity, profile, i, j) * table[n - i][k - j]
                        )
            table[n][k] = -total / c
    return tuple(tuple(row) for row in table)


def front_window(
    viscosity: Fraction,
    profile: ExpSum,
    table: tuple[tuple[Fraction, ...], ...],
    window: tuple[int, int],
) -> tuple[tuple[Fraction, ...], ...]:
    """Mirror ``frontWindow``: ``−2ν (D_x φ · Q)`` below the window."""
    n_t, n_x = window
    moved = shifted(profile)
    rows = []
    for n in range(n_t):
        row = []
        for k in range(n_x):
            total = Fraction(0)
            for i in range(n + 1):
                for j in range(k + 1):
                    total += heat_coeff(viscosity, moved, i, j) * table[n - i][k - j]
            row.append(-2 * viscosity * total)
        rows.append(tuple(row))
    return tuple(rows)


def window_spread(
    coefficients: tuple[tuple[Fraction, ...], ...], radius: tuple[Fraction, Fraction]
) -> Fraction:
    """Mirror ``windowSpread``: ``Σ_{(n,k) ≠ 0} |W_nk| r_t^n r_x^k``."""
    r_t, r_x = radius
    return sum(
        (
            abs(c) * r_t**n * r_x**k
            for n, row in enumerate(coefficients)
            for k, c in enumerate(row)
            if (n, k) != (0, 0)
        ),
        Fraction(0),
    )


def window_value(
    coefficients: tuple[tuple[Fraction, ...], ...], point: tuple[Fraction, Fraction]
) -> Fraction:
    """Mirror ``frontValue``: the window polynomial at a rational point."""
    t, x = point
    return sum(
        (
            c * t**n * x**k
            for n, row in enumerate(coefficients)
            for k, c in enumerate(row)
        ),
        Fraction(0),
    )


def choose_band(
    viscosity: Fraction,
    profile: ExpSum,
    claim: FieldClaim,
    limits: EntailmentLimits,
) -> WindowEvidence | str:
    """Find a checked window that proves the band, or a point that refutes it.

    The box is the claim's (symmetric, checked by the caller), the window
    `BAND_WINDOW`, ``ρ`` and ``r`` the truncation chooser's at that window and
    ``ε`` its certified bound. The window's constant term ``W₀``, spread ``B``
    over the box and ``ε`` give ``u ∈ [W₀ − B − ε, W₀ + B + ε]``; a band
    containing that interval is proved. Otherwise the box's corners and centre
    are tried as refutations, where the window leaves the band by more than
    ``ε``. Anything else is ``UNKNOWN`` with both intervals in the reason.
    """
    bounds = band_of(claim.post)
    if bounds is None:
        return "the postcondition is not a band lo ≤ u ≤ hi"
    lo, hi = bounds
    radius = (claim.time[1], claim.space[1])
    truncation = TruncationClaim(claim.port, BAND_WINDOW, radius, Fraction(0))
    found = _front_radii(viscosity, profile, truncation, limits)
    if isinstance(found, str):
        return found
    rho, radii, epsilon = found
    try:
        check_rational_bits(epsilon, limits)
    except GoalError:
        return "the certified error is over the coefficient bit limit"
    table = inverse_table(viscosity, profile, BAND_WINDOW)
    try:
        for row in table:
            for value in row:
                check_rational_bits(value, limits)
    except GoalError:
        return "the inverse's window is over the coefficient bit limit"
    coefficients = front_window(viscosity, profile, table, BAND_WINDOW)
    centre = coefficients[0][0]
    spread = window_spread(coefficients, radius)
    low, high = centre - spread - epsilon, centre + spread + epsilon
    if (lo is None or lo <= low) and (hi is None or high <= hi):
        return WindowEvidence(rho, radii, BAND_WINDOW, epsilon, table)
    r_t, r_x = radius
    for point in (
        (r_t, r_x),
        (r_t, -r_x),
        (-r_t, r_x),
        (-r_t, -r_x),
        (Fraction(0), Fraction(0)),
    ):
        value = window_value(coefficients, point)
        if hi is not None and value - epsilon > hi:
            return WindowEvidence(
                rho, radii, BAND_WINDOW, epsilon, table, point, "above"
            )
        if lo is not None and value + epsilon < lo:
            return WindowEvidence(
                rho, radii, BAND_WINDOW, epsilon, table, point, "below"
            )
    asked = f"[{lo if lo is not None else '−∞'}, {hi if hi is not None else '∞'}]"
    return (
        f"the window at {BAND_WINDOW[0]}×{BAND_WINDOW[1]} bounds u within "
        f"[{approximately(low)}, {approximately(high)}] on the box (spread "
        f"{approximately(spread)}, ε = {describe_bound(epsilon)}); the claim asks "
        f"{asked}, and neither the corners nor the centre of the box leave it by "
        "more than ε, so no proof and no refutation; a smaller box would tighten "
        "the bound"
    )
