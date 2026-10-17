"""A per-user record of import closures the kernel has fully replayed.

Every project check replays its module's constants in Lean's kernel on top of
its imports (`ProjectReplay.lean`). Every imported constant must also have been
replayed into an empty environment. For Mathlib that takes most of an hour, and
it depends only on the imported files, so the checker runs it as a separate
step and, after it succeeds, records an entry here under :func:`import_key`. A
later check whose key has an entry skips that step; its own constants are still
replayed.

An entry lists the closure's modules in load order, each with
:func:`module_digest` of its files, under a :func:`context_digest` of the
toolchain and the replay program. A closure without an entry may still extend
an entry recorded as ``extendable`` whose modules it loads with identical files
(:func:`find_base`): only the constants of its other modules are then replayed,
over the base rebuilt from those files (``imports-incremental``).

The directory is the user's cache (:func:`cache_directory`), created ``0700``,
with one ``0600`` file per key. Only the checker reads or writes it, outside
the sandbox; no Lean process can see it. Whoever can write it can forge an
entry, which is the same trust as being able to change the user's files.
Anything unreadable, malformed or not matching its key is a miss, and only the
newest :data:`MAX_ENTRIES` entries, within :data:`MAX_TOTAL_BYTES`, are kept.
"""

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Any

from glitnir.lean.project_inspection import TOOLCHAIN

FORMAT = "glitnir.replay-cache/v2"
#: Entries kept; the least recently used beyond this are removed.
MAX_ENTRIES = 64
#: Bytes all entries may take together; the least recently used go first.
MAX_TOTAL_BYTES = 256 * 1024**2
#: One entry lists every module of a closure (Mathlib: thousands).
ENTRY_BYTES = 16 * 1024**2
#: Each module's files that ``importModules`` may read.
MODULE_SUFFIXES = (".olean", ".olean.server", ".olean.private", ".ir")
_KEY = re.compile(r"[0-9a-f]{64}\Z")


def cache_base() -> Path | None:
    """Return the user's cache directory by platform convention, or None."""
    try:
        home = Path.home()
    except (KeyError, RuntimeError):
        return None
    if sys.platform == "darwin":
        base = home / "Library" / "Caches"
    else:
        configured = os.environ.get("XDG_CACHE_HOME", "")
        base = Path(configured) if os.path.isabs(configured) else home / ".cache"
    return base if base.is_absolute() else None


def cache_directory() -> Path | None:
    """Return the user's replay cache directory, or None without a home."""
    base = cache_base()
    return None if base is None else base / "glitnir" / "replay"


def _digest(document: Any) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def context_digest(*, toolchain: dict[str, str], program_sha256: str) -> str:
    """Digest what every replay depends on besides the imported modules.

    Args:
        toolchain: Every captured toolchain file (the `lean` binary, its
            libraries and the standard library), relative path to SHA-256.
        program_sha256: The replay program's digest.
    """
    return _digest(
        {
            "format": FORMAT,
            "toolchain": TOOLCHAIN,
            "toolchain_files": sorted(toolchain.items()),
            "program": program_sha256,
            # The program passes no Lean options to the import or the kernel;
            # the memory limit only decides whether a run completes.
            "options": {},
        }
    )


def module_digest(files: dict[str, str]) -> str:
    """Digest one module's files: each suffix's SHA-256, or ``"absent"``."""
    return _digest(sorted(files.items()))


def import_key(*, context: str, imports: Any, modules: list[list[str]]) -> str:
    """Digest everything one closure's imported replay depends on.

    Args:
        context: :func:`context_digest`.
        imports: The checked module's imports, as the program reports them.
        modules: The imported closure in load order, each ``[name, digest]``
            with :func:`module_digest` of its files as loaded.
    """
    return _digest(
        {"format": FORMAT, "context": context, "imports": imports, "modules": modules}
    )


def _open_directory(directory: Path, *, create: bool) -> int | None:
    """Open the cache's last two components without following links.

    Both must be directories the user owns that nobody else can write; the
    last must not be accessible to anyone else at all. Returns None when that
    does not hold.
    """
    base = directory.parent.parent
    try:
        if create:
            os.makedirs(base, mode=0o700, exist_ok=True)
        descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for name in (directory.parent.name, directory.name):
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
            ):
                raise PermissionError("The replay cache is not the user's directory.")
        if os.fstat(descriptor).st_mode & 0o077:
            raise PermissionError("The replay cache is accessible to other users.")
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def _modules(value: Any) -> bool:
    """Whether ``value`` is a closure: distinct ``[name, digest]`` pairs."""
    return (
        isinstance(value, list)
        and all(
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
            and _KEY.fullmatch(item[1]) is not None
            for item in value
        )
        and len({item[0] for item in value}) == len(value)
    )


def _valid(value: Any, key: str, context: str) -> bool:
    """Return whether a parsed entry records success for exactly this key."""
    return (
        isinstance(value, dict)
        and value.get("format") == FORMAT
        and value.get("key") == key
        and value.get("toolchain") == TOOLCHAIN
        and value.get("context") == context
        and _modules(value.get("modules"))
        and type(value.get("extendable")) is bool
        and type(value.get("created")) in (int, float)
    )


