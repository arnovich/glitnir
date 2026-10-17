"""Artifacts for trajectory root goals, and their replay.

``gimle.forseti.trajectory-artifact/v1`` retains the complete root goal, the
exact evidence (a leaf certificate, or the initial-state witness), the root
theorem's identity, the Lean lemmas the proof cites, the documentary source,
the environment and the effective limits. `replay_root` believes none of it:

1. its goal must equal the caller's independently supplied goal;
2. the goal must name a supported model, interpretation, obligation,
   quantifier and observation, or nothing can be checked;
3. its evidence must have the right shape, and a witness must be the goal's
   own start time and initial state, so it violates *this* root;
4. its environment metadata must equal the pinned identity of the model's
   import set, and that closure is verified in the caller's project;
5. the root application is regenerated from the goal, the model entry and the
   evidence; the retained source must match;
6. the independent check compiles and kernel-replays it and collects its
   axioms, which must lie in the allowlist.

A proved leaf is never a proved root: only the rendered root theorem, about
the goal's own circuit, time, initial state and observation, is checked.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from glitnir.lean.environment import LEAN_AXIOM_ALLOWLIST, MAX_TIMEOUT_MS

from glitnir_forseti.entailment.evidence import (
    Certificate,
    EvidenceError,
    check_certificate,
    decode_evidence,
    encode_certificate,
    nonstrict,
)
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import (
    InvalidEvidence,
    ReplayUnavailable,
)
from glitnir_forseti.entailment.syntax import (
    GoalError,
    decode_rational,
    encode_rational,
)
from glitnir_forseti.lean.closure import closure_identity, verify_closure
from glitnir_forseti.lean.independent import IndependentStatus, check_independently
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.lean_release import (
    IMPORT_CLOSURE,
    LOCKFILE_SHA256,
    RELEASE_SHA256,
    SUPPORT_VERSION,
    TOOLCHAIN,
)

from . import render
from .goal import (
    MAX_STATES,
    TrajectoryGoal,
    check_goal_limits,
    decode_goal,
    encode_goal,
)
from .models import MODELS, ModelEntry, ObservationBand

ARTIFACT_SCHEMA = "gimle.forseti.trajectory-artifact/v1"
THEOREM = "forsetiSearch"
_KEYS = frozenset(
    {
        "schema",
        "status",
        "goal",
        "evidence",
        "theorem",
        "dependencies",
        "source",
        "environment",
        "limits",
        "producer",
    }
)


@dataclass(frozen=True, slots=True)
class InitialWitness:
    """The admitted declared input, read at the start time.

    Attributes:
        time: The start time, where the violation is read.
        initial: The initial state, by port ID, as the goal gives it.
        value: The observation's exact value there.
    """

    time: Fraction
    initial: tuple[tuple[str, Fraction], ...]
    value: Fraction


@dataclass(frozen=True, slots=True)
class ValidatedRoot:
    """Lean proved the expected root goal or its negation.

    Attributes:
        status: ``PROVEN`` or ``REFUTED``.
        goal: The expected goal, as the caller supplied it.
        evidence: The evidence Lean checked.
        axioms: The root theorem's axioms.
        dependencies: The Lean lemmas the proof cites.
    """

    status: str
    goal: TrajectoryGoal
    evidence: Certificate | InitialWitness
    axioms: tuple[str, ...]
    dependencies: tuple[str, ...]


RootOutcome = ValidatedRoot | InvalidEvidence | ReplayUnavailable


def supported(goal: TrajectoryGoal) -> tuple[ModelEntry, ObservationBand] | str:
    """Find the model entry and observation for a goal, or say why not."""
    entry = MODELS.get(goal.model)
    if entry is None:
        return f"no Lean-compiled model named {goal.model!r}"
    if goal.interpretation != "continuous-real":
        return f"the {goal.interpretation} interpretation is not supported"
    if goal.obligation != "contract":
        return (
            "only well-posed contracts are supported; partial correctness would "
            "hold vacuously of a circuit with no trajectory"
        )
    if goal.quantifier != "always":
        return f"the {goal.quantifier} quantifier is not supported"
    band = entry.observations.get(goal.observation)
    if band is None:
        return f"no contract is known about output {goal.observation!r}"
    if goal.name != band.name:
        return f"output {goal.observation!r} is named {band.name!r} here"
    refused = _driver_refusal(goal, entry)
    if refused is not None:
        return refused
    if {i for i, _ in goal.initial} != set(entry.initial_ids):
        return "the goal must give exactly the model's initial ports"
    # The cited theorems are about the declared axis, start and state; for any
    # other, no template applies (and Lean would refuse one).
    if goal.axis != entry.axis:
        return f"the model evolves along {entry.axis!r}, not {goal.axis!r}"
    if goal.start != entry.start:
        return f"the known contracts start at t = {entry.start}, not {goal.start}"
    declared = dict(zip(entry.initial_ids, entry.declared_initial))
    if dict(goal.initial) != declared:
        return "the known contracts are about the declared initial state only"
    return entry, band


def _driver_refusal(goal: TrajectoryGoal, entry: ModelEntry) -> str | None:
    """Say why the goal's driver precondition is not the model's, if it is not.

    A driven model's contract holds only under its drivers' precondition, so a
    goal that omits it asks for a claim no template proves: without it, the
    root's precondition would be ``True``.
    """
    drivers = entry.drivers
    if drivers is None:
        if goal.drivers:
            return f"{entry.key} has no drivers to bound"
        return None
    if not goal.drivers:
        return (
            f"{entry.key} is driven: the goal must state its driver precondition,"
            f" a bound on {drivers.bounded!r} (at most {drivers.bound})"
        )
    ports = [port for port, _ in goal.drivers]
    if ports != [drivers.bounded]:
        return f"the known contract bounds exactly the driver {drivers.bounded!r}"
    ((_, bound),) = goal.drivers
    if bound > drivers.bound:
        return (
            f"the known contract assumes |{drivers.bounded}| <= {drivers.bound},"
            f" not {bound}"
        )
    return None


def environment_identity(entry: ModelEntry) -> dict[str, Any]:
    """Describe the pinned environment a model's root theorems are checked in."""
    return {
        "toolchain": TOOLCHAIN,
        "imports": list(entry.imports),
        "support_version": SUPPORT_VERSION,
        "release_sha256": RELEASE_SHA256,
        "lockfile_sha256": LOCKFILE_SHA256,
        "closure_sha256": closure_identity(entry.imports),
        "dependencies": [
            {"name": name, "revision": revision} for name, revision in IMPORT_CLOSURE
        ],
    }


