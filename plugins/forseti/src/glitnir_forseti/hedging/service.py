"""Decide a hedging goal with exact arithmetic: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.

The decider runs no Lean and no solver. It first refuses (``UNKNOWN:
unsupported``) what the library's theorems do not speak of: a payoff it does
not register, a spot or a step count that is not positive, a band whose up or
down move is not positive or whose down move is ``1`` or more (the spot would
reach zero), and for a ``shortfall`` claim an against band that does not
contain the hedge's band. Then it caps the steps before any arithmetic runs
(``UNKNOWN: resource-limit``): `COVERS_CAP` for a ``covers`` claim, whose block
has the kernel evaluate one price, and `SHORTFALL_CAP` for a ``shortfall``
claim, whose search for the worst extreme path is about cubic in the steps in
rational operations, and whose refutation has the kernel evaluate the hedge
along a path of every step. Then one rule per claim:

* ``covers``: the binomial price is computed exactly. A premium at least the
  price holds (`covers_hedge` from the price, then `Covers.mono`); a premium
  below it is refuted for every spot-feedback strategy (`not_covers_of_lt`).
  The reason says how far short the band's own hedge ends from the premium:
  from the price it ends at the payoff on every extreme path of its band, so
  from the premium it ends exactly the difference short on each, with no
  search.
* ``shortfall``: the hedge run on its own band with a bound of at least ``0``
  holds (`covers_hedge`). Otherwise the worst extreme path of the against band
  is found by `numbers.worst_extreme_path`; if the hedge ends more than the
  bound below the payoff on it, that path refutes the claim, and if not the
  claim is ``UNKNOWN: gap``: no theorem bounds the shortfall over the band's
  other paths for a hedge built for a narrower band.

Every figure is a `fractions.Fraction` (`hedging.numbers`), shown in a reason
by the rule the stepped lane shares (`figures.figure`): exactly when short,
with its decimal beside it when four places do not write it, and by its
decimal alone when long. The path search, which only a ``shortfall`` claim
runs, stops as soon as the request is cancelled or out of time. A
decided goal carries the Lean application of its template (`hedging.render`),
unchecked: it is a proposal for the project's Lean to check. There is no
pinned replay for this lane, so a request that asks for one ends ``UNKNOWN:
lean-unavailable`` instead of claiming what nothing checked.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import GoalError, check_rational_bits
from glitnir_forseti.figures import figure
from glitnir_forseti.trajectory.render import RootApplication

from . import numbers, render
from .goal import HedgingGoal
from .numbers import Interrupted, Stop
from .payoffs import PAYOFFS, Payoff

#: The most steps a ``covers`` claim is decided for: its block has the kernel
#: evaluate the price, a sum of ``N + 1`` terms over ``ℚ``.
COVERS_CAP = 250
#: The most steps a ``shortfall`` claim is decided for: the worst-path search
#: is about cubic in the steps, and its refutation has the kernel evaluate the
#: hedge along a path of every step.
SHORTFALL_CAP = 60
_CAPS = {"covers": COVERS_CAP, "shortfall": SHORTFALL_CAP}


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
        stage: ``binding``, or the rule that ran: ``price``, ``own-band`` or
            ``extreme-path``.
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
class HedgingResult:
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
class Band:
    """One step's band: from ``S`` the spot moves into ``[S (1 - down), S (1 + up)]``.

    Attributes:
        up: How far the spot may rise, as a fraction of it.
        down: How far the spot may fall, as a fraction of it.
    """

    up: Fraction
    down: Fraction


@dataclass(frozen=True, slots=True)
class _Verdict:
    """What one rule found: a template to propose, or why there is none."""

    stage: str
    status: Status
    detail: str
    reason: UnknownReason | None = None
    application: RootApplication | None = None


def decide_hedging(
    goal: HedgingGoal,
    limits: EntailmentLimits | None = None,
    *,
    project: Path | None = None,
    cancel: threading.Event | None = None,
    replay: bool = True,
) -> HedgingResult:
    """Decide ``goal`` by exact arithmetic within ``limits``.

    Args:
        goal: The hedging goal.
        limits: The effective limits; the defaults when None. The wall time
            bounds the arithmetic, which the path search checks before each
            state of the tree; the coefficient bits bound the goal's numbers.
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
    if type(goal) is not HedgingGoal:
        raise TypeError("goal must be a HedgingGoal")
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
    payoff, hedged = found
    cap = _CAPS[goal.claim]
    if goal.steps > cap:
        stage = Stage(
            "binding",
            "resource-limit",
            f"a {goal.claim} claim is decided up to {cap} steps, not {goal.steps}",
        )
        return _unknown(UnknownReason.RESOURCE_LIMIT, [stage], limits)
    stages = [
        Stage(
            "binding",
            "supported",
            f"the {payoff.key} struck at {figure(goal.strike)}, {goal.steps} steps",
        )
    ]

    def stop() -> bool:
        cancelled = cancel is not None and cancel.is_set()
        return cancelled or time.monotonic() > deadline

    started = time.monotonic()
    interrupted = False
    try:
        verdict = _rule(goal, payoff, hedged, stop)
    except Interrupted:
        # Only the extreme-path search asks whether to stop.
        verdict = _Verdict("extreme-path", Status.UNKNOWN, "")
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
        assert verdict.reason is UnknownReason.GAP
        stages.append(Stage(verdict.stage, "none", verdict.detail, search_ms))
        return _unknown(verdict.reason, stages, limits)
    if replay:
        stages.append(
            Stage(
                verdict.stage,
                "found",
                f"{verdict.detail}; the hedging lane has no pinned replay, so the"
                " block is for the project's Lean to check",
                search_ms,
                "unavailable",
            )
        )
        return _unknown(UnknownReason.LEAN_UNAVAILABLE, stages, limits)
    stages.append(Stage(verdict.stage, "found", verdict.detail, search_ms, "unchecked"))
    return HedgingResult(
        verdict.status,
        stages=tuple(stages),
        limits=limits.as_dict(),
        dependencies=verdict.application.dependencies,
        application=verdict.application,
    )


