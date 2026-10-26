"""A preview's evidence and decision runs, one provider run at a time."""

import json
import logging
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from glitnir.compute.client import remote_identity
from glitnir.lean.awake import awake
from glitnir.lean.digest import fingerprint_ok
from glitnir.lean.fingerprint import (
    environment_fingerprint,
    header_words,
    unknown_git_note,
)
from glitnir.lean.project import describe_project
from glitnir.lean.project_checking import TIMED_OUT, check_candidate, check_seconds
from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.provider_lean import provider_imports_error, run_provider_lean
from glitnir.notebook.check_records import (
    MAX_MESSAGE,
    MAX_TYPE,
    decision_prefix_sha256,
    decision_status,
    evidence_prefix_sha256,
    failure_message,
    load_records,
    save_records,
    saved_decisions,
    with_decision,
    with_decision_failure,
    with_evidence,
    with_evidence_failure,
    without_decision,
)
from glitnir.notebook.directives import decision_directives, evidence_directives
from glitnir.preview.cards import (
    decision_cards,
    decision_request,
    environment_digest,
    evidence_cards,
    evidence_request,
    provider_failure,
)
from glitnir.preview.snapshot import expected_theorem, indexed_cells
from glitnir.providers import (
    DecisionProvider,
    DecisionRequest,
    EvidenceProvider,
    EvidenceRequest,
)
from glitnir.providers.outcomes import checked_decision, checked_evidence
from glitnir.service.context import CLOSED, PreviewContext, _sha256

_LOG = logging.getLogger(__name__)

RUN_BUSY = "A provider run is already in progress."
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
    Not thread-safe: its caller holds `context.state_lock` around it.
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


