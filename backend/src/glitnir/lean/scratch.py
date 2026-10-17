"""Owner-marked scratch directories that survive their owner only until a sweep.

A project snapshot can be gigabytes. ``tempfile.TemporaryDirectory`` removes
one only when its context exits, which never happens when the backend is
killed. Every scratch directory therefore holds a marker file, ``.glitnir-owner``,
on which its owner keeps an exclusive ``flock`` for the directory's lifetime.
The kernel releases that lock when the owner dies however it dies, so a marker
anyone else can lock names a dead owner, and a reused process id is never
mistaken for it. The marker is published already locked (created under a
private name, locked, then linked into place), so it is never seen unlocked
while its owner lives.

:func:`sweep_stale_scratch` removes the ``glitnir-*`` directories of dead
owners, including those plugins create here, and the unmarked snapshot
directories (:data:`LEGACY_PREFIXES`) older than :data:`LEGACY_SECONDS` that
earlier versions leaked. It holds the marker's lock while removing and removes
the marker last, so an interrupted sweep leaves a directory the next sweep
finishes.
"""

import atexit
import fcntl
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Every scratch directory's name starts with this; the sweep looks nowhere else.
NAMESPACE = "glitnir-"
_PREFIX = re.compile(r"glitnir-(?:[a-z0-9]+-)+\Z")
#: The owner's lock and diagnostic process id inside each scratch directory.
MARKER = ".glitnir-owner"
#: Unmarked project snapshots older than this are leaks from earlier versions.
LEGACY_SECONDS = 6 * 3600
#: The unmarked directories the legacy rule removes: snapshots, which can be
#: gigabytes. Others are small, and an earlier version may still be using one.
LEGACY_PREFIXES = ("glitnir-check-", "glitnir-view-", "glitnir-trace-")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _claim(directory: Path) -> int | None:
    """Lock a new marker in ``directory``; ``None`` when one already exists.

    Returns:
        The descriptor holding the lock; closing it releases the claim.
    """
    private = directory / f"{MARKER}.{os.getpid()}.{time.monotonic_ns()}"
    descriptor = os.open(
        private, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        try:
            os.link(private, directory / MARKER)
        except FileExistsError:
            os.close(descriptor)
            return None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        private.unlink(missing_ok=True)


def _make_tree_writable(descriptor: int) -> None:
    """Give the owner full access to an open directory and those below it.

    Every directory is opened relative to its parent without following links
    and changed through its descriptor, so a swapped link redirects nothing.
    Files need no change: unlinking one only needs its directory writable.
    """
    mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
    os.fchmod(descriptor, mode | stat.S_IRWXU)
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
    for name in names:
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
        try:
            _make_tree_writable(child)
        finally:
            os.close(child)


def _make_writable(directory: Path) -> None:
    """Give the owner full access to every real directory below ``directory``."""
    descriptor = os.open(directory, _DIRECTORY_FLAGS)
    try:
        _make_tree_writable(descriptor)
    finally:
        os.close(descriptor)


def _remove(directory: Path) -> None:
    """Delete ``directory`` without following links, removing its marker last."""
    _make_writable(directory)
    with os.scandir(directory) as entries:
        children = [entry for entry in entries if entry.name != MARKER]
    for entry in children:
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)
    (directory / MARKER).unlink(missing_ok=True)
    os.rmdir(directory)


def _release(directory: Path, descriptor: int) -> None:
    """Remove an owned directory, leaving any remainder to a later sweep."""
    try:
        _remove(directory)
    except OSError:
        pass  # the marker outlives the lock, so the next sweep retries
    finally:
        os.close(descriptor)


def _create(prefix: str) -> tuple[Path, int]:
    """Create and claim a private scratch directory in the system temp area."""
    if not _PREFIX.match(prefix):
        raise ValueError(f"Scratch prefix {prefix!r} is not `glitnir-<words>-`.")
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        descriptor = _claim(directory)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    if descriptor is None:  # nobody else knows the fresh name
        raise FileExistsError(f"{directory} was claimed before its creator.")
    return directory, descriptor


@contextmanager
def scratch_directory(prefix: str) -> Iterator[Path]:
    """Yield an owner-marked private directory, removed on exit.

    Read-only files and directories inside are made writable before removal;
    symbolic links are removed, never followed.

    Args:
        prefix: ``glitnir-`` then lowercase words, each ending in ``-``,
            for example ``glitnir-forseti-session-``.

    Raises:
        ValueError: ``prefix`` is outside the ``glitnir-`` namespace.
    """
    directory, descriptor = _create(prefix)
    try:
        yield directory
    finally:
        _release(directory, descriptor)


def persistent_scratch_directory(prefix: str) -> Path:
    """Create an owner-marked directory that lives until the process exits."""
    directory, descriptor = _create(prefix)
    atexit.register(_release, directory, descriptor)
    return directory


def _owned_directory(path: Path) -> int | None:
    """Open ``path`` when it is a real directory of this user, else ``None``."""
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError:
        return None  # a link, a file, or gone
    try:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            return descriptor
    except OSError:
        pass
    os.close(descriptor)
    return None


def _claim_stale(path: Path, directory: int) -> int | None:
    """Take the lock of a dead owner's directory; ``None`` when it is not stale."""
    try:
        marker = os.open(
            MARKER,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    except FileNotFoundError:
        if not path.name.startswith(LEGACY_PREFIXES):
            return None
        age = time.time() - os.fstat(directory).st_mtime
        return _claim(path) if age > LEGACY_SECONDS else None
    try:
        info = os.fstat(marker)
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
            fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return marker
    except OSError:
        pass  # the owner is alive, or another sweep has it
    os.close(marker)
    return None


def _sweep_one(path: Path) -> None:
    """Remove ``path`` when it is a stale scratch directory of this user."""
    directory = _owned_directory(path)
    if directory is None:
        return
    try:
        lock = _claim_stale(path, directory)
        if lock is None:
            return
        try:
            opened, current = os.fstat(directory), os.lstat(path)
            if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                _remove(path)
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def sweep_stale_scratch() -> None:
    """Remove scratch directories in the temp area whose owners are gone.

    Best effort: errors are swallowed per directory and never raised. A live
    owner's directory is only opened to test its marker, never walked.
    """
    try:
        root = Path(tempfile.gettempdir())
        with os.scandir(root) as entries:
            names = [
                entry.name for entry in entries if entry.name.startswith(NAMESPACE)
            ]
    except OSError:
        return
    for name in names:
        try:
            _sweep_one(root / name)
        except Exception:  # one bad entry must not stop startup
            continue
