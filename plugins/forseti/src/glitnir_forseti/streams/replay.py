"""Artifacts for stream and field root goals, and their replay.

``gimle.forseti.stream-artifact/v2`` retains the root goal, the exact evidence
(none for the full-stream identity, a leaf certificate, a witness, the
radius ``ρ`` of a heat truncation proof, or ``ρ`` and the inverse's radii
``r`` of a front truncation proof), the
theorem's identity, the lemmas it cites, the documentary source, the
environment and the limits. `replay_stream` follows the trajectory replay:
the goal must equal the caller's, be supported, and the evidence fit it; the
environment must be the pinned one for the claim's import set; the root is
regenerated, the retained source must match, and `check_root` verifies the
closure and checks the unit independently.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from glitnir_forseti.entailment.evidence import (
    Certificate,
    EvidenceError,
    Witness,
    check_certificate,
    decode_evidence,
    encode_certificate,
    nonstrict,
)
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import InvalidEvidence, ReplayUnavailable
from glitnir_forseti.entailment.syntax import (
    Context,
    GoalError,
    decode_rational,
    encode_poly,
    encode_rational,
)
from glitnir_forseti.lean.closure import closure_identity
from glitnir_forseti.lean_release import (
    IMPORT_CLOSURE,
    LOCKFILE_SHA256,
    RELEASE_SHA256,
    SUPPORT_VERSION,
    TOOLCHAIN,
)
from glitnir_forseti.trajectory.render import RootApplication
from glitnir_forseti.trajectory.replay import check_root

from . import mild, mild_render, render
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
    decode_goal,
    encode_goal,
)
from .models import MODELS, PROFILE, StreamModel
from .tail import (
    BAND_WINDOW,
    FrontEvidence,
    TailEvidence,
    WindowEvidence,
    constant_term,
)
from .trig import (
    MAX_BAND_WINDOW,
    BandEvidence,
    RadiusEvidence,
    classical_start_reason,
    largest_size,
    weight,
)

ARTIFACT_SCHEMA = "gimle.forseti.stream-artifact/v2"
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
class ContractOnly:
    """No evidence: the root is the model's contract itself."""


Evidence = (
    ContractOnly
    | Certificate
    | Witness
    | TailEvidence
    | FrontEvidence
    | WindowEvidence
    | RadiusEvidence
    | BandEvidence
    | mild.MildEvidence
)


@dataclass(frozen=True, slots=True)
class ValidatedStream:
    """Lean proved the expected root goal or its negation."""

    status: str
    goal: StreamGoal
    evidence: Evidence
    axioms: tuple[str, ...]
    dependencies: tuple[str, ...]


StreamOutcome = ValidatedStream | InvalidEvidence | ReplayUnavailable


