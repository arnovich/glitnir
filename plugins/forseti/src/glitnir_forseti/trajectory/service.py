"""Decide a closed trajectory root goal: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.

Two bounded templates, in order, each ending in a fresh replay of the root:

1. *Certificate.* The model's named contract proves an invariant of the
   observation for all forward time. The entailment certificate lane searches for
   a certificate that the invariant entails the goal's postcondition; with one,
   the root follows by ``Contract.consequence``.
2. *Initial witness.* The entailment refutation lane asks whether the postcondition
   fails at the observation's exact value at the declared start (a named
   lemma). With a rational point, the admitted declared input's own solution
   violates the root there.

A leaf result is never a root result: each candidate is rendered as the whole
root theorem and replayed by `replay_root` (or, with ``replay=False``, returned
unchecked with that application, for the caller's Lean to check). A
counterexample to the leaf entailment alone — a value inside the invariant
where the postcondition fails — is not a root refutation, because no
admissible trajectory is shown to reach it; that goal ends ``UNKNOWN``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from glitnir_forseti.entailment.evidence import Certificate, PsatzCertificate
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import InvalidEvidence, ReplayUnavailable
from glitnir_forseti.entailment.search import certify, refute
from glitnir_forseti.entailment.syntax import (
    Add,
    Atom,
    Const,
    Goal,
    GoalError,
    Neg,
    Relation,
    Var,
)

from .goal import TrajectoryGoal, check_goal_limits
from .render import RootApplication
from .replay import (
    InitialWitness,
    ValidatedRoot,
    application,
    build_artifact,
    replay_root,
    supported,
)


class Status(str, Enum):
    """The three external outcomes."""

    PROVEN = "PROVEN"
    REFUTED = "REFUTED"
    UNKNOWN = "UNKNOWN"


class UnknownReason(str, Enum):
    """Why a request ended ``UNKNOWN``: unsupported, exhausted or interrupted."""

    UNSUPPORTED = "unsupported"
    EXHAUSTED = "exhausted"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RESOURCE_LIMIT = "resource-limit"
    Z3_UNAVAILABLE = "z3-unavailable"
    LEAN_UNAVAILABLE = "lean-unavailable"


@dataclass(frozen=True, slots=True)
class Stage:
    """One stage of a request and how it ended.

    Attributes:
        stage: ``binding``, ``certificate`` or ``initial-witness``.
        outcome: The stage's own outcome.
        detail: Its diagnostic.
        search_ms: Time in the solver.
        check: ``validated``, ``invalid``, ``unavailable``, ``unchecked``
            (found without a replay) or empty.
        check_ms: Time in Lean.
        stop: What stopped an unavailable check, if anything.
    """

    stage: str
    outcome: str
    detail: str = ""
    search_ms: int = 0
    check: str = ""
    check_ms: int = 0
    stop: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the stage for machine-readable reports."""
        return {
            "stage": self.stage,
            "outcome": self.outcome,
            "detail": self.detail,
            "search_ms": self.search_ms,
            "check": self.check,
            "check_ms": self.check_ms,
            "stop": self.stop,
        }


@dataclass(frozen=True, slots=True)
class TrajectoryResult:
    """The outcome of one request.

    Attributes:
        status: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.
        reason: For ``UNKNOWN``, why.
        artifact: For a decided goal, the replayable root artifact.
        stages: Every stage that ran, in order.
        limits: The effective limits.
        axioms: The root theorem's axioms, when decided.
        dependencies: The Lean lemmas the root proof cites, when decided.
        application: Without a replay, the unchecked root application of the
            evidence found; None otherwise.
    """

    status: Status
    reason: UnknownReason | None = None
    artifact: bytes | None = None
    stages: tuple[Stage, ...] = ()
    limits: dict[str, int] = field(default_factory=dict)
    axioms: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    application: RootApplication | None = None


#: How a stopped producer or check becomes an unknown reason.
_STOPS = {
    "timeout": UnknownReason.TIMEOUT,
    "cancelled": UnknownReason.CANCELLED,
    "resource-limit": UnknownReason.RESOURCE_LIMIT,
    "output": UnknownReason.RESOURCE_LIMIT,
    "heartbeats": UnknownReason.RESOURCE_LIMIT,
    "z3-unavailable": UnknownReason.Z3_UNAVAILABLE,
}


