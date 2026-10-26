"""Tests for the local notebook server.

The server serves one saved Lean file in project mode: snapshots, explicit
project checks, proof steps and views, behind a per-session capability.
"""

import hashlib
import http.client
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import settle

from glitnir.notebook.check_records import records_path
from glitnir.service.cli import main
from glitnir.service.preview import SourcePreviewService
from glitnir.service.server import make_server

NOTEBOOK = """import Mathlib

/-! # Energy

The energy of a dissipative system never increases. -/

theorem energy_falls (x : ℝ) : -x ^ 2 ≤ 0 := by
  nlinarith [sq_nonneg x]
"""


@pytest.fixture
def notebook(tmp_path) -> Path:
    path = tmp_path / "Dissipative.lean"
    path.write_text(NOTEBOOK, encoding="utf-8")
    return path


@pytest.fixture
def service(notebook):
    running = SourcePreviewService(notebook)
    yield running
    running.close()


@pytest.fixture
def server(service):
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def _request(server, method, path, body=None, headers=None, app=True):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    sent = {"X-Glitnir": server.RequestHandlerClass.auth_token} if app else {}
    sent.update(headers or {})
    connection.request(method, path, body=body, headers=sent)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


def _post(server, path, body):
    return _request(
        server, "POST", path, json.dumps(body), {"Content-Type": "application/json"}
    )


def test_the_server_binds_to_localhost_only(server) -> None:
    assert server.server_address[0] == "127.0.0.1"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/app.js",
        "/app.css",
        "/cards.css",
        "/cards.js",
        "/circuits.css",
        "/circuits.js",
        "/katex/katex.min.js",
        "/katex/katex.min.css",
        "/katex/fonts/KaTeX_Main-Regular.woff2",
        "/../server.py",
        "/app/app.js",
        "/index.html",
        "/api/decide/catalog",
        "/api/goal?claim=energy_falls",
    ],
)
def test_browser_shell_catalog_and_pinned_reads_are_not_served(server, path) -> None:
    response, _ = _request(server, "GET", path)
    assert response.status == 404
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Access-Control-Allow-Origin") is None


def test_the_notebook_endpoint_returns_the_snapshot(server) -> None:
    response, body = _request(server, "GET", "/api/notebook")
    assert response.status == 200
    snapshot = json.loads(body)
    assert snapshot["file"] == "Dissipative.lean"
    assert snapshot["preview"]["mode"] == "project"
    assert "prose" in [c["kind"] for c in snapshot["cells"]]


def test_requests_for_a_foreign_host_are_refused(server) -> None:
    for method in ("GET", "POST"):
        response, _ = _request(
            server, method, "/api/notebook", headers={"Host": "attacker.example"}
        )
        assert response.status == 403


def test_requests_that_start_work_must_be_bounded_json(server, service) -> None:
    response, _ = _request(server, "POST", "/api/project/check", body="{}")
    assert response.status == 415
    json_headers = {"Content-Type": "application/json"}
    for body in ('{"sha256": "short"}', '["not", "an", "object"]', "not json"):
        response, _ = _request(server, "POST", "/api/project/check", body, json_headers)
        assert response.status == 400
    response, _ = _request(
        server, "POST", "/api/project/check", "x" * 70_000, json_headers
    )
    assert response.status == 413
    response, _ = _request(server, "POST", "/api/other", "{}", json_headers)
    assert response.status == 404
    assert not records_path(service.path).exists()


def test_the_serve_command_starts_the_server(monkeypatch, notebook) -> None:
    seen = []
    monkeypatch.setattr(
        "glitnir.service.cli.serve",
        lambda path, port: seen.append((path, port)),
    )
    assert main(["serve", str(notebook), "--port", "9000"]) == 0
    assert seen == [(notebook, 9000)]


def test_the_command_line_has_no_pinned_mode(notebook) -> None:
    for argv in (["status", str(notebook)], ["refresh", str(notebook)]):
        with pytest.raises(SystemExit):
            main(argv)


