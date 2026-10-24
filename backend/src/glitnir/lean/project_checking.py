"""Independent observations of a saved Lean file under a private project snapshot."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Callable

from glitnir.lean import replay_cache
from glitnir.lean.artifacts import olean_path
from glitnir.lean.awake import awake, noting_sleep
from glitnir.lean.axiom_report import REPORT_PROGRAM, REPORT_SHA256
from glitnir.lean.environment import (
    LEAN_AXIOM_ALLOWLIST,
    bind_to_parent,
    kill_and_reap,
)
from glitnir.lean.lexing import (
    CommandBoundaries,
    code_tokens,
    command_boundaries,
    only_comments,
)
from glitnir.lean.limits import check_seconds, memory_limit_mb
from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_inspection import (
    TOOLCHAIN,
    _toolchain,
    inspect_project,
    lean_support,
)
from glitnir.lean.project_snapshot import (
    SLOT_WAITING,
    ProjectSnapshot,
    SnapshotError,
    artifact_lease,
    capture,
    copy_file,
    module_path,
    snapshot_slot,
)
from glitnir.lean.sandbox import SandboxUnavailable, confine
from glitnir.lean.scratch import scratch_directory

PROGRAMS = Path(__file__).parent
OUTPUT_BYTES = 16 * 1024**2
#: What `run_observer` raises when a run reaches its own deadline.
TIMED_OUT = "Project check timed out."
MEMORY_EXHAUSTED = (
    "Lean reached its memory limit of {limit} MB. The limit includes the library "
    "files Lean loads (about 4–5 GB with Mathlib) as well as its working memory. "
    "Raise the glitnir.memoryLimitMb setting and try again."
)
MODULE_BYTES = 256 * 1024**2
#: The one-time replay of an import closure has its own, longer deadline; the
#: check's own budget starts again after it.
IMPORTS_REPLAY_SECONDS = 3600
STAGE_BYTES = 512 * 1024**2
STAGE_ENTRIES = 4096
#: At most this many uses are recorded per claim; more are marked truncated.
MAX_USES = 200
#: The prefix of the theorem names examples are checked under; see `example_tag`.
EXAMPLE_TAG = "_glitnir_example_"
_KEYWORD = b"example"
#: Added to an unknown-identifier failure inside an example.
EXAMPLE_NOTE = (
    "Glitnir checks this example as a theorem, which includes only the section "
    "variables its statement mentions"
)
#: What may follow a checked example's keyword: whitespace or a signature.
_AFTER_KEYWORD = (b" ", b"\t", b"\n", b"(", b"{", b"[", b":", "\u2983".encode())
#: Declaration modifiers; an example after one is not checked.
_MODIFIERS = frozenset(
    (
        "private",
        "protected",
        "public",
        "meta",
        "noncomputable",
        "unsafe",
        "partial",
        "nonrec",
    )
)
CARRIAGE_RETURN = (
    "This file contains a carriage return outside a CRLF line ending, which Lean "
    "reads differently from the checker; remove carriage returns and save again."
)


class StageTimes:
    """How long each stage of a check took, in milliseconds.

    Measured on the monotonic clock, so time the computer slept is left out.
    Display only: never part of a status, a saved record or an identity. The
    stages are `resolve` (`lake setup-file`), `capture` (the snapshot), `read`
    (ProjectRead), `compile` (the candidate), `report_reference` and
    `report_candidate` (ProjectReport, each with its isolated tree), `reports`
    (both reports from start to end), `replay_imports` (a one-time replay of
    the imports, when one runs) and `replay` (ProjectReplay); `total` is the
    whole call. A stage run again, as when a check retries the source before
    its first failure, adds up.
    """

    def __init__(self) -> None:
        """Start timing the whole call now."""
        self._started = time.monotonic()
        self._ms: dict[str, int] = {}
        self._lock = threading.Lock()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time the block as stage ``name``, whether or not it raises."""
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            with self._lock:
                self._ms[name] = self._ms.get(name, 0) + elapsed

    def summary(self) -> dict[str, int]:
        """Return each stage's milliseconds so far, and the `total`."""
        with self._lock:
            return {
                **self._ms,
                "total": round((time.monotonic() - self._started) * 1000),
            }


def stage_summary(stage_ms: dict[str, int]) -> str:
    """Write a check's `stage_ms` on one line, in seconds, for logs."""
    return ", ".join(f"{name} {ms / 1000:.1f} s" for name, ms in stage_ms.items())


def _check_stage(root: Path) -> None:
    """Monitor scratch use without following candidate-created symbolic links."""
    total = entries = 0

    def visit(descriptor: int, depth: int) -> None:
        nonlocal total, entries
        if depth > 64:
            raise SnapshotError("Project check exceeded its scratch directory depth.")
        with os.scandir(descriptor) as children:
            for child in children:
                entries += 1
                if entries > STAGE_ENTRIES:
                    raise SnapshotError(
                        "Project check exceeded its scratch file limit."
                    )
                try:
                    info = child.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        nested = os.open(
                            child.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor,
                        )
                        try:
                            visit(nested, depth + 1)
                        finally:
                            os.close(nested)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                except (FileNotFoundError, NotADirectoryError):
                    continue
                if total > STAGE_BYTES:
                    raise SnapshotError(
                        "Project check exceeded its scratch byte limit."
                    )

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor, 0)
    finally:
        os.close(descriptor)


def _run(
    snapshot: ProjectSnapshot,
    arguments: list[str],
    *,
    cwd: Path,
    readable: list[Path],
    writable: bool,
    cancel: threading.Event,
    deadline: float,
) -> str:
    """Run one frozen stage, exposing no original project or toolchain paths.

    The snapshot's stored files are visible read-only through their store
    namespaces: the toolchain's and this project's, never another project's.
    """
    stored = sorted(set(snapshot.stores.values()))
    return run_observer(
        snapshot.toolchain,
        arguments,
        cwd=cwd,
        readable=[snapshot.root, *stored, *readable],
        writable=writable,
        cancel=cancel,
        deadline=deadline,
    )


def run_observer(
    toolchain: Path,
    arguments: list[str],
    *,
    cwd: Path,
    readable: list[Path],
    writable: bool,
    cancel: threading.Event,
    deadline: float,
) -> str:
    """Run bounded Lean with only explicit read roots and private writable cwd.

    This execution primitive grants no status or immutable-input guarantee.
    The project checker supplies its frozen roots; observations may supply the
    installed toolchain and prepared project as read-only inputs.
    """
    if cancel.is_set():
        raise SnapshotError("Project check cancelled.")
    # All helper source compilation happens with a toolchain-only search path.
    environment = {
        "PATH": f"{toolchain / 'bin'}:/usr/bin:/bin",
        "LEAN_SYSROOT": str(toolchain),
        "LEAN_PATH": "",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
    }
    command = confine(
        [str(toolchain / "bin" / arguments[0]), *arguments[1:]],
        readable=readable,
        writable=[cwd] if writable else [],
        cwd=cwd,
        required=True,
    )
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, awake():
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            preexec_fn=partial(bind_to_parent, os.getpid(), MODULE_BYTES),
        )
        try:
            while process.poll() is None:
                if writable:
                    _check_stage(cwd)
                if cancel.is_set():
                    raise SnapshotError("Project check cancelled.")
                if time.monotonic() >= deadline:
                    raise SnapshotError(TIMED_OUT)
                if any(
                    os.fstat(stream.fileno()).st_size > OUTPUT_BYTES
                    for stream in (out, err)
                ):
                    raise SnapshotError("Project check exceeded its output limit.")
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if writable:
                _check_stage(cwd)
            texts = []
            for stream in (out, err):
                if os.fstat(stream.fileno()).st_size > OUTPUT_BYTES:
                    raise SnapshotError("Project check exceeded its output limit.")
                stream.seek(0)
                texts.append(
                    stream.read(OUTPUT_BYTES + 1).decode("utf-8", errors="replace")
                )
            if process.returncode != 0:
                limit = next(
                    (a[2:] for a in arguments if re.fullmatch(r"-M\d+", a)), None
                )
                if limit and "excessive memory consumption detected" in texts[1]:
                    raise SnapshotError(MEMORY_EXHAUSTED.format(limit=limit))
                raise SnapshotError(
                    f"{arguments[0]} did not complete successfully "
                    f"(exit {process.returncode}).\n" + (texts[1] or texts[0])[-6000:]
                )
            return texts[0]
        finally:
            kill_and_reap(process)


