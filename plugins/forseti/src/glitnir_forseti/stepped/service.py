"""Decide a stepped goal with exact arithmetic: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.

The decider runs no Lean and no solver. It binds the goal to a registered
model, refusing (``UNKNOWN: unsupported``) what the model's theorems do not
speak of: another model, step, start or initial state, no step or more than
the model's cap (fewer for a fixed-date or expected claim, whose proofs may have
the kernel unroll a recursion over every step), a non-positive step or barrier,
an observation the model lacks, or a claim of the wrong kind for the
observation. Then, for each law class and claim, one rule:

* a probability claim (``horizon`` or ``fixed-date``) whose bound is at least
  ``1`` holds for every probability law, in either class;
* ``horizon``, two-moment: it holds when ``N·Δ²·v/B² ≤ q``; it is refuted when
  ``q < 1 − (1 − 2ε)ᴺ``, the breach probability of the three-point law with
  jumps ``±L``, ``L = ⌊B/Δ⌋ + 1`` and ``ε = v/(2L²)`` (barrier ``1`` only, the
  barrier `step_big_breaches` is stated for); otherwise it is ``UNKNOWN: gap``;
* ``horizon``, sub-Gaussian: unsupported, no horizon theorem in this version;
* ``fixed-date``, sub-Gaussian with a whole-number proxy ``v``: it holds when
  ``2/E(r) ≤ q`` at ``r = B²/(2·c_up)``, ``c_up`` the proxy at the last mark
  rounded up; otherwise ``UNKNOWN: gap``, since nothing refutes it here;
* ``fixed-date``, two-moment: unsupported, Chebyshev is not in this version;
* ``expected``, two-moment: the expected energy ``Δ²·v·∑_{k<N} ρᵏ`` is exact,
  so the claim holds when it is at most the bound and is refuted otherwise;
* ``expected``, sub-Gaussian: unsupported.

Every figure is a `fractions.Fraction` (`stepped.numbers`). A reason shows each
by one rule (`figures.figure`): exactly when it is short, with its decimal
beside it when four places do not already write it, and by its decimal alone
when it is long; a decimal too small or too large for four places takes two
significant digits, ``1.3e-111``. The recursions over the horizon stop as soon
as the request is cancelled or out of time. A decided goal carries the
Lean application of its template (`stepped.render`), unchecked: it is a
proposal for the project's Lean to check. There is no pinned replay for this
lane, so a request that asks for one ends ``UNKNOWN: lean-unavailable``
instead of claiming what nothing checked.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import GoalError, check_rational_bits
from glitnir_forseti.figures import equals, figure
from glitnir_forseti.trajectory.render import RootApplication

from . import render
from .goal import SteppedGoal
from .models import MODELS, SteppedEntry, SteppedObservation
from .numbers import (
    Interrupted,
    Stop,
    bernoulli_lower,
    expected_energy,
    fixed_date_bound,
    horizon_bound,
    proxy,
    rho,
    three_point,
)

#: The largest ``⌊r⌋`` a fixed-date block evaluates ``e^⌊r⌋`` at. A larger
#: ``r`` is replaced by this smaller one, which the proof accepts as well.
EXP_CAP = 256
#: The claims whose proofs may have the kernel unroll a recursion over every
#: step, which `SteppedEntry.unrolled_cap` bounds, with how a refusal names them.
_UNROLLED = {"fixed-date": "a fixed-date claim", "expected": "an expected claim"}
#: The model's theorems start from its initial state, where the energy is 0.
INITIAL_ENERGY = Fraction(0)


class Status(str, Enum):
    """The three external outcomes."""

    PROVEN = "PROVEN"
    REFUTED = "REFUTED"
    UNKNOWN = "UNKNOWN"


class UnknownReason(str, Enum):
    """Why a request ended ``UNKNOWN``."""

    UNSUPPORTED = "unsupported"
    GAP = "gap"
    RESOURCE_LIMIT = "resource-limit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    LEAN_UNAVAILABLE = "lean-unavailable"


@dataclass(frozen=True, slots=True)
class Stage:
    """One stage of a request and how it ended.

    Attributes:
        stage: ``binding``, or the rule that ran: ``trivial-bound``,
            ``horizon``, ``three-point``, ``fixed-date`` or ``expected``.
        outcome: ``supported``, ``found``, ``none``, ``unsupported`` or why
            the request stopped.
        detail: The figures behind the outcome, or what is unsupported.
        search_ms: Time in the arithmetic.
        check: ``unchecked`` for a decided goal (no Lean ran), ``unavailable``
            when a replay was asked for, or empty.
    """

    stage: str
    outcome: str
    detail: str = ""
    search_ms: int = 0
    check: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the stage for machine-readable reports."""
        return {
            "stage": self.stage,
            "outcome": self.outcome,
            "detail": self.detail,
            "search_ms": self.search_ms,
            "check": self.check,
        }


