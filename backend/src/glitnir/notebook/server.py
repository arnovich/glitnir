"""Serve the editor notebook API on localhost.

The server reads the saved Lean file on every request, so the preview always
reflects the file on disk. It runs Lean only when asked: a project check
elaborates the saved file in its Lake project (or, on request, only its lines
through a chosen claim), proof steps observe one saved
proof, a view request starts or reports drawing one listed directive in a
worker, an inspection reads the project's setup in a worker, an evidence run computes
one listed directive's evidence and saves it beside the notebook, and a decision
run asks a provider for a Lean block, checks it and saves the outcome beside the
notebook. Source editing belongs to the editor, which inserts a checked block it
reads from ``/api/decide/candidate``. Another notebook of the same project,
named by a prose link, is served read-only at ``/api/linked`` with its own saved
results; nothing is run for it. Nothing it serves grants proof authority; only
the project checker's verdict on a candidate block does.

It binds to ``127.0.0.1`` only and refuses requests whose ``Host`` is not a
local name, so a web page cannot reach it through DNS rebinding. Every API
request must carry a per-session capability in the ``X-Glitnir`` header, which
a cross-site page cannot send without a CORS preflight this server never
answers, and must not be marked as cross-site by ``Sec-Fetch-Site``; requests
that start work must also be ``application/json``. Images the prose shows are
read through the same check at ``/asset``, with the capability in the header
and never in the URL, and are served only at the hash the snapshot announced.
"""

import json
import os
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.scratch import sweep_stale_scratch
from glitnir.notebook.assets import AssetError
from glitnir.notebook.document import NotebookError
from glitnir.notebook.source_preview import NotebookLinkError, SourcePreviewService
from glitnir.notebook.static_export import export_styles

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY_BYTES = 65_536
#: Header the workbench sends on every API request; see the module docstring.
API_HEADER = "X-Glitnir"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_JOB = re.compile(r"[0-9a-f]{32}")


