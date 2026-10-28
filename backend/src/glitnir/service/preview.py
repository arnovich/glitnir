"""Saved-source previews with explicitly requested checks, evidence and decisions."""

import hashlib
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from glitnir.compute.client import check_remote, from_environment, remote_identity
from glitnir.lean.environment import MAX_SOURCE_BYTES
from glitnir.lean.fingerprint import (
    Fingerprint,
    FingerprintRecord,
    environment_fingerprint,
    unknown_git_note,
)
from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_checking import check_project, through_line_end
from glitnir.lean.project_inspection import Inspection, inspect_project, lean_support
from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.results import CheckOutcome, with_message
from glitnir.notebook.check_records import (
    load_records,
    records_from_check,
    save_records,
)
from glitnir.notebook.document import (
    NotebookError,
)
from glitnir.preview import assets
from glitnir.preview.linked import (
    project_root,
    read_linked_notebook,
    unbuilt_sibling_imports,
)
from glitnir.preview.snapshot import (
    CheckState,
    Fingerprints,
    InspectionSummary,
    PreviewState,
    Snapshot,
    build_snapshot,
    cells,
    cut_short,
    prose,
    read_source,
    uncompared,
)
from glitnir.providers import Providers, load_providers
from glitnir.service.context import CLOSED, PreviewContext
from glitnir.service.proof_steps import ProofSteps
from glitnir.service.runs import Candidate, DirectiveRuns
from glitnir.service.views import ViewDrawing

# How long closing waits for every worker together before giving up on them.
CLOSE_SECONDS = 55
INSPECTING = "Inspecting the saved project setup"
BUSY = "Project inspection or checking is already running."
NOTHING_INSPECTING = "No inspection is running."
NOTHING_CHECKING = "No check is running."
INSPECTION_CANCELLED = "Project inspection cancelled."


def _inspection_summary(result: Inspection, sha256: str) -> InspectionSummary:
    """Keep what the preview shows of an inspection; never the setup itself.

    The setup lists every module of the import closure (megabytes with
    Mathlib) and the resolution the bytes of each configuration file it read,
    so only the message and the packages are kept for the snapshot.
    """
    summary: InspectionSummary = {
        "status": str(result.get("status", "unavailable")),
        "source_sha256": sha256,
        "message": str(result.get("message", "")),
    }
    resolution = result.get("resolution")
    if isinstance(resolution, dict):
        summary["resolution"] = {
            "kind": resolution.get("kind"),
            "packages": resolution.get("packages", []),
        }
    return summary


def _check_state(sha256: str, covered: int | None, result: CheckOutcome) -> CheckState:
    """Return the service's view of a finished check: the result, lines covered."""
    if result["status"] == "checked_snapshot":
        return {"source_sha256": sha256, "through_line": covered, **result}
    return {"source_sha256": sha256, "through_line": covered, **result}