def application(
    goal: TrajectoryGoal,
    entry: ModelEntry,
    band: ObservationBand,
    evidence: Certificate | InitialWitness,
) -> render.RootApplication:
    """Regenerate the root application from the goal and the evidence."""
    if isinstance(evidence, Certificate):
        return render.prove(goal, entry, band, evidence)
    return render.refute(goal, entry, band, evidence.value)


def build_artifact(
    goal: TrajectoryGoal,
    evidence: Certificate | InitialWitness,
    limits: EntailmentLimits,
    producer: dict[str, Any],
) -> bytes:
    """Encode an artifact for evidence about to be replayed.

    Raises:
        ValueError: If the goal is unsupported.
    """
    found = supported(goal)
    if isinstance(found, str):
        raise ValueError(found)
    entry, band = found
    unit = application(goal, entry, band, evidence)
    proven = isinstance(evidence, Certificate)
    document = {
        "schema": ARTIFACT_SCHEMA,
        "status": "PROVEN" if proven else "REFUTED",
        "goal": encode_goal(goal),
        "evidence": _encode_evidence(evidence),
        "theorem": {"name": THEOREM, "statement": "root" if proven else "negation"},
        "dependencies": list(unit.dependencies),
        "source": unit.source,
        "environment": environment_identity(entry),
        "limits": limits.as_dict(),
        "producer": producer,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _encode_evidence(evidence: Certificate | InitialWitness) -> dict[str, Any]:
    if isinstance(evidence, Certificate):
        return {"kind": "leaf-certificate", "certificate": encode_certificate(evidence)}
    return {
        "kind": "initial-witness",
        "time": encode_rational(evidence.time),
        "initial": [[i, encode_rational(v)] for i, v in evidence.initial],
        "value": encode_rational(evidence.value),
    }


def replay_root(
    artifact: bytes,
    expected: TrajectoryGoal,
    limits: EntailmentLimits | None = None,
    project: Path | None = None,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> RootOutcome:
    """Check an artifact about ``expected`` in the caller's pinned environment.

    Args:
        artifact: The encoded artifact, untrusted.
        expected: The root goal the caller wants decided, supplied independently.
        limits: Replay limits; the defaults when None.
        project: The trusted Lean project; ``FORSETI_LEAN_PROJECT`` when None.
        deadline: A ``time.monotonic()`` instant the replay must meet.
        cancel: Set to stop the replay.

    Returns:
        The validated root, why the evidence is invalid, or why nothing could
        be checked.
    """
    limits = limits or EntailmentLimits()
    if deadline is None:
        deadline = time.monotonic() + limits.wall_time_ms / 1000
    decided = regenerate_application(artifact, expected, limits)
    if not isinstance(decided, tuple):
        return decided
    status, evidence, unit = decided
    entry = MODELS[expected.model]
    checked = check_root(unit, entry.imports, limits, project, deadline, cancel)
    if not isinstance(checked, tuple):
        return checked
    return ValidatedRoot(status, expected, evidence, checked, unit.dependencies)


def regenerate_application(
    artifact: bytes,
    expected: TrajectoryGoal,
    limits: EntailmentLimits | None = None,
) -> (
    tuple[str, Certificate | InitialWitness, render.RootApplication]
    | InvalidEvidence
    | ReplayUnavailable
):
    """Bind an artifact to ``expected`` and regenerate its root application.

    This is the part of `replay_root` that runs no Lean, so the status
    it returns is only what the artifact claims, never a decision. The
    application is what a replay would check; showing it grants nothing.
    """
    limits = limits or EntailmentLimits()
    try:
        check_goal_limits(expected, limits)
    except GoalError as error:
        return ReplayUnavailable(f"the expected goal exceeds a limit: {error}")
    found = supported(expected)
    if isinstance(found, str):
        return ReplayUnavailable(f"unsupported goal: {found}")
    entry, band = found
    decoded = _decode(artifact, expected, entry, band, limits)
    if isinstance(decoded, InvalidEvidence):
        return decoded
    status, evidence, source = decoded
    unit = application(expected, entry, band, evidence)
    if source != unit.source:
        return InvalidEvidence("the retained source does not match regeneration")
    return status, evidence, unit


def _decode(
    artifact: bytes,
    expected: TrajectoryGoal,
    entry: ModelEntry,
    band: ObservationBand,
    limits: EntailmentLimits,
) -> tuple[str, Certificate | InitialWitness, str] | InvalidEvidence:
    """Read and bind the artifact to the expected goal, or say why not."""
    if type(artifact) is not bytes or len(artifact) > limits.max_artifact_bytes:
        return InvalidEvidence("the artifact is not bytes within the byte limit")
    try:
        document: Any = json.loads(artifact)
    except (ValueError, RecursionError):
        return InvalidEvidence("the artifact is not JSON")
    if type(document) is not dict or set(document) != _KEYS:
        return InvalidEvidence("the artifact does not have exactly the v1 fields")
    if document["schema"] != ARTIFACT_SCHEMA:
        return InvalidEvidence("unsupported artifact schema")
    try:
        goal = decode_goal(document["goal"], limits)
    except (GoalError, RecursionError, ValueError) as error:
        return InvalidEvidence(f"the artifact's goal is malformed: {error}")
    if goal != expected:
        return InvalidEvidence("the artifact is about a different root goal")
    status = document["status"]
    if type(status) is not str or status not in ("PROVEN", "REFUTED"):
        return InvalidEvidence("the status is neither PROVEN nor REFUTED")
    theorem = {
        "name": THEOREM,
        "statement": "root" if status == "PROVEN" else "negation",
    }
    if document["theorem"] != theorem:
        return InvalidEvidence("the artifact names a different theorem")
    try:
        evidence = _decode_evidence(document["evidence"], expected, band, limits)
    except (EvidenceError, GoalError, RecursionError, ValueError) as error:
        return InvalidEvidence(f"the evidence is malformed: {error}")
    if isinstance(evidence, Certificate) != (status == "PROVEN"):
        return InvalidEvidence("the status does not match the kind of evidence")
    if isinstance(evidence, InitialWitness) and (
        evidence.time != expected.start or evidence.initial != expected.initial
    ):
        return InvalidEvidence("the witness is not about this root's start state")
    if document["environment"] != environment_identity(entry):
        return InvalidEvidence("the artifact names a different Lean environment")
    if type(document["source"]) is not str:
        return InvalidEvidence("the retained source is not text")
    return status, evidence, document["source"]


def _decode_evidence(
    data: Any,
    goal: TrajectoryGoal,
    band: ObservationBand,
    limits: EntailmentLimits,
) -> Certificate | InitialWitness:
    """Read retained evidence, checking its shape against the goal."""
    if type(data) is not dict:
        raise EvidenceError("evidence is an object")
    if data.get("kind") == "leaf-certificate" and set(data) == {"kind", "certificate"}:
        certificate = decode_evidence(data["certificate"], goal.context, limits)
        if not isinstance(certificate, Certificate):
            raise EvidenceError("a leaf certificate is a certificate")
        antecedent = nonstrict(band.invariant)
        consequent = nonstrict(goal.post)
        if antecedent is None or consequent is None:
            raise EvidenceError("the postcondition is outside the certificate fragment")
        check_certificate(certificate, antecedent, consequent, limits)
        return certificate
    if data.get("kind") == "initial-witness" and set(data) == {
        "kind",
        "time",
        "initial",
        "value",
    }:
        entries = data["initial"]
        if type(entries) is not list or len(entries) > MAX_STATES:
            raise EvidenceError("the witness's initial state is a bounded list")
        initial = []
        for entry in entries:
            if type(entry) is not list or len(entry) != 2 or type(entry[0]) is not str:
                raise EvidenceError("an initial entry is [port ID, rational]")
            initial.append((entry[0], decode_rational(entry[1], limits)))
        return InitialWitness(
            decode_rational(data["time"], limits),
            tuple(initial),
            decode_rational(data["value"], limits),
        )
    raise EvidenceError("evidence is a leaf certificate or an initial witness")


def check_root(
    unit: render.RootApplication,
    imports: tuple[str, ...],
    limits: EntailmentLimits,
    project: Path | None,
    deadline: float,
    cancel: threading.Event | None,
) -> tuple[str, ...] | InvalidEvidence | ReplayUnavailable:
    """Verify the import set's closure, then check a regenerated root unit.

    Shared by every root lane: the environment is verified against the replay
    pin, the unit compiled and kernel-replayed in a fresh sandboxed process,
    and its axioms held to the allowlist.

    Returns:
        The root theorem's axioms, why Lean rejected it, or why nothing could
        be checked.
    """
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        return ReplayUnavailable("no time left to check", "timeout")
    session = SessionLimits(
        timeout_ms=min(remaining_ms, MAX_TIMEOUT_MS),
        max_heartbeats=limits.max_heartbeats,
        max_output_bytes=limits.max_lean_output_bytes,
    )
    environment = verify_closure(
        imports, session, project, deadline=deadline, cancel=cancel
    )
    if environment.status is not IndependentStatus.CONFIRMED:
        return ReplayUnavailable(environment.reason, environment.stop)
    check = check_independently(
        unit.prelude,
        unit.statement,
        unit.script,
        session,
        project,
        deadline=deadline,
        cancel=cancel,
    )
    if check.status is IndependentStatus.UNKNOWN:
        return ReplayUnavailable(check.reason, check.stop)
    if check.status is IndependentStatus.REJECTED:
        return InvalidEvidence(f"Lean rejected the root proof: {check.reason}")
    forbidden = sorted(set(check.axioms) - set(LEAN_AXIOM_ALLOWLIST))
    if forbidden:
        return InvalidEvidence(f"the root theorem depends on {', '.join(forbidden)}")
    return check.axioms
