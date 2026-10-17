"""Decide one closed entailment goal: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.

The whole request is one goal and explicit limits; there is no goal discovery,
schema instantiation or model in the loop. Two untrusted lanes produce
evidence, refutation first (a rational point, or a real-algebraic one built
from Z3's exact model) and then the fixed-dictionary certificate
when the goal is in its fragment and budget remains. Every result that is not
``UNKNOWN`` comes from `replay.replay_artifact` on the encoded artifact, the same fresh
check an offline replay runs: a solver answer by itself decides nothing.
With ``replay=False`` nothing is replayed: a ``PROVEN`` or ``REFUTED`` status
then only says a lane found evidence, and the caller must have Lean check the
application the result carries.

A failed certificate is never a refutation, and the absence of a
counterexample is never a proof. Both lanes' outcomes are retained.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from . import render
from .algebraic import AlgebraicWitness
from .algebraic_search import build_witness
from .artifact import build_artifact
from .evidence import Certificate, PsatzCertificate, Witness
from .limits import EntailmentLimits
from .replay import (
    InvalidEvidence,
    ReplayUnavailable,
    ValidatedEntailment,
    replay_artifact,
)
from .search import LaneOutcome, certify_with_products, refute
from .syntax import Goal, GoalError, Poly, check_goal_limits


class Status(str, Enum):
    """The three external outcomes."""

    PROVEN = "PROVEN"
    REFUTED = "REFUTED"
    UNKNOWN = "UNKNOWN"


class UnknownReason(str, Enum):
    """Why a request ended ``UNKNOWN``."""

    UNSUPPORTED_FRAGMENT = "unsupported-fragment"
    UNSUPPORTED_WITNESS = "unsupported-witness"
    DICTIONARY_EXHAUSTED = "dictionary-exhausted"
    RESOURCE_LIMIT = "resource-limit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_EVIDENCE = "invalid-evidence"
    Z3_UNAVAILABLE = "z3-unavailable"
    Z3_UNKNOWN = "z3-unknown"
    LEAN_UNAVAILABLE = "lean-unavailable"


#: The fraction of the remaining wall time each lane's search may use.
_SEARCH_SHARE = {"refutation": 1 / 3, "certificate": 1 / 2}

#: When lanes end differently, the first reason in this order is reported;
#: every lane's own outcome is retained regardless.
_PRECEDENCE = (
    UnknownReason.CANCELLED,
    UnknownReason.TIMEOUT,
    UnknownReason.UNSUPPORTED_WITNESS,
    UnknownReason.INVALID_EVIDENCE,
    UnknownReason.LEAN_UNAVAILABLE,
    UnknownReason.RESOURCE_LIMIT,
    UnknownReason.Z3_UNAVAILABLE,
    UnknownReason.Z3_UNKNOWN,
    UnknownReason.DICTIONARY_EXHAUSTED,
    UnknownReason.UNSUPPORTED_FRAGMENT,
)


@dataclass(frozen=True, slots=True)
class LaneRecord:
    """One lane's search and, when it found evidence, its Lean check.

    Attributes:
        lane: ``refutation`` or ``certificate``.
        outcome: The producer's outcome (see `search.LaneOutcome`), or
            ``skipped``.
        detail: The backend diagnostic.
        search_ms: Wall time in the solver.
        check: ``validated``, ``invalid``, ``unavailable``, ``unchecked``
            (found without a replay) or empty.
        check_detail: What the check reported.
        check_ms: Wall time in Lean.
    """

    lane: str
    outcome: str
    detail: str = ""
    search_ms: int = 0
    check: str = ""
    check_detail: str = ""
    check_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return the record for machine-readable reports."""
        return {
            "lane": self.lane,
            "outcome": self.outcome,
            "detail": self.detail,
            "search_ms": self.search_ms,
            "check": self.check,
            "check_detail": self.check_detail,
            "check_ms": self.check_ms,
        }


