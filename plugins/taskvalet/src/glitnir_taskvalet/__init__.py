"""Source-bound Taskvalet requests and separately scoped explicit launch controls."""

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


def _bound_request(value: dict[str, Any], token: str) -> tuple[str, str]:
    """Verify the original actor, credential, destination and storage identity."""
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
    return server, project


def submit(value: dict[str, Any], token: str) -> dict[str, Any]:
    """Submit or recover the exact saved create envelope, never a campaign."""
    server, project = _bound_request(value, token)
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
        "stage": task.get("stage", "unknown"),
        "message": "Request submitted; awaiting owner setup and launch.",
    }


def status(value: dict[str, Any], token: str) -> dict[str, Any]:
    """Read one submitted request task; never inspect or launch a campaign."""
    task_id = value["task"]
    if not isinstance(task_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", task_id
    ):
        raise ValueError("Invalid request task identifier")
    server, project = _bound_request(value["request"], token)
    task = _request(
        server, project, token, "GET", f"/api/v1/projects/{project}/tasks/{task_id}"
    )
    if task.get("id") != task_id or task.get("project") != project:
        raise ValueError("Taskvalet returned a different request task")
    stages = {"new", "ready", "ongoing", "needs_input", "review", "finished"}
    stage = task.get("stage", "unknown")
    result: dict[str, Any] = {
        "task": task_id,
        "stage": stage if stage in stages else "unknown",
    }
    if value.get("results") is True:
        result["results"] = _research_results(
            server, project, task_id, value["request"], token
        )
    return result


def _research_results(
    server: str, project: str, task: str, request: dict[str, Any], token: str
) -> dict[str, Any]:
    """Project bounded, source-bound research as untrusted review text, never proof."""
    try:
        view = _request(
            server,
            project,
            token,
            "GET",
            f"/api/v1/projects/{project}/tasks/{task}/research-results",
        )
    except ValueError:
        return {
            "availability": "unavailable",
            "message": (
                "Research results could not be read. The server may need the "
                "request-link update, or this project/credential may not "
                "support research. Open Taskvalet for details."
            ),
        }
    if view.get("project") != project or view.get("task") != task:
        raise ValueError("Research results belong to another request")
    investigations = view.get("investigations")
    if not isinstance(investigations, list) or len(investigations) > 20:
        raise ValueError("Invalid research results")
    expected = hashlib.sha256(request["command"]["text"].strip().encode()).hexdigest()
    sections: list[dict[str, Any]] = []
    identities: list[str] = []
    remaining = 50_000
    has_findings = False
    for investigation in investigations:
        problem = investigation["problem"]
        origin = problem["data"].get("origin", {})
        if (
            investigation.get("project") != project
            or origin.get("task") != task
            or origin.get("context_sha256") != expected
        ):
            raise ValueError(
                "Research origin does not match the submitted request context"
            )
        problem_id = str(uuid.UUID(problem["id"]))
        for group in ("summaries", "findings", "verifications", "history"):
            records = investigation.get(group, [])
            if not isinstance(records, list):
                raise ValueError("Invalid research record list")
            for record in records:
                if record.get("problem") != problem_id:
                    raise ValueError("Research record belongs to another problem")
                data = record["data"]
                if group in {"summaries", "verifications"} or (
                    group == "findings"
                    and data.get("kind") not in {"direction", "note"}
                ):
                    has_findings = True
                text = (
                    data.get("text")
                    if group != "history"
                    else "\n".join(
                        str(data.get(key, ""))
                        for key in ("title", "statement", "assumptions")
                    )
                )
                text = str(text or "")
                for artifact in data.get("artifacts", []):
                    text += (
                        "\nArtifact: "
                        + str(artifact.get("uri", ""))
                        + "\nPinned revision: "
                        + str(artifact.get("revision", ""))
                    )
                if data.get("references"):
                    text += "\nReferences: " + ", ".join(
                        str(reference) for reference in data["references"]
                    )
                if data.get("supersedes"):
                    text += "\nSupersedes finding: " + str(data["supersedes"])
                report = record.get("compute_report")
                if isinstance(report, dict):
                    text += "\nWorker compute evidence: " + (
                        "report satisfies the server evidence gate"
                        if report.get("passed") is True
                        else "not established"
                    )
                    text += (
                        "\nThis is worker evidence, not an independent "
                        "local proof check."
                    )
                    for issue in report.get("issues", []):
                        text += "\nLimitation: " + str(issue)
                identities.append(str(record["id"]) + ":" + str(record["version"]))
                if remaining <= 0 or len(sections) >= 100:
                    continue
                excerpt = text[: min(remaining, 10_000)]
                remaining -= len(excerpt)
                sections.append(
                    {
                        "kind": str(data.get("kind", record["kind"])),
                        "investigation": str(
                            problem["data"].get("title", "Investigation")
                        )[:240],
                        "author": str(record.get("author", ""))[:200],
                        "version": record["problem_version"],
                        "problem": problem_id,
                        "text": excerpt,
                        "truncated": len(excerpt) < len(text),
                    }
                )
        for assignment in investigation.get("assignments", [])[:40]:
            if assignment.get("problem") != problem_id:
                raise ValueError("Assignment belongs to another problem")
            current = assignment.get("task", {})
            if len(sections) < 100:
                sections.append(
                    {
                        "kind": "assignment",
                        "author": str(current.get("agent") or "Unassigned")[:200],
                        "version": assignment["problem_version"],
                        "problem": problem_id,
                        "text": str(current.get("title", ""))[:240]
                        + " — "
                        + str(current.get("stage", "unknown"))[:50],
                    }
                )
    return {
        "availability": "available",
        "sections": sections,
        "digest": hashlib.sha256("\n".join(sorted(identities)).encode()).hexdigest(),
        "hasFindings": has_findings,
        "truncated": remaining <= 0 or len(sections) >= 100,
        "message": (
            "No investigation is linked yet."
            if not investigations
            else (
                "Research statements and findings are proposals. Independent local "
                "checking remains the proof authority. Campaign controls and any "
                "reported resource usage remain on the board."
            )
        ),
    }