def supported(goal: StreamGoal) -> StreamModel | str:
    """Find the model for a goal, or say why it is unsupported."""
    model = MODELS.get(goal.model)
    if model is None:
        return f"no stream model named {goal.model!r}"
    if goal.interpretation == "stochastic-ito":
        return "stochastic readings of the stream circuit are not supported"
    claim = goal.claim
    if (
        type(claim) is MildTruncationClaim or type(claim) is MildBandClaim
    ) or goal.interpretation in (
        "mild-truncation",
        "mild-band",
    ):
        return _supported_mild(goal, model)
    if goal.basis == "interaction":
        return "the interaction basis is reserved for mild readings"
    if (
        model.trig is not None
        or isinstance(goal.profile, Modes)
        or type(claim) in (TrigTruncationClaim, TrigBandClaim)
        or goal.interpretation in ("trig-truncation", "trig-band")
    ):
        return _supported_trig(goal, model)
    if goal.interpretation == "analytic-field" or (
        type(claim) is FieldClaim and isinstance(goal.profile, ExpSum)
    ):
        if goal.interpretation != "analytic-field":
            return (
                "a field claim about an exponential-sum profile is read as "
                "analytic-field"
            )
        if type(claim) is not FieldClaim:
            return "an analytic-field goal is a field claim"
        if not isinstance(goal.profile, ExpSum):
            return (
                "analytic fields are bounded for exponential-sum profiles only; a "
                "polynomial profile's field is read as evaluated-field"
            )
        return _supported_band(goal, model, claim, goal.profile)
    if (
        goal.interpretation == "analytic-tail"
        or type(claim) is TruncationClaim
        or isinstance(goal.profile, ExpSum)
    ):
        if goal.interpretation != "analytic-tail":
            return "a truncation claim is read as analytic-tail"
        if type(claim) is not TruncationClaim:
            return "an analytic-tail goal is a truncation claim"
        if not isinstance(goal.profile, ExpSum):
            return (
                "analytic tails are certified for exponential-sum profiles only; "
                "a polynomial profile has no tail"
            )
        return _supported_tail(goal, model, claim.port, goal.profile)
    if goal.viscosity is not None:
        return "only a viscous model's truncation claim names a viscosity"
    if model.full_stream is None or model.coefficients is None or model.field is None:
        return f"{model.key} supports truncation claims only"
    if encode_poly(goal.profile) != encode_poly(PROFILE):
        return "the known theorems are about the declared profile x² only"
    field = type(claim) is FieldClaim
    wanted = "evaluated-field" if field else "formal-stream"
    if goal.interpretation != wanted:
        return (
            f"a {type(claim).__name__} is read as {wanted}, not {goal.interpretation}"
        )
    if type(claim) is FullStreamClaim:
        if goal.basis != "ogf":
            return "the full-stream contract is about the OGF circuit"
        expected = [(port, encode_poly(p)) for port, p in model.full_stream.outputs]
        if [(port, encode_poly(p)) for port, p in claim.outputs] != expected:
            return "the only full-stream identity known is [u_xx, u] = [2, x² + 2t]"
    elif type(claim) is CoefficientClaim:
        if goal.basis != "ogf":
            return "the coefficient observation is of the OGF circuit"
        if claim.observed != model.coefficients.observed:
            return (
                "coefficient claims are about the observed window "
                f"{list(model.coefficients.observed)} only"
            )
    elif type(claim) is FieldClaim:
        if claim.port != "u":
            return "field claims are about the rebuilt output u"
        (_, _), (x0, x1) = claim.time, claim.space
        if x0 != -x1:
            return "the box lemma needs a symmetric x interval"
    return model


def _supported_tail(
    goal: StreamGoal, model: StreamModel, port: str, profile: ExpSum
) -> StreamModel | str:
    """Decide whether the model's truncation lane takes the goal's output ``port``."""
    tail = model.tail
    if tail is None:
        return f"{model.key} has no certified truncation"
    if port != "u":
        return "truncation claims are about the rebuilt output u"
    if goal.basis not in tail.bases:
        circuits = ", ".join(basis.upper() for basis in tail.bases)
        return f"the {model.key} truncation theorems are about the {circuits} circuit"
    if tail.viscous:
        if goal.viscosity is None:
            return f"a {model.key} goal names its viscosity"
        if goal.viscosity <= 0:
            return (
                "the certified front needs a positive viscosity; the inviscid "
                "equation has no Cole–Hopf potential"
            )
        if constant_term(profile) == 0:
            return (
                "the potential's constant term Σ cᵢ is zero, so the Cole–Hopf "
                "quotient −2ν φ_x/φ is not a front"
            )
    elif goal.viscosity is not None:
        return f"the {model.key} truncation theorems take no viscosity"
    return model


def _supported_band(
    goal: StreamGoal, model: StreamModel, claim: FieldClaim, profile: ExpSum
) -> StreamModel | str:
    """Decide whether the model's window lane takes the field band (Rule 7.33)."""
    found = _supported_tail(goal, model, claim.port, profile)
    if isinstance(found, str):
        return found.replace("truncation claims", "analytic-field claims")
    tail = model.tail
    assert tail is not None
    if not tail.viscous:
        return (
            "field bands from a checked window are certified for the Burgers front "
            f"only; the {model.key} exponential-sum model has no window root"
        )
    (t0, t1), (x0, x1) = claim.time, claim.space
    if t0 != -t1 or x0 != -x1:
        return (
            "the certified box is centred at the origin: both intervals are symmetric"
        )
    if band_of(claim.post) is None:
        return (
            "an analytic-field postcondition is a band: u >= lo, u <= hi, or both; "
            "polynomial postconditions in t, x and u are not bounded"
        )
    return model


