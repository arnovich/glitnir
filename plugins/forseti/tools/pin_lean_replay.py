"""Write the replay pin: the hashed import closures of every replay.

Usage, from a built release checkout that already satisfies the release pin::

    uv run python tools/pin_lean_replay.py PROJECT [--check]

Replay of an entailment artifact imports ``Gimle.Forseti.Syntax.Certificate``,
and replay of a trajectory root also imports its model's Lean module; each
import set's closure is thousands of modules. `glitnir_forseti.lean.closure`
hashes every artifact Lean loads for them, and every package source, into one
digest per package; this script records those digests, per import set, in
``src/glitnir_forseti/lean-replay.v1.json``. The toolchain binaries are pinned
per platform, under the toolchain they belong to: running this on another
platform adds that platform's digest and keeps the rest, and fails if the
closures there are not byte-identical. A release bump keeps every platform's
digest while the toolchain stays the same; changing the toolchain keeps only
the platform the pin is made on. `.github/workflows/forseti-pins.yml` runs
this on Linux: it records ``Linux-x86_64`` and keeps ``Darwin-arm64`` while
the toolchain is unchanged; after a toolchain change, run this on macOS too.

The replay pin is separate from ``lean-release.v1.json`` and names that pin's
sha256, so the two move together and neither widens the other. Run it after
`tools/pin_lean_release.py` whenever the release pin changes. `--check`
compares instead of writing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from glitnir_forseti.entailment.render import ALGEBRAIC_IMPORTS, IMPORTS, PSATZ_IMPORTS
from glitnir_forseti.lean.closure import ClosureObservation, build_pin, observe_closure
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.stepped.models import replay_imports as stepped_imports
from glitnir_forseti.streams.models import replay_imports as stream_imports
from glitnir_forseti.trajectory.models import replay_imports

REPOSITORY = Path(__file__).resolve().parents[1]
REPLAY_PIN = REPOSITORY / "src/glitnir_forseti/lean-replay.v1.json"


def main() -> int:
    """Write or check the replay pin.

    Returns:
        Zero, or one when the closure cannot be observed or `--check` finds the
        pin would change.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("project", type=Path)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()

    observations = []
    import_sets = {
        IMPORTS,
        ALGEBRAIC_IMPORTS,
        PSATZ_IMPORTS,
        *replay_imports(),
        *stream_imports(),
        *stepped_imports(),
    }
    for imports in sorted(import_sets):
        observed = observe_closure(
            imports, SessionLimits(timeout_ms=300_000), arguments.project.resolve()
        )
        if not isinstance(observed, ClosureObservation):
            print(f"cannot observe {imports}: {observed.reason}", file=sys.stderr)
            return 1
        observations.append(observed)
    previous = json.loads(REPLAY_PIN.read_bytes()) if REPLAY_PIN.is_file() else None
    try:
        pin = build_pin(observations, previous)
    except ValueError as error:
        print(f"cannot pin: {error}", file=sys.stderr)
        return 1
    data = (json.dumps(pin, indent=2) + "\n").encode()
    if arguments.check:
        stale = not REPLAY_PIN.is_file() or REPLAY_PIN.read_bytes() != data
        if stale:
            print(f"would change: {REPLAY_PIN.relative_to(REPOSITORY)}")
        return 1 if stale else 0
    REPLAY_PIN.write_bytes(data)
    sizes = ", ".join(str(o.modules) for o in observations)
    print(f"wrote {REPLAY_PIN.relative_to(REPOSITORY)} ({sizes} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
