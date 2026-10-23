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
#: Sleep shorter than this is not reported. A maintenance DarkWake slice lasts
#: about 10 s and a stage's own time varies by more than that, so only sleep
#: of a minute or more explains a slow stage.
NOTED_SLEEP_SECONDS = 60.0

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
    """Count one more holder, starting caffeinate for the first.

    The holder is counted only once the start has been tried, so a start
    that raises anything at all counts nothing: a later run starts again.
    """
    global _holders, _process
    with _lock:
        if _holders == 0 and _enabled():
            try:
                _process = subprocess.Popen(
                    [CAFFEINATE, "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:  # A failure to start is ignored.
                _process = None
        _holders += 1


def _release() -> None:
    """Count one holder less, stopping caffeinate after the last one.

    It is stopped outside the lock, so a run starting meanwhile never waits
    for it; such a run starts its own.
    """
    global _holders, _process
    process = None
    with _lock:
        _holders -= 1
        if _holders == 0:
            process, _process = _process, None
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


def _with_sleep() -> Callable[[], float] | None:
    """Return a monotonic clock that keeps counting while the computer sleeps.

    That is `CLOCK_BOOTTIME` on Linux and `CLOCK_MONOTONIC` on macOS, where
    Python's `time.monotonic` is the uptime clock instead. Neither moves when
    the wall clock is set or stepped (by NTP, say). None elsewhere.
    """
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        clock = time.CLOCK_BOOTTIME
    elif sys.platform == "darwin" and hasattr(time, "CLOCK_MONOTONIC"):
        clock = time.CLOCK_MONOTONIC
    else:
        return None
    return lambda: time.clock_gettime(clock)


class SleepWatch:
    """Measure how long the computer slept since the watch was started.

    `time.monotonic` stops while the computer sleeps (on macOS and Linux),
    which is also why no deadline fires during sleep, and the clock of
    `_with_sleep` does not; the difference of their advances is the time
    asleep. The wall clock is never used, so setting it is no sleep. Where no
    such clock exists, no sleep is measured.
    """

    def __init__(
        self,
        with_sleep: Callable[[], float] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the watch now; ``with_sleep`` defaults to `_with_sleep`."""
        self._with_sleep = with_sleep or _with_sleep()
        self._monotonic = monotonic
        self._monotonic_start = monotonic()
        self._with_sleep_start = (
            self._with_sleep() if self._with_sleep is not None else 0.0
        )

    def slept_seconds(self) -> float:
        """Return the seconds the computer slept since the watch started."""
        if self._with_sleep is None:
            return 0.0
        total = self._with_sleep() - self._with_sleep_start
        awake_for = self._monotonic() - self._monotonic_start
        return max(0.0, total - awake_for)

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
