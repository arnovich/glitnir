"""Isolated transport fixture; no test launches remote work."""

import pytest


@pytest.fixture
def api(monkeypatch):
    """Return recorded requests for an authenticated writable project."""
    calls = []

    def request(server, project, token, method, route, body=None):
        calls.append((method, route, body))
        if route == "/api/v1/auth/me":
            return {
                "id": "key-1",
                "actor": "agent/test",
                "role": "agent",
                "projects": ["lab"],
            }
        if method == "GET":
            return {
                "projects": [
                    {
                        "id": "lab",
                        "revision": "revision-1",
                        "storage_id": "storage-1",
                        "writable": True,
                        "diagnostics": [],
                    }
                ]
            }
        return {
            "request_id": body["request_id"],
            "task": {"id": "001-request", "project": "lab", "stage": "new"},
        }

    monkeypatch.setattr("glitnir_taskvalet._request", request)
    return calls
