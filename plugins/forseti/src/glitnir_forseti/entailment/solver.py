"""Run the Z3 worker as a bounded child process and bound what it says.

The worker is untrusted search. This module owns its lifetime: a wall-clock
deadline, cooperative cancellation, a memory cap and an output cap, and on any
stop the whole process group is killed and reaped. Its answers are shaped and
bounded here; whether they mean anything is decided by Lean.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import partial
from typing import Any

from glitnir.lean.environment import bind_to_parent, kill_and_reap
from glitnir.lean.scratch import scratch_directory

#: The worker's address-space cap. Z3's nonlinear search can grow without
#: bound; this turns that into a reported failure instead of host pressure.
WORKER_MEMORY_BYTES = 4 * 1024**3
#: The largest answer the worker may print.
WORKER_OUTPUT_BYTES = 4 * 1024**2


@dataclass(frozen=True, slots=True)
class WorkerStop:
    """Why the worker did not answer.

    ``timeout``, ``cancelled``, ``output``, or ``failed`` when it crashed or
    printed something that is not an answer.
    """

    cause: str
    detail: str = ""


def _limit_memory() -> None:
    # ``resource`` is imported at module level: an import after fork can
    # deadlock on an import lock another thread held at the fork. macOS does
    # not enforce an address-space limit and refuses to set one, so there the
    # worker is bounded by its wall-clock deadline and output cap alone.
    if sys.platform == "darwin":
        return
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))


def _child_setup(parent: int) -> None:
    """Run in the forked child: die with the parent and cap output and memory."""
    bind_to_parent(parent, WORKER_OUTPUT_BYTES)
    _limit_memory()


def run_worker(
    request: dict[str, Any],
    deadline: float,
    cancel: threading.Event | None = None,
) -> dict[str, Any] | WorkerStop:
    """Send one request to a fresh worker and return its answer.

    Args:
        request: The request; its ``timeout_ms`` is set from the deadline.
        deadline: A ``time.monotonic()`` instant the worker may not outlive.
        cancel: Set to stop the worker and report ``cancelled``.

    Returns:
        The worker's answer object, or why there was none.
    """
    remaining = deadline - time.monotonic()
    if cancel is not None and cancel.is_set():
        return WorkerStop("cancelled")
    if remaining <= 0:
        return WorkerStop("timeout")
    # Z3's own timeout ends the search cleanly; the kill below is the backstop.
    body = json.dumps({**request, "timeout_ms": max(1, int(remaining * 1000))})
    with (
        tempfile.TemporaryFile() as stdin,
        tempfile.TemporaryFile() as out,
        scratch_directory("glitnir-forseti-z3-") as owned,
    ):
        scratch = owned / "work"  # empty: the owner marker stays beside it
        scratch.mkdir()
        stdin.write(body.encode())
        stdin.seek(0)
        try:
            # -P keeps the working directory off sys.path, and the worker runs
            # in an empty directory, so no stray module there is imported.
            process = subprocess.Popen(
                [sys.executable, "-P", "-m", "glitnir_forseti.entailment.worker"],
                stdin=stdin,
                stdout=out,
                stderr=subprocess.DEVNULL,
                cwd=scratch,
                start_new_session=True,
                preexec_fn=(
                    partial(_child_setup, os.getpid()) if os.name == "posix" else None
                ),
            )
        except (OSError, subprocess.SubprocessError) as error:
            return WorkerStop("failed", f"the worker did not start: {error}")
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    kill_and_reap(process)
                    return WorkerStop("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    kill_and_reap(process)
                    return WorkerStop("timeout")
                try:
                    process.wait(timeout=min(remaining, 0.05))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            kill_and_reap(process)
            raise
        if out.tell() > WORKER_OUTPUT_BYTES:
            return WorkerStop("output")
        out.seek(0)
        raw = out.read()
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError):
        return WorkerStop("failed", f"exit {process.returncode}, no answer")
    if type(answer) is not dict or type(answer.get("result")) is not str:
        return WorkerStop("failed", "the answer was malformed")
    return answer
