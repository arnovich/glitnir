"""A fast identity for the environment a notebook is checked in.

It reads small files Lean and Lake already maintain, never ``.olean`` files,
and runs no project code. It decides whether saved results are still current;
the full identity recorded by an explicit check stays the authority.

The identity names no location: project sources are keyed by their path
relative to the project, files and dependency states by their contents and
commits, and each imported module by the build identity in its Lake trace
(`trace_digest`). Saved results therefore stay current in another checkout,
on another machine or operating system, exactly when the toolchain, sources,
dependencies and compiled artifacts are byte-identical there.
"""

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, NoReturn

from glitnir.lean import (
    axiom_report,
    project_checking,
    project_inspection,
    provider_lean,
)
from glitnir.lean.lexing import code_tokens, scan
from glitnir.lean.project import describe_project, read_regular

_IMPORT = re.compile(r"^(?:(?:public|private|meta)\s+)*import\s+(.+)$")
_SKIP = re.compile(r"^(?:module|prelude)\s*$")
_OUTSIDE = re.compile(r"/-|--")
_IN_BLOCK = re.compile(r"/-|-/")
_MAX_FILE = 4 * 1024 * 1024
_MAX_SOURCES = 20000
_COMPONENT = re.compile(r"[A-Za-z_][A-Za-z0-9_'!?]*|«[^/\\\x00]+»")
#: A fingerprint for a function of the modules loaded besides the notebook's.
Fingerprint = Callable[..., str]

#: Seconds all git commands of one fingerprint may take together.
FINGERPRINT_SECONDS = 90
#: Seconds one ``git status`` may take; it is retried once after a timeout.
_STATUS_SECONDS = 60
#: Seconds one ``git rev-parse HEAD`` may take.
_HEAD_SECONDS = 10
#: The longest git error line kept as a reason.
_REASON_CHARS = 200
_NOT_REACHED = f"git was not read within the {FINGERPRINT_SECONDS} s limit"
_clock = time.monotonic
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
#: Source hashes and imports by path and stat, so polls reread only changed files.
_DIGESTS: dict[tuple[str, int, int, int, int, int], tuple[str, list[str] | None]] = {}
_DIGESTS_GUARD = threading.Lock()
_MAX_DIGESTS = 50_000
#: Headers read, by the SHA-256 of their text: polls read the same texts again
#: and again, which are never kept themselves.
_HEADERS: dict[tuple[bytes, bool], tuple[str, ...] | None] = {}
_HEADERS_GUARD = threading.Lock()
_MAX_HEADERS = 256
#: How much of each source is read for its header's imports.
_HEADER_BYTES = 64 * 1024


def _without_comments(text: str) -> str:
    """Replace nested ``/- ... -/`` blocks and ``--`` line comments by a space.

    A block comment becomes one space even when it spans lines, so text on
    either side of it reads as one line. An unclosed block hides the rest.
    """
    out: list[str] = []
    depth = 0
    position = 0
    while position < len(text):
        pattern = _IN_BLOCK if depth else _OUTSIDE
        match = pattern.search(text, position)
        if match is None:
            if not depth:
                out.append(text[position:])
            break
        token = match[0]
        if not depth:
            out.append(text[position : match.start()])
        if token == "--":
            newline = text.find("\n", match.start())
            position = len(text) if newline < 0 else newline
            continue
        depth += 1 if token == "/-" else -1
        if depth == 0:
            out.append(" ")
        position = match.end()
    return "".join(out)


def direct_imports(text: str) -> list[str]:
    """Return the modules named in the file header, in order."""
    modules: list[str] = []
    for line in _without_comments(text).split("\n"):
        stripped = line.strip()
        if not stripped or _SKIP.match(stripped):
            continue
        match = _IMPORT.match(stripped)
        if match is None:
            break
        modules.extend(match[1].split())
    return modules


