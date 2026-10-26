"""Lean process limits and lifetime shared by every Lean adapter.

The axiom allowlist, resource ceilings and the helpers that bind a Lean child
to its parent and reap it live here, apart from any particular Lean release
or project environment.

Nothing here decides anything.  It bounds and cleans up an untrusted process.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Callable

from glitnir.lean._supervisor import FAILED

#: The exit status of the sandbox supervisor when it, not the command, failed.
SUPERVISOR_FAILED: int = FAILED

LEAN_AXIOM_ALLOWLIST = ("Classical.choice", "Quot.sound", "propext")

MAX_TIMEOUT_MS = 1_800_000
MAX_HEARTBEATS = 10_000_000
#: The axiom report writes the canonical entry of every constant a module
#: declares, and large generated definitions (such as rational tables) make
#: that output grow quickly, so the cap leaves generous headroom.
MAX_OUTPUT_BYTES = 4_194_304
#: The largest saved notebook or source file any layer reads: 16 MiB.
MAX_SOURCE_BYTES = 16_777_216

_PR_SET_PDEATHSIG = 1
#: How long the macOS sandbox supervisor may take to kill and reap its command.
_SUPERVISOR_GRACE_SECONDS = 5


def _linux_prctl() -> Callable[..., int] | None:
    """Resolve prctl in the parent, so a forked child only has to call it."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None


_PRCTL = _linux_prctl()


def kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Terminate a Lean process group and wait for it, leaving no child.

    Args:
        process: The Lean process started in its own session.

    On macOS a confined process is the sandbox's supervisor, and the Lean
    process under it may have left the group, so the supervisor is first asked
    to kill it by process id. The group, whose id is the process's own since it
    leads its session, is then killed whether or not its leader is still there.
    """
    if sys.platform == "darwin":
        process.terminate()
        try:
            process.wait(timeout=_SUPERVISOR_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()
    process.wait()


def set_output_file_limit(max_output_bytes: int) -> None:
    """Cap the child's writable output in the child itself.

    Args:
        max_output_bytes: The permitted output size in bytes.
    """
    import resource

    ceiling = max_output_bytes + 1
    resource.setrlimit(resource.RLIMIT_FSIZE, (ceiling, ceiling))


def bind_to_parent(parent_pid: int, max_output_bytes: int | None = None) -> None:
    """Child setup: die with the spawning process, optionally capping output.

    Every Lean child runs in its own session so a timeout can kill its whole
    group, which also means killing the host's group never reaches it. After a
    SIGKILL the host runs no cleanup, so on Linux the kernel is asked to
    SIGKILL the child when its parent dies. Linux delivers that signal when
    the spawning *thread* exits, so spawn from a thread that outlives the
    child. Elsewhere this only caps output; on macOS, when Lean is confined, the
    sandbox's supervisor kills it instead, and unconfined nothing does (see
    ``sandbox`` and ``README.md``).

    Args:
        parent_pid: ``os.getpid()`` of the spawning process, taken before fork.
        max_output_bytes: The permitted output size in bytes, or None.

    Raises:
        OSError: If the kernel refuses the parent-death signal.
    """
    if max_output_bytes is not None:
        set_output_file_limit(max_output_bytes)
    if _PRCTL is None:
        return
    if _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot set the parent-death signal")
    if os.getppid() != parent_pid:
        os._exit(1)
