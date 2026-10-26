"""Prepare a private importable lemma module before allocating a proof-check job."""

import fcntl
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from glitnir.compute.client import save_receipt
from glitnir.compute.lemma_bundle import canonical, unique_object, validate_bundle
from glitnir.compute.preparation_limits import require_build_limits
from glitnir.compute.protocol import (
    checker_digest,
    context_digest,
    digest,
    make_request,
    source_path,
    stage_project,
    validate_request,
)
from glitnir.lean.environment import (
    MAX_OUTPUT_BYTES,
    kill_and_reap,
    set_output_file_limit,
)
from glitnir.lean.project import read_regular
from glitnir.lean.project_inspection import _toolchain, inspect_project
from glitnir.lean.sandbox import confine


def _child_limits(max_bytes: int) -> None:
    """Apply inherited limits; the lock-holding supervisor handles parent death."""
    set_output_file_limit(max_bytes)


def _files(root: Path) -> Iterator[Path]:
    """Traverse fail-closed, including unreadable directories and special files."""

    def failed(error: OSError) -> None:
        raise error

    count = 0
    for parent, directories, files in os.walk(root, followlinks=False, onerror=failed):
        for name in sorted([*directories, *files]):
            count += 1
            if count > 200000:
                raise ValueError("Handoff exceeds traversal budget")
            path = Path(parent) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                yield path
            elif not stat.S_ISDIR(mode):
                raise ValueError("Handoff contains a link or non-regular file")


def _inventory(root: Path, monitor: Callable[[], None]) -> dict[str, str]:
    """Hash every regular file without silently ignoring unreadable artifacts."""
    result = {}
    if not root.is_dir():
        raise ValueError("Handoff build directory is missing")
    for path in _files(root):
        monitor()
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                monitor()
                checksum.update(chunk)
        result[path.relative_to(root).as_posix()] = checksum.hexdigest()
    return result


def _size(root: Path) -> int:
    """Bound traversal and reject links rather than following build outputs."""
    size = 0
    for path in _files(root):
        if path.is_symlink():
            raise ValueError("Handoff trees must not contain symbolic links")
        size += path.stat().st_size
    return size


def build_module(
    project: Path,
    module_path: str,
    deadline: float,
    memory_mb: int,
    root: Path,
    max_bytes: int,
    lock_fd: int,
) -> None:
    """Run real confined Lake, exposing only private build output as writable."""
    require_build_limits(memory_mb)
    toolchain = _toolchain()
    builds = project / ".lake"
    builds.mkdir(exist_ok=True)
    command = confine(
        [
            str(toolchain / "bin/lake"),
            "--keep-toolchain",
            "--no-cache",
            "build",
            module_path + ":leanArts",
        ],
        readable=[project, toolchain],
        writable=[builds],
        cwd=project,
        required=True,
    )
    environment = {
        "PATH": f"{toolchain / 'bin'}:/usr/bin:/bin",
        "LEAN_SYSROOT": str(toolchain),
        "LEAN_PATH": "",
        "LAKE_HOME": str(toolchain),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LEAN_NUM_THREADS": "2",
    }
    command_file, command_result = root / "command.json", root / "command-result.json"
    command_result.unlink(missing_ok=True)
    save_receipt(
        command_file,
        {"command": command, "parent": os.getpid(), "result": str(command_result)},
    )
    with tempfile.TemporaryFile() as out:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "glitnir.compute.build_process",
                str(command_file),
            ],
            cwd=project,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=partial(_child_limits, max_bytes),
            pass_fds=(lock_fd,),
        )
        try:
            last_size_check = 0.0
            while process.poll() is None:
                if time.time() >= deadline:
                    raise ValueError("Handoff preparation deadline exhausted")
                if out.tell() > MAX_OUTPUT_BYTES:
                    raise ValueError("Handoff preparation log exceeds output limit")
                if time.monotonic() - last_size_check > 0.25:
                    if _size(root) > max_bytes:
                        raise ValueError("Handoff exceeds disk budget")
                    last_size_check = time.monotonic()
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            out.seek(0)
            output = out.read(MAX_OUTPUT_BYTES + 1)
            if (
                not command_result.is_file()
                or json.loads(read_regular(command_result, 1024)).get("returncode") != 0
                or len(output) > MAX_OUTPUT_BYTES
            ):
                raise ValueError(
                    "Handoff Lake build failed: "
                    + output.decode(errors="replace")[-4000:]
                )
        finally:
            kill_and_reap(process)


def _private_directory(root: Path) -> None:
    """Require an operator-owned private directory without symlink ancestors."""
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Handoff directory must not cross a symbolic link")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = root.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("Handoff directory must be private (chmod 700)")
    _size(root)


