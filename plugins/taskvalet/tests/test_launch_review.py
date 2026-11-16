"""Launch review is source bound, read-only and never treats preflight as admission."""

import hashlib

import pytest


def test_launch_review_binds_request_and_exposes_only_display_fields(monkeypatch):
    from glitnir_taskvalet import launch_review

    calls = []

    def request(server, project, token, method, route, body=None):
        calls.append((method, route, body))
        return dict(
            format="taskvalet.notebook-launch/v1",
            task="001-request",
            source_sha256="a" * 64,
            storage_id="storage",
            context_sha256=hashlib.sha256(b"context").hexdigest(),
            ready=False,
            team="Prepared team",
            team_version=1,
            environment="lean-project",
            max_seconds=300,
            max_workers=2,
            compute_available=1,
            status="not_started",
            blockers=["Bounded host unavailable"],
            private="do not display",
        )

    monkeypatch.setattr("glitnir_taskvalet._request", request)
    payload = dict(
        request=dict(
            server="https://tasks.example",
            project="lab",
            source_sha256="a" * 64,
            storage_id="storage",
            command={"text": "context"},
        ),
        task="001-request",
    )
    result = launch_review(payload, "launch-secret")
    assert result["team"] == "Prepared team"
    assert result["ready"] is False
    assert "private" not in result
    assert calls == [("POST", "/api/v1/projects/lab/notebook-launch/preview", {})]
    payload["task"] = "002-another"
    with pytest.raises(ValueError, match="binding"):
        launch_review(payload, "launch-secret")


def test_unknown_contract_cannot_enable_launch(monkeypatch):
    from glitnir_taskvalet import launch_review

    monkeypatch.setattr(
        "glitnir_taskvalet._request", lambda *args: dict(format="future", ready=True)
    )
    with pytest.raises(ValueError):
        launch_review(
            dict(
                request=dict(
                    server="https://tasks.example",
                    project="lab",
                    source_sha256="a" * 64,
                    storage_id="storage",
                    command={"text": "context"},
                ),
                task="001-request",
            ),
            "secret",
        )
