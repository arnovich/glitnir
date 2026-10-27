"""Serial prepared-project worker; native proof checking stays sandboxed."""

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

from glitnir.compute.client import Client, save_receipt
from glitnir.compute.environments import EnvironmentCache, Recipe
from glitnir.compute.protocol import (
    checker_digest,
    context_digest,
    source_path,
    stage_project,
    validate_request,
)
from glitnir.compute.telemetry import (
    MAX_MS,
    native_observations,
)
from glitnir.compute.telemetry import observations as valid_observations
from glitnir.compute.telemetry import supports
from glitnir.lean.environment import kill_and_reap
from glitnir.lean.limits import DEFAULT_MEMORY_LIMIT_MB
from glitnir.lean.project_checking import check_project


def execute(
    prepared: Path,
    request: dict[str, Any],
    scratch: Path,
    cancel: threading.Event,
    *,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the existing sandboxed checker, preserving exact source/context bindings."""
    try:
        validate_request(request)
        if request["checker_sha256"] != checker_digest():
            raise ValueError("Worker checker implementation does not match the request")
        if cancel.is_set():
            raise ValueError("Computation cancelled")
        staging_started = time.monotonic()
        staged = stage_project(prepared, request, scratch)
        if observations is not None:
            observations["staging_ms"] = round(
                (time.monotonic() - staging_started) * 1000
            )
        result = check_project(
            staged,
            expected_sha256=request["source_sha256"],
            cancel=cancel,
            through_line=request.get("through_line"),
        )
        if observations is not None:
            observations.update(native_observations(result))
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
    kill_and_reap(process)


class Worker:
    """Pull compatible jobs while keeping the prepared project and checker caches."""

    def __init__(
        self,
        client: Client,
        prepared: Path | None,
        journal: Path,
        *,
        recipes: list[Recipe] | None = None,
        cache: EnvironmentCache | None = None,
        max_seconds: int = 1800,
        max_memory_mb: int = DEFAULT_MEMORY_LIMIT_MB,
    ) -> None:
        """Bind a serial worker to one prepared project and private journal."""
        if (
            (prepared is None and not recipes)
            or (prepared is not None and (not prepared.is_dir() or recipes is not None))
            or (
                recipes is not None
                and (
                    cache is None
                    or any(r.project != client.settings.project for r in recipes)
                )
            )
            or not 1 <= max_seconds <= 1800
            or not 1024 <= max_memory_mb <= 32768
        ):
            raise ValueError(
                "Worker needs a prepared project and valid resource ceilings"
            )
        self.client, self.prepared, self.journal = (
            client,
            prepared.resolve() if prepared is not None else None,
            journal.resolve(),
        )
        self.recipes = {r.environment: r for r in recipes or []}
        self.cache = cache
        self.identity = str(uuid4())
        self.capabilities: dict[str, Any] = {
            "worker_id": self.identity,
            "environment": client.settings.environment,
            "checker_sha256": checker_digest(),
            "max_seconds": max_seconds,
            "max_memory_mb": max_memory_mb,
        }
        if recipes:
            del self.capabilities["environment"]
            self.capabilities["environments"] = list(self.recipes)
        self.journal.mkdir(parents=True, exist_ok=True, mode=0o700)

    def register(self) -> None:
        """Register immutable capabilities under this computation credential."""
        self.client.request("POST", "/workers", self.capabilities)

    def run_once(self, stop: threading.Event) -> bool:
        """Process at most one lease; cancellation or network loss stops computation."""
        hints = (
            {"ready_environments": self.cache.ready(list(self.recipes.values()))}
            if self.cache
            else {}
        )
        leased = self.client.request("POST", f"/workers/{self.identity}/claim", hints)
        if leased is None:
            return False
        job, lease = leased["job"], leased["lease"]
        request = job["request"]
        binding = {"worker_id": self.identity, "lease": lease}
        try:
            validate_request(request)
            if (
                request["environment"]
                not in (list(self.recipes) or [self.capabilities["environment"]])
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
            if key in {"HOME", "PATH", "USER", "TMPDIR", "LANG", "LC_ALL", "ELAN_HOME"}
        }
        environment.update(
            GLITNIR_EXECUTION_BACKEND="local",
            GLITNIR_ELABORATION_TIMEOUT_MS=str(max(1, int(remaining * 1000))),
            GLITNIR_MEMORY_LIMIT_MB=str(request["memory_mb"]),
        )
        phase = "preparing" if self.cache else "checking"
        telemetry_enabled = supports(job)
        telemetry_live = telemetry_enabled
        started = time.monotonic()
        checking_started = None if self.cache else started
        cache_observation = "unknown" if self.cache else "prepared"
        checker_observations: dict[str, Any] = {}
        heartbeat_at = 0.0
        budget_at = 0.0
        recipe = self.recipes.get(request["environment"])

        def measurements() -> dict[str, Any]:
            now = time.monotonic()
            value: dict[str, Any] = {
                "elapsed_ms": min(MAX_MS, max(0, round((now - started) * 1000))),
                "preparation_ms": (
                    min(
                        MAX_MS,
                        max(
                            0,
                            round(
                                (
                                    (
                                        checking_started
                                        if checking_started is not None
                                        else now
                                    )
                                    - started
                                )
                                * 1000
                            ),
                        ),
                    )
                    if self.cache
                    else 0
                ),
                "environment_cache": cache_observation,
                **checker_observations,
            }
            if checking_started is not None:
                value["checking_ms"] = min(
                    MAX_MS, max(0, round((now - checking_started) * 1000))
                )
            return value

        def remember(value: dict[str, Any]) -> None:
            try:
                save_receipt(directory / "worker-observations.json", value)
            except (OSError, ValueError):
                pass  # Optional measurements cannot discard a checker result.

        def publish() -> None:
            nonlocal telemetry_live
            if not telemetry_live:
                return
            value = measurements()
            remember(value)
            try:
                self.client.request(
                    "POST",
                    f"/jobs/{job['id']}/telemetry",
                    {**binding, "observations": value},
                    timeout_seconds=max(0.01, min(1.0, deadline - time.monotonic())),
                )
            except (OSError, ValueError):
                telemetry_live = False

        def monitor() -> None:
            nonlocal heartbeat_at, budget_at
            now = time.monotonic()
            if stop.is_set() or now >= deadline:
                raise InterruptedError("Computation cancelled or deadline reached")
            if now >= heartbeat_at:
                body = {
                    **binding,
                    **({"phase": phase} if self.cache or telemetry_enabled else {}),
                }
                state = self.client.request(
                    "POST", f"/jobs/{job['id']}/heartbeat", body
                )
                if state["state"] != "running":
                    raise InterruptedError("Computation lease is no longer running")
                heartbeat_at = time.monotonic() + 10
                publish()
            if now >= budget_at and self.cache and recipe and self.cache.active_entry:
                self.cache.check_budget(self.cache.active_entry, recipe)
                budget_at = now + 1

        def run(
            command: list[str], cwd: Path, execution_env: dict[str, str] | None = None
        ) -> str:
            monitor()
            # Build code is operator-trusted; remove ambient credentials/configuration.
            private = (
                self.cache.active_entry
                if self.cache and self.cache.active_entry
                else directory
            ) / "build-home"
            private.mkdir(exist_ok=True, mode=0o700)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(private),
                "TMPDIR": str(private),
                "LANG": "C.UTF-8",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
            if execution_env is not None:
                env = execution_env
            command_file, command_result = (
                directory / "command.json",
                directory / "command-result.json",
            )
            command_result.unlink(missing_ok=True)
            save_receipt(
                command_file,
                {
                    "command": command,
                    "parent": os.getpid(),
                    "result": str(command_result),
                },
            )
            log_name = "checker.log" if execution_env is not None else "build.log"
            with (directory / log_name).open("w+b") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-m",
                        "glitnir.compute.build_process",
                        str(command_file),
                    ],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(
                        (self.cache.lock_fd,)
                        if self.cache and self.cache.lock_fd is not None
                        else ()
                    ),
                )
                try:
                    while process.poll() is None:
                        monitor()
                        if log.tell() > 10_000_000:
                            raise ValueError("Preparation output exceeds limit")
                        stop.wait(0.1)
                    monitor()
                    if (
                        not command_result.is_file()
                        or json.loads(command_result.read_text()).get("returncode") != 0
                    ):
                        raise ValueError(
                            "Computation command failed; see worker journal"
                        )
                finally:
                    _stop(process)
                log.seek(0)
                data = log.read(10_000_001)
                if len(data) > 10_000_000:
                    raise ValueError("Preparation output exceeds limit")
                return data.decode(errors="replace")

        try:
            with ExitStack() as stack:
                prepared = self.prepared
                scratch = self.journal / self.identity / "stage"
                if self.cache and recipe:
                    entry = stack.enter_context(
                        self.cache.prepare(recipe, run, monitor)
                    )
                    prepared, scratch = entry / "project", entry / "stage"
                    reused = getattr(self.cache, "active_reused", None)
                    cache_observation = (
                        "hit"
                        if reused is True
                        else "miss" if reused is False else "unknown"
                    )
                    checking_started = time.monotonic()
                assert prepared is not None
                phase, heartbeat_at = "checking", 0.0
                monitor()
                environment["GLITNIR_ELABORATION_TIMEOUT_MS"] = str(
                    max(1, int((deadline - time.monotonic()) * 1000))
                )
                command = [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "glitnir.compute",
                    "execute",
                    "--prepared",
                    str(prepared),
                    "--request",
                    str(payload),
                    "--output",
                    str(output),
                    "--scratch",
                    str(scratch),
                ]
                if telemetry_enabled:
                    command.extend(
                        ["--observations", str(directory / "checker-observations.json")]
                    )
                run(command, directory, environment)
                if not output.is_file() or output.stat().st_size > 1_000_000:
                    raise ValueError("Worker exited without a bounded checker result")
                result = json.loads(output.read_text())
                measured = directory / "checker-observations.json"
                try:
                    if (
                        telemetry_enabled
                        and measured.is_file()
                        and measured.stat().st_size <= 16_384
                    ):
                        value = valid_observations(json.loads(measured.read_text()))
                        if value is not None:
                            checker_observations.update(
                                {
                                    k: v
                                    for k, v in value.items()
                                    if k
                                    in {
                                        "staging_ms",
                                        "native_stage_ms",
                                        "imports_replay",
                                    }
                                }
                            )
                except (OSError, ValueError):
                    pass
        except InterruptedError:
            return True
        except ValueError as error:
            result = {"status": "unavailable", "message": str(error), "theorems": []}
        completion = {**binding, "result": result}
        if telemetry_enabled:
            value = valid_observations(measurements())
            if value is not None:
                remember(value)
                completion["observations"] = value
        save_receipt(directory / "completion.json", completion)
        self.client.request("POST", f"/jobs/{job['id']}/complete", completion)
        return True

    def run(self, stop: threading.Event) -> None:
        """Register and poll serially; a transport failure ends this worker safely."""
        self.register()
        while not stop.is_set():
            if not self.run_once(stop):
                stop.wait(2)


def run_workers(workers: list[Worker], stop: threading.Event) -> None:
    """Poll projects round-robin without overlapping leases or execution."""
    for worker in workers:
        worker.register()
    while not stop.is_set():
        worked = False
        for worker in workers:
            if stop.is_set():
                break
            worked = worker.run_once(stop) or worked
        if not worked:
            stop.wait(2)
