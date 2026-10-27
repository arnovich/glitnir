"""Async receipts and exact result bindings do not trust a successful HTTP alone."""

import json
import threading
from io import BytesIO
from urllib.error import HTTPError

import pytest

from glitnir.compute.client import Client, Settings, result_from_job
from glitnir.compute.protocol import make_request


@pytest.mark.parametrize("method,body", [("GET", None), ("POST", {})])
def test_transport_identifies_glitnir_to_api_gateways(method, body, monkeypatch):
    """Gateways rejecting generic Python clients must receive a product identity."""
    client = Client(Settings("https://tasks.example.test", "lab", "prepared", "secret"))

    def gateway(request, timeout):
        if request.get_header("User-agent") != "Glitnir":
            raise HTTPError(request.full_url, 403, "Client blocked", {}, None)
        assert request.get_header("Authorization") == "Bearer secret"
        assert request.get_method() == method
        assert timeout == 10
        return BytesIO(b'{"state": "queued"}')

    monkeypatch.setattr(client._opener, "open", gateway)
    assert client.request(method, "/jobs", body) == {"state": "queued"}


def test_submit_persists_request_before_network_and_retries_exactly(
    compute_project, tmp_path, monkeypatch
):
    client = Client(Settings("https://tasks.example.test", "lab", "prepared", "secret"))
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    receipt = tmp_path / "receipt.json"
    sent = []

    def send(method, path, body=None):
        assert json.loads(receipt.read_text())["request"] == request
        assert "secret" not in receipt.read_text()
        sent.append(body)
        if len(sent) == 1:
            raise OSError("connection lost after submit")
        return {"id": request["request_id"], "state": "queued", "request": request}

    monkeypatch.setattr(client, "request", send)
    with pytest.raises(OSError):
        client.submit(request, receipt)
    assert client.submit(request, receipt)["state"] == "queued"
    assert sent == [request, request]
    assert receipt.stat().st_mode & 0o077 == 0


def test_result_requires_exact_snapshot_and_completed_state(compute_project):
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    job = {
        "id": request["request_id"],
        "worker": "worker-id",
        "state": "completed",
        "request": request,
        "result": {
            "status": "checked_snapshot",
            "source_sha256": request["source_sha256"],
            "theorems": [],
        },
    }
    assert result_from_job(job, request)["status"] == "checked_snapshot"
    for bad in [
        {**job, "state": "cancelled"},
        {**job, "request": {**request, "context_sha256": "0" * 64}},
        {**job, "result": {**job["result"], "source_sha256": "0" * 64}},
    ]:
        with pytest.raises(ValueError):
            result_from_job(bad, request)


def test_cancelled_wait_sends_cancellation_without_waiting_for_lean(monkeypatch):
    client = Client(Settings("https://tasks.example.test", "lab", "prepared", "secret"))
    cancel = threading.Event()
    cancel.set()
    calls = []
    monkeypatch.setattr(
        client, "request", lambda *args: calls.append(args) or {"state": "cancelled"}
    )
    with pytest.raises(ValueError, match="cancelled"):
        client.wait("job", cancel=cancel)
    assert calls == [("POST", "/jobs/job/cancel", {})]


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:password@example.test",
        "https://example.test/path",
        "https://example.test?token=x",
    ],
)
def test_configuration_refuses_credential_leaks_and_plain_remote_http(url):
    with pytest.raises(ValueError):
        Settings(url, "lab", "prepared", "secret")


def test_dependency_change_while_waiting_rejects_result(compute_project, monkeypatch):
    """A remote completion cannot become current after a local context edit."""
    from glitnir.compute.client import check_remote

    monkeypatch.setenv("GLITNIR_EXECUTION_BACKEND", "taskvalet")
    monkeypatch.setenv("GLITNIR_TASKVALET_URL", "https://tasks.example.test")
    monkeypatch.setenv("GLITNIR_TASKVALET_PROJECT", "lab")
    monkeypatch.setenv("GLITNIR_TASKVALET_ENVIRONMENT", "prepared")
    monkeypatch.setenv("GLITNIR_TASKVALET_TOKEN", "secret")
    source = compute_project / "Example.lean"
    request = make_request(source, "prepared", 600, 8192)

    def submit(self, request, receipt):
        from glitnir.compute.client import save_receipt

        save_receipt(receipt, {"request": request})
        return {"id": request["request_id"]}

    def wait(*args, **kwargs):
        (compute_project / "Helper.lean").write_text("changed dependency")
        return {"state": "completed"}

    monkeypatch.setattr(Client, "submit", submit)
    monkeypatch.setattr(Client, "wait", wait)
    with pytest.raises(ValueError, match="context changed"):
        check_remote(
            source,
            expected_sha256=request["source_sha256"],
            cancel=threading.Event(),
            progress=lambda _: None,
        )
