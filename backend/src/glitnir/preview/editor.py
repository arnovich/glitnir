"""Installed renderer export and compatibility identity for editor clients.

The API version describes the notebook HTTP/JSON and renderer bridge contract,
not the Lean release. Bump it when an incompatible client change is required.
The renderer digest binds a packaged editor to the assets it was tested with.
Neither value grants proof authority or replaces release/replay verification.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import TypedDict

EDITOR_API_VERSION = 2
_APP = Path(__file__).with_name("app")


class RuntimeInfo(TypedDict):
    """Compatibility fields shared by an editor build and runtime discovery."""

    apiVersion: int
    rendererSha256: str


def renderer_directory() -> Path:
    """Return the directory holding the packaged renderer (`app/`).

    This is the supported way for a gallery to locate the renderer's assets
    (`app.js`, the stylesheets, `katex/`); do not reach for private names.
    """
    return _APP


def _assets() -> list[Path]:
    """Return only shipped assets, in a path-stable order."""
    return sorted(
        path
        for path in _APP.rglob("*")
        if path.is_file()
        and not any(
            part.startswith(".")
            or part.endswith(("~", ".swp"))
            or part == "__pycache__"
            for part in path.relative_to(_APP).parts
        )
    )


def runtime_info() -> RuntimeInfo:
    """Identify the API and exact renderer independently of Lean's pin."""
    digest = hashlib.sha256()
    for source in _assets():
        relative = source.relative_to(_APP).as_posix().encode()
        data = source.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return {"apiVersion": EDITOR_API_VERSION, "rendererSha256": digest.hexdigest()}


def export_renderer(destination: Path) -> RuntimeInfo:
    """Copy installed assets into a new directory, refusing existing paths."""
    destination.mkdir(parents=True, exist_ok=False)
    for source in _assets():
        target = destination / source.relative_to(_APP)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    info = runtime_info()
    (destination / "editor-runtime.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8"
    )
    return info


def main() -> None:
    """Print compatibility information or export an installed renderer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="new asset directory to create")
    args = parser.parse_args()
    info = export_renderer(args.export) if args.export else runtime_info()
    print(json.dumps(info))


if __name__ == "__main__":
    main()
