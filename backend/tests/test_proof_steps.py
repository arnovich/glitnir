"""Editor proof observation lifecycle is explicit, cancellable and read-only."""

import hashlib
import threading

import pytest

from glitnir.notebook.proof_steps import ProofSteps


def test_trace_worker_stays_idle_until_requested_and_cancels(tmp_path, monkeypatch):
    """Cancellation clears observations and blocks overlapping workers."""
    from glitnir.notebook import proof_steps

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := by trivial\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def observe(path, **options):
        calls.append((path, options))
        entered.set()
        release.wait(3)
        return {"status": "ready", "nodes": [{"id": 0}]}

    monkeypatch.setattr(proof_steps, "trace_file", observe)
    service = ProofSteps(source, timeout_ms=42_000)
    try:
        assert service.snapshot() == {"status": "idle", "nodes": []}
        assert not calls
        first = service.start(digest)
        assert entered.wait(2)
        assert first["status"] == "running"
        with pytest.raises(ValueError, match="running"):
            service.start(digest)
        service.cancel()
        assert calls[0][1]["cancel"].is_set()
        assert calls[0][1]["timeout_ms"] == 42_000
    finally:
        release.set()
        service.close()
    result = service.snapshot()
    assert result["id"] == first["id"]
    assert result["status"] == "cancelled"
    assert result["nodes"] == []
    assert not source.with_suffix(".forseti.json").exists()
    with pytest.raises(ValueError, match="closed"):
        service.start(digest)


def test_failed_trace_unblocks_refresh(tmp_path, monkeypatch):
    """Unexpected worker failures reach the user instead of staying busy."""
    from glitnir.notebook import proof_steps

    def fail(*args, **kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(proof_steps, "trace_file", fail)
    service = ProofSteps(tmp_path / "A.lean")
    with pytest.raises(ValueError, match="sha256"):
        service.start("bad")
    service.start("a" * 64)
    service.close()
    assert service.snapshot()["status"] in ("unavailable", "cancelled")


def test_editor_trace_routes_are_explicit_and_do_not_write_metadata(
    tmp_path, monkeypatch
):
    """The preview starts, polls and cancels proof-step observations."""
    import http.client
    import json

    from glitnir.notebook import proof_steps
    from glitnir.notebook.server import make_server
    from glitnir.notebook.source_preview import SourcePreviewService

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := by\n  trivial\n")
    calls = []

    def observe(path, **options):
        calls.append(options)
        return {
            "status": "ready",
            "nodes": [],
            "environment": {"kind": "read_only_live_dependencies"},
        }

    monkeypatch.setattr(proof_steps, "trace_file", observe)
    service = SourcePreviewService(source)
    server = make_server(service, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, method="GET", body=None, headers=True):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=5
        )
        connection.request(
            method,
            path,
            json.dumps(body) if body is not None else None,
            (
                {
                    "X-Glitnir": server.RequestHandlerClass.auth_token,
                    "Content-Type": "application/json",
                }
                if headers
                else {}
            ),
        )
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    try:
        assert request("/api/proof/trace")[1]["status"] == "idle"
        assert not calls
        body = {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "claim": "t"}
        assert request("/api/proof/start", "POST", body, headers=False)[0] == 403
        status, first = request("/api/proof/start", "POST", body)
        assert status == 202
        service.proof_steps._worker.join(timeout=3)
        recorded = request("/api/proof/trace")[1]
        assert recorded["id"] == first["id"]
        assert recorded["status"] == "ready"
        assert "claim" not in calls[0]
        assert request("/api/proof/cancel", "POST", {})[0] == 202
        assert not source.with_suffix(".forseti.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        service.close()
