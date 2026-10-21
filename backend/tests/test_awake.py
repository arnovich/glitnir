"""Tests for holding an idle-sleep assertion while Lean or Lake runs."""

import os
import subprocess
from typing import Any

import pytest

from glitnir.lean import awake as awake_module


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
