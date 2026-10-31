"""Validate an atlas sidecar with the installed Glitnir backend's own validator."""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    """Check atlas structure and outline bindings without compiling Lean."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outline", type=Path)
    args = parser.parse_args()
    try:
        from glitnir.preview.atlas import load_atlas
    except ImportError:
        parser.error(
            "use the Python interpreter of a Glitnir runtime with proof atlas support"
        )
    try:
        outline = json.loads(args.outline.read_bytes())
        if not isinstance(outline, dict):
            raise ValueError("outline must be an object")
        atlas = load_atlas(args.outline, outline)
        if atlas is None:
            raise ValueError("atlas.json is missing beside the outline")
    except (OSError, ValueError) as error:
        print(f"Invalid proof atlas: {error}", file=sys.stderr)
        return 1
    print(
        f"Valid proof atlas: {len(atlas['nodes'])} arguments, {len(atlas['routes'])} curated routes. No proof status granted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
