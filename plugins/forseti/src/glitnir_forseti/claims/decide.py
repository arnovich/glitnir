"""One front door to the four deciders, for the agent and the notebook.

`read_goal` accepts a goal as text (`glitnir_forseti.claims.text`) or as its
JSON encoding; `decide` hands it to the decider for its kind, unchanged, and
reports that decider's own answer: ``PROVEN`` or ``REFUTED`` with a replayable
artifact, or ``UNKNOWN`` with a reason. Nothing here searches, checks or
decides; a status is only ever copied from a service result. The stepped
decider keeps no artifact: it answers only without a replay, below, and a
replayed request about a stepped goal ends ``UNKNOWN: lean-unavailable``.

For a decided goal the checked Lean statement and script are regenerated from
the artifact by the kind's ``regenerate_application``, the same regeneration a
replay performs before running Lean. They are shown so a person or an agent
can see what was proved; showing them grants nothing, and the artifact, not
this record, is what replays.

``decide(..., replay=False)`` runs no Lean at all: the decider stops at the
first evidence it finds and reports the Lean application of that evidence,
unchecked and without an artifact. Its ``PROVEN`` or ``REFUTED`` is then only a
proposal, which the caller must have Lean check.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from glitnir_forseti.entailment import replay as entailment_replay
from glitnir_forseti.entailment import syntax as entailment_syntax
from glitnir_forseti.entailment.limits import CEILINGS, EntailmentLimits
from glitnir_forseti.entailment.syntax import Goal, GoalError, Tru
from glitnir_forseti.stepped import service as stepped_service
from glitnir_forseti.stepped.examples import BARRIER, VARIANCE, YEAR, breach_goal
from glitnir_forseti.stepped.goal import SteppedGoal
from glitnir_forseti.stepped.models import MODELS as STEPPED_MODELS
from glitnir_forseti.stepped.models import SteppedEntry
from glitnir_forseti.stepped.numbers import horizon_bound
from glitnir_forseti.streams import goal as stream_goal
from glitnir_forseti.streams import replay as stream_replay
from glitnir_forseti.streams.examples import EXAMPLES as STREAM_EXAMPLES
from glitnir_forseti.streams.goal import StreamGoal
from glitnir_forseti.streams.models import MODELS as STREAM_MODELS
from glitnir_forseti.streams.models import StreamModel
from glitnir_forseti.trajectory import goal as trajectory_goal
from glitnir_forseti.trajectory import replay as trajectory_replay
from glitnir_forseti.trajectory.goal import TrajectoryGoal
from glitnir_forseti.trajectory.models import MODELS as TRAJECTORY_MODELS

from .text import (
    MAX_TEXT,
    AnyGoal,
    GoalTextError,
    parse_goal,
    render_goal,
    render_short_goal,
)

#: Decode JSON goals at the hard ceilings, so a goal over the request's limits
#: reaches its decider and ends ``UNKNOWN: resource-limit`` as its text does.
_DECODE_LIMITS = EntailmentLimits(**CEILINGS)


@dataclass(frozen=True, slots=True)
class _Kind:
    """How one kind of goal is decoded, decided, bound and replayed."""

    name: str
    module: str
    schema: str
    decode: Callable[[Any, EntailmentLimits], Any]
    encode: Callable[[Any], dict[str, Any]]
    decide: Callable[..., Any]
    regenerate: Callable[..., Any]


def _decide_entailment(*args: Any, **kwargs: Any) -> Any:
    """Load the entailment producer only for an explicit decision request."""
    from glitnir_forseti.entailment.service import decide_entailment

    return decide_entailment(*args, **kwargs)


def _decide_trajectory(*args: Any, **kwargs: Any) -> Any:
    """Load the trajectory producer only for an explicit decision request."""
    from glitnir_forseti.trajectory.service import decide_trajectory

    return decide_trajectory(*args, **kwargs)


def _decide_stream(*args: Any, **kwargs: Any) -> Any:
    """Load the streams producer only for an explicit decision request."""
    from glitnir_forseti.streams.service import decide_stream

    return decide_stream(*args, **kwargs)


_KINDS: dict[type, _Kind] = {
    Goal: _Kind(
        "entailment",
        "glitnir_forseti.entailment",
        "gimle.forseti.entailment-goal/v1",
        entailment_syntax.decode_goal,
        entailment_syntax.encode_goal,
        _decide_entailment,
        entailment_replay.regenerate_application,
    ),
    TrajectoryGoal: _Kind(
        "trajectory",
        "glitnir_forseti.trajectory",
        trajectory_goal.GOAL_SCHEMA,
        trajectory_goal.decode_goal,
        trajectory_goal.encode_goal,
        _decide_trajectory,
        trajectory_replay.regenerate_application,
    ),
    StreamGoal: _Kind(
        "stream",
        "glitnir_forseti.streams",
        stream_goal.GOAL_SCHEMA,
        stream_goal.decode_goal,
        stream_goal.encode_goal,
        _decide_stream,
        stream_replay.regenerate_application,
    ),
    SteppedGoal: _Kind(
        "stepped",
        "glitnir_forseti.stepped",
        SteppedGoal.SCHEMA,
        SteppedGoal.from_json,
        SteppedGoal.to_json,
        stepped_service.decide_stepped,
        stepped_service.regenerate_application,
    ),
}


def goal_kind(goal: AnyGoal) -> str:
    """Name a parsed goal's kind: entailment, trajectory, stream or stepped."""
    return _KINDS[type(goal)].name