@dataclass(frozen=True, slots=True)
class SteppedResult:
    """The outcome of one request.

    Attributes:
        status: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.
        reason: For ``UNKNOWN``, why.
        stages: Every stage that ran, in order.
        limits: The effective limits.
        dependencies: The Lean lemmas the proposed block cites, when decided.
        application: For a decided goal, the unchecked Lean application.
        axioms: Always empty: nothing was checked.
        artifact: Always None: the lane keeps no replayable artifact.
    """

    status: Status
    reason: UnknownReason | None = None
    stages: tuple[Stage, ...] = ()
    limits: dict[str, int] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    application: RootApplication | None = None
    axioms: tuple[str, ...] = ()
    artifact: bytes | None = None


@dataclass(frozen=True, slots=True)
class _Verdict:
    """What one rule found: a template to propose, or why there is none."""

    stage: str
    status: Status
    detail: str
    reason: UnknownReason | None = None
    application: RootApplication | None = None


def decide_stepped(
    goal: SteppedGoal,
    limits: EntailmentLimits | None = None,
    *,
    project: Path | None = None,
    cancel: threading.Event | None = None,
    replay: bool = True,
) -> SteppedResult:
    """Decide ``goal`` by exact arithmetic within ``limits``.

    Args:
        goal: The stepped goal.
        limits: The effective limits; the defaults when None. The wall time
            bounds the arithmetic, which the recursions over the horizon
            check at every step; the coefficient bits bound the goal's
            numbers.
        project: Unused: the lane has no replay to run in a project.
        cancel: Set to stop the request, checked as often as the wall time;
            it then ends ``UNKNOWN: cancelled``.
        replay: Whether a pinned replay is asked for. There is none, so a
            decided goal then ends ``UNKNOWN: lean-unavailable``; without
            one it ends with its unchecked application.

    Returns:
        Exactly one of ``PROVEN``, ``REFUTED`` or ``UNKNOWN`` with its reason.
    """
    del project
    limits = limits or EntailmentLimits()
    if type(goal) is not SteppedGoal:
        raise TypeError("goal must be a SteppedGoal")
    deadline = time.monotonic() + limits.wall_time_ms / 1000
    if cancel is not None and cancel.is_set():
        return _unknown(
            UnknownReason.CANCELLED, [Stage("binding", "cancelled")], limits
        )
    try:
        _check_bits(goal, limits)
    except GoalError as error:
        stage = Stage("binding", "resource-limit", str(error))
        return _unknown(UnknownReason.RESOURCE_LIMIT, [stage], limits)
    found = supported(goal)
    if isinstance(found, str):
        stage = Stage("binding", "unsupported", found)
        return _unknown(UnknownReason.UNSUPPORTED, [stage], limits)
    entry, observation = found
    stages = [Stage("binding", "supported", f"{entry.key}/{goal.observation}")]

    def stop() -> bool:
        cancelled = cancel is not None and cancel.is_set()
        return cancelled or time.monotonic() > deadline

    started = time.monotonic()
    interrupted = False
    try:
        verdict = _rule(goal, entry, observation, stop)
    except Interrupted:
        # Only the fixed-date and expected rules recur over the horizon.
        verdict = _Verdict(goal.claim, Status.UNKNOWN, "")
        interrupted = True
    search_ms = _elapsed(started)
    stopped = None
    if cancel is not None and cancel.is_set():
        stopped = UnknownReason.CANCELLED
    elif interrupted or time.monotonic() > deadline:
        stopped = UnknownReason.TIMEOUT
    if stopped is not None:
        stages.append(Stage(verdict.stage, stopped.value, "", search_ms))
        return _unknown(stopped, stages, limits)
    if verdict.application is None:
        outcome = "none" if verdict.reason is UnknownReason.GAP else "unsupported"
        stages.append(Stage(verdict.stage, outcome, verdict.detail, search_ms))
        assert verdict.reason is not None
        return _unknown(verdict.reason, stages, limits)
    if replay:
        stages.append(
            Stage(
                verdict.stage,
                "found",
                f"{verdict.detail}; the stepped lane has no pinned replay, so the"
                " block is for the project's Lean to check",
                search_ms,
                "unavailable",
            )
        )
        return _unknown(UnknownReason.LEAN_UNAVAILABLE, stages, limits)
    stages.append(Stage(verdict.stage, "found", verdict.detail, search_ms, "unchecked"))
    return SteppedResult(
        verdict.status,
        stages=tuple(stages),
        limits=limits.as_dict(),
        dependencies=verdict.application.dependencies,
        application=verdict.application,
    )


def regenerate_application(artifact: bytes, goal: Any, limits: Any) -> str:
    """Refuse to bind an artifact: the stepped lane keeps none."""
    del artifact, goal, limits
    return "the stepped lane keeps no artifacts"