def _trace_relative(module: str) -> Path | None:
    """Map a module name to its Lake trace path, or None if it is not a plain name.

    Each dot-separated part must be one Lean identifier component, so a name can
    never be absolute, empty or contain ``..`` and reach outside the build directory.
    """
    parts = module.split(".")
    if not all(_COMPONENT.fullmatch(part) for part in parts):
        return None
    return Path(*parts[:-1], parts[-1] + ".trace")


#: The parts of a Lake trace that identify a build; see `trace_digest`.
_TRACE_IDENTITY = ("schemaVersion", "depHash", "outputs")


def trace_digest(path: Path) -> str | None:
    """Hash the build identity in a Lake trace: its schema, input hash and outputs.

    Only ``schemaVersion``, ``depHash`` and ``outputs`` are hashed. ``depHash``
    is the hash Lake compares to decide that a module is up to date: it mixes
    the source text by content, the toolchain, the options and Lean arguments,
    the module and package names, the libraries, and the import traces and
    artifact hashes, and holds no path. ``outputs`` holds the content hashes of
    the files the build produced. Lake 4.35 also writes ``inputs``, a debug
    dump of the tree whose hashes ``depHash`` mixes, captioned, with the
    source's absolute path as one caption (Lake never hashes captions);
    ``log``, the command line with its paths; and ``synthetic``, true for a
    trace fetched from a cache. All three are dropped, so the same build at
    another path, or fetched instead of built, has the same digest when the
    recorded outputs are equal; Mathlib's traces carry none of them. (A fetch
    from a local cache can record an extra ``l`` output, and then reads as
    out of date: the safe direction.)

    This is sound: dropping ``inputs`` drops only captions, since its hashes
    are already in ``depHash``. Whatever a location really puts into compiled
    bytes (a message, ``include_str`` data, a reflected file name) changes
    ``outputs``, and through the artifact hashes the ``depHash`` of every module
    importing it, so such a build still differs. A fetched trace records the
    input hash as its ``depHash``, and equal ``outputs`` are equal bytes.

    Returns None for an unreadable trace, or one without a string ``depHash``,
    which identifies no build. `environment_fingerprint` makes such a trace,
    unlike a missing one, an unknown state.
    """
    try:
        trace = json.loads(read_regular(path, _MAX_FILE))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(trace, dict) or not isinstance(trace.get("depHash"), str):
        return None
    identity = {key: trace.get(key) for key in _TRACE_IDENTITY}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _reason(text: str) -> str:
    """Return the first non-empty line of git's error output, bounded and printable."""
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    line = "".join(c if c.isprintable() else "?" for c in line)
    return line[:_REASON_CHARS]


