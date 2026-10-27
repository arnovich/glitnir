"""Run provider Lean code in the notebook's sandbox; its output is untrusted data."""

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from glitnir.lean import fingerprint
from glitnir.lean.awake import awake, noting_sleep
from glitnir.lean.digest import fingerprint_ok
from glitnir.lean.environment import MAX_SOURCE_BYTES
from glitnir.lean.limits import memory_limit_mb
from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_checking import (
    OUTPUT_BYTES,
    TIMED_OUT,
    check_seconds,
    run_observer,
)
from glitnir.lean.project_inspection import MODULE_NAME, _toolchain, inspect_project
from glitnir.lean.project_snapshot import (
    SLOT_BUSY,
    SLOT_WAITING,
    SnapshotError,
    snapshot_slot,
)
from glitnir.lean.proof_trace import _absolute_setup, _check_configuration
from glitnir.lean.sandbox import SandboxUnavailable
from glitnir.lean.scratch import scratch_directory

PROGRAM = Path(__file__).with_name("ViewElab.lean")
NO_OUTPUT = {
    "view": "The provider printed no drawing.",
    "evidence": "The provider printed no evidence.",
}
# Without Lake there are no prepared imports, not even the prelude.
LAKE_NEEDED = {
    "view": "Views need a Lake project; see the README.",
    "evidence": "Evidence needs a Lake project; see the README.",
    "decision": "Decisions need a Lake project; see the README.",
}
CANCELLED = {
    "view": "View drawing cancelled.",
    "evidence": "Evidence run cancelled.",
}
#: What a provider run reports while it waits for, prepares and runs Lean.
WAITING = SLOT_WAITING
PREPARING = "Reading the project's setup with Lake."
RUNNING = {"view": "Lean is drawing it.", "evidence": "Lean is running it."}
# Lake lists only workspace modules in `importArts`; the toolchain's own
# libraries are always found through the sysroot search path.
TOOLCHAIN_LIBRARIES = frozenset({"Init", "Std", "Lean", "Lake"})


#: How much of a provider's first Lean error a card shows.
ERROR_LINES = 12
ERROR_CHARACTERS = 2000

#: How many prepared Lake setups views and evidence keep for reuse.
MAX_SETUPS = 8
_SetupKey = tuple[str, tuple[str, ...], tuple[str, ...], str]
# Prepared setups by (notebook, its header imports, provider modules, environment
# digest), oldest first. A setup is reused only while the environment digest,
# which covers the import closure's sources, build traces and package git
# states, is unchanged; `_check_configuration` still re-reads the Lake
# configuration bytes before and after every run, and a mismatch drops the
# entry. Project checks and decision candidate checks never use it.
_SETUPS: OrderedDict[_SetupKey, tuple[dict[str, Any], dict[str, Any], Path]] = (
    OrderedDict()
)
_SETUPS_LOCK = threading.Lock()

# Failures that say nothing about the output: a later request may succeed.
INTERRUPTED = ("cancelled.", "timed out.")


def _failure(error: Exception) -> dict[str, Any]:
    """Report an error, marking a busy slot, cancellation or timeout transient."""
    message = str(error)
    if isinstance(error, SnapshotError) and (
        message == SLOT_BUSY or message.endswith(INTERRUPTED)
    ):
        return {"error": message, "transient": True}
    return {"error": message}


def _available(module: str, setup: dict[str, Any]) -> bool:
    """Whether Lean can load `module` from the prepared setup or the toolchain."""
    return module in setup["importArts"] or module.split(".")[0] in TOOLCHAIN_LIBRARIES


def _invalid_import(imports: tuple[str, ...]) -> dict[str, Any] | None:
    """Refuse a declared import that is not a module name, before it reaches Lean."""
    for module in imports:
        if not MODULE_NAME.fullmatch(module):
            return {"error": f"The provider declares an invalid import `{module}`."}
    return None


def _missing_import(
    imports: tuple[str, ...], setup: dict[str, Any], needs: str
) -> dict[str, Any] | None:
    """Name the first import the notebook's prepared setup cannot load."""
    for module in imports:
        if not _available(module, setup):
            return {"error": f"This {needs} needs `import {module}` in the notebook."}
    return None


