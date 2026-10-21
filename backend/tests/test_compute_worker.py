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
