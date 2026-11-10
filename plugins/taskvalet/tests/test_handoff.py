"""Task requests preserve reviewed source and never launch campaigns."""

import pytest

from glitnir_taskvalet import prepare, submit


def test_prepare_freezes_a_source_bound_unqueued_request(api):
    draft = prepare(
        {
            "server": "https://tasks.example",
            "project": "lab",
            "name": "Demo.lean",
            "source": "theorem demo : True := by trivial\n",
            "objective": "Explore a simpler proof",
        },
        "secret",
    )
    assert draft["actor"] == "agent/test"
    assert draft["command"]["action"] == "create"
    assert "source_sha256" in draft["command"]["text"]
    assert "No swarm has been launched" in draft["command"]["outcome"]
    assert "next" not in draft["command"]
    assert "secret" not in str(draft)
    first = submit(draft, "secret")
    second = submit(draft, "secret")
    assert first == second
    posts = [body for method, route, body in api if method == "POST"]
    assert posts == [draft["command"], draft["command"]]
    assert all("campaign" not in route for _, route, _ in api)


@pytest.mark.parametrize(
    "server",
    [
        "http://tasks.example",
        "https://u:p@tasks.example",
        "https://tasks.example/path",
        "https://tasks.example/?x=1",
    ],
)
def test_refuses_unsafe_destination(server, api):
    with pytest.raises(ValueError, match="HTTPS"):
        prepare(
            {
                "server": server,
                "project": "lab",
                "name": "A.lean",
                "source": "x",
                "objective": "Explore",
            },
            "secret",
        )
    assert not api


def test_recovery_refuses_changed_actor(api, monkeypatch):
    draft = prepare(
        {
            "server": "https://tasks.example",
            "project": "lab",
            "name": "A.lean",
            "source": "x",
            "objective": "Explore",
        },
        "secret",
    )
    draft["actor"] = "agent/other"
    with pytest.raises(ValueError, match="identity"):
        submit(draft, "secret")
    assert not any(method == "POST" for method, _, _ in api)


def test_definitive_conflict_can_be_retired_but_uncertain_failure_cannot(
    api, monkeypatch
):
    from glitnir_taskvalet import RequestRefused

    draft = prepare(
        {
            "server": "https://tasks.example",
            "project": "lab",
            "name": "A.lean",
            "source": "x",
            "objective": "Explore",
        },
        "secret",
    )
    import glitnir_taskvalet

    original = glitnir_taskvalet._request

    def rejected(server, project, token, method, route, body=None):
        if method == "POST":
            raise RequestRefused("revision conflict")
        return original(server, project, token, method, route, body)

    monkeypatch.setattr(glitnir_taskvalet, "_request", rejected)
    assert submit(draft, "secret")["refused"] is True

    def uncertain(server, project, token, method, route, body=None):
        if method == "POST":
            raise ValueError("lost response")
        return original(server, project, token, method, route, body)

    monkeypatch.setattr(glitnir_taskvalet, "_request", uncertain)
    with pytest.raises(ValueError, match="lost response"):
        submit(draft, "secret")


@pytest.mark.parametrize("role", ["owner", "machine", "reader", "compute_client"])
def test_non_agent_credentials_cannot_submit(role, monkeypatch):
    monkeypatch.setattr(
        "glitnir_taskvalet._request",
        lambda *a, **k: {
            "role": role,
            "actor": "someone",
            "projects": ["lab"],
            "id": "key",
        },
    )
    with pytest.raises(ValueError, match="registered agent"):
        prepare(
            {
                "server": "https://tasks.example",
                "project": "lab",
                "name": "A.lean",
                "source": "x",
                "objective": "Explore",
            },
            "secret",
        )


@pytest.mark.parametrize(
    "status,detail,safe",
    [
        (403, "Access denied", False),
        (409, "Request ID was already used for a different command", False),
        (409, "The project changed. Refresh before retrying this action.", True),
    ],
)
def test_transport_only_classifies_receipt_checked_revision_conflicts(
    monkeypatch, status, detail, safe
):
    import io
    import json
    import urllib.error
    from types import SimpleNamespace

    from glitnir_taskvalet import RequestRefused, _request

    def reject(request, timeout):
        assert request.get_header("User-agent") == "glitnir-taskvalet/0.1"
        raise urllib.error.HTTPError(
            request.full_url,
            status,
            "Refused",
            {},
            io.BytesIO(json.dumps({"code": "refused", "detail": detail}).encode()),
        )

    monkeypatch.setattr(
        "urllib.request.build_opener", lambda *args: SimpleNamespace(open=reject)
    )
    with pytest.raises(ValueError) as caught:
        _request("https://tasks.example", "lab", "secret", "POST", "/commands", {})
    assert isinstance(caught.value, RequestRefused) is safe
