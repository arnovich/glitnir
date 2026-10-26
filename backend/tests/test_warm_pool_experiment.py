"""Experimental reuse preserves request identity and never grants proof authority."""

from uuid import uuid4

import pytest

from experiments.warm_pool.runtime import Limits, Pool


def test_submit_returns_while_worker_is_busy_and_bounds_admission(stub_warm_runtime):
    environment, factory, calls, gate, started = stub_warm_runtime
    with Pool(Limits(workers=1, max_pending=2), session_factory=factory) as pool:
        first = pool.submit(environment, "blocked", client="one")
        assert started.wait(2)
        second = pool.submit(environment, "second", client="two")
        assert not first.future.done() and not second.future.done()
        with pytest.raises(ValueError, match="full"):
            pool.submit(environment, "third", client="one")
        gate.set()
        assert first.future.result(2)["state"] == "completed"
        result = second.future.result(2)
        assert result["client"] == "two" and result["queue_ms"] > 0
        assert result["advisory"]["proof_authority"] is False
    assert [source for _, source, _ in calls] == ["blocked", "second"]


def test_same_request_is_retrieved_without_reexecution(stub_warm_runtime):
    environment, factory, calls, _, _ = stub_warm_runtime
    identity = str(uuid4())
    with Pool(Limits(workers=1), session_factory=factory) as pool:
        first = pool.submit(environment, "source", client="one", identity=identity)
        assert first.future.result(2)["request_id"] == identity
        assert (
            pool.submit(environment, "source", client="one", identity=identity) is first
        )
        with pytest.raises(ValueError, match="different"):
            pool.submit(environment, "changed", client="one", identity=identity)
    assert len(calls) == 1


def test_cancelled_queued_job_never_runs(stub_warm_runtime):
    environment, factory, calls, gate, started = stub_warm_runtime
    with Pool(Limits(workers=1), session_factory=factory) as pool:
        first = pool.submit(environment, "blocked", client="one")
        assert started.wait(2)
        cancelled = pool.submit(environment, "cancelled", client="two")
        cancelled.cancel.set()
        gate.set()
        first.future.result(2)
        assert cancelled.future.result(2)["state"] == "cancelled"
    assert [source for _, source, _ in calls] == ["blocked"]


def test_interrupted_job_is_not_retried_and_next_job_gets_a_new_process(
    stub_warm_runtime,
):
    environment, factory, calls, _, _ = stub_warm_runtime
    with Pool(Limits(workers=1), session_factory=factory) as pool:
        interrupted = pool.submit(environment, "crash", client="one")
        result = interrupted.future.result(2)
        assert result["state"] == "interrupted"
        next_job = pool.submit(environment, "valid", client="two")
        recovered = next_job.future.result(2)
        assert recovered["state"] == "completed"
        assert recovered["generation"] != result["generation"]
        assert recovered["request_id"] != result["request_id"]
    assert [source for _, source, _ in calls] == ["crash", "valid"]


def test_request_count_recycles_process_without_repeating_work(stub_warm_runtime):
    environment, factory, calls, _, _ = stub_warm_runtime
    with Pool(Limits(workers=1, max_requests=2), session_factory=factory) as pool:
        results = [
            pool.submit(environment, str(i), client="one").future.result(2)
            for i in range(3)
        ]
    assert results[0]["generation"] == results[1]["generation"]
    assert results[2]["generation"] != results[1]["generation"]
    assert len(calls) == 3


def test_changed_environment_is_refused_before_elaboration(stub_warm_runtime):
    environment, factory, calls, _, _ = stub_warm_runtime
    environment.changed = True
    with Pool(Limits(workers=1), session_factory=factory) as pool:
        result = pool.submit(environment, "source", client="one").future.result(2)
    assert result["state"] == "interrupted" and "environment" in result["reason"]
    assert calls == []


def test_live_warm_process_reuses_imports_but_not_declarations(warm_environment):
    source = "import PoolBench.Basic\ntheorem candidate : True := by trivial\n"
    with Pool(Limits(workers=1, request_seconds=120)) as pool:
        first = pool.submit(warm_environment, source, client="one").future.result(150)
        assert first["state"] == "completed", first
        assert first["advisory"]["status"] == "elaborated", first
        again = pool.submit(warm_environment, source, client="two").future.result(150)
        assert again["generation"] == first["generation"]
        assert again["advisory"]["status"] == "elaborated", again
        leaked = pool.submit(
            warm_environment,
            "import PoolBench.Basic\ntheorem other : True := candidate\n",
            client="two",
        ).future.result(150)
        assert leaked["advisory"]["status"] == "errors", leaked
        changed = pool.submit(
            warm_environment,
            "import Lean\ntheorem candidate : True := by trivial\n",
            client="one",
        ).future.result(150)
        assert changed["advisory"]["status"] == "errors", changed


def test_owned_disk_counts_hardlinks_once_and_refuses_over_budget(tmp_path):
    from experiments.warm_pool.resources import Budget

    first = tmp_path / "a"
    first.write_bytes(b"x" * 10)
    (tmp_path / "b").hardlink_to(first)
    budget = Budget(tmp_path, disk_bytes=15)
    assert budget.check() == 10
    (tmp_path / "c").write_bytes(b"y" * 6)
    with pytest.raises(ValueError, match="disk"):
        budget.check()
    assert budget.cancel.is_set()


