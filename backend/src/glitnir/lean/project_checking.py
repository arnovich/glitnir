"""Independent observations of a saved Lean file under a private project snapshot."""

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from glitnir.lean import replay_cache
from glitnir.lean.artifact_store import ArtifactStore
from glitnir.lean.awake import awake, noting_sleep
from glitnir.lean.axiom_report import REPORT_PROGRAM, REPORT_SHA256
from glitnir.lean.candidate import candidate_text, candidate_verdict, failure_location
from glitnir.lean.environment import LEAN_AXIOM_ALLOWLIST, MAX_SOURCE_BYTES
from glitnir.lean.examples import (
    EXAMPLE_NOTE,
    check_rewrite,
    example_edits,
    example_tag,
    original_offset,
    rewrite_examples,
)
from glitnir.lean.failures import (
    CommandFailure,
    failure_message,
    first_error_offset,
    line_range,
)
from glitnir.lean.lexing import CommandBoundaries, command_boundaries
from glitnir.lean.limits import check_seconds, memory_limit_mb
from glitnir.lean.observer import MODULE_BYTES, OUTPUT_BYTES, run_observer
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
from glitnir.lean.replay import (
    replay_identity,
    replay_report,
    replayed_axioms,
    toolchain_provenance,
)
from glitnir.lean.reports import (
    claim_uses,
    compare_reports,
    declarations,
    import_closure,
    project_modules,
)
from glitnir.lean.sandbox import SandboxUnavailable
from glitnir.lean.scratch import scratch_directory

PROGRAMS = Path(__file__).parent
#: The one-time replay of an import closure has its own, longer deadline; the
#: check's own budget starts again after it.
IMPORTS_REPLAY_SECONDS = 3600
#: At most this many uses are recorded per claim; more are marked truncated.
MAX_USES = 200
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


class _Runner(Protocol):
    """Run one Lean stage under the check's snapshot, cancel event and deadline."""

    def __call__(
        self, arguments: list[str], *, cwd: Path, readable: list[Path], writable: bool
    ) -> str: ...


@dataclass(frozen=True)
class _Check:
    """What one check was asked: fixed for the whole call."""

    source: Path
    text: str
    text_sha256: str
    saved_sha256: str
    cancel: threading.Event
    progress: Callable[[str], None]
    rest: str | None
    timed: Callable[[str], AbstractContextManager[None]]


@dataclass(frozen=True)
class _Environment:
    """Phase 1, `_resolve`: the project and the toolchain the check compiles in."""

    toolchain: Path
    setup: dict[str, Any]
    resolution: dict[str, Any] | None
    project_root: Path | None
    module: str


@dataclass(frozen=True)
class _Frozen:
    """Phase 2, `_capture`: the snapshot, the helper programs and the identity."""

    root: Path
    snapshot: ProjectSnapshot
    digest: str
    reader: Path
    reporter: Path
    replay: Path
    identity: str
    deadline: float
    memory: int
    setup_path: Path
    tag: str
    continuation: list[str]
    builds: dict[str, Path]
    relative: Path
    run: _Runner


@dataclass(frozen=True)
class _Read:
    """Phase 3, `_read`: what the reader reported about the saved text."""

    entries: list[dict[str, Any]]
    unclaimed: list[dict[str, Any]]
    saved_bytes: bytes


@dataclass(frozen=True)
class _Compiled:
    """Phase 4, `_compile`: the examples claiming nothing, as the rewrite kept them."""

    unclaimed: list[dict[str, Any]]