@dataclass(frozen=True, slots=True)
class EntailmentResult:
    """The outcome of one request.

    Attributes:
        status: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.
        reason: For ``UNKNOWN``, why.
        artifact: For ``PROVEN`` and ``REFUTED``, the replayable artifact.
        lanes: Every lane that ran, in order.
        limits: The effective limits.
        axioms: The checked theorem's axioms, when decided.
        application: Without a replay, the unchecked application of the
            evidence found; None otherwise.
    """

    status: Status
    reason: UnknownReason | None = None
    artifact: bytes | None = None
    lanes: tuple[LaneRecord, ...] = ()
    limits: dict[str, int] = field(default_factory=dict)
    axioms: tuple[str, ...] = ()
    application: render.Application | None = None


def decide_entailment(
    goal: Goal,
    limits: EntailmentLimits | None = None,
    *,
    dictionary: tuple[Poly, ...] | None = None,
    project: Path | None = None,
    cancel: threading.Event | None = None,
    replay: bool = True,
) -> EntailmentResult:
    """Decide ``goal`` within ``limits``.

    Args:
        goal: The closed goal: an ordered context and two formulas.
        limits: The effective limits; the defaults when None.
        dictionary: The square dictionary for the certificate lane;
            ``{1, x_0, …, x_(n-1)}`` when None.
        project: The trusted Lean project; ``FORSETI_LEAN_PROJECT`` when None.
        cancel: Set to stop the request; it then ends ``UNKNOWN``.
        replay: Whether evidence is replayed in the pinned Lean release. When
            False, the first evidence found ends the request unchecked, with
            its application and no artifact.

    Returns:
        Exactly one of ``PROVEN``, ``REFUTED`` or ``UNKNOWN`` with its reason.
    """
    limits = limits or EntailmentLimits()
    if type(goal) is not Goal:
        raise TypeError("goal must be a Goal")
    deadline = time.monotonic() + limits.wall_time_ms / 1000
    try:
        check_goal_limits(goal, limits)
    except GoalError as error:
        return EntailmentResult(
            Status.UNKNOWN,
            UnknownReason.RESOURCE_LIMIT,
            lanes=(LaneRecord("request", "resource-limit", str(error)),),
            limits=limits.as_dict(),
        )
    lanes: list[LaneRecord] = []
    for lane in ("refutation", "certificate"):
        if cancel is not None and cancel.is_set():
            lanes.append(LaneRecord(lane, "cancelled"))
            break
        if time.monotonic() >= deadline:
            lanes.append(LaneRecord(lane, "timeout"))
            break
        started = time.monotonic()
        # Each search gets a share of what is left, so a hard refutation
        # cannot starve the certificate lane or the Lean check.
        share = _SEARCH_SHARE[lane] * (deadline - started)
        found = (
            refute(goal, limits, started + share, cancel)
            if lane == "refutation"
            else certify_with_products(
                goal, limits, started + share, cancel, dictionary
            )
        )
        search_ms = _elapsed(started)
        evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness | None = (
            found.evidence
        )
        if found.roots:
            found, evidence = _algebraic(goal, found, limits, started + share, cancel)
        if found.outcome != "found" or evidence is None:
            lanes.append(LaneRecord(lane, found.outcome, found.detail, search_ms))
            if found.outcome == "cancelled" or time.monotonic() >= deadline:
                break
            continue
        if not replay:
            lanes.append(
                LaneRecord(lane, "found", found.detail, search_ms, "unchecked")
            )
            proven = isinstance(evidence, (Certificate, PsatzCertificate))
            return EntailmentResult(
                Status.PROVEN if proven else Status.REFUTED,
                lanes=tuple(lanes),
                limits=limits.as_dict(),
                application=render.application(goal, evidence),
            )
        decided = _check(
            goal, evidence, found, lane, search_ms, limits, deadline, project, cancel
        )
        lanes.append(decided[0])
        if decided[1] is not None:
            outcome, artifact = decided[1]
            return EntailmentResult(
                Status(outcome.status),
                artifact=artifact,
                lanes=tuple(lanes),
                limits=limits.as_dict(),
                axioms=outcome.axioms,
            )
        if decided[0].check == "unavailable":
            # Lean could not check this lane's evidence; it could not check
            # the other lane's either, so do not spend the budget finding it.
            break
    return EntailmentResult(
        Status.UNKNOWN, _reason(lanes), lanes=tuple(lanes), limits=limits.as_dict()
    )


