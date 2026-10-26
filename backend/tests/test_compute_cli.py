"""Agent CLI submission returns while the checker job remains queued."""

import json

from glitnir.compute.__main__ import main
from glitnir.compute.client import Client


def test_submit_returns_receipt_without_polling(
    compute_project, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GLITNIR_TASKVALET_TOKEN", "private-token")
    calls = []

    def send(self, method, path, body=None):
        calls.append((method, path))
        return {"id": body["request_id"], "request": body, "state": "queued"}

    monkeypatch.setattr(Client, "request", send)
    receipt = tmp_path / "receipt.json"
    assert (
        main(
            [
                "--server",
                "https://tasks.example.test",
                "--project",
                "lab",
                "--environment",
                "prepared",
                "submit",
                str(compute_project / "Example.lean"),
                "--receipt",
                str(receipt),
            ]
        )
        == 0
    )
    assert calls == [("POST", "/jobs")]
    assert json.loads(capsys.readouterr().out)["state"] == "queued"
    assert "private-token" not in receipt.read_text()


def test_receipt_cannot_redirect_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GLITNIR_TASKVALET_TOKEN", "private-token")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "format": "glitnir.compute-receipt/v1",
                "url": "https://attacker.test",
                "project": "lab",
            }
        )
    )
    calls = []
    monkeypatch.setattr(Client, "request", lambda *args: calls.append(args))
    assert (
        main(
            [
                "--server",
                "https://tasks.example.test",
                "--project",
                "lab",
                "--environment",
                "prepared",
                "status",
                str(receipt),
            ]
        )
        == 2
    )
    assert not calls
    assert "configured server" in capsys.readouterr().err


def test_issued_credentials_and_completion_retry_keep_original_lease(
    tmp_path, monkeypatch, capsys, compute_project
):
    """Recovery republishes exact saved bytes, without starting another check."""
    from glitnir.compute.protocol import make_request

    credential = tmp_path / "credential.json"
    credential.write_text(
        json.dumps(
            {
                "url": "https://tasks.example.test",
                "projects": ["lab"],
                "token": "private-token",
            }
        )
    )
    credential.chmod(0o600)
    request = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    binding = {"worker_id": "original-worker", "lease": "a" * 64}
    completion = {**binding, "result": {"status": "unavailable", "theorems": []}}
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
    (tmp_path / "completion.json").write_text(json.dumps(completion))
    calls = []
    monkeypatch.setattr(
        Client,
        "request",
        lambda self, *args: calls.append(args) or {"state": "completed"},
    )
    assert (
        main(
            [
                "--server",
                "https://tasks.example.test",
                "--project",
                "lab",
                "--environment",
                "prepared",
                "--credential-file",
                str(credential),
                "retry-completion",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert calls == [
        ("POST", "/jobs/" + request["request_id"] + "/complete", completion)
    ]
    assert "private-token" not in capsys.readouterr().out


def test_worker_cli_capacity_covers_default_editor_request(
    compute_project, tmp_path, monkeypatch
):
    """The service command must not override the compatible Worker default."""
    from glitnir.compute.worker import Worker
    from glitnir.lean.limits import memory_limit_mb

    monkeypatch.setenv("GLITNIR_TASKVALET_TOKEN", "private-token")
    monkeypatch.delenv("GLITNIR_MEMORY_LIMIT_MB", raising=False)
    monkeypatch.setattr("glitnir.lean.limits._physical_memory_mb", lambda: 65536)
    capacities = []
    monkeypatch.setattr(
        Worker,
        "run",
        lambda self, stop: capacities.append(self.capabilities["max_memory_mb"]),
    )
    assert (
        main(
            [
                "--server",
                "https://tasks.example.test",
                "--project",
                "lab",
                "--environment",
                "prepared",
                "worker",
                "--prepared",
                str(compute_project),
                "--journal",
                str(tmp_path / "journal"),
            ]
        )
        == 0
    )
    assert capacities and capacities[0] >= memory_limit_mb()


def test_registry_checks_credential_for_every_project(tmp_path, monkeypatch, capsys):
    from glitnir.compute.environments import Recipe
    from glitnir.lean.project_inspection import TOOLCHAIN

    recipes = [
        Recipe(
            p,
            "env",
            "https://example.test/repo.git",
            "a" * 40,
            TOOLCHAIN,
            ("Example",),
            1000,
        )
        for p in ("a", "b")
    ]
    monkeypatch.setattr("glitnir.compute.__main__.load_registry", lambda _: recipes)
    credential = tmp_path / "credential.json"
    credential.write_text(
        json.dumps(
            {"url": "https://tasks.example.test", "projects": ["a"], "token": "secret"}
        )
    )
    credential.chmod(0o600)
    called = []
    monkeypatch.setattr(
        "glitnir.compute.__main__.run_workers",
        lambda workers, stop: called.extend(workers),
    )
    args = [
        "--server",
        "https://tasks.example.test",
        "--credential-file",
        str(credential),
        "worker",
        "--registry",
        str(tmp_path / "registry.toml"),
        "--cache",
        str(tmp_path / "cache"),
        "--journal",
        str(tmp_path / "journal"),
    ]
    assert main(args) == 2
    assert not called
    credential.write_text(
        json.dumps(
            {
                "url": "https://tasks.example.test",
                "projects": ["a", "b"],
                "token": "secret",
            }
        )
    )
    assert main(args) == 0
    assert [w.client.settings.project for w in called] == ["a", "b"]
    assert all(w.capabilities["environments"] == ["env"] for w in called)