def _resolve(check: _Check) -> _Environment:
    """Resolve the project's environment, or refuse one Glitnir cannot check."""
    project = describe_project(check.source)
    toolchain = _toolchain()
    resolution = None
    if project["kind"] == "lake":
        check.progress("Resolving project environment")
        with check.timed("resolve"):
            inspected = inspect_project(
                check.source, expected_sha256=check.saved_sha256, cancel=check.cancel
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
    if (toolchain / "lib/lean" / module_path(module)).with_suffix(".olean").exists():
        raise SnapshotError("The checked module shadows a trusted toolchain module.")
    return _Environment(
        toolchain=toolchain,
        setup=setup,
        resolution=resolution,
        project_root=project_root,
        module=module,
    )


def _capture(
    check: _Check, environment: _Environment, root: Path, store: ArtifactStore
) -> _Frozen:
    """Freeze the project in a private snapshot and compose the helper programs."""
    check.progress("Preparing isolated environment")
    with check.timed("capture"):
        snapshot = capture(
            root,
            check.source,
            environment.setup,
            environment.toolchain,
            environment.project_root,
            check.cancel,
            configuration=(
                {
                    item["path"]: item["sha256"]
                    for item in environment.resolution["inputs"]
                }
                if environment.resolution
                else None
            ),
            text=check.text,
            store=store,
        )
    digest = hashlib.sha256(snapshot.source.read_bytes()).hexdigest()
    if digest != check.text_sha256:
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
            + json.dumps(environment.resolution, sort_keys=True)
        ).encode()
    ).hexdigest()
    deadline = time.monotonic() + check_seconds()
    memory = memory_limit_mb()
    setup_path = root / "programs/setup.json"
    setup_path.write_text(json.dumps(snapshot.setup))
    check.progress("Checking saved file")
    tag = example_tag(check.text)
    continuation = []
    if check.rest is not None:
        # Only parsed, after the prefix's last command, never compiled.
        rest_path = root / "programs" / "rest.lean"
        rest_path.write_text(check.rest)
        continuation = [str(rest_path)]

    def run(
        arguments: list[str], *, cwd: Path, readable: list[Path], writable: bool
    ) -> str:
        """Run one stage over the snapshot, within the check's deadline."""
        return _run(
            snapshot,
            arguments,
            cwd=cwd,
            readable=readable,
            writable=writable,
            cancel=check.cancel,
            deadline=deadline,
        )

    builds = {key: root / key for key in ("reference", "candidate")}
    relative = module_path(environment.module)
    for directory in builds.values():
        (directory / relative.parent).mkdir(parents=True)
    return _Frozen(
        root=root,
        snapshot=snapshot,
        digest=digest,
        reader=reader,
        reporter=reporter,
        replay=replay,
        identity=identity,
        deadline=deadline,
        memory=memory,
        setup_path=setup_path,
        tag=tag,
        continuation=continuation,
        builds=builds,
        relative=relative,
        run=run,
    )


def _read(check: _Check, frozen: _Frozen) -> _Read:
    """Run the reader over the saved text and validate what it reported."""
    report_path = frozen.builds["reference"] / "source-report.json"
    try:
        with check.timed("read"):
            frozen.run(
                [
                    "lean",
                    f"-M{frozen.memory}",
                    "--run",
                    str(frozen.reader),
                    str(frozen.snapshot.source),
                    str(frozen.setup_path),
                    str(frozen.snapshot.artifacts),
                    frozen.tag,
                    str(frozen.builds["reference"] / (str(frozen.relative) + ".olean")),
                    str(report_path),
                    *frozen.continuation,
                ],
                cwd=frozen.builds["reference"],
                readable=[frozen.reader.parent],
                writable=True,
            )
    except SnapshotError as error:
        failure = _reader_failure(error, report_path, check.text)
        if failure is not None:
            raise failure from error
        raise
    source_report = json.loads(read_regular(report_path, OUTPUT_BYTES))
    entries = (
        source_report.get("declarations") if isinstance(source_report, dict) else None
    )
    if not isinstance(entries, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and type(item.get("start")) is int
        and type(item.get("end")) is int
        and 0 <= item["start"] < item["end"] <= frozen.snapshot.source.stat().st_size
        for item in entries
    ):
        raise SnapshotError("The source declaration report is malformed.")
    unclaimed = source_report.get("examples", [])
    if not isinstance(unclaimed, list):
        raise SnapshotError("The source declaration report is malformed.")
    saved_bytes = frozen.snapshot.source.read_bytes()
    if saved_bytes != check.text.encode():
        raise SnapshotError("Saved source changed; refresh and retry.")
    return _Read(entries=entries, unclaimed=unclaimed, saved_bytes=saved_bytes)


