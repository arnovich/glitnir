"""Run a confined command and kill it when the process that started it exits.

macOS has no parent-death signal, so the Seatbelt backend of `sandbox` starts
every Lean process through this program, outside the sandbox. It waits on
kqueue for either the Glitnir process or the command to exit. When Glitnir
exits first, even by SIGKILL, the command is killed; otherwise the command's
exit status is passed on, a death by signal ``N`` as ``128 + N`` like ``elan``.

The command may leave the process group it shares with this program, so the
command is always killed by its process id, which stays valid until this
program reaps it: on SIGTERM, SIGINT or SIGHUP, and on any error of this
program's own, which then exits with `FAILED` so that a caller cannot mistake
it for a verdict of the command. Glitnir stops a confined process by sending
SIGTERM here first. It reads nothing from the standard streams, which the
command inherits.

Usage: ``python -I -S _supervisor.py PARENT_PID COMMAND...``
"""

# kqueue exists only on macOS and the BSDs, but other modules import `FAILED`.
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from typing import Any

#: The exit status when this program, not the command, failed.
FAILED = 125
_STOPPING = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class _Stopped(Exception):
    """Raised in the main thread when a stopping signal arrives."""

    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.number = number


def _stop(number: int, frame: object) -> None:
    raise _Stopped(number)


def _watch(queue: Any, pid: int) -> bool:
    """Ask kqueue to report the process's exit; False if it has already gone."""
    # kqueue exists only on macOS and the BSDs; the check also tells type
    # checkers on other platforms that the rest is unreachable there.
    if sys.platform != "darwin":
        raise OSError("kqueue supervision is macOS only")
    event = select.kevent(
        pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT
    )
    try:
        queue.control([event], 0)
    except ProcessLookupError:
        return False
    return True


def _supervise(parent: int, child: subprocess.Popen[bytes]) -> None:
    """Return when the child exits, killing it first if the parent exits."""
    if sys.platform != "darwin":
        raise OSError("kqueue supervision is macOS only")
    queue = select.kqueue()
    if not _watch(queue, parent) or os.getppid() != parent:
        child.kill()  # the parent exited before it could be watched
    elif _watch(queue, child.pid):
        (event,) = queue.control(None, 1)
        if event.ident == parent:
            child.kill()


def main(arguments: list[str]) -> int:
    """Run the command, returning its exit status or killing it with the parent.

    Args:
        arguments: The parent's process id, then the command and its arguments.

    Returns:
        The command's exit status, 128 plus the signal that ended it or that
        stopped this program, or `FAILED`.
    """
    for number in _STOPPING:
        signal.signal(number, _stop)
    child = None
    try:
        parent, command = int(arguments[0]), arguments[1:]
        child = subprocess.Popen(command)
        _supervise(parent, child)
        status = child.wait()
    except _Stopped as stopped:
        status = -stopped.number
    except BaseException:
        status = FAILED
    finally:
        if child is not None and child.returncode is None:
            child.kill()
            child.wait()
    return 128 - status if status < 0 else status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
