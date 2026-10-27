"""Bounded HTTPS transport with private, retryable asynchronous job receipts."""

import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from glitnir.compute.protocol import checker_digest, context_digest, make_request
from glitnir.compute.telemetry import describe
from glitnir.lean.limits import check_seconds, memory_limit_mb

MAX_RESPONSE = 2_500_000


@dataclass(frozen=True)
class Settings:
    """An explicitly trusted server and project; credentials are never represented."""

    url: str
    project: str
    environment: str
    token: str = field(repr=False)
    ca_file: str | None = None

    def __post_init__(self) -> None:
        """Require an origin, scoped identifiers and encrypted remote transport."""
        url = urllib.parse.urlsplit(self.url)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or (
                url.scheme != "https"
                and not (
                    url.scheme == "http"
                    and url.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            )
        ):
            raise ValueError(
                "Taskvalet requires HTTPS (HTTP is allowed only for loopback tests)"
            )
        for value in (self.project, self.environment):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
                raise ValueError(
                    "Taskvalet project and environment must be configured identifiers"
                )
        if (
            not self.token
            or len(self.token) > 256
            or any(c.isspace() for c in self.token)
        ):
            raise ValueError("Configure a Taskvalet computation credential")


def from_environment() -> Settings | None:
    """Read the editor or CLI's explicit destination, never a silent remote fallback."""
    mode = os.environ.get("GLITNIR_EXECUTION_BACKEND", "local")
    if mode == "local":
        return None
    if mode != "taskvalet":
        raise ValueError("Glitnir execution backend must be local or taskvalet")
    return Settings(
        os.environ.get("GLITNIR_TASKVALET_URL", ""),
        os.environ.get("GLITNIR_TASKVALET_PROJECT", ""),
        os.environ.get("GLITNIR_TASKVALET_ENVIRONMENT", ""),
        os.environ.get("GLITNIR_TASKVALET_TOKEN", ""),
        os.environ.get("GLITNIR_TASKVALET_CA_FILE") or None,
    )


def execution_identity() -> dict[str, str] | None:
    """Invalidate saved local/remote results when the selected authority changes."""
    if os.environ.get("GLITNIR_EXECUTION_BACKEND", "local") == "local":
        return None
    return {
        key: os.environ.get(key, "")
        for key in (
            "GLITNIR_EXECUTION_BACKEND",
            "GLITNIR_TASKVALET_URL",
            "GLITNIR_TASKVALET_PROJECT",
            "GLITNIR_TASKVALET_ENVIRONMENT",
        )
    }