def _compile(check: _Check, frozen: _Frozen, read: _Read) -> _Compiled:
    """Compile the text, its examples as the theorems the reader read them as."""
    edits, unclaimed = example_edits(
        check.text, read.entries, read.unclaimed, frozen.tag
    )
    compiled_source = frozen.snapshot.source
    compiled_text = None
    if edits:
        # Examples compile as the theorems the reader read them as. Only
        # their keywords change, so every line keeps its number.
        compiled_source = frozen.root / "compiled" / frozen.snapshot.source.name
        compiled_source.parent.mkdir()
        compiled_source.write_bytes(rewrite_examples(read.saved_bytes, edits))
        compiled_source.chmod(0o400)
        compiled_bytes = compiled_source.read_bytes()
        check_rewrite(read.saved_bytes, compiled_bytes, edits)
        compiled_text = compiled_bytes.decode()
    try:
        with check.timed("compile"):
            frozen.run(
                [
                    "lean",
                    f"-M{frozen.memory}",
                    "-DmaxHeartbeats=1000000",
                    "--setup",
                    str(frozen.setup_path),
                    "-o",
                    str(frozen.builds["candidate"] / (str(frozen.relative) + ".olean")),
                    str(compiled_source),
                ],
                cwd=frozen.builds["candidate"],
                readable=[frozen.reader.parent, compiled_source.parent],
                writable=True,
            )
    except SnapshotError as error:
        failure = _candidate_failure(
            error, check.text, read.entries, compiled_text, edits
        )
        if failure is not None:
            raise failure from error
        raise
    return _Compiled(unclaimed=unclaimed)


@dataclass(frozen=True)
class _Reported:
    """Phase 5, `_report`: both reports compared, and the candidate's import closure."""

    candidate: dict[str, Any]
    theorems: list[dict[str, Any]]
    examples: set[str]
    spans: dict[str, tuple[int, int]]
    printed: dict[str, str]
    notebooks: set[str]
    isolated: Path
    imports: Any
    closure: list[list[str]]
    text: str


@dataclass(frozen=True)
class _ImportsReplay:
    """How the imports were replayed before the module: the cached path's outcome.

    ``deadline`` is the check's budget, moved by the time a one-time replay
    took, since the check's own budget excludes it.
    """

    mode: str
    replayed_now: bool
    added_modules: int
    incremental: bool
    deadline: float


@dataclass(frozen=True)
class _Replayed:
    """Phase 6, `_replay`: the kernel replay's report and how its imports came."""

    replayed: dict[str, Any]
    replay_kind: str
    added_modules: int


