"""Run provider Lean code in the notebook's sandbox; its output is untrusted data."""

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from glitnir.lean.limits import memory_limit_mb
from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_checking import OUTPUT_BYTES, check_seconds, run_observer
from glitnir.lean.project_inspection import _toolchain, inspect_project
from glitnir.lean.project_snapshot import SLOT_BUSY, SnapshotError, snapshot_slot
from glitnir.lean.proof_trace import _absolute_setup, _check_configuration
from glitnir.lean.sandbox import SandboxUnavailable
from glitnir.lean.scratch import scratch_directory

PROGRAM = Path(__file__).with_name("ViewElab.lean")
SOURCE_BYTES = 16 * 1024**2
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
# Lake lists only workspace modules in `importArts`; the toolchain's own
# libraries are always found through the sysroot search path.
TOOLCHAIN_LIBRARIES = frozenset({"Init", "Std", "Lean", "Lake"})


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


MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*")


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


def compose_provider_source(
    text: str,
    line: int,
    imports: tuple[str, ...],
    preamble: str,
    command: str,
    *,
    needs: str,
) -> str:
    """Return provider imports, the notebook before `line`, preamble and command."""
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        raise ValueError(
            f"{needs.capitalize()} directive line {line} is outside the notebook."
        )
    prefix = "".join(item + "\n" for item in lines[: line - 1])
    # Imports must precede every command; Lean accepts duplicated imports.
    header = "".join(f"import {module}\n" for module in imports)
    return header + prefix + "\n" + preamble + "\n" + command + "\n"


def _saved(source: Path) -> str:
    return read_regular(source, SOURCE_BYTES).decode().replace("\r\n", "\n")


def _setup(
    source: Path, expected_sha256: str, cancel: threading.Event, needs: str
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Prepare the notebook's Lake environment exactly as project tracing does."""
    project = describe_project(source)
    if project["kind"] != "lake":
        raise SnapshotError(LAKE_NEEDED[needs])
    inspected = inspect_project(source, expected_sha256=expected_sha256, cancel=cancel)
    if inspected["status"] != "resolved":
        raise SnapshotError(inspected["message"])
    return (
        inspected["setup"],
        inspected["resolution"],
        Path(project["root"]).resolve(),
    )


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


def _marker_payload(
    report: Any, marker: str, imports: int, line: int, needs: str
) -> dict[str, Any]:
    """Prefer the provider's marked output; else the first line of the first error.

    The payload is returned unvalidated. An error's line is in the composed
    source: `imports` provider imports, then the notebook before the directive
    `line`, then the provider's own code, which is reported at the directive.
    `needs` words the errors, as in `run_provider_lean`.
    """
    information = _messages(report, "information", needs)
    errors = _errors(report, needs)
    for message in reversed(information):
        if message.startswith(marker):
            return {"payload": message[len(marker) :]}
    if errors:
        error_line, text = errors[0]
        first = next((part for part in text.splitlines() if part.strip()), "error")
        if error_line is None:
            return {"error": first}
        notebook_line = error_line - imports
        if not 1 <= notebook_line < line:
            notebook_line = line
        return {"error": f"Line {notebook_line}: {first}"}
    return {"error": NO_OUTPUT[needs]}


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
) -> dict[str, Any]:
    """Elaborate the notebook prefix and a provider command in the sandbox.

    Returns the text after `marker` in the last message that carries it, as
    `{"payload": text}`, or `{"error": message}`. `needs` is `"view"` or
    `"evidence"` and words the messages.
    """
    try:
        if describe_project(source)["kind"] != "lake":
            return {"error": LAKE_NEEDED[needs]}
        saved = _saved(source)
        if hashlib.sha256(saved.encode()).hexdigest() != expected_sha256:
            raise SnapshotError("Saved source changed; refresh and retry.")
        try:  # provider code must never break the preview
            invalid = _invalid_import(imports)
            if invalid is not None:
                return invalid
            composed = compose_provider_source(
                saved, line, imports, preamble, command, needs=needs
            )
        except ValueError:
            raise
        except Exception as error:
            return {
                "error": f"Provider {provider_name} failed: "
                f"{type(error).__name__}: {error}"
            }
        with (
            snapshot_slot(),
            scratch_directory("glitnir-view-") as temporary,
        ):
            root = Path(temporary).resolve()
            toolchain = _toolchain()
            setup, resolution, project_root = _setup(
                source, expected_sha256, cancel, needs
            )
            # Lean only sees modules the notebook already imports.
            missing = _missing_import(imports, setup, needs)
            if missing is not None:
                return missing
            setup = _absolute_setup(setup, project_root)
            _check_configuration(project_root, resolution)
            programs = root / "programs"
            programs.mkdir()
            helper = programs / PROGRAM.name
            helper.write_bytes(PROGRAM.read_bytes())
            setup_path = programs / "setup.json"
            setup_path.write_text(json.dumps(setup))
            input_path = programs / "Source.lean"
            input_path.write_text(composed)
            output = root / "output"
            output.mkdir()
            report = output / "view.json"
            run_observer(
                toolchain,
                [
                    "lean",
                    f"-M{memory_limit_mb()}",
                    "--run",
                    str(helper),
                    str(input_path),
                    str(setup_path),
                    str(report),
                ],
                cwd=output,
                readable=[programs, toolchain, project_root],
                writable=True,
                cancel=cancel,
                deadline=time.monotonic() + check_seconds(),
            )
            if cancel.is_set():
                raise SnapshotError(CANCELLED[needs])
            result = _marker_payload(
                json.loads(read_regular(report, OUTPUT_BYTES)),
                marker,
                len(imports),
                line,
                needs,
            )
            if _saved(source) != saved:
                raise SnapshotError("Saved source changed; refresh and retry.")
            _check_configuration(project_root, resolution)
            return result
    except (OSError, ValueError, KeyError, SandboxUnavailable) as error:
        return _failure(error)


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
