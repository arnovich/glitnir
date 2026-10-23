"""Keep a Mac from idle-sleeping while a Lean run is in progress."""

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator

CAFFEINATE = "/usr/bin/caffeinate"
_STOP_SECONDS = 2.0
#: Sleep shorter than this is not reported; the wall clock may be adjusted a
#: little while a run is in progress.
NOTED_SLEEP_SECONDS = 30.0

_lock = threading.Lock()
_holders = 0
_process: "subprocess.Popen[bytes] | None" = None


def _enabled() -> bool:
    """Whether this platform holds an assertion."""
    return sys.platform == "darwin"


@contextlib.contextmanager
def awake() -> Iterator[None]:
    """Hold an idle-sleep assertion for the block, on macOS only.

    A sleeping Mac would otherwise stall a long check, view, evidence or
    decision run: the assertion is held for the whole run, not only while a
    Lean or Lake process runs, since the Python stages between them (such as
    the snapshot capture) take minutes too. Blocks nest, across threads too:
    one caffeinate runs while any block holds the assertion and stops when the
    last one ends. caffeinate also exits by itself when this process does. It
    is started directly, never through the sandbox, and a failure to start it
    is ignored.
    """
    _acquire()
    try:
        yield
    finally:
        _release()


def _acquire() -> None:
    global _holders, _process
    with _lock:
        _holders += 1
        if _holders == 1 and _enabled():
            try:
                _process = subprocess.Popen(
                    [CAFFEINATE, "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                _process = None


def _release() -> None:
    global _holders, _process
    with _lock:
        _holders -= 1
        if _holders == 0 and _process is not None:
            process, _process = _process, None
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


class SleepWatch:
    """Measure how long the computer slept since the watch was started.

    `time.monotonic` stops while the computer sleeps (on macOS and Linux) and
    `time.time` does not, so the difference of their advances is the time
    asleep. That is also why no monotonic deadline fires during sleep.
    """

    def __init__(
        self,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the watch now."""
        self._wall = wall
        self._monotonic = monotonic
        self._wall_start = wall()
        self._monotonic_start = monotonic()

    def slept_seconds(self) -> float:
        """Return the seconds the computer slept since the watch started."""
        wall = self._wall() - self._wall_start
        awake_for = self._monotonic() - self._monotonic_start
        return max(0.0, wall - awake_for)

    def note(self) -> str:
        """Return " (the computer slept for N min)", or "" for no notable sleep."""
        slept = self.slept_seconds()
        if slept < NOTED_SLEEP_SECONDS:
            return ""
        if slept < 120:
            return f" (the computer slept for {round(slept)} s)"
        return f" (the computer slept for {round(slept / 60)} min)"


def noting_sleep(progress: Callable[[str], None]) -> Callable[[str], None]:
    """Return ``progress`` with each message noting the sleep since this call.

    A run that the computer's sleep held up then says so in its progress, so a
    stalled stage is not mistaken for a slow one.
    """
    watch = SleepWatch()

    def report(message: str) -> None:
        progress(message + watch.note())

    return report
