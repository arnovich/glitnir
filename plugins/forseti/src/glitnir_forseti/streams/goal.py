"""Closed root goals about a stream model's streams and fields.

A goal names the model, an interpretation, the basis, the declared initial
profile and one claim, and for a model with a parameter its viscosity. The
profile is a polynomial in ``x``, or an exponential sum ``Σᵢ cᵢ·e^(aᵢx)``
given by its exact pairs ``(cᵢ, aᵢ)``; which stream it is the profile of is
the model's to say (the heat reconstruction's boundary, or the Cole–Hopf
potential of the Burgers front). The claims are:

- ``full-stream``: the output streams, as polynomials over ``[t, x]``, and
  the boundary kept along ``t``;
- ``coefficients``: a postcondition over named output coefficients, each an
  output port and its ``(t, x)`` degrees;
- ``field``: a postcondition over ``(t, x, u)`` on a rational box of
  ``[t, x]``, about the evaluated field of an output port;
- ``truncation``: the analytic field of an output port is within ``ε`` of its
  window ``(N_t, N_x)`` uniformly on the box ``|t| ≤ r_t``, ``|x| ≤ r_x``.

A field claim read as ``analytic-field`` is about the analytic field of a
stream with a certified truncation: its box is symmetric about
the origin and its postcondition a band ``lo ≤ u ≤ hi``, or one side of one.

A trigonometric stream is declared by its start, a finite list of
modes ``(k₁, k₂, c)`` meaning ``c·e^{i k·x}`` on the torus, and its claims are
one-axis: ``trig-truncation``, the field's series through ``t``-degree ``N − 1``
is within ``ε`` of its sum on ``|t| ≤ r``, and ``trig-band``, a band on that
sum there. The explicit interpretations ``euler-truncation`` and ``euler-band``
add the classical vorticity equation, initial-value realization and mean-zero
and even invariants to the same quantitative claims. These require mean-zero,
even initial data, checked again by Lean; old interpretations keep their meaning.

The physical ``mild-truncation`` and ``mild-band`` claims instead use the
canonical ``interaction`` basis and a forward ``time`` horizon ``T``. The
window keeps interaction terms below ``N``; those terms already depend on
physical time. Every mild reading includes the full classical, convergent,
spectrally dissipative circuit contract, with derivative identities on the
interior and quantitative bounds and continuity on the closed interval.

``gimle.forseti.stream-goal/v2`` encodes it strictly (v2 adds the
exponential-sum profile and the truncation claim; the ``viscosity`` field is
present exactly when the goal names one, so documents without it read as
before; the modes start and the two trigonometric claims are new kinds within
v2, which older readers refuse as unknown); whether a goal is supported is the
service's question, answered ``UNKNOWN`` with a reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Union

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import (
    Atom,
    Context,
    DecodeBudget,
    Formula,
    Goal,
    GoalError,
    Poly,
    Relation,
    Tru,
)
from glitnir_forseti.entailment.syntax import check_goal_limits as check_formula_limits
from glitnir_forseti.entailment.syntax import (
    check_rational_bits,
    decode_formula,
    decode_poly,
    decode_rational,
    encode_formula,
    encode_poly,
    encode_rational,
)

GOAL_SCHEMA = "gimle.forseti.stream-goal/v2"
INTERPRETATIONS = (
    "formal-stream",
    "evaluated-field",
    "analytic-tail",
    "analytic-field",
    "trig-truncation",
    "trig-band",
    "euler-truncation",
    "euler-band",
    "mild-truncation",
    "mild-band",
    "stochastic-ito",
)
BASES = ("ogf", "egf", "interaction")
CLASSICAL_EULER = ("euler-truncation", "euler-band")
_PORT = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_MAX_DEGREE = 64
_MAX_OBSERVED = 64
_MAX_EXPONENTIALS = 16
_MAX_MODES = 64
_MAX_MODE = 64


@dataclass(frozen=True, slots=True)
class FullStreamClaim:
    """The outputs are these whole streams, and the boundary is kept.

    Attributes:
        outputs: ``(port, polynomial over [t, x])`` for every output, in order.
    """

    outputs: tuple[tuple[str, Poly], ...]


@dataclass(frozen=True, slots=True)
class CoefficientClaim:
    """A postcondition over finitely many named output coefficients.

    Attributes:
        observed: ``(port, (t degree, x degree))`` per coefficient, in order;
            coordinate ``i`` of ``post`` is ``observed[i]``.
        post: The postcondition.
    """

    observed: tuple[tuple[str, tuple[int, int]], ...]
    post: Formula


@dataclass(frozen=True, slots=True)
class FieldClaim:
    """A postcondition on the evaluated field of an output, over a box.

    Attributes:
        port: The observed output port.
        time: ``(lower, upper)`` bounds on ``t``.
        space: ``(lower, upper)`` bounds on ``x``.
        post: The postcondition over ``(t, x, u)``, ``u`` the field value.
    """

    port: str
    time: tuple[Fraction, Fraction]
    space: tuple[Fraction, Fraction]
    post: Formula


@dataclass(frozen=True, slots=True)
class TruncationClaim:
    """The window ``N`` of an output is within ``ε`` of its analytic field.

    Attributes:
        port: The observed output port.
        window: ``(N_t, N_x)``: the window keeps degrees below these.
        radius: ``(r_t, r_x)``: the box ``|t| ≤ r_t``, ``|x| ≤ r_x``.
        epsilon: The claimed uniform bound on the truncation error.
    """

    port: str
    window: tuple[int, int]
    radius: tuple[Fraction, Fraction]
    epsilon: Fraction


@dataclass(frozen=True, slots=True)
class ExpSum:
    """The profile ``Σᵢ cᵢ·e^(aᵢx)``, as exact pairs ``(cᵢ, aᵢ)``."""

    terms: tuple[tuple[Fraction, Fraction], ...]


@dataclass(frozen=True, slots=True)
class Modes:
    """A trigonometric start ``Σ c·e^{i k·x}``, as exact triples ``(k₁, k₂, c)``."""

    terms: tuple[tuple[int, int, Fraction], ...]


@dataclass(frozen=True, slots=True)
class TrigTruncationClaim:
    """The sum through ``t``-degree ``N − 1`` is within ``ε`` on ``|t| ≤ r``.

    Attributes:
        port: The observed output port.
        window: ``N``: the window keeps ``t``-degrees below it.
        radius: ``r``: the interval ``|t| ≤ r``, every ``x``.
        epsilon: The claimed uniform bound on the truncation error.
    """

    port: str
    window: int
    radius: Fraction
    epsilon: Fraction


@dataclass(frozen=True, slots=True)
class TrigBandClaim:
    """A band on the field's sum on ``|t| ≤ r``, every ``x``.

    Attributes:
        port: The observed output port.
        radius: ``r``: the interval ``|t| ≤ r``.
        post: The postcondition over ``u``, the field value, read by `band_of`
            over the coordinates ``(t, x, u)``.
    """

    port: str
    radius: Fraction
    post: Formula


@dataclass(frozen=True, slots=True)
class MildTruncationClaim:
    """The first N physical interaction terms approximate the sum on [0, T]."""

    port: str
    window: int
    time: Fraction
    epsilon: Fraction


@dataclass(frozen=True, slots=True)
class MildBandClaim:
    """A band on the physical mild field throughout [0, T], at every x."""

    port: str
    time: Fraction
    post: Formula


Claim = Union[
    FullStreamClaim,
    CoefficientClaim,
    FieldClaim,
    TruncationClaim,
    TrigTruncationClaim,
    TrigBandClaim,
    MildTruncationClaim,
    MildBandClaim,
]
Profile = Union[Poly, ExpSum, Modes]

#: The coordinates of a field claim's formulas.
FIELD_CONTEXT = Context(("t", "x", "u"))
#: The coordinates of a stream polynomial.
AXES_CONTEXT = Context(("t", "x"))
#: The coordinate of a profile.
PROFILE_CONTEXT = Context(("x",))


def coefficient_context(size: int) -> Context:
    """Name ``size`` synthetic coordinates for a coefficient claim."""
    return Context(tuple(f"c{i}" for i in range(max(size, 1))))


@dataclass(frozen=True, slots=True)
class StreamGoal:
    """A closed root goal about a stream model.

    Attributes:
        model: The model key.
        interpretation: ``formal-stream``, ``evaluated-field``,
            ``analytic-tail`` or ``analytic-field`` (supported), or another
            reading, which ends ``UNKNOWN``.
        basis: ``ogf`` or ``egf`` for Taylor streams, ``interaction`` for mild.
        profile: The declared initial profile: a polynomial in ``x`` or an
            exponential sum.
        claim: The claim.
        viscosity: The model's viscosity ``ν``, for a model that has one;
            ``None`` for the heat reconstruction.
    """

    model: str
    interpretation: str
    basis: str
    profile: Profile
    claim: Claim
    viscosity: Fraction | None = None

    def __post_init__(self) -> None:
        """Check tags, ports, degrees, bounds and each formula's coordinates."""
        if self.interpretation not in INTERPRETATIONS:
            raise GoalError(f"unknown interpretation {self.interpretation!r}")
        if self.basis not in BASES:
            raise GoalError(f"unknown basis {self.basis!r}")
        if type(self.model) is not str or not _PORT.fullmatch(
            self.model.replace("-", "_")
        ):
            raise GoalError("invalid model key")
        if self.viscosity is not None and type(self.viscosity) is not Fraction:
            raise GoalError("a viscosity is a fraction")
        if isinstance(self.profile, ExpSum):
            terms = self.profile.terms
            if (
                type(terms) is not tuple or not 0 < len(terms) <= _MAX_EXPONENTIALS
            ) or any(
                type(term) is not tuple
                or len(term) != 2
                or any(type(v) is not Fraction for v in term)
                for term in terms
            ):
                raise GoalError("an exponential sum is a bounded list of exact pairs")
        elif isinstance(self.profile, Modes):
            modes = self.profile.terms
            if (type(modes) is not tuple or not 0 < len(modes) <= _MAX_MODES) or any(
                type(mode) is not tuple
                or len(mode) != 3
                or any(type(k) is not int or abs(k) > _MAX_MODE for k in mode[:2])
                or type(mode[2]) is not Fraction
                for mode in modes
            ):
                raise GoalError("a start is a bounded list of modes (k₁, k₂, c)")
            if len({(k1, k2) for k1, k2, _ in modes}) != len(modes):
                raise GoalError("the modes of a start are distinct")
            if self.viscosity is None:
                raise GoalError("a start of modes names its viscosity")
        else:
            Goal(PROFILE_CONTEXT, _atom(self.profile), Tru())
        claim = self.claim
        if type(claim) is FullStreamClaim:
            if not claim.outputs:
                raise GoalError("a full-stream claim names its outputs")
            for port, stream in claim.outputs:
                _check_port(port)
                Goal(AXES_CONTEXT, _atom(stream), Tru())
        elif type(claim) is CoefficientClaim:
            if not 0 < len(claim.observed) <= _MAX_OBSERVED:
                raise GoalError(
                    "a coefficient claim observes a bounded, non-empty list"
                )
            if len(set(claim.observed)) != len(claim.observed):
                raise GoalError("observed coefficients are distinct")
            for port, degrees in claim.observed:
                _check_port(port)
                if (
                    type(degrees) is not tuple
                    or len(degrees) != 2
                    or any(
                        type(d) is not int or not 0 <= d <= _MAX_DEGREE for d in degrees
                    )
                ):
                    raise GoalError("degrees are two bounded natural numbers")
            Goal(coefficient_context(len(claim.observed)), Tru(), claim.post)
        elif type(claim) is FieldClaim:
            _check_port(claim.port)
            for low, high in (claim.time, claim.space):
                if (
                    type(low) is not Fraction
                    or type(high) is not Fraction
                    or low > high
                ):
                    raise GoalError("a box side is two ordered fractions")
            Goal(FIELD_CONTEXT, Tru(), claim.post)
        elif type(claim) is TruncationClaim:
            _check_port(claim.port)
            if (
                type(claim.window) is not tuple
                or len(claim.window) != 2
                or any(
                    type(n) is not int or not 0 <= n <= _MAX_DEGREE
                    for n in claim.window
                )
            ):
                raise GoalError("a window is two bounded natural numbers")
            if (
                type(claim.radius) is not tuple
                or len(claim.radius) != 2
                or any(type(r) is not Fraction or r < 0 for r in claim.radius)
            ):
                raise GoalError("box radii are two nonnegative fractions")
            if type(claim.epsilon) is not Fraction:
                raise GoalError("ε is a fraction")
        elif type(claim) is MildTruncationClaim or type(claim) is MildBandClaim:
            _check_port(claim.port)
            if type(claim.time) is not Fraction or claim.time < 0:
                raise GoalError("the forward time is a nonnegative fraction")
            if type(claim) is MildTruncationClaim:
                if (
                    type(claim.window) is not int
                    or not 0 <= claim.window <= _MAX_DEGREE
                ):
                    raise GoalError("a mild window is a bounded natural number")
                if type(claim.epsilon) is not Fraction:
                    raise GoalError("ε is a fraction")
            else:
                assert type(claim) is MildBandClaim
                Goal(FIELD_CONTEXT, Tru(), claim.post)
        elif type(claim) is TrigTruncationClaim:
            _check_port(claim.port)
            if type(claim.window) is not int or not 0 <= claim.window <= _MAX_DEGREE:
                raise GoalError("a window is a bounded natural number")
            if type(claim.radius) is not Fraction or claim.radius < 0:
                raise GoalError("the radius is a nonnegative fraction")
            if type(claim.epsilon) is not Fraction:
                raise GoalError("ε is a fraction")
        elif type(claim) is TrigBandClaim:
            _check_port(claim.port)
            if type(claim.radius) is not Fraction or claim.radius < 0:
                raise GoalError("the radius is a nonnegative fraction")
            Goal(FIELD_CONTEXT, Tru(), claim.post)
        else:
            raise GoalError(
                "the claim is full-stream, coefficients, field, truncation, "
                "trig-truncation or trig-band"
            )


