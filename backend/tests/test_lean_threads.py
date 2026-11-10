"""Operator thread budgets reach native Lean without changing proof inputs."""

import sys
import threading
import time

import pytest

from glitnir.lean import limits, observer, project_inspection


@pytest.mark.parametrize(
    "value, expected", [(None, None), ("1", 1), ("4", 4), ("256", 256)]
)
def test_thread_budget(value, expected, monkeypatch):
    """Unset preserves Lean's default; explicit budgets are bounded integers."""
    monkeypatch.delenv("GLITNIR_LEAN_THREADS", raising=False)
    if value is not None:
        monkeypatch.setenv("GLITNIR_LEAN_THREADS", value)
    assert limits.lean_threads() == expected


@pytest.mark.parametrize(
    "value", ["", "0", "-1", "257", "2.0", " 2", "²", "２", "9" * 5000]
)
def test_invalid_budget_prevents_worker_start(
    value, compute_project, tmp_path, monkeypatch
):
    """Invalid operator settings fail before registration or a lease claim."""
    from glitnir.compute.client import Client, Settings
    from glitnir.compute.worker import Worker

    monkeypatch.setenv("GLITNIR_LEAN_THREADS", value)
    client = Client(Settings("https://tasks.example.test", "lab", "prepared", "secret"))
    monkeypatch.setattr(client, "request", lambda *a, **k: pytest.fail("network call"))
    with pytest.raises(ValueError, match="GLITNIR_LEAN_THREADS"):
        Worker(client, compute_project, tmp_path / "journal")
    assert not (tmp_path / "journal").exists()


@pytest.mark.parametrize("budget", [None, "2"])
@pytest.mark.parametrize("stage", ["observer", "inspection", "lake_version"])
def test_native_command_budget(budget, stage, tmp_path, monkeypatch):
    """Inspection and all observer runs configure Lean, never Lake's CLI."""
    monkeypatch.delenv("GLITNIR_LEAN_THREADS", raising=False)
    if budget is not None:
        monkeypatch.setenv("GLITNIR_LEAN_THREADS", budget)
    commands = []

    def confine(command, **kwargs):
        commands.append(command)
        return [sys.executable, "-c", "print('ok')"]

    module = observer if stage == "observer" else project_inspection
    monkeypatch.setattr(module, "confine", confine)
    if stage == "observer":
        output = observer.run_observer(
            tmp_path,
            ["lean", "--run", "helper.lean"],
            cwd=tmp_path,
            readable=[],
            writable=False,
            cancel=threading.Event(),
            deadline=time.monotonic() + 10,
        )
    else:
        output = project_inspection._run(
            ["--version"],
            tmp_path,
            tmp_path,
            threading.Event(),
            program=tmp_path / "helper.lean" if stage == "inspection" else None,
        )
    assert output.strip() == "ok"
    assert ("-j2" in commands[0]) == (budget == "2" and stage != "lake_version")


@pytest.mark.parametrize("budget", [None, "2"])
def test_worker_preserves_slot_configuration(
    budget, dynamic_worker_case, tmp_path, monkeypatch
):
    """The actual checker supervisor gets private paths and a fixed thread budget."""
    import subprocess

    from glitnir.compute.worker import Worker

    original_worker, _, _, _ = dynamic_worker_case
    monkeypatch.delenv("GLITNIR_LEAN_THREADS", raising=False)
    if budget is not None:
        monkeypatch.setenv("GLITNIR_LEAN_THREADS", budget)
    worker = Worker(
        original_worker.client,
        None,
        tmp_path / "other-journal",
        recipes=list(original_worker.recipes.values()),
        cache=original_worker.cache,
    )
    monkeypatch.setenv("GLITNIR_LEAN_THREADS", "8")
    cache = str(tmp_path / "private-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", cache)
    launched = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        environment = kwargs.get("env", {})
        if environment.get("GLITNIR_EXECUTION_BACKEND") == "local":
            launched.append(environment)
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    assert worker.run_once(threading.Event())
    assert len(launched) == 1
    assert launched[0].get("GLITNIR_LEAN_THREADS") == budget
    assert launched[0]["XDG_CACHE_HOME"] == cache