def test_api_calls_without_the_workbench_header_or_from_other_sites_are_refused(
    server,
) -> None:
    for method, path in (("GET", "/api/notebook"), ("GET", "/api/proof/trace")):
        response, _ = _request(server, method, path, app=False)
        assert response.status == 403
    response, _ = _request(
        server,
        "POST",
        "/api/project/check",
        "{}",
        {"Content-Type": "application/json"},
        False,
    )
    assert response.status == 403
    response, _ = _request(
        server, "GET", "/api/notebook", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status == 403
    response, _ = _request(
        server, "GET", "/api/notebook", headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert response.status == 200


def test_a_bad_content_length_is_rejected(server) -> None:
    for length in ("-5", "many"):
        response, _ = _request(
            server,
            "POST",
            "/api/project/check",
            "{}",
            {"Content-Type": "application/json", "Content-Length": length},
        )
        assert response.status == 400


def test_export_styles_embed_fonts_without_network_or_local_dependencies(
    server,
) -> None:
    """A shared file needs its styles and maths fonts without a running server."""
    response, body = _request(server, "GET", "/api/export/styles")
    assert response.status == 200
    css = json.loads(body)["css"]
    assert ".katex" in css and ".static-notebook" in css
    assert ".reading-card" in css and ".card-example" in css
    assert "data:font/woff2;base64," in css
    assert "url(fonts/" not in css
    assert not re.search(r"url\([\"']?https?://", css)
    assert "KaTeX" in css and "MIT" in css
    assert "SIL OPEN FONT LICENSE Version 1.1" in css
    assert "Design Science, Inc." in css
    assert "KaTeX_Main" in css


@pytest.mark.parametrize(
    "route",
    [
        "/api/runs",
        "/api/runs/hint",
        "/api/runs/reply",
        "/api/runs/accept",
        "/api/runs/stop",
        "/api/runs/discard",
        "/api/monitor",
    ],
)
def test_retired_agent_routes_are_absent(server, route) -> None:
    """Old clients cannot launch or accept an agent through the application."""
    response, _ = _post(server, route, {"claim": "energy_falls"})
    assert response.status == 404


@pytest.mark.parametrize(
    "route",
    [
        "/api/cells/edit",
        "/api/cells/insert",
        "/api/cells/remove",
        "/api/assets/pin",
        "/api/decide",
        "/api/decide/parse",
        "/api/decide/accept",
        "/api/replay",
    ],
)
def test_browser_mutation_routes_are_absent(server, service, route) -> None:
    before = service.path.read_bytes()
    sidecar = records_path(service.path)
    assert not sidecar.exists()
    response, _ = _post(server, route, {})
    assert response.status == 404
    assert service.path.read_bytes() == before
    assert not sidecar.exists()


def test_project_inspection_http_is_explicit_and_bound(tmp_path, monkeypatch):
    """Only the explicit authenticated POST can invoke project inspection."""
    from glitnir.service.preview import SourcePreviewService

    source = tmp_path / "A.lean"
    source.write_text("example : True := True.intro\n")
    service = SourcePreviewService(source)
    calls = []
    monkeypatch.setattr(
        service, "inspect", lambda sha: calls.append(sha) or {"status": "running"}
    )
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert _request(httpd, "GET", "/api/notebook")[0].status == 200
        assert _request(httpd, "GET", "/api/project/inspect")[0].status == 404
        assert (
            _request(httpd, "POST", "/api/project/inspect", app=False)[0].status == 403
        )
        headers = {"Content-Type": "application/json"}
        assert (
            _request(httpd, "POST", "/api/project/inspect", "{}", headers)[0].status
            == 400
        )
        assert not calls
        digest = service.snapshot()["sha256"]
        response, data = _request(
            httpd,
            "POST",
            "/api/project/inspect",
            json.dumps({"sha256": digest}),
            headers,
        )
        # Accepted, then reported through snapshots like a check.
        assert response.status == 202
        assert json.loads(data)["status"] == "running"
        assert calls == [digest]
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


def test_check_cancel_http_needs_a_running_check(tmp_path, monkeypatch):
    """`/api/project/check/cancel` takes an empty object and stops the check."""
    from glitnir.service.preview import SourcePreviewService

    source = tmp_path / "A.lean"
    source.write_text("example : True := True.intro\n")
    service = SourcePreviewService(source)
    cancelled = []
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    headers = {"Content-Type": "application/json"}
    route = "/api/project/check/cancel"
    try:
        response, data = _request(httpd, "POST", route, "{}", headers)
        assert response.status == 409
        assert json.loads(data) == {"error": "No check is running."}
        assert _request(httpd, "POST", route, '{"x": 1}', headers)[0].status == 400
        monkeypatch.setattr(service, "cancel_check", lambda: cancelled.append(1))
        assert _request(httpd, "POST", route, "{}", headers)[0].status == 202
        assert cancelled == [1]
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


def test_inspection_cancel_http_stops_the_inspection(tmp_path, monkeypatch):
    """`/api/project/inspect/cancel` takes an empty object and stops Lake."""
    from glitnir.service import preview as source_preview
    from glitnir.service.preview import SourcePreviewService

    started = threading.Event()

    def inspect(path, *, expected_sha256, cancel, **options):
        started.set()
        assert cancel.wait(30)
        return {"status": "unavailable", "message": "Lake was stopped."}

    monkeypatch.setattr(source_preview, "inspect_project", inspect)
    source = tmp_path / "A.lean"
    source.write_text("example : True := True.intro\n")
    service = SourcePreviewService(source)
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    headers = {"Content-Type": "application/json"}
    try:
        route = "/api/project/inspect/cancel"
        response, data = _request(httpd, "POST", route, "{}", headers)
        assert response.status == 409
        assert json.loads(data) == {"error": "No inspection is running."}
        body = json.dumps({"sha256": service.snapshot()["sha256"]})
        started_at = _request(httpd, "POST", "/api/project/inspect", body, headers)
        assert started_at[0].status == 202
        assert started.wait(30)
        assert _request(httpd, "POST", route, '{"x": 1}', headers)[0].status == 400
        assert _request(httpd, "POST", route, "{}", headers)[0].status == 202
        settle(service)
        found = service.snapshot()["preview"]["inspection"]
        assert found["message"] == "Project inspection cancelled."
        # The gate is open again.
        again = _request(httpd, "POST", "/api/project/inspect", body, headers)
        assert again[0].status == 202
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


def test_project_check_http_requires_saved_digest(tmp_path, monkeypatch):
    """Project checks have an authenticated, bounded, asynchronous request path."""
    from glitnir.service.preview import SourcePreviewService

    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := True.intro\n")
    service = SourcePreviewService(path)
    calls = []
    monkeypatch.setattr(
        service, "check", lambda digest, through=None: calls.append((digest, through))
    )
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    headers = {"Content-Type": "application/json"}
    try:
        assert _request(httpd, "POST", "/api/project/check", app=False)[0].status == 403
        assert (
            _request(httpd, "POST", "/api/project/check", "{}", headers)[0].status
            == 400
        )
        assert not calls
        digest = service.snapshot()["sha256"]
        response, data = _request(
            httpd, "POST", "/api/project/check", json.dumps({"sha256": digest}), headers
        )
        assert response.status == 202
        assert json.loads(data)["status"] == "queued"
        assert calls == [(digest, None)]
        # A check through a chosen line names it as a positive integer.
        response, _ = _request(
            httpd,
            "POST",
            "/api/project/check",
            json.dumps({"sha256": digest, "through_line": 3}),
            headers,
        )
        assert response.status == 202
        assert calls[-1] == (digest, 3)
        for body in (
            {"sha256": digest, "through_line": 0},
            {"sha256": digest, "through_line": -2},
            {"sha256": digest, "through_line": "3"},
            {"sha256": digest, "through_line": True},
            {"sha256": digest, "through_line": 1.5},
            {"sha256": digest, "through_line": 3, "file": "Other.lean"},
            {"sha256": digest.upper()},
        ):
            response, _ = _request(
                httpd, "POST", "/api/project/check", json.dumps(body), headers
            )
            assert response.status == 400, body
        assert len(calls) == 2
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize(
    "method,route",
    [
        ("GET", "/api/notebook"),
        ("GET", "/api/proof/trace"),
        ("POST", "/api/project/check"),
    ],
)
@pytest.mark.parametrize(
    "token", ["", "1", "incorrect-session-token", "0" * 64, "é" * 64]
)
def test_session_capability_rejects_unauthorized_local_clients(
    server, method, route, token
):
    """Loopback access and the former fixed header do not grant notebook access."""
    response, _ = _request(server, method, route, headers={"X-Glitnir": token})
    assert response.status == 403


def test_sessions_have_distinct_capabilities_and_reject_invalid_configuration(service):
    """An owned server never falls back to a public fixed token."""
    first, second = make_server(service), make_server(service)
    try:
        assert len(first.RequestHandlerClass.auth_token) == 64
        assert (
            first.RequestHandlerClass.auth_token
            != second.RequestHandlerClass.auth_token
        )
        for invalid in ("", "1", "f" * 63, "é" * 64):
            with pytest.raises(ValueError, match="session capability"):
                make_server(service, token=invalid)
    finally:
        first.server_close()
        second.server_close()


def test_serve_consumes_capability_before_constructing_workers(tmp_path, monkeypatch):
    """Workers must not inherit the client's process capability."""
    import os

    from glitnir.service import server as module

    monkeypatch.setenv("GLITNIR_SERVER_TOKEN", "a" * 64)

    def construct(path):
        assert "GLITNIR_SERVER_TOKEN" not in os.environ
        raise RuntimeError("construction reached")

    monkeypatch.setattr(module, "SourcePreviewService", construct)
    with pytest.raises(RuntimeError, match="construction reached"):
        module.serve(tmp_path / "A.lean", 0)


def test_view_cancel_http_stops_the_drawing(tmp_path, monkeypatch, draw_each_alone):
    """`/api/view/cancel` takes an empty JSON object and stops the drawing."""
    from glitnir.providers import Providers, ViewProvider
    from glitnir.service import preview as source_preview
    from glitnir.service.preview import (
        NOTHING_DRAWING,
        VIEW_CANCELLED,
        SourcePreviewService,
    )

    provider = ViewProvider("demo", frozenset({"circuit"}), (), "", "m:", lambda *a: "")
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({"circuit": provider}, {}, {}, {}),
    )
    started = threading.Event()

    def render(*_args, cancel, **_options):
        started.set()
        cancel.wait(30)
        return {"error": "View drawing cancelled.", "transient": True}

    draw_each_alone(render)
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n\n-- @view circuit c\n")
    service = SourcePreviewService(path)
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    headers = {"Content-Type": "application/json"}
    try:
        snapshot = service.snapshot()
        named = json.dumps({"sha256": snapshot["sha256"], "line": 3})
        response, data = _request(httpd, "POST", "/api/view/cancel", named, headers)
        assert response.status == 409
        assert json.loads(data) == {"error": NOTHING_DRAWING}
        cell = snapshot["views"][0]["cell"]
        _, data = _request(httpd, "GET", f"/api/view?cell={cell}&line=3")
        job = json.loads(data)["job"]
        assert started.wait(30)
        for bad in ("{}", '{"line": 3}', json.dumps({"sha256": "x", "line": 3})):
            refused = _request(httpd, "POST", "/api/view/cancel", bad, headers)
            assert refused[0].status == 400
        response, _ = _request(httpd, "POST", "/api/view/cancel", named, headers)
        assert response.status == 202
        settle(service)
        _, data = _request(httpd, "GET", f"/api/view/job?id={job}")
        assert json.loads(data) == {"error": VIEW_CANCELLED}
        for query in ("", "?id=x", f"?id={job}&id={job}", f"?id={job}&cell=0"):
            assert _request(httpd, "GET", f"/api/view/job{query}")[0].status == 400
        polled = _request(httpd, "GET", f"/api/view/job?id={job}", app=False)
        assert polled[0].status == 403
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


def test_project_view_http_draws_directives(tmp_path, monkeypatch, draw_each_alone):
    """Project mode answers /api/view for listed directives only."""
    from glitnir.providers import Providers, ViewProvider
    from glitnir.service import preview as source_preview
    from glitnir.service.preview import SourcePreviewService

    provider = ViewProvider("demo", frozenset({"circuit"}), (), "", "m:", lambda *a: "")
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({"circuit": provider}, {}, {}, {}),
    )
    draw_each_alone(lambda *a, **k: {"svg": "<svg/>"})
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n\n-- @view circuit c\n")
    service = SourcePreviewService(path)
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        cell = service.snapshot()["views"][0]["cell"]
        response, data = _request(httpd, "GET", f"/api/view?cell={cell}&line=3")
        # The first request starts the drawing and answers at once.
        assert response.status == 200 and json.loads(data)["status"] == "drawing"
        settle(service)
        job = json.loads(data)["job"]
        _, polled = _request(httpd, "GET", f"/api/view/job?id={job}")
        assert json.loads(polled) == {"svg": "<svg/>"}
        response, data = _request(httpd, "GET", f"/api/view?cell={cell}&line=3")
        assert response.status == 200 and json.loads(data) == {"svg": "<svg/>"}
        assert _request(httpd, "GET", f"/api/view?cell={cell}&line=99")[0].status == 404
        assert _request(httpd, "GET", "/api/view?cell=x&line=3")[0].status == 400
        assert (
            _request(httpd, "GET", "/api/view?cell=0&line=3", app=False)[0].status
            == 403
        )
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