class DirectiveRuns:
    """The evidence and decision runs of one preview, and what they leave."""

    def __init__(self, context: PreviewContext) -> None:
        """Keep the service's shared objects; no worker is started.

        Args:
            context: The service's path, locks, cancel and callables.
        """
        self._context = context
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
        # `state_lock`.
        self._emissions = _Emissions()
        # Keys whose last run error was also saved as the card's failure.
        self._saved_failures: set[str] = set()

    def state(self) -> tuple[str | None, dict[str, str], frozenset[str]]:
        """Return the running key, run errors and saved failures; hold `state_lock`."""
        return self._running, dict(self._run_errors), frozenset(self._saved_failures)

    def cancel(self) -> None:
        """Stop the running decision, if any; hold `state_lock`."""
        if self._decision_cancel is not None:
            self._decision_cancel.set()

    def close(self, deadline: float) -> None:
        """Wait for the evidence, then the decision worker until `deadline`."""
        with self._context.state_lock:
            workers = [self._evidence_worker, self._decision_worker]
        for worker in workers:
            if worker is not None:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def run_evidence(self, sha256: str, line: int) -> str:
        """Start running the evidence directive at `line`; return its card key.

        Raises:
            SnapshotError: The preview is closed, the project's Lean version
                is not supported, a run is already active, the saved source
                is not `sha256`, or the directive at `line` is missing, in
                error or unavailable.
        """
        return self._start(lambda: self._evidence_job(sha256, line))

    def _evidence_job(self, sha256: str, line: int) -> _EvidenceJob:
        """Find the runnable directive at `line` in the saved text `sha256`."""
        text = self._context.text()
        if _sha256(text) != sha256:
            raise SnapshotError(SOURCE_CHANGED)
        directives = evidence_directives(indexed_cells(text), text)
        directive = next((d for d in directives if d["line"] == line), None)
        if directive is None:
            raise SnapshotError(f"No evidence directive at line {line}.")
        providers = self._context.providers()
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

    def _start(self, make_job: Callable[[], _EvidenceJob | _DecisionJob]) -> str:
        """Take the run gate, build the job and start its worker; return its key.

        `make_job` runs only once the gate is held, so a busy, closed or
        unsupported preview is refused before the saved source is examined.
        A decision job gets its own cancel event, which `stop_decision` sets.

        Raises:
            SnapshotError: The preview is closed, the project's Lean version
                is not supported, a run is already active, or `make_job`
                refuses.
        """
        if self._context.cancel.is_set():
            raise SnapshotError(CLOSED)
        unsupported = self._context.unsupported()
        if unsupported is not None:
            raise SnapshotError(unsupported)
        if not self._run_lock.acquire(blocking=False):
            raise SnapshotError(RUN_BUSY)
        try:
            job = make_job()
            decision = isinstance(job, _DecisionJob)
            with self._context.state_lock:
                if self._context.cancel.is_set():
                    raise SnapshotError(CLOSED)
                cancel = threading.Event() if decision else None
                self._run_errors.pop(job.key, None)
                self._running = job.key
                self._decision_cancel = cancel
                worker = threading.Thread(
                    target=self._work, args=(job, cancel), daemon=False
                )
                if decision:
                    self._decision_worker = worker
                else:
                    self._evidence_worker = worker
                try:
                    worker.start()
                except RuntimeError as error:
                    self._running = None
                    self._decision_cancel = None
                    if decision:
                        self._decision_worker = None
                    else:
                        self._evidence_worker = None
                    kind = "Decision" if decision else "Evidence"
                    raise SnapshotError(
                        f"{kind} run could not start: {error}"
                    ) from error
        except BaseException:
            self._run_lock.release()
            raise
        return job.key

    def _work(
        self, job: _EvidenceJob | _DecisionJob, cancel: threading.Event | None
    ) -> None:
        """Run one job, keeping its error for the card; always end the run."""
        error: str | None = None
        saved = False
        kind = "Evidence" if isinstance(job, _EvidenceJob) else "Decision"
        try:
            # The provider's own computation may take minutes after Lean too,
            # and a decision is followed by checking its candidate: one run.
            with awake():
                if isinstance(job, _EvidenceJob):
                    error, saved = self._evidence_run(job)
                else:
                    assert cancel is not None
                    error, saved = self._decision_run(job, cancel)
        except Exception as failure:  # the run must always end
            error = f"{kind} run failed: {failure}"
        finally:
            self._end_run(job.key, error, saved)

    def _end_run(self, key: str, error: str | None, saved: bool = False) -> None:
        """Keep a run's error for its card and open the gate for the next run.

        `saved` says the error was also saved as the card's failure.
        """
        with self._context.state_lock:
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
        environment = environment_fingerprint(
            self._context.path,
            job.text,
            imports,
            remote=remote_identity(self._context.path),
        )
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
        with self._context.state_lock:
            kept = None if key is None else self._emissions.get(key)
        if kept is not None:
            result: dict[str, Any] = {"payload": kept}
        else:
            result = run_provider_lean(
                self._context.path,
                expected_sha256=job.sha256,
                line=job.line,
                provider_name=provider.name,
                imports=imports,
                preamble=preamble,
                command=command,
                marker=marker,
                needs="evidence",
                cancel=self._context.cancel,
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
        with self._context.records_lock:
            try:
                unchanged = _sha256(self._context.text()) == job.sha256
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                return CHANGED_DURING_RUN, None
            try:
                records = with_evidence(
                    load_records(self._context.path),
                    job.key,
                    entry,
                    keep=job.keep,
                    notebook=self._context.path,
                )
                save_records(self._context.path, records)
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
            self._context.path,
            job.text,
            imports,
            environment.get("packages"),
            remote=remote_identity(self._context.path),
        )
        if after.get("unknown") or after.get("sha256") != environment["sha256"]:
            return
        with self._context.state_lock:
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
        if self._context.cancel.is_set():
            return _not_saved(run, kind, "the preview is closing")
        if environment.get("unknown"):
            return _not_saved(run, kind, "the environment is unknown")
        if _names_local_path(message, self._context.path):
            return _not_saved(run, kind, "its message names a local path")
        entry = {
            "prefix_sha256": prefix,
            "fingerprint": environment["sha256"],
            "provider": job.provider.name,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "message": failure_message(message),
        }
        with self._context.records_lock:
            if cancel is not None:
                with self._context.state_lock:
                    if cancel.is_set():
                        return _not_saved(run, kind, "the run was stopped")
                    self._decision_cancel = None
            try:
                if _sha256(self._context.text()) != job.sha256:
                    return _not_saved(
                        run, kind, "the saved source changed during the run"
                    )
                records = write(
                    load_records(self._context.path),
                    job.key,
                    entry,
                    keep=job.keep,
                    notebook=self._context.path,
                )
                save_records(self._context.path, records)
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
        return self._start(lambda: self._decision_job(sha256, line))

    def stop_decision(self) -> None:
        """Ask the running decision to stop; it then saves nothing.

        Raises:
            SnapshotError: No decision is running.
        """
        with self._context.state_lock:
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
        if self._context.cancel.is_set():
            raise SnapshotError(CLOSED)
        with self._context.records_lock:
            text, directive, _keys = self._decision_directive(sha256, line)
            with self._context.state_lock:
                # A run that has not yet saved would race the discard.
                if (
                    self._decision_cancel is not None
                    and self._running == directive["key"]
                ):
                    raise SnapshotError(DECISION_RUNNING)
            try:
                records = without_decision(
                    load_records(self._context.path),
                    directive["key"],
                    self._context.path,
                )
            except ValueError as failure:
                raise SnapshotError(str(failure)) from failure
            save_records(self._context.path, records)
        with self._context.state_lock:
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
        text = self._context.text()
        directives = decision_directives(indexed_cells(text), text)
        directive = next((d for d in directives if d["key"] == key), None)
        if directive is None:
            raise SnapshotError(NO_DECISION)
        if text.startswith("\ufeff"):
            raise SnapshotError(BYTE_ORDER_MARK)
        with self._context.state_lock:
            if self._decision_cancel is not None and self._running == key:
                raise SnapshotError(DECISION_RUNNING)
        entry = saved_decisions(load_records(self._context.path)).get(key)
        if entry is None:
            raise SnapshotError(NOT_DECIDED)
        end_line = directive["end_line"]
        status = decision_status(
            entry, text, end_line, environment_digest(self._context.fingerprint(text))
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
        text = self._context.text()
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
        providers = self._context.providers()
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
        environment = environment_fingerprint(
            self._context.path, job.text, remote=remote_identity(self._context.path)
        )
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
            self._context.path,
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
                    self._context.path,
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
        with self._context.records_lock:
            # Stop sets `cancel` under the state lock, so this is atomic with
            # it: past here Stop finds nothing to stop rather than a run that
            # would save anyway. The lock order (records, then state) is the
            # one discarding uses too.
            with self._context.state_lock:
                if cancel.is_set():
                    return DECISION_CANCELLED, None
                self._decision_cancel = None
            try:
                unchanged = _sha256(self._context.text()) == job.sha256
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                return CHANGED_DURING_RUN, None
            try:
                records = with_decision(
                    load_records(self._context.path),
                    job.key,
                    entry,
                    keep=job.keep,
                    notebook=self._context.path,
                )
                save_records(self._context.path, records)
            except ValueError as failure:
                return str(failure), None
            except OSError as failure:
                return f"The decision could not be saved: {failure}.", None
        # Saved, but under a fingerprint that matches nothing: say why.
        return unknown_git_note(environment), None