def _programs(destination: Path) -> tuple[Path, Path, Path]:
    """Compose fixed reporting code, never from user-controlled source text."""
    common = REPORT_PROGRAM.read_bytes()
    if hashlib.sha256(common).hexdigest() != REPORT_SHA256:
        raise SnapshotError("The independent report program was modified.")
    marker = b"def main (args : List String)"
    if common.count(marker) != 1:
        raise SnapshotError("The independent report program has an unexpected shape.")
    destination.mkdir()
    reader = destination / "ProjectRead.lean"
    reporter = destination / "ProjectReport.lean"
    reader.write_bytes((PROGRAMS / reader.name).read_bytes())
    reporter.write_bytes(
        common.split(marker)[0] + (PROGRAMS / reporter.name).read_bytes()
    )
    replay = destination / "ProjectReplay.lean"
    replay.write_bytes((PROGRAMS / replay.name).read_bytes())
    return reader, reporter, replay


def _isolate(build: Path, destination: Path, module: str) -> None:
    """Move only bounded regular module data; no elaborator helpers or executables."""
    relative = module_path(module)
    for suffix in (".olean", ".olean.server", ".olean.private"):
        source = build / (str(relative) + suffix)
        if suffix == ".olean" or source.exists():
            copy_file(
                source, destination / (str(relative) + suffix), max_bytes=MODULE_BYTES
            )


def _reports(value: Any) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Validate independent constant and theorem reports before comparison."""
    if not isinstance(value, dict) or not isinstance(value.get("imports"), list):
        raise SnapshotError("The independent module report is malformed.")
    constants: dict[str, dict[str, Any]] = {}
    theorems: dict[str, dict[str, Any]] = {}
    for field, result in (("constants", constants), ("theorems", theorems)):
        if not isinstance(value.get(field), list):
            raise SnapshotError("The independent module report is malformed.")
        for item in value[field]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or item["name"] in result
            ):
                raise SnapshotError("The independent declaration report is malformed.")
            result[item["name"]] = item
    for item in constants.values():
        if not all(
            isinstance(item.get(key), str) for key in ("kind", "meaning")
        ) or not isinstance(item.get("levels"), list):
            raise SnapshotError("The independent constant report is malformed.")
    for item in theorems.values():
        if not all(
            isinstance(item.get(key), str) for key in ("canonical", "type")
        ) or not isinstance(item.get("axioms"), list):
            raise SnapshotError("The independent theorem report is malformed.")
        for axiom in item["axioms"]:
            if (
                not isinstance(axiom, dict)
                or not isinstance(axiom.get("name"), str)
                or not isinstance(axiom.get("module"), (str, type(None)))
            ):
                raise SnapshotError("The independent axiom report is malformed.")
        uses = item.get("uses", [])
        if not isinstance(uses, list) or not all(
            isinstance(use, dict)
            and isinstance(use.get("name"), str)
            and isinstance(use.get("module"), str)
            for use in uses
        ):
            raise SnapshotError("The independent theorem report is malformed.")
    return constants, theorems


def project_modules(setup: dict[str, Any], project_root: Path | None) -> set[str]:
    """Return the imported modules the checked project builds itself.

    These are the project's own `.lean` files, the other notebooks a claim may
    use, as opposed to its dependencies' and the toolchain's modules: their
    compiled files lie in the project, outside `.lake/packages`.
    """
    if project_root is None:
        return set()
    packages = project_root / ".lake" / "packages"
    found = set()
    for module, paths in setup["importArts"].items():
        olean = Path(olean_path(paths))
        if not olean.is_absolute():
            olean = project_root / olean
        try:
            resolved = olean.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_relative_to(project_root) and not resolved.is_relative_to(
            packages
        ):
            found.add(module)
    return found


def claim_uses(
    reported: list[dict[str, str]],
    module: str,
    claims: set[str],
    notebooks: set[str],
) -> list[dict[str, Any]]:
    """Return the claims a theorem uses, from the theorems it names directly.

    Display only: what the module report says a theorem's type and value name,
    kept when it is a claim of this notebook (`claims`, in `module`) or a
    theorem of another notebook of the project (a module in `notebooks`).
    """
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for use in reported:
        name, owner = use["name"], use["module"]
        if owner == module:
            entry = {"module": owner, "name": name, "in_notebook": True}
            if name not in claims:
                continue
        elif owner in notebooks and not name.startswith("_private."):
            entry = {"module": owner, "name": name, "in_notebook": False}
        else:
            continue
        if (owner, name) not in seen:
            seen.add((owner, name))
            found.append(entry)
    return found


def compare_reports(
    reference: Any, candidate: Any, names: list[str]
) -> list[dict[str, Any]]:
    """Bind exact theorem names/propositions and every reference non-proof constant."""
    expected, expected_theorems = _reports(reference)
    found, found_theorems = _reports(candidate)
    if (
        reference.get("module") != candidate.get("module")
        or reference["imports"] != candidate["imports"]
    ):
        raise SnapshotError("Candidate and reference imports/module differ.")
    for name, constant in expected.items():
        other = found.get(name)
        if (
            other is None
            or constant["kind"] != other["kind"]
            or constant["levels"] != other["levels"]
        ):
            raise SnapshotError(f"The candidate changed reference declaration {name}.")
        if constant["kind"] != "theorem" and constant["meaning"] != other["meaning"]:
            raise SnapshotError(f"The candidate changed reference definition {name}.")
    for name, theorem in expected_theorems.items():
        if (
            name not in found_theorems
            or theorem["canonical"] != found_theorems[name]["canonical"]
        ):
            raise SnapshotError(
                f"The candidate changed the reference proposition {name}."
            )
    if len(names) != len(set(names)) or any(
        name not in expected_theorems for name in names
    ):
        raise SnapshotError(
            "Source declaration identities do not match the reference module."
        )
    return [found_theorems[name] for name in names]


def _closure(report: Any, module: str) -> list[list[str]] | None:
    """Return the reporter's loaded closure without ``module``, which comes last."""
    modules = report.get("modules") if isinstance(report, dict) else None
    if (
        not isinstance(modules, list)
        or not modules
        or not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in modules
        )
        or modules[-1][0] != module
    ):
        return None
    return modules[:-1]