def _unprovided(
    imports: tuple[str, ...], setup: dict[str, Any], needs: str
) -> dict[str, Any] | None:
    """Name the first provider module that no package of the project provides.

    `setup` was prepared with `imports` added to the notebook's own, so a
    module missing from it is one Lake found in no package.
    """
    for module in imports:
        if not _available(module, setup):
            return {
                "error": f"This {needs} needs the module `{module}`, which no "
                "package of this Lake project provides. Add the package that "
                "provides it to the project's dependencies."
            }
    return None


def _unbuilt(
    imports: tuple[str, ...], setup: dict[str, Any], root: Path, needs: str
) -> str:
    """Say which provider modules to build; `setup` is the notebook's own."""
    modules = [m for m in imports if not _available(m, setup)] or list(imports)
    return (
        f"This {needs} needs {', '.join(f'`{m}`' for m in modules)}, which this "
        f"project has not built or has out of date. Run "
        f"`lake build {' '.join(modules)}` in {root}, then try again."
    )


def _project_modules(imports: tuple[str, ...]) -> tuple[str, ...]:
    """Leave out toolchain libraries, which Lean finds without Lake's setup."""
    return tuple(m for m in imports if m.split(".")[0] not in TOOLCHAIN_LIBRARIES)


def _provider_source(
    text: str, lines: list[int], imports: tuple[str, ...], *, needs: str
) -> tuple[str, list[int]]:
    """Return SOURCE and the byte offset in it of each directive line in `lines`.

    SOURCE is the provider imports and the notebook before the last of
    `lines`. Each view is spliced in at the start of its directive line: the
    program elaborates the notebook up to there, then the provider's preamble
    and command, which it is given separately.
    """
    split = text.split("\n")
    for line in lines:
        if not 1 <= line <= len(split):
            raise ValueError(
                f"{needs.capitalize()} directive line {line} is outside the notebook."
            )
    # Imports must precede every command; Lean accepts duplicated imports.
    header = "".join(f"import {module}\n" for module in imports)
    last = max(lines, default=1)
    source = header + "".join(item + "\n" for item in split[: last - 1])
    # starts[n] is the byte offset of notebook line n + 1 in SOURCE.
    starts = [len(header.encode())]
    for item in split[: last - 1]:
        starts.append(starts[-1] + len(item.encode()) + 1)
    return source, [starts[line - 1] for line in lines]


def _saved(source: Path) -> str:
    return read_regular(source, MAX_SOURCE_BYTES).decode().replace("\r\n", "\n")


def _setup_key(
    source: Path, imports: tuple[str, ...], environment_sha256: str | None
) -> _SetupKey | None:
    """Return the cache key of a setup, or None when it must not be kept."""
    if not fingerprint_ok(environment_sha256):
        return None
    assert environment_sha256 is not None
    try:
        text = _saved(source)
    except (OSError, ValueError):
        return None
    # The environment digest leaves out the notebook's own text, but Lake's
    # setup depends on the header's `module`, `prelude` and import modifiers
    # too, so the whole header is part of the key.
    header = fingerprint.header_words(text) or (
        "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
    )
    return (
        str(source.resolve()),
        tuple(header),
        _project_modules(imports),
        environment_sha256,
    )


def _forget_setup(
    source: Path, imports: tuple[str, ...], environment_sha256: str | None
) -> None:
    """Drop the kept setup for these arguments, if there is one."""
    key = _setup_key(source, imports, environment_sha256)
    if key is not None:
        with _SETUPS_LOCK:
            _SETUPS.pop(key, None)