def _supported_mild(goal: StreamGoal, model: StreamModel) -> StreamModel | str:
    """Admit physical mild claims only for even, mean-zero finite starts."""
    from .mild import admitted_start

    claim = goal.claim
    if model.key != "vorticity" or not isinstance(goal.profile, Modes):
        return "a mild goal names vorticity and a finite start of modes"
    if type(claim) is not MildTruncationClaim and type(claim) is not MildBandClaim:
        return "a mild claim is a forward-time truncation or band"
    wanted = "mild-truncation" if type(claim) is MildTruncationClaim else "mild-band"
    if goal.interpretation != wanted:
        return f"this mild claim is read as {wanted}"
    if type(claim) is MildTruncationClaim and claim.window == 0:
        return "a mild truncation window has at least one interaction term"
    if goal.basis != "interaction":
        return "mild claims require the interaction basis"
    if claim.port != "u":
        return "mild claims observe the vorticity u"
    if goal.viscosity is None or goal.viscosity < 0:
        return "a mild goal requires nonnegative viscosity"
    if not admitted_start(goal.profile):
        return "the start must be even and mean-zero"
    if type(claim) is MildBandClaim and band_of(claim.post) is None:
        return "a mild-band postcondition is a band on u"
    return model


def _supported_trig(goal: StreamGoal, model: StreamModel) -> StreamModel | str:
    """Decide whether the vorticity model's Euler-radius lanes take the goal."""
    trig = model.trig
    if trig is None:
        return (
            f"{model.key} has no trigonometric lane; a start of modes names vorticity"
        )
    claim = goal.claim
    if not isinstance(goal.profile, Modes):
        return (
            "a vorticity goal declares its start as modes: `start modes [k1, k2, c], …`"
        )
    if type(claim) is TrigTruncationClaim:
        wanted = "trig-truncation"
    elif type(claim) is TrigBandClaim:
        wanted = "trig-band"
    else:
        return (
            "a vorticity goal is a one-axis truncation `truncation u window N radius r "
            "epsilon ε` or a band `band u radius r : …`"
        )
    classical = goal.interpretation in CLASSICAL_EULER
    if goal.interpretation not in (wanted, wanted.replace("trig-", "euler-")):
        return (
            f"a {type(claim).__name__} is read as {wanted}, not {goal.interpretation}"
        )
    if claim.port != "u":
        return "vorticity claims are about the vorticity u"
    if goal.basis not in trig.bases:
        return "the Euler radius theorems are about the OGF stream"
    if type(claim) is TrigTruncationClaim and claim.window == 0:
        return "a truncation window has at least one degree"
    assert goal.viscosity is not None  # a start of modes names it (goal.py)
    if goal.viscosity < 0:
        return "the viscosity is not negative"
    if goal.viscosity > 0:
        return (
            "no radius is certified for positive viscosity: asgard-lean proves "
            "Gevrey-1 growth there, not convergence (Streams.Gevrey); the Euler "
            "equation is ν = 0"
        )
    if classical:
        reason = classical_start_reason(goal.profile)
        if reason is not None:
            return reason
    if largest_size(goal.profile) == 0 and not (
        classical and weight(goal.profile) == 0
    ):
        return "the start has only the zero mode; its stream is constant"
    if type(claim) is TrigBandClaim and band_of(claim.post) is None:
        return (
            "a trig-band postcondition is a band: u >= lo, u <= hi, or both; "
            "polynomial postconditions in t, x and u are not bounded"
        )
    return model


def imports_for(goal: StreamGoal, model: StreamModel) -> tuple[str, ...]:
    """Return the import set a goal's root is checked with."""
    if type(goal.claim) in (MildTruncationClaim, MildBandClaim):
        return mild_render.IMPORTS
    if type(goal.claim) in (TrigTruncationClaim, TrigBandClaim):
        assert model.trig is not None
        return (
            model.trig.classical_imports
            if goal.interpretation in CLASSICAL_EULER
            else model.trig.imports
        )
    if type(goal.claim) is FieldClaim and goal.interpretation == "analytic-field":
        assert model.tail is not None
        return model.tail.imports
    if type(goal.claim) is FieldClaim:
        return model.field_imports
    if type(goal.claim) is TruncationClaim:
        assert model.tail is not None
        return model.tail.imports
    if type(goal.claim) is FullStreamClaim:
        return model.full_stream_imports
    return model.formal_imports