def regenerate_application(artifact: bytes, goal: Any, limits: Any) -> str:
    """Refuse to bind an artifact: the hedging lane keeps none."""
    del artifact, goal, limits
    return "the hedging lane keeps no artifacts"


def supported(goal: HedgingGoal) -> tuple[Payoff, Band] | str:
    """Bind a goal to its payoff and its hedge's band, or say why they cannot be.

    The checks need no arithmetic. A goal's own checks already refuse a
    non-positive spot, step count or move; they are repeated here so that the
    decider answers ``UNKNOWN`` with a sentence for any goal value it is given.
    """
    payoff = PAYOFFS.get(goal.payoff)
    if payoff is None:
        known = ", ".join(PAYOFFS)
        return f"no hedging payoff {goal.payoff!r}; the payoffs are {known}"
    if goal.steps < 1:
        return f"a hedge runs over at least one step, got {goal.steps}"
    if goal.spot <= 0:
        return f"the spot must be positive, got {figure(goal.spot)}"
    problem = _band_problem("the band", goal.up, goal.down)
    if problem is not None:
        return problem
    if goal.claim == "shortfall":
        assert goal.against_up is not None and goal.against_down is not None
        problem = _band_problem("the against band", goal.against_up, goal.against_down)
        if problem is not None:
            return problem
        if goal.against_up < goal.up or goal.against_down < goal.down:
            return (
                f"the against band, down {figure(goal.against_down)} up"
                f" {figure(goal.against_up)}, does not contain the hedge's band,"
                f" down {figure(goal.down)} up {figure(goal.up)}; a shortfall is"
                " decided for a hedge run on its own band or a wider one"
            )
    return payoff, Band(goal.up, goal.down)


def _band_problem(name: str, up: Fraction, down: Fraction) -> str | None:
    """Say why a band is not one `Band.ofRat` accepts, or return None."""
    if up <= 0:
        return f"{name}'s up move must be positive, got {figure(up)}"
    if down <= 0:
        return f"{name}'s down move must be positive, got {figure(down)}"
    if down >= 1:
        return (
            f"{name}'s down move {figure(down)} would take the spot to zero or"
            " below; the band's theorems need a down move below 1"
        )
    return None


def _rule(goal: HedgingGoal, payoff: Payoff, hedged: Band, stop: Stop) -> _Verdict:
    """Apply the one rule for the goal's claim.

    Raises:
        Interrupted: If ``stop`` asks the extreme-path search to stop.
    """
    if goal.claim == "covers":
        return _covers(goal, payoff, hedged)
    return _shortfall(goal, payoff, hedged, stop)


