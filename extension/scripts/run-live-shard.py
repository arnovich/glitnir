"""Partition the complete live suite while keeping module fixtures together."""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


def shard_key(node: str) -> str:
    """Split independent notebook checks; keep shared corpus fixtures together."""
    module = node.split("::", 1)[0]
    if module in {
        "tests/test_example_notebooks.py",
        "tests/test_linear_example_notebooks.py",
    }:
        return node
    return module


def shard_index(node: str, count: int) -> int:
    """Keep the three eager decision corpora on distinct normal CI shards."""
    corpora = (
        "tests/test_streams_lean.py",
        "tests/test_trajectory_lean.py",
        "tests/test_entailment_lean.py",
    )
    key = shard_key(node)
    if key in corpora:
        return corpora.index(key) % count
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % count


def main() -> None:
    """Collect once, record the selected partition, then run it with diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    if not 0 <= args.index < args.count:
        parser.error("shard index must be within the positive shard count")
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args.paths],
        capture_output=True,
        text=True,
    )
    if collected.returncode:
        print(collected.stdout, end="")
        print(collected.stderr, end="", file=sys.stderr)
        collected.check_returncode()
    nodes = [
        line
        for line in collected.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    ]
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("Live collection is empty or contains duplicate node IDs")
    selected = [node for node in nodes if shard_index(node, args.count) == args.index]
    if not selected:
        raise ValueError("Live shard is empty")
    identity = hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()
    print(
        f"Shard {args.index}/{args.count}: {len(selected)} of {len(nodes)} tests; "
        f"collection {identity}",
        flush=True,
    )
    Path(f"live-shard-{args.index}.txt").write_text("\n".join(selected) + "\n")
    if not args.list_only:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-v",
                "--maxfail=1",
                "--tb=short",
                "--durations=25",
                f"--junitxml=live-shard-{args.index}.xml",
                *selected,
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