def environment_identity(imports: tuple[str, ...]) -> dict[str, Any]:
    """Describe the pinned environment roots with these imports are checked in."""
    return {
        "toolchain": TOOLCHAIN,
        "imports": list(imports),
        "support_version": SUPPORT_VERSION,
        "release_sha256": RELEASE_SHA256,
        "lockfile_sha256": LOCKFILE_SHA256,
        "closure_sha256": closure_identity(imports),
        "dependencies": [
            {"name": name, "revision": revision} for name, revision in IMPORT_CLOSURE
        ],
    }


def application(
    goal: StreamGoal, model: StreamModel, evidence: Evidence
) -> RootApplication:
    """Regenerate the root application from the goal and the evidence.

    Raises:
        ValueError: If the evidence does not fit the claim.
    """
    claim = goal.claim
    if (
        type(claim) is MildTruncationClaim or type(claim) is MildBandClaim
    ) and isinstance(evidence, mild.MildEvidence):
        return mild_render.prove(goal, evidence)
    if type(claim) is FullStreamClaim and isinstance(evidence, ContractOnly):
        return render.full_stream(model, claim.outputs)
    if type(claim) is CoefficientClaim and isinstance(evidence, Certificate):
        return render.prove_coefficients(model, claim, evidence)
    if type(claim) is CoefficientClaim and isinstance(evidence, Witness):
        return render.refute_coefficients(model, claim, evidence)
    if type(claim) is FieldClaim and isinstance(evidence, Certificate):
        return render.prove_field(goal, model, claim, evidence)
    if type(claim) is FieldClaim and isinstance(evidence, Witness):
        return render.refute_field(goal, model, claim, evidence)
    if type(claim) is TruncationClaim and isinstance(evidence, TailEvidence):
        return render.prove_truncation(goal, evidence.rho)
    if type(claim) is TruncationClaim and isinstance(evidence, FrontEvidence):
        return render.prove_front(goal, evidence.rho, evidence.radii)
    if type(claim) is FieldClaim and isinstance(evidence, WindowEvidence):
        if evidence.refutes:
            return render.refute_band(goal, evidence)
        return render.prove_band(goal, evidence)
    if type(claim) is TrigTruncationClaim and isinstance(evidence, RadiusEvidence):
        return render.prove_trig_truncation(goal, evidence)
    if type(claim) is TrigBandClaim and isinstance(evidence, BandEvidence):
        return render.prove_trig_band(goal, evidence)
    raise ValueError("the evidence does not fit the claim")


def refutes(evidence: Evidence) -> bool:
    """Whether evidence is of the root's negation: a witness, or a window's point."""
    return isinstance(evidence, Witness) or (
        isinstance(evidence, WindowEvidence) and evidence.refutes
    )