def _covers(goal: HedgingGoal, payoff: Payoff, hedged: Band) -> _Verdict:
    assert goal.premium is not None
    premium = goal.premium
    fair = numbers.price(
        payoff, goal.strike, goal.spot, hedged.up, hedged.down, goal.steps
    )
    if fair <= premium:
        return _Verdict(
            "price",
            Status.PROVEN,
            f"the binomial price {figure(fair)} is at most the premium"
            f" {figure(premium)}",
            application=render.covers_hold(goal, payoff),
        )
    return _Verdict(
        "price",
        Status.REFUTED,
        f"the premium {figure(premium)} is below the binomial price"
        f" {figure(fair)}, so no spot-feedback strategy covers from it;"
        f" {_own_paths(goal, hedged, fair - premium)}",
        application=render.covers_refutation(goal, payoff),
    )


def _own_paths(goal: HedgingGoal, hedged: Band, short: Fraction) -> str:
    """Say how far the band's own hedge from the premium ends below the payoff.

    From the price the hedge ends at the payoff on every extreme path of its
    band, the binomial tree's, so from the premium it ends ``short``, the
    price less the premium, below it on each. No search runs: the all-up path
    is named as one of them, so nothing can stop an established refutation.
    """
    return (
        f"from it the band's own hedge ends the difference, {figure(short)},"
        " below the payoff on every extreme path of the band, the all-up path of"
        f" {goal.steps} rises of {figure(hedged.up)} among them"
    )


def _shortfall(goal: HedgingGoal, payoff: Payoff, hedged: Band, stop: Stop) -> _Verdict:
    assert goal.against_up is not None and goal.against_down is not None
    assert goal.bound is not None
    against, bound = Band(goal.against_up, goal.against_down), goal.bound
    if against == hedged and bound >= 0:
        return _Verdict(
            "own-band",
            Status.PROVEN,
            "run on its own band the hedge covers the payoff from its price, so"
            f" it ends at most 0 ≤ {figure(bound)} below it",
            application=render.shortfall_hold(goal, payoff),
        )
    path, worst = numbers.worst_extreme_path(
        payoff,
        goal.strike,
        goal.spot,
        hedged.up,
        hedged.down,
        against.up,
        against.down,
        goal.steps,
        stop,
    )
    moves = _moves(path, against)
    if worst < -bound:
        returns = _returns(path, against)
        return _Verdict(
            "extreme-path",
            Status.REFUTED,
            f"on the extreme path {moves} the hedge ends {figure(-worst)} below"
            f" the payoff, more than {figure(bound)}",
            application=render.shortfall_refutation(goal, payoff, returns, worst),
        )
    return _Verdict(
        "extreme-path",
        Status.UNKNOWN,
        f"the worst extreme path {moves} ends {figure(-worst)} below the payoff,"
        f" within the claimed {figure(bound)}; no theorem in this version bounds"
        " the shortfall over the against band's other paths for a hedge built"
        " for a narrower band",
        UnknownReason.GAP,
    )


def _returns(path: Sequence[bool], against: Band) -> tuple[Fraction, ...]:
    """Read an extreme path's returns: ``+up`` for a rise, ``-down`` for a fall."""
    return tuple(against.up if rise else -against.down for rise in path)


def _moves(path: Sequence[bool], moved: Band) -> str:
    """Write an extreme path as ``UDUD (U a rise of 1/50, D a fall of 1/50)``."""
    letters = "".join("U" if rise else "D" for rise in path)
    return (
        f"{letters} (U a rise of {figure(moved.up)}, D a fall of"
        f" {figure(moved.down)})"
    )


def _check_bits(goal: HedgingGoal, limits: EntailmentLimits) -> None:
    """Refuse a goal whose numbers exceed the coefficient limit."""
    values = [goal.strike, goal.spot, goal.up, goal.down]
    for value in (goal.premium, goal.against_up, goal.against_down, goal.bound):
        if value is not None:
            values.append(value)
    for value in values:
        check_rational_bits(value, limits)


def _unknown(
    reason: UnknownReason, stages: list[Stage], limits: EntailmentLimits
) -> HedgingResult:
    return HedgingResult(
        Status.UNKNOWN, reason, stages=tuple(stages), limits=limits.as_dict()
    )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
