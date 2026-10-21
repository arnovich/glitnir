"""Serial prepared-project worker; native proof checking stays sandboxed."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from glitnir.compute.client import Client, save_receipt
from glitnir.compute.protocol import (
    checker_digest,
    context_digest,
    source_path,
    stage_project,
    validate_request,
)
from glitnir.lean.project_checking import check_project


def execute(
    prepared: Path, request: dict[str, Any], scratch: Path, cancel: threading.Event
) -> dict[str, Any]:
    """Run the existing sandboxed checker, preserving exact source/context bindings."""
    try:
        validate_request(request)
        if request["checker_sha256"] != checker_digest():
            raise ValueError("Worker checker implementation does not match the request")
        if cancel.is_set():
            raise ValueError("Computation cancelled")
        staged = stage_project(prepared, request, scratch)
        result = check_project(
            staged,
            expected_sha256=request["source_sha256"],
            cancel=cancel,
            through_line=request.get("through_line"),
        )
        if cancel.is_set():
            raise ValueError("Computation cancelled")
        if (
            context_digest(prepared / source_path(request["path"]))
            != request["context_sha256"]
        ):
            raise ValueError("Prepared dependency context changed during the check")
        return result
    except (OSError, ValueError) as error:
        return {"status": "unavailable", "message": str(error), "theorems": []}


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


class Worker:
    """Pull compatible jobs while keeping the prepared project and checker caches."""

    def __init__(
        self,
        client: Client,
        prepared: Path,
        journal: Path,
        *,
        max_seconds: int = 1800,
        max_memory_mb: int = 8192,
    ) -> None:
        """Bind a serial worker to one prepared project and private journal."""
        if (
            not prepared.is_dir()
            or not 1 <= max_seconds <= 1800
            or not 1024 <= max_memory_mb <= 32768
        ):
            raise ValueError(
                "Worker needs a prepared project and valid resource ceilings"
            )
        self.client, self.prepared, self.journal = (
            client,
            prepared.resolve(),
            journal.resolve(),
        )
        self.identity = str(uuid4())
        self.capabilities = {
            "worker_id": self.identity,
            "environment": client.settings.environment,
            "checker_sha256": checker_digest(),
            "max_seconds": max_seconds,
            "max_memory_mb": max_memory_mb,
        }
        self.journal.mkdir(parents=True, exist_ok=True, mode=0o700)

    def register(self) -> None:
        """Register immutable capabilities under this computation credential."""
        self.client.request("POST", "/workers", self.capabilities)

    def run_once(self, stop: threading.Event) -> bool:
        """Process at most one lease; cancellation or network loss stops computation."""
        leased = self.client.request("POST", f"/workers/{self.identity}/claim", {})
        if leased is None:
            return False
        job, lease = leased["job"], leased["lease"]
        request = job["request"]
        binding = {"worker_id": self.identity, "lease": lease}
        try:
            validate_request(request)
            if (
                request["environment"] != self.capabilities["environment"]
                or request["checker_sha256"] != self.capabilities["checker_sha256"]
                or request["memory_mb"] > self.capabilities["max_memory_mb"]
                or request["timeout_seconds"] > self.capabilities["max_seconds"]
            ):
                raise ValueError("Server dispatched a job outside worker capabilities")
        except (ValueError, TypeError, KeyError) as error:
            self.client.request(
                "POST",
                f"/jobs/{job['id']}/complete",
                {
                    **binding,
                    "result": {
                        "status": "unavailable",
                        "theorems": [],
                        "message": f"Invalid computation request: {error}",
                    },
                },
            )
            return True
        # No rerun after an uncertain/crashed invocation: retain its original directory.
        directory = self.journal / str(uuid4())
        directory.mkdir(mode=0o700)
        payload, output = directory / "request.json", directory / "result.json"
        save_receipt(payload, request)
        binding = {"worker_id": self.identity, "lease": lease}
        save_receipt(
            directory / "lease.json",
            {
                "url": self.client.settings.url.rstrip("/"),
                "project": self.client.settings.project,
                "job": job,
                **binding,
            },
        )
        remaining = max(0, job["deadline"] - time.time())
        deadline = time.monotonic() + min(remaining, request["timeout_seconds"])
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "PATH", "USER", "TMPDIR", "LANG", "LC_ALL"}
        }
        environment.update(
            GLITNIR_EXECUTION_BACKEND="local",
            GLITNIR_ELABORATION_TIMEOUT_MS=str(max(1, int(remaining * 1000))),
            GLITNIR_MEMORY_LIMIT_MB=str(request["memory_mb"]),
        )
        command = [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "glitnir.compute",
            "execute",
            "--prepared",
            str(self.prepared),
            "--request",
            str(payload),
            "--output",
            str(output),
            "--scratch",
            str(self.journal / self.identity / "stage"),
        ]
        process = None
        try:
            with (directory / "stderr.log").open("wb") as errors:
                process = subprocess.Popen(
                    command,
                    env=environment,
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    start_new_session=True,
                )
                heartbeat_at = 0.0
                while process.poll() is None:
                    if stop.is_set() or time.monotonic() >= deadline:
                        _stop(process)
                        return True
                    if time.monotonic() >= heartbeat_at:
                        state = self.client.request(
                            "POST", f"/jobs/{job['id']}/heartbeat", binding
                        )
                        if state["state"] != "running":
                            _stop(process)
                            return True
                        heartbeat_at = time.monotonic() + 10
                    stop.wait(0.1)
            if (
                process.returncode != 0
                or not output.is_file()
                or output.stat().st_size > 1_000_000
            ):
                result = {
                    "status": "unavailable",
                    "message": "Worker exited without a bounded checker result",
                    "theorems": [],
                }
            else:
                result = json.loads(output.read_text())
            save_receipt(directory / "completion.json", {**binding, "result": result})
            self.client.request(
                "POST", f"/jobs/{job['id']}/complete", {**binding, "result": result}
            )
            return True
        finally:
            if process is not None:
                _stop(process)

    def run(self, stop: threading.Event) -> None:
        """Register and poll serially; a transport failure ends this worker safely."""
        self.register()
        while not stop.is_set():
            if not self.run_once(stop):
                stop.wait(2)