def decide_trajectory(
    goal: TrajectoryGoal,
    limits: EntailmentLimits | None = None,
    *,
    project: Path | None = None,
    cancel: threading.Event | None = None,
    replay: bool = True,
) -> TrajectoryResult:
    """Decide ``goal`` within ``limits``.

    Args:
        goal: The closed root goal.
        limits: The effective limits; the defaults when None.
        project: The trusted Lean project; ``FORSETI_LEAN_PROJECT`` when None.
        cancel: Set to stop the request; it then ends ``UNKNOWN``.
        replay: Whether the root is replayed in the pinned Lean release. When
            False, the first evidence found ends the request unchecked, with
            its root application and no artifact.

    Returns:
        Exactly one of ``PROVEN``, ``REFUTED`` or ``UNKNOWN`` with its reason.
    """
    limits = limits or EntailmentLimits()
    if type(goal) is not TrajectoryGoal:
        raise TypeError("goal must be a TrajectoryGoal")
    deadline = time.monotonic() + limits.wall_time_ms / 1000
    try:
        check_goal_limits(goal, limits)
    except GoalError as error:
        return _unknown(
            UnknownReason.RESOURCE_LIMIT,
            [Stage("binding", "resource-limit", str(error))],
            limits,
        )
    found = supported(goal)
    if isinstance(found, str):
        refused = Stage("binding", "unsupported", found)
        return _unknown(UnknownReason.UNSUPPORTED, [refused], limits)
    entry, band = found
    stages: list[Stage] = [Stage("binding", "supported", f"{entry.key}/{band.port}")]
    context = goal.context
    leaves = (
        ("certificate", Goal(context, band.invariant, goal.post)),
        (
            "initial-witness",
            Goal(
                context,
                Atom(Relation.EQ, Add(Var(0), Neg(Const(band.initial_value)))),
                goal.post,
            ),
        ),
    )
    for stage, leaf in leaves:
        if cancel is not None and cancel.is_set():
            stages.append(Stage(stage, "cancelled"))
            break
        started = time.monotonic()
        # Each search gets half of what is left, keeping time for the check.
        share = (deadline - started) / 2
        produced = (
            certify(leaf, limits, started + share, cancel)
            if stage == "certificate"
            else refute(leaf, limits, started + share, cancel)
        )
        search_ms = _elapsed(started)
        if produced.outcome != "found" or produced.evidence is None:
            stages.append(Stage(stage, produced.outcome, produced.detail, search_ms))
            if produced.outcome == "cancelled" or time.monotonic() >= deadline:
                break
            continue
        evidence: Certificate | InitialWitness
        if isinstance(produced.evidence, Certificate):
            evidence = produced.evidence
        elif isinstance(produced.evidence, PsatzCertificate):
            # Leaves search `certify` alone, which never names products.
            raise TypeError("a trajectory leaf has no Psatz certificate")
        else:
            ((_, value),) = produced.evidence.assignment
            evidence = InitialWitness(goal.start, goal.initial, value)
        if not replay:
            unit = application(goal, entry, band, evidence)
            stages.append(
                Stage(stage, "found", produced.detail, search_ms, "unchecked")
            )
            return TrajectoryResult(
                Status.PROVEN if isinstance(evidence, Certificate) else Status.REFUTED,
                stages=tuple(stages),
                limits=limits.as_dict(),
                dependencies=unit.dependencies,
                application=unit,
            )
        artifact = build_artifact(
            goal,
            evidence,
            limits,
            {"stage": stage, "backend": "z3", "search_ms": search_ms},
        )
        started = time.monotonic()
        outcome = replay_root(
            artifact, goal, limits, project, deadline=deadline, cancel=cancel
        )
        check_ms = _elapsed(started)
        if isinstance(outcome, ValidatedRoot):
            stages.append(
                Stage(stage, "found", produced.detail, search_ms, "validated", check_ms)
            )
            return TrajectoryResult(
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


def _reason(stages: list[Stage]) -> UnknownReason:
    """Name why no stage decided the goal: interrupted, unsupported or exhausted."""
    for stage in stages:
        if stage.check == "unavailable":
            return _STOPS.get(stage.stop, UnknownReason.LEAN_UNAVAILABLE)
    for stage in stages:
        if stage.outcome in _STOPS:
            return _STOPS[stage.outcome]
    outcomes = {stage.stage: stage.outcome for stage in stages}
    if outcomes.get("certificate") == "unsupported-fragment" or (
        outcomes.get("initial-witness") == "unsupported-witness"
    ):
        return UnknownReason.UNSUPPORTED
    # No certificate over the dictionary, no violation at the start, or a
    # template Lean did not accept for this root: the bounded templates are
    # exhausted, which says nothing about the root's truth.
    return UnknownReason.EXHAUSTED


def _unknown(
    reason: UnknownReason, stages: list[Stage], limits: EntailmentLimits
) -> TrajectoryResult:
    return TrajectoryResult(
        Status.UNKNOWN, reason, stages=tuple(stages), limits=limits.as_dict()
    )


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
