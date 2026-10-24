"""Inspect an installed Glitnir environment without running Lean or Lake."""

import json
import sys
from pathlib import Path


def main() -> None:
    """Return a small environment description or an actionable error."""
    try:
        if sys.version_info < (3, 12):
            raise ValueError("Glitnir requires Python 3.12 or newer")
        if len(sys.argv) > 1:
            raise ValueError("The environment probe takes no arguments")
        from glitnir.service import server
        from glitnir.preview.editor import runtime_info

        expected = json.loads(
            Path(__file__).with_name("editor-runtime.json").read_text()
        )
        editor = runtime_info()
        if editor != expected:
            raise ValueError(
                "The installed Glitnir runtime is incompatible with this Glitnir build. "
                "Install its matching runtime or rebuild Glitnir against this runtime."
            )

        source = Path(server.__file__).resolve()
        source_root = next(
            (p for p in source.parents if (p / "pyproject.toml").is_file()), None
        )
        result = {
            "python": sys.executable,
            "editor": editor,
            "sourceRoot": str(source_root) if source_root else None,
        }
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
