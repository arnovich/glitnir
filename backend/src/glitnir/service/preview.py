"""Saved-source previews with explicitly requested checks, evidence and decisions."""

import hashlib
import json
import logging
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from glitnir.compute.client import check_remote, from_environment
from glitnir.lean.awake import awake
from glitnir.lean.digest import fingerprint_ok
from glitnir.lean.fingerprint import (
    environment_fingerprint,
    header_words,
    unknown_git_note,
)
from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.project_checking import (
    TIMED_OUT,
    check_candidate,
    check_project,
    check_seconds,
    through_line_end,
)
from glitnir.lean.project_inspection import inspect_project, lean_support
from glitnir.lean.project_snapshot import (
    SLOT_WAIT_TIMED_OUT,
    SnapshotError,
)
from glitnir.lean.project_views import render_project_views
from glitnir.lean.provider_lean import (
    INTERRUPTED,
    provider_imports_error,
    run_provider_lean,
)
from glitnir.notebook.check_records import (
    MAX_MESSAGE,
    MAX_TYPE,
    decision_prefix_sha256,
    decision_status,
    evidence_prefix_sha256,
    failure_message,
    load_records,
    records_from_check,
    save_records,
    saved_decisions,
    saved_views,
    view_status,
    with_decision,
    with_decision_failure,
    with_evidence,
    with_evidence_failure,
    with_view,
    without_decision,
)
from glitnir.notebook.directives import (
    decision_directives,
    evidence_directives,
    starts_command,
    view_keys,
)
from glitnir.notebook.document import (
    NotebookError,
)
from glitnir.preview import assets
from glitnir.preview.cards import (
    decision_cards,
    decision_request,
    environment_digest,
    evidence_cards,
    evidence_request,
    provider_failure,
    provider_imports,
    view_provider_hash,
)
from glitnir.preview.linked import (
    project_root,
    read_linked_notebook,
    unbuilt_sibling_imports,
)
from glitnir.preview.snapshot import (
    Fingerprint,
    Fingerprints,
    PreviewState,
    build_snapshot,
    cells,
    cut_short,
    expected_theorem,
    indexed_cells,
    prose,
    read_source,
    uncompared,
)
from glitnir.providers import (
    DecisionProvider,
    DecisionRequest,
    EvidenceProvider,
    EvidenceRequest,
    Providers,
    ViewProvider,
    load_providers,
)
from glitnir.providers.outcomes import (
    checked_decision,
    checked_drawing,
    checked_evidence,
)
from glitnir.service.proof_steps import ProofSteps

_LOG = logging.getLogger(__name__)

MAX_CACHED_VIEWS = 64
RUN_BUSY = "A provider run is already in progress."
CLOSED = "This preview is closed."
SOURCE_CHANGED = "Saved source changed; refresh and retry."
CHANGED_DURING_RUN = "Saved source changed during the run; run again."
MAX_RUN_ERRORS = 64
#: How many of Lean's evidence outputs a preview remembers, and how many
#: characters they may hold together (`_Emissions`).
MAX_EMISSIONS = 16
MAX_EMISSION_CHARACTERS = 32 * 1024**2
NOTHING_TO_STOP = "No decision is running."
DECISION_CANCELLED = "Decision run cancelled."
DECISION_RUNNING = "This decision is running; stop it first."
NO_DECISION = "This decision is no longer in the saved notebook."
NOT_DECIDED = "This decision has no saved result; run it first."
# The editor's text drops a leading byte-order mark that the saved digest keeps.
BYTE_ORDER_MARK = (
    "Notebooks starting with a byte-order mark cannot accept decisions yet; "
    "remove the BOM."
)
# Why a saved decision that is not a current candidate cannot be accepted.
NOT_ACCEPTABLE = {
    "unknown": "This decision found no block to accept.",
    "rejected": "The checker rejected this decision's block.",
    "stale": "This decision is out of date; run it again.",
    "accepted": "This decision's block is already in the notebook.",
    "candidate": "This decision's block was not checked.",
}
# How long closing waits for every worker together before giving up on them.
CLOSE_SECONDS = 55
INSPECTING = "Inspecting the saved project setup"
BUSY = "Project inspection or checking is already running."
# Views are drawn in a worker, one Lean run at a time, each run drawing every
# view it can (`SourcePreviewService.view`); the editor polls `view` for them.
VIEW_STARTING = "Preparing the view."
VIEW_QUEUED = "Waiting for another view to finish drawing."
VIEW_CANCELLED = "View drawing cancelled."
NOTHING_DRAWING = "That view is not being drawn."
UNKNOWN_JOB = "This drawing is no longer known; draw it again."
NOTHING_INSPECTING = "No inspection is running."
NOTHING_CHECKING = "No check is running."
INSPECTION_CANCELLED = "Project inspection cancelled."


def _timed_out(failure: dict[str, Any]) -> bool:
    """Whether a view run failed by reaching its time limit.

    Not when it only waited too long for another Lean run to end: that says
    nothing about how long its own views take.
    """
    message = str(failure.get("error", "")).rstrip()
    return message.endswith("timed out.") and not message.startswith(
        SLOT_WAIT_TIMED_OUT
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _crash(
    provider: EvidenceProvider | DecisionProvider, failure: BaseException
) -> tuple[str, tuple[str, str] | None]:
    """Word a provider crash, and the failure to save for it, if any.

    A crash's text is not written for the reader and may name any path or
    URL, so only its exception type is saved. An `ImportError` is a missing
    or broken dependency, a setup failure that says nothing about the
    directive: it is shown, never saved. For evidence and decisions alike.
    """
    message = provider_failure(provider, failure)
    if isinstance(failure, ImportError):
        return message, None
    return message, (
        "provider",
        f"Provider {provider.name} failed: {type(failure).__name__}",
    )


def _not_saved(run: str, kind: str, reason: str) -> bool:
    """Log why a failed `run` was not saved, never its message; return False."""
    _LOG.debug("A failed %s run (%s) was not saved: %s.", run, kind, reason)
    return False


# What looks like an absolute path on any machine: a POSIX path of at least two
# segments ("/opt/x"), a Windows drive path ("C:\x" or "C:/x"), a path under
# the home directory ("~/x") or a file URL. A fraction such as "1/2/3", a
# relative path and a web URL's path do not match.
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w.:/~])/[^\s/]+/|(?<!\w)[A-Za-z]:[\\/]|(?<![\w/])~/|(?i:file):/"
)


