"""A per-user store of the exact files project checks read, named by content.

A project snapshot used to copy every input into new files for each check: for
a project importing all of Mathlib, about 54,000 files and 8 GB. Creating that
many files dominated a check on hosts whose malware scanner inspects each new
file on its first open, and copying is not needed for what a snapshot
guarantees. The snapshot now hashes each input once and links to an immutable
file in this store whose name is that digest; only small generated files are
written per check.

Layout, under the user's cache (:func:`store_directory`)::

    glitnir/              owned by the user, writable by nobody else
      artifacts/          0700
        .lock             shared while a check uses the store, exclusive to prune
        .usage            the total size found by the last full pruning scan
        blobs/            0700
          toolchain/      0700; files of the Lean toolchain, shared by all checks
          p<hash>/        0700; one project's compiled inputs (`project_namespace`)
            ab/           0700; first two hex digits of the digest
              ab12...ef   0400; exactly the bytes whose SHA-256 is its name
        uses/             0700; one record per check of the stored files it used

Stored files are separated by namespace, and a check's sandbox sees only the
toolchain namespace and its own project's, read-only: never another project's
files. Project sources, configuration and manifests are hashed for a snapshot's
identity but never stored.

A file is written once, under a private temporary name created with
``O_EXCL``, synced, read back and verified against its name, made ``0400``
and linked into place without ever replacing an existing name. Nothing opens a
stored file for writing again. Before a check uses a stored file, it requires a
regular ``0400`` file of the user with the size of the bytes it just hashed;
anything else is refused. It does not hash the stored file again: the store is
in the same trust boundary as the replay cache (`replay_cache`), whose records
already let anyone who can write the user's cache skip a kernel replay, and as
the per-check copies were, which the same user could also rewrite while a check
ran.

The store is bounded: after every check, if no other check holds it, it is
pruned to :data:`MAX_STORE_BYTES` and so that it never leaves less than
:data:`FREE_RESERVE_BYTES` (or a tenth of a small disk) free. Least recently
used files go first. A file's last use comes from the newest of the use
records naming it, else from when it was stored; a stored file's own metadata
never changes, because a malware scanner may treat a changed file as new and
inspect it again. Every check holds a shared lock on ``.lock`` from before its
snapshot is captured until its last Lean process has finished, and pruning
needs the exclusive lock, so no file an in-progress check references is ever
removed. A full scan of the stored files is skipped while the last scan's total
plus what checks since have added is within bounds.
"""

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from glitnir.lean import replay_cache