class SourcePreviewService:
    """Preview source; inspect and check confined projects only on request."""

    def __init__(
        self,
        path: Path,
        *,
        providers: Callable[[], Providers] | None = None,
        save_drawings: bool = True,
    ) -> None:
        """Keep only the file location; no workers or processes are started.

        Args:
            path: The notebook to preview.
            providers: Where to get installed providers; this service loads
                them itself when None.
            save_drawings: Save each finished drawing in the notebook's
                records; a static export (which writes nothing) passes False.
        """
        self.path = path
        self._shared_providers = providers
        self.proof_steps = ProofSteps(path)
        self._cancel = threading.Event()
        self._inspection_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._check: CheckState | None = None
        self._check_cancel: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._inspection: InspectionSummary | None = None
        self._inspection_worker: threading.Thread | None = None
        self._inspection_cancel: threading.Event | None = None
        self._providers_lock = threading.Lock()
        self._loaded_providers: Providers | None = None
        # Every read-modify-write of the records file, so none drops another's.
        self._records_lock = threading.Lock()
        context = PreviewContext(
            path=path,
            cancel=self._cancel,
            state_lock=self._state_lock,
            records_lock=self._records_lock,
            providers=self._providers,
            text=self._text,
            fingerprint=self._fingerprint,
            snapshot=self._snapshot,
            unsupported=self._unsupported,
        )
        self._views = ViewDrawing(context, save_drawings=save_drawings)
        self._runs = DirectiveRuns(context)

    def _providers(self) -> Providers:
        """Load installed providers once, when a directive first needs them."""
        if self._shared_providers is not None:
            return self._shared_providers()
        with self._providers_lock:
            if self._loaded_providers is None:
                self._loaded_providers = load_providers()
            return self._loaded_providers

    def _unsupported(self) -> str | None:
        """Why Lean cannot run for this notebook's project, or None if it can.

        Every Lean request (inspection, check, view, evidence and decision
        runs; `ProofSteps.start` too) is refused with these words up front,
        before it takes a gate or a slot, starts a worker or runs Lake.
        """
        message: str | None = lean_support(describe_project(self.path))["message"]
        return message

    def close(self) -> None:
        """Cancel requested work and wait briefly for worker cleanup."""
        self.proof_steps.close()
        with self._state_lock:
            self._cancel.set()
            self._runs.cancel()
            self._views.cancel()
            if self._inspection_cancel is not None:
                self._inspection_cancel.set()
            if self._check_cancel is not None:
                self._check_cancel.set()
            workers = [self._worker, self._inspection_worker]
        deadline = time.monotonic() + CLOSE_SECONDS
        for worker in workers:
            if worker is not None:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self._runs.close(deadline)
        self._views.close(deadline)

    def inspect(self, sha256: str) -> InspectionSummary:
        """Start inspecting the saved source's project setup; return its state.

        The inspection runs in a worker, one at a time and never beside a
        check, and is reported through snapshots as `preview.inspection`, so
        no request waits for Lake. It is bounded as a whole by the check time
        limit and stopped by `cancel_inspection`. Nothing about proof is
        retained.
        """
        if self._cancel.is_set():
            raise NotebookError(CLOSED)
        unsupported = self._unsupported()
        if unsupported is not None:
            raise NotebookError(unsupported)
        if not self._inspection_lock.acquire(blocking=False):
            raise NotebookError(BUSY)
        with self._state_lock:
            if self._cancel.is_set():
                self._inspection_lock.release()
                raise NotebookError(CLOSED)
            self._inspection = {
                "status": "running",
                "source_sha256": sha256,
                "message": INSPECTING,
            }
            cancel = threading.Event()
            self._inspection_cancel = cancel
            self._inspection_worker = threading.Thread(
                target=self._inspect_work, args=(sha256, cancel), daemon=False
            )
            try:
                self._inspection_worker.start()
            except RuntimeError as error:
                self._inspection = {
                    "status": "unavailable",
                    "source_sha256": sha256,
                    "message": str(error),
                }
                self._inspection_worker = None
                self._inspection_cancel = None
                self._inspection_lock.release()
            return self._inspection.copy()

    def cancel_inspection(self) -> None:
        """Stop the running inspection; it then reports that it was cancelled.

        Raises:
            SnapshotError: No inspection is running.
        """
        with self._state_lock:
            if self._inspection_cancel is None:
                raise SnapshotError(NOTHING_INSPECTING)
            self._inspection_cancel.set()

    def _inspect_work(self, sha256: str, cancel: threading.Event) -> None:
        """Inspect, keep the summary for snapshots, and always open the gate."""
        try:
            result = _inspection_summary(
                inspect_project(self.path, expected_sha256=sha256, cancel=cancel),
                sha256,
            )
            if cancel.is_set() and not self._cancel.is_set():
                result = {**result, "message": INSPECTION_CANCELLED}
        except Exception as error:  # a background failure must end the run
            result = {
                "status": "unavailable",
                "source_sha256": sha256,
                "message": f"Project inspection failed: {error}",
            }
        with self._state_lock:
            self._inspection = result
            self._inspection_cancel = None
        self._inspection_lock.release()

    def check(self, sha256: str, through_line: int | None = None) -> None:
        """Queue an explicit check, retaining historical results in memory.

        With ``through_line``, only the saved file's lines 1 through
        ``through_line`` are checked (`check_project`); the saved results of
        later claims are kept, not rechecked.
        """
        if self._cancel.is_set():
            raise NotebookError("This preview is closed.")
        unsupported = self._unsupported()
        if unsupported is not None:
            raise NotebookError(unsupported)
        if through_line is not None:
            try:
                data = self._text().encode()
            except (OSError, ValueError):
                data = b""
            if through_line_end(data, through_line) >= len(data):
                through_line = None  # nothing after it: a check of the whole file
        if not self._inspection_lock.acquire(blocking=False):
            raise NotebookError(BUSY)
        with self._state_lock:
            if self._cancel.is_set():
                self._inspection_lock.release()
                raise NotebookError("This preview is closed.")
            self._check = {
                "status": "running",
                "source_sha256": sha256,
                "through_line": through_line,
                "message": "Preparing check",
                "theorems": [],
            }
            cancel = threading.Event()
            self._check_cancel = cancel
            self._worker = threading.Thread(
                target=self._work, args=(sha256, through_line, cancel), daemon=False
            )
            try:
                self._worker.start()
            except RuntimeError as error:
                self._check = {
                    "status": "unavailable",
                    "source_sha256": sha256,
                    "through_line": through_line,
                    "message": str(error),
                    "theorems": [],
                }
                self._worker = None
                self._check_cancel = None
                self._inspection_lock.release()

    def cancel_check(self) -> None:
        """Stop the running check, also while it waits for the Lean slot.

        Raises:
            SnapshotError: No check is running.
        """
        with self._state_lock:
            if self._check_cancel is None:
                raise SnapshotError(NOTHING_CHECKING)
            self._check_cancel.set()

    def _with_import_hint(
        self, result: CheckOutcome, environment: FingerprintRecord
    ) -> CheckOutcome:
        """Explain unbuilt sibling imports, unless the check was cut short."""
        message = str(result.get("message", ""))
        if (
            self._cancel.is_set()
            or cut_short(message)
            or "Saved source changed" in message
        ):
            return result
        missing = unbuilt_sibling_imports(self.path, environment.get("imports", {}))
        if not missing:
            return result
        if describe_project(self.path)["kind"] == "lake":
            commands = ", ".join(f"`lake build {name}`" for name in missing)
            hint = (
                f"If {', '.join(missing)} are modules of this project, build "
                f"them first ({commands}), then check again."
            )
        else:
            hint = "Imports of other notebooks need a Lake project; see the README."
        return with_message(result, " ".join(filter(None, [message, hint])))

    def _work(
        self,
        sha256: str,
        through_line: int | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """Publish phases and a complete result; never touch the notebook source."""
        cancel = cancel or self._cancel

        def progress(message: str) -> None:
            with self._state_lock:
                if self._check is not None:
                    self._check = {**self._check, "message": message}

        try:
            text = read_regular(self.path, MAX_SOURCE_BYTES).decode()
            progress("Reading dependency state")
            environment = environment_fingerprint(
                self.path, text, remote=remote_identity(self.path)
            )
            compiled: list[FingerprintRecord] = []

            def acquired() -> None:
                # Retaken once the check holds the Lean slot, before it reads
                # anything: the environment may change while it waits, and the
                # saved results must describe the one it compiles in. Any
                # change after this leaves them out of date.
                if not compiled:
                    progress("Reading dependency state")
                    compiled.append(
                        environment_fingerprint(
                            self.path, text, remote=remote_identity(self.path)
                        )
                    )

            result: CheckOutcome
            if from_environment() is not None:

                def remote_acquired(context_sha256: str) -> None:
                    acquired()
                    if compiled[0].get("compute_context") != context_sha256:
                        raise ValueError("Dependency context changed before submission")

                result = check_remote(
                    self.path,
                    expected_sha256=sha256,
                    cancel=cancel,
                    progress=progress,
                    through_line=through_line,
                    acquired=remote_acquired,
                )
            else:
                result = check_project(
                    self.path,
                    expected_sha256=sha256,
                    cancel=cancel,
                    progress=progress,
                    through_line=through_line,
                    acquired=acquired,
                )
            if compiled:
                environment = compiled[0]
            fingerprint = environment["sha256"]
            unknown = unknown_git_note(environment)
            if result.get("status") == "unavailable":
                result = self._with_import_hint(result, environment)
            if (
                result["status"] == "checked_snapshot"
                and result["source_sha256"] == sha256
                and hashlib.sha256(
                    text.replace("\r\n", "\n").encode("utf-8")
                ).hexdigest()
                == sha256
            ):
                try:
                    with self._records_lock:
                        previous = load_records(self.path)
                        records = records_from_check(
                            text, result, fingerprint, previous
                        )
                        save_records(self.path, records)
                    if unknown:
                        result = with_message(
                            result, f"{result.get('message', '')} {unknown}".strip()
                        )
                except (OSError, ValueError) as error:
                    result = with_message(
                        result,
                        f"{result.get('message', '')} "
                        f"Results could not be saved: {error}.".strip(),
                    )
            # The lines a finished check covered (a cut inside a command moves
            # back); for a check that could not run, the lines asked for.
            withheld = (
                result["withheld"] if result["status"] == "checked_snapshot" else None
            )
            if isinstance(withheld, dict):
                covered = withheld.get("through_line")
            elif result.get("status") == "checked_snapshot":
                covered = None
            else:
                covered = through_line
            with self._state_lock:
                self._check = _check_state(sha256, covered, result)
        except Exception as error:
            # A background failure must always unblock the editor controls.
            with self._state_lock:
                self._check = {
                    "status": "unavailable",
                    "source_sha256": sha256,
                    "through_line": through_line,
                    "message": f"Project check failed: {error}",
                    "theorems": [],
                }
        finally:
            with self._state_lock:
                self._check_cancel = None
            self._inspection_lock.release()

    def _text(self) -> str:
        return read_source(self.path)

    def _fingerprint(self, text: str) -> Fingerprints:
        """Compute each environment fingerprint of `text` at most once."""
        return Fingerprints(self.path, text)

    def snapshot(self) -> Snapshot:
        """Preserve all source, using prose cells only when parsing is lossless."""
        text = self._text()
        return self._snapshot(text, self._fingerprint(text))

    def _snapshot(
        self, text: str, fingerprint: Fingerprint, *, compare_environment: bool = True
    ) -> Snapshot:
        """Build the snapshot of `text` from this service's current state."""
        with self._state_lock:
            running, run_errors, saved_failures = self._runs.state()
            state = PreviewState(
                check=self._check,
                inspection=self._inspection,
                running=running,
                run_errors=run_errors,
                saved_failures=saved_failures,
            )
        return build_snapshot(
            self.path,
            text,
            fingerprint,
            state,
            self._providers,
            compare_environment=compare_environment,
        )

    def linked(self, link: str) -> Snapshot:
        """Return a read-only snapshot of another notebook of this project.

        The snapshot shows the linked notebook's saved source and its own
        saved results, as its own preview would before any request. Nothing
        is checked, observed, drawn or run for it here.

        Raises:
            NotebookLinkError: See `read_linked_notebook`.
        """
        target, text = read_linked_notebook(self.path, link)
        # Saved results are compared with the environment only inside this
        # notebook's own project. A nested project would be fingerprinted
        # from its own manifest, running git outside this project on every
        # poll, so its results are shown as saved, not compared.
        compared = project_root(target) == project_root(self.path)
        other_fingerprint: Fingerprint = (
            Fingerprints(target, text) if compared else (lambda *_: "")
        )
        snapshot = build_snapshot(
            target, text, other_fingerprint, PreviewState(), self._providers
        )
        if not compared:
            snapshot = uncompared(snapshot)
        snapshot["preview"]["linked"] = {
            "path": link,
            "file": target.name,
            "compared": compared,
        }
        return snapshot

    def asset(self, reference: str, sha256: str) -> tuple[bytes, str]:
        """Return an image the saved prose shows, only at the expected hash.

        Raises:
            assets.AssetError: See `assets.read`.
        """
        found = assets.references(prose(cells(self._text())))
        return assets.read(self.path, found, reference, sha256)

    def view(self, cell: int, line: int) -> Mapping[str, Any]:
        """Report one listed directive's drawing (`ViewDrawing.view`)."""
        return self._views.view(cell, line)

    def view_job(self, job_id: str) -> Mapping[str, Any]:
        """Answer a poll for one view job (`ViewDrawing.view_job`)."""
        return self._views.view_job(job_id)

    def cancel_view(self, sha256: str, line: int) -> None:
        """Stop drawing the view at `line` (`ViewDrawing.cancel_view`)."""
        self._views.cancel_view(sha256, line)

    def run_evidence(self, sha256: str, line: int) -> str:
        """Start the evidence run at `line` (`DirectiveRuns.run_evidence`)."""
        return self._runs.run_evidence(sha256, line)

    def run_decision(self, sha256: str, line: int) -> str:
        """Start deciding the directive at `line` (`DirectiveRuns.run_decision`)."""
        return self._runs.run_decision(sha256, line)

    def stop_decision(self) -> None:
        """Ask the running decision to stop (`DirectiveRuns.stop_decision`)."""
        self._runs.stop_decision()

    def discard_decision(self, sha256: str, line: int) -> str:
        """Remove a saved decision (`DirectiveRuns.discard_decision`)."""
        return self._runs.discard_decision(sha256, line)

    def decision_candidate(self, key: str) -> Candidate:
        """Return a decision's block to insert (`DirectiveRuns.decision_candidate`)."""
        return self._runs.decision_candidate(key)
