"""Validate an atlas sidecar with the installed Glitnir backend's own validator."""

import argparse
import importlib
import sys
from pathlib import Path


def main() -> int:
    """Check atlas structure and outline bindings without compiling Lean."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outline", type=Path)
    parser.add_argument(
        "--report", action="store_true", help="show curated route coverage"
    )
    args = parser.parse_args()
    try:
        # The optional runtime is checked separately with its Python 3.12 target.
        load_walkthrough = importlib.import_module(
            "glitnir.preview.walkthrough"
        ).load_walkthrough
    except ImportError:
        parser.error(
            "use the Python interpreter of a Glitnir runtime with proof atlas support"
        )
    try:
        if args.outline.name != "outline.json":
            raise ValueError("pass the walkthrough outline.json used by the preview")
        walkthrough = load_walkthrough(args.outline)
        if walkthrough is None:
            raise ValueError("outline.json is missing")
        atlas = walkthrough.get("atlas")
        if args.report and atlas is not None:
            coverage = importlib.import_module("glitnir.preview.atlas").atlas_coverage
            print("Authored argument coverage (depth in support edges)")
            print("\n".join(coverage(atlas)))
        if walkthrough["problems"]:
            raise ValueError("; ".join(walkthrough["problems"]))
        if atlas is None:
            raise ValueError("atlas.json is missing beside the outline")
    except (OSError, ValueError) as error:
        print(f"Invalid proof atlas: {error}", file=sys.stderr)
        return 1
    print(
        f"Valid proof atlas: {len(atlas['nodes'])} arguments, "
        f"{len(atlas['routes'])} curated routes. No proof status granted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