def _read(descriptor: int, name: str) -> Any:
    """Parse one entry, refusing links, others' files and oversized ones.

    Raises:
        OSError, ValueError or RecursionError when the entry is not usable.
    """
    entry = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
    )
    try:
        info = os.fstat(entry)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > ENTRY_BYTES
        ):
            raise PermissionError("Not a private replay record.")
        chunks, total = [], 0
        while chunk := os.read(entry, ENTRY_BYTES + 1 - total):
            chunks.append(chunk)
            total += len(chunk)
            if total > ENTRY_BYTES:
                raise ValueError("The replay record is too large.")
        return json.loads(b"".join(chunks))
    finally:
        os.close(entry)


def _touch(descriptor: int, name: str) -> None:
    """Mark one entry as just used, so pruning keeps it longest.

    Only an entry a check actually uses is touched: the exact hit, or the base
    chosen for an incremental replay, never every entry a search reads.
    """
    try:
        entry = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
    except OSError:
        return
    try:
        os.utime(entry)
    except OSError:
        pass
    finally:
        os.close(entry)


def contains(
    directory: Path, key: str, *, context: str, modules: list[list[str]]
) -> bool:
    """Return whether a valid entry records a full replay of exactly this closure."""
    if not _KEY.fullmatch(key):
        return False
    descriptor = _open_directory(directory, create=False)
    if descriptor is None:
        return False
    try:
        value = _read(descriptor, key + ".json")
        if not (_valid(value, key, context) and value["modules"] == modules):
            return False
        _touch(descriptor, key + ".json")
        return True
    except (OSError, ValueError, RecursionError):
        return False
    finally:
        os.close(descriptor)


def find_base(
    directory: Path, *, context: str, modules: list[list[str]]
) -> list[str] | None:
    """Return the largest extendable recorded closure this closure contains.

    A recorded closure qualifies when it was recorded under the same context,
    as extendable, and every one of its modules is in ``modules`` with the
    same digest. Its module names are returned in its recorded load order,
    which the replay merges its constants in. The replay itself checks that
    the base is closed under imports and loads its constants unchanged.
    """
    descriptor = _open_directory(directory, create=False)
    if descriptor is None:
        return None
    digests = dict((name, digest) for name, digest in modules)
    best: list[str] | None = None
    chosen = ""
    try:
        with os.scandir(descriptor) as children:
            names = [c.name for c in children if c.name.endswith(".json")]
        for name in sorted(names):
            key = name[: -len(".json")]
            if not _KEY.fullmatch(key):
                continue
            try:
                value = _read(descriptor, name)
            except (OSError, ValueError, RecursionError):
                continue
            if not _valid(value, key, context) or not value["extendable"]:
                continue
            recorded = value["modules"]
            if len(recorded) > len(modules) or any(
                digests.get(module) != digest for module, digest in recorded
            ):
                continue
            if best is None or len(recorded) > len(best):
                best = [module for module, _ in recorded]
                chosen = name
        if best is not None:
            _touch(descriptor, chosen)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return best


def record(
    directory: Path,
    key: str,
    *,
    context: str,
    modules: list[list[str]],
    extendable: bool,
) -> None:
    """Record a full replay for ``key``; failing to record changes nothing."""
    if not _KEY.fullmatch(key) or not _modules(modules):
        return
    descriptor = _open_directory(directory, create=True)
    if descriptor is None:
        return
    temporary = f".{key}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    data = json.dumps(
        {
            "format": FORMAT,
            "key": key,
            "toolchain": TOOLCHAIN,
            "context": context,
            "modules": modules,
            "extendable": extendable,
            "created": time.time(),
        }
    ).encode()
    if len(data) > ENTRY_BYTES:
        os.close(descriptor)
        return
    try:
        output = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        try:
            os.fchmod(output, 0o600)
            written = 0
            while written < len(data):
                written += os.write(output, data[written:])
            os.fsync(output)
        finally:
            os.close(output)
        # Replaces a name, never writes through it, even if it is a link.
        os.replace(
            temporary, key + ".json", src_dir_fd=descriptor, dst_dir_fd=descriptor
        )
        _prune(descriptor)
    except OSError:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _prune(descriptor: int) -> None:
    """Remove the least recently used entries beyond the count and byte bounds.

    The newest entry always stays. Temporary files count as entries, so an
    interrupted write is removed in time too. Checks run one at a time per user
    (``snapshot_slot``), so no other check is writing one while this runs.
    """
    entries = []
    with os.scandir(descriptor) as children:
        for child in children:
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append((info.st_mtime_ns, child.name, info.st_size))
    entries.sort()
    total = sum(size for _, _, size in entries)
    count = len(entries)
    for _, name, size in entries[:-1]:
        if count <= MAX_ENTRIES and total <= MAX_TOTAL_BYTES:
            break
        try:
            os.unlink(name, dir_fd=descriptor)
        except OSError:
            continue
        count -= 1
        total -= size
