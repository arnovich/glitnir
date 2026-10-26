"""Source and prepared-dependency bindings shared by clients and workers."""

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from uuid import UUID, uuid4

from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_inspection import TOOLCHAIN, lean_support

MAX_SOURCE = 1_000_000
MAX_CONTEXT_FILES = 30000
MAX_CONTEXT_BYTES = 512 * 1024 * 1024
CONFIG_FILES = (
    "lakefile.toml",
    "lakefile.lean",
    "lean-toolchain",
    "lake-manifest.json",
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def digest(data: bytes) -> str:
    """Hash exact bytes."""
    return hashlib.sha256(data).hexdigest()


def source_path(value: Any) -> Path:
    """Accept only a bounded relative Lean source path."""
    if (
        not isinstance(value, str)
        or len(value) > 512
        or not value.endswith(".lean")
        or PurePosixPath(value).name == "lakefile.lean"
        or PurePosixPath(value).is_absolute()
        or any(not p or p.startswith(".") for p in value.split("/"))
        or any(c in value for c in ("\\", "\x00", ":"))
    ):
        raise ValueError("Invalid relative Lean source path")
    return Path(value)


def checker_digest() -> str:
    """Identify the installed Python checker and its Lean helper programs."""
    root = Path(__file__).resolve().parents[1]
    files = {
        p.relative_to(root).as_posix(): digest(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.suffix in {".py", ".lean", ".template"} and p.is_file()
    }
    return digest(
        json.dumps({"toolchain": TOOLCHAIN, "files": files}, sort_keys=True).encode()
    )


def project_root(source: Path) -> Path:
    """Find the saved source's project without executing its configuration."""
    project = describe_project(source)
    return (
        Path(project["root"]) if project["kind"] == "lake" else source.absolute().parent
    )


def _packages(root: Path) -> list[tuple[str, Path, Path]]:
    """Read the root's flattened Lake dependency manifest without running Lake."""
    if (root / ".lake/package-overrides.json").exists():
        raise ValueError("Remote checks do not support Lake dependency overrides")
    path = root / "lake-manifest.json"
    if not path.exists():
        return []
    manifest = json.loads(read_regular(path))
    packages = manifest.get("packages", [])
    if not isinstance(packages, list) or len(packages) > 256:
        raise ValueError("Invalid prepared dependency manifest")
    found = []
    for entry in packages:
        if (
            entry.get("configFile", "lakefile.lean")
            not in {"lakefile.lean", "lakefile.toml"}
            or entry.get("manifestFile", "lake-manifest.json") != "lake-manifest.json"
        ):
            raise ValueError(
                "Remote checks require standard Lake configuration filenames"
            )
        name = entry.get("name", "")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("Invalid dependency name")
        relative = Path(
            entry.get("dir")
            or str(Path(manifest.get("packagesDir", ".lake/packages")) / name)
        )
        location = root / relative
        if entry.get("subDir"):
            location = location / entry["subDir"]
            relative = relative / entry["subDir"]
        if (
            relative.is_absolute()
            or relative.parts[:2] != (".lake", "packages")
            or ".." in relative.parts
            or not location.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("Remote checks require dependencies under .lake/packages")
        if not location.is_dir():
            raise ValueError(f"Prepared dependency {name} is missing")
        found.append((name, location, relative))
    return found


def _sources(root: Path) -> Iterator[Path]:
    """Read Lean sources only, excluding builds, VCS state and tool environments."""
    visited = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        visited += len(directories) + len(files)
        if visited > 200000:
            raise ValueError("Prepared source tree exceeds traversal limit")
        directories[:] = sorted(
            d
            for d in directories
            if not d.startswith(".") and d not in {"node_modules", "__pycache__"}
        )
        for directory in directories:
            if (Path(parent) / directory).is_symlink():
                raise ValueError(
                    "Prepared source directories must not be symbolic links"
                )
        for name in sorted(files):
            if name.endswith(".lean") and name not in CONFIG_FILES:
                yield Path(parent) / name


def context_digest(source: Path) -> str:
    """Bind every prepared source/configuration byte except the submitted candidate.

    Dependency sources are hashed, including saved uncommitted edits. Compiled
    artifacts are native to the worker; Glitnir's full check captures and replays
    those artifacts independently. Provisioning/building them is an operator step.
    """
    root = project_root(source)
    entries: dict[str, str] = {}
    total = 0
    roots = [
        ("project", root),
        *((f"packages/{name}", path) for name, path, _ in _packages(root)),
    ]
    for prefix, base in roots:
        if (base / ".lake/package-overrides.json").exists():
            raise ValueError("Remote checks do not support Lake dependency overrides")
        files = [base / name for name in CONFIG_FILES if (base / name).exists()]
        for path in [*files, *_sources(base)]:
            if path.absolute() == source.absolute():
                continue
            data = read_regular(path, 8 * 1024 * 1024)
            total += len(data)
            entries[f"{prefix}/{path.relative_to(base).as_posix()}"] = digest(data)
            if len(entries) > MAX_CONTEXT_FILES or total > MAX_CONTEXT_BYTES:
                raise ValueError("Prepared dependency context exceeds its size limit")
    return digest(
        b"glitnir.compute-context/v1\0"
        + json.dumps(
            {"toolchain": TOOLCHAIN, "files": entries}, sort_keys=True
        ).encode()
    )


def make_request(
    source: Path,
    environment: str,
    seconds: int,
    memory: int,
    through_line: int | None = None,
) -> dict[str, Any]:
    """Freeze one candidate and its context before it is sent or edited again."""
    source = source.absolute()
    message = lean_support(describe_project(source))["message"]
    if message is not None:
        raise ValueError(message)
    relative = source_path(source.relative_to(project_root(source)).as_posix())
    text = read_regular(source, MAX_SOURCE).decode().replace("\r\n", "\n")
    request = {
        "request_id": str(uuid4()),
        "kind": "glitnir.check/v1",
        "environment": environment,
        "checker_sha256": checker_digest(),
        "context_sha256": context_digest(source),
        "source_sha256": digest(text.encode()),
        "source": text,
        "path": relative.as_posix(),
        "through_line": through_line,
        "timeout_seconds": seconds,
        "memory_mb": memory,
    }
    validate_request(request)
    return request


def validate_request(request: dict[str, Any]) -> None:
    """Validate a server-delivered job again before touching worker files."""
    source_path(request.get("path"))
    if str(UUID(request.get("request_id", ""))) != request["request_id"]:
        raise ValueError("Invalid request UUID")
    if request.get("kind") != "glitnir.check/v1" or not _NAME.fullmatch(
        str(request.get("environment", ""))
    ):
        raise ValueError("Unsupported computation kind or environment")
    for name in ("checker_sha256", "context_sha256", "source_sha256"):
        if not isinstance(request.get(name), str) or not _HASH.fullmatch(request[name]):
            raise ValueError(f"Invalid {name}")
    text = request.get("source")
    if (
        not isinstance(text, str)
        or len(text.encode()) > MAX_SOURCE
        or "\r" in text
        or digest(text.encode()) != request["source_sha256"]
    ):
        raise ValueError("Candidate source hash or size is invalid")
    for name, low, high in (("timeout_seconds", 1, 1800), ("memory_mb", 1024, 32768)):
        if type(request.get(name)) is not int or not low <= request[name] <= high:
            raise ValueError(f"Invalid {name}")
    if request.get("through_line") is not None and (
        type(request["through_line"]) is not int or request["through_line"] < 1
    ):
        raise ValueError("Invalid through_line")


def _copy_regular(
    source: str, destination: str, monitor: Callable[[], None] = lambda: None
) -> str:
    path = Path(source)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Prepared artifacts must be regular files")
    with path.open("rb") as incoming, Path(destination).open("wb") as outgoing:
        while chunk := incoming.read(1024 * 1024):
            monitor()
            outgoing.write(chunk)
            outgoing.flush()
            monitor()
    shutil.copystat(source, destination)
    return destination


def stage_project(
    prepared: Path,
    request: dict[str, Any],
    destination: Path,
    *,
    monitor: Callable[[], None] = lambda: None,
) -> Path:
    """Reuse one private, materialized project per serial worker process.

    Dependencies/builds are copied once, never linked to mutable operator files.
    Native inspection and replay retain their existing confinement and caches.
    A journal restores the previous candidate even after an interrupted child.
    """
    validate_request(request)
    monitor()
    source = prepared / source_path(request["path"])
    if project_root(source).resolve() != prepared.resolve():
        raise ValueError("Source path crosses a nested project boundary")
    if context_digest(source) != request["context_sha256"]:
        raise ValueError(
            "Prepared dependency context differs from the submitted context"
        )
    root = destination / "project"
    marker = destination / "candidate.json"
    if not destination.exists():
        destination.mkdir(parents=True, mode=0o700)
        root.mkdir()
        for path in [
            *(prepared / name for name in CONFIG_FILES if (prepared / name).exists()),
            *_sources(prepared),
        ]:
            target = root / path.relative_to(prepared)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read_regular(path, 8 * 1024 * 1024))
            monitor()
        if (prepared / ".lake").exists():
            # Reject links before copytree can follow them. Ignore repository metadata.
            for parent, directories, files in os.walk(prepared / ".lake"):
                monitor()
                directories[:] = [d for d in directories if d != ".git"]
                if any(
                    (Path(parent) / name).is_symlink()
                    for name in [*directories, *files]
                ):
                    raise ValueError(
                        "Prepared build trees must not contain symbolic links"
                    )
            shutil.copytree(
                prepared / ".lake",
                root / ".lake",
                copy_function=partial(_copy_regular, monitor=monitor),
                ignore=shutil.ignore_patterns(".git"),
            )
        (destination / "ready").touch()
    monitor()
    if not (destination / "ready").is_file():
        raise ValueError("Worker staging was interrupted; restart with a new journal")
    if marker.exists():
        previous = source_path(json.loads(marker.read_text())["path"])
        original = prepared / previous
        if original.exists():
            (root / previous).write_bytes(read_regular(original, MAX_SOURCE))
        else:
            (root / previous).unlink(missing_ok=True)
    target = root / source_path(request["path"])
    if context_digest(target) != request["context_sha256"]:
        raise ValueError("Private prepared context differs; restart this worker")
    marker.write_text(json.dumps({"path": request["path"]}))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(request["source"])
    if context_digest(target) != request["context_sha256"]:
        raise ValueError("Prepared dependency context changed while staging")
    monitor()
    return target
