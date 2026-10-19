"""Keep a Mac from idle-sleeping while Lean or Lake runs."""

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator

CAFFEINATE = "/usr/bin/caffeinate"
_STOP_SECONDS = 2.0


def _enabled() -> bool:
    """Whether this platform holds an assertion."""
    return sys.platform == "darwin"


@contextlib.contextmanager
def awake() -> Iterator[None]:
    """Hold an idle-sleep assertion for the block, on macOS only.

    A sleeping Mac would otherwise stall a long Lean or Lake run. caffeinate
    also exits by itself when this process does. It is started directly, never
    through the sandbox, and a failure to start it is ignored.
    """
    process = None
    if _enabled():
        try:
            process = subprocess.Popen(
                [CAFFEINATE, "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            process = None
    try:
        yield
    finally:
        if process is not None:
            _stop(process)


def _stop(process: "subprocess.Popen[bytes]") -> None:
    """End caffeinate, escalating to a kill; never raise."""
    try:
        process.terminate()
        try:
            process.wait(timeout=_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_STOP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass
