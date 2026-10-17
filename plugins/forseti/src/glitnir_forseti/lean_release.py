"""Installed Python policy for the standalone Lean release; never downloads code.

The packaged inventory selects expected bytes. A chosen checkout cannot declare
itself trusted. Source verification does not authenticate compiled cache objects;
foreign enrollment retains its explicit installed-environment trust boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath

RELEASE_BYTES = files("glitnir_forseti").joinpath("lean-release.v1.json").read_bytes()
RELEASE_SHA256 = hashlib.sha256(RELEASE_BYTES).hexdigest()
LOCKFILE_BYTES = files("glitnir_forseti").joinpath("lean-lockfile.v1.json").read_bytes()
_RELEASE = json.loads(RELEASE_BYTES)
TOOLCHAIN: str = _RELEASE["toolchain"]
SUPPORT_VERSION: str = _RELEASE["support_version"]
SUPPORT_SHA256: str = _RELEASE["support_source_sha256"]
CHECKER_SHA256: str = _RELEASE["checker_sha256"]
LOCKFILE_SHA256: str = _RELEASE["lockfile_sha256"]
IMPORT_CLOSURE: tuple[tuple[str, str], ...] = tuple(
    (entry["name"], entry["revision"]) for entry in _RELEASE["dependencies"]
)
SOURCE_PINS: tuple[tuple[str, str, str], ...] = tuple(
    (entry["package"], entry["path"], entry["sha256"])
    for entry in _RELEASE["core_sources"] + _RELEASE["build_configs"]
)
MAX_SOURCE_BYTES = 1_048_576


class LeanReleaseError(ValueError):
    """Missing or incompatible explicitly selected Lean support installation."""


@dataclass(frozen=True)
class VerifiedLeanProject:
    """Owned source bytes and resolved roots from one checked installation."""

    roots: tuple[tuple[str, Path], ...]
    sources: tuple[tuple[str, str, bytes], ...]
    toolchain: bytes
    lockfile: bytes

    def source_bytes(self, package: str, relative: str) -> bytes:
        """Select already-verified bytes without rereading the mutable checkout."""
        for name, path, data in self.sources:
            if (name, path) == (package, relative):
                return data
        raise LeanReleaseError("source is outside the pinned release")


def select_project(project: Path | None) -> Path | None:
    """Prefer the explicit option over configuration; never infer a checkout."""
    if project is not None and not isinstance(project, Path):
        raise TypeError("Lean project must be a pathlib.Path")
    if project is None:
        configured = os.environ.get("FORSETI_LEAN_PROJECT")
        if not configured:
            return None
        project = Path(configured)
    return project.resolve()


def _relative(value: object) -> Path:
    if type(value) is not str or not value or "\\" in value:
        raise LeanReleaseError("invalid Lake package path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise LeanReleaseError("Lake package path escapes its root")
    return Path(path)


def package_roots(project: Path, document: dict) -> dict[str, Path]:
    """Resolve pinned Git package subdirectories without evaluating Lake code."""
    try:
        root_name = document["name"]
        if type(root_name) is not str or len(_relative(root_name).parts) != 1:
            raise LeanReleaseError("invalid root package name")
        packages = project / _relative(document.get("packagesDir", ".lake/packages"))
        roots = {root_name: project.resolve()}
        for package in document["packages"]:
            name = package["name"]
            if len(_relative(name).parts) != 1 or name in roots:
                raise LeanReleaseError("invalid or duplicate package name")
            if package["type"] != "git":
                raise LeanReleaseError(
                    "release dependencies must use pinned Git sources"
                )
            clone = (packages / name).resolve()
            relative = _relative(package.get("subDir") or ".")
            root = (clone / relative).resolve()
            if not root.is_relative_to(clone):
                raise LeanReleaseError("Lake subdirectory escapes its clone")
            roots[name] = root
        return roots
    except (KeyError, TypeError) as error:
        raise LeanReleaseError("invalid Lake dependency manifest") from error


def _read_source(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LeanReleaseError("release source must be a regular file")
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise LeanReleaseError("release source size limit")
        return raw
    except OSError as error:
        raise LeanReleaseError(f"cannot read release source: {path.name}") from error


def _lock_mismatch_detail(lockfile: bytes) -> str:
    """Explain a lock mismatch, naming checkout drift when the evidence shows it.

    A sibling checkout sitting on `main` produces exactly the same failure as a
    genuinely stale pin, and the two have opposite remedies. Comparing the
    observed dependency revisions against the pinned ones distinguishes them
    without weakening anything: every mismatch still fails closed.
    """
    try:
        observed = json.loads(lockfile)
        packages = observed["packages"]
        found = {
            package["name"]: package.get("rev")
            for package in packages
            if type(package) is dict and type(package.get("name")) is str
        }
    except (ValueError, KeyError, TypeError):
        return "its lake-manifest.json is not the pinned document"
    expected = dict(IMPORT_CLOSURE)
    moved = sorted(
        name
        for name, revision in expected.items()
        if name in found and found[name] != revision
    )
    if moved:
        name = moved[0]
        return (
            f"its {name} dependency is {str(found[name])[:12]}, not the pinned "
            f"{expected[name][:12]}"
        )
    absent = sorted(name for name in expected if name not in found)
    if absent:
        return f"its lock omits the pinned dependency {absent[0]}"
    return "its lake-manifest.json differs from the pinned bytes"


def verify_project(project: Path | None) -> VerifiedLeanProject:
    """Bind toolchain, complete lock, transitive sources and executable configs."""
    if project is None:
        raise LeanReleaseError("set --lean-project or FORSETI_LEAN_PROJECT")
    toolchain = _read_source(project / "lean-toolchain")
    if toolchain.strip() != TOOLCHAIN.encode():
        raise LeanReleaseError("unsupported Lean toolchain")
    lockfile = _read_source(project / "lake-manifest.json")
    if hashlib.sha256(lockfile).hexdigest() != LOCKFILE_SHA256:
        raise LeanReleaseError(
            "unsupported Lean dependency lock: "
            + _lock_mismatch_detail(lockfile)
            + ". A checkout at the wrong revision is the usual cause, and looks"
            " identical to a stale pin; check out the reviewed revision rather"
            " than the branch tip and rebuild before concluding the pin is stale"
        )
    roots = package_roots(project, json.loads(lockfile))
    sources = []
    for package, relative, expected in SOURCE_PINS:
        root = roots[package]
        path = root / _relative(relative)
        if not path.resolve().is_relative_to(root):
            raise LeanReleaseError("release source escapes package root")
        raw = _read_source(path)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise LeanReleaseError(f"unsupported release source: {package}/{relative}")
        sources.append((package, relative, raw))
    return VerifiedLeanProject(
        tuple(roots.items()), tuple(sources), toolchain, lockfile
    )
