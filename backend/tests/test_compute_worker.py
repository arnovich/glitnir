"""A computation worker invokes the native checker only for matching inputs."""

import threading

from glitnir.compute.protocol import make_request
from glitnir.compute.worker import execute


def test_execute_uses_private_source_and_existing_full_checker(
    compute_project, tmp_path, monkeypatch
):
    source = compute_project / "Example.lean"
    request = make_request(source, "prepared", 60, 4096)
    observed = []

    def check(path, **kwargs):
        observed.append((path, kwargs))
        assert path != source and path.read_text() == request["source"]
        return {
            "status": "checked_snapshot",
            "source_sha256": request["source_sha256"],
            "theorems": [],
        }

    monkeypatch.setattr("glitnir.compute.worker.check_project", check)
    result = execute(compute_project, request, tmp_path / "stage", threading.Event())
    assert result["status"] == "checked_snapshot"
    assert observed[0][1]["expected_sha256"] == request["source_sha256"]
    assert source.read_text() == request["source"]


def test_mismatched_checker_or_changed_context_never_publishes_success(
    compute_project, tmp_path, monkeypatch
):
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    called = []
    monkeypatch.setattr(
        "glitnir.compute.worker.check_project", lambda *a, **k: called.append(True)
    )
    request["checker_sha256"] = "0" * 64
    assert (
        execute(compute_project, request, tmp_path / "bad", threading.Event())["status"]
        == "unavailable"
    )
    assert not called


def test_prepared_inputs_changing_during_check_discard_result(
    compute_project, tmp_path, monkeypatch
):
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)

    def check(*args, **kwargs):
        (compute_project / "Helper.lean").write_text("changed during check")
        return {
            "status": "checked_snapshot",
            "source_sha256": request["source_sha256"],
            "theorems": [],
        }

    monkeypatch.setattr("glitnir.compute.worker.check_project", check)
    result = execute(compute_project, request, tmp_path / "stage", threading.Event())
    assert result["status"] == "unavailable" and not result["theorems"]


def test_live_prepared_worker_reuses_build_and_preserves_admitted_status(
    prepared_lean_project, tmp_path
):
    """Actual sandbox inspection and kernel replay work from the private build."""
    source = prepared_lean_project / "src/Example.lean"
    request = make_request(source, "prepared", 600, 4096)
    stage = tmp_path / "worker-stage"
    result = execute(prepared_lean_project, request, stage, threading.Event())
    assert result["status"] == "checked_snapshot", result
    assert result["theorems"][0]["status"] == "checked"
    source.write_text("import Example.Helper\ntheorem result : False := by sorry\n")
    request = make_request(source, "prepared", 600, 4096)
    result = execute(prepared_lean_project, request, stage, threading.Event())
    assert result["status"] == "checked_snapshot", result
    assert result["theorems"][0]["status"] != "checked"


