"""The stream examples: the heat reconstruction and the Burgers front.

The heat circuit rebuilds ``u`` from ``u_xx`` and a boundary profile on axes
``[t, x]``; with the profile ``x²`` the solution is ``u = x² + 2t``. The
Burgers circuit at ``ν = 1/2`` rebuilds the Cole–Hopf front of the potential
``1 + e^(−x)`` from its ``t = 0`` slice. The examples are written as data; no
evidence is.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Formula,
    Mul,
    Neg,
    Poly,
    Relation,
    Var,
)

from .goal import (
    CoefficientClaim,
    ExpSum,
    FieldClaim,
    FullStreamClaim,
    MildBandClaim,
    MildTruncationClaim,
    Modes,
    Profile,
    StreamGoal,
    TrigBandClaim,
    TrigTruncationClaim,
    TruncationClaim,
)
from .models import BURGERS, HEAT, PROFILE, VORTICITY


def _const(value: int) -> Const:
    return Const(Fraction(value))


def _equals(left: Poly, right: Poly) -> Formula:
    difference = Add(left, Neg(right))
    return And(Atom(Relation.GE, difference), Atom(Relation.LE, difference))


def heat_goal(
    claim: object,
    *,
    interpretation: str | None = None,
    basis: str = "ogf",
    profile: Profile = PROFILE,
) -> StreamGoal:
    """Pose a goal about the heat reconstruction with the declared profile."""
    if interpretation is None:
        interpretation = (
            "evaluated-field" if isinstance(claim, FieldClaim) else "formal-stream"
        )
    return StreamGoal(
        "formal-heat", interpretation, basis, profile, claim  # type: ignore[arg-type]
    )


def burgers_goal(
    claim: object,
    *,
    viscosity: Fraction | None = Fraction(1, 2),
    interpretation: str = "analytic-tail",
    basis: str = "ogf",
    profile: Profile | None = None,
) -> StreamGoal:
    """Pose a goal about the Burgers front; the potential ``1 + e^(−x)`` by default."""
    return StreamGoal(
        BURGERS.key,
        interpretation,
        basis,
        FRONT if profile is None else profile,
        claim,  # type: ignore[arg-type]
        viscosity,
    )


_T, _X, _U = Var(0), Var(1), Var(2)
assert HEAT.coefficients is not None and HEAT.full_stream is not None
OBSERVED = HEAT.coefficients.observed
OUTPUTS = HEAT.full_stream.outputs


def field_claim(post: Formula) -> FieldClaim:
    """Pose a field claim on the unit strip ``[0, 1] × [−1, 1]``."""
    return FieldClaim(
        "u", (Fraction(0), Fraction(1)), (Fraction(-1), Fraction(1)), post
    )


def band(bound: int) -> Formula:
    """``0 ≤ u ≤ bound``."""
    return And(Atom(Relation.GE, _U), Atom(Relation.LE, Add(_U, Neg(_const(bound)))))


#: The profile ``e^x`` and ``cosh x``, as exponential sums.
EXP = ExpSum(((Fraction(1), Fraction(1)),))
COSH = ExpSum(((Fraction(1, 2), Fraction(1)), (Fraction(1, 2), Fraction(-1))))
#: The Cole–Hopf potential ``1 + e^(−x)`` of the front ``1/(1 + e^(x − t/2))``.
FRONT = ExpSum(((Fraction(1), Fraction(0)), (Fraction(1), Fraction(-1))))
#: The potential ``e^(−x) − 1``, with no constant term: no front.
NO_FRONT = ExpSum(((Fraction(-1), Fraction(0)), (Fraction(1), Fraction(-1))))


def truncation(
    epsilon: Fraction,
    radius: tuple[Fraction, Fraction] = (Fraction(1, 4), Fraction(1, 2)),
) -> TruncationClaim:
    """Pose a truncation claim about ``u`` at the window ``(8, 16)``."""
    return TruncationClaim("u", (8, 16), radius, epsilon)


def front_truncation(
    epsilon: Fraction,
    radius: tuple[Fraction, Fraction] = (Fraction(1, 6), Fraction(1, 8)),
) -> TruncationClaim:
    """Pose a truncation claim about ``u`` at the window ``(16, 16)``."""
    return TruncationClaim("u", (16, 16), radius, epsilon)


def front_band(
    lo: Fraction | None,
    hi: Fraction | None,
    radius: tuple[Fraction, Fraction] = (Fraction(1, 6), Fraction(1, 8)),
) -> FieldClaim:
    """Pose a band ``lo ≤ u ≤ hi`` (one side or both) on the front's box."""
    parts: list[Formula] = []
    if lo is not None:
        parts.append(Atom(Relation.GE, Add(_U, Neg(Const(lo)))))
    if hi is not None:
        parts.append(Atom(Relation.LE, Add(_U, Neg(Const(hi)))))
    post = parts[0] if len(parts) == 1 else And(parts[0], parts[1])
    r_t, r_x = radius
    return FieldClaim("u", (-r_t, r_t), (-r_x, r_x), post)