def test_evidence_run_http_starts_listed_directives_only(tmp_path, monkeypatch):
    """Runs need the saved digest and a listed line; refusals are conflicts."""
    from glitnir.providers import EvidenceProvider, EvidenceRequest, Providers
    from glitnir.service import preview as source_preview
    from glitnir.service.preview import SourcePreviewService

    provider = EvidenceProvider(
        name="demo",
        verbs=frozenset({"simulate"}),
        imports=(),
        preamble="",
        marker="m:",
        parse=lambda verb, target, options: EvidenceRequest(verb, target, {}),
        command=lambda request: "",
        compute=lambda request, payload: {},
        unavailable=lambda: None,
    )
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({}, {"simulate": provider}, {}, {}),
    )
    started = threading.Event()

    def run(source, **options):
        started.set()
        return {"error": "Lean is not here."}

    monkeypatch.setattr(source_preview, "run_provider_lean", run)
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n\n-- @evidence simulate c\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    service = SourcePreviewService(path)
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for body in [
            {},
            {"sha256": "x", "line": 3},
            {"sha256": digest},
            {"sha256": digest, "line": 0},
            {"sha256": digest, "line": "3"},
            {"sha256": digest, "line": True},
            {"sha256": digest, "line": 3, "key": "k"},
        ]:
            assert _post(httpd, "/api/evidence/run", body)[0].status == 400, body
        assert not started.is_set()
        response, data = _post(
            httpd, "/api/evidence/run", {"sha256": digest, "line": 1}
        )
        assert response.status == 409
        assert json.loads(data) == {"error": "No evidence directive at line 1."}
        response, data = _post(
            httpd, "/api/evidence/run", {"sha256": "0" * 64, "line": 3}
        )
        assert response.status == 409
        assert "changed" in json.loads(data)["error"]
        response, data = _post(
            httpd, "/api/evidence/run", {"sha256": digest, "line": 3}
        )
        assert response.status == 202
        [card] = service.snapshot()["evidence"]
        assert json.loads(data) == {"running": card["key"]}
        assert started.wait(5)
        settle(service)
        [card] = service.snapshot()["evidence"]
        assert card["run_error"] == "Lean is not here."
        assert not records_path(path).exists()
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


