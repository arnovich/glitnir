"""Decide a stream or field root goal: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.

- *Full stream.* The only template is the model's contract: a supported
  identity is replayed as the root, with no search.
- *Coefficients.* Task 111's certificate lane searches the leaf that asgard
  023's lowering poses (the observed coefficients as polynomials in the input
  halo, under the admitted input's halo values); a found certificate is lifted
  by ``lift_certificate``. Otherwise the refutation lane looks for halo values
  that satisfy the admitted input's facts and violate the claim; Lean checks
  they are the admitted input's own and lifts them with ``refutes_root``.
- *Field.* The certificate lane searches ``t``'s interval and the box bound
  ``R² − x² ≥ 0`` against the post with ``u`` replaced by the field; the root
  cites ``sq_le_sq_of_box`` for the bound. Otherwise candidate witnesses are
  the box's corners, in a fixed order, then a Z3 point in the box with ``u``
  the field; Lean checks the point against the solution's own field.
- *Analytic tail and field band.* The producers of `tail` choose the radii
  of a certified truncation or the checked window of a band on the Burgers
  front; Lean decides every side condition.
- *Euler radius.* For the vorticity stream at ``ν = 0`` the producer of
  `trig` reads the start's size and ℓ¹ bounds and, for a band, the Picard
  tables' norms; the certified bound is ``tailBound``, and a band is never
  refuted (task 217).

Every decided result is a fresh replay of the root by `replay_stream`, or,
with ``replay=False``, the unchecked root application for the caller's Lean to
check. A leaf result is never a root result, and a failed search is never a
refutation.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from glitnir_forseti.entailment.evidence import PsatzCertificate, Witness
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.polynomial import expand
from glitnir_forseti.entailment.replay import InvalidEvidence, ReplayUnavailable
from glitnir_forseti.entailment.search import LaneOutcome, certify, refute
from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Fls,
    Formula,
    Goal,
    GoalError,
    Neg,
    Not,
    Or,
    Poly,
    Relation,
    Tru,
)
from glitnir_forseti.trajectory.render import RootApplication
from glitnir_forseti.trajectory.service import (
    _STOPS,
    Stage,
    Status,
    UnknownReason,
)

from . import mild, mild_carrier, render
from .goal import (
    CLASSICAL_EULER,
    FIELD_CONTEXT,
    CoefficientClaim,
    ExpSum,
    FieldClaim,
    FullStreamClaim,
    MildBandClaim,
    MildTruncationClaim,
    Modes,
    StreamGoal,
    TrigBandClaim,
    TrigTruncationClaim,
    TruncationClaim,
    band_of,
    check_goal_limits,
)
from .models import StreamModel
from .render import band_total, window_bounds
from .replay import (
    ContractOnly,
    Evidence,
    ValidatedStream,
    application,
    build_artifact,
    leaf_constraints,
    refutes,
    replay_stream,
    supported,
)
from .tail import (
    approximately,
    choose,
    choose_band,
    choose_front,
    describe_bound,
    front_tail_bound,
    tail_bound,
)
from .trig import SearchStopped
from .trig import choose_band as choose_trig_band
from .trig import choose_truncation, geometric
from .trig import tail_bound as trig_tail_bound


@dataclass(frozen=True, slots=True)
class StreamResult:
    """The outcome of one request; fields as for trajectory results."""

    status: Status
    reason: UnknownReason | None = None
    artifact: bytes | None = None
    stages: tuple[Stage, ...] = ()
    limits: dict[str, int] = field(default_factory=dict)
    axioms: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    application: RootApplication | None = None


def decide_stream(
    goal: StreamGoal,
    limits: EntailmentLimits | None = None,
    *,
    project: Path | None = None,
    cancel: threading.Event | None = None,
    replay: bool = True,
) -> StreamResult:
    """Decide ``goal`` within ``limits``; exactly one of the three outcomes.

    With ``replay`` False, the first evidence that renders as a root
    application ends the request unchecked, with that application and no
    artifact; nothing runs in the pinned Lean release.
    """
    limits = limits or EntailmentLimits()
    if type(goal) is not StreamGoal:
        raise TypeError("goal must be a StreamGoal")
    deadline = time.monotonic() + limits.wall_time_ms / 1000
    try:
        check_goal_limits(goal, limits)
    except GoalError as error:
        limited = Stage("binding", "resource-limit", str(error))
        return _unknown(UnknownReason.RESOURCE_LIMIT, [limited], limits)
    model = supported(goal)
    if isinstance(model, str):
        return _unknown(
            UnknownReason.UNSUPPORTED, [Stage("binding", "unsupported", model)], limits
        )
    stages = [Stage("binding", "supported", f"{model.key}/{type(goal.claim).__name__}")]
    candidates: list[tuple[str, Callable[[float], _Produced]]]
    if type(goal.claim) is FullStreamClaim:
        candidates = [
            (
                "contract",
                lambda _: (
                    LaneOutcome("found", "the model's contract"),
                    ContractOnly(),
                ),
            )
        ]
    elif type(goal.claim) is TruncationClaim:
        candidates = [("tail", lambda _: _tail(goal, model, limits))]
    elif goal.interpretation == "analytic-field":
        candidates = [("window", lambda _: _band(goal, limits))]
    elif type(goal.claim) in (MildTruncationClaim, MildBandClaim):
        candidates = [("mild-bound", lambda d: _mild(goal, limits, d, cancel))]
    elif type(goal.claim) is TrigTruncationClaim:
        candidates = [("radius", lambda _: _trig_truncation(goal, limits))]
    elif type(goal.claim) is TrigBandClaim:
        candidates = [("spread", lambda d: _trig_band(goal, limits, d, cancel))]
    else:
        candidates = [
            ("certificate", lambda d: _plain(_certify(goal, model, limits, d, cancel))),
            ("witness", lambda d: _plain(_witness(goal, model, limits, d, cancel))),
        ]
    for stage, produce in candidates:
        if cancel is not None and cancel.is_set():
            stages.append(Stage(stage, "cancelled"))
            break
        started = time.monotonic()
        produced, evidence = produce(started + (deadline - started) / 2)
        search_ms = _elapsed(started)
        if produced.outcome != "found" or evidence is None:
            stages.append(Stage(stage, produced.outcome, produced.detail, search_ms))
            if produced.outcome == "cancelled" or time.monotonic() >= deadline:
                break
            continue
        if not replay:
            try:
                unit = application(goal, model, evidence)
            except ValueError as error:
                detail = f"the evidence does not fit the claim: {error}"
                stages.append(Stage(stage, "found", detail, search_ms, "invalid"))
                continue
            stages.append(
                Stage(stage, "found", produced.detail, search_ms, "unchecked")
            )
            return StreamResult(
                Status.REFUTED if refutes(evidence) else Status.PROVEN,
                stages=tuple(stages),
                limits=limits.as_dict(),
                dependencies=unit.dependencies,
                application=unit,
            )
        artifact = build_artifact(
            goal, evidence, limits, {"stage": stage, "search_ms": search_ms}
        )
        started = time.monotonic()
        outcome = replay_stream(
            artifact, goal, limits, project, deadline=deadline, cancel=cancel
        )
        check_ms = _elapsed(started)
        if isinstance(outcome, ValidatedStream):
            stages.append(
                Stage(stage, "found", produced.detail, search_ms, "validated", check_ms)
            )
            return StreamResult(
                Status(outcome.status),
                artifact=artifact,
                stages=tuple(stages),
                limits=limits.as_dict(),
                axioms=outcome.axioms,
                dependencies=outcome.dependencies,
            )
        kind = "invalid" if isinstance(outcome, InvalidEvidence) else "unavailable"
        stop = outcome.stop if isinstance(outcome, ReplayUnavailable) else ""
        stages.append(
            Stage(stage, "found", outcome.reason, search_ms, kind, check_ms, stop)
        )
        if isinstance(outcome, ReplayUnavailable):
            break
    return _unknown(_reason(stages), stages, limits)


_Produced = tuple[LaneOutcome, Evidence | None]


def _plain(outcome: LaneOutcome) -> _Produced:
    """Pair a search outcome with the evidence it found, if any."""
    if isinstance(outcome.evidence, PsatzCertificate):
        # Stream leaves search `certify` alone, which never names products.
        raise TypeError("a stream leaf has no Psatz certificate")
    return outcome, outcome.evidence


def _tail(goal: StreamGoal, model: StreamModel, limits: EntailmentLimits) -> _Produced:
    """Choose the radii for a truncation proof; the bound is Lean's to check."""
    claim, profile = goal.claim, goal.profile
    assert type(claim) is TruncationClaim and isinstance(profile, ExpSum)
    if goal.viscosity is not None:  # `supported` matched it to a viscous lane
        front = choose_front(goal.viscosity, profile, claim, limits)
        if isinstance(front, str):
            return LaneOutcome("none", front), None
        bound = front_tail_bound(goal.viscosity, profile, claim, front.radii)
        r_t, r_x = front.radii
        detail = (
            f"ρ = {front.rho}, inverse radii (r_t, r_x) = ({r_t}, {r_x}), certified "
            f"bound {describe_bound(bound)} ≤ ε = {claim.epsilon}"
        )
        return LaneOutcome("found", detail), front
    chosen = choose(profile, claim, limits)
    if isinstance(chosen, str):
        return LaneOutcome("none", chosen), None
    bound = tail_bound(profile, claim, chosen.rho)
    detail = (
        f"ρ = {chosen.rho}, certified bound {describe_bound(bound)} "
        f"≤ ε = {claim.epsilon}"
    )
    return LaneOutcome("found", detail), chosen