def _setup(
    source: Path,
    expected_sha256: str,
    cancel: threading.Event,
    needs: str,
    imports: tuple[str, ...] = (),
    environment_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Prepare the notebook's Lake environment exactly as project tracing does.

    With a valid `environment_sha256` the prepared setup is kept and handed
    out again for the same notebook, imports and digest (see `_SETUPS`);
    without one it is prepared every time. Callers drop a kept setup with
    `_forget_setup` when its configuration no longer matches.

    With `imports`, the setup also loads those modules from the project's
    existing build, as if the notebook imported them. If Lake then finds an
    import out of date while the notebook's own setup is fine, the error names
    the provider modules to build.
    """
    project = describe_project(source)
    if project["kind"] != "lake":
        raise SnapshotError(LAKE_NEEDED[needs])
    root = Path(project["root"]).resolve()
    extra = _project_modules(imports)
    key = _setup_key(source, imports, environment_sha256)
    if key is not None:
        with _SETUPS_LOCK:
            kept = _SETUPS.get(key)
            if kept is not None:
                _SETUPS.move_to_end(key)
        if kept is not None:
            return copy.deepcopy(kept[0]), kept[1], kept[2]
    inspected = inspect_project(
        source, expected_sha256=expected_sha256, cancel=cancel, imports=extra
    )
    if inspected["status"] != "resolved":
        if extra and inspected.get("needs_build") and not cancel.is_set():
            # Tell an unbuilt provider module from an unbuilt notebook import.
            own = inspect_project(
                source, expected_sha256=expected_sha256, cancel=cancel
            )
            if own["status"] == "resolved":
                raise SnapshotError(_unbuilt(extra, own["setup"], root, needs))
        raise SnapshotError(inspected["message"])
    prepared = (inspected["setup"], inspected["resolution"], root)
    if key is not None:
        with _SETUPS_LOCK:
            _SETUPS[key] = (copy.deepcopy(prepared[0]), prepared[1], root)
            _SETUPS.move_to_end(key)
            while len(_SETUPS) > MAX_SETUPS:
                _SETUPS.popitem(last=False)
    return prepared


def _messages(value: Any, key: str, needs: str) -> list[str]:
    if not isinstance(value, dict):
        raise ValueError(f"Malformed {needs} report.")
    texts = value.get(key)
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise ValueError(f"Malformed {needs} report.")
    return texts


def _errors(value: Any, needs: str) -> list[tuple[int | None, str]]:
    """Read errors as `(line, text)`; older reports carry only the text."""
    entries = value.get("errors") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"Malformed {needs} report.")
    errors: list[tuple[int | None, str]] = []
    for entry in entries:
        if isinstance(entry, str):
            errors.append((None, entry))
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
            raise ValueError(f"Malformed {needs} report.")
        line = entry.get("line")
        if line is not None and (type(line) is not int or line < 1):
            raise ValueError(f"Malformed {needs} report.")
        errors.append((line, entry["text"]))
    return errors


def _bounded(text: str) -> str:
    """Return a Lean error's non-blank lines, bounded; a last line `…` marks a cut.

    At most `ERROR_LINES` lines and `ERROR_CHARACTERS` characters are kept:
    Lean's first line alone can hide the cause, such as an ambiguous name's
    "Possible interpretations". The text is plain; whoever shows it escapes it.
    """
    lines = [part.rstrip() for part in text.splitlines() if part.strip()]
    shown = "\n".join(lines[:ERROR_LINES])[:ERROR_CHARACTERS].rstrip()
    if not shown:
        return "error"
    if len(lines) > ERROR_LINES or len("\n".join(lines)) > ERROR_CHARACTERS:
        return shown + "\n…"
    return shown


def _marker_payload(
    report: Any, marker: str, imports: int, line: int, needs: str
) -> dict[str, Any]:
    """Prefer the provider's marked output; else the first error, bounded.

    The payload is returned unvalidated. An error's line is in SOURCE:
    `imports` provider imports, then the notebook before the directive `line`.
    An error without a line is in the provider's own code, and is reported at
    the directive. `needs` words the errors, as in `run_provider_lean`.
    """
    information = _messages(report, "information", needs)
    errors = _errors(report, needs)
    for message in reversed(information):
        if message.startswith(marker):
            return {"payload": message[len(marker) :]}
    if errors:
        error_line, text = errors[0]
        shown = _bounded(text)
        notebook_line = line if error_line is None else error_line - imports
        if not 1 <= notebook_line < line:
            notebook_line = line
        return {"error": f"Line {notebook_line}: {shown}"}
    return {"error": NO_OUTPUT[needs]}


def _view_reports(report: Any, count: int, needs: str) -> list[dict[str, Any]]:
    """Return the report's `count` views; `_marker_payload` reads the rest."""
    views = report.get("views") if isinstance(report, dict) else None
    if not isinstance(views, list) or len(views) != count:
        raise ValueError(f"Malformed {needs} report.")
    for view in views:
        if not isinstance(view, dict) or type(view.get("proofs_skipped")) is not bool:
            raise ValueError(f"Malformed {needs} report.")
    return views


def _view_result(
    view: dict[str, Any], marker: str, imports: int, line: int, needs: str
) -> dict[str, Any]:
    """One view's payload, saying whether it was drawn with proofs skipped."""
    result = _marker_payload(view, marker, imports, line, needs)
    if "payload" in result:
        return {**result, "proofs_skipped": view["proofs_skipped"]}
    return result


def _check_configuration_or_forget(
    project_root: Path,
    resolution: dict[str, Any],
    source: Path,
    imports: tuple[str, ...],
    environment_sha256: str | None,
) -> None:
    """Check the Lake configuration; a kept setup that fails is dropped."""
    try:
        _check_configuration(project_root, resolution)
    except ValueError:
        _forget_setup(source, imports, environment_sha256)
        raise


def run_provider_batch(
    source: Path,
    *,
    expected_sha256: str,
    provider_name: str,
    imports: tuple[str, ...],
    preamble: str,
    marker: str,
    needs: str,
    views: list[tuple[int, str]],
    cancel: threading.Event,
    progress: Callable[[str], None] | None = None,
    wait: bool = False,
    environment_sha256: str | None = None,
) -> dict[str, Any]:
    """Elaborate the notebook and several provider commands in one Lean run.

    `views` holds `(directive line, command)` pairs, lines ascending. Each
    command is elaborated after the notebook before its line and the
    provider's `preamble`, all in one sandboxed Lean process. The notebook is
    first elaborated with its proofs skipped; a view whose marked output is
    then missing is drawn again with them.

    Returns `{"views": [result, ...]}` in the order of `views`, each result
    being `{"payload": text, "proofs_skipped": bool}`, with the text after
    `marker` in the last message that carries it, or `{"error": message}`.
    A failure of the whole run is `{"error": message}`, marked `transient`
    when a later request may succeed. `needs` is `"view"` or `"evidence"`
    and words the messages. The provider's `imports` are loaded from the
    project's existing build, whether or not the notebook imports them. With
    `wait`, a busy Lean slot is waited for, up to the check time limit,
    rather than refused. `progress` is told what the run is doing.
    `environment_sha256` is the environment digest of the notebook with the
    provider's imports; when it is a valid digest the prepared Lake setup is
    reused for it (see `_SETUPS`), otherwise it is prepared for this run.
    """
    report = noting_sleep(progress or (lambda _message: None))
    try:
        if describe_project(source)["kind"] != "lake":
            return {"error": LAKE_NEEDED[needs]}
        saved = _saved(source)
        if hashlib.sha256(saved.encode()).hexdigest() != expected_sha256:
            raise SnapshotError("Saved source changed; refresh and retry.")
        lines = [line for line, _command in views]
        if lines != sorted(lines):
            raise ValueError(f"{needs.capitalize()} directives must be in order.")
        try:  # provider code must never break the preview
            invalid = _invalid_import(imports)
            if invalid is not None:
                return invalid
            if not isinstance(preamble, str) or not all(
                isinstance(command, str) for _line, command in views
            ):
                raise TypeError("the preamble and commands must be text")
            text, offsets = _provider_source(saved, lines, imports, needs=needs)
        except ValueError:
            raise
        except Exception as error:
            return {
                "error": f"Provider {provider_name} failed: "
                f"{type(error).__name__}: {error}"
            }
        if not views:
            return {"views": []}
        slot = (
            snapshot_slot(cancel=cancel, waiting=lambda: report(WAITING))
            if wait
            else snapshot_slot()
        )
        with (
            slot,
            awake(),
            scratch_directory("glitnir-view-") as temporary,
        ):
            root = Path(temporary).resolve()
            toolchain = _toolchain()
            report(PREPARING)
            setup, resolution, project_root = _setup(
                source, expected_sha256, cancel, needs, imports, environment_sha256
            )
            # The setup loads the provider's modules from the project's build.
            missing = _unprovided(imports, setup, needs)
            if missing is not None:
                return missing
            setup = _absolute_setup(setup, project_root)
            _check_configuration_or_forget(
                project_root, resolution, source, imports, environment_sha256
            )
            programs = root / "programs"
            programs.mkdir()
            helper = programs / PROGRAM.name
            helper.write_bytes(PROGRAM.read_bytes())
            setup_path = programs / "setup.json"
            setup_path.write_text(json.dumps(setup))
            input_path = programs / "Source.lean"
            input_path.write_bytes(text.encode())
            request_path = programs / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "source": str(input_path),
                        "setup": str(setup_path),
                        "marker": marker,
                        "views": [
                            {"at": at, "preamble": preamble, "command": command}
                            for at, (_line, command) in zip(offsets, views)
                        ],
                    }
                )
            )
            output = root / "output"
            output.mkdir()
            report_path = output / "view.json"
            report(RUNNING[needs])
            run_observer(
                toolchain,
                [
                    "lean",
                    f"-M{memory_limit_mb()}",
                    "--run",
                    str(helper),
                    str(request_path),
                    str(report_path),
                ],
                cwd=output,
                readable=[programs, toolchain, project_root],
                writable=True,
                cancel=cancel,
                deadline=time.monotonic() + check_seconds(),
            )
            if cancel.is_set():
                raise SnapshotError(CANCELLED[needs])
            reports = _view_reports(
                json.loads(read_regular(report_path, OUTPUT_BYTES)), len(views), needs
            )
            results = [
                _view_result(view, marker, len(imports), line, needs)
                for view, line in zip(reports, lines)
            ]
            if _saved(source) != saved:
                raise SnapshotError("Saved source changed; refresh and retry.")
            _check_configuration_or_forget(
                project_root, resolution, source, imports, environment_sha256
            )
            return {"views": results}
    except (OSError, ValueError, KeyError, SandboxUnavailable) as error:
        return _failure(error)