def _names_local_path(message: str, notebook: Path) -> bool:
    """Whether ``message`` names a path, so that it may not be saved.

    Anything that looks like an absolute path (`_ABSOLUTE_PATH`) counts, and
    so do the notebook's directory and project, the home directory and the
    temporary directory, as given and resolved.
    """
    if _ABSOLUTE_PATH.search(message):
        return True
    places = [notebook.parent, Path.home(), Path(tempfile.gettempdir())]
    try:
        root = describe_project(notebook)["root"]
    except Exception:  # noqa: BLE001 - the other places are still looked for
        root = None
    if root:
        places.append(Path(root))
    names = set()
    for place in places:
        for form in (place, place.absolute()):
            names.add(str(form))
            try:
                names.add(str(form.resolve()))
            except (OSError, RuntimeError):
                pass
    return any(name != "/" and name in message for name in names)


class _Emissions:
    """What Lean printed for evidence runs, by everything that Lean was given.

    An evidence run's Lean output is fixed by the notebook text before the
    directive's line, that line's number, the provider's name, imports,
    preamble, marker and command, and the environment the fingerprint
    identifies. The directive's options never reach Lean; only the provider's
    `compute` reads them. So a run whose key matches a kept one, typically the
    same directive with corrected options, is computed on the kept output
    without running Lean again, and a mistake only `compute` can see, such as
    a port count that does not match the circuit, is reported in seconds.

    Output is kept only when the environment was the same before and after
    the run, and whatever Lean printed is kept, even text that is not JSON: it
    would print the same again, and the rerun reports it the same way.

    In memory only, at most `entries` outputs and `size` characters together
    (not bytes), the oldest dropped first; one larger than `size` is not kept.
    Not thread-safe: the service holds `_state_lock` around it.
    """

    def __init__(
        self, entries: int = MAX_EMISSIONS, size: int = MAX_EMISSION_CHARACTERS
    ) -> None:
        self._entries = entries
        self._size = size
        self._kept: OrderedDict[tuple[str, ...], str] = OrderedDict()
        self._total = 0

    def get(self, key: tuple[str, ...]) -> str | None:
        """Return the output kept for `key`, or None."""
        return self._kept.get(key)

    def keep(self, key: tuple[str, ...], payload: str) -> None:
        """Keep `payload` for `key`, dropping the oldest beyond the bounds."""
        if len(payload) > self._size:
            return
        earlier = self._kept.pop(key, None)
        if earlier is not None:
            self._total -= len(earlier)
        self._kept[key] = payload
        self._total += len(payload)
        while len(self._kept) > self._entries or self._total > self._size:
            _key, dropped = self._kept.popitem(last=False)
            self._total -= len(dropped)


def _emission_key(
    job: "_EvidenceJob",
    imports: tuple[str, ...],
    preamble: Any,
    marker: Any,
    command: Any,
    environment: dict[str, Any],
) -> tuple[str, ...] | None:
    """Name everything an evidence run's Lean is given, or None if it cannot be.

    None when the environment is unknown (its digest is random) or the
    provider gave something other than text, which the run itself reports.
    Lines below the directive are not part of it: Lean never sees them. The
    header's words are, wherever the header is: Lake's setup reads them.
    """
    fingerprint = environment.get("sha256")
    if environment.get("unknown") or not fingerprint_ok(fingerprint):
        return None
    if not all(isinstance(part, str) for part in (preamble, marker, command)):
        return None
    return (
        str(job.provider.name),
        json.dumps(list(imports)),
        preamble,
        marker,
        command,
        str(job.line),
        evidence_prefix_sha256(job.text, job.line - 1),
        json.dumps(header_words(job.text)),
        str(fingerprint),
    )


@dataclass(frozen=True)
class _EvidenceJob:
    """One requested evidence run: the saved text, its directive and provider."""

    text: str
    sha256: str
    line: int
    key: str
    keep: frozenset[str]
    provider: EvidenceProvider
    request: EvidenceRequest


@dataclass(frozen=True)
class _DecisionJob:
    """One requested decision: the saved text, its directive and provider."""

    text: str
    sha256: str
    name: str
    end_line: int
    key: str
    keep: frozenset[str]
    provider: DecisionProvider
    request: DecisionRequest


@dataclass(frozen=True)
class _ViewRecord:
    """What saving a view's drawing needs, taken when its job started.

    `text` is the saved source, `key` the directive's key, `keep` the keys of
    every view directive in it, `provider` the provider's name and
    `provider_sha256` its hash for this view (`view_provider_sha256`).
    """

    text: str
    key: str
    keep: frozenset[str]
    provider: str
    provider_sha256: str


# Labels a drawing is shown with but never saved with.
_TRANSIENT = ("uncertain", "proofs_skipped")


@dataclass
class _ViewJob:
    """One view of a run: its cache key, its own Stop and what it is doing.

    The key is (saved source, stable environment, line): the environment
    without the random value an unknown state adds (`environment_fingerprint`),
    so an unknown state never makes each poll a new drawing. Such a drawing
    is `uncertain`: it may be out of date.
    """

    key: tuple[str, str, int]
    cancel: threading.Event
    uncertain: bool
    # The digest a kept Lake setup is bound to; None while the state is unknown.
    environment_sha256: str | None = None
    # How to save the drawing; None when it is not saved (see `_save_drawing`).
    record: _ViewRecord | None = None
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    message: str = VIEW_STARTING
    # Whether its answer is kept for its polls (`_end_view_job`).
    ended: bool = False


@dataclass
class _ViewRun:
    """The Lean run drawing views of one saved source with one provider.

    `jobs` are its views by directive line. `cancel` stops its process: it is
    set when every view has ended (each stopped by its own `cancel`), on
    close and when the saved source is replaced. The views are drawn by
    `provider` from the saved source `sha256`.
    """

    jobs: dict[int, _ViewJob]
    cancel: threading.Event
    provider: ViewProvider
    sha256: str


def _drawing_answer(job: _ViewJob) -> dict[str, Any]:
    """Answer for a view still being drawn: its progress and its job."""
    return {"status": "drawing", "message": job.message, "job": job.id}


def _drawn_view(
    job: _ViewJob, entry: dict[str, Any]
) -> tuple[int, str, str, tuple[str, ...]]:
    """Name the view `render_project_views` draws for `job`, listed as `entry`."""
    return (job.key[2], entry["kind"], entry["name"], tuple(entry["using"]))


def _labelled(drawing: dict[str, Any], uncertain: bool) -> dict[str, Any]:
    """Mark a drawing made or kept while the environment is not fully known."""
    if uncertain and "error" not in drawing:
        return {**drawing, "uncertain": True}
    return drawing