#: Bytes the stored files may take together after a check; LRU beyond that.
MAX_STORE_BYTES = 40 * 1024**3
#: Free space pruning keeps on the store's volume, at most a tenth of the disk.
FREE_RESERVE_BYTES = 10 * 1024**3
#: Use records kept after a full scan.
MAX_USE_RECORDS = 16
#: One use record lists at most this many files (a snapshot's file limit).
USE_RECORD_ENTRIES = 100_000
#: A full scan happens at least this often, whatever the recorded totals say.
FULL_SCAN_SECONDS = 24 * 3600
#: The namespace of toolchain files, which every check may read.
TOOLCHAIN = "toolchain"
_NAMESPACE = re.compile(r"(?:toolchain|p[0-9a-f]{32})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SHARD = re.compile(r"[0-9a-f]{2}\Z")
_TEMPORARY = re.compile(r"\.[0-9a-f]{64}\.[0-9]+\.[0-9a-f]{16}\.tmp\Z")
_USE_RECORD = re.compile(r"[0-9]{20}\.[0-9]+\.[0-9a-f]{16}\Z")
_USED = re.compile(r"(?:toolchain|p[0-9a-f]{32})/[0-9a-f]{64}\Z")
_ADDED = re.compile(r"# added ([0-9]{1,20})\Z")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class StoreError(ValueError):
    """The artifact store is unusable, or holds a file that is not a valid blob."""


def store_directory() -> Path | None:
    """Return the user's artifact store directory, or None without a home."""
    base = replay_cache.cache_base()
    return None if base is None else base / "glitnir" / "artifacts"


def project_namespace(project: Path) -> str:
    """Return the namespace of one project's stored files.

    It is keyed by the project directory's real path and its device and inode,
    so a different project later created at the same path gets another
    namespace, unless the file system reuses the inode number.
    """
    real = os.path.realpath(project)
    info = os.stat(real)
    key = f"{real}\0{info.st_dev}\0{info.st_ino}".encode()
    return (
        "p" + hashlib.sha256(b"glitnir.artifact-namespace/v2\0" + key).hexdigest()[:32]
    )


def _blob_path(blobs: Path, namespace: str, digest: str) -> Path:
    """Return where the file with SHA-256 ``digest`` lives in ``namespace``."""
    return blobs / namespace / digest[:2] / digest


def open_lock_file(path: str | Path, *, dir_fd: int | None = None) -> int:
    """Open a lock file, creating it if needed, without following links.

    Concurrent ``O_CREAT`` opens of one new name can fail with ``ENOENT`` on
    macOS, so the file is created only exclusively and opened again otherwise.
    """
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    for _ in range(16):
        try:
            return os.open(path, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        try:
            return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            pass
    raise OSError(errno.ENOENT, "The lock file could not be opened", str(path))


def _directory(name: str, parent: int, *, private: bool = True) -> int:
    """Create or open a directory of the user, refusing links.

    A private directory is accessible to nobody else; any other must at least
    be writable by nobody else.
    """
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (0o077 if private else 0o022)
    ):
        os.close(descriptor)
        raise StoreError(f"The artifact store directory {name} is not private.")
    return descriptor


class ArtifactStore:
    """One lease on the store: add files and look them up by digest.

    Obtained from :func:`lease`, which holds the shared lock while it is open.
    """

    def __init__(self, root: Path, blobs: int) -> None:
        """Wrap the store at ``root`` whose open blob directory is ``blobs``."""
        self.root = root
        self.blobs = root / "blobs"
        self._descriptor = blobs
        self._validated: set[str] = set()
        #: ``namespace/digest`` of every stored file this lease used.
        self.used: set[str] = set()
        #: Bytes this lease stored.
        self.added = 0

    def namespace(self, namespace: str) -> Path:
        """Return the directory a sandbox may read for ``namespace``."""
        if not _NAMESPACE.fullmatch(namespace):
            raise StoreError("Not an artifact store namespace.")
        return self.blobs / namespace

    def path(self, namespace: str, digest: str) -> Path:
        """Return the stored file's path for ``digest`` in ``namespace``."""
        return _blob_path(self.blobs, namespace, digest)

    def _name(self, namespace: str, digest: str) -> str:
        """Create or validate the file's directories once per lease."""
        if not _NAMESPACE.fullmatch(namespace) or not _DIGEST.fullmatch(digest):
            raise StoreError("Not a stored file's namespace and digest.")
        shard = f"{namespace}/{digest[:2]}"
        if shard not in self._validated:
            outer = _directory(namespace, self._descriptor)
            try:
                os.close(_directory(digest[:2], outer))
            finally:
                os.close(outer)
            self._validated.add(shard)
        return f"{shard}/{digest}"

    def contains(self, namespace: str, digest: str, size: int) -> bool:
        """Whether a valid file of ``size`` bytes is stored under ``digest``.

        Records it as used by this lease. A stored name that is not a ``0400``
        regular file of the user with that size is refused rather than trusted
        or replaced.
        """
        name = self._name(namespace, digest)
        try:
            info = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_size != size
        ):
            raise StoreError(
                f"The artifact store file {self.path(namespace, digest)} is not a "
                "valid stored file; delete it to let the next check restore it."
            )
        self.used.add(f"{namespace}/{digest}")
        return True

    def add(self, namespace: str, digest: str, blocks: Iterable[bytes]) -> None:
        """Store ``blocks`` under ``digest`` unless already stored.

        The bytes are written to a new private file, synced and read back; they
        are published only if they hash to ``digest``, and never over an
        existing name. A concurrent writer of the same digest wins harmlessly.

        Raises:
            StoreError: The bytes do not hash to ``digest``, or a stored file
                with that name is not valid.
        """
        name = self._name(namespace, digest)
        temporary = (
            f"{namespace}/{digest[:2]}/"
            f".{digest}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        output = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._descriptor,
        )
        published = False
        try:
            written = hashlib.sha256()
            size = 0
            for block in blocks:
                view = memoryview(block)
                while view:
                    view = view[os.write(output, view) :]
                written.update(block)
                size += len(block)
            if written.hexdigest() != digest:
                raise StoreError("A snapshot input changed while it was captured.")
            os.fsync(output)
            check = hashlib.sha256()
            offset = 0
            while chunk := os.pread(output, 1024 * 1024, offset):
                check.update(chunk)
                offset += len(chunk)
            if offset != size or check.hexdigest() != digest:
                raise StoreError("The artifact store did not keep the bytes written.")
            os.fchmod(output, 0o400)
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=self._descriptor,
                    dst_dir_fd=self._descriptor,
                )
                published = True
                self.used.add(f"{namespace}/{digest}")
                self.added += size
            except FileExistsError:
                pass
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise StoreError("No space left for the artifact store.") from error
            raise
        finally:
            os.close(output)
            try:
                os.unlink(temporary, dir_fd=self._descriptor)
            except OSError:
                pass
        if not published and not self.contains(namespace, digest, size):
            raise StoreError("The artifact store lost a file it was given.")