def _atom(poly: Poly) -> Formula:
    """Wrap a polynomial in an atom, to check it like a formula."""
    return Atom(Relation.EQ, poly)


def _check_port(port: object) -> None:
    if type(port) is not str or not _PORT.fullmatch(port):
        raise GoalError(f"invalid port name {port!r}")


def check_goal_limits(goal: StreamGoal, limits: EntailmentLimits) -> None:
    """Refuse a goal over the node, depth, variable or coefficient limits."""
    if goal.viscosity is not None:
        check_rational_bits(goal.viscosity, limits)
    if isinstance(goal.profile, ExpSum):
        for term in goal.profile.terms:
            for value in term:
                check_rational_bits(value, limits)
    elif isinstance(goal.profile, Modes):
        for _, _, value in goal.profile.terms:
            check_rational_bits(value, limits)
    else:
        check_formula_limits(Goal(PROFILE_CONTEXT, _atom(goal.profile), Tru()), limits)
    claim = goal.claim
    if type(claim) is FullStreamClaim:
        for _, stream in claim.outputs:
            check_formula_limits(Goal(AXES_CONTEXT, _atom(stream), Tru()), limits)
    elif type(claim) is CoefficientClaim:
        context = coefficient_context(len(claim.observed))
        check_formula_limits(Goal(context, Tru(), claim.post), limits)
    elif type(claim) is FieldClaim:
        check_formula_limits(Goal(FIELD_CONTEXT, Tru(), claim.post), limits)
        for value in (*claim.time, *claim.space):
            check_rational_bits(value, limits)
    elif type(claim) is TruncationClaim:
        for value in (*claim.radius, claim.epsilon):
            check_rational_bits(value, limits)
    elif type(claim) is MildTruncationClaim or type(claim) is MildBandClaim:
        check_rational_bits(claim.time, limits)
        if type(claim) is MildTruncationClaim:
            check_rational_bits(claim.epsilon, limits)
        else:
            assert type(claim) is MildBandClaim
            check_formula_limits(Goal(FIELD_CONTEXT, Tru(), claim.post), limits)
    elif type(claim) is TrigTruncationClaim:
        for value in (claim.radius, claim.epsilon):
            check_rational_bits(value, limits)
    elif type(claim) is TrigBandClaim:
        check_formula_limits(Goal(FIELD_CONTEXT, Tru(), claim.post), limits)
        check_rational_bits(claim.radius, limits)