def _directory_lock(directory: Path) -> threading.Lock:
    """Return the lock that keeps git runs in one checkout from overlapping."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(directory), threading.Lock())


def git_state(
    directory: Path, rev: str | None, deadline: float | None = None
) -> dict[str, Any]:
    """Report whether a dependency checkout is at its manifest commit and clean.

    Not cached: an unstaged edit changes neither HEAD nor the index, so only
    running ``git status`` each time detects it. On a large checkout such as
    Mathlib with a cold file cache, or during a concurrent snapshot copy,
    ``git status`` can take tens of seconds, so it may take `_STATUS_SECONDS`
    and is retried once after a timeout; ``rev-parse HEAD`` gets
    `_HEAD_SECONDS`. No command runs past `deadline` (a `_clock` value,
    `FINGERPRINT_SECONDS` from now by default), and runs in one checkout never
    overlap, so polls and workers do not pile up on a cold disk.

    If git cannot answer (a timeout, a missing git, or a non-zero exit such
    as git refusing a checkout with dubious ownership), ``head`` or ``clean``
    is None (unknown), never a guess: a slow disk is not a dirty checkout.
    The state then also has a ``reason``, the first line of the last error.
    `environment_fingerprint` makes an unknown state match nothing.
    """
    if deadline is None:
        deadline = _clock() + FINGERPRINT_SECONDS
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") and k != "GLITNIR_TASKVALET_TOKEN"
    }
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    reasons: list[str] = []

    def git(cap: float, *args: str) -> str | None:
        for attempt in range(2):
            remaining = deadline - _clock()
            if remaining <= 0:
                if attempt == 0:  # a retry keeps the timeout as its reason
                    reasons.append(_NOT_REACHED)
                return None
            try:
                done = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false", *args],
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=min(cap, remaining),
                )
            except subprocess.TimeoutExpired:
                reasons.append(f"git {args[0]} did not answer in time")
                continue
            except OSError as error:
                reasons.append(_reason(f"git could not run: {error.strerror}"))
                return None
            if done.returncode == 0:
                return done.stdout.strip()
            reasons.append(
                _reason(done.stderr)
                or f"git {args[0]} exited with status {done.returncode}"
            )
            return None
        return None

    lock = _directory_lock(directory)
    if not lock.acquire(timeout=max(deadline - _clock(), 0)):
        head = status = None
        reasons.append(_NOT_REACHED)
    else:
        try:
            head = git(_HEAD_SECONDS, "rev-parse", "HEAD")
            status = git(
                _STATUS_SECONDS, "status", "--porcelain", "--untracked-files=no"
            )
        finally:
            lock.release()
    state: dict[str, Any] = {
        "head": head,
        "at_manifest": rev is not None and head == rev,
        "clean": None if status is None else status == "",
    }
    if head is None or status is None:
        state["reason"] = reasons[-1]
    return state


def _package_dirs(root: Path) -> list[tuple[dict[str, Any], Path]]:
    try:
        manifest = json.loads(read_regular(root / "lake-manifest.json", _MAX_FILE))
    except (OSError, ValueError, RecursionError):
        return []
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    found = []
    for entry in packages if isinstance(packages, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        directory = entry.get("dir")
        base = (
            root / directory
            if isinstance(directory, str)
            else (root / ".lake/packages" / entry["name"])
        )
        found.append((entry, base))
    return found


def _module_key(module: str) -> str:
    """Spell a module name as its file path spells it, without «» quotes."""
    return ".".join(
        part.removeprefix("«").removesuffix("»") for part in module.split(".")
    )


#: Words of a module header before the first `import`, and before each one.
_HEADER_START = ("module", "prelude")
_IMPORT_MODIFIERS = ("public", "private", "meta")
#: A later line that looks like an import: the header may have been misread.
_LATE_IMPORT = re.compile(r"^\s*(?:(?:public|private|meta)\s+)*import\b", re.M)


class _Incomplete(Exception):
    """The header may continue past the text read so far."""


def _header_tokens(
    text: str, tokens: list[tuple[int, int]], final: bool
) -> tuple[list[str], int]:
    """Read `module`, `prelude` and the imports from Lean's code tokens.

    Returns the imported modules and the number of tokens the header uses.
    `final` says the tokens end where the file does.

    Raises:
        ValueError: The tokens are not a header this reads as Lean does.
        _Incomplete: The tokens end where the header might continue.
    """

    def more() -> NoReturn:
        raise ValueError("the file ends inside an import") if final else _Incomplete

    words = [text[start:end] for start, end in tokens]
    index = 0
    seen: set[str] = set()
    while index < len(words) and words[index] in _HEADER_START:
        if words[index] in seen:
            raise ValueError("repeated header keyword")
        seen.add(words[index])
        index += 1
    modules: list[str] = []
    while index < len(words):
        start = index
        while index < len(words) and words[index] in _IMPORT_MODIFIERS:
            index += 1
        if index == len(words):
            more()
        if words[index] != "import":
            # A command such as `public section` starts after the header.
            return modules, start
        index += 1
        if index < len(words) and words[index] == "all":
            index += 1
        parts: list[str] = []
        while True:
            if index == len(words):
                more()
            part = words[index]
            if not _COMPONENT.fullmatch(part):
                raise ValueError("not a module name")
            parts.append(part)
            index += 1
            # A name continues only through a dot glued to both components.
            if (
                index + 1 < len(words)
                and words[index] == "."
                and tokens[index][0] == tokens[index - 1][1]
                and tokens[index + 1][0] == tokens[index][1]
            ):
                index += 1
                continue
            if index < len(words) and words[index] == ".":
                raise ValueError("a dot that does not join a module name")
            break
        modules.append(".".join(parts))
    if not final:
        raise _Incomplete  # another import may follow
    return modules, len(words)


def header_imports(text: str, complete: bool = True) -> list[str] | None:
    """Return the modules a Lean file's header imports, read with the lexer.

    The header is read from Lean's code tokens (`lexing.code_tokens`), so
    comments, ``«»`` names and the ``module``, ``prelude``, ``public``,
    ``private`` and ``meta`` modifiers are read as Lean reads them, from the
    shortest prefix of whole lines that holds the whole header. Returns None
    when the header is not fully understood: the lexer is unsettled there, a
    word stands where an import may follow, a name is not plain components,
    or a later line looks like another import. Without `complete`, `text`
    is only the start of a file: a header that reaches its end is not
    understood.
    """
    key = (hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest(), complete)
    with _HEADERS_GUARD:
        if key in _HEADERS:
            found = _HEADERS[key]
            return None if found is None else list(found)
    found = _header_imports(text, complete)
    with _HEADERS_GUARD:
        _HEADERS[key] = found
        while len(_HEADERS) > _MAX_HEADERS:
            del _HEADERS[next(iter(_HEADERS))]
    return None if found is None else list(found)


def header_words(text: str) -> tuple[str, ...] | None:
    """Return every code token of a Lean file's header, or None if not understood.

    Unlike `header_imports` this keeps `module`, `prelude` and the import
    modifiers, which change how Lean loads the imports.
    """
    read = _read_header(text, True)
    return None if read is None else read[1]


def _header_imports(text: str, complete: bool) -> tuple[str, ...] | None:
    """Read the header of `text`; see `header_imports`."""
    read = _read_header(text, complete)
    return None if read is None else read[0]


def _read_header(
    text: str, complete: bool
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Read the header's imports and all its tokens; see `header_imports`."""
    if not complete:
        text = text[: text.rfind("\n") + 1]  # the last line may be cut
    lines = text.split("\n")
    count = 1
    while True:
        prefix = "\n".join(lines[:count]) + ("\n" if count < len(lines) else "")
        final = count >= len(lines) and complete
        tokens = code_tokens(prefix)
        if tokens is None:
            if count < len(lines) and scan(prefix).open_at_end:
                count = min(len(lines), count * 2)  # inside a comment or literal
                continue
            return None
        try:
            modules, used = _header_tokens(prefix, tokens, final)
        except _Incomplete:
            if count >= len(lines):
                return None  # the header may continue past what was read
            # Line by line, so the prefix rarely reaches code the lexer
            # cannot settle; a long run of comment lines is read faster.
            count = min(len(lines), count + 1 if count < 64 else count * 2)
            continue
        except ValueError:
            return None
        if _later_import(text, prefix, tokens, used):
            return None
        words = tuple(prefix[start:end] for start, end in tokens[:used])
        return tuple(modules), words


