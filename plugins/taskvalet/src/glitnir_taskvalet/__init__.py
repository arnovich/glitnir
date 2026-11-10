"""Source-bound Taskvalet requests; no campaign launch authority."""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from glitnir.providers.handoff import HandoffProvider


class RequestRefused(ValueError):
    """An authoritative POST rejection, safe to retire after explicit review."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a credential to a redirect destination."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _destination(value: dict[str, Any]) -> tuple[str, str]:
    """Validate a single HTTPS origin and Taskvalet project identifier."""
    server, project = value["server"], value["project"]
    url = urllib.parse.urlsplit(server)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or any(c.isspace() for c in server)
    ):
        raise ValueError("Use a credential-free HTTPS origin")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project):
        raise ValueError("Invalid Taskvalet project")
    return server.rstrip("/"), project


def _request(
    server: str,
    project: str,
    token: str,
    method: str,
    route: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one bounded authenticated request without redirects or retries."""
    if not token or len(token) > 256 or any(c.isspace() for c in token):
        raise ValueError("Invalid handoff credential")
    request = urllib.request.Request(
        server + route,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "glitnir-taskvalet/0.1",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=20
        ) as response:
            data = response.read(2_000_001)
    except urllib.error.HTTPError as error:
        try:
            refusal = json.loads(error.read(4096))
        except (ValueError, OSError):
            refusal = {}
        # These storage errors occur after durable receipt lookup. Other 4xx
        # responses can precede replay and do not resolve a prior lost response.
        if (
            method == "POST"
            and error.code == 409
            and isinstance(refusal, dict)
            and refusal.get("code") == "refused"
            and refusal.get("detail")
            in {
                "The project changed. Refresh before retrying this action.",
                "Project changed; refresh before retrying this decision",
            }
        ):
            raise RequestRefused(
                "Taskvalet refused this request without creating a task"
            ) from error
        raise ValueError(
            "Taskvalet request failed; preserve the saved request"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ValueError(
            "Taskvalet request failed; retain the original request for recovery"
        ) from error
    if len(data) > 2_000_000:
        raise ValueError("Taskvalet response exceeds the handoff limit")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Invalid Taskvalet response")
    return result


def _identity(server: str, project: str, token: str) -> dict[str, Any]:
    """Require a registered agent, never an owner administration credential."""
    identity = _request(server, project, token, "GET", "/api/v1/auth/me")
    if (
        identity.get("role") != "agent"
        or not isinstance(identity.get("actor"), str)
        or project not in identity.get("projects", [])
    ):
        raise ValueError("Use a project-registered agent handoff credential")
    return identity


def prepare(value: dict[str, Any], token: str) -> dict[str, Any]:
    """Freeze a reviewed request without creating a task or launching work."""
    server, project = _destination(value)
    source, name, objective = value["source"], value["name"], value["objective"]
    if (
        not isinstance(source, str)
        or len(source.encode()) > 30_000
        or not isinstance(name, str)
        or not re.fullmatch(r"[\w .-]+\.lean", name)
        or not isinstance(objective, str)
        or not 1 <= len(objective.strip()) <= 2000
    ):
        raise ValueError(
            "Use a saved notebook under 30 KB and a short research objective"
        )
    identity = _identity(server, project, token)
    board = _request(server, project, token, "GET", f"/api/v1/projects/{project}")
    configured: dict[str, Any] = next(
        (p for p in board.get("projects", []) if p.get("id") == project), {}
    )
    if (
        not configured.get("writable")
        or configured.get("diagnostics")
        or configured.get("error")
    ):
        raise ValueError("Taskvalet project is unavailable or read-only")
    digest = hashlib.sha256(source.encode()).hexdigest()
    # JSON indentation prevents notebook headings from becoming task sections.
    context = (
        "Saved notebook research request. Source only; imports and environment "
        "are not uploaded or verified.\n\n"
        + json.dumps(
            {
                "notebook": name,
                "source_sha256": digest,
                "objective": objective.strip(),
                "source": source,
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    command = {
        "action": "create",
        "revision": configured["revision"],
        "request_id": str(uuid.uuid4()),
        "title": f"Research request: {name}"[:240],
        "text": context,
        "outcome": (
            "- [ ] Owner reviews the saved source/objective, supplies dependencies "
            "and environment, and prepares research assignments before previewing "
            "any campaign. No swarm has been launched. Findings remain proposals "
            "until independently checked against the exact target."
        ),
        "priority": "medium",
        "labels": ["glitnir", "research-request"],
    }
    if len(context) > 50_000:
        raise ValueError("Notebook request exceeds Taskvalet's text limit")
    return {
        "server": server,
        "project": project,
        "actor": identity["actor"],
        "credential_id": identity["id"],
        "storage_id": configured["storage_id"],
        "source_sha256": digest,
        "command": command,
    }


def submit(value: dict[str, Any], token: str) -> dict[str, Any]:
    """Submit or recover the exact saved create envelope, never a campaign."""
    server, project = _destination(value)
    identity = _identity(server, project, token)
    if identity["actor"] != value["actor"] or identity["id"] != value["credential_id"]:
        raise ValueError("The saved request belongs to a different credential identity")
    board = _request(server, project, token, "GET", f"/api/v1/projects/{project}")
    configured: dict[str, Any] = next(
        (p for p in board.get("projects", []) if p.get("id") == project), {}
    )
    if (
        not value.get("storage_id")
        or configured.get("storage_id") != value["storage_id"]
    ):
        raise ValueError("Taskvalet storage changed; reconcile the original request")
    command = value["command"]
    if command.get("action") != "create" or set(command) != {
        "action",
        "revision",
        "request_id",
        "title",
        "text",
        "outcome",
        "priority",
        "labels",
    }:
        raise ValueError("Only the saved unqueued request can be submitted")
    uuid.UUID(command["request_id"])
    try:
        receipt = _request(
            server,
            project,
            token,
            "POST",
            f"/api/v1/projects/{project}/commands",
            command,
        )
    except RequestRefused:
        return {
            "refused": True,
            "message": (
                "Taskvalet refused the request. Retire it explicitly "
                "and prepare a fresh preview."
            ),
        }
    task = receipt["task"]
    if (
        receipt.get("request_id") != command["request_id"]
        or task.get("project") != project
    ):
        raise ValueError("Taskvalet receipt does not match the saved request")
    task_id = task["id"]
    if not isinstance(task_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", task_id
    ):
        raise ValueError("Invalid Taskvalet task receipt")
    return {
        "url": f"{server}/#task={project}/{task_id}",
        "task": task_id,
        "message": "Request submitted; awaiting owner setup and launch.",
    }


def providers() -> list[HandoffProvider]:
    """Register the optional Taskvalet handoff with Glitnir."""
    return [HandoffProvider("taskvalet", "Send to Taskvalet", prepare, submit)]
