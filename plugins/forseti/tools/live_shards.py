"""Split the plugin's test modules into CI's ``plugin-live`` shards.

Usage, from ``plugins/forseti``::

    python3 tools/live_shards.py --names     # the shard names, as a JSON list
    python3 tools/live_shards.py SHARD       # that shard's test modules

Each named shard holds the slow Lean modules listed here; the ``rest`` shard
holds every other test module, so together the shards run the whole suite
exactly once. A listed module that does not exist, or one listed twice, fails
here rather than silently leaving the suite smaller. The CI matrix is built
from ``--names``, so it cannot drift from this list. Only the standard library
is used: the matrix is computed before anything is installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The slow Lean modules, grouped so each shard finishes well within CI's limit.
SHARDS: dict[str, tuple[str, ...]] = {
    "stepped": ("tests/test_stepped_lean.py",),
    "streams": (
        "tests/test_streams_lean.py",
        "tests/test_mild_stream_lean.py",
        "tests/test_stream_decision_lean.py",
    ),
    "trajectory": (
        "tests/test_trajectory_lean.py",
        "tests/test_classical_euler_lean.py",
    ),
    "decisions": (
        "tests/test_decision_provider_lean.py",
        "tests/test_entailment_lean.py",
        "tests/test_evidence_provider_lean.py",
        "tests/test_views_lean.py",
    ),
}
#: The shard holding every module no named shard lists.
REST = "rest"


def test_modules(root: Path = ROOT) -> list[str]:
    """Return every test module of the suite, relative to ``root``."""
    return sorted(
        path.relative_to(root).as_posix() for path in root.glob("tests/test_*.py")
    )


def shard_modules(name: str, root: Path = ROOT) -> list[str]:
    """Return the test modules of one shard.

    Raises:
        ValueError: If the shard is unknown, or a listed module is missing or
            listed twice.
    """
    every = test_modules(root)
    listed = [module for modules in SHARDS.values() for module in modules]
    missing = sorted(set(listed) - set(every))
    if missing:
        raise ValueError(f"shards list modules that do not exist: {missing}")
    if len(set(listed)) != len(listed):
        raise ValueError("a module is listed in more than one shard")
    if name == REST:
        return [module for module in every if module not in listed]
    if name not in SHARDS:
        raise ValueError(f"unknown shard: {name}")
    return list(SHARDS[name])


def main(arguments: list[str]) -> int:
    """Print the shard names or one shard's modules.

    Returns:
        Zero, or two on a usage or shard error.
    """
    if len(arguments) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if arguments[0] == "--names":
            for name in (*SHARDS, REST):
                shard_modules(name)
            print(json.dumps([*SHARDS, REST]))
        else:
            print("\n".join(shard_modules(arguments[0])))
    except ValueError as error:
        print(f"live_shards: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
