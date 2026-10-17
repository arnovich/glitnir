"""Check or serve a saved Lean file in its Lake project.

Usage::

    python -m glitnir.notebook check FILE.lean
    python -m glitnir.notebook serve FILE.lean [--port PORT]

``check`` elaborates the saved file in its Lake project and prints the result
as JSON; it writes nothing. ``serve`` starts the editor API at
``http://127.0.0.1:PORT/``; it does not serve a browser UI.
"""

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from glitnir.lean.project import read_regular
from glitnir.lean.project_checking import check_project
from glitnir.lean.scratch import sweep_stale_scratch
from glitnir.notebook.server import serve

#: The largest saved file ``check`` reads.
MAX_SOURCE_BYTES = 16 * 1024 * 1024


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments without the program name; ``sys.argv[1:]`` when None.

    Returns:
        The process exit status: 0 when every theorem checked, 1 when the
        check ran but did not check everything (including claims withheld
        after a failing command), 2 when it could not run.
    """
    parser = argparse.ArgumentParser(
        prog="python -m glitnir.notebook",
        description="Check or serve a saved Lean file in its Lake project.",
    )
    parser.add_argument("command", choices=("check", "serve"))
    parser.add_argument("notebook", type=Path)
    parser.add_argument(
        "--port", type=int, default=8421, help="port for serve (default 8421)"
    )
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(args.notebook, args.port)
        return 0
    sweep_stale_scratch()
    try:
        text = (
            read_regular(args.notebook, MAX_SOURCE_BYTES).decode().replace("\r\n", "\n")
        )
        result = check_project(
            args.notebook, expected_sha256=hashlib.sha256(text.encode()).hexdigest()
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    if result["status"] != "checked_snapshot":
        return 2
    complete = not result.get("withheld")
    checked = all(t["status"] == "checked" for t in result["theorems"])
    return 0 if complete and checked else 1


if __name__ == "__main__":
    sys.exit(main())