def _inspection_summary(result: dict[str, Any], sha256: str) -> dict[str, Any]:
    """Keep what the preview shows of an inspection; never the setup itself.

    The setup lists every module of the import closure (megabytes with
    Mathlib) and the resolution the bytes of each configuration file it read,
    so only the message and the packages are kept for the snapshot.
    """
    summary: dict[str, Any] = {
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
        self._save_drawings = save_drawings
        self.proof_steps = ProofSteps(path)
        self._cancel = threading.Event()
        self._inspection_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._check: dict[str, Any] | None = None
        self._check_cancel: threading.Event | None = None
        self._worker: threading.Thread | None = None
        self._inspection: dict[str, Any] | None = None
        self._inspection_worker: threading.Thread | None = None
        self._inspection_cancel: threading.Event | None = None
        # Drawn views by (source, stable environment, line); finished jobs'
        # answers by job id, for the polls that ask for them; the run drawing
        # views, if any, and its worker. All under `_state_lock`.
        self._drawings: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._finished_views: dict[str, tuple[dict[str, Any], bool]] = {}
        self._view_run: _ViewRun | None = None
        self._view_worker: threading.Thread | None = None
        # Keys of views whose run of several timed out, oldest first: each is
        # drawn alone when asked for again. Under `_state_lock`.
        self._timed_out: dict[tuple[str, str, int], None] = {}
        self._providers_lock = threading.Lock()
        self._loaded_providers: Providers | None = None
        # Held for a whole provider run, evidence or decision: one at a time
        # per server.
        self._run_lock = threading.Lock()
        self._evidence_worker: threading.Thread | None = None
        self._decision_worker: threading.Thread | None = None
        # Set by Stop or close; present only while a decision runs.
        self._decision_cancel: threading.Event | None = None
        self._running: str | None = None
        self._run_errors: dict[str, str] = {}
        # Lean's output of earlier evidence runs, for reruns. Under
        # `_state_lock`.
        self._emissions = _Emissions()
        # Keys whose last run error was also saved as the card's failure.
        self._saved_failures: set[str] = set()
        # Every read-modify-write of the records file, so none drops another's.
        self._records_lock = threading.Lock()

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
            if self._decision_cancel is not None:
                self._decision_cancel.set()
            if self._view_run is not None:
                self._cancel_run(self._view_run)
            if self._inspection_cancel is not None:
                self._inspection_cancel.set()
            if self._check_cancel is not None:
                self._check_cancel.set()
            workers = [
                self._worker,
                self._inspection_worker,
                self._evidence_worker,
                self._decision_worker,
                self._view_worker,
            ]
        deadline = time.monotonic() + CLOSE_SECONDS
        for worker in workers:
            if worker is not None:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def inspect(self, sha256: str) -> dict[str, Any]:
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
            return dict(self._inspection)

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
        self, result: dict[str, Any], environment: dict[str, Any]
    ) -> dict[str, Any]:
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
        return {**result, "message": " ".join(filter(None, [message, hint]))}

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
            text = read_regular(self.path, 16 * 1024 * 1024).decode()
            progress("Reading dependency state")
            environment = environment_fingerprint(self.path, text)
            compiled: list[dict[str, Any]] = []

            def acquired() -> None:
                # Retaken once the check holds the Lean slot, before it reads
                # anything: the environment may change while it waits, and the
                # saved results must describe the one it compiles in. Any
                # change after this leaves them out of date.
                if not compiled:
                    progress("Reading dependency state")
                    compiled.append(environment_fingerprint(self.path, text))

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
                result.get("status") == "checked_snapshot"
                and result.get("source_sha256") == sha256
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
                        result = {
                            **result,
                            "message": f"{result.get('message', '')} "
                            f"{unknown}".strip(),
                        }
                except (OSError, ValueError) as error:
                    result = {
                        **result,
                        "message": f"{result.get('message', '')} "
                        f"Results could not be saved: {error}.".strip(),
                    }
            # The lines a finished check covered (a cut inside a command moves
            # back); for a check that could not run, the lines asked for.
            withheld = result.get("withheld")
            if isinstance(withheld, dict):
                covered = withheld.get("through_line")
            elif result.get("status") == "checked_snapshot":
                covered = None
            else:
                covered = through_line
            with self._state_lock:
                self._check = {
                    "source_sha256": sha256,
                    "through_line": covered,
                    **result,
                }
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

    def snapshot(self) -> dict[str, Any]:
        """Preserve all source, using prose cells only when parsing is lossless."""
        text = self._text()
        return self._snapshot(text, self._fingerprint(text))

    def _snapshot(
        self, text: str, fingerprint: Fingerprint, *, compare_environment: bool = True
    ) -> dict[str, Any]:
        """Build the snapshot of `text` from this service's current state."""
        with self._state_lock:
            state = PreviewState(
                check=self._check,
                inspection=self._inspection,
                running=self._running,
                run_errors=dict(self._run_errors),
                saved_failures=frozenset(self._saved_failures),
            )
        return build_snapshot(
            self.path,
            text,
            fingerprint,
            state,
            self._providers,
            compare_environment=compare_environment,
        )

    def linked(self, link: str) -> dict[str, Any]:
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

    def view(self, cell: int, line: int) -> dict[str, Any]:
        """Report one listed directive's drawing, starting it if needed.

        Never waits for Lean. Views are drawn in a worker, one Lean run at a
        time and each run bounded by the check time limit. A run draws the
        view asked for and, when its directive starts between commands
        (`starts_command`), every other listed view of the same provider
        that does too and has no current drawing, in memory or saved. A view
        whose run of several timed out is drawn alone (`_draw_views`). Each
        view of the run answers `{"status": "drawing", "message": ...,
        "job": id}` with its own job until the run ends; any other view
        answers `"queued"` with the id of a job being drawn. The caller
        polls `view_job` for that id, which answers without reading the
        notebook. A finished drawing is kept for its saved source and
        environment, the provider's modules included; one made while the
        environment is not fully known is marked `uncertain` and kept for
        that same unknown state, so asking again never draws it again. A
        drawing saved in the notebook's records whose source, environment
        and provider hash all match is answered without Lean; every other
        finished drawing made in a known environment is saved there
        (`_save_drawing`). A failure goes to the polls of its job only, so
        asking here again draws again. Raises KeyError if no directive is
        listed at `cell` and `line`. For a project on a Lean version Glitnir
        does not check (`lean_support`), it answers `{"error": message}` and
        draws nothing, saved drawings included.
        """
        unsupported = self._unsupported()
        if unsupported is not None:
            return {"error": unsupported}
        text = self._text()
        fingerprint = self._fingerprint(text)
        snapshot = self._snapshot(text, fingerprint)
        views = snapshot["views"]
        entry = next(
            (v for v in views if v["cell"] == cell and v["line"] == line), None
        )
        if entry is None:
            raise KeyError(f"no view directive at cell {cell}, line {line}")
        if entry["error"]:
            return {"error": entry["error"]}
        providers = self._providers()
        provider = providers.views[entry["kind"]]
        imports = provider_imports(provider)
        environment = fingerprint.environment(imports)
        stable = str(environment.get("stable_sha256", environment["sha256"]))
        uncertain = bool(environment.get("unknown", False))
        sha256 = snapshot["sha256"]
        key = (sha256, stable, line)
        with self._state_lock:
            cached = self._drawings.get(key)
        if cached is not None:
            return _labelled(cached, uncertain)
        digest = None if uncertain else str(environment["sha256"])
        keys = view_keys(text, views)
        saved = {} if digest is None else saved_views(load_records(self.path))

        def job_for(view: dict[str, Any]) -> _ViewJob | dict[str, Any]:
            """Return a new job drawing `view`, or its current saved drawing."""
            at = view["line"]
            provider_sha256 = view_provider_hash(providers, view)
            record = None
            if digest is not None and provider_sha256 is not None:
                drawing = self._saved_drawing(
                    text, keys[at], at, digest, provider_sha256, saved=saved
                )
                if drawing is not None:
                    return drawing
                if self._save_drawings:
                    record = _ViewRecord(
                        text,
                        keys[at],
                        frozenset(keys.values()),
                        provider.name,
                        provider_sha256,
                    )
            return _ViewJob(
                (sha256, stable, at), threading.Event(), uncertain, digest, record
            )

        asked = job_for(entry)
        if not isinstance(asked, _ViewJob):
            with self._state_lock:
                self._keep_drawing(key, asked)
            return asked
        with self._state_lock:
            answer = self._answer_during_run(key)
        if answer is not None:
            return answer
        with self._state_lock:
            timed_out = set(self._timed_out)
        members = [(asked, entry)]
        if key not in timed_out and starts_command(text, line):
            for view in views:
                if len(members) == MAX_CACHED_VIEWS:
                    break  # every answer of the run must outlast it
                if (
                    view["line"] != line
                    and not view["error"]
                    and providers.views.get(view["kind"]) is provider
                    and (sha256, stable, view["line"]) not in timed_out
                    and starts_command(text, view["line"])
                ):
                    member = job_for(view)
                    if isinstance(member, _ViewJob):
                        members.append((member, view))
        members.sort(key=lambda member: member[0].key[2])
        with self._state_lock:
            cached = self._drawings.get(key)
            if cached is not None:
                return _labelled(cached, uncertain)
            answer = self._answer_during_run(key)
            if answer is not None:
                return answer
            if self._cancel.is_set():
                return {"error": CLOSED}
            # Drawn in memory meanwhile, by a run that has just ended.
            members = [
                (job, view)
                for job, view in members
                if job is asked or job.key not in self._drawings
            ]
            run = _ViewRun(
                {job.key[2]: job for job, _view in members},
                threading.Event(),
                provider,
                sha256,
            )
            worker = threading.Thread(
                target=self._draw_views, args=(run, members), daemon=False
            )
            self._view_run, self._view_worker = run, worker
            try:
                worker.start()
            except RuntimeError as error:
                self._view_run = self._view_worker = None
                return {"error": f"View drawing could not start: {error}"}
            return _drawing_answer(asked)

    def _answer_during_run(self, key: tuple[str, str, int]) -> dict[str, Any] | None:
        """Answer for the view `key` while a run draws, or None; hold `_state_lock`.

        A view of the run answers with its own job; any other waits for one
        of the run's jobs. A run of a replaced saved source is stopped first:
        its drawings would be refused.
        """
        run = self._view_run
        if run is None:
            return None
        job = run.jobs.get(key[2])
        if job is not None and job.key == key and not job.ended:
            return _drawing_answer(job)
        if run.sha256 != key[0]:
            self._cancel_run(run)
        waited = next((job for job in run.jobs.values() if not job.ended), None)
        job = waited or next(iter(run.jobs.values()))
        return {"status": "queued", "message": VIEW_QUEUED, "job": job.id}

    def view_job(self, job_id: str) -> dict[str, Any]:
        """Answer a poll for one view job, without reading the notebook.

        While the job draws, its status and progress; once it has ended, its
        drawing or failure, as often as asked; a job no longer remembered is
        a transient failure, so the caller asks `view` again.
        """
        with self._state_lock:
            run = self._view_run
            for job in run.jobs.values() if run is not None else ():
                if job.id == job_id and not job.ended:
                    return _drawing_answer(job)
            found = self._finished_views.get(job_id)
        if found is None:
            return {"error": UNKNOWN_JOB, "transient": True}
        return _labelled(*found)

    def _cancel_run(self, run: _ViewRun) -> None:
        """Stop a whole run and each of its views; hold `_state_lock`."""
        run.cancel.set()
        for job in run.jobs.values():
            job.cancel.set()

    def _draw_views(
        self, run: _ViewRun, members: list[tuple[_ViewJob, dict[str, Any]]]
    ) -> None:
        """Draw a run's views; always end every one of its jobs, then the run.

        `members` are its jobs, each with its listed directive, in line
        order. If a run of several views fails as a whole other than by
        timeout or Stop, for example by reaching the memory limit, each view
        still wanted is drawn again alone, in line order, in this worker. If
        it timed out, its views are remembered, so that each is drawn alone
        when asked for again rather than in the same run.
        """
        failure: dict[str, Any] = {"error": "View drawing failed."}
        try:
            whole = self._draw_together(run, members)
            if whole is not None:
                failure = whole
                if len(members) > 1 and _timed_out(whole):
                    with self._state_lock:
                        for job, _entry in members:
                            self._remember_timed_out(job.key)
                if self._retries(run, members, whole):
                    with self._state_lock:
                        for job in run.jobs.values():
                            job.message = VIEW_QUEUED
                    for job, entry in members:
                        self._draw_alone(run, job, entry)
        except Exception as error:  # every job must always end
            failure = {"error": f"View drawing failed: {error}"}
        finally:
            with self._state_lock:
                for job, _entry in members:
                    self._end_view_job(job, failure)
                self._view_run = None

    def _draw_together(
        self, run: _ViewRun, members: list[tuple[_ViewJob, dict[str, Any]]]
    ) -> dict[str, Any] | None:
        """Draw every view of a run in one Lean run; return its whole failure."""

        def progress(message: str) -> None:
            with self._state_lock:
                for job in run.jobs.values():
                    job.message = message

        result = render_project_views(
            self.path,
            expected_sha256=run.sha256,
            provider=run.provider,
            views=[_drawn_view(job, entry) for job, entry in members],
            cancel=run.cancel,
            progress=progress,
            environment_sha256=members[0][0].environment_sha256,
        )
        if "views" not in result:
            return result
        for (job, _entry), drawing in zip(members, result["views"]):
            self._finish_view(job, drawing)
        return None

    def _retries(
        self,
        run: _ViewRun,
        members: list[tuple[_ViewJob, dict[str, Any]]],
        failure: dict[str, Any],
    ) -> bool:
        """Whether a run that failed as a whole draws its views again alone.

        Only a run of several views does, and not when it timed out or was
        stopped: its message then ends with "timed out." or "cancelled.".
        """
        message = str(failure.get("error", "")).rstrip()
        return (
            len(members) > 1
            and not run.cancel.is_set()
            and not self._cancel.is_set()
            and not message.endswith(INTERRUPTED)
        )

    def _draw_alone(self, run: _ViewRun, job: _ViewJob, entry: dict[str, Any]) -> None:
        """Draw one view of a failed run in a Lean run of its own, if still wanted.

        Its own Stop stops that run.
        """
        with self._state_lock:
            if job.ended or job.cancel.is_set():
                return

        def progress(message: str) -> None:
            with self._state_lock:
                job.message = message

        try:
            result = render_project_views(
                self.path,
                expected_sha256=run.sha256,
                provider=run.provider,
                views=[_drawn_view(job, entry)],
                cancel=job.cancel,
                progress=progress,
                environment_sha256=job.environment_sha256,
            )
            drawing = result["views"][0] if "views" in result else result
        except Exception as error:  # the next view is still drawn
            drawing = {"error": f"View drawing failed: {error}"}
        self._finish_view(job, drawing)

    def _finish_view(self, job: _ViewJob, drawing: dict[str, Any]) -> None:
        """End a job with its drawing, then save the drawing if it is kept."""
        with self._state_lock:
            if job.ended:
                return  # stopped meanwhile, and answered so
            self._end_view_job(job, drawing)
            if job.cancel.is_set() or "error" in drawing:
                return
        self._save_drawing(job, drawing)

    def _end_view_job(self, job: _ViewJob, drawing: dict[str, Any]) -> None:
        """Keep a job's answer for its polls, once; hold `_state_lock`.

        A stopped job answers that it was cancelled, whatever was drawn: its
        drawing is neither kept nor saved.
        """
        if job.ended:
            return
        if job.cancel.is_set():
            # Asked for, so not transient: drawing it again needs a request.
            drawing = {"error": CLOSED if self._cancel.is_set() else VIEW_CANCELLED}
        elif "error" not in drawing:
            self._keep_drawing(job.key, drawing)
        job.ended = True
        self._finished_views[job.id] = (drawing, job.uncertain)
        while len(self._finished_views) > MAX_CACHED_VIEWS:
            del self._finished_views[next(iter(self._finished_views))]

    def _remember_timed_out(self, key: tuple[str, str, int]) -> None:
        """Draw the view `key` alone from now on; hold `_state_lock`."""
        self._timed_out.pop(key, None)
        self._timed_out[key] = None
        while len(self._timed_out) > MAX_CACHED_VIEWS:
            del self._timed_out[next(iter(self._timed_out))]

    def _keep_drawing(self, key: tuple[str, str, int], drawing: dict[str, Any]) -> None:
        """Keep a drawing in memory, the oldest dropped; hold `_state_lock`."""
        self._drawings[key] = drawing
        while len(self._drawings) > MAX_CACHED_VIEWS:
            del self._drawings[next(iter(self._drawings))]

    def _saved_drawing(
        self,
        text: str,
        key: str,
        line: int,
        fingerprint: str,
        provider_sha256: str,
        *,
        compare_environment: bool = True,
        saved: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Return the drawing saved for the directive `key` at `line`, if current.

        It is checked again (`checked_drawing`) before it is shown.
        `compare_environment` is as in `view_status`. `saved` is the records'
        drawings (`saved_views`), read here when None.
        """
        if saved is None:
            saved = saved_views(load_records(self.path))
        entry = saved.get(key)
        if entry is None:
            return None
        status = view_status(
            entry,
            text,
            line,
            fingerprint,
            provider_sha256,
            compare_environment=compare_environment,
        )
        if status != "current":
            return None
        drawing = checked_drawing(json.dumps(entry["drawing"]))
        return None if "error" in drawing else drawing

    def _save_drawing(self, job: _ViewJob, drawing: dict[str, Any]) -> None:
        """Save a finished drawing in the notebook's records, if it may be.

        Only a drawing made in a known environment, with a provider that could
        be hashed, is saved, and only while the saved source is the one it was
        drawn from; directives no longer in the file lose their drawings.
        Saving never fails the drawing: if it cannot be saved (an unreadable
        records file, too large), it is still shown and kept in memory.
        """
        record = job.record
        if record is None or job.environment_sha256 is None:
            return
        entry = {
            "prefix_sha256": evidence_prefix_sha256(record.text, job.key[2]),
            "fingerprint": job.environment_sha256,
            "provider": record.provider,
            "provider_sha256": record.provider_sha256,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "drawing": {k: v for k, v in drawing.items() if k not in _TRANSIENT},
        }
        with self._records_lock:
            try:
                if _sha256(self._text()) != job.key[0]:
                    return  # changed during the drawing
                records = with_view(
                    load_records(self.path),
                    record.key,
                    entry,
                    keep=record.keep,
                    notebook=self.path,
                )
                save_records(self.path, records)
            except Exception:  # noqa: BLE001 - saving never fails the drawing
                return  # shown and kept in memory all the same

    def cancel_view(self, sha256: str, line: int) -> None:
        """Stop drawing the view at `line` of the saved source `sha256`.

        Only that view is dropped: it answers at once that it was cancelled,
        and its drawing is neither kept nor saved. The run goes on for its
        other views; once none is left, its process is stopped too.

        Raises:
            SnapshotError: That view is not being drawn.
        """
        with self._state_lock:
            run = self._view_run
            job = None if run is None or run.sha256 != sha256 else run.jobs.get(line)
            if run is None or job is None or job.ended:
                raise SnapshotError(NOTHING_DRAWING)
            job.cancel.set()
            self._end_view_job(job, {"error": VIEW_CANCELLED})
            if all(other.ended for other in run.jobs.values()):
                run.cancel.set()

    def run_evidence(self, sha256: str, line: int) -> str:
        """Start running the evidence directive at `line`; return its card key.

        Raises:
            SnapshotError: The preview is closed, the project's Lean version
                is not supported, a run is already active, the saved source
                is not `sha256`, or the directive at `line` is missing, in
                error or unavailable.
        """
        if self._cancel.is_set():
            raise SnapshotError(CLOSED)
        unsupported = self._unsupported()
        if unsupported is not None:
            raise SnapshotError(unsupported)
        if not self._run_lock.acquire(blocking=False):
            raise SnapshotError(RUN_BUSY)
        try:
            job = self._evidence_job(sha256, line)
            with self._state_lock:
                if self._cancel.is_set():
                    raise SnapshotError(CLOSED)
                self._run_errors.pop(job.key, None)
                self._running = job.key
                worker = threading.Thread(
                    target=self._run_evidence_work, args=(job,), daemon=False
                )
                self._evidence_worker = worker
                try:
                    worker.start()
                except RuntimeError as error:
                    self._running = None
                    self._evidence_worker = None
                    raise SnapshotError(
                        f"Evidence run could not start: {error}"
                    ) from error
        except BaseException:
            self._run_lock.release()
            raise
        return job.key

    def _evidence_job(self, sha256: str, line: int) -> _EvidenceJob:
        """Find the runnable directive at `line` in the saved text `sha256`."""
        text = self._text()
        if _sha256(text) != sha256:
            raise SnapshotError(SOURCE_CHANGED)
        directives = evidence_directives(indexed_cells(text), text)
        directive = next((d for d in directives if d["line"] == line), None)
        if directive is None:
            raise SnapshotError(f"No evidence directive at line {line}.")
        providers = self._providers()
        [card] = evidence_cards([directive], providers)
        if card["error"]:
            raise SnapshotError(card["error"])
        if card["unavailable"]:
            raise SnapshotError(card["unavailable"])
        provider = providers.evidence[directive["verb"]]
        request, error = evidence_request(provider, directive)
        if request is None:
            raise SnapshotError(error)
        return _EvidenceJob(
            text=text,
            sha256=sha256,
            line=line,
            key=directive["key"],
            keep=frozenset(d["key"] for d in directives),
            provider=provider,
            request=request,
        )

    def _run_evidence_work(self, job: _EvidenceJob) -> None:
        """Run one job, keeping its error for the card; always end the run."""
        error: str | None = None
        saved = False
        try:
            # The provider's own computation may take minutes after Lean too.
            with awake():
                error, saved = self._evidence_run(job)
        except Exception as failure:  # the run must always end
            error = f"Evidence run failed: {failure}"
        finally:
            self._end_run(job.key, error, saved)

    def _end_run(self, key: str, error: str | None, saved: bool = False) -> None:
        """Keep a run's error for its card and open the gate for the next run.

        `saved` says the error was also saved as the card's failure.
        """
        with self._state_lock:
            self._running = None
            self._decision_cancel = None
            if saved:
                self._saved_failures.add(key)
            else:
                self._saved_failures.discard(key)
            if error is not None:
                self._run_errors[key] = error
                while len(self._run_errors) > MAX_RUN_ERRORS:
                    oldest = next(iter(self._run_errors))
                    del self._run_errors[oldest]
                    self._saved_failures.discard(oldest)
        self._run_lock.release()

    def _evidence_run(self, job: _EvidenceJob) -> tuple[str | None, bool]:
        """Elaborate, compute, validate and save; return the error, if any.

        A failure that says something about the directive is saved too
        (`_save_failure`); the error returned is the run's own either way.
        The flag says whether it was saved.
        """
        provider = job.provider
        try:  # provider code must never break the preview
            imports = tuple(provider.imports)
        except BaseException as failure:
            return provider_failure(provider, failure), False
        # Taken before the run so an environment change during it leaves the
        # saved evidence out of date, and before the provider's command so a
        # failure in it can be saved.
        environment = environment_fingerprint(self.path, job.text, imports)
        message, to_save = self._evidence_attempt(job, imports, environment)
        if message is None or to_save is None:
            return message, False
        kind, saved = to_save
        return message, self._save_failure(job, environment, kind, saved)

    def _evidence_attempt(
        self, job: _EvidenceJob, imports: tuple[str, ...], environment: dict[str, Any]
    ) -> tuple[str | None, tuple[str, str] | None]:
        """Run the job; return its error, if any, and the failure to save.

        The failure is its kind (`check_records.EVIDENCE_FAILURE_KINDS`) and
        the message to save, or None for an error that is not saved: anything
        but the directive's own Lean error, the provider's crash or refusal,
        output that is not evidence, or the run's own time limit. A crash is
        saved as its exception type alone (`_crash`). Lean's output kept from
        an earlier run with the same input is used without running Lean
        (`_Emissions`).
        """
        provider, request = job.provider, job.request
        fingerprint = environment["sha256"]
        try:  # provider code must never break the preview
            preamble = provider.preamble
            marker = provider.marker
            command = provider.command(request)
        except BaseException as failure:
            return _crash(provider, failure)
        key = _emission_key(job, imports, preamble, marker, command, environment)
        with self._state_lock:
            kept = None if key is None else self._emissions.get(key)
        if kept is not None:
            result: dict[str, Any] = {"payload": kept}
        else:
            result = run_provider_lean(
                self.path,
                expected_sha256=job.sha256,
                line=job.line,
                provider_name=provider.name,
                imports=imports,
                preamble=preamble,
                command=command,
                marker=marker,
                needs="evidence",
                cancel=self._cancel,
                wait=True,
                environment_sha256=None if environment.get("unknown") else fingerprint,
            )
            if key is not None and isinstance(result.get("payload"), str):
                self._keep_emission(key, result["payload"], job, imports, environment)
        if "payload" not in result:
            if result.get("timed_out") is True:
                limit = f"{check_seconds():g}"
                timed_out = f"The run reached its time limit of {limit} s."
                return timed_out, ("timeout", timed_out)
            error = str(result["error"])
            return error, ("lean", error) if result.get("lean") is True else None
        try:
            payload = json.loads(result["payload"])
        except (ValueError, RecursionError):
            invalid = f"Provider {provider.name} printed evidence that is not JSON."
            return invalid, ("invalid", invalid)
        try:
            body = provider.compute(request, payload)
        except ValueError as failure:
            # A provider's way of saying it cannot serve this request: its
            # message is meant for the reader, not a crash report.
            refused = str(failure) or provider_failure(provider, failure)
            return refused, ("refused", refused)
        except BaseException as failure:
            return _crash(provider, failure)
        try:
            snapshot = checked_evidence(body)
        except ValueError as failure:
            invalid = f"Provider {provider.name} returned invalid evidence: {failure}"
            return invalid, ("invalid", invalid)
        entry = {
            "prefix_sha256": evidence_prefix_sha256(job.text, job.line),
            "fingerprint": fingerprint,
            "provider": provider.name,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "snapshot": snapshot,
        }
        with self._records_lock:
            try:
                unchanged = _sha256(self._text()) == job.sha256
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                return CHANGED_DURING_RUN, None
            try:
                records = with_evidence(
                    load_records(self.path),
                    job.key,
                    entry,
                    keep=job.keep,
                    notebook=self.path,
                )
                save_records(self.path, records)
            except ValueError as failure:
                return str(failure), None
            except OSError as failure:
                return f"Evidence could not be saved: {failure}.", None
        # Saved, but under a fingerprint that matches nothing: say why.
        return unknown_git_note(environment), None

    def _keep_emission(
        self,
        key: tuple[str, ...],
        payload: str,
        job: _EvidenceJob,
        imports: tuple[str, ...],
        environment: dict[str, Any],
    ) -> None:
        """Keep Lean's output if the environment did not change during the run.

        A build changed while Lean ran may have been loaded by it, so its
        output would describe neither environment for sure.
        """
        after = environment_fingerprint(
            self.path, job.text, imports, environment.get("packages")
        )
        if after.get("unknown") or after.get("sha256") != environment["sha256"]:
            return
        with self._state_lock:
            self._emissions.keep(key, payload)

    def _save_failure(
        self,
        job: _EvidenceJob | _DecisionJob,
        environment: dict[str, Any],
        kind: str,
        message: str,
        cancel: threading.Event | None = None,
    ) -> bool:
        """Save a failed run in the notebook's records, if it may be; say if it was.

        Not when the preview is closing, the environment is unknown, the
        message names a local path (committed records hold none), or the
        saved source is no longer the one that was run. A decision's failure
        is not saved once its `cancel` is set: checked under the state lock,
        as a decision's save is, so Stop either wins or finds nothing to stop.
        Saving never replaces the run's own error: a failure that cannot be
        saved is only shown, and logged without its message, which may name a
        path.
        """
        if isinstance(job, _EvidenceJob):
            run, write = "evidence", with_evidence_failure
            prefix = evidence_prefix_sha256(job.text, job.line)
        else:
            run, write = "decision", with_decision_failure
            prefix = decision_prefix_sha256(job.text, job.end_line)
        if self._cancel.is_set():
            return _not_saved(run, kind, "the preview is closing")
        if environment.get("unknown"):
            return _not_saved(run, kind, "the environment is unknown")
        if _names_local_path(message, self.path):
            return _not_saved(run, kind, "its message names a local path")
        entry = {
            "prefix_sha256": prefix,
            "fingerprint": environment["sha256"],
            "provider": job.provider.name,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "message": failure_message(message),
        }
        with self._records_lock:
            if cancel is not None:
                with self._state_lock:
                    if cancel.is_set():
                        return _not_saved(run, kind, "the run was stopped")
                    self._decision_cancel = None
            try:
                if _sha256(self._text()) != job.sha256:
                    return _not_saved(
                        run, kind, "the saved source changed during the run"
                    )
                records = write(
                    load_records(self.path),
                    job.key,
                    entry,
                    keep=job.keep,
                    notebook=self.path,
                )
                save_records(self.path, records)
            except Exception as error:  # noqa: BLE001 - the run's error is shown
                _LOG.warning(
                    "A failed %s run (%s) was not saved: %s",
                    run,
                    kind,
                    type(error).__name__,
                )
                return False
        return True

    def run_decision(self, sha256: str, line: int) -> str:
        """Start deciding the directive at `line`; return its card key.

        Shares the evidence runs' gate: one provider run at a time.

        Raises:
            SnapshotError: The preview is closed, the project's Lean version
                is not supported, a run is already active, the saved source
                is not `sha256`, or the directive at `line` is missing, in
                error or unavailable.
        """
        if self._cancel.is_set():
            raise SnapshotError(CLOSED)
        unsupported = self._unsupported()
        if unsupported is not None:
            raise SnapshotError(unsupported)
        if not self._run_lock.acquire(blocking=False):
            raise SnapshotError(RUN_BUSY)
        try:
            job = self._decision_job(sha256, line)
            with self._state_lock:
                if self._cancel.is_set():
                    raise SnapshotError(CLOSED)
                cancel = threading.Event()
                self._run_errors.pop(job.key, None)
                self._running = job.key
                self._decision_cancel = cancel
                worker = threading.Thread(
                    target=self._run_decision_work, args=(job, cancel), daemon=False
                )
                self._decision_worker = worker
                try:
                    worker.start()
                except RuntimeError as error:
                    self._running = None
                    self._decision_cancel = None
                    self._decision_worker = None
                    raise SnapshotError(
                        f"Decision run could not start: {error}"
                    ) from error
        except BaseException:
            self._run_lock.release()
            raise
        return job.key

    def stop_decision(self) -> None:
        """Ask the running decision to stop; it then saves nothing.

        Raises:
            SnapshotError: No decision is running.
        """
        with self._state_lock:
            if self._decision_cancel is None:
                raise SnapshotError(NOTHING_TO_STOP)
            self._decision_cancel.set()

    def discard_decision(self, sha256: str, line: int) -> str:
        """Remove the decision saved for the directive at `line`; return its key.

        Raises:
            SnapshotError: The preview is closed, the saved source is not
                `sha256`, there is no decision directive at `line`, it is
                running, nothing is saved for it, or the records file cannot
                be read.
        """
        if self._cancel.is_set():
            raise SnapshotError(CLOSED)
        with self._records_lock:
            text, directive, _keys = self._decision_directive(sha256, line)
            with self._state_lock:
                # A run that has not yet saved would race the discard.
                if (
                    self._decision_cancel is not None
                    and self._running == directive["key"]
                ):
                    raise SnapshotError(DECISION_RUNNING)
            try:
                records = without_decision(
                    load_records(self.path), directive["key"], self.path
                )
            except ValueError as failure:
                raise SnapshotError(str(failure)) from failure
            save_records(self.path, records)
        with self._state_lock:
            self._run_errors.pop(directive["key"], None)
        return str(directive["key"])

    def decision_candidate(self, key: str) -> dict[str, Any]:
        """Return the checked block the editor may insert for the decision `key`.

        The backend never writes the notebook: the editor inserts `block` right
        after the one-based `insert_after_line`, and only into the saved text
        whose digest is `sha256`.

        Raises:
            SnapshotError: The decision is not in the saved text, the saved
                text starts with a byte-order mark (the editor's text drops
                it, so no digest could match), the decision is running, or its
                saved outcome is not a current candidate.
        """
        text = self._text()
        directives = decision_directives(indexed_cells(text), text)
        directive = next((d for d in directives if d["key"] == key), None)
        if directive is None:
            raise SnapshotError(NO_DECISION)
        if text.startswith("\ufeff"):
            raise SnapshotError(BYTE_ORDER_MARK)
        with self._state_lock:
            if self._decision_cancel is not None and self._running == key:
                raise SnapshotError(DECISION_RUNNING)
        entry = saved_decisions(load_records(self.path)).get(key)
        if entry is None:
            raise SnapshotError(NOT_DECIDED)
        end_line = directive["end_line"]
        status = decision_status(
            entry, text, end_line, environment_digest(self._fingerprint(text))
        )
        verification = entry["verification"]
        if status != "candidate" or verification["status"] != "checked":
            raise SnapshotError(NOT_ACCEPTABLE[status])
        return {
            "sha256": _sha256(text),
            "insert_after_line": end_line,
            "block": checked_decision(entry["outcome"])["block"],
        }

    def _decision_directive(
        self, sha256: str, line: int
    ) -> tuple[str, dict[str, Any], frozenset[str]]:
        """Return the saved text, its decision directive at `line` and all keys."""
        text = self._text()
        if _sha256(text) != sha256:
            raise SnapshotError(SOURCE_CHANGED)
        directives = decision_directives(indexed_cells(text), text)
        directive = next((d for d in directives if d["line"] == line), None)
        if directive is None:
            raise SnapshotError(f"No decision directive at line {line}.")
        return text, directive, frozenset(d["key"] for d in directives)

    def _decision_job(self, sha256: str, line: int) -> _DecisionJob:
        """Find the decidable directive at `line` in the saved text `sha256`."""
        text, directive, keys = self._decision_directive(sha256, line)
        providers = self._providers()
        [card] = decision_cards([directive], providers)
        if card["error"]:
            raise SnapshotError(card["error"])
        if card["unavailable"]:
            raise SnapshotError(card["unavailable"])
        provider = providers.decisions[directive["kind"]]
        request, error = decision_request(provider, directive)
        if request is None:
            raise SnapshotError(error)
        return _DecisionJob(
            text=text,
            sha256=sha256,
            name=directive["name"],
            end_line=directive["end_line"],
            key=directive["key"],
            keep=keys,
            provider=provider,
            request=request,
        )

    def _run_decision_work(self, job: _DecisionJob, cancel: threading.Event) -> None:
        """Run one decision, keeping its error for the card; always end the run."""
        error: str | None = None
        saved = False
        try:
            # Deciding and then checking the candidate: one run, kept awake.
            with awake():
                error, saved = self._decision_run(job, cancel)
        except Exception as failure:  # the run must always end
            error = f"Decision run failed: {failure}"
        finally:
            self._end_run(job.key, error, saved)

    def _decision_run(
        self, job: _DecisionJob, cancel: threading.Event
    ) -> tuple[str | None, bool]:
        """Decide, validate, check the candidate and save; return the error, if any.

        Nothing is saved once `cancel` is set. A failure that says something
        about the directive is saved as its failure (`_decision_attempt`); the
        error returned is the run's own either way, and the flag says whether
        it was saved.
        """
        provider = job.provider
        try:  # provider code must never break the preview
            imports = tuple(provider.imports)
        except BaseException as failure:
            return provider_failure(provider, failure), False
        # Taken before the run so an environment change during it leaves the
        # saved decision, or failure, out of date.
        environment = environment_fingerprint(self.path, job.text)
        message, to_save = self._decision_attempt(job, cancel, imports, environment)
        if message is None or to_save is None:
            return message, False
        kind, saved = to_save
        if self._save_failure(job, environment, kind, saved, cancel):
            return message, True
        # Stopped before the failure could be saved: like a stopped run.
        return (DECISION_CANCELLED if cancel.is_set() else message), False

    def _decision_attempt(
        self,
        job: _DecisionJob,
        cancel: threading.Event,
        imports: tuple[str, ...],
        environment: dict[str, Any],
    ) -> tuple[str | None, tuple[str, str] | None]:
        """Run the decision; return its error, if any, and the failure to save.

        The failure is its kind (`check_records.DECISION_FAILURE_KINDS`) and
        the message to save, or None for an error that is not saved: anything
        but the provider's crash or refusal, an outcome that is not a decision
        of the directive, or the candidate check's own time limit. A crash is
        saved as its exception type alone (`_crash`). A block the kernel check
        rejects is no failure: it is saved as the decision, `rejected`.
        """
        provider = job.provider
        fingerprint = environment["sha256"]
        # The block is inserted into the notebook, so the notebook must already
        # import what it needs.
        refused = provider_imports_error(
            self.path,
            expected_sha256=job.sha256,
            imports=imports,
            needs="decision",
            cancel=cancel,
        )
        if refused is not None:
            return (
                DECISION_CANCELLED if cancel.is_set() else str(refused["error"])
            ), None
        try:
            answer = provider.decide(job.request, cancel)
        except ValueError as failure:
            if cancel.is_set():  # how a provider may honour the cancel
                return DECISION_CANCELLED, None
            # The provider's way of saying it cannot decide this goal: its
            # message is meant for the reader, not a crash report.
            message = str(failure) or provider_failure(provider, failure)
            return message, ("refused", message)
        except BaseException as failure:
            if cancel.is_set() and isinstance(failure, Exception):
                return DECISION_CANCELLED, None
            return _crash(provider, failure)
        if cancel.is_set():
            return DECISION_CANCELLED, None
        try:
            outcome = checked_decision(answer)
        except ValueError as failure:
            invalid = (
                f"Provider {provider.name} returned an invalid decision: {failure}"
            )
            return invalid, ("invalid", invalid)
        verification: dict[str, Any] | None = None
        if outcome["status"] != "unknown":
            expected = expected_theorem(job.name, outcome["status"])
            if outcome["theorem"].split(".")[-1] != expected:
                misnamed = (
                    f"The provider proposed `{outcome['theorem']}`, "
                    f"expected `{expected}`."
                )
                return misnamed, ("invalid", misnamed)
            try:
                verdict = check_candidate(
                    self.path,
                    expected_sha256=job.sha256,
                    insert_after_line=job.end_line,
                    block=outcome["block"],
                    theorem=outcome["theorem"],
                    cancel=cancel,
                )
            except SnapshotError as failure:
                if cancel.is_set():
                    return DECISION_CANCELLED, None
                if str(failure) == TIMED_OUT:
                    # The check's own limit, not a wait for the slot.
                    limit = f"{check_seconds():g}"
                    timed_out = (
                        f"The candidate check reached its time limit of {limit} s."
                    )
                    return timed_out, ("timeout", timed_out)
                return str(failure), None
            kind = verdict["type"]
            if kind is not None and len(kind) > MAX_TYPE:
                kind = kind[: MAX_TYPE - 1] + "…"
            verification = {
                **verdict,
                "message": verdict["message"][:MAX_MESSAGE],
                "type": kind,
            }
        entry = {
            "prefix_sha256": decision_prefix_sha256(job.text, job.end_line),
            "fingerprint": fingerprint,
            "provider": provider.name,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "outcome": outcome,
            "verification": verification,
        }
        with self._records_lock:
            # Stop sets `cancel` under the state lock, so this is atomic with
            # it: past here Stop finds nothing to stop rather than a run that
            # would save anyway. The lock order (records, then state) is the
            # one discarding uses too.
            with self._state_lock:
                if cancel.is_set():
                    return DECISION_CANCELLED, None
                self._decision_cancel = None
            try:
                unchanged = _sha256(self._text()) == job.sha256
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                return CHANGED_DURING_RUN, None
            try:
                records = with_decision(
                    load_records(self.path),
                    job.key,
                    entry,
                    keep=job.keep,
                    notebook=self.path,
                )
                save_records(self.path, records)
            except ValueError as failure:
                return str(failure), None
            except OSError as failure:
                return f"The decision could not be saved: {failure}.", None
        # Saved, but under a fingerprint that matches nothing: say why.
        return unknown_git_note(environment), None
