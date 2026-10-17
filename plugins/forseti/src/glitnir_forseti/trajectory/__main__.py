"""Command line: decide a trajectory root goal, or replay a root artifact.

    python -m glitnir_forseti.trajectory decide GOAL.json [--artifact OUT.json]
        [--lean-project P] [--wall-time-ms N]
    python -m glitnir_forseti.trajectory replay ARTIFACT.json --goal GOAL.json
        [--lean-project P] [--wall-time-ms N]

A goal file is a ``gimle.forseti.trajectory-goal/v1`` document. ``decide``
searches with task 111's Z3 producers and needs the ``z3`` extra; ``replay``
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

from .goal import decode_goal
from .replay import ValidatedRoot, replay_root

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
    from .service import decide_trajectory

    goal = decode_goal(_read_json(arguments.goal), limits)
    result = decide_trajectory(goal, limits, project=arguments.lean_project)
    if result.artifact is not None and arguments.artifact is not None:
        arguments.artifact.write_bytes(result.artifact)
    print(
        json.dumps(
            {
                "status": result.status.value,
                "reason": result.reason.value if result.reason else None,
                "axioms": list(result.axioms),
                "dependencies": list(result.dependencies),
                "stages": [stage.as_dict() for stage in result.stages],
                "limits": result.limits,
            },
            indent=2,
        )
    )
    return 0


def _replay(arguments: argparse.Namespace, limits: EntailmentLimits) -> int:
    """Replay retained evidence against the goal file; no solver runs."""
    goal = decode_goal(_read_json(arguments.goal), limits)
    artifact = _read(arguments.artifact, limits.max_artifact_bytes)
    outcome = replay_root(artifact, goal, limits, arguments.lean_project)
    report: dict[str, Any]
    if isinstance(outcome, ValidatedRoot):
        report = {
            "outcome": "validated",
            "status": outcome.status,
            "model": goal.model,
            "time": {"axis": goal.axis, "start": str(goal.start)},
            "initial": {port: str(value) for port, value in goal.initial},
            "observation": goal.observation,
            "axioms": list(outcome.axioms),
            "dependencies": list(outcome.dependencies),
        }
        if goal.drivers:
            # The driver bounds the decided root assumes.
            report["drivers"] = {port: str(bound) for port, bound in goal.drivers}
    elif isinstance(outcome, InvalidEvidence):
        report = {"outcome": "invalid", "reason": outcome.reason}
    else:
        report = {"outcome": "unavailable", "reason": outcome.reason}
    print(json.dumps(report, indent=2))
    return 0 if isinstance(outcome, ValidatedRoot) else 1


def main(argv: list[str] | None = None) -> int:
    """Run one command."""
    parser = argparse.ArgumentParser(prog="python -m glitnir_forseti.trajectory")
    commands = parser.add_subparsers(dest="command", required=True)
    decide = commands.add_parser("decide", help="search, then check the root")
    decide.add_argument("goal", type=Path)
    decide.add_argument("--artifact", type=Path)
    check = commands.add_parser("replay", help="check a root artifact")
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