def read_goal(source: str) -> AnyGoal:
    """Read a goal written as text, or as a JSON document of any of the kinds.

    Size is bounded here; the request's own limits are the decider's to apply,
    so a goal over them is ``UNKNOWN: resource-limit`` in either encoding.

    Raises:
        GoalTextError: The source is neither a goal's text nor its encoding.
    """
    if type(source) is not str:
        raise GoalTextError("a goal is text")
    if not source.lstrip().startswith("{"):
        return parse_goal(source)
    if len(source) > MAX_TEXT:
        raise GoalTextError(f"a goal document exceeds {MAX_TEXT} characters")
    limits = _DECODE_LIMITS
    try:
        document = json.loads(source)
    except (ValueError, RecursionError) as error:
        raise GoalTextError(f"the goal is not valid JSON: {error}") from None
    schema = document.get("schema") if type(document) is dict else None
    for kind in _KINDS.values():
        if schema == kind.schema:
            try:
                goal: AnyGoal = kind.decode(document, limits)
                return goal
            except (GoalError, RecursionError, ValueError) as error:
                raise GoalTextError(f"malformed {kind.name} goal: {error}") from None
    raise GoalTextError(f"unknown goal schema {schema!r}")


@dataclass(frozen=True, slots=True)
class LeanApplication:
    """What Lean checked: the prelude, the theorem's statement and its proof."""

    prelude: str
    statement: str
    script: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Decision:
    """A decider's answer about one goal, as that decider gave it.

    Attributes:
        kind: ``entailment``, ``trajectory``, ``stream`` or ``stepped``.
        goal: The goal decided.
        status: ``PROVEN``, ``REFUTED`` or ``UNKNOWN``.
        reason: For ``UNKNOWN``, the decider's reason.
        stages: Each lane or stage the decider ran, in order.
        axioms: The checked theorem's axioms, when decided.
        dependencies: The Lean lemmas a root proof cites, when decided.
        artifact: The replayable artifact, when decided.
        lean: The checked application, regenerated from the artifact; or,
            decided without a replay, the unchecked application to check.
    """

    kind: str
    goal: AnyGoal
    status: str
    reason: str | None = None
    stages: tuple[dict[str, Any], ...] = ()
    axioms: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    artifact: bytes | None = field(default=None, repr=False)
    lean: LeanApplication | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready record, without the artifact's bytes.

        ``goal`` is the goal's text, or its JSON encoding when it has none.
        """
        try:
            goal: Any = render_goal(self.goal)
        except GoalTextError:
            goal = _KINDS[type(self.goal)].encode(self.goal)
        record: dict[str, Any] = {
            "kind": self.kind,
            "goal": goal,
            "status": self.status,
            "reason": self.reason,
            "stages": list(self.stages),
            "axioms": list(self.axioms),
            "dependencies": list(self.dependencies),
        }
        if self.lean is not None:
            record["lean"] = {
                "prelude": self.lean.prelude,
                "statement": self.lean.statement,
                "script": list(self.lean.script),
            }
        return record


def decide(
    goal: AnyGoal,
    limits: EntailmentLimits | None = None,
    *,
    project: Path | None = None,
    cancel: threading.Event | None = None,
    replay: bool = True,
) -> Decision:
    """Ask the decider for ``goal``'s kind, and report exactly its answer.

    With ``replay`` False nothing is checked: a decided status is the
    decider's unchecked proposal, and ``lean`` is the application to check.
    """
    kind = _KINDS.get(type(goal))
    if kind is None:
        raise TypeError(
            "goal must be an entailment, trajectory, stream or stepped goal"
        )
    if replay:
        result = kind.decide(goal, limits, project=project, cancel=cancel)
    else:
        result = kind.decide(goal, limits, cancel=cancel, replay=False)
    stages = getattr(result, "lanes", None) or getattr(result, "stages", None) or ()
    lean = None
    if not replay and result.application is not None:
        unchecked = result.application
        lean = LeanApplication(
            unchecked.prelude, unchecked.statement, tuple(unchecked.script)
        )
    elif result.artifact is not None:
        # The service has already replayed this artifact; this only rebuilds
        # the application it checked, for display.
        bound = kind.regenerate(result.artifact, goal, limits)
        if not isinstance(bound, tuple) or bound[0] != result.status.value:
            # The service just built this artifact from this goal; a mismatch
            # is a defect, and reporting the status without it would hide it.
            raise RuntimeError(f"a fresh {kind.name} artifact does not bind: {bound}")
        application = bound[2]
        lean = LeanApplication(
            application.prelude, application.statement, tuple(application.script)
        )
    return Decision(
        kind=kind.name,
        goal=goal,
        status=result.status.value,
        reason=result.reason.value if result.reason is not None else None,
        stages=tuple(stage.as_dict() for stage in stages),
        axioms=tuple(result.axioms),
        dependencies=tuple(getattr(result, "dependencies", ())),
        artifact=result.artifact,
        lean=lean,
    )


def from_artifact(artifact: bytes, goal: AnyGoal) -> Decision | None:
    """Rebuild the decision an artifact records about ``goal``, running no Lean.

    The status is the artifact's claim and the Lean is what a replay would
    check; a caller that grants anything must still have Lean check it. This
    binds a kept artifact to a goal supplied independently.

    Returns:
        The decision, or None when the artifact is not about ``goal``.
    """
    kind = _KINDS.get(type(goal))
    if kind is None or type(artifact) is not bytes:
        return None
    bound = kind.regenerate(artifact, goal, None)
    if not isinstance(bound, tuple):
        return None
    status, _, application = bound
    return Decision(
        kind=kind.name,
        goal=goal,
        status=status,
        artifact=artifact,
        lean=LeanApplication(
            application.prelude, application.statement, tuple(application.script)
        ),
    )


def save(decision: Decision, directory: Path) -> dict[str, str]:
    """Write a decided goal and its artifact, named by the artifact's digest.

    Files are created exclusively and never followed or overwritten: a file
    of that name must already hold the same bytes, or the save fails.

    Returns:
        The two paths and the command that replays the artifact offline.

    Raises:
        ValueError: The decision has no artifact (it is ``UNKNOWN``).
        OSError: A file could not be written, or one of that name differs.
    """
    if decision.artifact is None:
        raise ValueError("only a decided goal has an artifact to save")
    kind = _KINDS[type(decision.goal)]
    digest = hashlib.sha256(decision.artifact).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{kind.name}-{digest}.artifact.json"
    goal = directory / f"{kind.name}-{digest}.goal.json"
    encoded = (json.dumps(kind.encode(decision.goal), indent=2) + "\n").encode()
    _write_new(artifact, decision.artifact)
    _write_new(goal, encoded)
    return {
        "artifact": str(artifact),
        "goal": str(goal),
        "replay": replay_command(decision.goal, artifact, goal),
    }


def replay_command(goal: AnyGoal, artifact: Path, kept_goal: Path) -> str:
    """Return the shell command that replays an artifact against a kept goal."""
    module = _KINDS[type(goal)].module
    return (
        f"python -m {module} replay {shlex.quote(str(artifact))}"
        f" --goal {shlex.quote(str(kept_goal))}"
    )


def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` holding ``data``; never follow or replace a file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise OSError(f"{path} exists with different content") from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def catalog() -> str:
    """Describe the models, ports and readings the deciders support.

    Generated from the model registries, so it cannot fall out of date.
    """
    lines = [
        "Trajectory models (interpretation continuous-real, obligation contract,"
        " quantifier always):"
    ]
    for entry in TRAJECTORY_MODELS.values():
        initial = ", ".join(
            f"{port} = {_number(value)}"
            for port, value in zip(entry.initial_ids, entry.declared_initial)
        )
        observed = ", ".join(
            f"observe {port} as {band.name}"
            for port, band in entry.observations.items()
        )
        drivers = ""
        if entry.drivers is not None:
            drivers = (
                f"; drivers {entry.drivers.bounded} within B, required,"
                f" 0 <= B <= {_number(entry.drivers.bound)}"
            )
        lines.append(
            f"  {entry.key}: start {entry.axis} = {_number(entry.start)}"
            f" initial {initial}{drivers}; {observed}"
        )
        for band in entry.observations.values():
            short = render_short_goal(_declared(entry, band, Tru()))
            if short is not None:
                head = short.rsplit(" : ", 1)[0]
                lines.append(f"    short: {head} : <formula over {band.name}>")
    lines.append(
        "Stream models (interpretation formal-stream, evaluated-field,"
        " analytic-tail, analytic-field, trig-truncation, trig-band, euler-truncation,"
        " euler-band, mild-truncation or mild-band; basis ogf or egf for Taylor"
        " streams, interaction for mild; field formulas over t, x, u;"
        " coefficient formulas over c0, c1, ...; streams over t, x; profile"
        " over x, or for a truncation claim `exponentials [c, a], ...` for"
        " Σ c·e^(a·x); a model with a viscosity names it after its key and"
        " writes `potential exponentials ...`, the Cole–Hopf potential's, or"
        " for the vorticity `start modes [k1, k2, c], ...`, the modes of its"
        " start):"
    )
    for model in STREAM_MODELS.values():
        lines.append(f"  {model.key}: {model.summary}")
    lines.append(
        "Stepped models (shocks `variance v` or `subgaussian v`; claims"
        " `within N observe X breach B probability <= q`,"
        " `at every mark within N observe X breach B probability <= q`,"
        " `observe X expected <= c at N`):"
    )
    for stepped in STEPPED_MODELS.values():
        initial = ", ".join(_number(value) for value in stepped.initial)
        observed = ", ".join(
            f"{name} ({'breach' if seen.kind == 'exposure' else 'expected'} claims)"
            for name, seen in stepped.observations.items()
        )
        lines.append(
            f"  {stepped.key}: from ({initial}) at {_number(stepped.start)}"
            f" step {_number(stepped.delta)}, at most {stepped.horizon_cap} steps;"
            f" observe {observed}"
        )
    return "\n".join(lines)


def _declared(entry: Any, band: Any, post: Any) -> TrajectoryGoal:
    """Pose ``post`` about a model's band from its declared start and state."""
    return TrajectoryGoal(
        model=entry.key,
        interpretation="continuous-real",
        obligation="contract",
        axis=entry.axis,
        start=entry.start,
        initial=tuple(sorted(zip(entry.initial_ids, entry.declared_initial))),
        observation=band.port,
        name=band.name,
        quantifier="always",
        post=post,
        drivers=(
            ()
            if entry.drivers is None
            else ((entry.drivers.bounded, entry.drivers.bound),)
        ),
    )


