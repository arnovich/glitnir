"""Operational measurements never supply proof admission or saved proof bindings."""

import copy
import threading

from glitnir.compute.client import Client, Settings, result_from_job
from glitnir.compute.protocol import make_request
from glitnir.compute.worker import execute


def test_summary_distinguishes_queue_worker_and_native_replay(compute_telemetry):
    from glitnir.compute.telemetry import describe

    text = describe({"state": "completed", "telemetry": compute_telemetry})
    assert "queue 2.0s" in text
    assert "preparation 3.0s" in text and "checking 5.0s" in text
    assert "replay 1.2s" in text and "environment cache hit" in text
    assert "imports cached" in text and "proven" not in text


def test_missing_invalid_future_or_regressed_metadata(compute_telemetry):
    from glitnir.compute.telemetry import describe

    assert describe({"state": "running", "phase": "checking"}) == "checking"
    for bad in (
        {"format": "future"},
        {
            **compute_telemetry,
            "worker_observations": {"elapsed_ms": True, "status": "checked"},
        },
    ):
        assert (
            describe({"state": "running", "phase": "checking", "telemetry": bad})
            == "checking"
        )
    for observed in ("bad", True, float("nan")):
        bad = {**compute_telemetry, "worker_observed_at": observed}
        assert describe({"state": "completed", "telemetry": bad}) == "completed"
    bad = dict(compute_telemetry)
    del bad["worker_observed_at"]
    assert describe({"state": "completed", "telemetry": bad}) == "completed"
    rollback = copy.deepcopy(compute_telemetry)
    rollback["timestamps"]["leased"] = 999.0
    assert "queue " not in describe({"state": "completed", "telemetry": rollback})


def test_client_progress_timings_never_change_result(
    compute_project, compute_telemetry, monkeypatch
):
    request = make_request(compute_project / "Example.lean", "prepared", 600, 4096)
    report = {
        "status": "checked_snapshot",
        "source_sha256": request["source_sha256"],
        "theorems": [],
    }
    job = {
        "id": request["request_id"],
        "state": "completed",
        "worker": "worker",
        "request": request,
        "result": report,
    }
    expected = result_from_job(job, request)
    client = Client(Settings("https://tasks.example.test", "lab", "prepared", "secret"))
    monkeypatch.setattr(
        client, "request", lambda *args: {**job, "telemetry": compute_telemetry}
    )
    messages = []
    assert client.wait(job["id"], progress=messages.append)["state"] == "completed"
    assert "replay 1.2s" in messages[-1]
    assert result_from_job({**job, "telemetry": compute_telemetry}, request) == expected
    assert "telemetry" not in expected and "telemetry" not in expected["execution"]


def test_execute_observations_preserve_exact_native_result(
    compute_project, tmp_path, monkeypatch
):
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    native = {
        "status": "checked_snapshot",
        "source_sha256": request["source_sha256"],
        "theorems": [],
        "stage_ms": {"compile": 5, "replay": 15, "total": 25},
        "replay": "imports cached",
    }
    monkeypatch.setattr(
        "glitnir.compute.worker.check_project", lambda *args, **kwargs: native
    )
    observations = {}
    result = execute(
        compute_project,
        request,
        tmp_path / "stage",
        threading.Event(),
        observations=observations,
    )
    assert result == native
    assert observations["native_stage_ms"] == native["stage_ms"]
    assert (
        observations["imports_replay"] == "cached" and observations["staging_ms"] >= 0
    )
    assert "environment_cache" not in result and "observations" not in result


def test_cache_reuse_observation_follows_integrity_check(
    registered_repositories, tmp_path
):
    from glitnir.compute.environments import EnvironmentCache

    recipes, run, builds = registered_repositories
    cache = EnvironmentCache(tmp_path / "cache", max_bytes=40000000)
    seen = []
    for _ in range(2):
        with cache.prepare(recipes[0], run, lambda: None):
            seen.append(cache.active_reused)
        assert cache.active_reused is None
    assert seen == [False, True] and len(builds) == 1


def test_worker_negotiates_atomic_observations_and_reports_real_cache_reuse(
    dynamic_worker_case, compute_telemetry, monkeypatch
):
    import json
    from uuid import uuid4

    worker, _, calls, _ = dynamic_worker_case
    original = worker.client.request

    def send(method, path, body=None, **kwargs):
        value = original(method, path, body)
        if path.endswith("/claim"):
            value["job"]["request"] = {
                **value["job"]["request"],
                "request_id": str(uuid4()),
            }
            value["job"]["id"] = value["job"]["request"]["request_id"]
            value["job"]["telemetry"] = compute_telemetry
        if path.endswith("/telemetry"):
            assert kwargs["timeout_seconds"] <= 1
        return value

    monkeypatch.setattr(worker.client, "request", send)
    for _ in range(2):
        assert worker.run_once(threading.Event())
    completions = [body for path, body in calls if path.endswith("/complete")]
    assert [x["observations"]["environment_cache"] for x in completions] == [
        "miss",
        "hit",
    ]
    assert all(x["observations"]["checking_ms"] >= 0 for x in completions)
    assert all("observations" not in x["result"] for x in completions)
    saved = [
        json.loads(p.read_text()) for p in worker.journal.glob("*/completion.json")
    ]
    assert all(value in completions for value in saved) and len(saved) == 2