def _algebraic(
    goal: Goal,
    found: LaneOutcome,
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None,
) -> tuple[LaneOutcome, AlgebraicWitness | None]:
    """Turn an irrational model into a witness ``algebraicRefutes`` can check."""
    built = build_witness(goal, found.roots, limits, deadline, cancel)
    if isinstance(built, str):
        if cancel is not None and cancel.is_set():
            outcome = "cancelled"
        elif time.monotonic() >= deadline:
            outcome = "timeout"
        else:
            outcome = "unsupported-witness"
        return LaneOutcome(outcome, f"{found.detail}; {built}"), None
    return LaneOutcome("found", f"algebraic model: {found.detail}"), built


def _check(
    goal: Goal,
    evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness,
    found: LaneOutcome,
    lane: str,
    search_ms: int,
    limits: EntailmentLimits,
    deadline: float,
    project: Path | None,
    cancel: threading.Event | None,
) -> tuple[LaneRecord, tuple[ValidatedEntailment, bytes] | None]:
    """Encode the evidence as an artifact and replay it; nothing else decides."""
    try:
        return _replay(
            goal, evidence, found, lane, search_ms, limits, deadline, project, cancel
        )
    except OSError as error:
        # The pins or the project could not be read: nothing was checked.
        record = LaneRecord(
            lane, "found", found.detail, search_ms, "unavailable", str(error)
        )
        return record, None


def _replay(
    goal: Goal,
    evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness,
    found: LaneOutcome,
    lane: str,
    search_ms: int,
    limits: EntailmentLimits,
    deadline: float,
    project: Path | None,
    cancel: threading.Event | None,
) -> tuple[LaneRecord, tuple[ValidatedEntailment, bytes] | None]:
    """Build the artifact and replay it, recording the check."""
    application = render.application(goal, evidence)
    artifact = build_artifact(
        goal,
        evidence,
        application,
        limits,
        {"lane": lane, "backend": "z3", "search_ms": search_ms},
    )
    if len(artifact) > limits.max_artifact_bytes:
        record = LaneRecord(
            lane, "resource-limit", "the artifact exceeds the byte limit", search_ms
        )
        return record, None
    started = time.monotonic()
    outcome = replay_artifact(
        artifact, goal, limits, project, deadline=deadline, cancel=cancel
    )
    check_ms = _elapsed(started)
    if isinstance(outcome, ValidatedEntailment):
        record = LaneRecord(
            lane, "found", found.detail, search_ms, "validated", "", check_ms
        )
        return record, (outcome, artifact)
    if isinstance(outcome, InvalidEvidence):
        record = LaneRecord(
            lane, "found", found.detail, search_ms, "invalid", outcome.reason, check_ms
        )
        return record, None
    assert isinstance(outcome, ReplayUnavailable)
    stopped = {
        "timeout": "timeout",
        "cancelled": "cancelled",
        "output": "resource-limit",
        "heartbeats": "resource-limit",
    }
    record = LaneRecord(
        lane,
        stopped.get(outcome.stop, "found"),
        found.detail,
        search_ms,
        "unavailable",
        outcome.reason,
        check_ms,
    )
    return record, None


def _reason(lanes: list[LaneRecord]) -> UnknownReason:
    """Report the first reason in precedence among the lanes' own."""
    seen = set()
    for record in lanes:
        if record.check == "invalid":
            seen.add(UnknownReason.INVALID_EVIDENCE)
        elif record.check == "unavailable" and record.outcome == "found":
            seen.add(UnknownReason.LEAN_UNAVAILABLE)
        elif record.outcome == "none":
            if record.lane == "certificate":
                seen.add(UnknownReason.DICTIONARY_EXHAUSTED)
        elif record.outcome in {r.value for r in UnknownReason}:
            seen.add(UnknownReason(record.outcome))
    for reason in _PRECEDENCE:
        if reason in seen:
            return reason
    # Only a refutation lane that found no counterexample, with the
    # certificate lane not reached: nothing supports a positive answer.
    return UnknownReason.UNSUPPORTED_FRAGMENT


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