def _report_build(
    check: _Check, frozen: _Frozen, environment: _Environment, key: str
) -> Any:
    """Report one build from its own isolated tree."""
    with check.timed(f"report_{key}"):
        isolated = frozen.root / (key + "-isolated")
        # Separate candidate/reference namespaces, without hard links.
        isolated.mkdir()
        # Lean resolves a namespace from its first search root. Present
        # the checked module and its frozen dependencies in one tree.
        for dependency in frozen.snapshot.artifacts.rglob("*"):
            if dependency.is_file():
                target = isolated / dependency.relative_to(frozen.snapshot.artifacts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(dependency)
        _isolate(frozen.builds[key], isolated, environment.module)
        # Reporter installs output and dependency roots after
        # compiling trusted imports. Kernel replay loads no project code.
        return json.loads(
            frozen.run(
                [
                    "lean",
                    f"-M{frozen.memory}",
                    "--run",
                    str(frozen.reporter),
                    environment.module,
                    str(isolated),
                    str(frozen.snapshot.artifacts),
                ],
                cwd=isolated,
                readable=[frozen.reader.parent, isolated],
                writable=False,
            )
        )


def _report(
    check: _Check, frozen: _Frozen, environment: _Environment, read: _Read
) -> _Reported:
    """Report both builds, compare them and find the candidate's import closure."""
    with check.timed("reports"):
        reports = {
            key: _report_build(check, frozen, environment, key)
            for key in ("reference", "candidate")
        }
    text = frozen.snapshot.source.read_bytes().decode().replace("\r\n", "\n")
    spans = {e["name"]: (e["start"], e["end"]) for e in read.entries}
    # The claims each claim uses: display only, like the reporter's axioms.
    notebooks = project_modules(environment.setup, environment.project_root)
    # How the reader's Lean printed each statement in its own scope: display
    # only, beside the reporter's kernel form (`type`).
    printed = {
        e["name"]: e["statement"]
        for e in read.entries
        if isinstance(e.get("statement"), str)
    }
    examples = {e["name"] for e in read.entries if "keyword" in e}
    theorems = compare_reports(
        reports["reference"], reports["candidate"], [e["name"] for e in read.entries]
    )
    for name in declarations(reports["candidate"])[1]:
        if name.rsplit(".", 1)[-1].startswith(frozen.tag) and name not in examples:
            raise SnapshotError(
                f"The candidate declares {name} under a name reserved for examples."
            )
    isolated = frozen.root / "candidate-isolated"
    # Python alone reads and writes the record of fully replayed imports;
    # the sandboxed replay only learns whether it may skip that step.
    imports = reports["candidate"]["imports"]
    closure = import_closure(reports["candidate"], environment.module)
    if closure is None:
        raise SnapshotError("The independent module report has no import closure.")
    toolchain_provenance(frozen.snapshot, closure)
    return _Reported(
        candidate=reports["candidate"],
        theorems=theorems,
        examples=examples,
        spans=spans,
        printed=printed,
        notebooks=notebooks,
        isolated=isolated,
        imports=imports,
        closure=closure,
        text=text,
    )


def _replay_imports(
    check: _Check,
    frozen: _Frozen,
    environment: _Environment,
    reported: _Reported,
    identity: tuple[str, str, list[list[str]]] | None,
    cache: Path | None,
    readable: list[Path],
) -> _ImportsReplay:
    """Replay the imports once, unless an earlier check's replay of them is kept.

    Without an identity for them (some file is unknown), or when the one-time
    replay could not replay them, the module replay replays everything.
    """
    if identity is None:
        return _ImportsReplay(
            mode="full",
            replayed_now=False,
            added_modules=0,
            incremental=False,
            deadline=frozen.deadline,
        )
    replay_key, context, digested = identity
    if cache is not None and replay_cache.contains(
        cache, replay_key, context=context, modules=digested
    ):
        return _ImportsReplay(
            mode="imports-cached",
            replayed_now=False,
            added_modules=0,
            incremental=False,
            deadline=frozen.deadline,
        )
    base = (
        replay_cache.find_base(cache, context=context, modules=digested)
        if cache is not None
        else None
    )
    arguments = [str(reported.isolated), "imports-only"]
    if base is not None:
        base_file = frozen.reader.parent / "replay-base.json"
        base_file.write_text(json.dumps(base))
        arguments = [str(reported.isolated), "imports-incremental", str(base_file)]
    check.progress(
        "Replaying imported modules once; later checks reuse it"
        if base is None
        else f"Replaying {len(reported.closure) - len(base)} imported modules "
        "once over an earlier replay; later checks reuse it"
    )
    started = time.monotonic()
    try:
        with check.timed("replay_imports"):
            imported = replay_report(
                _run(
                    frozen.snapshot,
                    [
                        "lean",
                        f"-M{frozen.memory}",
                        "--run",
                        str(frozen.replay),
                        environment.module,
                    ]
                    + arguments,
                    cwd=reported.isolated,
                    readable=readable,
                    writable=False,
                    cancel=check.cancel,
                    deadline=started + IMPORTS_REPLAY_SECONDS,
                ),
                reported.closure,
                reported.imports,
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
    mode = "imports-cached"
    replayed_now = False
    added_modules = 0
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
            added_modules = len(reported.closure) - imported["base_modules"]
    else:
        mode = "full"
    # The check's own budget excludes the one-time import replay.
    return _ImportsReplay(
        mode=mode,
        replayed_now=replayed_now,
        added_modules=added_modules,
        incremental=imported["replay"] == "imports incremental",
        deadline=frozen.deadline + (time.monotonic() - started),
    )


def _replay(
    check: _Check, frozen: _Frozen, environment: _Environment, reported: _Reported
) -> _Replayed:
    """Replay the module in the kernel over its imports, replayed once or now."""
    identity = replay_identity(
        frozen.snapshot,
        reported.isolated,
        frozen.replay,
        reported.imports,
        reported.closure,
    )
    cache = replay_cache.cache_directory()
    if cache is not None and cache.is_relative_to(frozen.root):
        cache = None
    readable = [frozen.reader.parent, reported.isolated]
    imports_replay = _replay_imports(
        check, frozen, environment, reported, identity, cache, readable
    )
    check.progress(
        "Independently replaying the module over its replayed imports"
        if imports_replay.mode == "imports-cached"
        else "Independently replaying the imported proof closure"
    )
    try:
        with check.timed("replay"):
            replayed = replay_report(
                _run(
                    frozen.snapshot,
                    [
                        "lean",
                        f"-M{frozen.memory}",
                        "--run",
                        str(frozen.replay),
                        environment.module,
                    ]
                    + [str(reported.isolated), imports_replay.mode],
                    cwd=reported.isolated,
                    readable=readable,
                    writable=False,
                    cancel=check.cancel,
                    deadline=imports_replay.deadline,
                ),
                reported.closure,
                reported.imports,
            )
    except SnapshotError as error:
        raise SnapshotError(f"Independent kernel replay failed: {error}") from error
    if replayed["replay"] in ("imports only", "imports incremental") or (
        replayed["replay"] == "imports cached"
        and imports_replay.mode != "imports-cached"
    ):
        raise SnapshotError("Independent kernel replay skipped its imports.")
    # Every constant was replayed during this check unless the imports'
    # replay came, entirely or but for some modules, from an earlier one.
    replay_kind = "full"
    if replayed["replay"] == "imports cached":
        if not imports_replay.replayed_now:
            replay_kind = "imports cached"
        elif imports_replay.added_modules:
            plural = "" if imports_replay.added_modules == 1 else "s"
            replay_kind = (
                f"imports cached (+{imports_replay.added_modules} module{plural})"
            )
        elif imports_replay.incremental:
            # The base held every module: all imports come from earlier.
            replay_kind = "imports cached"
    return _Replayed(
        replayed=replayed,
        replay_kind=replay_kind,
        added_modules=imports_replay.added_modules,
    )


def _observe(
    frozen: _Frozen,
    environment: _Environment,
    compiled: _Compiled,
    reported: _Reported,
    replayed: _Replayed,
) -> dict[str, Any]:
    """Decide each claim's status from the replay's axioms and assemble the result."""
    # Statuses come from the replay's axioms alone; the module report's
    # must equal them.
    authority = replayed_axioms(replayed.replayed, declarations(reported.candidate)[1])
    claim_names = set(reported.spans)
    observations = []
    for theorem in reported.theorems:
        axioms = authority[theorem["name"]]
        standard = all(
            a["name"] in LEAN_AXIOM_ALLOWLIST
            and a["module"] is not None
            and (frozen.snapshot.toolchain / "lib/lean" / module_path(a["module"]))
            .with_suffix(".olean")
            .is_file()
            for a in axioms
        )
        status = (
            "admitted"
            if any(a["name"] == "sorryAx" for a in axioms)
            else "checked" if standard else "assumptions"
        )
        first, last = line_range(reported.text, *reported.spans[theorem["name"]])
        uses = claim_uses(
            theorem.get("uses", []), environment.module, claim_names, reported.notebooks
        )
        observations.append(
            {
                "name": theorem["name"],
                "type": theorem["type"],
                "statement": reported.printed.get(theorem["name"]),
                "uses": uses[:MAX_USES],
                **({"uses_truncated": True} if len(uses) > MAX_USES else {}),
                "axioms": axioms,
                "status": status,
                "start_line": first,
                "end_line": last,
                "example": theorem["name"] in reported.examples,
                "proposition_sha256": hashlib.sha256(
                    (frozen.identity + "\0" + theorem["canonical"]).encode()
                ).hexdigest(),
            }
        )
    added_modules = replayed.added_modules
    return {
        "status": "checked_snapshot",
        "source_sha256": frozen.digest,
        "environment_sha256": frozen.identity,
        "toolchain": TOOLCHAIN,
        "module": environment.module,
        "theorems": observations,
        # Examples that claim nothing: no type, or one that is not a
        # proposition. They are read as Lean reads them.
        "non_claims": [
            dict(
                zip(
                    ("start_line", "end_line"),
                    line_range(reported.text, item["start"], item["end"]),
                    strict=True,
                )
            )
            for item in compiled.unclaimed
        ],
        "snapshot_bytes": frozen.snapshot.bytes,
        "resolution": environment.resolution,
        "replay": replayed.replay_kind,
        "message": (
            "Last checked snapshot: independently replayed with its "
            "compiled imports"
            + (
                " (their replay is reused from an earlier check of "
                "identical files). "
                if replayed.replay_kind == "imports cached"
                else (
                    (
                        " (their replay is reused from an earlier check of "
                        f"identical files, except {added_modules} "
                        f"module{'' if added_modules == 1 else 's'} replayed "
                        "now). "
                    )
                    if replayed.replay_kind.startswith("imports cached")
                    else ". "
                )
            )
            + "Statements are interpreted by the selected Lean frontend."
        ),
    }


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
    if "\r" in text:
        # Lean converts CRLF again, so a stray carriage return before a line
        # end would move every position Lean reports against these bytes.
        raise SnapshotError(CARRIAGE_RETURN)
    check = _Check(
        source=source,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        saved_sha256=saved_sha256,
        cancel=cancel,
        progress=progress,
        rest=rest,
        timed=(stages or StageTimes()).stage,
    )
    # A check waits for another Lean run, a view say, rather than failing at
    # once, so views drawn one after another never keep it out.
    # The Mac is kept awake from the slot to the last Lean process: the stages
    # between Lean runs, the snapshot capture above all, take minutes too.
    with (
        snapshot_slot(cancel=check.cancel, waiting=lambda: progress(SLOT_WAITING)),
        awake(),
        scratch_directory("glitnir-check-") as temporary,
        artifact_lease(temporary) as store,
    ):
        if check.cancel.is_set():
            raise SnapshotError("Project check cancelled.")
        acquired()
        environment = _resolve(check)
        frozen = _capture(check, environment, Path(temporary).resolve(), store)
        read = _read(check, frozen)
        compiled = _compile(check, frozen, read)
        reported = _report(check, frozen, environment, read)
        replayed = _replay(check, frozen, environment, reported)
        return _observe(frozen, environment, compiled, reported, replayed)


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
        text = read_regular(source, MAX_SOURCE_BYTES).decode().replace("\r\n", "\n")
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
            text = read_regular(source, MAX_SOURCE_BYTES).decode()
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
                "message": failure_location(failure, text, insert_after_line),
                "axioms": [],
                "type": None,
            }
        else:
            verdict = candidate_verdict(
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