def _band(goal: StreamGoal, limits: EntailmentLimits) -> _Produced:
    """Compute the front's checked window for a field band; Lean checks it."""
    claim, profile = goal.claim, goal.profile
    assert type(claim) is FieldClaim and isinstance(profile, ExpSum)
    assert goal.viscosity is not None  # `supported` matched it to the viscous lane
    found = choose_band(goal.viscosity, profile, claim, limits)
    if isinstance(found, str):
        return LaneOutcome("none", found), None
    r_t, r_x = found.radii
    n_t, n_x = found.window
    head = (
        f"ρ = {found.rho}, inverse radii (r_t, r_x) = ({r_t}, {r_x}), window "
        f"{n_t} × {n_x}, certified error {describe_bound(found.epsilon)}"
    )
    if found.point is not None:
        p_t, p_x = found.point
        detail = (
            f"{head}; the window at ({p_t}, {p_x}) is {found.side} the band by more "
            "than the certified error"
        )
    else:
        low, high = window_bounds(goal, claim, found)
        detail = (
            f"{head}; the window bounds u within [{approximately(low)}, "
            f"{approximately(high)}] on the box"
        )
    return LaneOutcome("found", detail), found


def _mild(
    goal: StreamGoal,
    limits: EntailmentLimits,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> _Produced:
    """Try both physical bounds; only replay can promote the result to PROVEN."""
    claim, start = goal.claim, goal.profile
    assert isinstance(start, Modes) and goal.viscosity is not None
    assert type(claim) is MildTruncationClaim or type(claim) is MildBandClaim
    try:
        if type(claim) is MildTruncationClaim:
            found = mild.choose_truncation(
                start,
                goal.viscosity,
                claim.time,
                claim.window,
                claim.epsilon,
                limits,
                deadline=deadline,
                cancel=cancel,
            )
        else:
            assert type(claim) is MildBandClaim
            band = band_of(claim.post)
            assert band is not None
            found = mild.choose_band(
                start,
                goal.viscosity,
                claim.time,
                *band,
                limits,
                deadline=deadline,
                cancel=cancel,
            )
    except SearchStopped as error:
        return LaneOutcome(error.outcome, str(error)), None
    if isinstance(found, str):
        return LaneOutcome("none", found), None
    bound = (
        mild.tail_bound if type(claim) is MildTruncationClaim else mild.band_bound
    )(found, claim.time)
    detail = (
        f"{found.kind}, K = {found.size}, L = {found.weight}, window {found.window}; "
        f"physical {'tail' if type(claim) is MildTruncationClaim else 'absolute band'} "
        f"bound {describe_bound(bound)} on 0 ≤ t ≤ {claim.time}; "
        "full circuit, convergence, classical and dissipation contract"
    )
    try:
        terms = mild_carrier.exact_terms(
            start,
            goal.viscosity,
            min(found.window - 1, 3),
            limits,
            deadline=deadline,
            cancel=cancel,
        )
        detail += "\n" + mild_carrier.report(terms, goal.viscosity, claim.time)
    except SearchStopped as error:
        if error.outcome == "cancelled":
            return LaneOutcome(error.outcome, str(error)), None
        detail += (
            f"\nCarrier report omitted: {error}; rational proof premises are unchanged."
        )
    return LaneOutcome("found", detail), found


def _trig_truncation(goal: StreamGoal, limits: EntailmentLimits) -> _Produced:
    """Read the start's constants for an Euler truncation; Lean decides them."""
    claim, start = goal.claim, goal.profile
    assert type(claim) is TrigTruncationClaim and isinstance(start, Modes)
    found = choose_truncation(
        start, claim, limits, allow_zero=goal.interpretation in CLASSICAL_EULER
    )
    if isinstance(found, str):
        return LaneOutcome("none", found), None
    big_m, rho = geometric(start)
    bound = trig_tail_bound(big_m, rho, claim.radius, claim.window)
    radius = (
        f"certified radius 1/ρ = {1 / rho}"
        if rho
        else "zero growth; every finite radius is certified"
    )
    detail = (
        f"K = {found.size}, L = {found.weight}, M = 3L = {big_m}, ρ = 72LK = {rho}, "
        f"{radius}, "
        f"certified bound {describe_bound(bound)} "
        f"≤ ε = {claim.epsilon}"
    )
    return LaneOutcome("found", detail), found


def _trig_band(
    goal: StreamGoal,
    limits: EntailmentLimits,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> _Produced:
    """Choose the window whose ℓ¹ spread bounds the field; Lean decides the norms."""
    claim, start = goal.claim, goal.profile
    assert type(claim) is TrigBandClaim and isinstance(start, Modes)
    try:
        found = choose_trig_band(
            start,
            claim,
            limits,
            deadline=deadline,
            cancel=cancel,
            allow_zero=goal.interpretation in CLASSICAL_EULER,
        )
    except SearchStopped as error:
        return LaneOutcome(error.outcome, str(error)), None
    if isinstance(found, str):
        return LaneOutcome("none", found), None
    big_m, rho = geometric(start)
    total = band_total(start, claim, found)
    detail = (
        f"K = {found.size}, L = {found.weight}, M = 3L = {big_m}, ρ = 72LK = {rho}, "
        f"window {found.window}, norms {', '.join(str(a) for a in found.norms)}; the "
        f"window bounds u within [{approximately(-total)}, {approximately(total)}] on "
        f"|t| ≤ {claim.radius}"
    )
    return LaneOutcome("found", detail), found


def _certify(
    goal: StreamGoal,
    model: StreamModel,
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None,
) -> LaneOutcome:
    """Search the leaf the root template lifts."""
    context, antecedent, consequent = leaf_constraints(goal, model)
    return certify(Goal(context, antecedent, consequent), limits, deadline, cancel)


def _witness(
    goal: StreamGoal,
    model: StreamModel,
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None,
) -> LaneOutcome:
    """Look for a point the root template can lift to a refutation."""
    claim = goal.claim
    if type(claim) is CoefficientClaim:
        context, antecedent, consequent = leaf_constraints(goal, model)
        return refute(Goal(context, antecedent, consequent), limits, deadline, cancel)
    assert type(claim) is FieldClaim
    corner = _corner(model, claim, limits)
    if corner is not None:
        return LaneOutcome("found", "a corner of the box", corner)
    assert model.field is not None
    value = Add(FIELD_CONTEXT.var("u"), Neg(model.field.expression))
    leaf = Goal(
        FIELD_CONTEXT,
        And(render.field_domain(claim), Atom(Relation.EQ, value)),
        claim.post,
    )
    return refute(leaf, limits, deadline, cancel)


def _corner(
    model: StreamModel, claim: FieldClaim, limits: EntailmentLimits
) -> Witness | None:
    """Find the first box corner where the post fails at the field's value.

    A producer heuristic: the value is computed here only to choose a
    candidate, and Lean re-derives it from the solution's field.
    """
    assert model.field is not None
    for t in (claim.time[1], claim.time[0]):
        for x in (claim.space[1], claim.space[0]):
            u = _evaluate_poly(model.field.expression, (t, x, Fraction(0)), limits)
            if not _holds(claim.post, (t, x, u), limits):
                return Witness((("t", t), ("x", x), ("u", u)))
    return None


def _evaluate_poly(
    poly: Poly, point: tuple[Fraction, ...], limits: EntailmentLimits
) -> Fraction:
    """Evaluate a polynomial exactly at a rational point, for a candidate."""
    total = Fraction(0)
    for monomial, coefficient in expand(poly, len(point), limits).items():
        term = coefficient
        for index in monomial:
            term *= point[index]
        total += term
    return total


def _holds(
    formula: Formula, point: tuple[Fraction, ...], limits: EntailmentLimits
) -> bool:
    if type(formula) is Tru:
        return True
    if type(formula) is Fls:
        return False
    if type(formula) is Atom:
        value = _evaluate_poly(formula.polynomial, point, limits)
        return {
            Relation.EQ: value == 0,
            Relation.LE: value <= 0,
            Relation.LT: value < 0,
            Relation.GE: value >= 0,
            Relation.GT: value > 0,
        }[formula.relation]
    if type(formula) is And:
        return _holds(formula.left, point, limits) and _holds(
            formula.right, point, limits
        )
    if type(formula) is Or:
        return _holds(formula.left, point, limits) or _holds(
            formula.right, point, limits
        )
    if type(formula) is Not:
        return not _holds(formula.argument, point, limits)
    raise TypeError(f"not a formula: {formula!r}")


def _reason(stages: list[Stage]) -> UnknownReason:
    """Name why no stage decided the goal."""
    for stage in stages:
        if stage.check == "unavailable":
            return _STOPS.get(stage.stop, UnknownReason.LEAN_UNAVAILABLE)
    for stage in stages:
        if stage.outcome in _STOPS:
            return _STOPS[stage.outcome]
    outcomes = {stage.stage: stage.outcome for stage in stages}
    if "unsupported-fragment" in outcomes.values() or "unsupported-witness" in (
        outcomes.values()
    ):
        if not any(stage.check == "invalid" for stage in stages):
            return UnknownReason.UNSUPPORTED
    return UnknownReason.EXHAUSTED


def _unknown(
    reason: UnknownReason, stages: list[Stage], limits: EntailmentLimits
) -> StreamResult:
    return StreamResult(
        Status.UNKNOWN, reason, stages=tuple(stages), limits=limits.as_dict()
    )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