def supported(goal: SteppedGoal) -> tuple[SteppedEntry, SteppedObservation] | str:
    """Bind a goal to its model and observation, or say why the model cannot."""
    entry = MODELS.get(goal.model)
    if entry is None:
        known = ", ".join(sorted(MODELS))
        return f"no stepped model {goal.model!r}; the stepped models are {known}"
    if goal.step <= 0:
        return f"the step must be positive, got {figure(goal.step)}"
    if goal.step != entry.delta:
        return (
            f"{entry.key} is stepped by {figure(entry.delta)}, the step its"
            f" theorems are about, not {figure(goal.step)}"
        )
    if goal.start != entry.start:
        return (
            f"{entry.key} starts at {figure(entry.start)}, not at the start"
            f" {figure(goal.start)}"
        )
    if goal.initial != entry.initial:
        return (
            f"{entry.key}'s theorems start from its initial state"
            f" {_point(entry.initial)}, not {_point(goal.initial)}"
        )
    if goal.horizon == 0:
        return "a claim ranges over at least one step"
    if goal.horizon > entry.horizon_cap:
        return (
            f"the horizon {goal.horizon} exceeds this lane's cap of"
            f" {entry.horizon_cap} steps"
        )
    if goal.claim in _UNROLLED and goal.horizon > entry.unrolled_cap:
        return (
            f"{_UNROLLED[goal.claim]} is supported up to {entry.unrolled_cap}"
            f" steps, not {goal.horizon}, because its proof may unroll a"
            f" recursion over every step, which the kernel checks only that far;"
            f" a breach-within-a-horizon claim (`within N` without `at every"
            f" mark`) goes up to {entry.horizon_cap}"
        )
    if goal.barrier is not None and goal.barrier <= 0:
        return f"the barrier must be positive, got {figure(goal.barrier)}"
    observation = entry.observations.get(goal.observation)
    if observation is None:
        names = ", ".join(entry.observations)
        return (
            f"{entry.key} has no observation {goal.observation!r}; it observes"
            f" {names}"
        )
    if goal.claim == "expected" and observation.kind != "energy":
        return (
            f"an expected claim bounds an energy; {goal.observation} is an"
            f" {observation.kind}"
        )
    if goal.claim != "expected" and observation.kind != "exposure":
        return (
            f"a breach claim observes an exposure; {goal.observation} is an"
            f" {observation.kind}"
        )
    return entry, observation


def _rule(
    goal: SteppedGoal, entry: SteppedEntry, observation: SteppedObservation, stop: Stop
) -> _Verdict:
    """Apply the one rule for the goal's class and claim.

    Raises:
        Interrupted: If ``stop`` asks a recursion over the horizon to stop.
    """
    if goal.claim != "expected" and goal.bound >= 1:
        return _Verdict(
            "trivial-bound",
            Status.PROVEN,
            f"a bound of at least 1 ({figure(goal.bound)}) holds for every"
            " probability law",
            application=render.at_most_one(goal, entry),
        )
    if goal.claim == "horizon":
        if goal.law == "sub-gaussian":
            return _refused(
                "horizon",
                "no horizon theorem for the sub-Gaussian class in this version;"
                " its bound holds at each mark taken separately"
                " (`at every mark within N`)",
            )
        return _horizon(goal, entry)
    if goal.claim == "fixed-date":
        if goal.law == "two-moment":
            return _refused(
                "fixed-date",
                "Chebyshev is not in this version: a bound at each mark needs"
                " the sub-Gaussian class (`shocks subgaussian v`)",
            )
        return _fixed_date(goal, entry, observation, stop)
    if goal.law == "sub-gaussian":
        return _refused(
            "expected",
            "an expected claim needs the two-moment class (`shocks variance v`),"
            " the class its energy theorem is stated for",
        )
    return _expected(goal, entry, stop)