def _later_import(
    text: str, prefix: str, tokens: list[tuple[int, int]], used: int
) -> bool:
    """Whether an `import` follows the header, so the header was misread.

    Lean rejects such a file, but its imports might then be wider than read.
    Where the whole text's tokens are known, only an `import` token counts;
    otherwise any later line that looks like one does.
    """
    every = code_tokens(text)
    if every is not None:
        return any(
            text[start:end] == "import" and text[start - 1 : start] != "."
            for start, end in every[used:]
        )
    start = tokens[used][0] if used < len(tokens) else len(prefix)
    return _LATE_IMPORT.search(text, start) is not None


def _read_source(
    path: str,
) -> tuple[str | None, list[str] | None, tuple[str, int, int, int, int, int] | None]:
    """Hash a regular file by streaming it and read its header's imports.

    Returns the SHA-256 (None if the file could not be read whole), the
    header's imports (None if not understood) and the file's identity from an
    fstat after the read, or None when it changed while being read. Never
    raises, and never follows a symbolic link.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, None, None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None, None, None
        digest = hashlib.sha256()
        head = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            if len(head) < _HEADER_BYTES:
                head += chunk[: _HEADER_BYTES - len(head)]
        after = os.fstat(descriptor)
    except OSError:
        return None, None, None
    finally:
        os.close(descriptor)
    identity: tuple[str, int, int, int, int, int] | None = _stat_key(path, after)
    if identity != _stat_key(path, before):
        identity = None  # written during the read: hashed bytes may be mixed
    text = bytes(head).decode("utf-8", "replace")
    imports = header_imports(text, complete=len(head) < _HEADER_BYTES)
    return digest.hexdigest(), imports, identity


def _stat_key(path: str, info: os.stat_result) -> tuple[str, int, int, int, int, int]:
    return (
        path,
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _source_digest(
    path: str, info: os.stat_result
) -> tuple[str | None, list[str] | None]:
    """Hash a source file and read its imports, reused while its stat is unchanged.

    The digest is None when the file could not be read whole, or changed while
    it was read: its state is then unknown, and is not kept.
    """
    with _DIGESTS_GUARD:
        found = _DIGESTS.get(_stat_key(path, info))
    if found is not None:
        return found
    digest, imports, identity = _read_source(path)
    if digest is None or identity is None:
        return None, imports
    with _DIGESTS_GUARD:
        if len(_DIGESTS) >= _MAX_DIGESTS:
            _DIGESTS.clear()
        _DIGESTS[identity] = (digest, imports)
    return digest, imports


def project_sources(
    root: Path, notebook: Path, imports: list[str] | None = None
) -> dict[str, Any]:
    """Hash the project's own Lean sources that the notebook may import.

    A fresh check never rebuilds (setup uses ``--no-build``), so an edited
    module that Lake has not rebuilt yet makes it unavailable while traces
    still match. With `imports`, the notebook's header imports and any
    provider modules, only sources in the import closure count: those whose
    path ends with the path of an imported module, and those such a source
    imports, transitively. A module may match more than one file; all of them
    count. If any header in the closure is not fully understood
    (`header_imports`), or `imports` is None, every project source counts.

    Each counts by the SHA-256 of its bytes, streamed whatever its size, so a
    rewrite with the same contents (a re-save, a branch switched back) changes
    nothing, and another notebook of the project changes nothing unless it is
    imported. Dot-directories such as ``.lake`` and ``.git`` are skipped,
    symbolic links are never followed, and the notebook itself is left out:
    its text is covered by each claim's prefix hash. A source that cannot be
    read whole or changes while read, and a walk cut short at `_MAX_SOURCES`
    files, are listed under ``unknown``: `environment_fingerprint` then
    matches nothing. Never raises.
    """
    candidates: dict[str, list[tuple[str, str, os.stat_result]]] = {}
    every: list[tuple[str, str, os.stat_result]] = []
    errors: list[str] = []
    unknown: list[str] = []
    truncated = False
    try:
        own = os.lstat(notebook)
        own_id: tuple[int, int] | None = (own.st_dev, own.st_ino)
    except OSError:
        own_id = None

    def failed(error: OSError) -> None:
        errors.append(os.path.relpath(error.filename or root, root))

    for directory, dirnames, filenames in os.walk(root, onerror=failed):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".lean"):
                continue
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root)
            try:
                info = os.lstat(path)
            except OSError:
                errors.append(relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if (info.st_dev, info.st_ino) == own_id:
                continue
            if len(every) >= _MAX_SOURCES:
                truncated = True
                break
            every.append((relative, path, info))
            # Index the file under every module name its path could spell.
            parts = Path(relative).with_suffix("").parts
            for start in range(len(parts)):
                module = ".".join(parts[start:])
                candidates.setdefault(module, []).append((relative, path, info))
        if truncated:
            break
    if truncated:
        unknown.append(f"more than {_MAX_SOURCES} project sources")
    files: dict[str, str | None] = {}
    closure = imports is not None
    pending = [_module_key(module) for module in imports or []]
    while closure and pending:
        for relative, path, info in candidates.get(pending.pop(), []):
            if relative in files:
                continue
            digest, found = _source_digest(path, info)
            files[relative] = digest
            if found is None:
                closure = False  # a header not understood: count every source
                break
            pending.extend(_module_key(module) for module in found)
    if not closure:
        for relative, path, info in every:
            if relative not in files:
                files[relative] = _source_digest(path, info)[0]
    unknown.extend(name for name, digest in sorted(files.items()) if digest is None)
    return {
        "files": sorted([name, digest] for name, digest in files.items()),
        "errors": sorted(errors),
        "truncated": truncated,
        "closure": closure,
        "unknown": unknown,
    }


def checker_identity() -> dict[str, Any]:
    """Identify the checking code shipped with Glitnir and what it accepts.

    The helper programs are the exact files a check copies into its snapshot,
    and the program that elaborates provider Lean for views and evidence, so
    an upgrade that changes checking or evidence invalidates every saved result.
    The supported toolchain is recorded too, so moving to another Lean
    invalidates them even when no helper program changes.
    """
    paths = [
        project_checking.PROGRAMS / name
        for name in ("ProjectRead.lean", "ProjectReport.lean", "ProjectReplay.lean")
    ]
    paths.append(axiom_report.REPORT_PROGRAM)
    paths.append(provider_lean.PROGRAM)
    programs: dict[str, str | None] = {}
    for path in paths:
        try:
            programs[path.name] = hashlib.sha256(
                read_regular(path, _MAX_FILE)
            ).hexdigest()
        except OSError:
            programs[path.name] = None
    return {
        "toolchain": project_inspection.TOOLCHAIN,
        "programs": programs,
        "axiom_allowlist": sorted(project_checking.LEAN_AXIOM_ALLOWLIST),
    }


def environment_fingerprint(
    source: Path,
    text: str,
    imports: tuple[str, ...] = (),
    known_packages: list[dict[str, Any]] | None = None,
    remote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Identify toolchain, Lake files, builds, project sources and dependencies.

    The identity holds no absolute path: the toolchain named by
    ``lean-toolchain``, the Lake files by content, each dependency by its
    manifest revision, HEAD and cleanliness, each imported module by its
    `trace_digest`, the project sources by relative path and content
    (`project_sources`), and the checker (`checker_identity`). Under remote
    execution the caller passes `remote`, the fields it adds as they are:
    ``execution``, the selected backend and its Taskvalet URL, project and
    environment; ``compute_context``, the prepared dependency context by
    relative path and content; and ``compute_checker``, the installed checker's
    files by relative path and content (`glitnir.compute.client.remote_identity`
    makes them; this layer cannot import it). None of them holds a path
    either. So the same sources and byte-identical builds in another checkout,
    or on another machine, give the same digest, and results saved there are
    current here.

    `imports` are modules loaded besides the notebook's own, as a view or
    evidence provider's are: their project sources and build traces count as
    the notebook's imports do. `known_packages`, the dependency states of another
    fingerprint of the same notebook taken just before, are reused rather
    than asking git again.

    Saved results keep only the digest. When the HEAD or cleanliness of a
    git dependency is unknown (see `git_state`), a project source could not
    be read (see `project_sources`), or an import's trace exists but is no
    Lake trace (`trace_digest` gives None; it is then listed under
    ``unreadable_traces``), the digest also covers a fresh random
    value, so it matches no saved result, not even one saved while the state
    was unknown, and nothing saved under it is ever current. Results then
    read as out of date until the state is known again; an unknown state is
    never taken for a known one. `unknown_git_note` explains such a
    fingerprint. ``stable_sha256`` is the same identity without the random
    value, with each unknown state marked as unknown: it names work done for
    an environment (a drawing, say) but never says a saved result is current.
    Git gets `FINGERPRINT_SECONDS` in total; dependencies it does not reach in
    time are unknown.
    """
    project = describe_project(source)
    files = {
        name: info.get("sha256") or info.get("error")
        for name, info in project.get("files", {}).items()
    }
    packages: list[dict[str, Any]] = []
    roots: list[Path] = []
    sources: dict[str, Any] | None = None
    header = header_imports(text)
    named = [*(direct_imports(text) if header is None else header)]
    named += [module for module in imports if module not in named]
    if project["kind"] == "lake":
        root = Path(project["root"])
        roots.append(root)
        sources = project_sources(root, source, None if header is None else named)
        deadline = _clock() + FINGERPRINT_SECONDS
        known = {
            (package.get("name"), package.get("rev")): package
            for package in known_packages or []
            if isinstance(package, dict)
        }
        for entry, base in _package_dirs(root):
            roots.append(base)
            reused = known.get((entry["name"], entry.get("rev")))
            if reused is not None:
                packages.append(dict(reused))
                continue
            state = (
                git_state(base, entry.get("rev"), deadline)
                if entry.get("type") == "git" and (base / ".git").exists()
                else {"head": None, "at_manifest": False, "clean": False}
            )
            packages.append({"name": entry["name"], "rev": entry.get("rev"), **state})
    traces: dict[str, str | None] = {}
    unreadable: list[str] = []
    for module in named:
        relative = _trace_relative(module)
        traces[module] = None
        if relative is None:
            continue
        for base in roots:
            path = base / ".lake/build/lib/lean" / relative
            traces[module] = trace_digest(path)
            if traces[module] is not None:
                break
            if _exists(path):
                # Present but no Lake trace: unknown, never a missing build.
                unreadable.append(module)
                break
    identity = {
        "toolchain": project.get("toolchain"),
        "files": files,
        "packages": packages,
        "imports": traces,
        "sources": sources,
        "checker": checker_identity(),
    }
    if unreadable:
        identity["unreadable_traces"] = sorted(unreadable)
    if remote:
        identity.update(remote)
    unknown = (
        any("reason" in package for package in packages)
        or bool(sources and sources["unknown"])
        or bool(unreadable)
    )
    stable: dict[str, Any] = {
        **identity,
        "packages": [
            {**package, "reason": "unknown"} if "reason" in package else package
            for package in packages
        ],
    }
    hashed: dict[str, Any] = dict(identity)
    if unknown:
        hashed["unknown"] = secrets.token_hex(16)
    return {
        "sha256": _digest(hashed),
        "stable_sha256": _digest(stable),
        "unknown": unknown,
        **identity,
    }