def test_invalid_claim_is_reported_without_terminating_worker(
    compute_project, tmp_path, monkeypatch
):
    """A malformed job cannot kill the polling service or invoke Lean."""
    from glitnir.compute.client import Client, Settings
    from glitnir.compute.worker import Worker

    client = Client(Settings("https://tasks.example.test", "lab", "prepared", "secret"))
    worker = Worker(client, compute_project, tmp_path / "journal")
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    request["path"] = "lakefile.lean"
    calls = []

    def send(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/claim"):
            return {
                "job": {"id": request["request_id"], "request": request},
                "lease": "a" * 64,
            }
        return {"state": "completed"}

    monkeypatch.setattr(client, "request", send)
    assert worker.run_once(threading.Event())
    assert calls[-1][1].endswith("/complete")
    assert calls[-1][2]["result"]["status"] == "unavailable"


def test_default_worker_accepts_default_editor_memory(
    compute_project, tmp_path, monkeypatch
):
    """Default remote checks fit the worker's advertised resource ceiling."""
    from glitnir.compute.client import Client, Settings
    from glitnir.compute.worker import Worker
    from glitnir.lean.limits import memory_limit_mb

    monkeypatch.delenv("GLITNIR_MEMORY_LIMIT_MB", raising=False)
    monkeypatch.setattr("glitnir.lean.limits._physical_memory_mb", lambda: 65536)
    client = Client(Settings("https://tasks.example.test", "lab", "prepared", "secret"))
    worker = Worker(client, compute_project, tmp_path / "journal")
    request = make_request(
        compute_project / "Example.lean", "prepared", 600, memory_limit_mb()
    )
    assert request["memory_mb"] <= worker.capabilities["max_memory_mb"]


def test_stop_kills_descendant_after_leader_has_exited(tmp_path):
    """A successful build leader cannot leave a writer in the published cache."""
    import os
    import subprocess
    import sys
    import time

    from glitnir.compute.worker import _stop

    marker = tmp_path / "survivor"
    program = (
        "import os,time,pathlib; "
        "child=os.fork(); "
        "os._exit(0) if child else None; "
        "time.sleep(1); pathlib.Path(" + repr(str(marker)) + ').write_text("survived")'
    )
    process = subprocess.Popen([sys.executable, "-c", program], start_new_session=True)
    process.wait(timeout=10)
    try:
        _stop(process)
        time.sleep(1.3)
        assert not marker.exists()
    finally:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass


def test_projects_are_polled_serially_and_rotate_after_work(monkeypatch):
    from glitnir.compute.worker import run_workers

    stop = threading.Event()
    order = []

    class FakeWorker:
        def __init__(self, name):
            self.name = name

        def register(self):
            order.append("register-" + self.name)

        def run_once(self, event):
            order.append(self.name)
            if order.count("b") == 2:
                event.set()
            return True

    run_workers([FakeWorker("a"), FakeWorker("b")], stop)
    assert order == ["register-a", "register-b", "a", "b", "a", "b"]


def test_dynamic_run_once_reports_preparation_then_checking(dynamic_worker_case):
    worker, lake, calls, request = dynamic_worker_case
    assert worker.run_once(threading.Event())
    phases = [body["phase"] for path, body in calls if path.endswith("/heartbeat")]
    assert phases[0] == "preparing" and phases[-1] == "checking"
    completed = [body for path, body in calls if path.endswith("/complete")]
    assert len(completed) == 1
    assert completed[0]["result"]["status"] == "unavailable"
    assert worker.cache.ready(list(worker.recipes.values())) == ["a"]


def test_cancel_during_build_kills_process_and_discards_cache(
    dynamic_worker_case, tmp_path
):
    import time

    worker, lake, calls, request = dynamic_worker_case
    marker = tmp_path / "late-write"
    started = tmp_path / "started"
    lake.write_text(
        lake.read_text() + f'else:\n open({str(started)!r}, "w").write("started")\n'
        f' time.sleep(2)\n open({str(marker)!r}, "w").write("bad")\n'
    )
    stop = threading.Event()

    def cancel_when_building():
        limit = time.monotonic() + 30
        while not started.exists() and time.monotonic() < limit:
            time.sleep(0.02)
        stop.set()

    timer = threading.Thread(target=cancel_when_building)
    timer.start()
    try:
        assert worker.run_once(stop)
        assert started.exists()
        time.sleep(2.3)
        assert not marker.exists()
        assert not any(path.endswith("/complete") for path, _ in calls)
        assert worker.cache.ready(list(worker.recipes.values())) == []
    finally:
        timer.join(timeout=30)


def test_worker_forwards_selected_toolchain_to_checker(
    dynamic_worker_case, monkeypatch
):
    import subprocess

    worker, lake, calls, request = dynamic_worker_case
    monkeypatch.setenv("ELAN_HOME", "/operator/selected-elan")
    seen = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        if kwargs.get("env", {}).get("GLITNIR_EXECUTION_BACKEND") == "local":
            seen.append(kwargs["env"])
        return original(*args, **kwargs)

    monkeypatch.setattr("glitnir.compute.worker.subprocess.Popen", spawn)
    assert worker.run_once(threading.Event())
    assert seen and seen[-1].get("ELAN_HOME") == "/operator/selected-elan"


def test_live_dynamic_worker_preserves_custom_toolchain_location(
    registered_repositories, project_toolchain, tmp_path, monkeypatch
):
    """Run a real pinned build and checker through the supervised worker path."""
    import time
    from dataclasses import replace
    from pathlib import Path

    from glitnir.compute.client import Client, Settings
    from glitnir.compute.environments import EnvironmentCache
    from glitnir.compute.worker import Worker

    recipes, _, _ = registered_repositories
    recipe = replace(recipes[0], max_bytes=50000000)
    monkeypatch.setattr(
        "glitnir.compute.environments._toolchain", lambda: project_toolchain
    )
    monkeypatch.setenv("ELAN_HOME", str(project_toolchain.parents[1]))
    home = tmp_path / "worker-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    client = Client(Settings("https://tasks.example.test", "lab", "a", "secret"))
    request = make_request(Path(recipe.repository) / "Example.lean", "a", 600, 4096)
    completed = []

    def send(method, path, body=None):
        if path.endswith("/claim"):
            return {
                "job": {
                    "id": request["request_id"],
                    "request": request,
                    "deadline": time.time() + 600,
                },
                "lease": "a" * 64,
            }
        if path.endswith("/complete"):
            completed.append(body["result"])
        return {"state": "running"}

    monkeypatch.setattr(client, "request", send)
    worker = Worker(
        client,
        None,
        tmp_path / "journal",
        recipes=[recipe],
        cache=EnvironmentCache(tmp_path / "cache", max_bytes=50000000),
    )
    assert worker.run_once(threading.Event())
    assert len(completed) == 1, completed
    result = completed[0]
    assert result["status"] == "checked_snapshot", result
    assert [(t["name"], t["status"]) for t in result["theorems"]] == [
        ("result", "checked")
    ], result
    assert result.get("withheld") is None, result