def build_artifact(
    goal: StreamGoal,
    evidence: Evidence,
    limits: EntailmentLimits,
    producer: dict[str, Any],
) -> bytes:
    """Encode an artifact for evidence about to be replayed.

    Raises:
        ValueError: If the goal is unsupported or the evidence does not fit.
    """
    model = supported(goal)
    if isinstance(model, str):
        raise ValueError(model)
    unit = application(goal, model, evidence)
    refuted = refutes(evidence)
    document = {
        "schema": ARTIFACT_SCHEMA,
        "status": "REFUTED" if refuted else "PROVEN",
        "goal": encode_goal(goal),
        "evidence": _encode_evidence(evidence, goal, model),
        "theorem": {"name": THEOREM, "statement": "negation" if refuted else "root"},
        "dependencies": list(unit.dependencies),
        "source": unit.source,
        "environment": environment_identity(imports_for(goal, model)),
        "limits": limits.as_dict(),
        "producer": producer,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def admitted_input(goal: StreamGoal, model: StreamModel) -> str:
    """Name the admitted full input a refutation runs through, as Lean has it.

    Retained with a witness so the artifact records the whole input the
    refutation used, not only the observed values; replay checks it is the
    one the regenerated proof cites.
    """
    if type(goal.claim) is FieldClaim:
        assert model.field is not None
        return (
            f"{model.field.admitted} .{goal.basis} (_root_.Polynomial.X ^ 2) 0: "
            "[the heat solution, its boundary profile, 0]"
        )
    return "![" + ", ".join(model.admitted_input) + "]"


def _encode_evidence(
    evidence: Evidence, goal: StreamGoal, model: StreamModel
) -> dict[str, Any]:
    if isinstance(evidence, mild.MildEvidence):
        encoded_mild = {
            "kind": evidence.kind,
            "size": evidence.size,
            "weight": encode_rational(evidence.weight),
            "window": evidence.window,
        }
        if evidence.radius is not None:
            encoded_mild["radius"] = encode_rational(evidence.radius)
        return encoded_mild
    if isinstance(evidence, ContractOnly):
        return {"kind": "contract"}
    if isinstance(evidence, Certificate):
        return {"kind": "leaf-certificate", "certificate": encode_certificate(evidence)}
    if isinstance(evidence, TailEvidence):
        return {"kind": "tail-radius", "rho": encode_rational(evidence.rho)}
    if isinstance(evidence, RadiusEvidence):
        return {
            "kind": "euler-radius",
            "size": evidence.size,
            "weight": encode_rational(evidence.weight),
        }
    if isinstance(evidence, BandEvidence):
        return {
            "kind": "euler-band",
            "size": evidence.size,
            "weight": encode_rational(evidence.weight),
            "window": evidence.window,
            "norms": [encode_rational(value) for value in evidence.norms],
        }
    if isinstance(evidence, FrontEvidence):
        return {
            "kind": "front-radii",
            "rho": encode_rational(evidence.rho),
            "r": {
                "t": encode_rational(evidence.radii[0]),
                "x": encode_rational(evidence.radii[1]),
            },
        }
    if isinstance(evidence, WindowEvidence):
        encoded: dict[str, Any] = {
            "kind": "window",
            "rho": encode_rational(evidence.rho),
            "r": {
                "t": encode_rational(evidence.radii[0]),
                "x": encode_rational(evidence.radii[1]),
            },
            "window": [int(evidence.window[0]), int(evidence.window[1])],
            "epsilon": encode_rational(evidence.epsilon),
            "table": [
                [encode_rational(value) for value in row] for row in evidence.table
            ],
        }
        if evidence.point is not None:
            encoded["point"] = {
                "t": encode_rational(evidence.point[0]),
                "x": encode_rational(evidence.point[1]),
            }
            encoded["side"] = evidence.side
        return encoded
    return {
        "kind": "witness",
        "input": admitted_input(goal, model),
        "values": [
            [name, encode_rational(value)] for name, value in evidence.assignment
        ],
    }


def leaf_constraints(goal: StreamGoal, model: StreamModel) -> tuple[Context, Any, Any]:
    """Return the context and leaf formulas a certificate for this goal answers."""
    claim = goal.claim
    if type(claim) is CoefficientClaim:
        assert model.coefficients is not None
        context = Context(("b", "u"))
        return (
            context,
            model.coefficients.antecedent,
            render.coefficient_leaf_consequent(model, claim),
        )
    assert type(claim) is FieldClaim
    return (
        FIELD_CONTEXT,
        render.field_antecedent(claim),
        render.field_leaf_consequent(model, claim),
    )


def replay_stream(
    artifact: bytes,
    expected: StreamGoal,
    limits: EntailmentLimits | None = None,
    project: Path | None = None,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> StreamOutcome:
    """Check an artifact about ``expected`` in the caller's pinned environment."""
    limits = limits or EntailmentLimits()
    if deadline is None:
        deadline = time.monotonic() + limits.wall_time_ms / 1000
    decided = regenerate_application(artifact, expected, limits)
    if not isinstance(decided, tuple):
        return decided
    status, evidence, unit = decided
    model = MODELS[expected.model]
    checked = check_root(
        unit, imports_for(expected, model), limits, project, deadline, cancel
    )
    if not isinstance(checked, tuple):
        return checked
    return ValidatedStream(status, expected, evidence, checked, unit.dependencies)


def regenerate_application(
    artifact: bytes,
    expected: StreamGoal,
    limits: EntailmentLimits | None = None,
) -> tuple[str, Evidence, RootApplication] | InvalidEvidence | ReplayUnavailable:
    """Bind an artifact to ``expected`` and regenerate its root application.

    This is the part of `replay_stream` that runs no Lean, so the status
    it returns is only what the artifact claims, never a decision. The
    application is what a replay would check; showing it grants nothing.
    """
    limits = limits or EntailmentLimits()
    try:
        check_goal_limits(expected, limits)
    except GoalError as error:
        return ReplayUnavailable(f"the expected goal exceeds a limit: {error}")
    model = supported(expected)
    if isinstance(model, str):
        return ReplayUnavailable(f"unsupported goal: {model}")
    decoded = _decode(artifact, expected, model, limits)
    if isinstance(decoded, InvalidEvidence):
        return decoded
    status, evidence, source = decoded
    try:
        unit = application(expected, model, evidence)
    except ValueError as error:
        return InvalidEvidence(f"the evidence does not fit the claim: {error}")
    if source != unit.source:
        return InvalidEvidence("the retained source does not match regeneration")
    return status, evidence, unit


def _decode(
    artifact: bytes, expected: StreamGoal, model: StreamModel, limits: EntailmentLimits
) -> tuple[str, Evidence, str] | InvalidEvidence:
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
        evidence = _decode_evidence(document["evidence"], expected, model, limits)
    except (EvidenceError, GoalError, RecursionError, ValueError) as error:
        return InvalidEvidence(f"the evidence is malformed: {error}")
    if refutes(evidence) != (status == "REFUTED"):
        return InvalidEvidence("the status does not match the kind of evidence")
    if document["environment"] != environment_identity(imports_for(expected, model)):
        return InvalidEvidence("the artifact names a different Lean environment")
    if type(document["source"]) is not str:
        return InvalidEvidence("the retained source is not text")
    return status, evidence, document["source"]


def _decode_evidence(
    data: Any, goal: StreamGoal, model: StreamModel, limits: EntailmentLimits
) -> Evidence:
    """Read retained evidence, checking its shape against the goal."""
    if type(data) is not dict:
        raise EvidenceError("evidence is an object")
    kind = data.get("kind")
    claim = goal.claim
    if kind == "contract" and set(data) == {"kind"}:
        if type(claim) is not FullStreamClaim:
            raise EvidenceError(
                "only the full-stream identity rests on the contract alone"
            )
        return ContractOnly()
    if type(claim) is FullStreamClaim:
        raise EvidenceError("a full-stream claim carries no leaf evidence")
    if type(claim) is MildTruncationClaim or type(claim) is MildBandClaim:
        return _decode_mild_evidence(data, goal, limits)
    if type(claim) in (TrigTruncationClaim, TrigBandClaim):
        return _decode_trig_evidence(data, goal, limits)
    if type(claim) is FieldClaim and goal.interpretation == "analytic-field":
        return _decode_window_evidence(data, goal, claim, limits)
    if type(claim) is TruncationClaim:
        if goal.viscosity is not None:  # `supported` matched it to a viscous lane
            return _decode_front_evidence(data, limits)
        if kind != "tail-radius" or set(data) != {"kind", "rho"}:
            raise EvidenceError("a truncation proof carries only its radius ρ")
        rho = decode_rational(data["rho"], limits)
        if rho <= 0:
            raise EvidenceError("ρ is positive")
        return TailEvidence(rho)
    if kind in ("tail-radius", "front-radii", "window"):
        raise EvidenceError(
            "only a truncation or analytic-field claim carries a radius"
        )
    context, antecedent, consequent = leaf_constraints(goal, model)
    if kind == "leaf-certificate" and set(data) == {"kind", "certificate"}:
        certificate = decode_evidence(data["certificate"], context, limits)
        if not isinstance(certificate, Certificate):
            raise EvidenceError("a leaf certificate is a certificate")
        before, after = nonstrict(antecedent), nonstrict(consequent)
        if before is None or after is None:
            raise EvidenceError("the claim is outside the certificate fragment")
        check_certificate(certificate, before, after, limits)
        return certificate
    if kind == "witness" and set(data) == {"kind", "input", "values"}:
        if data["input"] != admitted_input(goal, model):
            raise EvidenceError("the witness names another admitted input")
        entries = data["values"]
        if type(claim) is FieldClaim:
            assert model.field is not None
            names = model.field.names
        else:
            names = ("b", "u")
        if type(entries) is not list or len(entries) != len(names):
            raise EvidenceError("a witness gives one value per coordinate")
        values: list[tuple[str, Fraction]] = []
        for entry, name in zip(entries, names):
            if type(entry) is not list or len(entry) != 2 or entry[0] != name:
                raise EvidenceError("witness values are named in order")
            values.append((name, decode_rational(entry[1], limits)))
        return Witness(tuple(values))
    raise EvidenceError(
        "evidence is the contract, a leaf certificate, a witness or a radius"
    )


def _decode_mild_evidence(
    data: dict[str, Any], goal: StreamGoal, limits: EntailmentLimits
) -> mild.MildEvidence:
    """Decode bounded numerical premises; no caller-supplied carrier is trusted."""
    kind = data.get("kind")
    keys = {"kind", "size", "weight", "window"}
    if kind == "mild-contraction":
        keys.add("radius")
    elif kind != "mild-scale":
        raise EvidenceError("a mild proof uses mild-scale or mild-contraction evidence")
    if set(data) != keys:
        raise EvidenceError("mild evidence has exactly its rational theorem premises")
    claim, start = goal.claim, goal.profile
    assert (
        type(claim) is MildTruncationClaim or type(claim) is MildBandClaim
    ) and isinstance(start, Modes)
    size, big_l = data["size"], decode_rational(data["weight"], limits)
    expected_size, expected_weight = mild.start_constants(start)
    if type(size) is not int or size != expected_size or big_l != expected_weight:
        raise EvidenceError("mild size and weight must match the declared start")
    window = data["window"]
    if type(window) is not int or not 1 <= window <= (
        64 if type(claim) is MildTruncationClaim else mild.MAX_BAND_WINDOW
    ):
        raise EvidenceError("the mild window is a bounded positive natural number")
    if type(claim) is MildTruncationClaim and window != claim.window:
        raise EvidenceError("the mild truncation window must match the goal")
    radius = (
        decode_rational(data["radius"], limits) if kind == "mild-contraction" else None
    )
    if radius is not None and radius < 0:
        raise EvidenceError("a heat radius is nonnegative")
    # The independent Lean replay proves strictness, heat absorption, and the
    # requested bound; decoding a well-shaped candidate grants no authority.
    return mild.MildEvidence(kind, size, big_l, window, radius)


def _decode_trig_evidence(
    data: dict[str, Any], goal: StreamGoal, limits: EntailmentLimits
) -> RadiusEvidence | BandEvidence:
    """Read an Euler-radius proof's constants, strictly.

    The size and the weight are the start's own, recomputed here; a band's
    window is bounded and its norms are one per degree. Lean decides every
    value again; this only refuses what no producer writes.
    """
    start = goal.profile
    assert isinstance(start, Modes)
    kind = data.get("kind")
    if type(goal.claim) is TrigTruncationClaim:
        if kind != "euler-radius" or set(data) != {"kind", "size", "weight"}:
            raise EvidenceError("an Euler radius proof carries its size and weight")
    elif kind != "euler-band" or set(data) != {
        "kind",
        "size",
        "weight",
        "window",
        "norms",
    }:
        raise EvidenceError(
            "an Euler band proof carries its size, weight, window and norms"
        )
    size, big_l = data["size"], decode_rational(data["weight"], limits)
    if type(size) is not int or size != largest_size(start):
        raise EvidenceError("the size is the largest mode size of the start")
    if big_l != weight(start):
        raise EvidenceError("the weight is the start's absolute sum")
    if kind == "euler-radius":
        return RadiusEvidence(size, big_l)
    window = data["window"]
    if type(window) is not int or not 1 <= window <= MAX_BAND_WINDOW:
        raise EvidenceError(f"the window is between 1 and {MAX_BAND_WINDOW}")
    norms = data["norms"]
    if type(norms) is not list or len(norms) != window:
        raise EvidenceError("one norm per degree of the window")
    values = tuple(decode_rational(value, limits) for value in norms)
    if any(value < 0 for value in values):
        raise EvidenceError("norms are not negative")
    return BandEvidence(size, big_l, window, values)


def _decode_front_evidence(
    data: dict[str, Any], limits: EntailmentLimits
) -> FrontEvidence:
    """Read a front truncation proof's ``ρ`` and inverse radii ``r``."""
    if data.get("kind") != "front-radii" or set(data) != {"kind", "rho", "r"}:
        raise EvidenceError(
            "a front truncation proof carries only its radius ρ and radii r"
        )
    rho = decode_rational(data["rho"], limits)
    if type(data["r"]) is not dict or set(data["r"]) != {"t", "x"}:
        raise EvidenceError("the radii r are given per axis t, x")
    radii = (
        decode_rational(data["r"]["t"], limits),
        decode_rational(data["r"]["x"], limits),
    )
    if rho <= 0 or any(r <= 0 for r in radii):
        raise EvidenceError("ρ and the radii r are positive")
    return FrontEvidence(rho, radii)


_WINDOW_KEYS = frozenset({"kind", "rho", "r", "window", "epsilon", "table"})


def _axes(data: Any, what: str, limits: EntailmentLimits) -> tuple[Fraction, Fraction]:
    if type(data) is not dict or set(data) != {"t", "x"}:
        raise EvidenceError(f"{what} is given per axis t, x")
    return decode_rational(data["t"], limits), decode_rational(data["x"], limits)


def _decode_window_evidence(
    data: dict[str, Any], goal: StreamGoal, claim: FieldClaim, limits: EntailmentLimits
) -> WindowEvidence:
    """Read a field band's checked window, strictly.

    The window is the fixed `BAND_WINDOW`, the table's shape is the
    window's, and a refutation names a point of the claim's box and the side
    it leaves the band on, which the claim must bound. Lean checks the
    table and the point; this only refuses what no producer writes.
    """
    keys = set(data)
    if data.get("kind") != "window" or not (
        keys == _WINDOW_KEYS or keys == _WINDOW_KEYS | {"point", "side"}
    ):
        raise EvidenceError(
            "a field band carries its radius ρ, radii r, window, error, table "
            "and, refuting, a point and a side"
        )
    rho = decode_rational(data["rho"], limits)
    radii = _axes(data["r"], "the radii r", limits)
    if rho <= 0 or any(r <= 0 for r in radii):
        raise EvidenceError("ρ and the radii r are positive")
    window = data["window"]
    if (
        type(window) is not list
        or len(window) != 2
        or any(type(n) is not int for n in window)
        or tuple(window) != BAND_WINDOW
    ):
        raise EvidenceError(f"the window is the fixed {list(BAND_WINDOW)}")
    epsilon = decode_rational(data["epsilon"], limits)
    if epsilon < 0:  # zero is a bound too: a constant potential has no tail
        raise EvidenceError("the certified error ε is not negative")
    rows = data["table"]
    if type(rows) is not list or len(rows) != window[0]:
        raise EvidenceError("the table has one row per time degree of the window")
    table = []
    for row in rows:
        if type(row) is not list or len(row) != window[1]:
            raise EvidenceError("each table row has one entry per space degree")
        table.append(tuple(decode_rational(value, limits) for value in row))
    if "point" not in data:
        return WindowEvidence(rho, radii, (window[0], window[1]), epsilon, tuple(table))
    point = _axes(data["point"], "the refutation point", limits)
    (_, r_t), (_, r_x) = claim.time, claim.space
    if abs(point[0]) > r_t or abs(point[1]) > r_x:
        raise EvidenceError("the refutation point is in the claim's box")
    side = data["side"]
    if side not in ("above", "below"):
        raise EvidenceError("the side is above or below")
    bounds = band_of(claim.post)
    assert bounds is not None
    lo, hi = bounds
    if (side == "above" and hi is None) or (side == "below" and lo is None):
        raise EvidenceError("the refutation's side is one the claim bounds")
    return WindowEvidence(
        rho, radii, (window[0], window[1]), epsilon, tuple(table), point, side
    )