def encode_profile(profile: Profile) -> Any:
    """Encode a profile: a polynomial, or ``{"exponentials": [[c, a], …]}``."""
    if isinstance(profile, ExpSum):
        return {
            "exponentials": [
                [encode_rational(c), encode_rational(a)] for c, a in profile.terms
            ]
        }
    if isinstance(profile, Modes):
        return {"modes": [[k1, k2, encode_rational(c)] for k1, k2, c in profile.terms]}
    return encode_poly(profile)


def encode_goal(goal: StreamGoal) -> dict[str, Any]:
    """Encode a goal as its versioned JSON document."""
    claim = goal.claim
    encoded: dict[str, Any]
    if type(claim) is FullStreamClaim:
        encoded = {
            "kind": "full-stream",
            "outputs": [[port, encode_poly(stream)] for port, stream in claim.outputs],
            "boundary": "kept",
        }
    elif type(claim) is CoefficientClaim:
        encoded = {
            "kind": "coefficients",
            "observed": [[port, list(degrees)] for port, degrees in claim.observed],
            "post": encode_formula(claim.post),
        }
    elif type(claim) is TruncationClaim:
        encoded = {
            "kind": "truncation",
            "port": claim.port,
            "window": list(claim.window),
            "radius": {
                "t": encode_rational(claim.radius[0]),
                "x": encode_rational(claim.radius[1]),
            },
            "epsilon": encode_rational(claim.epsilon),
        }
    elif type(claim) is MildTruncationClaim or type(claim) is MildBandClaim:
        encoded = {
            "kind": (
                "mild-truncation" if type(claim) is MildTruncationClaim else "mild-band"
            ),
            "port": claim.port,
            "time": encode_rational(claim.time),
        }
        if type(claim) is MildTruncationClaim:
            encoded.update(window=claim.window, epsilon=encode_rational(claim.epsilon))
        else:
            assert type(claim) is MildBandClaim
            encoded["post"] = encode_formula(claim.post)
    elif type(claim) is TrigTruncationClaim:
        encoded = {
            "kind": "trig-truncation",
            "port": claim.port,
            "window": claim.window,
            "radius": encode_rational(claim.radius),
            "epsilon": encode_rational(claim.epsilon),
        }
    elif type(claim) is TrigBandClaim:
        encoded = {
            "kind": "trig-band",
            "port": claim.port,
            "radius": encode_rational(claim.radius),
            "post": encode_formula(claim.post),
        }
    else:
        assert type(claim) is FieldClaim
        encoded = {
            "kind": "field",
            "port": claim.port,
            "domain": {
                "t": [encode_rational(v) for v in claim.time],
                "x": [encode_rational(v) for v in claim.space],
            },
            "post": encode_formula(claim.post),
        }
    document: dict[str, Any] = {
        "schema": GOAL_SCHEMA,
        "model": goal.model,
        "interpretation": goal.interpretation,
        "basis": goal.basis,
        "profile": encode_profile(goal.profile),
        "claim": encoded,
    }
    if goal.viscosity is not None:
        document["viscosity"] = encode_rational(goal.viscosity)
    return document


