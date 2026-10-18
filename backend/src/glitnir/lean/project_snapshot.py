"""Bounded snapshots of project-checker inputs, linked to the artifact store.

A snapshot is a private directory tree for one check. Each toolchain file and
compiled input in it is a symbolic link to the immutable file in the artifact
store (`artifact_store`) whose name is the SHA-256 of the bytes hashed while
capturing: toolchain files in the shared toolchain namespace, the project's in
its own. The ``lean`` executable, which the loader locates its libraries from,
and the checked source are new private files. The project's sources and
configuration are hashed into the snapshot's identity and not kept. No snapshot
entry is a hard link.
"""

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from glitnir.lean import artifact_store
from glitnir.lean.artifact_store import ArtifactStore, StoreError
from glitnir.lean.limits import check_seconds
from glitnir.lean.project import read_regular

# Snapshots link to stored files rather than copying them, and the store keeps
# its own size and free-space budget; a whole-Mathlib project with large
# dependency modules passes 8 GiB.
MAX_SNAPSHOT_BYTES = 32 * 1024**3
MAX_SNAPSHOT_FILES = 100_000
MAX_FILE_BYTES = 1024**3
#: Inputs up to this size are hashed and, when new, stored from one read.
BUFFERED_BYTES = 64 * 1024**2
# Checks, views and evidence runs share one slot; a refusal says nothing
# about the notebook.
SLOT_BUSY = "Another Lean run is in progress; retry when it finishes."
_MODULE = re.compile(r"[A-Za-z_][A-Za-z_0-9']*(?:\.[A-Za-z_][A-Za-z_0-9']*)*\Z")


class SnapshotError(ValueError):
    """The checker could not capture a bounded environment."""


#: What a caller waiting for a busy slot reports.
SLOT_WAITING = "Waiting for another Lean run to finish."
SLOT_WAIT_CANCELLED = "Waiting for another Lean run was cancelled."
SLOT_WAIT_TIMED_OUT = "Waiting for another Lean run timed out."
#: Seconds between attempts to take a busy slot while waiting for it.
_SLOT_POLL_SECONDS = 0.25


