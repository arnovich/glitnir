"""Bounded observations of saved tactics; never checking or proof acceptance."""

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from glitnir.lean.artifacts import map_artifacts
from glitnir.lean.environment import MAX_TIMEOUT_MS
from glitnir.lean.limits import (
    SessionLimits,
    elaboration_timeout_ms,
    memory_limit_mb,
)
from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_checking import OUTPUT_BYTES, run_observer
from glitnir.lean.project_inspection import (
    TOOLCHAIN,
    _toolchain,
    inspect_project,
    lean_support,
)
from glitnir.lean.project_snapshot import SnapshotError, snapshot_slot
from glitnir.lean.sandbox import SandboxUnavailable
from glitnir.lean.scratch import scratch_directory

PROGRAM = Path(__file__).with_name("ProofTrace.lean")
MAX_NODES = 4096
MAX_TRACE_TEXT_BYTES = 8 * 1024**2


def _texts(value: Any) -> list[str]:
    """Validate bounded display text emitted by untrusted elaboration."""
    if (
        not isinstance(value, list)
        or len(value) > 4096
        or not all(
            isinstance(item, str) and len(item.encode()) <= 262144 for item in value
        )
    ):
        raise ValueError("Malformed proof trace text.")
    return value


def validate_trace(value: Any, source: str) -> dict[str, Any]:
    """Validate byte ranges and retain each tactic's own before/after context."""
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        raise ValueError("Malformed proof trace.")
    if len(value["nodes"]) > MAX_NODES:
        raise ValueError("Proof trace exceeds its node limit.")
    data = source.encode()
    nodes: list[dict[str, Any]] = []

    text_bytes = 0

    def reserve(size: int) -> None:
        nonlocal text_bytes
        text_bytes += size
        if text_bytes > MAX_TRACE_TEXT_BYTES:
            raise ValueError("Proof trace exceeds its expanded text limit.")

    errors, warnings = _texts(value.get("errors")), _texts(value.get("warnings"))
    reserve(sum(len(text.encode()) for text in errors + warnings))

    for index, node in enumerate(value["nodes"]):
        if not isinstance(node, dict):
            raise ValueError("Malformed tactic observation.")
        start, end, parent = (node.get(key) for key in ("start", "end", "parent"))
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(data)
            or (
                parent is not None
                and (type(parent) is not int or not 0 <= parent < index)
            )
            or not isinstance(node.get("declaration"), str)
            or not isinstance(node.get("kind"), str)
        ):
            raise ValueError("Invalid tactic source range or parent.")
        if parent is not None and not (
            nodes[parent]["start"] <= start < end <= nodes[parent]["end"]
        ):
            raise ValueError("Tactic range escapes its parent.")
        before, after = _texts(node.get("before")), _texts(node.get("after"))
        reserve(
            end
            - start
            + sum(len(text.encode()) for text in before + after)
            + len(node["declaration"].encode())
            + len(node["kind"].encode())
        )
        nodes.append(
            {
                "id": index,
                "start": start,
                "end": end,
                "parent": parent,
                "text": data[start:end].decode(),
                "before": before,
                "after": after,
                "declaration": node["declaration"],
                "kind": node["kind"],
            }
        )
    # Scan source once, even when thousands of nodes occur near its end.
    positions = {}
    previous, line, character = 0, 1, 0
    for offset in sorted({n[key] for n in nodes for key in ("start", "end")}):
        segment = data[previous:offset]
        line += segment.count(b"\n")
        last_newline = segment.rfind(b"\n")
        if last_newline >= 0:
            character = (
                len(segment[last_newline + 1 :].decode().encode("utf-16-le")) // 2
            )
        else:
            character += len(segment.decode().encode("utf-16-le")) // 2
        positions[offset] = {"line": line, "character": character}
        previous = offset
    for node in nodes:
        node["range"] = {key: positions[node[key]] for key in ("start", "end")}
    return {"nodes": nodes, "errors": errors, "warnings": warnings}


def _absolute_setup(setup: dict[str, Any], project: Path | None) -> dict[str, Any]:
    """Resolve inspected artifact paths for the observation's private cwd."""
    result: dict[str, Any] = json.loads(json.dumps(setup))

    def absolute(name: str) -> str:
        path = Path(name)
        if not path.is_absolute():
            if project is None:
                raise SnapshotError("Relative observation input without a project.")
            path = project / path
        return str(path)

    result["importArts"] = {
        module: map_artifacts(paths, lambda name, _: absolute(name))
        for module, paths in result["importArts"].items()
    }
    for field in ("plugins", "dynlibs"):
        result[field] = [
            (
                {**item, "path": absolute(item["path"])}
                if isinstance(item, dict)
                else absolute(item)
            )
            for item in result[field]
        ]
    return result