def _object(data: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(data) is not dict or set(data) != keys:
        raise GoalError(f"{label} has exactly the fields {sorted(keys)}")
    return data


def _decode_profile(data: Any, limits: EntailmentLimits) -> Profile:
    """Read a polynomial profile, or an exponential sum's exact pairs."""
    if type(data) is dict and "modes" in data:
        modes = _object(data, {"modes"}, "a start")["modes"]
        if type(modes) is not list or not 0 < len(modes) <= _MAX_MODES:
            raise GoalError("a start is a bounded, non-empty list of modes")
        terms = []
        for mode in modes:
            if (
                type(mode) is not list
                or len(mode) != 3
                or any(type(k) is not int for k in mode[:2])
            ):
                raise GoalError("a mode is [k₁, k₂, c]")
            terms.append((mode[0], mode[1], decode_rational(mode[2], limits)))
        return Modes(tuple(terms))
    if type(data) is dict:
        pairs = _object(data, {"exponentials"}, "an exponential sum")["exponentials"]
        if type(pairs) is not list or not 0 < len(pairs) <= _MAX_EXPONENTIALS:
            raise GoalError("an exponential sum is a bounded, non-empty list")
        pairs_decoded: list[tuple[Fraction, Fraction]] = []
        for pair in pairs:
            if type(pair) is not list or len(pair) != 2:
                raise GoalError("an exponential term is [c, a]")
            pairs_decoded.append(
                (decode_rational(pair[0], limits), decode_rational(pair[1], limits))
            )
        return ExpSum(tuple(pairs_decoded))
    return decode_poly(data, DecodeBudget(limits, 1))


def decode_goal(data: Any, limits: EntailmentLimits) -> StreamGoal:
    """Read a versioned goal document, refusing anything malformed or too large."""
    keys = {"schema", "model", "interpretation", "basis", "profile", "claim"}
    if type(data) is dict and "viscosity" in data:
        keys.add("viscosity")
    document = _object(data, keys, "a goal")
    viscosity = (
        decode_rational(document["viscosity"], limits)
        if "viscosity" in document
        else None
    )
    if document["schema"] != GOAL_SCHEMA:
        raise GoalError("unsupported stream goal schema")
    for label in ("model", "interpretation", "basis"):
        if type(document[label]) is not str:
            raise GoalError(f"{label} is text")
    profile = _decode_profile(document["profile"], limits)
    raw = document["claim"]
    if type(raw) is not dict:
        raise GoalError("the claim is an object")
    kind = raw.get("kind")
    claim: Claim
    if kind == "full-stream":
        raw = _object(raw, {"kind", "outputs", "boundary"}, "a full-stream claim")
        if raw["boundary"] != "kept" or type(raw["outputs"]) is not list:
            raise GoalError("a full-stream claim keeps its boundary and lists outputs")
        if len(raw["outputs"]) > _MAX_OBSERVED:
            raise GoalError("too many outputs")
        budget = DecodeBudget(limits, 2)
        outputs = []
        for entry in raw["outputs"]:
            if type(entry) is not list or len(entry) != 2:
                raise GoalError("an output is [port, polynomial]")
            outputs.append((entry[0], decode_poly(entry[1], budget)))
        claim = FullStreamClaim(tuple(outputs))
    elif kind == "coefficients":
        raw = _object(raw, {"kind", "observed", "post"}, "a coefficient claim")
        if type(raw["observed"]) is not list or len(raw["observed"]) > _MAX_OBSERVED:
            raise GoalError("observed coefficients are a bounded list")
        observed = []
        for entry in raw["observed"]:
            if (
                type(entry) is not list
                or len(entry) != 2
                or type(entry[1]) is not list
                or len(entry[1]) != 2
            ):
                raise GoalError("an observed coefficient is [port, [t, x]]")
            observed.append((entry[0], (entry[1][0], entry[1][1])))
        budget = DecodeBudget(limits, max(len(observed), 1))
        claim = CoefficientClaim(tuple(observed), decode_formula(raw["post"], budget))
    elif kind == "field":
        raw = _object(raw, {"kind", "port", "domain", "post"}, "a field claim")
        domain = _object(raw["domain"], {"t", "x"}, "a field domain")
        sides = []
        for axis in ("t", "x"):
            side = domain[axis]
            if type(side) is not list or len(side) != 2:
                raise GoalError("a box side is [lower, upper]")
            sides.append(
                (decode_rational(side[0], limits), decode_rational(side[1], limits))
            )
        claim = FieldClaim(
            raw["port"],
            sides[0],
            sides[1],
            decode_formula(raw["post"], DecodeBudget(limits, 3)),
        )
    elif kind == "truncation":
        raw = _object(
            raw, {"kind", "port", "window", "radius", "epsilon"}, "a truncation claim"
        )
        window = raw["window"]
        if type(window) is not list or len(window) != 2:
            raise GoalError("a window is [N_t, N_x]")
        radius = _object(raw["radius"], {"t", "x"}, "box radii")
        claim = TruncationClaim(
            raw["port"],
            (window[0], window[1]),
            (
                decode_rational(radius["t"], limits),
                decode_rational(radius["x"], limits),
            ),
            decode_rational(raw["epsilon"], limits),
        )
    elif kind == "mild-truncation":
        raw = _object(
            raw,
            {"kind", "port", "window", "time", "epsilon"},
            "a mild-truncation claim",
        )
        claim = MildTruncationClaim(
            raw["port"],
            raw["window"],
            decode_rational(raw["time"], limits),
            decode_rational(raw["epsilon"], limits),
        )
    elif kind == "mild-band":
        raw = _object(raw, {"kind", "port", "time", "post"}, "a mild-band claim")
        claim = MildBandClaim(
            raw["port"],
            decode_rational(raw["time"], limits),
            decode_formula(raw["post"], DecodeBudget(limits, 3)),
        )
    elif kind == "trig-truncation":
        raw = _object(
            raw,
            {"kind", "port", "window", "radius", "epsilon"},
            "a trig-truncation claim",
        )
        claim = TrigTruncationClaim(
            raw["port"],
            raw["window"],
            decode_rational(raw["radius"], limits),
            decode_rational(raw["epsilon"], limits),
        )
    elif kind == "trig-band":
        raw = _object(raw, {"kind", "port", "radius", "post"}, "a trig-band claim")
        claim = TrigBandClaim(
            raw["port"],
            decode_rational(raw["radius"], limits),
            decode_formula(raw["post"], DecodeBudget(limits, 3)),
        )
    else:
        raise GoalError(
            "the claim is full-stream, coefficients, field, truncation, "
            "trig-truncation or trig-band"
        )
    return StreamGoal(
        document["model"],
        document["interpretation"],
        document["basis"],
        profile,
        claim,
        viscosity,
    )


def band_of(post: Formula) -> tuple[Fraction | None, Fraction | None] | None:
    """Read a postcondition as a band ``lo ≤ u ≤ hi`` on the field value ``u``.

    Accepts a conjunction of atoms ``u ≥ c`` and ``u ≤ c`` (as ``u − c ≥ 0``,
    ``c − u ≤ 0`` and the like: anything linear in ``u`` alone with
    coefficient ``±1``), keeping the largest lower and smallest upper bound.
    Returns ``None`` for any other postcondition, one with no bound, one whose
    bounds cross (``lo > hi``, an empty band), or one past the expansion limits.
    """
    from glitnir_forseti.entailment.polynomial import ExpansionLimit, expand
    from glitnir_forseti.entailment.syntax import And as Conjunction

    lo: Fraction | None = None
    hi: Fraction | None = None
    atoms: list[Formula] = []
    pending = [post]
    while pending:
        node = pending.pop()
        if type(node) is Conjunction:
            pending.extend((node.left, node.right))
        elif type(node) is Atom:
            atoms.append(node)
        else:
            return None
    for atom in atoms:
        assert type(atom) is Atom
        if atom.relation not in (Relation.LE, Relation.GE):
            return None
        try:
            sparse = expand(
                atom.polynomial, len(FIELD_CONTEXT.names), EntailmentLimits()
            )
        except ExpansionLimit:
            return None  # far from linear in u: not a band
        if set(sparse) - {(), (2,)} or (2,) not in sparse:
            return None
        slope, offset = sparse[(2,)], sparse.get((), Fraction(0))
        if abs(slope) != 1:
            return None
        # slope·u + offset ≥ 0 (GE) or ≤ 0 (LE)
        bound = -offset / slope
        lower = (atom.relation is Relation.GE) == (slope > 0)
        if lower:
            lo = bound if lo is None else max(lo, bound)
        else:
            hi = bound if hi is None else min(hi, bound)
    if lo is None and hi is None or (lo is not None and hi is not None and lo > hi):
        return None  # no bound, or an empty band: not a band to prove or refute
    return lo, hi