def test_decision_http_runs_stops_and_discards_listed_directives(
    tmp_path, monkeypatch, patch_fingerprint
):
    """Decisions name the saved digest and a listed line; refusals are conflicts."""
    from glitnir.providers import DecisionProvider, DecisionRequest, Providers
    from glitnir.service import preview as source_preview

    started, release = threading.Event(), threading.Event()

    def decide(request, cancel):
        started.set()
        while not (release.is_set() or cancel.is_set()):
            cancel.wait(0.01)
        return {"status": "unknown"}

    provider = DecisionProvider(
        name="demo",
        kinds=frozenset({"sum"}),
        imports=(),
        parse=lambda name, kind, goal: DecisionRequest(name, kind, goal, {}),
        decide=decide,
        unavailable=lambda: None,
    )
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({}, {}, {"sum": provider}, {}),
    )
    patch_fingerprint(lambda *a: {"sha256": "f" * 64})
    monkeypatch.setattr(
        source_preview, "provider_imports_error", lambda source, **options: None
    )
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n\n-- @decide four : sum\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    service = SourcePreviewService(path)
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        for route in ["/api/decide/run", "/api/decide/discard"]:
            for body in [
                {},
                {"sha256": "x", "line": 3},
                {"sha256": digest},
                {"sha256": digest, "line": 0},
                {"sha256": digest, "line": "3"},
                {"sha256": digest, "line": True},
                {"sha256": digest, "line": 3, "block": "theorem x : True"},
            ]:
                assert _post(httpd, route, body)[0].status == 400, (route, body)
            response, data = _post(httpd, route, {"sha256": digest, "line": 1})
            assert response.status == 409
            assert json.loads(data) == {"error": "No decision directive at line 1."}
            response, data = _post(httpd, route, {"sha256": "0" * 64, "line": 3})
            assert response.status == 409
            assert "changed" in json.loads(data)["error"]
        for body in [{"line": 3}, {"sha256": digest, "line": 3}]:
            assert _post(httpd, "/api/decide/stop", body)[0].status == 400, body
        response, data = _post(httpd, "/api/decide/stop", {})
        assert response.status == 409
        assert json.loads(data) == {"error": "No decision is running."}
        response, data = _post(
            httpd, "/api/decide/discard", {"sha256": digest, "line": 3}
        )
        assert response.status == 409
        assert json.loads(data) == {"error": "No decision is saved for this directive."}
        assert not started.is_set()
        # A stopped run saves nothing.
        response, data = _post(httpd, "/api/decide/run", {"sha256": digest, "line": 3})
        assert response.status == 202
        [card] = service.snapshot()["decisions"]
        assert json.loads(data) == {"running": card["key"]}
        assert started.wait(5)
        response, data = _post(httpd, "/api/decide/run", {"sha256": digest, "line": 3})
        assert response.status == 409
        assert json.loads(data) == {"error": "A provider run is already in progress."}
        response, data = _post(
            httpd, "/api/decide/discard", {"sha256": digest, "line": 3}
        )
        assert response.status == 409
        assert json.loads(data) == {"error": "This decision is running; stop it first."}
        response, data = _post(httpd, "/api/decide/stop", {})
        assert (response.status, json.loads(data)) == (202, {"status": "cancelling"})
        settle(service)
        [card] = service.snapshot()["decisions"]
        assert card["run_error"] == "Decision run cancelled."
        assert not records_path(path).exists()
        # A finished run saves its outcome, and discarding removes it.
        release.set()
        assert (
            _post(httpd, "/api/decide/run", {"sha256": digest, "line": 3})[0].status
            == 202
        )
        settle(service)
        assert service.snapshot()["decisions"][0]["status"] == "unknown"
        response, data = _post(
            httpd, "/api/decide/discard", {"sha256": digest, "line": 3}
        )
        assert (response.status, json.loads(data)) == (200, {"discarded": card["key"]})
        assert service.snapshot()["decisions"][0]["status"] == "never"
        assert path.read_text() == "def c := 1\n\n-- @decide four : sum\n"
    finally:
        release.set()
        service.close()
        httpd.shutdown()
        httpd.server_close()


