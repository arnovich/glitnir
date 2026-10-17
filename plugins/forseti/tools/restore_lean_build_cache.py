"""Restore the audited large K5 proof before the complete CI build.

The archive contains only Lake outputs for one module. Lake still validates
source/dependency freshness, and CI independently checks every replay closure.
No native executable or Lake configuration is restored.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
from pathlib import Path

ARCHIVE_SHA256 = "0e40d767cd0dbd7b29f878bd2cd4610db287aa18846571fb4bfb816e523b67d6"
MODULE = "Gimle/Forseti/Examples/GalerkinNS/K5Below6"
MEMBERS = frozenset(
    [
        f"lib/lean/{MODULE}{suffix}"
        for suffix in (".olean", ".olean.hash", ".ilean", ".ilean.hash", ".trace")
    ]
    + [f"ir/{MODULE}{suffix}" for suffix in (".c", ".c.hash")]
)


def restore_cache(archive: Path, project: Path, expected_sha256: str) -> None:
    """Check the immutable digest and restore exactly seven regular outputs."""
    with archive.open("rb") as archive_source:
        digest = hashlib.file_digest(archive_source, "sha256").hexdigest()
    if digest != expected_sha256:
        raise ValueError("Lean build cache checksum mismatch")
    project = project.resolve(strict=True)
    build = project / ".lake/build"
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if (
            len(members) != len(MEMBERS)
            or {member.name for member in members} != MEMBERS
            or any(not member.isfile() for member in members)
        ):
            raise ValueError(
                "Lean build cache must contain exactly the audited outputs"
            )
        destinations = [build / member.name for member in members]
        if any(
            path.is_symlink() or not path.resolve().is_relative_to(project)
            for path in destinations
        ):
            raise ValueError("Lean build cache destination escapes the project")
        for member, destination in zip(members, destinations, strict=True):
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("Lean build cache member has no content")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


def main() -> None:
    """Restore the reviewed release asset supplied by CI."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    restore_cache(args.archive, args.project, ARCHIVE_SHA256)
    print(
        "Restored seven audited K5 build outputs; full build and replay audit required"
    )


if __name__ == "__main__":
    main()
