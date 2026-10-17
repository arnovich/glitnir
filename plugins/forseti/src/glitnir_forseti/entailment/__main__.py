"""Command line: decide a goal, or replay an artifact against a goal.

    python -m glitnir_forseti.entailment decide GOAL.json [--dictionary D.json]
        [--artifact OUT.json] [--lean-project P] [--wall-time-ms N]
    python -m glitnir_forseti.entailment replay ARTIFACT.json --goal GOAL.json
        [--lean-project P] [--wall-time-ms N]

A goal file is a ``gimle.forseti.entailment-goal/v1`` document; a dictionary
file is a JSON list of tagged polynomials over the goal's context. Both
commands print one JSON object. ``decide`` exits 0 for any of its three
outcomes; ``replay`` exits 0 only when the artifact is validated.

``replay`` imports no solver, so it runs where Z3 is not installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .limits import EntailmentLimits, LimitError
from .replay import InvalidEvidence, ValidatedEntailment, replay_artifact
from .syntax import GoalError, decode_goal

_MAX_INPUT_BYTES = 16_777_216


class _InputError(ValueError):
    """An input file could not be read."""


def _read_bytes(path: Path, limit: int) -> bytes:
    """Read a file, refusing one over ``limit`` bytes before reading it."""
    if path.stat().st_size > limit:
        raise _InputError(f"{path} exceeds {limit} bytes")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise _InputError(f"{path} exceeds {limit} bytes")
    return raw


def _read_json(path: Path) -> Any:
    """Read one bounded JSON document."""
    try:
        return json.loads(_read_bytes(path, _MAX_INPUT_BYTES))
    except RecursionError as error:
        raise _InputError(f"{path} is nested too deeply") from error


def _decide(arguments: argparse.Namespace, limits: EntailmentLimits) -> int:
    from .search import decode_dictionary
    from .service import decide_entailment

    goal = decode_goal(_read_json(arguments.goal), limits)
    dictionary = None
    if arguments.dictionary is not None:
        dictionary = decode_dictionary(
            _read_json(arguments.dictionary), goal.context, limits
        )
    result = decide_entailment(
        goal, limits, dictionary=dictionary, project=arguments.lean_project
    )
    if result.artifact is not None and arguments.artifact is not None:
        arguments.artifact.write_bytes(result.artifact)
    print(
        json.dumps(
            {
                "status": result.status.value,
                "reason": result.reason.value if result.reason else None,
                "axioms": list(result.axioms),
                "lanes": [lane.as_dict() for lane in result.lanes],
                "limits": result.limits,
            },
            indent=2,
        )
    )
    return 0


def _replay(arguments: argparse.Namespace, limits: EntailmentLimits) -> int:
    goal = decode_goal(_read_json(arguments.goal), limits)
    # One byte over the limit is enough for replay to refuse the artifact.
    with arguments.artifact.open("rb") as stream:
        artifact = stream.read(limits.max_artifact_bytes + 1)
    outcome = replay_artifact(artifact, goal, limits, arguments.lean_project)
    if isinstance(outcome, ValidatedEntailment):
        report = {
            "outcome": "validated",
            "status": outcome.status,
            "axioms": list(outcome.axioms),
        }
    elif isinstance(outcome, InvalidEvidence):
        report = {"outcome": "invalid", "reason": outcome.reason}
    else:
        report = {"outcome": "unavailable", "reason": outcome.reason}
    print(json.dumps(report, indent=2))
    return 0 if isinstance(outcome, ValidatedEntailment) else 1


def main(argv: list[str] | None = None) -> int:
    """Run one command."""
    parser = argparse.ArgumentParser(prog="python -m glitnir_forseti.entailment")
    commands = parser.add_subparsers(dest="command", required=True)
    decide = commands.add_parser("decide", help="search, then check in Lean")
    decide.add_argument("goal", type=Path)
    decide.add_argument("--dictionary", type=Path)
    decide.add_argument("--artifact", type=Path)
    check = commands.add_parser("replay", help="check an artifact against a goal")
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