def launch_review(value: dict[str, Any], token: str) -> dict[str, Any]:
    """Inspect one scoped team/environment without admitting or retrying any work."""
    saved = value["request"]
    server, project = _destination(saved)
    task = value["task"]
    if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task):
        raise ValueError("Invalid saved request")
    route = f"/api/v1/projects/{project}/notebook-launch"
    response = _request(server, project, token, "GET", route)
    _launch_binding(response, value)
    if response.get("campaign_id") or response.get("status") == "cancelled":
        return {
            **launch_control(
                {
                    **value,
                    "operation": "status",
                    "launch": {"scope_id": response["scope_id"]},
                },
                token,
            ),
            "team": "Prepared team",
            "team_version": response["team_version"],
            "environment": response["environment"],
            "max_seconds": response["max_seconds"],
            "max_workers": response["max_workers"],
            "budget_micro_usd": response.get("budget_micro_usd"),
            "compute_available": None,
            "blockers": [
                "This scope already has a launch decision. Recover status or stop it."
            ],
        }
    response = _request(server, project, token, "POST", route + "/preview", {})
    if (
        response.get("format") != "taskvalet.notebook-launch/v1"
        or response.get("task") != task
        or response.get("source_sha256") != saved["source_sha256"]
        or response.get("storage_id") != saved["storage_id"]
        or response.get("context_sha256")
        != hashlib.sha256(saved["command"]["text"].strip().encode()).hexdigest()
    ):
        raise ValueError("Launch scope binding differs from this notebook request")
    blockers = response.get("blockers")
    if (
        not isinstance(blockers, list)
        or len(blockers) > 20
        or not all(isinstance(item, str) and len(item) <= 2000 for item in blockers)
    ):
        raise ValueError("Invalid launch readiness")
    for key in ("team", "environment", "status"):
        if not isinstance(response.get(key), str) or len(response[key]) > 200:
            raise ValueError("Invalid launch display field")
    for key in ("team_version", "max_seconds", "max_workers"):
        if type(response.get(key)) is not int or not 1 <= response[key] <= 3600:
            raise ValueError("Invalid launch limits")
    available = response.get("compute_available")
    if available is not None and (
        type(available) is not int or not 0 <= available <= 1000
    ):
        raise ValueError("Invalid compute readiness")
    ready = response.get("ready") is True
    executable: dict[str, Any] = {"scope_id": str(uuid.UUID(response["scope_id"]))}
    if ready:
        import math

        budget, expires = response.get("budget_micro_usd"), response.get(
            "preview_expires_at"
        )
        if (
            blockers
            or type(budget) is not int
            or not 0 < budget <= 10**15
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
        ):
            raise ValueError("Invalid executable launch limits")
        executable = {
            "scope_id": str(uuid.UUID(response["scope_id"])),
            "preview_id": str(uuid.UUID(response["preview_id"])),
            "preview_expires_at": expires,
            "budget_micro_usd": budget,
        }
    return {
        **executable,
        **{
            key: response[key]
            for key in (
                "team",
                "team_version",
                "environment",
                "max_seconds",
                "max_workers",
                "status",
            )
        },
        "compute_available": available,
        "ready": ready,
        "blockers": blockers,
    }