def _module_files(
    snapshot: ProjectSnapshot, isolated: Path, name: str, loaded: str
) -> dict[str, str] | None:
    """Return the snapshot digests of the files a loaded module was read from.

    The module must have been read from its own path under the isolated
    dependency tree (links to snapshot artifacts) or the snapshot toolchain,
    and each of its files must be exactly the snapshot's entry, or absent from
    both. Each entry must link to the stored file named by its recorded digest,
    whose bytes never change. None means some file is unknown.
    """
    try:
        relative = str(module_path(name))
    except SnapshotError:
        return None
    for root, prefix, linked in (
        (isolated, "artifacts", True),
        (snapshot.toolchain / "lib/lean", "toolchain/lib/lean", False),
    ):
        if Path(loaded) == root / (relative + ".olean"):
            break
    else:
        return None
    files = {}
    for suffix in replay_cache.MODULE_SUFFIXES:
        location = root / (relative + suffix)
        digest = snapshot.digests.get(f"{prefix}/{relative}{suffix}")
        try:
            info = location.lstat()
        except FileNotFoundError:
            if digest is not None:
                return None
            files[suffix] = "absent"
            continue
        except OSError:
            return None
        if digest is None:
            return None
        if linked:
            target = snapshot.artifacts / (relative + suffix)
            if not stat.S_ISLNK(info.st_mode) or os.readlink(location) != str(target):
                return None
            location = target
        stored = snapshot.stored(f"{prefix}/{relative}{suffix}", digest)
        try:
            if stored is None or os.readlink(location) != str(stored):
                return None
        except OSError:
            return None
        files[suffix] = digest
    return files


def _replay_identity(
    snapshot: ProjectSnapshot,
    isolated: Path,
    program: Path,
    imports: Any,
    closure: list[list[str]],
) -> tuple[str, str, list[list[str]]] | None:
    """Key, context and digested modules of these imports; None if a file is unknown."""
    toolchain = {
        path: digest
        for path, digest in snapshot.digests.items()
        if path.startswith("toolchain/")
    }
    if not toolchain:
        return None
    modules = []
    for name, loaded in closure:
        files = _module_files(snapshot, isolated, name, loaded)
        if files is None:
            return None
        modules.append([name, replay_cache.module_digest(files)])
    context = replay_cache.context_digest(
        toolchain=toolchain,
        program_sha256=hashlib.sha256(program.read_bytes()).hexdigest(),
    )
    key = replay_cache.import_key(context=context, imports=imports, modules=modules)
    return key, context, modules


def _axiom_entry(value: Any) -> bool:
    """Whether ``value`` is one axiom as both helpers write it: name and module."""
    return (
        isinstance(value, dict)
        and set(value) == {"name", "module"}
        and isinstance(value["name"], str)
        and isinstance(value["module"], (str, type(None)))
    )


def _replay_report(
    output: str, closure: list[list[str]], imports: Any
) -> dict[str, Any]:
    """Validate the kernel replay's report and that it loaded the predicted closure."""
    try:
        value = json.loads(output)
    except ValueError:
        value = None
    if (
        not isinstance(value, dict)
        or value.get("replay")
        not in ("full", "imports only", "imports incremental", "imports cached")
        or type(value.get("imports_replayed")) is not bool
        or type(value.get("extendable")) is not bool
        or not (
            value.get("base_modules") is None
            or (
                type(value["base_modules"]) is int
                and 0 <= value["base_modules"] <= len(closure)
            )
        )
        or (value["replay"] == "imports incremental")
        != (value.get("base_modules") is not None)
        or value.get("fresh") not in (None, "restated", "quotient")
        or not isinstance(value.get("imports"), list)
        or not isinstance(value.get("modules"), list)
        or not isinstance(value.get("theorems"), list)
        or not all(
            isinstance(item, dict)
            and set(item) == {"name", "axioms"}
            and isinstance(item["name"], str)
            and isinstance(item["axioms"], list)
            and all(_axiom_entry(axiom) for axiom in item["axioms"])
            for item in value["theorems"]
        )
        or len({item["name"] for item in value["theorems"]}) != len(value["theorems"])
        or (
            value["replay"] in ("imports only", "imports incremental")
            and value["theorems"]
        )
    ):
        raise SnapshotError("The independent kernel replay report is malformed.")
    if value["imports"] != imports or value["modules"] != closure:
        raise SnapshotError(
            "Independent kernel replay loaded a different import closure."
        )
    return value