def test_decision_candidates_are_read_by_one_key_and_refused_as_conflicts(
    tmp_path, monkeypatch
):
    """The editor reads a candidate's block by its key; nothing is written."""
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n\n-- @decide four : sum\n")
    service = SourcePreviewService(path)
    httpd = make_server(service, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    key = "a" * 64
    try:
        for query in [
            "",
            "?key=",
            "?key=" + "A" * 64,
            "?key=" + "a" * 63,
            f"?key={key}&key={key}",
            f"?key={key}&line=3",
        ]:
            response, _ = _request(httpd, "GET", "/api/decide/candidate" + query)
            assert response.status == 400, query
        response, data = _request(httpd, "GET", f"/api/decide/candidate?key={key}")
        assert response.status == 409
        assert json.loads(data) == {
            "error": "This decision is no longer in the saved notebook."
        }
        served = {"sha256": "b" * 64, "insert_after_line": 3, "block": "theorem t"}
        seen: list[str] = []
        monkeypatch.setattr(
            service, "decision_candidate", lambda key: seen.append(key) or served
        )
        response, data = _request(httpd, "GET", f"/api/decide/candidate?key={key}")
        assert (response.status, json.loads(data), seen) == (200, served, [key])
        response, _ = _post(httpd, "/api/decide/candidate", {"key": key})
        assert response.status == 404
        response, _ = _request(
            httpd, "GET", f"/api/decide/candidate?key={key}", app=False
        )
        assert response.status == 403
        assert path.read_text() == "def c := 1\n\n-- @decide four : sum\n"
        assert not records_path(path).exists()
    finally:
        service.close()
        httpd.shutdown()
        httpd.server_close()


# ----- images ------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture
def illustrated(notebook):
    notebook.write_text(
        NOTEBOOK.replace(
            "never increases. -/",
            "never increases.\n\n![Plot](assets/plot.png)\n\n"
            "![Sketch](assets/sketch.svg) -/",
        ),
        encoding="utf-8",
    )
    folder = notebook.parent / "assets"
    folder.mkdir()
    (folder / "plot.png").write_bytes(PNG)
    (folder / "sketch.svg").write_bytes(SVG)
    (folder / "unused.png").write_bytes(PNG)
    return notebook


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _asset_path(path: str, digest: str) -> str:
    return f"/asset?path={path}&sha256={digest}"


def test_the_snapshot_reports_each_referenced_image(server, illustrated) -> None:
    _, body = _request(server, "GET", "/api/notebook")
    assert json.loads(body)["assets"] == [
        {"path": "assets/plot.png", "status": "ok", "sha256": _sha(PNG)},
        {"path": "assets/sketch.svg", "status": "ok", "sha256": _sha(SVG)},
    ]


def test_images_are_served_only_at_their_current_hash(server, illustrated) -> None:
    response, body = _request(server, "GET", _asset_path("assets/plot.png", _sha(PNG)))
    assert response.status == 200 and body == PNG
    assert response.getheader("Content-Type") == "image/png"
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Cache-Control") == "no-store"
    response, body = _request(server, "GET", _asset_path("assets/plot.png", "0" * 64))
    assert response.status == 409
    assert body != PNG and "error" in json.loads(body)


def test_svg_images_are_served_with_a_policy_that_runs_no_scripts(
    server, illustrated
) -> None:
    response, body = _request(
        server, "GET", _asset_path("assets/sketch.svg", _sha(SVG))
    )
    assert response.status == 200 and body == SVG
    assert response.getheader("Content-Type") == "image/svg+xml"
    policy = response.getheader("Content-Security-Policy")
    assert "default-src 'none'" in policy and "sandbox" in policy
    assert "script-src" not in policy


def test_image_reads_need_the_session_capability(server, illustrated) -> None:
    path = _asset_path("assets/plot.png", _sha(PNG))
    response, body = _request(server, "GET", path, app=False)
    assert response.status == 403 and body != PNG
    response, body = _request(server, "GET", path + "&token=x", app=False)
    assert response.status == 403 and body != PNG
    response, body = _request(
        server, "GET", path, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status == 403 and body != PNG


@pytest.mark.parametrize(
    "path, status",
    [
        (_asset_path("assets/unused.png", _sha(PNG)), 404),
        (_asset_path("assets/../Dissipative.lean", "a" * 64), 404),
        (_asset_path("assets/gone.png", "a" * 64), 404),
        ("/asset?path=assets/plot.png", 400),
        ("/asset?path=assets/plot.png&sha256=xyz", 400),
        ("/asset?sha256=" + "a" * 64, 400),
        (_asset_path("assets/plot.png", "a" * 64) + "&path=assets/sketch.svg", 400),
    ],
)
def test_bad_image_requests_are_refused(server, illustrated, path, status) -> None:
    response, body = _request(server, "GET", path)
    assert response.status == status
    assert "error" in json.loads(body)


def test_lean_requests_on_another_version_answer_the_refusal(tmp_path) -> None:
    """Each endpoint refuses in the shape it already uses for a refusal."""
    (tmp_path / "lakefile.toml").write_text('name = "demo"')
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.1\n")
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n-- @view circuit c\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    service = SourcePreviewService(path)
    httpd = make_server(service, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        response, data = _request(httpd, "GET", "/api/notebook")
        message = json.loads(data)["preview"]["lean"]["message"]
        assert message.startswith("This project uses Lean 4.32.1. ")
        for route, body, status in [
            ("/api/project/inspect", {"sha256": digest}, 400),
            ("/api/project/check", {"sha256": digest}, 400),
            ("/api/proof/start", {"sha256": digest}, 400),
            ("/api/evidence/run", {"sha256": digest, "line": 2}, 409),
            ("/api/decide/run", {"sha256": digest, "line": 2}, 409),
        ]:
            response, data = _post(httpd, route, body)
            assert (route, response.status) == (route, status)
            assert json.loads(data) == {"error": message}
        response, data = _request(httpd, "GET", "/api/view?cell=0&line=2")
        assert response.status == 200
        assert json.loads(data) == {"error": message}
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.close()


def test_python_m_glitnir_notebook_still_names_itself() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-m", "glitnir.notebook", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0
    assert "python -m glitnir.notebook" in done.stdout
    assert "{check,serve}" in done.stdout