def prepare_handoff(
    prepared: Path,
    bundle: dict[str, Any],
    candidate: str,
    directory: Path,
    *,
    environment: str,
    candidate_path: str,
    timeout_seconds: int,
    memory_mb: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Resume frozen preparation inputs and return a binding only after staged setup.

    No server call, model call, compute submission or proof acceptance occurs.
    The original absolute deadline survives interruptions. A completed build is
    inventoried before publication and reused for a retry of metadata preflight.
    """
    started = time.time()
    bundle = validate_bundle(bundle)
    relative = source_path(candidate_path)
    request = bundle["job"]["request"]
    if candidate_path == request["path"]:
        raise ValueError("Use a separate follow-on candidate path")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**4:
        raise ValueError("Invalid handoff disk budget")
    # Validate resources, environment, exact candidate bytes and path before I/O.
    probe = {
        **request,
        "environment": environment,
        "path": candidate_path,
        "source": candidate,
        "source_sha256": digest(candidate.encode()),
        "timeout_seconds": timeout_seconds,
        "memory_mb": memory_mb,
    }
    validate_request(probe)
    require_build_limits(memory_mb)
    prepared, root = prepared.absolute(), directory.absolute()
    if any(p.is_symlink() for p in (prepared, *prepared.parents)):
        raise ValueError("Prepared base must not cross a symbolic link")
    if prepared.is_relative_to(root) or root.is_relative_to(prepared):
        raise ValueError("Handoff directory must be separate from the prepared base")
    if request["checker_sha256"] != checker_digest():
        raise ValueError("Lemma checker differs from the installed checker")
    if (
        context_digest(prepared / source_path(request["path"]))
        != request["context_sha256"]
    ):
        raise ValueError("Original lemma context differs from the prepared base")
    if (prepared / relative).exists():
        raise ValueError("Follow-on candidate path already exists in the prepared base")
    if _size(prepared) * 2 + len(candidate.encode()) * 2 > max_bytes:
        raise ValueError("Prepared copies exceed handoff disk budget")
    _private_directory(root)
    if _size(root) > max_bytes:
        raise ValueError("Handoff exceeds disk budget")
    with (root / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "Another preparation uses this handoff directory"
            ) from error
        return _prepare_locked(
            prepared,
            bundle,
            probe,
            root,
            max_bytes,
            lock.fileno(),
            started + timeout_seconds,
        )


def _prepare_locked(
    prepared: Path,
    bundle: dict[str, Any],
    probe: dict[str, Any],
    root: Path,
    max_bytes: int,
    lock_fd: int,
    deadline: float,
) -> dict[str, Any]:
    """Advance only after durable source/build/preflight boundaries under one lock."""
    inputs = {
        "base": str(prepared),
        "bundle_sha256": bundle["sha256"],
        "candidate": probe,
        "max_bytes": max_bytes,
    }
    state_path = root / "state.json"
    state = (
        json.loads(
            read_regular(state_path, 256 * 1024**2), object_pairs_hook=unique_object
        )
        if state_path.exists()
        else None
    )
    if state is None:
        if state_path.exists():
            raise ValueError(
                "Invalid saved handoff state; refusing to reset its budget"
            )
        state = {
            "format": "glitnir.lean-handoff-state/v1",
            "inputs": inputs,
            "deadline": deadline,
            "phase": "copying",
        }
        save_receipt(state_path, state)
        save_receipt(root / "bundle.json", bundle)
    if (
        not isinstance(state, dict)
        or state.get("format") != "glitnir.lean-handoff-state/v1"
        or state.get("inputs") != inputs
    ):
        raise ValueError("Handoff inputs changed; reuse the original inputs and limits")
    saved_deadline = state.get("deadline")
    if (
        not isinstance(saved_deadline, (int, float))
        or isinstance(saved_deadline, bool)
        or not math.isfinite(saved_deadline)
        or saved_deadline <= 0
        or saved_deadline > time.time() + probe["timeout_seconds"]
    ):
        raise ValueError(
            "Invalid saved handoff deadline; the budget cannot be extended"
        )

    last_disk_check = 0.0

    def monitor() -> None:
        nonlocal last_disk_check
        if time.time() >= state["deadline"]:
            raise ValueError(
                "Handoff preparation deadline exhausted; saved budget was not reset"
            )

        now = time.monotonic()
        if now - last_disk_check >= 0.25:
            if _size(root) > max_bytes:
                raise ValueError("Handoff exceeds disk budget")
            last_disk_check = time.monotonic()

    project = root / "build/project"
    candidate_path = source_path(probe["path"])
    if state.get("phase") in {"copying", "building"}:
        monitor()
        # An interrupted build is retried only inside its original deadline,
        # from the original base, never by trusting partial generated artifacts.
        shutil.rmtree(root / "build", ignore_errors=True)
        stage_project(
            prepared, bundle["job"]["request"], root / "build", monitor=monitor
        )
        state["phase"] = "building"
        save_receipt(state_path, state)
        monitor()
        build_module(
            project,
            bundle["job"]["request"]["path"],
            state["deadline"],
            probe["memory_mb"],
            root,
            max_bytes,
            lock_fd,
        )
        monitor()
        original = bundle["job"]["request"]
        if (
            context_digest(project / source_path(original["path"]))
            != original["context_sha256"]
        ):
            raise ValueError("Lake build changed original source or dependency context")
        if (
            digest(read_regular(project / source_path(original["path"])))
            != original["source_sha256"]
        ):
            raise ValueError("Lake build changed the lemma source")
        target = project / candidate_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(probe["source"])
        state["probe_request"] = make_request(
            target, probe["environment"], probe["timeout_seconds"], probe["memory_mb"]
        )
        state["probe_request"]["request_id"] = str(
            uuid5(NAMESPACE_URL, canonical(inputs))
        )
        state["files"] = _inventory(project, monitor)
        state["phase"] = "built"
        save_receipt(state_path, state)
    elif state.get("phase") not in {"built", "ready"}:
        raise ValueError("Invalid handoff journal phase")
    if _inventory(project, lambda: None) != state.get("files"):
        raise ValueError(
            "Prepared handoff artifact integrity differs from the saved build"
        )
    expected_probe = {
        **probe,
        "request_id": str(uuid5(NAMESPACE_URL, canonical(inputs))),
        "context_sha256": context_digest(project / candidate_path),
    }
    if state.get("probe_request") != expected_probe:
        raise ValueError("Saved preflight request differs from frozen handoff inputs")
    receipt = _receipt(bundle, probe, expected_probe, project)
    if state["phase"] == "ready":
        if _inventory(root / "preflight/project", lambda: None) != state.get(
            "preflight_files"
        ):
            raise ValueError(
                "Staged preflight artifact integrity differs from the receipt"
            )
        if state.get("receipt") != receipt:
            raise ValueError("Saved handoff receipt differs from frozen inputs")
        return receipt
    monitor()
    shutil.rmtree(root / "preflight", ignore_errors=True)
    if _size(root) + _size(project) > max_bytes:
        raise ValueError(
            "Built handoff leaves insufficient disk budget for preflight copy"
        )
    staged = stage_project(
        project, state["probe_request"], root / "preflight", monitor=monitor
    )
    if _size(root) > max_bytes:
        raise ValueError("Handoff exceeds disk budget")
    monitor()
    inspection = inspect_project(
        staged,
        expected_sha256=probe["source_sha256"],
        timeout_seconds=state["deadline"] - time.time(),
    )
    module = bundle["job"]["result"]["module"]
    if (
        inspection["status"] != "resolved"
        or module not in inspection["setup"]["importArts"]
    ):
        raise ValueError(
            "Worker-style handoff preflight failed: "
            + inspection.get("message", "candidate must import the lemma module")
        )
    monitor()
    state["preflight_files"] = _inventory(root / "preflight/project", monitor)
    if inspection["setup"]["name"] != receipt["preflight"]["module"]:
        raise ValueError("Candidate module must match its source-root path")
    state["receipt"] = receipt
    state["phase"] = "ready"
    save_receipt(state_path, state)
    if _size(root) > max_bytes:
        raise ValueError("Handoff receipt exceeds disk budget")
    return receipt


def _receipt(
    bundle: dict[str, Any],
    probe: dict[str, Any],
    request: dict[str, Any],
    project: Path,
) -> dict[str, Any]:
    """Reconstruct transport bindings from validated inputs, never saved output."""
    return {
        "format": "glitnir.lean-handoff/v1",
        "bundle_sha256": bundle["sha256"],
        "origin": bundle["origin"],
        "job": {
            "id": bundle["job"]["id"],
            "worker": bundle["job"]["worker"],
            "source_sha256": bundle["job"]["request"]["source_sha256"],
        },
        "prepared": str(project),
        "binding": {
            k: request[k]
            for k in (
                "environment",
                "checker_sha256",
                "context_sha256",
                "path",
                "timeout_seconds",
                "memory_mb",
            )
        },
        "source_sha256": probe["source_sha256"],
        "preflight": {
            "status": "resolved",
            "module": probe["path"].removesuffix(".lean").replace("/", "."),
            "import": bundle["job"]["result"]["module"],
        },
        "proof_authority": False,
    }