def examples() -> tuple[dict[str, str], ...]:
    """Return one example goal per registered model, and one entailment.

    Each is a goal the deciders accept as written, generated from the
    registries: a trajectory model's declared state, start and first proved
    band in the short form, a stream model's field on the unit box, and a
    stepped model's year-long breach claim at its exact horizon bound. They
    are starting points for a person to edit, not claims anyone made.
    """
    found = [
        {
            "kind": "entailment",
            "model": "",
            # A goal the certificate lane proves (corpus "disk").
            "goal": "entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1",
        }
    ]
    for entry in TRAJECTORY_MODELS.values():
        band = next(iter(entry.observations.values()))
        goal = _declared(entry, band, band.invariant)
        found.append(
            {
                "kind": "trajectory",
                "model": entry.key,
                "goal": render_short_goal(goal) or render_goal(goal),
            }
        )
    for model in STREAM_MODELS.values():
        found.append({"kind": "stream", "model": model.key, "goal": _stream(model)})
    for stepped in STEPPED_MODELS.values():
        found.append(
            {
                "kind": "stepped",
                "model": stepped.key,
                "goal": render_goal(_stepped(stepped)),
            }
        )
    return tuple(found)


def _stepped(entry: SteppedEntry) -> SteppedGoal:
    """Pose a stepped model's breach claim at exactly its horizon bound.

    The claim is the first exposure's, over the examples' year (or the model's
    cap, if lower), bounded by ``N·Δ²·v/B²``, the figure its horizon theorem
    proves.
    """
    name = next(n for n, band in entry.observations.items() if band.kind == "exposure")
    horizon = min(YEAR, entry.horizon_cap)
    bound = horizon_bound(
        stepped_service.INITIAL_ENERGY, horizon, entry.delta, VARIANCE, BARRIER
    )
    return breach_goal(bound, entry=entry, horizon=horizon, observation=name)