def _record_use(uses: int, store: ArtifactStore) -> None:
    """Record what one check used and added, under a new unique name."""
    if not store.used and not store.added:
        return
    name = f"{time.time_ns():020d}.{os.getpid()}.{secrets.token_hex(8)}"
    output = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=uses
    )
    try:
        lines = [f"# added {store.added}", *sorted(store.used)]
        view = memoryview("".join(line + "\n" for line in lines).encode())
        while view:
            view = view[os.write(output, view) :]
    finally:
        os.close(output)


def _read_small(name: str, directory: int, limit: int) -> bytes | None:
    """Read one bounded regular file of the store, or None."""
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        data = b""
        while chunk := os.read(descriptor, limit + 1 - len(data)):
            data += chunk
            if len(data) > limit:
                return None
        return data
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _read_use(uses: int, name: str) -> tuple[int, list[str]]:
    """Return the bytes one check added and the files it used; nothing if invalid."""
    data = _read_small(name, uses, USE_RECORD_ENTRIES * 90 + 64)
    if data is None:
        return 0, []
    lines = data.decode("ascii", "replace").split("\n")
    added = _ADDED.fullmatch(lines[0]) if lines else None
    return (
        int(added[1]) if added else 0,
        [line for line in lines if _USED.fullmatch(line)],
    )


def _read_usage(root: int) -> tuple[int, int] | None:
    """Return the last full scan's total bytes and time, if recorded."""
    data = _read_small(".usage", root, 4096)
    try:
        value = json.loads(data) if data is not None else None
    except ValueError:
        return None
    if (
        isinstance(value, dict)
        and type(value.get("total")) is int
        and type(value.get("scanned")) is int
        and value["total"] >= 0
    ):
        return value["total"], value["scanned"]
    return None


def _write_usage(root: int, total: int, scanned: int) -> None:
    """Record a full scan's result, replacing the previous record."""
    temporary = f".usage.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    output = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=root,
    )
    try:
        os.write(output, json.dumps({"total": total, "scanned": scanned}).encode())
    finally:
        os.close(output)
    os.replace(temporary, ".usage", src_dir_fd=root, dst_dir_fd=root)


def _free_bytes(descriptor: int) -> tuple[int, int]:
    """Return the free space available to the user and the size of the volume."""
    info = os.fstatvfs(descriptor)
    return info.f_bavail * info.f_frsize, info.f_blocks * info.f_frsize