def run_provider_lean(
    source: Path,
    *,
    expected_sha256: str,
    line: int,
    provider_name: str,
    imports: tuple[str, ...],
    preamble: str,
    command: str,
    marker: str,
    needs: str,
    cancel: threading.Event,
    progress: Callable[[str], None] | None = None,
    wait: bool = False,
    environment_sha256: str | None = None,
) -> dict[str, Any]:
    """Elaborate the notebook prefix and one provider command in the sandbox.

    Returns the one view's result from `run_provider_batch`: `{"payload":
    text, "proofs_skipped": bool}` or `{"error": message}`, which a failure
    of the whole run also is. An error of the directive itself (Lean's first
    error, or no marked output) is marked `"lean": True`, and a run that
    reached its own time limit `"timed_out": True`: those say something about
    the directive. Nothing else is marked: a setup, sandbox or slot failure,
    or a cancellation, may name local paths or pass by itself.
    """
    result = run_provider_batch(
        source,
        expected_sha256=expected_sha256,
        provider_name=provider_name,
        imports=imports,
        preamble=preamble,
        marker=marker,
        needs=needs,
        views=[(line, command)],
        cancel=cancel,
        progress=progress,
        wait=wait,
        environment_sha256=environment_sha256,
    )
    if "views" not in result:
        if result.get("error") == TIMED_OUT:
            return {**result, "timed_out": True}
        return result
    view: dict[str, Any] = result["views"][0]
    if "error" in view:
        return {**view, "lean": True}
    return view