def _stream(model: StreamModel) -> str:
    """Write an example goal about a stream model."""
    if model.full_stream is not None and model.field is not None:
        port = model.full_stream.outputs[model.field.port][0]
        return (
            f"stream {model.key} interpretation evaluated-field basis ogf"
            f" profile x*x field {port} over t in [0, 1], x in [-1, 1]"
            f" : {port} >= 0"
        )
    example = next(e for e in STREAM_EXAMPLES if e.goal.model == model.key)
    return render_goal(example.goal)


def check_names(goal: AnyGoal) -> None:
    """Refuse a goal naming a model, port or observation that does not exist.

    A misspelled name is not a claim outside the deciders' fragment but a
    claim nobody meant, so it is an error listing the valid names, not
    ``UNKNOWN``. Everything else unsupported stays the decider's ``UNKNOWN``.

    Raises:
        GoalTextError: A name is not in the registries; the message lists them.
    """
    if type(goal) is TrajectoryGoal:
        entry = TRAJECTORY_MODELS.get(goal.model)
        if entry is None:
            raise GoalTextError(f"no trajectory model {goal.model!r}\n{catalog()}")
        band = entry.observations.get(goal.observation)
        if band is None or band.name != goal.name:
            raise GoalTextError(
                f"{goal.model} has no observation {goal.observation!r} named"
                f" {goal.name!r}\n{catalog()}"
            )
        if {port for port, _ in goal.initial} != set(entry.initial_ids):
            raise GoalTextError(
                f"{goal.model}'s initial ports are"
                f" {', '.join(entry.initial_ids)}\n{catalog()}"
            )
    elif type(goal) is StreamGoal and goal.model not in STREAM_MODELS:
        raise GoalTextError(f"no stream model {goal.model!r}\n{catalog()}")
    elif type(goal) is SteppedGoal:
        stepped = STEPPED_MODELS.get(goal.model)
        if stepped is None:
            raise GoalTextError(f"no stepped model {goal.model!r}\n{catalog()}")
        if goal.observation not in stepped.observations:
            raise GoalTextError(
                f"{goal.model} has no observation {goal.observation!r}\n{catalog()}"
            )


def _number(value: Any) -> str:
    return str(value.numerator) if value.denominator == 1 else str(value)
