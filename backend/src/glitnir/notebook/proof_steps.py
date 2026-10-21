"""One cancellable, historical proof observation owned by an editor preview."""

import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from glitnir.lean.project import describe_project
from glitnir.lean.project_inspection import lean_support
from glitnir.lean.proof_trace import trace_file


class ProofSteps:
    """Run Lean only for explicit requests; keep no independent proof status."""

    def __init__(self, path: Path, *, timeout_ms: int | None = None) -> None:
        """Create an idle observer without starting a worker."""
        self.path = path
        self._timeout_ms = timeout_ms
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._result: dict[str, Any] = {"status": "idle", "nodes": []}

    def snapshot(self) -> dict[str, Any]:
        """Return the most recent observation, including its immutable identity."""
        with self._lock:
            return dict(self._result)

    def start(self, digest: str) -> dict[str, Any]:
        """Start at most one bounded observation at a time."""
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Send the saved source sha256.")
        # Refused before any worker, slot or Lean run, as every Lean request.
        support = lean_support(describe_project(self.path))
        if not support["supported"]:
            raise ValueError(support["message"])
        with self._lock:
            if self._closed:
                raise ValueError("This preview is closed.")
            if self._worker is not None and self._worker.is_alive():
                raise ValueError(
                    "A proof observation is running. Cancel or wait first."
                )
            self._cancel = threading.Event()
            identity = uuid4().hex
            self._result = {
                "id": identity,
                "status": "running",
                "source_sha256": digest,
                "nodes": [],
                "message": "Preparing proof steps",
            }
            self._worker = threading.Thread(
                target=self._work, args=(digest, identity), daemon=False
            )
            self._worker.start()
            return dict(self._result)

    def _work(self, digest: str, identity: str) -> None:
        def progress(message: str) -> None:
            with self._lock:
                self._result = {**self._result, "message": message}

        try:
            result = trace_file(
                self.path,
                expected_sha256=digest,
                timeout_ms=self._timeout_ms,
                cancel=self._cancel,
                progress=progress,
            )
        except Exception as error:
            result = {"status": "unavailable", "nodes": [], "message": str(error)}
        with self._lock:
            self._result = {
                **result,
                "id": identity,
                "source_sha256": digest,
            }
            if self._cancel.is_set():
                self._result.update(
                    status="cancelled", nodes=[], message="Proof observation cancelled."
                )

    def cancel(self) -> None:
        """Request cancellation; never start a replacement while cleanup runs."""
        self._cancel.set()

    def close(self) -> None:
        """Cancel the process before deleting its private snapshot."""
        with self._lock:
            self._closed = True
            self._cancel.set()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=55)