def remote_identity(source: Path) -> dict[str, Any] | None:
    """Return the fields a fingerprint of `source` adds under remote execution.

    None for local execution. They go to `environment_fingerprint` as its
    `remote` argument, which `glitnir.lean` cannot compute itself.
    """
    execution = execution_identity()
    if execution is None:
        return None
    return {
        "execution": execution,
        "compute_context": context_digest(source),
        "compute_checker": checker_digest(),
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a credential to a redirected origin."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def save_receipt(path: Path, value: dict[str, Any]) -> None:
    """Replace a private journal atomically, without storing any credential."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


class Client:
    """Submit immediately and retrieve results later through the same scoped API."""

    def __init__(self, settings: Settings) -> None:
        """Bind transport to one server and project without credential redirects."""
        self.settings = settings
        self.base = (
            settings.url.rstrip("/") + f"/api/v1/projects/{settings.project}/compute"
        )
        context = ssl.create_default_context(cafile=settings.ca_file)
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 10,
    ) -> Any:
        """Make a bounded request without echoing credentials or source in errors."""
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.settings.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(req, timeout=timeout_seconds) as response:
                data = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            raise ValueError(
                f"Taskvalet returned HTTP {error.code}; retry the saved request"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OSError(
                "Taskvalet connection failed; retain the job receipt and retry"
            ) from None
        if len(data) > MAX_RESPONSE:
            raise ValueError("Taskvalet response exceeds its size limit")
        return json.loads(data)

    def submit(self, request: dict[str, Any], receipt: Path) -> dict[str, Any]:
        """Persist the exact request before sending it; a retry uses the same UUID."""
        journal = {
            "format": "glitnir.compute-receipt/v1",
            "url": self.settings.url.rstrip("/"),
            "project": self.settings.project,
            "request": request,
        }
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            if any(previous.get(key) != value for key, value in journal.items()):
                raise ValueError(
                    "Receipt belongs to different server, project or source inputs"
                )
        save_receipt(receipt, journal)
        job = self.request("POST", "/jobs", request)
        if (
            not isinstance(job, dict)
            or job.get("id") != request["request_id"]
            or job.get("request") != request
        ):
            raise ValueError("Taskvalet receipt does not match the submitted request")
        save_receipt(receipt, {**journal, "job": job})
        return job

    def wait(
        self,
        identity: str,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] = lambda _: None,
    ) -> dict[str, Any]:
        """Poll from an editor background thread; submit itself never waits for Lean."""
        cancel = cancel or threading.Event()
        deadline = time.monotonic() + 1810
        while time.monotonic() < deadline:
            if cancel.is_set():
                self.request("POST", f"/jobs/{identity}/cancel", {})
                raise ValueError("Taskvalet check cancelled")
            job = self.request("GET", f"/jobs/{identity}")
            state = job.get("state") if isinstance(job, dict) else None
            detail = describe(job) if isinstance(job, dict) else str(state)
            progress(f"Taskvalet job {identity}: {detail}")
            if state in {"completed", "cancelled", "timed_out", "interrupted"}:
                assert isinstance(job, dict)
                return job
            if state not in {"queued", "running"}:
                raise ValueError("Invalid Taskvalet job state")
            cancel.wait(1)
        raise ValueError("Taskvalet polling deadline reached; retain the receipt")


def result_from_job(job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Accept only a completed report bound to this exact immutable request."""
    if (
        job.get("id") != request["request_id"]
        or job.get("request") != request
        or job.get("state") != "completed"
        or not job.get("worker")
    ):
        raise ValueError(
            f"Taskvalet job is not a matching completed check ({job.get('state')})"
        )
    result = job.get("result")
    if (
        not isinstance(result, dict)
        or result.get("status") not in {"checked_snapshot", "unavailable"}
        or not isinstance(result.get("theorems"), list)
    ):
        raise ValueError("Invalid remote checker result")
    if (
        result["status"] == "checked_snapshot"
        and result.get("source_sha256") != request["source_sha256"]
    ):
        raise ValueError("Remote checker result belongs to different source")
    return {
        **result,
        "execution": {
            "kind": "taskvalet",
            "job": job["id"],
            "worker": job["worker"],
            "context_sha256": request["context_sha256"],
            "checker_sha256": request["checker_sha256"],
            "environment": request["environment"],
        },
    }


def check_remote(
    source: Path,
    *,
    expected_sha256: str,
    cancel: threading.Event,
    progress: Callable[[str], None],
    through_line: int | None = None,
    acquired: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Submit a full project check from the editor's existing background workflow."""
    settings = from_environment()
    if settings is None:
        raise ValueError("Remote execution is not configured")
    request = make_request(
        source,
        settings.environment,
        int(check_seconds()),
        memory_limit_mb(),
        through_line,
    )
    if acquired is not None:
        acquired(request["context_sha256"])
    if context_digest(source) != request["context_sha256"]:
        raise ValueError("Local dependency context changed before submission")
    if request["source_sha256"] != expected_sha256:
        raise ValueError("Saved source changed; refresh and retry")
    if cancel.is_set():
        raise ValueError("Taskvalet check cancelled")
    receipt = Path(tempfile.mkdtemp(prefix="glitnir-compute-")) / "receipt.json"
    client = Client(settings)
    progress(f"Submitting Taskvalet job {request['request_id']}; receipt: {receipt}")
    try:
        job = client.submit(request, receipt)
    except (OSError, ValueError) as error:
        raise ValueError(
            f"{error}. Recover this exact request with receipt: {receipt}"
        ) from None
    progress(f"Taskvalet job {job['id']} queued; receipt: {receipt}")
    job = client.wait(job["id"], cancel=cancel, progress=progress)
    journal = json.loads(receipt.read_text())
    save_receipt(receipt, {**journal, "job": job})
    if cancel.is_set():
        raise ValueError("Taskvalet check cancelled")
    if context_digest(source) != request["context_sha256"]:
        raise ValueError(
            "Local dependency context changed while awaiting the remote check"
        )
    result = result_from_job(job, request)
    result["execution"].update(
        server=settings.url.rstrip("/"), project=settings.project
    )
    return result
