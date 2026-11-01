"""Validate an atlas sidecar with the installed Glitnir backend's own validator."""

import argparse
import importlib
import sys
from pathlib import Path


def main() -> int:
    """Check atlas structure and outline bindings without compiling Lean."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outline", type=Path)
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
        if walkthrough["problems"]:
            raise ValueError("; ".join(walkthrough["problems"]))
        atlas = walkthrough.get("atlas")
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