def make_server(
    service: SourcePreviewService,
    port: int = 0,
    *,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """Build the HTTP server for a service, bound to localhost.

    Args:
        service: The notebook to serve.
        port: The port, or 0 for any free one.
        token: The 256-bit session capability as 64 hex digits; a fresh one
            when None.
    """
    capability = secrets.token_hex(32) if token is None else token
    if len(capability) != 64 or any(c not in "0123456789abcdef" for c in capability):
        raise ValueError("Notebook server requires a 256-bit session capability")

    class Handler(_Handler):
        notebook = service
        auth_token = capability

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


class _Handler(BaseHTTPRequestHandler):
    notebook: SourcePreviewService
    auth_token: str
    server_version = "GlitnirNotebook"

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the terminal quiet."""

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        """Serve snapshots, proof steps, styles, views and candidate blocks."""
        if not self._local() or not self._from_app():
            return
        url = urlparse(self.path)
        try:
            if url.path == "/api/notebook":
                self._json(HTTPStatus.OK, self.notebook.snapshot())
            elif url.path == "/api/linked":
                self._linked(parse_qs(url.query))
            elif url.path == "/api/proof/trace":
                self._json(HTTPStatus.OK, self.notebook.proof_steps.snapshot())
            elif url.path == "/api/export/styles":
                self._json(HTTPStatus.OK, {"css": export_styles()})
            elif url.path == "/api/view":
                self._view(parse_qs(url.query))
            elif url.path == "/api/view/job":
                self._view_job(parse_qs(url.query))
            elif url.path == "/asset":
                self._asset(parse_qs(url.query))
            elif url.path == "/api/decide/candidate":
                self._candidate(parse_qs(url.query))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (OSError, ValueError) as error:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
        """Start the checks, proof observations and runs requested by the editor."""
        if not self._local() or not self._from_app():
            return
        route = urlparse(self.path).path
        actions = {
            "/api/project/inspect": self._inspect_project,
            "/api/project/check": self._check_project,
            "/api/proof/start": self._start_proof,
            "/api/proof/cancel": self._cancel_proof,
            "/api/view/cancel": self._cancel_view,
            "/api/project/inspect/cancel": self._cancel_inspection,
            "/api/evidence/run": self._run_evidence,
            "/api/decide/run": self._run_decision,
            "/api/decide/stop": self._stop_decision,
            "/api/decide/discard": self._discard_decision,
        }
        if route not in actions:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "send application/json"},
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            status, answer = actions[route](body)
        except (ValueError, KeyError, NotebookError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except OSError as error:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return
        self._json(status, answer)

    def _view(self, query: dict[str, list[str]]) -> None:
        """Report the drawing of the view directive at one cell and line.

        It never waits for Lean: a drawing in progress answers with its
        status, and the workbench asks again (`SourcePreviewService.view`).
        """
        try:
            cell = int(query.get("cell", [""])[0])
            line = int(query.get("line", [""])[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "cell and line are numbers"})
            return
        try:
            self._json(HTTPStatus.OK, self.notebook.view(cell, line))
        except (KeyError, NotebookError) as error:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})

    def _view_job(self, query: dict[str, list[str]]) -> None:
        """Answer a poll for one view job; the notebook is not read."""
        ids = query.get("id", [])
        if set(query) != {"id"} or len(ids) != 1 or not _JOB.fullmatch(ids[0]):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "send one view job id"})
            return
        self._json(HTTPStatus.OK, self.notebook.view_job(ids[0]))

    def _linked(self, query: dict[str, list[str]]) -> None:
        """Serve another notebook of this project, read-only, by its link."""
        links = query.get("path", [])
        if set(query) != {"path"} or len(links) != 1:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "send one notebook path"})
            return
        try:
            self._json(HTTPStatus.OK, self.notebook.linked(links[0]))
        except NotebookLinkError as error:
            self._json(HTTPStatus(error.status), {"error": str(error)})

    def _asset(self, query: dict[str, list[str]]) -> None:
        """Serve one image the saved prose shows, at the hash the page expects."""
        paths, digests = query.get("path", []), query.get("sha256", [])
        if (
            set(query) != {"path", "sha256"}
            or len(paths) != 1
            or len(digests) != 1
            or not _SHA256.fullmatch(digests[0])
        ):
            self._json(
                HTTPStatus.BAD_REQUEST, {"error": "send one path and its sha256"}
            )
            return
        try:
            body, content_type = self.notebook.asset(paths[0], digests[0])
        except AssetError as error:
            status = (
                HTTPStatus.CONFLICT
                if error.status == "changed"
                else HTTPStatus.NOT_FOUND
            )
            self._json(status, {"error": str(error)})
            return
        self._send(HTTPStatus.OK, body, content_type, image=True)

    def _candidate(self, query: dict[str, list[str]]) -> None:
        """Serve a current candidate's block for the editor to insert."""
        keys = query.get("key", [])
        if set(query) != {"key"} or len(keys) != 1 or not _SHA256.fullmatch(keys[0]):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "send one decision key"})
            return
        try:
            self._json(HTTPStatus.OK, self.notebook.decision_candidate(keys[0]))
        except SnapshotError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _start_proof(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Observe a selected saved proof without modifying its check metadata."""
        digest = _text(body, "sha256")
        return HTTPStatus.ACCEPTED, self.notebook.proof_steps.start(digest)

    def _cancel_proof(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Cancel an observation even when its source has since changed."""
        self.notebook.proof_steps.cancel()
        return HTTPStatus.ACCEPTED, {"status": "cancelling"}

    def _cancel_view(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Stop drawing the view at one directive line of the saved source."""
        digest, line = _directive(body)
        try:
            self.notebook.cancel_view(digest, line)
        except SnapshotError as error:
            return HTTPStatus.CONFLICT, {"error": str(error)}
        return HTTPStatus.ACCEPTED, {"status": "cancelling"}

    def _cancel_inspection(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Stop the running project inspection."""
        if body:
            raise ValueError("send an empty object")
        try:
            self.notebook.cancel_inspection()
        except SnapshotError as error:
            return HTTPStatus.CONFLICT, {"error": str(error)}
        return HTTPStatus.ACCEPTED, {"status": "cancelling"}

    def _inspect_project(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Start resolving project setup on an explicit saved-source request.

        The result is reported through snapshot polling, as a check's is.
        """
        digest = body.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("send the preview source sha256")
        return HTTPStatus.ACCEPTED, self.notebook.inspect(digest)

    def _check_project(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Queue a saved-file check, reported through ordinary snapshot polling.

        ``through_line`` asks for a check of the saved file's lines 1 through
        that line only; without it the whole file is checked.
        """
        if not set(body) <= {"sha256", "through_line"}:
            raise ValueError("send only the preview source sha256 and through_line")
        digest = body.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("send the preview source sha256")
        through = body.get("through_line")
        if through is not None and (type(through) is not int or through < 1):
            raise ValueError("send through_line as a positive integer")
        self.notebook.check(digest, through)
        return HTTPStatus.ACCEPTED, {"status": "queued"}

    def _run_evidence(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Start one evidence run, reported through ordinary snapshot polling."""
        digest, line = _directive(body)
        try:
            key = self.notebook.run_evidence(digest, line)
        except SnapshotError as error:
            return HTTPStatus.CONFLICT, {"error": str(error)}
        return HTTPStatus.ACCEPTED, {"running": key}

    def _run_decision(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Start one decision run, reported through ordinary snapshot polling."""
        digest, line = _directive(body)
        try:
            key = self.notebook.run_decision(digest, line)
        except SnapshotError as error:
            return HTTPStatus.CONFLICT, {"error": str(error)}
        return HTTPStatus.ACCEPTED, {"running": key}

    def _stop_decision(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Cancel the running decision, which then saves nothing."""
        if body:
            raise ValueError("send an empty object")
        try:
            self.notebook.stop_decision()
        except SnapshotError as error:
            return HTTPStatus.CONFLICT, {"error": str(error)}
        return HTTPStatus.ACCEPTED, {"status": "cancelling"}

    def _discard_decision(self, body: dict[str, Any]) -> tuple[HTTPStatus, Any]:
        """Remove a directive's saved decision from the results file."""
        digest, line = _directive(body)
        try:
            key = self.notebook.discard_decision(digest, line)
        except SnapshotError as error:
            return HTTPStatus.CONFLICT, {"error": str(error)}
        return HTTPStatus.OK, {"discarded": key}

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host.lower() in LOCAL_HOSTS:
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "only local requests"})
        return False

    def _from_app(self) -> bool:
        """Require the owning client capability, including for loopback callers."""
        cross_site = self.headers.get("Sec-Fetch-Site", "same-origin") not in (
            "same-origin",
            "none",
        )
        supplied = self.headers.get(API_HEADER, "")
        authenticated = (
            len(supplied) == 64
            and all(c in "0123456789abcdef" for c in supplied)
            and secrets.compare_digest(supplied, self.auth_token)
        )
        if not authenticated or cross_site:
            self._json(HTTPStatus.FORBIDDEN, {"error": "only the workbench may call"})
            return False
        return True

    def _json(self, status: HTTPStatus, value: Any) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send(
        self, status: HTTPStatus, body: bytes, content_type: str, image: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Images are user files: opened on their own, an SVG runs no scripts.
        policy = (
            "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            if image
            else "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; frame-ancestors 'none'"
        )
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(body)


def _directive(body: dict[str, Any]) -> tuple[str, int]:
    """Read the saved source digest and directive line a run or discard names."""
    if not set(body) <= {"sha256", "line"}:
        raise ValueError("send only the preview source sha256 and directive line")
    digest, line = body.get("sha256"), body.get("line")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("send the preview source sha256")
    if type(line) is not int or line < 1:
        raise ValueError("send the directive line as a positive integer")
    return digest, line


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if type(value) is not str or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def serve(path: Path, port: int) -> None:
    """Serve a notebook until interrupted, printing its address."""
    token = os.environ.pop("GLITNIR_SERVER_TOKEN", "")
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("Start the notebook API through the Glitnir client")
    # Leaked snapshots can be gigabytes; removing them must not delay startup.
    threading.Thread(
        target=sweep_stale_scratch, name="glitnir-scratch-sweep", daemon=True
    ).start()
    service = SourcePreviewService(path)
    server = make_server(service, port, token=token)
    print(
        f"Serving {path.name} at http://127.0.0.1:{server.server_port}/"
        "  (Ctrl-C to stop)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
