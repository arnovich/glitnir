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
  other paths for a hedge built for a narrower band. A refutation's block
  states the path's exact shortfall, so one whose numerator or denominator
  has more than `MAX_SHORTFALL_BITS` bits is ``UNKNOWN: resource-limit``
  instead.

Every figure is a `fractions.Fraction` (`hedging.numbers`), shown in a reason
by the rule the stepped lane shares (`figures.figure`): exactly when short,
with its decimal beside it when four places do not write it, and by its
decimal alone when long. The path search, which only a ``shortfall`` claim
runs, stops as soon as the request is cancelled or out of time. A
decided goal carries the Lean application of its template (`hedging.render`),
unchecked: it is a proposal for the project's Lean to check. There is no
pinned replay for this lane, so a request that asks for one ends ``UNKNOWN:
lean-unavailable`` instead of claiming what nothing checked.

A band's moves are simple returns as fractions of the spot: ``down 1/50 up
1/50`` is ±2 % a step. A down move of ``1`` or more is refused with that hint,
since a goal written in percent (``down 2``) reaches the decider as one.
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
#: Most bits in the numerator or denominator of the shortfall a refutation's
#: block states exactly: about 3900 decimal digits, below Python's default
#: limit of 4300 on writing an integer in decimal. The search compounds the
#: band's numbers over every step, so a band written with long numbers can
#: exceed it well within the step cap.
MAX_SHORTFALL_BITS = 13_000
#: What a search that ran out of time was doing, for its reason.
_WORK = {
    "price": "computing the binomial price",
    "extreme-path": "searching for the worst extreme path",
}
#: How a band's moves are written, for a refusal of a down move of 1 or more.
_UNITS = (
    "moves are simple returns as fractions of the spot: ±2 % a step is"
    " `down 1/50 up 1/50` or `down 0.02 up 0.02`"
)


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
        detail = ""
        if stopped is UnknownReason.TIMEOUT:
            work = _WORK.get(verdict.stage, "deciding")
            detail = (
                f"{work} over {goal.steps} steps ran out of time"
                f" ({limits.wall_time_ms} ms); use fewer steps or a larger wall"
                " time"
            )
        stages.append(Stage(verdict.stage, stopped.value, detail, search_ms))
        return _unknown(stopped, stages, limits)
    if verdict.application is None:
        assert verdict.reason in (UnknownReason.GAP, UnknownReason.RESOURCE_LIMIT)
        outcome = "none" if verdict.reason is UnknownReason.GAP else "resource-limit"
        stages.append(Stage(verdict.stage, outcome, verdict.detail, search_ms))
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
            f" below; the band's theorems need a down move below 1, and {_UNITS}"
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
    band, the binomial tree's, by the replication identity, which no block
    states; so from the premium it ends ``short``, the price less the premium,
    below it on each. No search runs: the all-up path is named as one of them,
    so nothing can stop an established refutation.
    """
    return (
        f"from it the band's own hedge ends the difference, {figure(short)},"
        " below the payoff on every extreme path of the band, by the replication"
        " identity, which the block does not state; the all-up path of"
        f" {goal.steps} rises of {figure(hedged.up)} is one of them"
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
    moves = render.path_words(path, against.up, against.down)
    if worst < -bound:
        if max(worst.numerator.bit_length(), worst.denominator.bit_length()) > (
            MAX_SHORTFALL_BITS
        ):
            return _Verdict(
                "extreme-path",
                Status.UNKNOWN,
                f"the worst extreme path, {moves}, would refute the claim: the"
                f" hedge ends {_ends(worst)} on it; but that shortfall is too"
                " large to state exactly in a block (more than"
                f" {MAX_SHORTFALL_BITS} bits in its numerator or denominator);"
                " use fewer steps or a band written with shorter numbers",
                UnknownReason.RESOURCE_LIMIT,
            )
        returns = _returns(path, against)
        if bound >= 0 and worst < 0:
            found = (
                f"the hedge ends {figure(-worst)} below the payoff, more than"
                f" {figure(bound)}"
            )
        else:
            found = (
                f"the seller's account less the payoff ends at {figure(worst)},"
                f" below the claimed {figure(-bound)}"
            )
        return _Verdict(
            "extreme-path",
            Status.REFUTED,
            f"on {moves}, {found}",
            application=render.shortfall_refutation(goal, payoff, returns, worst),
        )
    return _Verdict(
        "extreme-path",
        Status.UNKNOWN,
        f"the worst extreme path, {moves}, ends {_ends(worst)}, within the claimed"
        f" {figure(bound)}: bounds below {figure(-worst)} are refuted on this"
        " path; no bound for a wider band is proved in this version;"
        f" `{_sampling(goal, payoff)}` samples the band's other paths",
        UnknownReason.GAP,
    )


def _ends(worst: Fraction) -> str:
    """Say where the hedge ends against the payoff: ``≈ 4.1129 below the payoff``."""
    if worst < 0:
        return f"{figure(-worst)} below the payoff"
    if worst > 0:
        return f"{figure(worst)} above the payoff"
    return "at the payoff"


def _sampling(goal: HedgingGoal, payoff: Payoff) -> str:
    """Write the ``hedge`` directive that samples a shortfall goal's against band."""
    assert goal.against_up is not None and goal.against_down is not None
    numbers_written = (
        ("strike", goal.strike),
        ("spot", goal.spot),
        ("down", goal.down),
        ("up", goal.up),
    )
    options = [f"({key} := {render.numeral(value)})" for key, value in numbers_written]
    options += [
        f"(steps := {goal.steps})",
        f"(against_down := {render.numeral(goal.against_down)})",
        f"(against_up := {render.numeral(goal.against_up)})",
    ]
    return f"-- @evidence hedge {payoff.lean_name} {' '.join(options)}"


def _returns(path: Sequence[bool], against: Band) -> tuple[Fraction, ...]:
    """Read an extreme path's returns: ``+up`` for a rise, ``-down`` for a fall."""
    return tuple(against.up if rise else -against.down for rise in path)


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
