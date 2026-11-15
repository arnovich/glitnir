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
            scope_id="08942b8e-3657-4531-b42f-dbe6989b936b",
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
    assert calls == [
        ("GET", "/api/v1/projects/lab/notebook-launch", None),
        ("POST", "/api/v1/projects/lab/notebook-launch/preview", {}),
    ]
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


def test_launch_control_recovers_without_posting_and_rejects_changed_scope(monkeypatch):
    from glitnir_taskvalet import launch_control

    calls = []
    scope = "08942b8e-3657-4531-b42f-dbe6989b936b"
    preview = "f34734b0-7e3d-4726-a329-122a74f0dab8"
    response = dict(
        format="taskvalet.notebook-launch/v1",
        task="001-request",
        source_sha256="a" * 64,
        storage_id="storage",
        context_sha256=hashlib.sha256(b"context").hexdigest(),
        scope_id=scope,
        status="unknown",
        cleanup="unknown",
        campaign_status="running",
        campaign_id=preview,
        preview_id=preview,
        can_stop=True,
        exposure_micro_usd=100,
    )

    def request(server, project, token, method, route, body=None):
        calls.append((method, route, body))
        return response

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
        operation="status",
        launch={"scope_id": scope, "preview_id": preview},
    )
    assert launch_control(payload, "secret")["cleanup"] == "unknown"
    assert [item[0] for item in calls] == ["GET"]
    payload["operation"] = "start"
    payload["launch"]["scope_id"] = preview
    with pytest.raises(ValueError, match="scope"):
        launch_control(payload, "secret")
    assert [item[0] for item in calls] == ["GET", "GET"]
