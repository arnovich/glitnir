"""Describe nearby Lean configuration without evaluating it or claiming readiness."""

import hashlib
import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Literal, TypedDict

MAX_CONFIG_BYTES = 1024 * 1024
MAX_ANCESTORS = 64


def read_regular(path: Path, limit: int = MAX_CONFIG_BYTES) -> bytes:
    """Read a bounded regular file; refuse symlinks and nonregular descriptors."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


class ProjectFile(TypedDict, total=False):
    """One configuration file: its digest and declaration, or its read error."""

    sha256: str
    declaration: str
    error: str


class LakeProject(TypedDict):
    """The nearest Lake project of a file: its root and its configuration files."""

    kind: Literal["lake"]
    root: str
    toolchain: str | None
    files: dict[str, ProjectFile]


class LooseProject(TypedDict):
    """A file outside any Lake project: the toolchain declared beside it, if any."""

    kind: Literal["loose"]
    root: str | None
    toolchain: str | None
    files: dict[str, ProjectFile]


#: What `describe_project` returns.
ProjectDescription = LakeProject | LooseProject


def _file(path: Path) -> ProjectFile:
    """Describe one configuration file; errors are observations, never fallback."""
    try:
        data = read_regular(path)
        text = data.decode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if path.suffix == ".toml":
            tomllib.loads(text)
        elif path.name == "lake-manifest.json":
            if not isinstance(json.loads(text), dict):
                raise ValueError("manifest must be a JSON object")
        result: ProjectFile = {"sha256": digest}
        if path.name == "lean-toolchain":
            if not text.strip() or len(text.strip().splitlines()) != 1:
                raise ValueError("expected one toolchain declaration")
            result["declaration"] = text.strip()
        return result
    except FileNotFoundError:
        return {"error": "missing"}
    except (OSError, ValueError, RecursionError) as error:
        return {"error": str(error)}


def _present(path: Path) -> bool:
    """Treat unreadable and nonregular markers as project boundaries too."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def describe_project(source: Path) -> ProjectDescription:
    """Find the nearest Lake boundary, or describe a loose file's toolchain."""
    directory = source.absolute().parent
    loose: Path | None = None
    for _ in range(MAX_ANCESTORS):
        names = ("lakefile.toml", "lakefile.lean")
        if any(_present(directory / name) for name in names):
            files = {
                name: _file(directory / name)
                for name in (*names, "lean-toolchain", "lake-manifest.json")
            }
            return {
                "kind": "lake",
                "root": str(directory),
                "files": files,
                "toolchain": files["lean-toolchain"].get("declaration"),
            }
        if loose is None and _present(directory / "lean-toolchain"):
            loose = directory
        if directory.parent == directory:
            break
        directory = directory.parent
    toolchain: ProjectFile = (
        _file(loose / "lean-toolchain") if loose else {"error": "missing"}
    )
    return {
        "kind": "loose",
        "root": str(loose) if loose else None,
        "toolchain": toolchain.get("declaration"),
        "files": {"lean-toolchain": toolchain},
    }