def _bound(total: int, budget: int, descriptor: int) -> int:
    """Return how many bytes the store may keep, given the free-space reserve."""
    free, size = _free_bytes(descriptor)
    reserve = min(FREE_RESERVE_BYTES, size // 10)
    return max(0, min(budget, total + free - reserve))


def _scan(blobs: int) -> dict[str, tuple[float, int]]:
    """Return every stored file's time and size, removing temporary files.

    Anything else directly in the blob directory, such as the namespace-less
    shards of an earlier layout, is removed.
    """
    entries: dict[str, tuple[float, int]] = {}
    with os.scandir(blobs) as children:
        names = [(c.name, c.is_dir(follow_symlinks=False)) for c in children]
    namespaces = [name for name, _ in names if _NAMESPACE.fullmatch(name)]
    for name, directory in names:
        if _NAMESPACE.fullmatch(name):
            continue
        try:
            if directory:
                shutil.rmtree(name, dir_fd=blobs)
            else:
                os.unlink(name, dir_fd=blobs)
        except OSError:
            pass
    for namespace in namespaces:
        try:
            outer = os.open(namespace, _DIRECTORY, dir_fd=blobs)
        except OSError:
            continue
        try:
            with os.scandir(outer) as children:
                shards = [c.name for c in children if _SHARD.fullmatch(c.name)]
            for shard in shards:
                try:
                    inner = os.open(shard, _DIRECTORY, dir_fd=outer)
                except OSError:
                    continue
                try:
                    with os.scandir(inner) as children:
                        for child in children:
                            if _TEMPORARY.fullmatch(child.name):
                                try:
                                    os.unlink(child.name, dir_fd=inner)
                                except OSError:
                                    pass
                            elif _DIGEST.fullmatch(child.name):
                                try:
                                    info = child.stat(follow_symlinks=False)
                                except OSError:
                                    continue
                                entries[f"{namespace}/{child.name}"] = (
                                    info.st_mtime,
                                    info.st_size,
                                )
                finally:
                    os.close(inner)
        finally:
            os.close(outer)
    return entries


def _prune(root: int, blobs: int, uses: int, budget: int) -> None:
    """Keep the store within its bounds, least recently used files first.

    Only called under the exclusive lock, when no check uses any stored file
    and no writer is active. When the last full scan's total plus what checks
    have added since is within bounds, nothing is scanned.
    """
    now = time.time_ns()
    with os.scandir(uses) as children:
        records = sorted(
            (c.name for c in children if _USE_RECORD.fullmatch(c.name)), reverse=True
        )
    usage = _read_usage(root)
    if usage is not None:
        total, scanned = usage
        newer = [name for name in records if int(name.split(".", 1)[0]) > scanned]
        if (
            0 <= now - scanned < FULL_SCAN_SECONDS * 10**9
            and len(newer) <= MAX_USE_RECORDS
        ):
            estimate = total + sum(_read_use(uses, name)[0] for name in newer)
            if estimate <= _bound(estimate, budget, blobs):
                return
    entries = _scan(blobs)
    total = sum(size for _, size in entries.values())
    bound = _bound(total, budget, blobs)
    if total > bound:
        used: dict[str, float] = {}
        for name in records[:MAX_USE_RECORDS]:
            when = int(name.split(".", 1)[0]) / 1e9
            for entry in _read_use(uses, name)[1]:
                used[entry] = max(used.get(entry, 0.0), when)
        order = sorted(
            (max(stored, used.get(entry, 0.0)), entry)
            for entry, (stored, _) in entries.items()
        )
        for _, entry in order:
            if total <= bound:
                break
            namespace, digest = entry.split("/")
            try:
                os.unlink(f"{namespace}/{digest[:2]}/{digest}", dir_fd=blobs)
            except OSError:
                continue
            total -= entries[entry][1]
    for name in records[MAX_USE_RECORDS:]:
        try:
            os.unlink(name, dir_fd=uses)
        except OSError:
            pass
    _write_usage(root, total, now)


def _finish(
    lock: int,
    root: int,
    blobs: int,
    uses: int,
    store: ArtifactStore,
    budget: int | None,
) -> None:
    """Record a lease's use, release it and prune if no other check holds the store.

    Best effort, and done however the check ended.
    """
    try:
        _record_use(uses, store)
    except OSError:
        pass  # the files then count as last used when they were stored
    fcntl.flock(lock, fcntl.LOCK_UN)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # another check uses the store; it prunes when it ends
    if budget is None:
        return
    try:
        _prune(root, blobs, uses, budget)
    except OSError:
        pass  # pruning is best effort; the next check retries


@contextmanager
def lease(
    directory: Path, *, budget: int | None = None, prune: bool = True
) -> Iterator[ArtifactStore]:
    """Hold the store at ``directory`` for one check; prune it afterwards.

    The shared lock is held while the context is open, so no stored file is
    removed while a check may read it. On exit, also after an error, the use
    is recorded and the store is pruned within ``budget`` (default
    :data:`MAX_STORE_BYTES`) and the free-space reserve, if no other check
    holds it. With ``prune`` false nothing is ever removed, for a store whose
    files are used after the lease ends.

    Raises:
        StoreError: The store or its parent is not the user's own directory.
    """
    directory = Path(os.path.realpath(directory))
    opened: list[int] = []
    try:
        os.makedirs(directory.parent.parent, mode=0o700, exist_ok=True)
        base = os.open(directory.parent.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            opened.append(_directory(directory.parent.name, base, private=False))
        finally:
            os.close(base)
        opened.append(_directory(directory.name, opened[0]))
        opened.append(open_lock_file(".lock", dir_fd=opened[1]))
        opened.append(_directory("blobs", opened[1]))
        opened.append(_directory("uses", opened[1]))
    except BaseException as error:
        for descriptor in opened:
            os.close(descriptor)
        if isinstance(error, OSError):
            raise StoreError(f"The artifact store is unusable: {error}") from error
        raise
    os.close(opened.pop(0))
    root, lock, blobs, uses = opened
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise StoreError("The artifact store lock is not a user-owned file.")
        fcntl.flock(lock, fcntl.LOCK_SH)
        store = ArtifactStore(directory, blobs)
        try:
            yield store
        finally:
            _finish(
                lock,
                root,
                blobs,
                uses,
                store,
                None if not prune else MAX_STORE_BYTES if budget is None else budget,
            )
    finally:
        for descriptor in opened:
            os.close(descriptor)