@contextmanager
def snapshot_slot(
    *,
    cancel: threading.Event | None = None,
    deadline: float | None = None,
    waiting: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Serialize expensive checks across backend processes in this user temp area.

    Without `cancel`, a busy slot is refused at once with `SLOT_BUSY`. With it,
    the caller waits for the slot: `waiting` is called once if it is busy, and
    the wait ends when the slot is free, when `cancel` is set
    (`SLOT_WAIT_CANCELLED`) or at the `time.monotonic` `deadline`, by default
    the check time limit from now (`SLOT_WAIT_TIMED_OUT`).
    """
    path = Path(tempfile.gettempdir()) / f"glitnir-project-check-{os.getuid()}.lock"
    fd = artifact_store.open_lock_file(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SnapshotError(
                "The project-check lock is not a user-owned regular file."
            )
        if deadline is None:
            deadline = time.monotonic() + check_seconds()
        told = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if cancel is None:
                    raise SnapshotError(SLOT_BUSY) from error
            if not told and waiting is not None:
                waiting()
            told = True
            if cancel.wait(
                max(0.0, min(_SLOT_POLL_SECONDS, deadline - time.monotonic()))
            ):
                raise SnapshotError(SLOT_WAIT_CANCELLED)
            if time.monotonic() >= deadline:
                raise SnapshotError(SLOT_WAIT_TIMED_OUT)
        yield
    finally:
        os.close(fd)


def _open_regular(path: Path) -> int:
    """Open through directory descriptors, refusing links in every component."""
    absolute = path.absolute()
    directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        return os.open(
            absolute.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory
        )
    finally:
        os.close(directory)


@contextmanager
def _open_input(source: Path, max_bytes: int) -> Iterator[tuple[BinaryIO, int]]:
    """Open a bounded regular input without following links; yield it and its mode."""
    fd = _open_regular(source)
    with os.fdopen(fd, "rb") as incoming:
        info = os.fstat(incoming.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError("Snapshot input is not a regular file.")
        if info.st_size > max_bytes:
            raise SnapshotError("Snapshot file exceeds its byte limit.")
        yield incoming, info.st_mode


def _blocks(
    incoming: BinaryIO,
    max_bytes: int,
    cancel: threading.Event | None,
    deadline: float | None,
) -> Iterator[bytes]:
    """Read at most ``max_bytes``, checking cancellation and the deadline."""
    total = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise SnapshotError("Project check cancelled while preparing inputs.")
        if deadline is not None and time.monotonic() >= deadline:
            raise SnapshotError("Preparing the project snapshot timed out.")
        block = incoming.read(min(1024 * 1024, max_bytes - total + 1))
        if not block:
            return
        total += len(block)
        if total > max_bytes:
            raise SnapshotError("Snapshot file exceeds its byte limit.")
        yield block


def copy_file(
    source: Path,
    target: Path,
    *,
    max_bytes: int,
    cancel: threading.Event | None = None,
    deadline: float | None = None,
) -> str:
    """Copy a bounded regular descriptor into a new inode and hash copied bytes."""
    with _open_input(source, max_bytes) as (incoming, mode):
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with target.open("xb") as output:
            for block in _blocks(incoming, max_bytes, cancel, deadline):
                output.write(block)
                digest.update(block)
        target.chmod(0o500 if mode & 0o111 else 0o400)
    return digest.hexdigest()


def hash_file(
    source: Path,
    *,
    max_bytes: int,
    cancel: threading.Event | None = None,
    deadline: float | None = None,
) -> tuple[str, int]:
    """Return the SHA-256 and size of a bounded regular file's bytes."""
    with _open_input(source, max_bytes) as (incoming, _mode):
        digest = hashlib.sha256()
        size = 0
        for block in _blocks(incoming, max_bytes, cancel, deadline):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def store_file(
    source: Path,
    store: ArtifactStore,
    namespace: str,
    *,
    max_bytes: int,
    cancel: threading.Event | None = None,
    deadline: float | None = None,
) -> tuple[str, int]:
    """Hash a bounded regular file and keep exactly those bytes in the store.

    Returns:
        The SHA-256 of the bytes read and their number. The stored file with
        that name holds exactly those bytes.
    """
    with _open_input(source, max_bytes) as (incoming, _mode):
        digest = hashlib.sha256()
        kept: list[bytes] | None = []
        size = 0
        for block in _blocks(incoming, max_bytes, cancel, deadline):
            digest.update(block)
            size += len(block)
            if kept is not None and size <= BUFFERED_BYTES:
                kept.append(block)
            else:
                kept = None
    name = digest.hexdigest()
    if not store.contains(namespace, name, size):
        if kept is not None:
            store.add(namespace, name, kept)
        else:
            # A large input is read again rather than held in memory; the
            # store publishes those bytes only if they hash to the same name.
            with _open_input(source, max_bytes) as (incoming, _mode):
                store.add(
                    namespace, name, _blocks(incoming, max_bytes, cancel, deadline)
                )
    return name, size


@contextmanager
def artifact_lease(scratch: Path) -> Iterator[ArtifactStore]:
    """Hold the user's artifact store for one check, or one in ``scratch``.

    The lease must stay open while any process may read the snapshot.
    """
    directory = artifact_store.store_directory()
    try:
        with artifact_store.lease(
            directory if directory is not None else scratch / "store"
        ) as store:
            yield store
    except StoreError as error:
        raise SnapshotError(str(error)) from error


def module_path(name: str) -> Path:
    """Map supported ordinary module names without accepting path syntax."""
    if not _MODULE.fullmatch(name):
        raise SnapshotError("This module name is not supported by the project checker.")
    return Path(*name.split("."))


@dataclass(frozen=True)
class ProjectSnapshot:
    """The exact files used by one isolated check, with a domain-separated identity.

    ``digests`` maps each captured file, relative to ``root``, to the SHA-256
    of its bytes. Entries under ``sources/`` and ``resolution-inputs/`` are
    hashed only and do not exist. Every other entry but ``toolchain/bin/lean``
    and ``Source.lean`` is a symbolic link to `stored` of its digest. ``stores``
    maps the first component of such entries to the store namespace holding
    their files; a sandbox reads exactly those directories.
    """

    root: Path
    toolchain: Path
    artifacts: Path
    source: Path
    setup: dict[str, Any]
    identity: str
    bytes: int
    files: int
    digests: dict[str, str] = dataclasses.field(default_factory=dict)
    stores: dict[str, Path] = dataclasses.field(default_factory=dict)

    def stored(self, relative: str, digest: str) -> Path | None:
        """Return the stored file the entry ``relative`` with ``digest`` links to."""
        namespace = self.stores.get(relative.split("/", 1)[0])
        if namespace is None:
            return None
        return namespace / digest[:2] / digest


def capture(
    destination: Path,
    source: Path,
    setup: dict[str, Any],
    toolchain: Path,
    project: Path | None,
    cancel: threading.Event,
    *,
    configuration: dict[str, str | None] | None = None,
    text: str | None = None,
    store: ArtifactStore | None = None,
) -> ProjectSnapshot:
    """Capture inputs before any project elaborator may execute.

    Each input is hashed and linked to its stored file in ``store``, a lease
    the caller holds for as long as the snapshot is used. Without one, the
    snapshot gets a private store under ``destination``, never pruned, since
    the snapshot is used after this returns.
    """
    try:
        if store is None:
            with artifact_store.lease(destination / "store", prune=False) as private:
                return _capture(
                    destination,
                    source,
                    setup,
                    toolchain,
                    project,
                    cancel,
                    configuration,
                    text,
                    private,
                )
        return _capture(
            destination,
            source,
            setup,
            toolchain,
            project,
            cancel,
            configuration,
            text,
            store,
        )
    except StoreError as error:
        raise SnapshotError(str(error)) from error


def _capture(
    destination: Path,
    source: Path,
    setup: dict[str, Any],
    toolchain: Path,
    project: Path | None,
    cancel: threading.Event,
    configuration: dict[str, str | None] | None,
    text: str | None,
    store: ArtifactStore,
) -> ProjectSnapshot:
    """Capture into ``destination``, linking every input to ``store``."""
    started = time.monotonic()
    # Copying a whole-Mathlib project takes minutes; share the check's limit.
    deadline = started + check_seconds()
    frozen = destination / "inputs"
    frozen.mkdir()
    namespaces = {
        "toolchain": artifact_store.TOOLCHAIN,
        "artifacts": (
            artifact_store.TOOLCHAIN
            if project is None
            else artifact_store.project_namespace(project)
        ),
    }
    namespaces["native"] = namespaces["artifacts"]
    copied_toolchain = frozen / "toolchain"
    artifacts = frozen / "artifacts"
    artifacts.mkdir()
    records: dict[str, str] = {}
    total = 0

    def copy(original: Path, target: Path, *, kept: str = "stored") -> None:
        """Link ``target`` to the stored bytes of ``original``.

        ``kept`` is ``"private"`` to copy them instead, or ``"hashed"`` to
        only record their digest under ``target``'s name.
        """
        nonlocal total
        roots = (toolchain,) if project is None else (toolchain, project)
        if not any(original.resolve().is_relative_to(root) for root in roots):
            raise SnapshotError(
                "Snapshot input escaped the selected project/toolchain."
            )
        if len(records) >= MAX_SNAPSHOT_FILES:
            raise SnapshotError("Project snapshot exceeds its file-count limit.")
        # Name the aggregate limit when it is the one reached; reading below
        # still enforces both limits on the bytes actually read.
        if total + original.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError(
                "Project snapshot exceeds its byte limit "
                f"({MAX_SNAPSHOT_BYTES // 1024**3} GiB)."
            )
        limit = min(MAX_FILE_BYTES, MAX_SNAPSHOT_BYTES - total)
        relative = str(target.relative_to(frozen))
        if kept == "private":
            digest = copy_file(
                original, target, max_bytes=limit, cancel=cancel, deadline=deadline
            )
            size = target.stat().st_size
        elif kept == "hashed":
            digest, size = hash_file(
                original, max_bytes=limit, cancel=cancel, deadline=deadline
            )
        else:
            namespace = namespaces[relative.split("/", 1)[0]]
            digest, size = store_file(
                original,
                store,
                namespace,
                max_bytes=limit,
                cancel=cancel,
                deadline=deadline,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(store.path(namespace, digest))
        total += size
        records[relative] = digest

    for name, expected in (configuration or {}).items():
        relative = Path(name)
        if project is None or relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError("Invalid prepared configuration path.")
        original = project / relative
        if not original.resolve().is_relative_to(project):
            raise SnapshotError("Prepared configuration escaped the project.")
        target = frozen / "resolution-inputs" / relative
        if expected is None:
            try:
                read_regular(original)
            except FileNotFoundError:
                records[str(target.relative_to(frozen))] = "absent"
            else:
                raise SnapshotError(
                    "Prepared package inputs changed; retry inspection."
                )
        else:
            copy(original, target, kept="hashed")
            if records[str(target.relative_to(frozen))] != expected:
                raise SnapshotError(
                    "Prepared package inputs changed; retry inspection."
                )

    # Selected installed Lean is trusted code. Stored files identify it and
    # prevent project/source mutations from changing what later processes load.
    # The loader finds Lean's libraries relative to the executable's real path,
    # so the executable itself is a private copy.
    for executable in ("lean",):
        copy(
            toolchain / "bin" / executable,
            copied_toolchain / "bin" / executable,
            kept="private",
        )
    for original in _walk_files(toolchain / "lib", cancel, deadline):
        if original.suffix in (".a", ".ilean", ".tbd"):
            continue
        resolved = original.resolve()
        if not resolved.is_relative_to(toolchain):
            raise SnapshotError("A toolchain library points outside its installation.")
        copy(resolved, copied_toolchain / original.relative_to(toolchain))
    rewritten = json.loads(json.dumps(setup))
    for module, paths in setup["importArts"].items():
        if (
            (toolchain / "lib/lean" / module_path(module))
            .with_suffix(".olean")
            .exists()
        ):
            raise SnapshotError(
                "A project artifact shadows a trusted toolchain module."
            )
        base = artifacts / module_path(module)
        suffixes = (".olean", ".ir", ".olean.server", ".olean.private")
        new_paths = []
        for name, suffix in zip(paths, suffixes, strict=False):
            original = Path(name)
            if not original.is_absolute():
                if project is None:
                    raise SnapshotError("Relative artifact without a project.")
                original = project / original
            target = Path(str(base) + suffix)
            copy(original.resolve(), target)
            new_paths.append(str(target))
        rewritten["importArts"][module] = new_paths
    for field in ("plugins", "dynlibs"):
        rewritten[field] = []
        for index, item in enumerate(setup[field]):
            name = item["path"] if isinstance(item, dict) else item
            original = Path(name)
            if not original.is_absolute():
                if project is None:
                    raise SnapshotError("Relative plugin without a project.")
                original = project / original
            target = frozen / "native" / field / str(index) / original.name
            copy(original.resolve(), target)
            rewritten[field].append(
                {**item, "path": str(target)} if isinstance(item, dict) else str(target)
            )
    if project is not None:
        for original in _walk_files(project, cancel, deadline):
            if original.suffix == ".lean" or original.name in (
                "lakefile.toml",
                "lean-toolchain",
                "lake-manifest.json",
            ):
                copy(
                    original,
                    frozen / "sources" / original.relative_to(project),
                    kept="hashed",
                )
    if len(records) >= MAX_SNAPSHOT_FILES:
        raise SnapshotError("Project snapshot exceeds its file-count limit.")
    saved = frozen / "Source.lean"
    # The preview and checker both use LF-normalized UTF-8 source.
    if text is None:
        text = read_regular(source, 16 * 1024 * 1024).decode().replace("\r\n", "\n")
    if total + len(text.encode()) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("Project snapshot exceeds its byte limit.")
    saved.write_text(text)
    saved.chmod(0o400)
    total += saved.stat().st_size
    records["Source.lean"] = hashlib.sha256(saved.read_bytes()).hexdigest()
    # Snapshot paths are fresh for each run; canonical identity uses relative paths.
    encoded = json.dumps(rewritten, sort_keys=True).replace(str(frozen), "$SNAPSHOT")
    identity = hashlib.sha256(
        b"glitnir.project-environment/v1\0"
        + encoded.encode()
        + json.dumps(records, sort_keys=True).encode()
    ).hexdigest()
    return ProjectSnapshot(
        frozen,
        copied_toolchain,
        artifacts,
        saved,
        rewritten,
        identity,
        total,
        len(records),
        dict(records),
        {section: store.namespace(name) for section, name in namespaces.items()},
    )


def _walk_files(root: Path, cancel: threading.Event, deadline: float) -> Iterator[Path]:
    """Bound directory traversal too, including files not selected for copying."""
    pending = [root]
    visited = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_SNAPSHOT_FILES:
                    raise SnapshotError(
                        "Project snapshot traversal exceeds its file limit."
                    )
                if cancel.is_set():
                    raise SnapshotError(
                        "Project check cancelled while preparing inputs."
                    )
                if time.monotonic() >= deadline:
                    raise SnapshotError("Preparing the project snapshot timed out.")
                if entry.is_dir(follow_symlinks=False):
                    ignored = entry.name in (
                        ".git",
                        ".venv",
                        "node_modules",
                        "__pycache__",
                    )
                    # Compiled inputs come from Lake's exact artifact map.
                    lake_build = (
                        entry.name == "build"
                        and Path(entry.path).parent.name == ".lake"
                    )
                    if not ignored and not lake_build:
                        pending.append(Path(entry.path))
                else:
                    yield Path(entry.path)