def _check_configuration(project: Path | None, resolution: dict[str, Any]) -> None:
    """Reject changed inspected package configuration, without claiming immutability."""
    assert project is not None
    for item in resolution["inputs"]:
        path = project / item["path"]
        try:
            digest = hashlib.sha256(read_regular(path)).hexdigest()
        except FileNotFoundError:
            digest = None
        if digest != item["sha256"]:
            raise SnapshotError("Project configuration changed; refresh proof steps.")


def trace_file(
    source: Path,
    *,
    expected_sha256: str,
    timeout_ms: int | None = None,
    cancel: threading.Event | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Observe copied saved source with read-only dependencies; never check a proof."""
    cancellation = cancel if cancel is not None else threading.Event()
    try:
        timeout = (
            SessionLimits(
                timeout_ms=min(
                    (
                        elaboration_timeout_ms(default=600_000)
                        if timeout_ms is None
                        else timeout_ms
                    ),
                    MAX_TIMEOUT_MS,
                )
            ).timeout_ms
            / 1000
        )
        # Refuse before the Lean slot or the toolchain is touched, with the
        # same words as every other refusal; the checks below still catch a
        # declaration changed meanwhile.
        message = lean_support(describe_project(source))["message"]
        if message is not None:
            raise SnapshotError(message)
        saved = read_regular(source, 16 * 1024**2).decode().replace("\r\n", "\n")
        if hashlib.sha256(saved.encode()).hexdigest() != expected_sha256:
            raise SnapshotError("Saved source changed; refresh and retry.")
        with (
            snapshot_slot(),
            scratch_directory("glitnir-trace-") as temporary,
        ):
            root = Path(temporary).resolve()
            toolchain = _toolchain()
            resolution = None
            project = describe_project(source)
            if project["kind"] == "lake":
                progress("Resolving project environment")
                inspected = inspect_project(
                    source, expected_sha256=expected_sha256, cancel=cancellation
                )
                if inspected["status"] != "resolved":
                    raise SnapshotError(inspected["message"])
                setup, resolution = inspected["setup"], inspected["resolution"]
                project_root: Path | None = Path(project["root"]).resolve()
            else:
                support = lean_support(project)
                if not support["supported"]:
                    raise SnapshotError(support["message"])
                setup = {
                    "name": "GlitnirTrace",
                    "isModule": False,
                    "importArts": {},
                    "plugins": [],
                    "dynlibs": [],
                    "options": {},
                }
                project_root = None
            progress("Preparing proof observation")
            # Observation dependencies remain read-only live inputs, as in
            # project inspection. Only checking promises a frozen environment.
            setup = _absolute_setup(setup, project_root)
            if resolution is not None:
                _check_configuration(project_root, resolution)
            programs = root / "programs"
            programs.mkdir()
            helper = programs / PROGRAM.name
            helper.write_bytes(PROGRAM.read_bytes())
            setup_path = programs / "setup.json"
            setup_path.write_text(json.dumps(setup))
            input_path = programs / "Source.lean"
            input_path.write_text(saved)
            output = root / "output"
            output.mkdir()
            report = output / "trace.json"
            progress("Recording saved tactic goals")
            run_observer(
                toolchain,
                [
                    "lean",
                    f"-M{memory_limit_mb()}",
                    "--run",
                    str(helper),
                    str(input_path),
                    str(setup_path),
                    # No extra library roots: dependencies come from setup.
                    "[]",
                    str(report),
                ],
                cwd=output,
                readable=[
                    programs,
                    toolchain,
                    *([project_root] if project_root else []),
                ],
                writable=True,
                cancel=cancellation,
                deadline=time.monotonic() + timeout,
            )
            result = validate_trace(
                json.loads(read_regular(report, OUTPUT_BYTES)), saved
            )
            if cancellation.is_set():
                raise SnapshotError("Proof observation cancelled.")
            if (
                read_regular(source, 16 * 1024**2).decode().replace("\r\n", "\n")
                != saved
            ):
                raise SnapshotError("Saved source changed; refresh proof steps.")
            if resolution is not None:
                _check_configuration(project_root, resolution)
            return {
                **result,
                "status": "partial" if result["errors"] else "ready",
                "source_sha256": expected_sha256,
                "environment": {
                    "kind": "read_only_live_dependencies",
                    "toolchain": TOOLCHAIN,
                    "project": str(project_root) if project_root else None,
                },
                "message": "Recorded goals; the claim check status is unchanged.",
            }
    except (OSError, ValueError, KeyError, SandboxUnavailable) as error:
        return {
            "status": "cancelled" if cancellation.is_set() else "unavailable",
            "source_sha256": expected_sha256,
            "nodes": [],
            "errors": [],
            "warnings": [],
            "message": str(error),
        }
