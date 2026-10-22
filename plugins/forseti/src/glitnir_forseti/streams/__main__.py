"""Command line: decide a stream or field root goal, or replay a stream artifact.

    python -m glitnir_forseti.streams decide GOAL.json [--artifact OUT.json]
        [--lean-project P] [--wall-time-ms N]
    python -m glitnir_forseti.streams replay ARTIFACT.json --goal GOAL.json
        [--lean-project P] [--wall-time-ms N]

A goal file is a ``gimle.forseti.stream-goal/v2`` document. ``decide``
searches with the entailment lane's Z3 producers and needs the ``z3`` extra; ``replay``
imports no solver and re-checks retained exact evidence with the Lean
toolchain alone. Both print one JSON object; ``replay`` exits 0 only when the
root is validated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from glitnir_forseti.entailment.limits import EntailmentLimits, LimitError
from glitnir_forseti.entailment.replay import InvalidEvidence
from glitnir_forseti.entailment.syntax import GoalError

from .goal import (
    CLASSICAL_EULER,
    ExpSum,
    FieldClaim,
    MildBandClaim,
    MildTruncationClaim,
    Modes,
    StreamGoal,
    TrigBandClaim,
    TrigTruncationClaim,
    TruncationClaim,
    decode_goal,
)
from .models import PROFILE
from .replay import ValidatedStream, replay_stream

_MAX_INPUT_BYTES = 16_777_216


class _InputError(ValueError):
    """An input file could not be read."""


def _read(path: Path, limit: int) -> bytes:
    """Read a file, refusing one over ``limit`` bytes."""
    if path.stat().st_size > limit:
        raise _InputError(f"{path} exceeds {limit} bytes")
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def _read_json(path: Path) -> Any:
    """Read one bounded JSON document."""
    raw = _read(path, _MAX_INPUT_BYTES)
    if len(raw) > _MAX_INPUT_BYTES:
        raise _InputError(f"{path} exceeds {_MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(raw)
    except RecursionError as error:
        raise _InputError(f"{path} is nested too deeply") from error


def _decide(arguments: argparse.Namespace, limits: EntailmentLimits) -> int:
    """Search, replay, and report the outcome."""
    from .service import decide_stream

    goal = decode_goal(_read_json(arguments.goal), limits)
    result = decide_stream(goal, limits, project=arguments.lean_project)
    if result.artifact is not None and arguments.artifact is not None:
        arguments.artifact.write_bytes(result.artifact)
    print(
        json.dumps(
            {
                "status": result.status.value,
                "reason": result.reason.value if result.reason else None,
                "profile": _profile(goal),
                "solution_class": _solution_class(goal),
                "axioms": list(result.axioms),
                "dependencies": list(result.dependencies),
                "stages": [stage.as_dict() for stage in result.stages],
                "limits": result.limits,
            },
            indent=2,
        )
    )
    return 0


def _profile(goal: StreamGoal) -> str:
    """Describe the declared initial profile, for the report."""
    if isinstance(goal.profile, ExpSum):
        return " + ".join(f"{c}·e^({a}x)" for c, a in goal.profile.terms)
    if isinstance(goal.profile, Modes):
        return " + ".join(
            f"{c}·e^(i({_mode(k1, k2)}))" for k1, k2, c in goal.profile.terms
        )
    return "x^2" if goal.profile == PROFILE else repr(goal.profile)


def _mode(k1: int, k2: int) -> str:
    """Write ``k·x`` for a mode, folding signs and dropping zero components."""
    parts = []
    for k, name in ((k1, "x"), (k2, "y")):
        if k == 0:
            continue
        coefficient = "" if abs(k) == 1 else str(abs(k))
        parts.append(f"{'-' if k < 0 else '+'}{coefficient}{name}")
    text = " ".join(parts).replace("+", "+ ").replace("-", "- ").strip()
    return text.removeprefix("+ ").replace("- ", "-", 1) if text else "0"


def _solution_class(goal: StreamGoal) -> str:
    """Say which objects the checked theorem is about, for the report."""
    if isinstance(goal.claim, (MildTruncationClaim, MildBandClaim)):
        return (
            "the unique formal output of the typed mild circuit, with classical "
            "physical realization on the time interior, with absolute convergence, "
            "Fourier reconstruction and spectral nonincrease on "
            f"0 ≤ t ≤ {goal.claim.time}; "
            "every input with "
            "the declared initial slice is admitted"
        )
    if isinstance(goal.claim, (TrigTruncationClaim, TrigBandClaim)):
        if goal.interpretation in CLASSICAL_EULER:
            return (
                "a total circuit contract for every output of Asgard's Euler "
                "vorticity circuit admitted by the initial slice; the lane checks "
                "evenness and zero mean and certifies formal uniqueness, initial "
                "values, invariants, convergence, the classical vorticity equation "
                f"and the requested bound on |t| <= {goal.claim.radius}; "
                "uniqueness among arbitrary classical PDE solutions is not claimed"
            )
        return (
            "the analytic field of the formal vorticity stream asgard-lean constructs "
            f"from the start at viscosity {goal.viscosity}, the sum of its t-series, "
            f"uniformly on |t| <= {goal.claim.radius} and every x (asgard-lean "
            "Streams.EulerRadius); a certified convergent series, whose sum solves "
            "the vorticity equation on the certified disc for an even, mean-zero "
            "start, which the lane expects but does not check (asgard-lean "
            "NS.euler_classical); the claim here is only the bound"
        )
    if isinstance(goal.claim, TruncationClaim):
        r_t, r_x = goal.claim.radius
        if goal.viscosity is not None:
            return (
                "the analytic field of every stream the Burgers circuit at viscosity "
                f"{goal.viscosity} reconstructs from the t = 0 slice of the Cole–Hopf "
                "front of the potential profile, against its window, uniformly on "
                f"|t| <= {r_t}, |x| <= {r_x} (Lean module "
                "Gimle.Asgard.Streams.ColeHopf); a certified convergent series, not a "
                "claim that the real field solves the PDE "
                "or equals the closed-form front"
            )
        return (
            "the analytic field of every stream the heat circuit reconstructs from "
            "the profile, against its window, uniformly on |t| <= "
            f"{r_t}, |x| <= {r_x} (Lean module Gimle.Asgard.Streams.AnalyticHeat); "
            "a certified convergent "
            "series, not a claim that the real field solves the PDE"
        )
    if isinstance(goal.claim, FieldClaim):
        (t0, t1), (x0, x1) = goal.claim.time, goal.claim.space
        return (
            "the real field of the finite polynomial stream realizing the output "
            "(Lean module Gimle.Asgard.Streams.Realization), "
            f"on t in [{t0}, {t1}], x in [{x0}, {x1}]; not a claim "
            "about arbitrary smooth fields"
        )
    return f"formal power series ({goal.basis.upper()}); no convergence claim"


def _replay(arguments: argparse.Namespace, limits: EntailmentLimits) -> int:
    """Replay retained evidence against the goal file; no solver runs."""
    goal = decode_goal(_read_json(arguments.goal), limits)
    artifact = _read(arguments.artifact, limits.max_artifact_bytes)
    outcome = replay_stream(artifact, goal, limits, arguments.lean_project)
    report: dict[str, Any]
    if isinstance(outcome, ValidatedStream):
        report = {
            "outcome": "validated",
            "status": outcome.status,
            "model": goal.model,
            "interpretation": goal.interpretation,
            "basis": goal.basis,
            "claim": type(goal.claim).__name__,
            "profile": _profile(goal),
            "solution_class": _solution_class(goal),
            "axioms": list(outcome.axioms),
            "dependencies": list(outcome.dependencies),
        }
    elif isinstance(outcome, InvalidEvidence):
        report = {"outcome": "invalid", "reason": outcome.reason}
    else:
        report = {"outcome": "unavailable", "reason": outcome.reason}
    print(json.dumps(report, indent=2))
    return 0 if isinstance(outcome, ValidatedStream) else 1


def main(argv: list[str] | None = None) -> int:
    """Run one command."""
    parser = argparse.ArgumentParser(prog="python -m glitnir_forseti.streams")
    commands = parser.add_subparsers(dest="command", required=True)
    decide = commands.add_parser("decide", help="search, then check the root")
    decide.add_argument("goal", type=Path)
    decide.add_argument("--artifact", type=Path)
    check = commands.add_parser("replay", help="check a stream artifact")
    check.add_argument("artifact", type=Path)
    check.add_argument("--goal", type=Path, required=True)
    for command in (decide, check):
        command.add_argument("--lean-project", type=Path)
        command.add_argument("--wall-time-ms", type=int)
    arguments = parser.parse_args(argv)
    try:
        limits = EntailmentLimits()
        if arguments.wall_time_ms is not None:
            limits = EntailmentLimits(
                **{**limits.as_dict(), "wall_time_ms": arguments.wall_time_ms}
            )
        if arguments.command == "decide":
            return _decide(arguments, limits)
        return _replay(arguments, limits)
    except (GoalError, LimitError, _InputError, json.JSONDecodeError, OSError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
