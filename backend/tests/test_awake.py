"""Tests for holding an idle-sleep assertion while a Lean run is in progress."""

import os
import subprocess
import sys
import threading
from typing import Any

import pytest

from glitnir.lean import awake as awake_module

#: The real class, which a test replaces in the module with clocks it controls.
SleepWatch = awake_module.SleepWatch


class _Process:
    def __init__(self, *, stubborn: bool = False) -> None:
        self.stubborn = stubborn
        self.calls: list[str] = []

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        if self.stubborn and "kill" not in self.calls:
            raise subprocess.TimeoutExpired("caffeinate", timeout or 0)
        return 0


def test_awake_holds_caffeinate_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[tuple[list[str], dict[str, Any]]] = []
    process = _Process()

    def popen(command: list[str], **options: Any) -> _Process:
        launched.append((command, options))
        return process

    monkeypatch.setattr(awake_module, "_enabled", lambda: True)
    monkeypatch.setattr(awake_module.subprocess, "Popen", popen)
    with awake_module.awake():
        assert process.calls == []
    assert launched == [
        (
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]
    assert process.calls == ["terminate", "wait"]


def test_awake_does_nothing_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    def popen(*args: Any, **options: Any) -> None:
        raise AssertionError("caffeinate must not start")

    monkeypatch.setattr(awake_module, "_enabled", lambda: False)
    monkeypatch.setattr(awake_module.subprocess, "Popen", popen)
    with awake_module.awake():
        pass


def test_awake_ignores_a_failed_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def popen(*args: Any, **options: Any) -> None:
        raise OSError("no caffeinate")

    monkeypatch.setattr(awake_module, "_enabled", lambda: True)
    monkeypatch.setattr(awake_module.subprocess, "Popen", popen)
    ran = []
    with awake_module.awake():
        ran.append(True)
    assert ran == [True]


def test_awake_kills_a_caffeinate_that_will_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(stubborn=True)
    monkeypatch.setattr(awake_module, "_enabled", lambda: True)
    monkeypatch.setattr(awake_module.subprocess, "Popen", lambda *a, **k: process)
    with awake_module.awake():
        pass
    assert process.calls == ["terminate", "wait", "kill", "wait"]


def test_nested_and_concurrent_runs_share_one_caffeinate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run holding the assertion and the Lean runs inside it start one process.

    It stops only when the last holder, in any thread, lets go.
    """
    processes: list[_Process] = []

    def popen(*args: Any, **options: Any) -> _Process:
        processes.append(_Process())
        return processes[-1]

    monkeypatch.setattr(awake_module, "_enabled", lambda: True)
    monkeypatch.setattr(awake_module.subprocess, "Popen", popen)
    inside, leave = threading.Event(), threading.Event()

    def other_run() -> None:
        with awake_module.awake():
            inside.set()
            assert leave.wait(10)

    with awake_module.awake():
        with awake_module.awake():
            worker = threading.Thread(target=other_run)
            worker.start()
            assert inside.wait(10)
        assert processes[0].calls == []
    assert processes[0].calls == []
    leave.set()
    worker.join(10)
    assert len(processes) == 1
    assert processes[0].calls == ["terminate", "wait"]
    with awake_module.awake():
        assert len(processes) == 2


def test_a_failed_start_is_not_retried_by_nested_holders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[int] = []

    def popen(*args: Any, **options: Any) -> None:
        starts.append(1)
        raise OSError("no caffeinate")

    monkeypatch.setattr(awake_module, "_enabled", lambda: True)
    monkeypatch.setattr(awake_module.subprocess, "Popen", popen)
    with awake_module.awake():
        with awake_module.awake():
            pass
    assert starts == [1]


def test_a_start_failing_in_any_way_counts_no_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that raises something other than OSError leaks no hold.

    Otherwise the count never returns to zero, and no later run would start
    caffeinate again.
    """
    attempts: list[int] = []

    def popen(*args: Any, **options: Any) -> _Process:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("cannot fork")
        return _Process()

    monkeypatch.setattr(awake_module, "_enabled", lambda: True)
    monkeypatch.setattr(awake_module.subprocess, "Popen", popen)
    with awake_module.awake():
        pass
    assert awake_module._holders == 0
    with awake_module.awake():
        assert awake_module._process is not None
    assert attempts == [1, 1]
    assert awake_module._holders == 0 and awake_module._process is None


class _Clocks:
    """A monotonic clock that stops while "asleep" and one that does not."""

    def __init__(self) -> None:
        self.monotonic_now = 1000.0
        self.with_sleep_now = 5000.0

    def run(self, seconds: float) -> None:
        self.monotonic_now += seconds
        self.with_sleep_now += seconds

    def sleep(self, seconds: float) -> None:
        self.with_sleep_now += seconds

    def watch(self) -> SleepWatch:
        return SleepWatch(
            with_sleep=lambda: self.with_sleep_now,
            monotonic=lambda: self.monotonic_now,
        )


@pytest.mark.parametrize(
    "slept, note",
    [
        (0, ""),
        (45, ""),
        (59, ""),
        (90, " (the computer slept for 90 s)"),
        (720, " (the computer slept for 12 min)"),
    ],
)
def test_sleep_is_the_time_the_monotonic_clock_missed(slept: float, note: str) -> None:
    clocks = _Clocks()
    watch = clocks.watch()
    clocks.run(300)
    clocks.sleep(slept)
    clocks.run(5)
    assert watch.slept_seconds() == pytest.approx(slept)
    assert watch.note() == note


@pytest.mark.parametrize("step", [3600.0, -3600.0])
def test_setting_the_wall_clock_is_no_sleep(
    monkeypatch: pytest.MonkeyPatch, step: float
) -> None:
    """An NTP step or a manual change moves neither clock the watch reads."""
    clocks = _Clocks()
    watch = clocks.watch()
    stepped = awake_module.time.time() + step
    monkeypatch.setattr(awake_module.time, "time", lambda: stepped)
    clocks.run(60)
    assert watch.slept_seconds() == 0.0
    assert watch.note() == ""


def test_the_real_clocks_count_no_sleep_over_a_moment() -> None:
    """On macOS and Linux the watch reads two real clocks that agree awake."""
    watch = SleepWatch()
    assert 0.0 <= watch.slept_seconds() < 1.0
    if sys.platform in ("darwin", "linux"):
        assert awake_module._with_sleep() is not None


def test_without_a_sleep_counting_clock_no_sleep_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(awake_module, "_with_sleep", lambda: None)
    watch = SleepWatch()
    assert watch.slept_seconds() == 0.0 and watch.note() == ""


def test_progress_notes_the_sleep_since_the_run_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clocks = _Clocks()
    monkeypatch.setattr(awake_module, "SleepWatch", lambda: clocks.watch())
    messages: list[str] = []
    progress = awake_module.noting_sleep(messages.append)
    progress("Resolving project environment")
    clocks.sleep(12 * 60)
    progress("Preparing isolated environment")
    clocks.run(10)
    progress("Checking saved file")
    assert messages == [
        "Resolving project environment",
        "Preparing isolated environment (the computer slept for 12 min)",
        "Checking saved file (the computer slept for 12 min)",
    ]