def band_goal(claim: FieldClaim, **kwargs: object) -> StreamGoal:
    """Pose a field band about the Burgers front, read as an analytic field."""
    kwargs.setdefault("interpretation", "analytic-field")
    return burgers_goal(claim, **kwargs)  # type: ignore[arg-type]


#: The start ``cos x + cos(x + y) + cos(2x + y)`` of asgard-lean's
#: ``Examples.EulerThreeMode``, as the six modes of its cosines.
EULER_START = Modes(
    tuple(
        (k1, k2, Fraction(1, 2))
        for k1, k2 in ((1, 0), (-1, 0), (1, 1), (-1, -1), (2, 1), (-2, -1))
    )
)
#: The radius of the Euler page's certified claims: a tenth of ``1/648``.
EULER_RADIUS = Fraction(1, 6480)


def vorticity_goal(
    claim: TrigTruncationClaim | TrigBandClaim,
    *,
    viscosity: Fraction | None = Fraction(0),
    interpretation: str | None = None,
    basis: str = "ogf",
    start: Profile = EULER_START,
) -> StreamGoal:
    """Pose a claim about the vorticity stream from a start of modes."""
    if interpretation is None:
        interpretation = (
            "trig-truncation" if type(claim) is TrigTruncationClaim else "trig-band"
        )
    return StreamGoal(
        VORTICITY.key, interpretation, basis, start, claim, viscosity=viscosity
    )


def trig_truncation(
    epsilon: Fraction, window: int = 3, radius: Fraction = EULER_RADIUS
) -> TrigTruncationClaim:
    """Pose a truncation claim about ``u`` through ``t^(window − 1)``."""
    return TrigTruncationClaim("u", window, radius, epsilon)


def trig_band(
    lo: Fraction | None, hi: Fraction | None, radius: Fraction = EULER_RADIUS
) -> TrigBandClaim:
    """Pose a band ``lo ≤ u ≤ hi`` (one side or both) on ``|t| ≤ radius``."""
    parts: list[Formula] = []
    if lo is not None:
        parts.append(Atom(Relation.GE, Add(_U, Neg(Const(lo)))))
    if hi is not None:
        parts.append(Atom(Relation.LE, Add(_U, Neg(Const(hi)))))
    post = parts[0] if len(parts) == 1 else And(parts[0], parts[1])
    return TrigBandClaim("u", radius, post)


@dataclass(frozen=True, slots=True)
class Example:
    """One example goal and what it must end as."""

    name: str
    claim: str
    goal: StreamGoal
    expected: str


