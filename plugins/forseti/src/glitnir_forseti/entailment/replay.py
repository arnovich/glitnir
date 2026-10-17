"""Replay an entailment artifact against an independently supplied goal.

This is the one path by which evidence about a goal becomes a result. It
imports no solver and runs nothing the artifact supplies:

1. The artifact is read under a byte limit and decoded strictly.
2. Its goal must equal the *expected* goal, which the caller supplies
   independently: context names and order, formulas, coefficients.
3. Its evidence must have the shape the expected goal's constraint lists
   require, and its kind must match its claimed status.
4. Its environment metadata must equal this installation's pinned identity; an
   artifact cannot nominate a different library.
5. The caller-selected Lean project is verified against the release pin, and
   the whole import closure of the evidence's checker module against the
   replay pin.
6. The checker application is regenerated from the expected goal and the
   validated evidence. The retained source is documentary, and must match.
7. `check_independently` compiles the regenerated unit in a fresh sandboxed
   process, replays it through the kernel and collects its axioms from the
   compiled module; the axioms must lie in the allowlist.

Validation returns `ValidatedEntailment` or `InvalidEvidence`. When the check
could not run (no Lean, a mismatched environment, a limit, cancellation) it
returns `ReplayUnavailable`, which says nothing about the evidence.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glitnir.lean.environment import LEAN_AXIOM_ALLOWLIST, MAX_TIMEOUT_MS

from glitnir_forseti.lean.closure import verify_closure
from glitnir_forseti.lean.independent import IndependentStatus, check_independently
from glitnir_forseti.lean.session import SessionLimits

from . import render
from .algebraic import AlgebraicWitness, decode_algebraic
from .artifact import ARTIFACT_KEYS, ARTIFACT_SCHEMA, THEOREM, environment_identity
from .evidence import (
    Certificate,
    EvidenceError,
    PsatzCertificate,
    Witness,
    check_certificate,
    check_psatz,
    check_witness,
    decode_evidence,
    nonstrict,
)
from .limits import EntailmentLimits
from .syntax import Goal, GoalError, check_goal_limits, decode_goal


@dataclass(frozen=True, slots=True)
class ValidatedEntailment:
    """Lean proved the expected goal's entailment or its negation.

    Attributes:
        status: ``PROVEN`` or ``REFUTED``.
        goal: The expected goal, as supplied by the caller.
        evidence: The evidence Lean checked.
        axioms: The axioms the checked theorem depends on.
    """

    status: str
    goal: Goal
    evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness
    axioms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InvalidEvidence:
    """The artifact does not establish anything about the expected goal."""

    reason: str


@dataclass(frozen=True, slots=True)
class ReplayUnavailable:
    """The check could not run; nothing is known about the evidence.

    Attributes:
        reason: What stopped it.
        stop: ``timeout``, ``cancelled``, ``output`` or empty.
    """

    reason: str
    stop: str = ""


ReplayOutcome = ValidatedEntailment | InvalidEvidence | ReplayUnavailable


def replay_artifact(
    artifact: bytes,
    expected: Goal,
    limits: EntailmentLimits | None = None,
    project: Path | None = None,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> ReplayOutcome:
    """Check an artifact about ``expected`` in the caller's pinned environment.

    Args:
        artifact: The encoded artifact, untrusted.
        expected: The goal the caller wants decided, supplied independently.
        limits: Replay limits; the defaults when None. The artifact's own
            retained limits are not used.
        project: The trusted Lean project; ``FORSETI_LEAN_PROJECT`` when None.
        deadline: A ``time.monotonic()`` instant the whole replay must meet;
            ``limits.wall_time_ms`` from now when None.
        cancel: Set to stop the replay.

    Returns:
        The validated entailment, the reason the evidence is invalid, or why
        the check could not run.
    """
    limits = limits or EntailmentLimits()
    if deadline is None:
        deadline = time.monotonic() + limits.wall_time_ms / 1000
    decided = regenerate_application(artifact, expected, limits)
    if not isinstance(decided, tuple):
        return decided
    status, evidence, application = decided
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        return ReplayUnavailable("no time left to check", "timeout")
    session = SessionLimits(
        timeout_ms=min(remaining_ms, MAX_TIMEOUT_MS),
        max_heartbeats=limits.max_heartbeats,
        max_output_bytes=limits.max_lean_output_bytes,
    )
    environment = verify_closure(
        render.imports_for(evidence), session, project, deadline=deadline, cancel=cancel
    )
    if environment.status is not IndependentStatus.CONFIRMED:
        return ReplayUnavailable(environment.reason, environment.stop)
    check = check_independently(
        application.prelude,
        application.statement,
        application.script,
        session,
        project,
        deadline=deadline,
        cancel=cancel,
    )
    if check.status is IndependentStatus.UNKNOWN:
        return ReplayUnavailable(check.reason, check.stop)
    if check.status is IndependentStatus.REJECTED:
        return InvalidEvidence(f"Lean rejected the evidence: {check.reason}")
    forbidden = sorted(set(check.axioms) - set(LEAN_AXIOM_ALLOWLIST))
    if forbidden:
        return InvalidEvidence(f"the theorem depends on {', '.join(forbidden)}")
    return ValidatedEntailment(status, expected, evidence, check.axioms)


def regenerate_application(
    artifact: bytes, expected: Goal, limits: EntailmentLimits | None = None
) -> (
    tuple[
        str,
        Certificate | PsatzCertificate | Witness | AlgebraicWitness,
        render.Application,
    ]
    | InvalidEvidence
    | ReplayUnavailable
):
    """Bind an artifact to ``expected`` and regenerate its checker application.

    This is the part of `replay_artifact` that runs no Lean, so the status
    it returns is only what the artifact claims, never a decision. The
    application is what a replay would check; showing it grants nothing.

    Returns:
        The artifact's status, its evidence and the regenerated application,
        or why the artifact does not bind to ``expected``.
    """
    limits = limits or EntailmentLimits()
    try:
        check_goal_limits(expected, limits)
    except GoalError as error:
        return ReplayUnavailable(f"the expected goal exceeds a limit: {error}")
    decoded = _decode(artifact, expected, limits)
    if isinstance(decoded, InvalidEvidence):
        return decoded
    status, evidence, source = decoded
    application = render.application(expected, evidence)
    if source != application.source:
        return InvalidEvidence("the retained source does not match regeneration")
    return status, evidence, application


def _decode(artifact: bytes, expected: Goal, limits: EntailmentLimits) -> (
    tuple[
        str,
        Certificate | PsatzCertificate | Witness | AlgebraicWitness,
        str,
    ]
    | InvalidEvidence
):
    """Read and bind the artifact to the expected goal, or say why not."""
    if type(artifact) is not bytes:
        return InvalidEvidence("an artifact is bytes")
    if len(artifact) > limits.max_artifact_bytes:
        return InvalidEvidence("the artifact exceeds the byte limit")
    try:
        document: Any = json.loads(artifact)
    except (ValueError, RecursionError):
        return InvalidEvidence("the artifact is not JSON")
    if type(document) is not dict or set(document) != ARTIFACT_KEYS:
        return InvalidEvidence("the artifact does not have exactly the v1 fields")
    if document["schema"] != ARTIFACT_SCHEMA:
        return InvalidEvidence("unsupported artifact schema")
    try:
        goal = decode_goal(document["goal"], limits)
    except (GoalError, RecursionError, ValueError) as error:
        return InvalidEvidence(f"the artifact's goal is malformed: {error}")
    if goal != expected:
        return InvalidEvidence("the artifact is about a different goal")
    raw = document["evidence"]
    evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness
    try:
        if type(raw) is dict and raw.get("kind") == "algebraic-witness":
            evidence = decode_algebraic(raw, expected.context, limits)
        else:
            evidence = decode_evidence(raw, expected.context, limits)
    except (EvidenceError, RecursionError, ValueError) as error:
        return InvalidEvidence(f"the evidence is malformed: {error}")
    status = document["status"]
    if type(status) is not str:
        return InvalidEvidence("the status is not text")
    expected_kinds = {
        "PROVEN": (Certificate, PsatzCertificate),
        "REFUTED": (Witness, AlgebraicWitness),
    }.get(status)
    if expected_kinds is None or type(evidence) not in expected_kinds:
        return InvalidEvidence("the status does not match the kind of evidence")
    theorem = {
        "name": THEOREM,
        "statement": "entailment" if status == "PROVEN" else "negation",
    }
    if document["theorem"] != theorem:
        return InvalidEvidence("the artifact names a different theorem")
    try:
        if isinstance(evidence, (Certificate, PsatzCertificate)):
            antecedent = nonstrict(expected.antecedent)
            consequent = nonstrict(expected.consequent)
            if antecedent is None or consequent is None:
                return InvalidEvidence("the goal is outside the certificate fragment")
            if isinstance(evidence, PsatzCertificate):
                check_psatz(evidence, antecedent, consequent, limits)
            else:
                check_certificate(evidence, antecedent, consequent, limits)
        elif isinstance(evidence, Witness):
            check_witness(evidence, expected.context, limits)
    except EvidenceError as error:
        return InvalidEvidence(f"the evidence has the wrong shape: {error}")
    if document["environment"] != environment_identity(render.imports_for(evidence)):
        return InvalidEvidence(
            "the artifact names a different Lean environment than the pinned one"
        )
    if type(document["source"]) is not str:
        return InvalidEvidence("the retained source is not text")
    return status, evidence, document["source"]