def _exists(path: Path) -> bool:
    """Whether something is at `path`; True when that cannot be told."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _digest(identity: dict[str, Any]) -> str:
    return hashlib.sha256(
        b"glitnir.environment-fingerprint/v4\0"
        + json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()


def unknown_git_note(fingerprint: dict[str, Any]) -> str | None:
    """Explain the git states, sources and build traces a fingerprint could not read.

    Returns:
        A sentence naming them and why, or None when every state is known.
    """
    names: dict[str, list[str]] = {}
    for package in fingerprint.get("packages", []):
        if "reason" in package:
            names.setdefault(package["reason"], []).append(package["name"])
    sources = (fingerprint.get("sources") or {}).get("unknown") or []
    traces = fingerprint.get("unreadable_traces") or []
    notes = []
    if names:
        parts = "; ".join(f"{', '.join(found)}: {why}" for why, found in names.items())
        notes.append(f"Could not read the git state of {parts}")
    if sources:
        shown = ", ".join(sources[:5]) + (", …" if len(sources) > 5 else "")
        notes.append(f"Could not read the project sources ({shown})")
    if traces:
        shown = ", ".join(traces[:5]) + (", …" if len(traces) > 5 else "")
        notes.append(f"Could not read the build traces of {shown}")
    if not notes:
        return None
    return "; ".join(notes) + "; results are saved as out of date."
