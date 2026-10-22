"""Write the packaged Lean release pin from a built release checkout.

Usage, from a built release checkout named by its tag::

    uv run python tools/pin_lean_release.py PROJECT --forseti-tag vX.Y.Z
        --asgard-tag vA.B.C

The pin is by content: `verify_project` accepts only a project whose toolchain,
dependency lock, core sources and build configurations hash to what the pin
records. This script computes those hashes from a checkout that has already been
built and reviewed, and writes the three files that must move together:

- `src/glitnir_forseti/lean-release.v1.json`, the content pin;
- `src/glitnir_forseti/lean-lockfile.v1.json`, the release's `lake-manifest.json`;
- `tests/fixtures/lean-release-origin.v1.json`, which tags the pin was cut from.

`--check` compares instead of writing, and fails if any file would change.

It never downloads anything and trusts nothing it reads: the output is a
proposal a reviewer compares against the release, exactly as a hand edit would
be, only without the arithmetic.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

from glitnir_forseti.lean_release import package_roots

REPOSITORY = Path(__file__).resolve().parents[1]
PACKAGE = REPOSITORY / "src/glitnir_forseti"
RELEASE_PIN = PACKAGE / "lean-release.v1.json"
LOCKFILE_PIN = PACKAGE / "lean-lockfile.v1.json"
ORIGIN_RECORD = REPOSITORY / "tests/fixtures/lean-release-origin.v1.json"

#: The support contract version. Bump it whenever the pinned content changes.
SUPPORT_VERSION = "gimle.forseti.lean-support/v20"

#: Sources whose bytes the Python side depends on, by package and path.
SUPPORT_SOURCE = ("forseti", "Gimle/Forseti.lean")
CHECKER_SOURCE = ("forseti", "Executables/Checker.lean")
CORE_SOURCES = (
    SUPPORT_SOURCE,
    CHECKER_SOURCE,
    ("asgard", "Gimle/Asgard.lean"),
    ("asgard", "Gimle/Asgard/Circuit.lean"),
    ("asgard", "Gimle/Asgard/Semantics/Real.lean"),
    ("asgard", "Gimle/Asgard/Rewrite.lean"),
)

#: Axioms a checked proof may depend on.
AXIOM_ALLOWLIST = ("Classical.choice", "Quot.sound", "propext")


def _sha256(path: Path) -> str:
    """Return a file's SHA-256, refusing anything but a regular file."""
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"not a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_config(root: Path) -> str:
    """Return the name of a package's Lake configuration file."""
    for name in ("lakefile.toml", "lakefile.lean"):
        if (root / name).is_file():
            return name
    raise SystemExit(f"no Lake configuration in {root}")


def release_pin(project: Path) -> tuple[bytes, bytes]:
    """Compute the release pin and lockfile bytes for a built checkout.

    Args:
        project: A built forseti-lean release checkout.

    Returns:
        The release pin document and the lockfile, both as file bytes.
    """
    lockfile = (project / "lake-manifest.json").read_bytes()
    manifest = json.loads(lockfile)
    # The same resolution `verify_project` uses: git dependencies only, and no
    # package path may escape the project.
    roots = package_roots(project, manifest)
    if manifest["name"] != "forseti":
        raise SystemExit(f"expected the forseti package, found {manifest['name']}")

    def pinned(package: str, path: str) -> dict[str, str]:
        source = roots[package] / path
        if not source.resolve().is_relative_to(roots[package]):
            raise SystemExit(f"pinned source escapes its package: {source}")
        return {"package": package, "path": path, "sha256": _sha256(source)}

    build_configs = [pinned("forseti", _build_config(project))] + [
        pinned(package["name"], _build_config(roots[package["name"]]))
        for package in manifest["packages"]
    ]
    document = {
        "schema": "gimle.forseti.lean-release/v1",
        "support_version": SUPPORT_VERSION,
        "toolchain": (project / "lean-toolchain").read_text().strip(),
        "support_source_sha256": _sha256(roots["forseti"] / SUPPORT_SOURCE[1]),
        "checker_sha256": _sha256(roots["forseti"] / CHECKER_SOURCE[1]),
        "lockfile_sha256": hashlib.sha256(lockfile).hexdigest(),
        "import": "Gimle.Forseti",
        "axiom_allowlist": list(AXIOM_ALLOWLIST),
        "dependencies": [
            {"name": package["name"], "revision": package["rev"]}
            for package in manifest["packages"]
        ],
        "core_sources": [pinned(package, path) for package, path in CORE_SOURCES],
        "build_configs": build_configs,
    }
    return (json.dumps(document, indent=2) + "\n").encode(), lockfile


def origin_record(
    lockfile: bytes, forseti_tag: str, asgard_tag: str, verified_on: str
) -> bytes:
    """Record which tags the pin was cut from."""
    manifest = json.loads(lockfile)
    asgard = next(p for p in manifest["packages"] if p["name"] == "asgard")
    document = {
        "schema": "gimle.forseti.lean-release-origin/v1",
        "comment": (
            "Which revisions the packaged pin was cut from. The pin itself is by "
            "content; this records the answer to 'which checkout satisfies it?' "
            "so it is not lost."
        ),
        "forseti_lean_tag": forseti_tag,
        "asgard_lean_tag": asgard_tag,
        "lockfile_sha256": hashlib.sha256(lockfile).hexdigest(),
        "asgard_revision": asgard["rev"],
        "verified_on": verified_on,
    }
    return (json.dumps(document, indent=2) + "\n").encode()


def main() -> int:
    """Write or check the pin files.

    Returns:
        Zero, or one when `--check` finds a file that would change.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("project", type=Path)
    parser.add_argument("--forseti-tag", required=True)
    parser.add_argument("--asgard-tag", required=True)
    parser.add_argument("--verified-on", default=datetime.date.today().isoformat())
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()

    project = arguments.project.resolve()
    pin, lockfile = release_pin(project)
    outputs = {
        RELEASE_PIN: pin,
        LOCKFILE_PIN: lockfile,
        ORIGIN_RECORD: origin_record(
            lockfile,
            arguments.forseti_tag,
            arguments.asgard_tag,
            arguments.verified_on,
        ),
    }
    stale = [path for path, data in outputs.items() if path.read_bytes() != data]
    if arguments.check:
        for path in stale:
            print(f"would change: {path.relative_to(REPOSITORY)}")
        return 1 if stale else 0
    for path, data in outputs.items():
        path.write_bytes(data)
        print(f"wrote {path.relative_to(REPOSITORY)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
