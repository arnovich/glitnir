"""One confined imported Lean process for the offline advisory experiment."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from functools import partial
from typing import Any

from experiments.warm_pool.environment import FrozenEnvironment
from experiments.warm_pool.runtime import Environment, Interrupted, Job, Limits
from glitnir.lean.environment import bind_to_parent, kill_and_reap
from glitnir.lean.sandbox import confine

MAX_REPLY = 1_000_000


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Interrupted("ambiguous duplicate response key")
        result[key] = value
    return result


def process_rss_kb(parent: int) -> int:
    """Sample descendants without reading command arguments or environment secrets."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,rss="],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise Interrupted("memory observer failed") from error
    entries = [tuple(map(int, line.split())) for line in result.stdout.splitlines()]
    selected = {parent}
    for _ in range(8):
        expanded = selected | {pid for pid, owner, _ in entries if owner in selected}
        if expanded == selected:
            break
        selected = expanded
    return sum(rss for pid, _, rss in entries if pid in selected)


class LeanSession:
    """A serial diagnostic process; reports cannot set a native proof verdict."""

    def __init__(
        self, environment: Environment, limits: Limits, generation: int
    ) -> None:
        """Keep only frozen operator inputs and small fixed experimental limits."""
        if not isinstance(environment, FrozenEnvironment):
            raise ValueError("The real session requires a native frozen environment")
        self.environment = environment
        self.limits, self.generation = limits, generation
        self.requests, self.closed = 0, False
        self.process: subprocess.Popen[bytes] | None = None
        self._selector = selectors.DefaultSelector()
        self._buffer, self._errors = bytearray(), bytearray()
        self._last_memory, self.peak_rss_kb = 0.0, 0
        self.startup_ms = 0.0

    def _start(self, request: Job) -> None:
        started = time.monotonic()
        frozen = self.environment.snapshot
        command = confine(
            [
                str(frozen.toolchain / "bin/lean"),
                f"-M{self.limits.memory_mb}",
                "-j2",
                "--run",
                str(self.environment.program),
                str(self.environment.setup_file),
                str(frozen.source),
            ],
            readable=[frozen.root, *set(frozen.stores.values())],
            cwd=frozen.root,
            required=True,
        )
        self.process = subprocess.Popen(
            command,
            cwd=frozen.root,
            env={
                "PATH": f"{frozen.toolchain / 'bin'}:/usr/bin:/bin",
                "LEAN_SYSROOT": str(frozen.toolchain),
                "LEAN_PATH": "",
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=partial(bind_to_parent, os.getpid(), MAX_REPLY),
        )
        assert self.process.stdin and self.process.stdout and self.process.stderr
        for stream, label in (
            (self.process.stdout, "out"),
            (self.process.stderr, "error"),
        ):
            os.set_blocking(stream.fileno(), False)
            self._selector.register(stream, selectors.EVENT_READ, label)
        os.set_blocking(self.process.stdin.fileno(), False)
        if self._read(request) != {"ready": True}:
            raise Interrupted("worker did not confirm its fixed protocol")
        self.startup_ms = (time.monotonic() - started) * 1000

    def _poll(self, request: Job) -> None:
        request.remaining()
        assert self.process is not None
        now = time.monotonic()
        if now - self._last_memory >= 0.1:
            self.peak_rss_kb = max(self.peak_rss_kb, process_rss_kb(self.process.pid))
            self._last_memory = time.monotonic()
            if self.peak_rss_kb > self.limits.memory_mb * 1024:
                raise Interrupted("sampled process-tree memory ceiling exceeded")
        if self.process.poll() is not None:
            reason = self._errors.decode(errors="replace")[-1500:]
            raise Interrupted("process exited before a complete reply: " + reason)

    def _read(self, request: Job) -> dict[str, Any]:
        while True:
            if b"\n" in self._buffer:
                line, remainder = self._buffer.split(b"\n", 1)
                self._buffer = bytearray(remainder)
                try:
                    value = json.loads(line, object_pairs_hook=_unique)
                except (ValueError, RecursionError) as error:
                    raise Interrupted(
                        "worker returned malformed diagnostic JSON"
                    ) from error
                if not isinstance(value, dict):
                    raise Interrupted("worker returned a non-object reply")
                return value
            self._poll(request)
            for key, _ in self._selector.select(min(0.05, request.remaining())):
                data = os.read(key.fd, 65536)
                if not data:
                    self._selector.unregister(key.fileobj)
                    if key.data == "out":
                        reason = self._errors.decode(errors="replace")[-1500:]
                        raise Interrupted(
                            "worker closed its response stream: " + reason
                        )
                elif key.data == "out":
                    self._buffer.extend(data)
                else:
                    self._errors.extend(data)
                if len(self._buffer) + len(self._errors) > MAX_REPLY:
                    raise Interrupted("worker response/output ceiling exceeded")

    def _send(self, payload: bytes, request: Job) -> None:
        assert self.process and self.process.stdin
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdin, selectors.EVENT_WRITE)
        try:
            position = 0
            while position < len(payload):
                self._poll(request)
                if selector.select(min(0.05, request.remaining())):
                    position += os.write(
                        self.process.stdin.fileno(),
                        payload[position : position + 65536],
                    )
        finally:
            selector.close()

    def check(self, request: Job) -> dict[str, Any]:
        """Reset the command state in Lean, not process-wide mutable tactic state."""
        if self.closed:
            raise Interrupted("worker is closed")
        startup = self.process is None
        if startup:
            self._start(request)
        payload = (
            json.dumps(
                {"id": request.identity, "source": request.source}, ensure_ascii=False
            ).encode()
            + b"\n"
        )
        self._send(payload, request)
        self.requests += 1
        if self._read(request) != {"started": request.identity}:
            raise Interrupted("worker did not acknowledge this exact request")
        request.started.set()
        response = self._read(request)
        if (
            set(response) != {"id", "status", "errors", "warnings", "elapsed_ms"}
            or response.get("id") != request.identity
            or response.get("status") not in {"elaborated", "errors"}
            or type(response.get("elapsed_ms")) is not int
            or response["elapsed_ms"] < 0
            or any(
                not isinstance(response.get(k), list)
                or any(not isinstance(x, str) for x in response[k])
                for k in ("errors", "warnings")
            )
        ):
            raise Interrupted("worker diagnostic receipt differs from this request")
        if (response["status"] == "errors") != bool(response["errors"]):
            raise Interrupted("worker diagnostic status contradicts its errors")
        result = {
            **response,
            "startup_ms": self.startup_ms if startup else 0.0,
            "peak_sampled_rss_kb": self.peak_rss_kb,
            "recycle_reason": (
                "memory_highwater"
                if self.peak_rss_kb >= self.limits.memory_mb * 1024 * 9 // 10
                else ""
            ),
        }
        if result["recycle_reason"]:
            self.close()
        return result

    def close(self) -> None:
        """Terminate and reap the owned process and close its streams."""
        self.closed = True
        if self.process is not None:
            kill_and_reap(self.process)
            for stream in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                if stream:
                    stream.close()
            self.process = None
        self._selector.close()