def _launch_binding(response: dict[str, Any], value: dict[str, Any]) -> None:
    """Bind every observation to the saved request, independently of editor state."""
    saved = value["request"]
    if (
        response.get("format") != "taskvalet.notebook-launch/v1"
        or response.get("task") != value["task"]
        or response.get("source_sha256") != saved["source_sha256"]
        or response.get("storage_id") != saved["storage_id"]
        or response.get("context_sha256")
        != hashlib.sha256(saved["command"]["text"].strip().encode()).hexdigest()
    ):
        raise ValueError("Launch scope binding differs from this notebook request")


def launch_control(value: dict[str, Any], token: str) -> dict[str, Any]:
    """Execute only explicit start/stop; status recovery never reissues admission."""
    server, project = _destination(value["request"])
    operation = value["operation"]
    if operation not in {"start", "status", "stop"}:
        raise ValueError("Unknown launch operation")
    route = f"/api/v1/projects/{project}/notebook-launch"
    response = _request(server, project, token, "GET", route)
    _launch_binding(response, value)
    saved = value["launch"]
    scope = str(uuid.UUID(saved["scope_id"]))
    if response.get("scope_id") != scope:
        raise ValueError(
            "Launch credential scope changed; preserve the original intent"
        )
    if operation != "status":
        body = (
            {"preview_id": str(uuid.UUID(saved["preview_id"]))}
            if operation == "start"
            else {}
        )
        response = _request(
            server, project, token, "POST", route + "/" + operation, body
        )
        if (
            response.get("scope_id") != scope
            or response.get("task") != value["task"]
            or response.get("format") != "taskvalet.notebook-launch/v1"
        ):
            raise ValueError("Launch receipt scope changed")
        if operation == "start":
            _launch_binding(response, value)
    if response.get("status") not in {
        "not_started",
        "admission_unknown",
        "admitted",
        "running",
        "stopping",
        "stopped",
        "failed",
        "cancelled",
        "unknown",
        "cleanup_pending",
    }:
        raise ValueError("Unknown launch status")
    if response.get("cleanup") not in {"not_started", "pending", "complete", "unknown"}:
        raise ValueError("Unknown cleanup status")
    campaign = response.get("campaign_id")
    if campaign is not None:
        campaign = str(uuid.UUID(campaign))
    exposure = response.get("exposure_micro_usd")
    if exposure is not None and (
        type(exposure) is not int or not 0 <= exposure <= 10**15
    ):
        raise ValueError("Invalid launch cost observation")
    return {
        "scope_id": scope,
        "campaign_id": campaign,
        "ready": False,
        "status": response["status"],
        "cleanup": response["cleanup"],
        "can_stop": response.get("can_stop") is True,
        "exposure_micro_usd": exposure,
    }


def providers() -> list[HandoffProvider]:
    """Register the optional Taskvalet handoff with Glitnir."""
    return [
        HandoffProvider(
            "taskvalet",
            "Send to Taskvalet",
            prepare,
            submit,
            status,
            launch_review,
            launch_control,
        )
    ]
