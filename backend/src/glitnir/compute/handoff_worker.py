"""Operator-only preparation and bounded serving of one imported Lean context."""

import fcntl
import json
import math
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from glitnir.compute.client import Client, save_receipt
from glitnir.compute.handoff import prepare_handoff
from glitnir.compute.lemma_bundle import unique_object, validate_bundle
from glitnir.compute.preparation_limits import require_build_limits
from glitnir.compute.protocol import digest, source_path
from glitnir.compute.worker import Worker
from glitnir.lean.project import read_regular
from glitnir.lean.project_inspection import inspect_project
from glitnir.lean.project_snapshot import capture


def _directory(root: Path) -> None:
    """Require a private operator-owned session without symlink ancestors."""
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Session directory must not cross a symbolic link")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = root.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("Session directory must be private (chmod 700)")


def _bytes(root: Path) -> int:
    """Count regular files without following the snapshot/cache's symbolic links."""
    total, count = 0, 0

    def failed(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    for parent, directories, files in os.walk(root, onerror=failed):
        count += len(directories) + len(files)
        if count > 500000:
            raise ValueError("Session exceeds file-count budget")
        for name in files:
            try:
                info = (Path(parent) / name).lstat()
            except FileNotFoundError:
                continue  # Atomic cache/journal replacement during monitoring.
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


@contextmanager
def _runtime(root: Path) -> Iterator[None]:
    """Keep worker caches and temporary writes inside this operator process's root.

    This is an operator entry point, not a thread-safe model-facing library API.
    ELAN_HOME/PATH remain explicit host configuration and are never bundle input.
    """
    keys = {key: os.environ.get(key) for key in ("HOME", "TMPDIR", "ELAN_HOME")}
    previous = tempfile.tempdir
    for name in ("home", "tmp"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    os.environ.update(
        HOME=str(root / "home"),
        TMPDIR=str(root / "tmp"),
        ELAN_HOME=keys["ELAN_HOME"] or str(Path.home() / ".elan"),
    )
    tempfile.tempdir = str(root / "tmp")
    try:
        yield
    finally:
        tempfile.tempdir = previous
        for key, value in keys.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def capture_preflight(
    receipt: dict[str, Any],
    root: Path,
    stop: threading.Event,
    deadline: float,
    max_bytes: int,
) -> None:
    """Actually capture the staged candidate under the worker's OS identity.

    No elaboration or kernel replay occurs. This checks directory descriptor
    permissions and full toolchain/artifact readability, not merely Lake metadata.
    The temporary private store is removed, so it does not warm the worker cache.
    """
    source = (
        root / "preparation/preflight/project" / source_path(receipt["binding"]["path"])
    )
    inspection = inspect_project(
        source,
        expected_sha256=receipt["source_sha256"],
        timeout_seconds=max(0.01, deadline - time.time()),
    )
    if (
        inspection["status"] != "resolved"
        or inspection["setup"]["name"] != receipt["preflight"]["module"]
        or receipt["preflight"]["import"] not in inspection["setup"]["importArts"]
    ):
        raise ValueError("Handoff worker capture preflight could not resolve imports")
    snapshot = root / "capture"
    shutil.rmtree(snapshot, ignore_errors=True)
    try:
        capture(
            snapshot,
            source,
            inspection["setup"],
            Path(inspection["toolchain_root"]),
            root / "preparation/preflight/project",
            stop,
            configuration={
                item["path"]: item["sha256"]
                for item in inspection["resolution"]["inputs"]
            },
        )
        if _bytes(root) > max_bytes:
            raise ValueError("Handoff capture exceeds session disk budget")
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


def serve_handoff(
    client: Client,
    prepared: Path,
    bundle: dict[str, Any],
    candidate: str,
    directory: Path,
    *,
    candidate_path: str,
    timeout_seconds: int,
    memory_mb: int,
    max_bytes: int,
    lifetime_seconds: int,
    stop: threading.Event,
    on_ready: Callable[[dict[str, Any]], None],
) -> None:
    """Prepare, capture, register and serve one explicit destination and context.

    Reusing the same private directory preserves its absolute session deadline.
    No credential enrollment, job submission, finding publication or acceptance
    occurs. The host must supply the hard lifetime, memory/pid and disk boundaries.
    """
    bundle = validate_bundle(bundle)
    settings = client.settings
    if (
        bundle["origin"]["server"].rstrip("/") != settings.url.rstrip("/")
        or bundle["origin"]["project"] != settings.project
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", settings.project)
    ):
        raise ValueError(
            "Handoff bundle destination differs from the operator destination"
        )
    if settings.environment == bundle["job"]["request"]["environment"]:
        raise ValueError("Use a distinct environment for the derived handoff context")
    if (
        type(lifetime_seconds) is not int
        or not 1 <= timeout_seconds <= lifetime_seconds <= 86400
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= 1024**4
    ):
        raise ValueError("Invalid handoff session limits")
    require_build_limits(memory_mb)
    root = directory.absolute()
    base = prepared.absolute()
    if root.is_relative_to(base) or base.is_relative_to(root):
        raise ValueError("Session must be separate from the prepared base")
    _directory(root)
    inputs = {
        "server": settings.url.rstrip("/"),
        "project": settings.project,
        "environment": settings.environment,
        "base": str(base),
        "bundle_sha256": bundle["sha256"],
        "source_sha256": digest(candidate.encode()),
        "path": candidate_path,
        "timeout_seconds": timeout_seconds,
        "memory_mb": memory_mb,
        "max_bytes": max_bytes,
        "lifetime_seconds": lifetime_seconds,
    }
    with (root / "session.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another worker uses this handoff session") from error
        state_path = root / "session.json"
        if not state_path.exists() and any(
            path.name != "session.lock" for path in root.iterdir()
        ):
            raise ValueError("Missing session journal in an existing session directory")
        # Ready files describe this live process only, never a previous incarnation.
        profile_path, ready_path = root / "compute-profile.json", root / "ready.json"
        for path in (profile_path, ready_path):
            path.unlink(missing_ok=True)
        if state_path.exists():
            state = json.loads(
                read_regular(state_path, 65536), object_pairs_hook=unique_object
            )
            if (
                not isinstance(state, dict)
                or state.get("format") != "glitnir.handoff-session/v1"
                or state.get("inputs") != inputs
            ):
                raise ValueError("Handoff session inputs changed")
        else:
            state = {
                "format": "glitnir.handoff-session/v1",
                "inputs": inputs,
                "deadline": time.time() + lifetime_seconds,
            }
            save_receipt(state_path, state)
        saved_deadline = state.get("deadline")
        if not isinstance(saved_deadline, (int, float)) or isinstance(
            saved_deadline, bool
        ):
            raise ValueError("Invalid handoff session deadline")
        deadline = float(saved_deadline)
        if (
            not math.isfinite(deadline)
            or not 0 < deadline <= time.time() + lifetime_seconds
        ):
            raise ValueError("Invalid handoff session deadline")
        if deadline <= time.time():
            raise ValueError("Handoff session deadline exhausted; budget was not reset")
        failures: list[Exception] = []
        finished = threading.Event()
        monotonic_deadline = time.monotonic() + deadline - time.time()

        def check() -> None:
            if failures:
                raise failures[0]
            if (
                stop.is_set()
                or time.time() >= deadline
                or time.monotonic() >= monotonic_deadline
            ):
                raise ValueError("Handoff session stopped or deadline exhausted")
            if _bytes(root) > max_bytes:
                raise ValueError("Handoff session exceeds disk budget")

        def monitor() -> None:
            while not finished.wait(0.25):
                if stop.is_set():
                    return
                if time.time() >= deadline or time.monotonic() >= monotonic_deadline:
                    stop.set()
                    return
                try:
                    if _bytes(root) > max_bytes:
                        raise ValueError("Handoff session exceeds disk budget")
                except (OSError, ValueError) as error:
                    failures.append(error)
                    stop.set()
                    return

        watcher = threading.Thread(
            target=monitor, daemon=True, name="glitnir-handoff-monitor"
        )
        watching = False
        try:
            with _runtime(root):
                check()
                handoff = prepare_handoff(
                    base,
                    bundle,
                    candidate,
                    root / "preparation",
                    environment=settings.environment,
                    candidate_path=candidate_path,
                    timeout_seconds=timeout_seconds,
                    memory_mb=memory_mb,
                    max_bytes=max_bytes,
                )
                check()
                capture_preflight(handoff, root, stop, deadline, max_bytes)
                check()
                # Preparation/inspection invoke Python preexec limit callbacks.
                # Keep this process single-threaded until those launches finish.
                watcher.start()
                watching = True
                worker = Worker(
                    client,
                    Path(handoff["prepared"]),
                    root / "worker",
                    max_seconds=timeout_seconds,
                    max_memory_mb=memory_mb,
                )
                worker.register()
                check()
                profile = {
                    "format": "taskvalet.compute-profile/v1",
                    "kind": "glitnir.check/v1",
                    "server": settings.url.rstrip("/"),
                    "project": settings.project,
                    "binding": handoff["binding"],
                }
                ready = {
                    "format": "glitnir.handoff-worker/v1",
                    "deadline": deadline,
                    "worker_id": worker.identity,
                    "handoff": handoff,
                    "profile": profile,
                    "proof_authority": False,
                }
                save_receipt(profile_path, profile)
                save_receipt(ready_path, ready)
                on_ready(ready)
                while not stop.is_set():
                    if (
                        time.time() >= deadline
                        or time.monotonic() >= monotonic_deadline
                    ):
                        stop.set()
                        break
                    if not worker.run_once(stop):
                        stop.wait(2)
                if failures:
                    raise failures[0]
        finally:
            finished.set()
            if watching:
                watcher.join()
            for path in (profile_path, ready_path):
                path.unlink(missing_ok=True)
