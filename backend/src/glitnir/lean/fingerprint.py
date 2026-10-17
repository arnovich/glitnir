"""A fast identity for the environment a notebook is checked in.

It reads small files Lean and Lake already maintain, never ``.olean`` files,
and runs no project code. It decides whether saved results are still current;
the full identity recorded by an explicit check stays the authority.
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
from typing import Any

from glitnir.lean import axiom_report, project_checking
from glitnir.lean.project import describe_project, read_regular

_IMPORT = re.compile(r"^(?:(?:public|private|meta)\s+)*import\s+(.+)$")
_SKIP = re.compile(r"^(?:module|prelude)\s*$")
_OUTSIDE = re.compile(r"/-|--")
_IN_BLOCK = re.compile(r"/-|-/")
_MAX_FILE = 4 * 1024 * 1024
_MAX_SOURCES = 20000
_COMPONENT = re.compile(r"[A-Za-z_][A-Za-z0-9_'!?]*|«[^/\\\x00]+»")
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


def trace_digest(path: Path) -> str | None:
    """Hash a Lake trace without its machine-specific build log."""
    try:
        trace = json.loads(read_regular(path, _MAX_FILE))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(trace, dict):
        return None
    trace.pop("log", None)
    return hashlib.sha256(json.dumps(trace, sort_keys=True).encode()).hexdigest()


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
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
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


def project_sources(root: Path, notebook: Path) -> dict[str, Any]:
    """List size and modification time of the project's own Lean sources.

    A fresh check never rebuilds (setup uses ``--no-build``), so an edited
    module that Lake has not rebuilt yet makes it unavailable while traces
    still match. Dot-directories such as ``.lake`` and ``.git`` are skipped,
    symbolic links are never followed, and the notebook itself is left out:
    its text is covered by each claim's prefix hash. Never raises.
    """
    files: list[list[Any]] = []
    errors: list[str] = []
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
            if len(files) >= _MAX_SOURCES:
                truncated = True
                break
            files.append([relative, info.st_size, info.st_mtime_ns])
        if truncated:
            break
    files.sort()
    return {"files": files, "errors": sorted(errors), "truncated": truncated}


def checker_identity() -> dict[str, Any]:
    """Identify the checking code shipped with Glitnir and what it accepts.

    The helper programs are the exact files a check copies into its snapshot,
    so an upgrade that changes checking invalidates every saved result.
    """
    paths = [
        project_checking.PROGRAMS / name
        for name in ("ProjectRead.lean", "ProjectReport.lean", "ProjectReplay.lean")
    ]
    paths.append(axiom_report.REPORT_PROGRAM)
    programs: dict[str, str | None] = {}
    for path in paths:
        try:
            programs[path.name] = hashlib.sha256(
                read_regular(path, _MAX_FILE)
            ).hexdigest()
        except OSError:
            programs[path.name] = None
    return {
        "programs": programs,
        "axiom_allowlist": sorted(project_checking.LEAN_AXIOM_ALLOWLIST),
    }


def environment_fingerprint(source: Path, text: str) -> dict[str, Any]:
    """Identify toolchain, Lake files, builds, project sources and dependencies.

    Saved results keep only the digest. When the HEAD or cleanliness of a
    git dependency is unknown (see `git_state`), the digest also covers a
    fresh random value, so it matches no saved result, not even one saved
    while the state was unknown, and nothing saved under it is ever current.
    Results then read as out of date until git answers again; an unknown
    state is never taken for a clean one that may in fact be dirty.
    `unknown_git_note` explains such a fingerprint. Git gets
    `FINGERPRINT_SECONDS` in total; dependencies it does not reach in time
    are unknown.
    """
    project = describe_project(source)
    files = {
        name: info.get("sha256") or info.get("error")
        for name, info in project.get("files", {}).items()
    }
    packages: list[dict[str, Any]] = []
    roots: list[Path] = []
    sources: dict[str, Any] | None = None
    if project["kind"] == "lake":
        root = Path(project["root"])
        roots.append(root)
        sources = project_sources(root, source)
        deadline = _clock() + FINGERPRINT_SECONDS
        for entry, base in _package_dirs(root):
            roots.append(base)
            state = (
                git_state(base, entry.get("rev"), deadline)
                if entry.get("type") == "git" and (base / ".git").exists()
                else {"head": None, "at_manifest": False, "clean": False}
            )
            packages.append({"name": entry["name"], "rev": entry.get("rev"), **state})
    imports: dict[str, str | None] = {}
    for module in direct_imports(text):
        relative = _trace_relative(module)
        imports[module] = None
        if relative is None:
            continue
        imports[module] = next(
            (
                digest
                for base in roots
                if (digest := trace_digest(base / ".lake/build/lib/lean" / relative))
            ),
            None,
        )
    identity = {
        "toolchain": project.get("toolchain"),
        "files": files,
        "packages": packages,
        "imports": imports,
        "sources": sources,
        "checker": checker_identity(),
    }
    hashed: dict[str, Any] = dict(identity)
    if any("reason" in package for package in packages):
        hashed["unknown"] = secrets.token_hex(16)
    digest = hashlib.sha256(
        b"glitnir.environment-fingerprint/v2\0"
        + json.dumps(hashed, sort_keys=True).encode()
    ).hexdigest()
    return {"sha256": digest, **identity}


def unknown_git_note(fingerprint: dict[str, Any]) -> str | None:
    """Explain the dependencies whose git state a fingerprint could not read.

    Returns:
        A sentence naming them and why, or None when every state is known.
    """
    names: dict[str, list[str]] = {}
    for package in fingerprint.get("packages", []):
        if "reason" in package:
            names.setdefault(package["reason"], []).append(package["name"])
    if not names:
        return None
    parts = "; ".join(f"{', '.join(found)}: {why}" for why, found in names.items())
    return (
        f"Could not read the git state of {parts}; " "results are saved as out of date."
    )