def _horizon(goal: SteppedGoal, entry: SteppedEntry) -> _Verdict:
    assert goal.barrier is not None
    n, q, barrier = goal.horizon, goal.bound, goal.barrier
    bound = horizon_bound(INITIAL_ENERGY, n, goal.step, goal.variance, barrier)
    if bound <= q:
        return _Verdict(
            "horizon",
            Status.PROVEN,
            f"N·Δ²·v/B² {equals(bound)} ≤ {figure(q)}",
            application=render.horizon_hold(goal, entry),
        )
    above = (
        f"the horizon bound N·Δ²·v/B² {equals(bound)} is above the claimed"
        f" {figure(q)}"
    )
    if barrier != 1:
        return _gap(
            f"{above}; the three-point refutation is stated for the barrier 1"
            f" only, not {figure(barrier)}"
        )
    try:
        reach, eps = three_point(goal.step, goal.variance, barrier)
    except ValueError as error:
        return _gap(f"{above}; the three-point refutation does not apply: {error}")
    exact = 1 - (1 - 2 * eps) ** n
    bernoulli = bernoulli_lower(eps, n)
    if q < exact:
        return _Verdict(
            "three-point",
            Status.REFUTED,
            f"the three-point law with jumps ±{reach} and ε {equals(eps)}"
            f" breaches with probability at least 1 − (1 − 2ε)^{n}"
            f" {equals(exact)}, above {figure(q)}",
            application=render.horizon_refutation(
                goal, entry, reach, exact=q >= bernoulli
            ),
        )
    return _gap(
        f"{above}, and not below the three-point threshold 1 − (1 − 2ε)^{n}"
        f" {equals(exact)} at ε {equals(eps)}, nor Bernoulli's 2εN/(1 + 2εN)"
        f" {equals(bernoulli)}"
    )


def _fixed_date(
    goal: SteppedGoal,
    entry: SteppedEntry,
    observation: SteppedObservation,
    stop: Stop,
) -> _Verdict:
    assert goal.barrier is not None and observation.component is not None
    v = goal.variance
    if v.denominator != 1:
        return _refused(
            "fixed-date",
            "the sub-Gaussian route reads the proxy as a whole number"
            f" (`exceeds_at_proxy` takes v : ℕ), got {figure(v)}",
        )
    if v == 0:
        return _refused(
            "fixed-date", "the sub-Gaussian route needs a positive proxy, got 0"
        )
    c = proxy(
        entry.matrix,
        entry.shock,
        observation.component,
        goal.step,
        v,
        goal.horizon,
        stop,
    )
    c_up, level, bound = fixed_date_bound(c, goal.barrier, cap=EXP_CAP)
    source = (
        f"at r {equals(level)}, from the proxy bound c_up {equals(c_up)} on the"
        f" proxy at mark {goal.horizon}, which is {figure(c)}"
    )
    if bound <= goal.bound:
        return _Verdict(
            "fixed-date",
            Status.PROVEN,
            f"2/E(r) {equals(bound)} ≤ {figure(goal.bound)} {source}",
            application=render.fixed_date_hold(goal, entry, c_up, level),
        )
    return _gap(
        f"the fixed-date bound 2/E(r) {equals(bound)} is above the claimed"
        f" {figure(goal.bound)} {source}; no refutation of a claim at each mark"
        " is in this version",
        "fixed-date",
    )


def _expected(goal: SteppedGoal, entry: SteppedEntry, stop: Stop) -> _Verdict:
    value = expected_energy(
        INITIAL_ENERGY, rho(goal.step), goal.step, goal.variance, goal.horizon, stop
    )
    formula = f"Δ²·v·∑_(k<{goal.horizon}) ρ^k {equals(value)}"
    if value <= goal.bound:
        return _Verdict(
            "expected",
            Status.PROVEN,
            f"{formula} ≤ {figure(goal.bound)}",
            application=render.expected_hold(goal, entry),
        )
    reach = _reach(goal.step, goal.variance)
    return _Verdict(
        "expected",
        Status.REFUTED,
        f"{formula} > {figure(goal.bound)}, the exact expectation under the"
        f" three-point law with jumps ±{reach}, which has variance"
        f" {figure(goal.variance)}",
        application=render.expected_refutation(goal, entry, reach),
    )


def _reach(step: Fraction, variance: Fraction) -> int:
    """Return the jump of a three-point witness of ``variance``, with ``ε ≤ 1/2``."""
    try:
        reach, _ = three_point(step, variance, Fraction(1))
    except ValueError:
        reach = math.isqrt(math.ceil(variance)) + 1
    return reach


def _refused(stage: str, detail: str) -> _Verdict:
    return _Verdict(stage, Status.UNKNOWN, detail, UnknownReason.UNSUPPORTED)


def _gap(detail: str, stage: str = "three-point") -> _Verdict:
    return _Verdict(stage, Status.UNKNOWN, detail, UnknownReason.GAP)


def _check_bits(goal: SteppedGoal, limits: EntailmentLimits) -> None:
    """Refuse a goal whose numbers exceed the coefficient limit."""
    numbers = [goal.start, goal.step, goal.variance, goal.bound, *goal.initial]
    if goal.barrier is not None:
        numbers.append(goal.barrier)
    for value in numbers:
        check_rational_bits(value, limits)


def _point(values: tuple[Fraction, ...]) -> str:
    return "(" + ", ".join(figure(value) for value in values) + ")"


def _unknown(
    reason: UnknownReason, stages: list[Stage], limits: EntailmentLimits
) -> SteppedResult:
    return SteppedResult(
        Status.UNKNOWN, reason, stages=tuple(stages), limits=limits.as_dict()
    )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