def test_budget_rejects_capture_that_cannot_fit_before_writing(tmp_path):
    from experiments.warm_pool.resources import Budget

    budget = Budget(tmp_path, disk_bytes=15)
    with pytest.raises(ValueError, match="fit"):
        budget.reserve(16)
    assert list(tmp_path.iterdir()) == []


def test_final_validation_checks_each_exact_candidate_independently(tmp_path):
    import hashlib
    import threading
    from types import SimpleNamespace

    from experiments.warm_pool.benchmark import validate

    source = tmp_path / "Candidate.lean"
    source.write_text("import PoolBench.Basic\n")
    environment = SimpleNamespace(source=source, assert_current=lambda: None)
    calls = []

    def checker(path, **kwargs):
        calls.append((path, kwargs))
        kwargs["stage_ms"].update(replay=42)
        return {"status": "rejected", "axioms": ["sorryAx"]}

    candidate = source.read_text() + "theorem candidate : True := by sorry\n"
    result = validate(environment, candidate, threading.Event(), checker=checker)
    assert result["verdict"]["status"] == "rejected"
    assert result["stage_ms"] == {"replay": 42}
    assert result["source_sha256"] == hashlib.sha256(candidate.encode()).hexdigest()
    assert calls[0][1]["block"] == "theorem candidate : True := by sorry\n"
    assert (
        calls[0][1]["expected_sha256"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )


def test_observer_failure_is_an_interruption(monkeypatch):
    import subprocess

    from experiments.warm_pool.runtime import Interrupted
    from experiments.warm_pool.session import process_rss_kb

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 2)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(Interrupted, match="memory observer"):
        process_rss_kb(1)


def test_memory_highwater_recycles_after_a_complete_reply(
    monkeypatch, stub_warm_runtime
):
    from types import SimpleNamespace

    from experiments.warm_pool.runtime import Job
    from experiments.warm_pool.session import LeanSession

    environment = stub_warm_runtime[0]
    session = object.__new__(LeanSession)
    session.closed, session.requests = False, 0
    session.process = SimpleNamespace()
    session.limits, session.peak_rss_kb = Limits(memory_mb=1024), 950 * 1024
    job = Job(str(uuid4()), "one", environment, "source", 0, 1)
    replies = iter(
        [
            {"started": job.identity},
            {
                "id": job.identity,
                "status": "elaborated",
                "errors": [],
                "warnings": [],
                "elapsed_ms": 1,
            },
        ]
    )
    monkeypatch.setattr(session, "_send", lambda *args: None)
    monkeypatch.setattr(session, "_read", lambda *args: next(replies))
    monkeypatch.setattr(session, "close", lambda: setattr(session, "closed", True))
    result = session.check(job)
    assert result["recycle_reason"] == "memory_highwater"
    assert result["status"] == "elaborated" and session.closed and job.started.is_set()


def test_fixture_timeout_stops_its_spawned_child(tmp_path):
    import os
    import sys
    import time
    from pathlib import Path

    from experiments.warm_pool.environment import build
    from experiments.warm_pool.resources import Budget

    marker = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "pathlib.Path('child.pid').write_text(str(child.pid)); time.sleep(60)"
    )
    with Budget(tmp_path) as budget:
        with pytest.raises(ValueError, match="timed out"):
            build(
                [sys.executable, "-c", script],
                root=tmp_path,
                budget=budget,
                env={"PATH": "/usr/bin:/bin"},
                seconds=1,
            )
    assert marker.exists()
    pid = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        state = Path(f"/proc/{pid}/stat")
        try:
            if state.read_text().rsplit(") ", 1)[1].startswith("Z "):
                break  # Killed grandchild awaiting its own parent's reaper.
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    else:
        pytest.fail("timed-out fixture left its child alive")


def test_selecting_a_different_environment_replaces_the_resident_process(
    stub_warm_runtime,
):
    environment, factory, calls, _, _ = stub_warm_runtime
    other = type(environment)()
    other.key = "b" * 64
    with Pool(Limits(workers=1), session_factory=factory) as pool:
        first = pool.submit(environment, "first", client="one").future.result(2)
        second = pool.submit(other, "second", client="two").future.result(2)
    assert first["generation"] != second["generation"]
    assert second["environment_sha256"] == other.key
    assert len(calls) == 2


@pytest.mark.parametrize("started", [False, True])
def test_wait_forwards_resource_cancellation_to_the_original_job(
    stub_warm_runtime, started
):
    import threading
    import time

    from experiments.warm_pool.runtime import Job, wait_for

    job = Job(
        str(uuid4()), "one", stub_warm_runtime[0], "source", 0, time.monotonic() + 60
    )
    stop = threading.Event()
    waiter = threading.Thread(
        target=wait_for, args=(job, stop), kwargs={"started": started}
    )
    waiter.start()
    try:
        stop.set()
        assert job.cancel.wait(1), "budget cancellation was not forwarded"
    finally:
        job.future.set_result({"state": "cancelled"})
        waiter.join(2)
    assert not waiter.is_alive()