def test_optional_telemetry_network_failure_does_not_rerun_or_discard_result(
    dynamic_worker_case, compute_telemetry, monkeypatch
):
    worker, _, calls, _ = dynamic_worker_case
    original = worker.client.request

    def send(method, path, body=None, **kwargs):
        if path.endswith("/telemetry"):
            raise OSError("optional metrics unavailable")
        value = original(method, path, body)
        if path.endswith("/claim"):
            value["job"]["telemetry"] = compute_telemetry
        return value

    monkeypatch.setattr(worker.client, "request", send)
    assert worker.run_once(threading.Event())
    done = [body for path, body in calls if path.endswith("/complete")]
    assert len(done) == 1 and done[0]["result"]["status"] == "unavailable"
    assert "observations" in done[0]
    assert len(list(worker.journal.glob("*/request.json"))) == 1


def test_legacy_worker_transport_never_sends_new_fields(dynamic_worker_case):
    worker, _, calls, _ = dynamic_worker_case
    assert worker.run_once(threading.Event())
    assert not any(path.endswith("/telemetry") for path, _ in calls)
    assert all(
        "observations" not in body for path, body in calls if path.endswith("/complete")
    )


def test_optional_sidecar_write_failure_preserves_completed_native_result(
    compute_project, tmp_path, monkeypatch
):
    import json

    from glitnir.compute import __main__ as cli

    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    payload, output, sidecar = (
        tmp_path / name for name in ("request.json", "result.json", "observations.json")
    )
    payload.write_text(json.dumps(request))
    native = {
        "status": "checked_snapshot",
        "source_sha256": request["source_sha256"],
        "theorems": [],
    }
    calls = []

    def execute_once(*args, **kwargs):
        calls.append(args)
        kwargs["observations"].update(staging_ms=1)
        return native

    save = cli.save_receipt

    def save_optional(path, value):
        if path == sidecar:
            raise OSError("optional write failed")
        save(path, value)

    monkeypatch.setattr(cli, "execute", execute_once)
    monkeypatch.setattr(cli, "save_receipt", save_optional)
    assert (
        cli.main(
            [
                "execute",
                "--prepared",
                str(compute_project),
                "--request",
                str(payload),
                "--output",
                str(output),
                "--scratch",
                str(tmp_path / "stage"),
                "--observations",
                str(sidecar),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text()) == native and len(calls) == 1


def test_extreme_or_nonfinite_clock_metadata_is_ignored(compute_telemetry):
    from glitnir.compute.telemetry import describe

    for value in (10**4000, float("nan"), float("inf"), True):
        bad = copy.deepcopy(compute_telemetry)
        bad["timestamps"]["submitted"] = value
        assert (
            describe({"state": "running", "phase": "checking", "telemetry": bad})
            == "checking"
        )


def test_cli_status_summary_and_completion_recovery_preserve_observations(
    compute_project, compute_telemetry, tmp_path, monkeypatch, capsys
):
    import json

    from glitnir.compute.__main__ import main

    monkeypatch.setenv("GLITNIR_TASKVALET_TOKEN", "private-token")
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "format": "glitnir.compute-receipt/v1",
                "url": "https://tasks.example.test",
                "project": "lab",
                "request": request,
            }
        )
    )
    job = {
        "id": request["request_id"],
        "request": request,
        "state": "completed",
        "telemetry": compute_telemetry,
    }
    sent = []

    def send(self, method, path, body=None):
        sent.append((method, path, body))
        return job

    monkeypatch.setattr(Client, "request", send)
    options = [
        "--server",
        "https://tasks.example.test",
        "--project",
        "lab",
        "--environment",
        "prepared",
    ]
    assert main([*options, "status", str(receipt), "--summary"]) == 0
    text = capsys.readouterr().out
    assert "native replay 1.2s" in text and "private-token" not in text
    binding = {"worker_id": "worker", "lease": "a" * 64}
    body = {
        **binding,
        "result": {"status": "unavailable", "theorems": []},
        "observations": compute_telemetry["worker_observations"],
    }
    (tmp_path / "lease.json").write_text(
        json.dumps(
            {
                **binding,
                "url": "https://tasks.example.test",
                "project": "lab",
                "job": {"request": request},
            }
        )
    )
    (tmp_path / "completion.json").write_text(json.dumps(body))
    assert main([*options, "retry-completion", str(tmp_path)]) == 0
    assert sent[-1] == ("POST", f"/jobs/{request['request_id']}/complete", body)
