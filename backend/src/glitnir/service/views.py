"""Drawing a preview's view directives, one Lean run at a time."""

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from glitnir.lean.project_snapshot import SLOT_WAIT_TIMED_OUT, SnapshotError
from glitnir.lean.project_views import render_project_views
from glitnir.lean.provider_lean import INTERRUPTED
from glitnir.notebook.check_records import (
    evidence_prefix_sha256,
    load_records,
    save_records,
    saved_views,
    view_status,
    with_view,
)
from glitnir.notebook.directives import starts_command, view_keys
from glitnir.preview.cards import provider_imports, view_provider_hash
from glitnir.providers import ViewProvider
from glitnir.providers.outcomes import checked_drawing
from glitnir.service.context import CLOSED, PreviewContext, _sha256

MAX_CACHED_VIEWS = 64
# Views are drawn in a worker, one Lean run at a time, each run drawing every
# view it can (`SourcePreviewService.view`); the editor polls `view` for them.
VIEW_STARTING = "Preparing the view."
VIEW_QUEUED = "Waiting for another view to finish drawing."
VIEW_CANCELLED = "View drawing cancelled."
NOTHING_DRAWING = "That view is not being drawn."
UNKNOWN_JOB = "This drawing is no longer known; draw it again."


def _timed_out(failure: dict[str, Any]) -> bool:
    """Whether a view run failed by reaching its time limit.

    Not when it only waited too long for another Lean run to end: that says
    nothing about how long its own views take.
    """
    message = str(failure.get("error", "")).rstrip()
    return message.endswith("timed out.") and not message.startswith(
        SLOT_WAIT_TIMED_OUT
    )


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


class ViewDrawing:
    """The view drawings of one preview: its worker, runs and kept drawings."""

    def __init__(self, context: PreviewContext, *, save_drawings: bool) -> None:
        """Keep the service's shared objects; no worker is started.

        Args:
            context: The service's path, locks, cancel and callables.
            save_drawings: Save each finished drawing in the notebook's
                records; a static export (which writes nothing) passes False.
        """
        self._context = context
        self._save_drawings = save_drawings
        # Drawn views by (source, stable environment, line); finished jobs'
        # answers by job id, for the polls that ask for them; the run drawing
        # views, if any, and its worker. All under `state_lock`.
        self._drawings: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._finished_views: dict[str, tuple[dict[str, Any], bool]] = {}
        self._view_run: _ViewRun | None = None
        self._view_worker: threading.Thread | None = None
        # Keys of views whose run of several timed out, oldest first: each is
        # drawn alone when asked for again. Under `state_lock`.
        self._timed_out: dict[tuple[str, str, int], None] = {}

    def cancel(self) -> None:
        """Stop the run drawing views, if any; hold `state_lock`."""
        if self._view_run is not None:
            self._cancel_run(self._view_run)

    def close(self, deadline: float) -> None:
        """Wait for the drawing worker until `deadline` (`time.monotonic`)."""
        with self._context.state_lock:
            worker = self._view_worker
        if worker is not None:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))

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
        unsupported = self._context.unsupported()
        if unsupported is not None:
            return {"error": unsupported}
        text = self._context.text()
        fingerprint = self._context.fingerprint(text)
        snapshot = self._context.snapshot(text, fingerprint)
        views = snapshot["views"]
        entry = next(
            (v for v in views if v["cell"] == cell and v["line"] == line), None
        )
        if entry is None:
            raise KeyError(f"no view directive at cell {cell}, line {line}")
        if entry["error"]:
            return {"error": entry["error"]}
        providers = self._context.providers()
        provider = providers.views[entry["kind"]]
        imports = provider_imports(provider)
        environment = fingerprint.environment(imports)
        stable = str(environment.get("stable_sha256", environment["sha256"]))
        uncertain = bool(environment.get("unknown", False))
        sha256 = snapshot["sha256"]
        key = (sha256, stable, line)
        with self._context.state_lock:
            cached = self._drawings.get(key)
        if cached is not None:
            return _labelled(cached, uncertain)
        digest = None if uncertain else str(environment["sha256"])
        keys = view_keys(text, views)
        saved = {} if digest is None else saved_views(load_records(self._context.path))

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
            with self._context.state_lock:
                self._keep_drawing(key, asked)
            return asked
        with self._context.state_lock:
            answer = self._answer_during_run(key)
        if answer is not None:
            return answer
        with self._context.state_lock:
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
        with self._context.state_lock:
            cached = self._drawings.get(key)
            if cached is not None:
                return _labelled(cached, uncertain)
            answer = self._answer_during_run(key)
            if answer is not None:
                return answer
            if self._context.cancel.is_set():
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
        with self._context.state_lock:
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
                    with self._context.state_lock:
                        for job, _entry in members:
                            self._remember_timed_out(job.key)
                if self._retries(run, members, whole):
                    with self._context.state_lock:
                        for job in run.jobs.values():
                            job.message = VIEW_QUEUED
                    for job, entry in members:
                        self._draw_alone(run, job, entry)
        except Exception as error:  # every job must always end
            failure = {"error": f"View drawing failed: {error}"}
        finally:
            with self._context.state_lock:
                for job, _entry in members:
                    self._end_view_job(job, failure)
                self._view_run = None

    def _draw_together(
        self, run: _ViewRun, members: list[tuple[_ViewJob, dict[str, Any]]]
    ) -> dict[str, Any] | None:
        """Draw every view of a run in one Lean run; return its whole failure."""

        def progress(message: str) -> None:
            with self._context.state_lock:
                for job in run.jobs.values():
                    job.message = message

        result = render_project_views(
            self._context.path,
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
            and not self._context.cancel.is_set()
            and not message.endswith(INTERRUPTED)
        )

    def _draw_alone(self, run: _ViewRun, job: _ViewJob, entry: dict[str, Any]) -> None:
        """Draw one view of a failed run in a Lean run of its own, if still wanted.

        Its own Stop stops that run.
        """
        with self._context.state_lock:
            if job.ended or job.cancel.is_set():
                return

        def progress(message: str) -> None:
            with self._context.state_lock:
                job.message = message

        try:
            result = render_project_views(
                self._context.path,
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
        with self._context.state_lock:
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
            drawing = {
                "error": CLOSED if self._context.cancel.is_set() else VIEW_CANCELLED
            }
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
            saved = saved_views(load_records(self._context.path))
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
        with self._context.records_lock:
            try:
                if _sha256(self._context.text()) != job.key[0]:
                    return  # changed during the drawing
                records = with_view(
                    load_records(self._context.path),
                    record.key,
                    entry,
                    keep=record.keep,
                    notebook=self._context.path,
                )
                save_records(self._context.path, records)
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
        with self._context.state_lock:
            run = self._view_run
            job = None if run is None or run.sha256 != sha256 else run.jobs.get(line)
            if run is None or job is None or job.ended:
                raise SnapshotError(NOTHING_DRAWING)
            job.cancel.set()
            self._end_view_job(job, {"error": VIEW_CANCELLED})
            if all(other.ended for other in run.jobs.values()):
                run.cancel.set()
