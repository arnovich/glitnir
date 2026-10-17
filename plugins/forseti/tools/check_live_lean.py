"""Fail the integration gate unless every pinned replay closure is available."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from glitnir.lean.sandbox import sandbox_available

from glitnir_forseti.lean.closure import replay_pin, verify_closure
from glitnir_forseti.lean.independent import IndependentStatus
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.lean_release import verify_project


def check(project: Path, repl: Path) -> None:
    """Verify sources, executable, confinement and every compiled import set."""
    verify_project(project)
    if not repl.is_file() or not os.access(repl, os.X_OK):
        raise SystemExit(f"missing REPL executable: {repl}")
    if not sandbox_available():
        raise SystemExit("live Lean gate requires a working sandbox")
    limits = SessionLimits(timeout_ms=300_000)
    closures = replay_pin().get("closures")
    if not isinstance(closures, list) or not closures:
        raise SystemExit("replay pin has no import closures to check")
    for entry in closures:
        imports = tuple(entry["imports"])
        result = verify_closure(imports, limits, project)
        if result.status is not IndependentStatus.CONFIRMED:
            raise SystemExit(f"unavailable replay closure {imports}: {result.reason}")
    print("All pinned Lean replay closures are available")


def main() -> None:
    """Require explicit paths so missing configuration cannot skip the gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--repl", type=Path, required=True)
    args = parser.parse_args()
    check(args.project.resolve(), args.repl.resolve())


if __name__ == "__main__":
    main()