def _replayed_axioms(
    replayed: dict[str, Any], reported: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Return each declared theorem's axioms as the kernel replay collected them.

    The replay collects them in the environment where it kernel-checked the
    module, which holds no imported extension state; they alone decide a
    theorem's status. The module report collects them again over Lean's own
    import. Both must list the same theorems with equal axioms, in the same
    order and with the same modules, or the check fails: a difference means one
    of the two read something the other did not.
    """
    found = {item["name"]: item["axioms"] for item in replayed["theorems"]}
    if set(found) != set(reported):
        raise SnapshotError(
            "The independent kernel replay and the module report list different "
            "theorems."
        )
    for name, theorem in reported.items():
        if theorem["axioms"] != found[name]:
            raise SnapshotError(
                "The independent kernel replay and the module report disagree on "
                f"the axioms of {name}."
            )
    return found


def _toolchain_provenance(snapshot: ProjectSnapshot, closure: list[list[str]]) -> None:
    """Require every module the toolchain provides to be loaded from the toolchain."""
    for name, loaded in closure:
        try:
            relative = str(module_path(name))
        except SnapshotError:
            continue  # Not a name the toolchain uses.
        trusted = snapshot.toolchain / "lib/lean" / (relative + ".olean")
        if os.path.lexists(trusted) and Path(loaded) != trusted:
            raise SnapshotError(
                f"The toolchain module {name} was loaded from outside the toolchain."
            )


def line_range(text: str, start: int, end: int) -> tuple[int, int]:
    """Return 1-based inclusive lines for UTF-8 byte offsets into LF text."""
    data = text.encode()
    first = data.count(b"\n", 0, start) + 1
    last = data.count(b"\n", 0, max(start, end - 1)) + 1
    return first, last


class CommandFailure(SnapshotError):
    """A Lean command failed; everything before ``offset`` may still be checked."""

    def __init__(self, message: str, *, offset: int, note: str = "") -> None:
        """Record the failure message, the command's byte offset and a note."""
        super().__init__(message)
        self.offset = offset
        self.note = note


def example_tag(text: str) -> str:
    """Return the prefix of the names the examples of ``text`` are checked under.

    The example whose `example` keyword starts at byte ``n`` is checked as
    ``private theorem <tag>n``. The tag occurs nowhere in ``text``, so the
    source cannot declare or mention any of these names itself.
    """
    tag = EXAMPLE_TAG
    while tag in text:
        tag = "_" + tag
    return tag


def _byte_offsets(text: str, offsets: list[int]) -> list[int]:
    """Convert increasing character offsets into ``text`` to UTF-8 byte offsets."""
    result = []
    position = size = 0
    for offset in offsets:
        size += len(text[position:offset].encode())
        position = offset
        result.append(size)
    return result


def _span(item: Any, size: int) -> tuple[int, int]:
    """Return a reported example's keyword span, checked against its command."""
    span = item.get("keyword") if isinstance(item, dict) else None
    if (
        not isinstance(span, list)
        or len(span) != 2
        or not all(type(value) is int for value in span)
        or type(item.get("start")) is not int
        or type(item.get("end")) is not int
    ):
        raise SnapshotError("The source declaration report is malformed.")
    first, last = span
    if (
        last - first != len(_KEYWORD)
        or not 0 <= item["start"] <= first < last <= item["end"] <= size
    ):
        raise SnapshotError("The source declaration report is malformed.")
    return first, last


def _token(text: str, tokens: list[tuple[int, int]], index: int) -> str:
    start, end = tokens[index]
    return text[start:end]


def _attribute_or_modifier(
    text: str, tokens: list[tuple[int, int]], index: int
) -> bool:
    """Whether code token ``index`` follows an attribute or a modifier.

    That is, the token before it is a modifier keyword, or the `]` closing an
    `@[…]` attribute list. Doc comments are not tokens, so they never count.
    """
    if index == 0:
        return False
    before = _token(text, tokens, index - 1)
    if before in _MODIFIERS:
        return text[tokens[index - 1][0] - 1 : tokens[index - 1][0]] != "."
    if before != "]":
        return False
    depth = 0
    for position in range(index - 1, -1, -1):
        piece = _token(text, tokens, position)
        depth += {"]": 1, "[": -1}.get(piece, 0)
        if depth == 0:
            return (
                position > 0
                and _token(text, tokens, position - 1) == "@"
                and tokens[position - 1][1] == tokens[position][0]
            )
    return False


def _boundary_before(data: bytes, offset: int, items: list[dict[str, Any]]) -> int:
    """Return the end of the last reported command before byte ``offset``.

    It ends a whole command, so the source before it can be checked alone; 0
    when there is none, which makes the check unavailable.
    """
    ends: list[int] = [item["end"] for item in items if item["end"] <= offset]
    if not ends:
        return 0
    end = max(ends)
    return end + 1 if data[end : end + 1] == b"\n" else end


def example_edits(
    text: str,
    entries: list[dict[str, Any]],
    unclaimed: list[dict[str, Any]],
    tag: str,
) -> tuple[list[tuple[int, bytes]], list[dict[str, Any]]]:
    """Return the edits for the claimed examples, and the examples claiming nothing.

    The reader, which runs the source's own code, is untrusted: it only
    classifies. Every `example` keyword Lean reads in ``text`` is found here by
    the trusted lexer, and the reader must account for exactly those, each
    once, as a claim (an entry with ``keyword``) or in ``unclaimed``. A claim's
    keyword must follow nothing but whitespace, comments and at most one
    docstring from its command's start, be followed by whitespace or its
    signature, and its theorem must carry the name the rewrite gives it.
    Otherwise the report is malformed. Examples claiming nothing are returned
    only when the lexer read the file exactly.

    Raises:
        CommandFailure: at the first claimed example when the lexer cannot read
            the file exactly; at an example after an attribute or a modifier,
            or one that uses `decl_name%`, which a renamed theorem reads
            differently; or before an `example` keyword that is not an example
            command.
        SnapshotError: the report does not match the source.
    """
    data = text.encode()
    claimed = [entry for entry in entries if "keyword" in entry]
    spans = [_span(entry, len(data)) for entry in claimed]
    others = [_span(entry, len(data)) for entry in unclaimed]
    tokens = code_tokens(text)
    if tokens is None:
        if claimed:
            raise CommandFailure(
                "Glitnir cannot read this file exactly enough to check its "
                "examples (for example, a string containing `{`); state them as "
                "named theorems instead.",
                offset=min(entry["start"] for entry in claimed),
            )
        return [], []
    indices = [
        index
        for index, (start, _) in enumerate(tokens)
        if _token(text, tokens, index) == _KEYWORD.decode()
        and text[start - 1 : start] != "."
    ]
    offsets = _byte_offsets(text, [tokens[index][0] for index in indices])
    token_at = dict(zip(offsets, indices, strict=True))
    reported = sorted(first for first, _ in spans + others)
    if len(set(reported)) != len(reported) or not set(reported) <= set(offsets):
        raise SnapshotError(
            "The reader's examples do not match the source's `example` keywords."
        )
    # Every reason to stop is collected, and the earliest one wins: stopping
    # later would leave an earlier one in the source checked before it.
    failures: list[CommandFailure] = []
    missing = sorted(set(offsets) - set(reported))
    if missing:
        line = data.count(b"\n", 0, missing[0]) + 1
        failures.append(
            CommandFailure(
                f"Line {line} has the `example` keyword outside an example "
                "command; Glitnir checks no example from there.",
                offset=_boundary_before(data, missing[0], entries + unclaimed),
            )
        )
    edits: list[tuple[int, bytes]] = []
    previous = 0
    for entry, (first, last) in zip(claimed, spans, strict=True):
        if _attribute_or_modifier(text, tokens, token_at[first]):
            failures.append(
                CommandFailure(
                    "Examples with attributes or modifiers are not checked; "
                    "name it as a theorem.",
                    offset=entry["start"],
                )
            )
        name = f"{tag}{first}"
        try:
            lead = data[entry["start"] : first].decode()
        except UnicodeDecodeError as error:
            raise SnapshotError(
                "The source declaration report is malformed."
            ) from error
        if (
            entry["start"] < previous
            or not only_comments(lead)
            or not data[last:].startswith(_AFTER_KEYWORD)
            or entry["name"].rsplit(".", 1)[-1] != name
        ):
            raise SnapshotError("The source declaration report is malformed.")
        if b"decl_name%" in data[entry["start"] : entry["end"]]:
            failures.append(
                CommandFailure(
                    "An example that uses `decl_name%` is not checked: Glitnir "
                    "checks an example as a theorem with another name.",
                    offset=entry["start"],
                )
            )
        edits.append((first, f"private theorem {name}".encode()))
        previous = entry["end"]
    if failures:
        raise min(failures, key=lambda failure: failure.offset)
    return edits, unclaimed


def rewrite_examples(data: bytes, edits: list[tuple[int, bytes]]) -> bytes:
    """Return ``data`` with the `example` keyword at each edit replaced."""
    parts = []
    cursor = 0
    for offset, replacement in edits:
        parts += [data[cursor:offset], replacement]
        cursor = offset + len(_KEYWORD)
    parts.append(data[cursor:])
    return b"".join(parts)


def check_rewrite(
    original: bytes, compiled: bytes, edits: list[tuple[int, bytes]]
) -> None:
    """Require ``compiled`` to differ from ``original`` only at the edits.

    Every other byte must be equal and in order, each edit must replace one
    `example` keyword, and no replacement may add a line: every line keeps its
    number, and only columns after a keyword on its own line move.
    """
    left = right = 0
    for offset, replacement in edits:
        length = offset - left
        if (
            length < 0
            or b"\n" in replacement
            or original[offset : offset + len(_KEYWORD)] != _KEYWORD
            or compiled[right : right + length] != original[left:offset]
            or compiled[right + length : right + length + len(replacement)]
            != replacement
        ):
            raise SnapshotError("The compiled source differs from the saved source.")
        left = offset + len(_KEYWORD)
        right += length + len(replacement)
    if compiled[right:] != original[left:]:
        raise SnapshotError("The compiled source differs from the saved source.")


def original_offset(offset: int, edits: list[tuple[int, bytes]]) -> int:
    """Map a byte offset in the compiled source back to the saved source.

    An offset inside a replacement maps to the start of its `example` keyword.
    """
    shift = 0
    for first, replacement in edits:
        start = first + shift
        if offset < start:
            break
        if offset < start + len(replacement):
            return first
        shift += len(replacement) - len(_KEYWORD)
    return offset - shift


_POSITION = re.compile(r"^.*?:(\d+):(\d+): error", re.MULTILINE)


def first_error_offset(stderr: str, text: str) -> int | None:
    """Return the byte offset of the first positioned Lean error, if any."""
    match = _POSITION.search(stderr)
    if match is None:
        return None
    line, column = int(match[1]), int(match[2])
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        return None
    before = "\n".join(lines[: line - 1])
    prefix = before + ("\n" if line > 1 else "") + lines[line - 1][:column]
    return len(prefix.encode())


def failure_message(error: str) -> str:
    """Return the headline of a Lean failure for display, at most 500 characters."""
    lines = [
        x.strip()
        for x in error.splitlines()
        if x.strip() and not x.startswith("lean did not complete successfully")
    ]
    if not lines:
        return ""
    positioned = next((x for x in lines if ": error" in x), None)
    if positioned is not None:
        return positioned.split(": error", 1)[-1].lstrip(": ").strip()[:500]
    return lines[0][:500]


def _reader_failure(
    error: SnapshotError, report_path: Path, text: str
) -> CommandFailure | None:
    """Translate a reader failure report into the failing command's offset."""
    if not report_path.is_file():
        return None
    try:
        stopped = json.loads(read_regular(report_path, OUTPUT_BYTES))
    except (OSError, ValueError):
        return None
    offset = stopped.get("failed_at") if isinstance(stopped, dict) else None
    if type(offset) is int and 0 <= offset <= len(text.encode()):
        return CommandFailure(str(error), offset=offset)
    return None


def _candidate_failure(
    error: SnapshotError,
    text: str,
    entries: list[dict[str, Any]],
    compiled: str | None = None,
    edits: list[tuple[int, bytes]] | None = None,
) -> CommandFailure | None:
    """Map a candidate compile error to the start of the theorem containing it.

    The error's position is in ``compiled``, the saved ``text`` with its
    examples rewritten by ``edits``, and is mapped back to ``text``.
    """
    position = first_error_offset(str(error), text if compiled is None else compiled)
    if position is None:
        return None
    position = original_offset(position, edits or [])
    for entry in entries:
        if entry["start"] <= position <= entry["end"]:
            unknown = "unknown identifier" in failure_message(str(error)).lower()
            note = EXAMPLE_NOTE if "keyword" in entry and unknown else ""
            return CommandFailure(str(error), offset=entry["start"], note=note)
    return None


def _refuse_unsupported(source: Path) -> None:
    """Refuse a project whose Lean version Glitnir does not check.

    This runs before the Lean slot, a scratch directory or the toolchain is
    touched, so the refusal says `lean_support`'s words and nothing else;
    the checks after the slot still catch a declaration changed meanwhile.
    """
    message = lean_support(describe_project(source))["message"]
    if message is not None:
        raise SnapshotError(message)


def _check_text(
    source: Path,
    text: str,
    *,
    saved_sha256: str,
    cancel: threading.Event,
    progress: Callable[[str], None],
    rest: str | None = None,
    acquired: Callable[[], None] = lambda: None,
    stages: StageTimes | None = None,
) -> dict[str, Any]:
    """Check one LF-normalised source text under a private project snapshot.

    ``text`` may be a prefix of the saved file whose digest is ``saved_sha256``;
    environment resolution is bound to the saved file, the snapshot to ``text``.
    With ``rest``, the saved text after that prefix, the reader also refuses a
    prefix whose last command Lean would read further into ``rest``.
    ``acquired`` is called once the Lean slot is held, before anything is read.
    ``stages`` records how long each stage takes (`StageTimes`).
    """
    cancellation = cancel
    timed = (stages or StageTimes()).stage
    if "\r" in text:
        # Lean converts CRLF again, so a stray carriage return before a line
        # end would move every position Lean reports against these bytes.
        raise SnapshotError(CARRIAGE_RETURN)
    expected_sha256 = hashlib.sha256(text.encode()).hexdigest()
    # A check waits for another Lean run, a view say, rather than failing at
    # once, so views drawn one after another never keep it out.
    # The Mac is kept awake from the slot to the last Lean process: the stages
    # between Lean runs, the snapshot capture above all, take minutes too.
    with (
        snapshot_slot(cancel=cancellation, waiting=lambda: progress(SLOT_WAITING)),
        awake(),
        scratch_directory("glitnir-check-") as temporary,
        artifact_lease(temporary) as store,
    ):
        if cancellation.is_set():
            raise SnapshotError("Project check cancelled.")
        acquired()
        project = describe_project(source)
        toolchain = _toolchain()
        resolution = None
        if project["kind"] == "lake":
            progress("Resolving project environment")
            with timed("resolve"):
                inspected = inspect_project(
                    source, expected_sha256=saved_sha256, cancel=cancellation
                )
            if inspected["status"] != "resolved":
                raise SnapshotError(inspected["message"])
            setup = inspected["setup"]
            resolution = inspected["resolution"]
            project_root = Path(project["root"]).resolve()
        else:
            support = lean_support(project)
            if not support["supported"]:
                raise SnapshotError(support["message"])
            setup = {
                "name": "GlitnirStandalone",
                "isModule": False,
                "importArts": {},
                "plugins": [],
                "dynlibs": [],
                "options": {},
            }
            project_root = None
        module = setup["name"]
        if (
            (toolchain / "lib/lean" / module_path(module))
            .with_suffix(".olean")
            .exists()
        ):
            raise SnapshotError(
                "The checked module shadows a trusted toolchain module."
            )
        root = Path(temporary).resolve()
        progress("Preparing isolated environment")
        with timed("capture"):
            snapshot = capture(
                root,
                source,
                setup,
                toolchain,
                project_root,
                cancellation,
                configuration=(
                    {item["path"]: item["sha256"] for item in resolution["inputs"]}
                    if resolution
                    else None
                ),
                text=text,
                store=store,
            )
        digest = hashlib.sha256(snapshot.source.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise SnapshotError("Saved source changed; refresh and retry.")
        reader, reporter, replay = _programs(root / "programs")
        helper_identity = hashlib.sha256(
            json.dumps(
                {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (reader, reporter, replay)
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        identity = hashlib.sha256(
            (
                "glitnir.project-check/v1\0"
                + snapshot.identity
                + helper_identity
                + json.dumps(resolution, sort_keys=True)
            ).encode()
        ).hexdigest()
        deadline = time.monotonic() + check_seconds()
        memory = memory_limit_mb()
        setup_path = root / "programs/setup.json"
        setup_path.write_text(json.dumps(snapshot.setup))
        progress("Checking saved file")
        tag = example_tag(text)
        continuation = []
        if rest is not None:
            # Only parsed, after the prefix's last command, never compiled.
            rest_path = root / "programs" / "rest.lean"
            rest_path.write_text(rest)
            continuation = [str(rest_path)]
        run = partial(_run, snapshot, cancel=cancellation, deadline=deadline)
        builds = {key: root / key for key in ("reference", "candidate")}
        relative = module_path(module)
        for directory in builds.values():
            (directory / relative.parent).mkdir(parents=True)
        try:
            with timed("read"):
                run(
                    [
                        "lean",
                        f"-M{memory}",
                        "--run",
                        str(reader),
                        str(snapshot.source),
                        str(setup_path),
                        str(snapshot.artifacts),
                        tag,
                        str(builds["reference"] / (str(relative) + ".olean")),
                        str(builds["reference"] / "source-report.json"),
                        *continuation,
                    ],
                    cwd=builds["reference"],
                    readable=[reader.parent],
                    writable=True,
                )
        except SnapshotError as error:
            failure = _reader_failure(
                error, builds["reference"] / "source-report.json", text
            )
            if failure is not None:
                raise failure from error
            raise
        source_report = json.loads(
            read_regular(builds["reference"] / "source-report.json", OUTPUT_BYTES)
        )
        entries = (
            source_report.get("declarations")
            if isinstance(source_report, dict)
            else None
        )
        if not isinstance(entries, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and type(item.get("start")) is int
            and type(item.get("end")) is int
            and 0 <= item["start"] < item["end"] <= snapshot.source.stat().st_size
            for item in entries
        ):
            raise SnapshotError("The source declaration report is malformed.")
        unclaimed = source_report.get("examples", [])
        if not isinstance(unclaimed, list):
            raise SnapshotError("The source declaration report is malformed.")
        saved_bytes = snapshot.source.read_bytes()
        if saved_bytes != text.encode():
            raise SnapshotError("Saved source changed; refresh and retry.")
        edits, unclaimed = example_edits(text, entries, unclaimed, tag)
        compiled_source = snapshot.source
        compiled_text = None
        if edits:
            # Examples compile as the theorems the reader read them as. Only
            # their keywords change, so every line keeps its number.
            compiled_source = root / "compiled" / snapshot.source.name
            compiled_source.parent.mkdir()
            compiled_source.write_bytes(rewrite_examples(saved_bytes, edits))
            compiled_source.chmod(0o400)
            compiled_bytes = compiled_source.read_bytes()
            check_rewrite(saved_bytes, compiled_bytes, edits)
            compiled_text = compiled_bytes.decode()
        try:
            with timed("compile"):
                run(
                    [
                        "lean",
                        f"-M{memory}",
                        "-DmaxHeartbeats=1000000",
                        "--setup",
                        str(setup_path),
                        "-o",
                        str(builds["candidate"] / (str(relative) + ".olean")),
                        str(compiled_source),
                    ],
                    cwd=builds["candidate"],
                    readable=[reader.parent, compiled_source.parent],
                    writable=True,
                )
        except SnapshotError as error:
            failure = _candidate_failure(error, text, entries, compiled_text, edits)
            if failure is not None:
                raise failure from error
            raise

        def report(key: str) -> Any:
            """Report one build from its own isolated tree."""
            with timed(f"report_{key}"):
                isolated = root / (key + "-isolated")
                # Separate candidate/reference namespaces, without hard links.
                isolated.mkdir()
                # Lean resolves a namespace from its first search root. Present
                # the checked module and its frozen dependencies in one tree.
                for dependency in snapshot.artifacts.rglob("*"):
                    if dependency.is_file():
                        target = isolated / dependency.relative_to(snapshot.artifacts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.symlink_to(dependency)
                _isolate(builds[key], isolated, module)
                # Reporter installs output and dependency roots after
                # compiling trusted imports. Kernel replay loads no project code.
                return json.loads(
                    run(
                        [
                            "lean",
                            f"-M{memory}",
                            "--run",
                            str(reporter),
                            module,
                            str(isolated),
                            str(snapshot.artifacts),
                        ],
                        cwd=isolated,
                        readable=[reader.parent, isolated],
                        writable=False,
                    )
                )

        with timed("reports"):
            reports = {key: report(key) for key in ("reference", "candidate")}
        text = snapshot.source.read_bytes().decode().replace("\r\n", "\n")
        spans = {e["name"]: (e["start"], e["end"]) for e in entries}
        # The claims each claim uses: display only, like the reporter's axioms.
        notebooks = project_modules(setup, project_root)
        claim_names = set(spans)
        # How the reader's Lean printed each statement in its own scope: display
        # only, beside the reporter's kernel form (`type`).
        printed = {
            e["name"]: e["statement"]
            for e in entries
            if isinstance(e.get("statement"), str)
        }
        examples = {e["name"] for e in entries if "keyword" in e}
        theorems = compare_reports(
            reports["reference"], reports["candidate"], [e["name"] for e in entries]
        )
        for name in _reports(reports["candidate"])[1]:
            if name.rsplit(".", 1)[-1].startswith(tag) and name not in examples:
                raise SnapshotError(
                    f"The candidate declares {name} under a name reserved for "
                    "examples."
                )
        isolated = root / "candidate-isolated"
        # Python alone reads and writes the record of fully replayed imports;
        # the sandboxed replay only learns whether it may skip that step.
        imports = reports["candidate"]["imports"]
        closure = _closure(reports["candidate"], module)
        if closure is None:
            raise SnapshotError("The independent module report has no import closure.")
        _toolchain_provenance(snapshot, closure)
        replay_identity = _replay_identity(snapshot, isolated, replay, imports, closure)
        cache = replay_cache.cache_directory()
        if cache is not None and cache.is_relative_to(root):
            cache = None
        readable = [reader.parent, isolated]
        mode = "full"
        replayed_now = False
        added_modules = 0
        if replay_identity is not None:
            replay_key, context, digested = replay_identity
            mode = "imports-cached"
            if cache is None or not replay_cache.contains(
                cache, replay_key, context=context, modules=digested
            ):
                base = (
                    replay_cache.find_base(cache, context=context, modules=digested)
                    if cache is not None
                    else None
                )
                arguments = [str(isolated), "imports-only"]
                if base is not None:
                    base_file = reader.parent / "replay-base.json"
                    base_file.write_text(json.dumps(base))
                    arguments = [str(isolated), "imports-incremental", str(base_file)]
                progress(
                    "Replaying imported modules once; later checks reuse it"
                    if base is None
                    else f"Replaying {len(closure) - len(base)} imported modules "
                    "once over an earlier replay; later checks reuse it"
                )
                started = time.monotonic()
                try:
                    with timed("replay_imports"):
                        imported = _replay_report(
                            _run(
                                snapshot,
                                ["lean", f"-M{memory}", "--run", str(replay), module]
                                + arguments,
                                cwd=isolated,
                                readable=readable,
                                writable=False,
                                cancel=cancellation,
                                deadline=started + IMPORTS_REPLAY_SECONDS,
                            ),
                            closure,
                            imports,
                        )
                except SnapshotError as error:
                    raise SnapshotError(
                        "Independent kernel replay failed while replaying the "
                        f"imported modules once: {error}"
                    ) from error
                if imported["replay"] == "imports incremental" and (
                    base is None or imported["base_modules"] != len(base)
                ):
                    raise SnapshotError(
                        "Independent kernel replay extended a base it was not given."
                    )
                if imported["replay"] not in ("imports only", "imports incremental"):
                    raise SnapshotError(
                        "Independent kernel replay did not replay the imported modules."
                    )
                if imported["imports_replayed"]:
                    # Replayed, and rebuilt unchecked into the same environment.
                    if cache is not None:
                        replay_cache.record(
                            cache,
                            replay_key,
                            context=context,
                            modules=digested,
                            extendable=imported["extendable"],
                        )
                    replayed_now = True
                    if imported["replay"] == "imports incremental":
                        added_modules = len(closure) - imported["base_modules"]
                else:
                    mode = "full"
                # The check's own budget excludes the one-time import replay.
                deadline += time.monotonic() - started
        progress(
            "Independently replaying the module over its replayed imports"
            if mode == "imports-cached"
            else "Independently replaying the imported proof closure"
        )
        try:
            with timed("replay"):
                replayed = _replay_report(
                    _run(
                        snapshot,
                        ["lean", f"-M{memory}", "--run", str(replay), module]
                        + [str(isolated), mode],
                        cwd=isolated,
                        readable=readable,
                        writable=False,
                        cancel=cancellation,
                        deadline=deadline,
                    ),
                    closure,
                    imports,
                )
        except SnapshotError as error:
            raise SnapshotError(f"Independent kernel replay failed: {error}") from error
        if replayed["replay"] in ("imports only", "imports incremental") or (
            replayed["replay"] == "imports cached" and mode != "imports-cached"
        ):
            raise SnapshotError("Independent kernel replay skipped its imports.")
        # Every constant was replayed during this check unless the imports'
        # replay came, entirely or but for some modules, from an earlier one.
        replay_kind = "full"
        if replayed["replay"] == "imports cached":
            if not replayed_now:
                replay_kind = "imports cached"
            elif added_modules:
                plural = "" if added_modules == 1 else "s"
                replay_kind = f"imports cached (+{added_modules} module{plural})"
            elif imported["replay"] == "imports incremental":
                # The base held every module: all imports come from earlier.
                replay_kind = "imports cached"
        # Statuses come from the replay's axioms alone; the module report's
        # must equal them.
        authority = _replayed_axioms(replayed, _reports(reports["candidate"])[1])
        observations = []
        for theorem in theorems:
            axioms = authority[theorem["name"]]
            standard = all(
                a["name"] in LEAN_AXIOM_ALLOWLIST
                and a["module"] is not None
                and (snapshot.toolchain / "lib/lean" / module_path(a["module"]))
                .with_suffix(".olean")
                .is_file()
                for a in axioms
            )
            status = (
                "admitted"
                if any(a["name"] == "sorryAx" for a in axioms)
                else "checked" if standard else "assumptions"
            )
            first, last = line_range(text, *spans[theorem["name"]])
            uses = claim_uses(theorem.get("uses", []), module, claim_names, notebooks)
            observations.append(
                {
                    "name": theorem["name"],
                    "type": theorem["type"],
                    "statement": printed.get(theorem["name"]),
                    "uses": uses[:MAX_USES],
                    **({"uses_truncated": True} if len(uses) > MAX_USES else {}),
                    "axioms": axioms,
                    "status": status,
                    "start_line": first,
                    "end_line": last,
                    "example": theorem["name"] in examples,
                    "proposition_sha256": hashlib.sha256(
                        (identity + "\0" + theorem["canonical"]).encode()
                    ).hexdigest(),
                }
            )
        return {
            "status": "checked_snapshot",
            "source_sha256": digest,
            "environment_sha256": identity,
            "toolchain": TOOLCHAIN,
            "module": module,
            "theorems": observations,
            # Examples that claim nothing: no type, or one that is not a
            # proposition. They are read as Lean reads them.
            "non_claims": [
                dict(
                    zip(
                        ("start_line", "end_line"),
                        line_range(text, item["start"], item["end"]),
                        strict=True,
                    )
                )
                for item in unclaimed
            ],
            "snapshot_bytes": snapshot.bytes,
            "resolution": resolution,
            "replay": replay_kind,
            "message": (
                "Last checked snapshot: independently replayed with its "
                "compiled imports"
                + (
                    " (their replay is reused from an earlier check of "
                    "identical files). "
                    if replay_kind == "imports cached"
                    else (
                        (
                            " (their replay is reused from an earlier check of "
                            f"identical files, except {added_modules} "
                            f"module{'' if added_modules == 1 else 's'} replayed "
                            "now). "
                        )
                        if replay_kind.startswith("imports cached")
                        else ". "
                    )
                )
                + "Statements are interpreted by the selected Lean frontend."
            ),
        }


def through_line_end(data: bytes, line: int) -> int:
    """Return the offset just after one-based ``line`` and its line feed.

    That is the start of the next line, or the end of ``data`` when ``data``
    has no line after ``line``.
    """
    offset = 0
    for _ in range(line):
        found = data.find(b"\n", offset)
        if found < 0:
            return len(data)
        offset = found + 1
    return offset


def requested_through(line: int) -> str:
    """Say why a check through ``line`` checked nothing after it."""
    return f"Checked through line {line} on request"


def _command_end(boundaries: CommandBoundaries, line: int) -> int:
    """Return the last line through ``line`` that ends a command, or 0.

    That is where a prefix of the file may be cut.
    """
    return line if boundaries.ends_at(line) else boundaries.last_end_before(line)


def _within(result: dict[str, Any], line: int) -> dict[str, Any]:
    """Return ``result`` when every claim it checked ends by ``line``.

    A prefix check through ``line`` cannot have checked a claim below it; the
    reader that reported the spans is untrusted, so this fails closed.
    """
    if any(theorem.get("end_line", 0) > line for theorem in result["theorems"]):
        raise SnapshotError(
            f"The checker reported a claim ending after line {line}, the last "
            "line it checked."
        )
    return result


def check_project(
    source: Path,
    *,
    expected_sha256: str,
    cancel: threading.Event | None = None,
    progress: Callable[[str], None] = lambda _: None,
    through_line: int | None = None,
    acquired: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Check a saved file; on a later failure, check only the commands before it.

    ``acquired`` is called each time the check holds the Lean slot, before it
    reads anything: a caller takes the environment's fingerprint there, so
    the fingerprint describes the environment the check compiled in.

    With ``through_line``, check only the saved file's lines 1 through
    ``through_line``: the same sandboxed check of that prefix, never a weaker
    one. Nothing after it is checked; the result's ``withheld`` says so
    (``from_line`` the next line, the reason ``requested_through``) and
    carries the ``through_line`` checked. A ``through_line`` at or past the
    last line is a check of the whole file.

    Every prefix checked, requested or before a failing command, ends where
    the trusted lexer finds a command end (`CommandBoundaries`); a cut inside
    a command moves back to the end of the command before it, so no command
    is ever compiled shortened. Where there is none, the check is refused.

    A finished check's result, and an unavailable one once a stage ran,
    carries ``stage_ms``, how long each stage took (`StageTimes`), for
    display and logs only.
    """
    cancellation = cancel if cancel is not None else threading.Event()
    progress = noting_sleep(progress)
    stages = StageTimes()
    try:
        if cancellation.is_set():
            raise SnapshotError("Project check cancelled.")
        if through_line is not None and (
            type(through_line) is not int or through_line < 1
        ):
            raise ValueError("Check through a positive line number.")
        source = source.absolute()
        _refuse_unsupported(source)
        text = read_regular(source, 16 * 1024 * 1024).decode().replace("\r\n", "\n")
        if hashlib.sha256(text.encode()).hexdigest() != expected_sha256:
            raise SnapshotError("Saved source changed; refresh and retry.")
        data = text.encode()
        boundaries = command_boundaries(text)
        requested: dict[str, int] = {}
        moved = ""
        checked = text
        if through_line is not None and through_line_end(data, through_line) < len(
            data
        ):
            line = _command_end(boundaries, through_line)
            if line < 1:
                raise SnapshotError(
                    f"Line {through_line} is inside the file's first command; "
                    "check the whole saved file instead."
                )
            if line != through_line:
                moved = f" (line {through_line} is inside a command)"
            requested = {"through_line": line}
            checked = data[: through_line_end(data, line)].decode()
        rest = text[len(checked) :] if requested else None
        checked_lines = checked.count("\n") + (0 if checked.endswith("\n") else 1)
        try:
            if requested:
                progress(
                    f"Checking lines 1–{requested['through_line']} of the saved file"
                )
            result = _within(
                _check_text(
                    source,
                    checked,
                    saved_sha256=expected_sha256,
                    cancel=cancellation,
                    progress=progress,
                    rest=rest,
                    acquired=acquired,
                    stages=stages,
                ),
                checked_lines,
            )
            if not requested:
                return {**result, "withheld": None, "stage_ms": stages.summary()}
            last = requested["through_line"]
            return {
                **result,
                "stage_ms": stages.summary(),
                "source_sha256": expected_sha256,
                "withheld": {
                    "from_line": last + 1,
                    "message": requested_through(last) + moved,
                    "through_line": last,
                },
            }
        except CommandFailure as failure:
            prefix_data = checked.encode()
            if failure.offset <= 0 or failure.offset >= len(prefix_data):
                raise
            failed, _ = line_range(text, failure.offset, failure.offset + 1)
            # The failing command's offset comes from the untrusted reader or
            # compiler: check only up to a command end before it.
            line = _command_end(boundaries, failed - 1)
            if line < 1:
                raise
            prefix = prefix_data[: through_line_end(prefix_data, line)].decode()
            progress("Checking the source before the first failure")
            result = _within(
                _check_text(
                    source,
                    prefix,
                    saved_sha256=expected_sha256,
                    cancel=cancellation,
                    progress=progress,
                    rest=text[len(prefix) :] if requested else None,
                    acquired=acquired,
                    stages=stages,
                ),
                line,
            )
            message = failure_message(str(failure))
            if failure.note:
                message = f"{message} ({failure.note})" if message else failure.note
            if line + 1 != failed:
                message = f"line {failed}: {message}" if message else f"line {failed}"
            return {
                **result,
                "stage_ms": stages.summary(),
                "source_sha256": expected_sha256,
                "withheld": {"from_line": line + 1, "message": message, **requested},
            }
    except (OSError, ValueError, RecursionError, SandboxUnavailable) as error:
        unavailable: dict[str, Any] = {
            "status": "unavailable",
            "message": str(error),
            "theorems": [],
        }
        stage_ms = stages.summary()
        if len(stage_ms) > 1:
            # How far it got, as for a finished check: a timeout's stages say
            # where its time went. A refusal before any stage has none.
            unavailable["stage_ms"] = stage_ms
        return unavailable


def candidate_text(saved: str, insert_after_line: int, block: str) -> str:
    """Return the saved lines through `insert_after_line`, then `block` and LF."""
    lines = saved.split("\n")
    if not 1 <= insert_after_line <= len(lines):
        raise SnapshotError(
            f"Decision directive line {insert_after_line} is outside the notebook."
        )
    return "".join(line + "\n" for line in lines[:insert_after_line]) + block + "\n"


def _failure_location(
    failure: CommandFailure, text: str, insert_after_line: int
) -> str:
    """Word a failure's line as a block line, or as a line of the saved notebook."""
    # A compile error carries its own position; a reader failure only the command's.
    position = first_error_offset(str(failure), text)
    if position is None:
        position = failure.offset
    line, _ = line_range(text, position, position + 1)
    message = failure_message(str(failure)) or "The checker stopped at this command."
    if line > insert_after_line:
        return f"Block line {line - insert_after_line}: {message}"
    return f"Line {line}: {message}"


def _candidate_verdict(
    theorems: list[dict[str, Any]], theorem: str, first: int, last: int
) -> dict[str, Any]:
    """Accept only one `checked` declaration of `theorem` within lines first..last."""
    found = [
        item
        for item in theorems
        if (item["name"] == theorem or item["name"].endswith("." + theorem))
        and first <= item["start_line"]
        and item["end_line"] <= last
    ]
    if len(found) != 1:
        message = (
            f"The block does not declare `{theorem}`."
            if not found
            else f"The block declares `{theorem}` more than once."
        )
        return {"status": "rejected", "message": message, "axioms": [], "type": None}
    name, status, axioms = found[0]["name"], found[0]["status"], found[0]["axioms"]
    verdict: dict[str, Any] = {
        "status": "rejected",
        # Names only (the saved decision and the card hold strings); the
        # modules stay in the rejection message below.
        "axioms": list(dict.fromkeys(a["name"] for a in axioms)),
        "type": found[0]["type"],
    }
    if status == "checked":
        message = f"The checker reports `{name}` as checked."
        return {**verdict, "status": "checked", "message": message}
    if status == "admitted":
        message = f"`{name}` is admitted: its proof depends on `sorry`."
    else:
        extra = [a["name"] for a in axioms if a["name"] not in LEAN_AXIOM_ALLOWLIST]
        if extra:
            listed = ", ".join(f"`{a}`" for a in extra)
            message = (
                f"`{name}` depends on axioms outside Lean's standard set: {listed}."
            )
        else:
            # Standard names, but not all provided by the toolchain's own modules.
            listed = ", ".join(
                f"`{a['name']}` (from `{a['module'] or 'an unknown module'}`)"
                for a in axioms
            )
            message = (
                f"`{name}` depends on axioms the checker could not trace to "
                f"Lean's own modules: {listed}."
            )
    return {**verdict, "message": message}


def check_candidate(
    source: Path,
    *,
    expected_sha256: str,
    insert_after_line: int,
    block: str,
    theorem: str,
    cancel: threading.Event,
    stage_ms: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Check the saved file through `insert_after_line` followed by `block`.

    This is the only proof authority for a decision. The candidate text is
    checked exactly like a saved prefix: the environment stays bound to the
    saved file, whose digest must be `expected_sha256` before and after. It is
    `checked` only when the block declares `theorem` (that full name, or one
    ending in `.<theorem>`) exactly once and the checker reports it `checked`.
    Declarations before the block do not affect the verdict, but a failing
    command anywhere rejects the candidate with its message. Returns
    `{"status": "checked" | "rejected", "message", "axioms", "type"}`; `axioms`
    and `type` are the matched theorem's, or `[]` and None without one;
    `axioms` is a list of axiom names (strings, in the checker's order,
    without duplicates), not the checker's records with their modules. `type`
    is the kernel's form of the statement, without notation
    (`Eq (instHAdd.hAdd 2 2) 4`).
    Anything that says nothing about the block (cancellation, a busy slot, a
    timeout, an unavailable environment or a changed file) raises
    `SnapshotError` with the checker's message.
    A ``stage_ms`` dictionary is filled with how long each stage took
    (`StageTimes`), whatever the outcome: for logs only, never in the verdict,
    which a decision saves.
    """
    stages = StageTimes()
    try:
        if cancel.is_set():
            raise SnapshotError("Project check cancelled.")
        source = source.absolute()
        _refuse_unsupported(source)

        def saved() -> str:
            text = read_regular(source, 16 * 1024 * 1024).decode()
            text = text.replace("\r\n", "\n")
            if hashlib.sha256(text.encode()).hexdigest() != expected_sha256:
                raise SnapshotError("Saved source changed; refresh and retry.")
            return text

        text = candidate_text(saved(), insert_after_line, block)
        try:
            result = _check_text(
                source,
                text,
                saved_sha256=expected_sha256,
                cancel=cancel,
                progress=lambda _: None,
                stages=stages,
            )
        except CommandFailure as failure:
            verdict: dict[str, Any] = {
                "status": "rejected",
                "message": _failure_location(failure, text, insert_after_line),
                "axioms": [],
                "type": None,
            }
        else:
            verdict = _candidate_verdict(
                result["theorems"],
                theorem,
                insert_after_line + 1,
                insert_after_line + block.count("\n") + 1,
            )
        if cancel.is_set():
            raise SnapshotError("Project check cancelled.")
        saved()
        return verdict
    except SnapshotError:
        raise
    except (OSError, ValueError, RecursionError, SandboxUnavailable) as error:
        raise SnapshotError(str(error)) from error
    finally:
        if stage_ms is not None:
            stage_ms.update(stages.summary())