EXAMPLES: tuple[Example, ...] = (
    Example(
        "full-stream",
        "every admitted input [u, boundary, anything] gives [u_xx, u] = [2, x² + 2t],"
        " boundary kept",
        heat_goal(FullStreamClaim(OUTPUTS)),
        "PROVEN",
    ),
    Example(
        "coefficients",
        "u[t¹] = 2, u[x²] = 1 and u_xx[1] = 2, read from the halo "
        "boundary[x²], u[x²]",
        heat_goal(
            CoefficientClaim(
                OBSERVED,
                And(
                    _equals(Var(0), _const(2)),
                    And(_equals(Var(1), _const(1)), _equals(Var(2), _const(2))),
                ),
            )
        ),
        "PROVEN",
    ),
    Example(
        "zero-constant",
        "u_xx has zero constant coefficient: refuted, it is 2",
        heat_goal(CoefficientClaim(OBSERVED, _equals(Var(2), _const(0)))),
        "REFUTED",
    ),
    Example(
        "other-stream",
        "a full-stream claim u = x², which no contract states",
        heat_goal(FullStreamClaim((("u_xx", _const(2)), ("u", Mul(Var(1), Var(1)))))),
        "UNKNOWN:unsupported",
    ),
    Example(
        "exp-tail",
        "profile e^x: the window (8, 16) is within 1/1000 of the analytic field on "
        "|t| ≤ 1/4, |x| ≤ 1/2 (certified bound 1/12288)",
        heat_goal(
            truncation(Fraction(1, 1000)), interpretation="analytic-tail", profile=EXP
        ),
        "PROVEN",
    ),
    Example(
        "cosh-tail-egf",
        "profile cosh x = (e^x + e^−x)/2 in the EGF circuit, within 1/10000",
        heat_goal(
            truncation(Fraction(1, 10000)),
            interpretation="analytic-tail",
            basis="egf",
            profile=COSH,
        ),
        "PROVEN",
    ),
    Example(
        "exp-tail-tight",
        "e^x within 1/20000: below the certified bound, so no proof, and no "
        "refutation either",
        heat_goal(
            truncation(Fraction(1, 20000)), interpretation="analytic-tail", profile=EXP
        ),
        "UNKNOWN:exhausted",
    ),
    Example(
        "exp-tail-radius",
        "e^x on |x| ≤ 1, the radius itself: outside every certificate",
        heat_goal(
            truncation(Fraction(1, 1000), radius=(Fraction(1, 4), Fraction(1))),
            interpretation="analytic-tail",
            profile=EXP,
        ),
        "UNKNOWN:exhausted",
    ),
    Example(
        "analytic-tail",
        "a coefficient claim read as an analytic series: not a truncation claim",
        heat_goal(
            CoefficientClaim(OBSERVED, _equals(Var(2), _const(2))),
            interpretation="analytic-tail",
        ),
        "UNKNOWN:unsupported",
    ),
    Example(
        "field-band",
        "u = x² + 2t on [0,1] × [−1,1] satisfies 0 ≤ u ≤ 3 (OGF circuit)",
        heat_goal(field_claim(band(3))),
        "PROVEN",
    ),
    Example(
        "field-band-egf",
        "the same bound for the EGF heat circuit",
        heat_goal(field_claim(band(3)), basis="egf"),
        "PROVEN",
    ),
    Example(
        "field-below-two",
        "u ≤ 2 on the strip: refuted at (t, x) = (1, 1), where u = 3",
        heat_goal(field_claim(band(2))),
        "REFUTED",
    ),
    Example(
        "field-stochastic",
        "the band under a stochastic reading",
        heat_goal(field_claim(band(3)), interpretation="stochastic-ito"),
        "UNKNOWN:unsupported",
    ),
    Example(
        "front-tail",
        "the Burgers front from 1 + e^(−x) at ν = 1/2 within 1/10000 of its "
        "16 × 16 window on |t| ≤ 1/6, |x| ≤ 1/8: the certified bound is 1/16384",
        burgers_goal(front_truncation(Fraction(1, 10000))),
        "PROVEN",
    ),
    Example(
        "front-tail-tight",
        "the front within 1/1000000: below every certified bound on the radii "
        "grid, so no proof, and no refutation either",
        burgers_goal(front_truncation(Fraction(1, 1000000))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "front-tail-inviscid",
        "the front at ν = 0: no Cole–Hopf potential",
        burgers_goal(front_truncation(Fraction(1, 10000)), viscosity=Fraction(0)),
        "UNKNOWN:unsupported",
    ),
    Example(
        "front-tail-no-constant",
        "a potential e^(−x) − 1 with Σ cᵢ = 0: no front",
        burgers_goal(front_truncation(Fraction(1, 10000)), profile=NO_FRONT),
        "UNKNOWN:unsupported",
    ),
    Example(
        "front-tail-egf",
        "the front's EGF circuit: the theorems are about the OGF circuit",
        burgers_goal(front_truncation(Fraction(1, 10000)), basis="egf"),
        "UNKNOWN:unsupported",
    ),
    Example(
        "front-band",
        "the front's analytic field within [2/5, 3/5] on |t| ≤ 1/6, |x| ≤ 1/8, "
        "for every reconstruction: the checked 16 × 16 window bounds it within "
        "[0.4477, 0.5523]",
        band_goal(front_band(Fraction(2, 5), Fraction(3, 5))),
        "PROVEN",
    ),
    Example(
        "front-band-upper",
        "the one-sided band u ≤ 3/5",
        band_goal(front_band(None, Fraction(3, 5))),
        "PROVEN",
    ),
    Example(
        "front-band-above",
        "u ≤ 27/50 on the same box: the window at the corner (1/6, −1/8) is "
        "0.5519, above the bound by more than the certified error",
        band_goal(front_band(Fraction(2, 5), Fraction(27, 50))),
        "REFUTED",
    ),
    Example(
        "front-band-below",
        "u ≥ 11/20: the window at the first corner tried, (1/6, 1/8), is 0.4896, "
        "below the bound by more than the certified error",
        band_goal(front_band(Fraction(11, 20), None)),
        "REFUTED",
    ),
    Example(
        "front-band-narrow",
        "u within [56/125, 69/125] = [0.448, 0.552], which holds (the field's "
        "range is about [0.4481, 0.5519]) but is inside the window's bound "
        "[0.4477, 0.5523], and no corner leaves it by more than the error: "
        "neither a proof nor a refutation",
        band_goal(front_band(Fraction(56, 125), Fraction(69, 125))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "front-band-shape",
        "a postcondition in t and u: not a band",
        band_goal(
            FieldClaim(
                "u",
                (Fraction(-1, 6), Fraction(1, 6)),
                (Fraction(-1, 8), Fraction(1, 8)),
                Atom(Relation.LE, Add(_U, Neg(_T))),
            )
        ),
        "UNKNOWN:unsupported",
    ),
    Example(
        "front-band-heat",
        "a band about the heat boundary's exponential sum: no window root",
        heat_goal(
            FieldClaim(
                "u",
                (Fraction(-1, 4), Fraction(1, 4)),
                (Fraction(-1, 2), Fraction(1, 2)),
                Atom(Relation.GE, _U),
            ),
            interpretation="analytic-field",
            profile=EXP,
        ),
        "UNKNOWN:unsupported",
    ),
    Example(
        "burgers-field",
        "a field claim about the Burgers circuit: it has no field lane",
        burgers_goal(
            field_claim(band(3)),
            viscosity=None,
            interpretation="evaluated-field",
            profile=PROFILE,
        ),
        "UNKNOWN:unsupported",
    ),
    Example(
        "euler-tail",
        "the Euler stream from cos x + cos(x+y) + cos(2x+y): the window through "
        "t² is within 1/100 of the field's sum on |t| ≤ 1/6480 (K = 3, L = 3, "
        "ρ = 648, the certified bound is exactly 1/100)",
        vorticity_goal(trig_truncation(Fraction(1, 100))),
        "PROVEN",
    ),
    Example(
        "euler-tail-tight",
        "the same within 1/1000: below the certified bound, so no proof, and no "
        "refutation either",
        vorticity_goal(trig_truncation(Fraction(1, 1000))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "euler-tail-radius",
        "the same on |t| ≤ 1/648, the certified radius itself: nothing is certified "
        "at or beyond it",
        vorticity_goal(trig_truncation(Fraction(1, 100), radius=Fraction(1, 648))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "euler-band",
        "the field's sum within [−31/10, 31/10] on |t| ≤ 1/6480: the window through "
        "t² bounds it by 3 + (8/5)/6480 + (223/260)/6480² + 1/100 ≈ 3.0102",
        vorticity_goal(trig_band(Fraction(-31, 10), Fraction(31, 10))),
        "PROVEN",
    ),
    Example(
        "euler-band-upper",
        "u ≤ 4 alone: the start's ℓ¹ norm and the tail at degree one suffice",
        vorticity_goal(trig_band(None, Fraction(4))),
        "PROVEN",
    ),
    Example(
        "euler-band-lower",
        "u ≥ -4 alone: the one-sided band below",
        vorticity_goal(trig_band(Fraction(-4), None)),
        "PROVEN",
    ),
    Example(
        "euler-band-narrow",
        "u ≤ 3, the start's own ℓ¹ norm: inside every window's bound, and this "
        "lane evaluates the field at no point, so neither a proof nor a refutation",
        vorticity_goal(trig_band(None, Fraction(3))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "euler-viscous",
        "the same truncation at ν = 1/10: Gevrey-1 growth is proved there and no "
        "radius is certified",
        vorticity_goal(trig_truncation(Fraction(1, 100)), viscosity=Fraction(1, 10)),
        "UNKNOWN:unsupported",
    ),
    Example(
        "euler-band-shape",
        "a postcondition u ≤ t on the Euler stream: not a band",
        vorticity_goal(
            TrigBandClaim("u", EULER_RADIUS, Atom(Relation.LE, Add(_U, Neg(_T))))
        ),
        "UNKNOWN:unsupported",
    ),
    Example(
        "euler-egf",
        "the Euler stream's EGF reading: the radius theorems are about the OGF stream",
        vorticity_goal(trig_truncation(Fraction(1, 100)), basis="egf"),
        "UNKNOWN:unsupported",
    ),
)


def mild_goal(
    claim: MildTruncationClaim | MildBandClaim, viscosity: Fraction = Fraction(1, 10)
) -> StreamGoal:
    """Build the three-mode physical mild circuit with interaction windows."""
    return StreamGoal(
        "vorticity",
        "mild-truncation" if type(claim) is MildTruncationClaim else "mild-band",
        "interaction",
        EULER_START,
        claim,
        viscosity,
    )


EXAMPLES += (
    Example(
        "mild-scale-tail",
        "The full mild contract with the Euler-scale four-term tail",
        mild_goal(MildTruncationClaim("u", 4, Fraction(1, 5760), Fraction(21, 1024))),
        "PROVEN",
    ),
    Example(
        "mild-contraction-tail",
        "Positive viscosity extends past the Euler scale",
        mild_goal(
            MildTruncationClaim("u", 4, Fraction(1, 576), Fraction(21, 1024)),
            Fraction(1),
        ),
        "PROVEN",
    ),
    Example(
        "mild-band",
        "The full mild contract with the contraction band",
        mild_goal(
            MildBandClaim(
                "u",
                Fraction(1, 5760),
                trig_band(Fraction(-3519, 1000), Fraction(3519, 1000)).post,
            )
        ),
        "PROVEN",
    ),
    Example(
        "mild-tight-band",
        "Neither sufficient bound certifies this band",
        mild_goal(
            MildBandClaim(
                "u", Fraction(1, 5760), trig_band(Fraction(-3), Fraction(3)).post
            )
        ),
        "UNKNOWN:exhausted",
    ),
    Example(
        "mild-euler-endpoint",
        "The Euler-scale strict endpoint is excluded",
        mild_goal(
            MildTruncationClaim("u", 4, Fraction(1, 648), Fraction(1)), Fraction(0)
        ),
        "UNKNOWN:exhausted",
    ),
)
