"""Run bounded Lean under the sandbox with only explicit read roots."""

import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from glitnir.lean.awake import awake
from glitnir.lean.environment import bind_to_parent, kill_and_reap
from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.sandbox import confine

OUTPUT_BYTES = 16 * 1024**2
#: What `run_observer` raises when a run reaches its own deadline.
TIMED_OUT = "Project check timed out."
MEMORY_EXHAUSTED = (
    "Lean reached its memory limit of {limit} MB. The limit includes the library "
    "files Lean loads (about 4–5 GB with Mathlib) as well as its working memory. "
    "Raise the glitnir.memoryLimitMb setting and try again."
)
MODULE_BYTES = 256 * 1024**2
STAGE_BYTES = 512 * 1024**2
STAGE_ENTRIES = 4096


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