def provider_imports_error(
    source: Path,
    *,
    expected_sha256: str,
    imports: tuple[str, ...],
    needs: str,
    cancel: threading.Event,
) -> dict[str, Any] | None:
    """Prepare the notebook's setup as a provider run does and check `imports`.

    For a provider whose own code runs outside Lean, but whose Lean output the
    notebook must be able to load. Returns None when Lean can load every module
    in `imports` from the saved notebook's prepared setup, else `{"error":
    message}`, marked transient like `run_provider_lean`'s errors. Without
    `imports` there is nothing to load, so no setup is prepared at all.

    The setup is not handed on to a later candidate check, which prepares its
    own on purpose: the provider runs for an unbounded time in between, during
    which the project may be rebuilt or reconfigured. Preparing the setup again
    keeps the snapshot bound to the Lake state at capture time, not to the
    state before the provider ran.
    """
    if not imports:
        return None
    try:
        invalid = _invalid_import(imports)
        if invalid is not None:
            return invalid
        if describe_project(source)["kind"] != "lake":
            return {"error": LAKE_NEEDED[needs]}
        with snapshot_slot():
            setup, _resolution, _root = _setup(source, expected_sha256, cancel, needs)
        return _missing_import(imports, setup, needs)
    except (OSError, ValueError, KeyError, SandboxUnavailable) as error:
        return _failure(error)
