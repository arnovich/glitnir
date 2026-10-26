"""One run for a notebook's views: queueing, stopping and drawing each alone."""

from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FP,
    demo_provider,
    drawn_view,
    patch_service_name,
    saved_drawings,
    settle,
    view_cell,
    view_service,
)

from glitnir.lean.project_inspection import TOOLCHAIN, supported_version
from glitnir.preview import snapshot as preview_snapshot
from glitnir.providers import Providers
from glitnir.service import runs as source_runs
from glitnir.service import views as source_views
from glitnir.service.preview import SourcePreviewService

#: Another Lean version, which Glitnir refuses.
OTHER = "4.32.1"


THREE_VIEWS = (
    "def a := 1\n-- @view circuit a\n"
    "def b := 2\n-- @view circuit b\n"
    "def c := 3\n-- @view circuit c\n"
)


MEMORY = (
    "Lean reached its memory limit of 16384 MB. The limit includes the library "
    "files Lean loads (about 4–5 GB with Mathlib) as well as its working memory. "
    "Raise the glitnir.memoryLimitMb setting and try again."
)


class _HeldRuns:
    """A fake `render_project_views` whose runs end only when released.

    Each call's lines, process cancel and other options are recorded. A run
    tells its views that Lean is drawing them, then waits for `release`, or
    for its cancel, which makes it a cancelled run. It answers the next of
    `answers`, if any, else a drawing per view naming its line.
    """

    def __init__(self, *answers: dict) -> None:
        import threading

        self.calls: list[tuple[list[int], Any, dict]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.answers = list(answers)

    def __call__(self, path, *, views, cancel, progress, **options):
        import time

        self.calls.append(([view[0] for view in views], cancel, options))
        progress("Lean is drawing it.")
        self.started.set()
        deadline = time.monotonic() + 10
        while not (self.release.is_set() or cancel.is_set()):
            assert time.monotonic() < deadline, "never released"
            time.sleep(0.005)
        if cancel.is_set():
            return {"error": "View drawing cancelled.", "transient": True}
        if self.answers:
            return self.answers.pop(0)
        return {"views": [{"svg": f"<svg>{view[0]}</svg>"} for view in views]}

    def lines(self) -> list[list[int]]:
        return [lines for lines, _cancel, _options in self.calls]


def _run_service(tmp_path, monkeypatch, runs, text=THREE_VIEWS, **options):
    """A view service drawing with `runs`, in a known environment."""
    from glitnir.service import preview as source_preview

    monkeypatch.setattr(source_views, "render_project_views", runs)
    for module in (source_preview, source_runs, preview_snapshot):
        monkeypatch.setattr(
            module,
            "environment_fingerprint",
            lambda *a, **k: {"sha256": FP, "stable_sha256": FP, "packages": []},
        )
    view_service(tmp_path, monkeypatch, text)
    return SourcePreviewService(tmp_path / "A.lean", **options)


def _ask(service, line: int) -> dict:
    return service.view(view_cell(service, line), line)


def _key(text: str, line: int) -> str:
    from glitnir.notebook.directives import evidence_key

    return evidence_key(text.split("\n")[line - 1], 0)


def test_views_of_one_provider_draw_in_one_run(tmp_path, monkeypatch):
    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        first = _ask(service, 2)
        assert runs.started.wait(10)
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        assert runs.lines() == [[2, 4, 6]]
        assert runs.calls[0][2]["environment_sha256"] == FP
        assert answers[2]["job"] == first["job"]
        assert len({answer["job"] for answer in answers.values()}) == 3
        for answer in answers.values():
            assert answer == {
                "status": "drawing",
                "message": "Lean is drawing it.",
                "job": answer["job"],
            }
            assert service.view_job(answer["job"]) == answer
        runs.release.set()
        settle(service)
        for line, answer in answers.items():
            drawn = {"svg": f"<svg>{line}</svg>"}
            assert service.view_job(answer["job"]) == drawn
            assert _ask(service, line) == drawn
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6]]
    assert set(saved_drawings(service.path)) == {
        _key(THREE_VIEWS, line) for line in (2, 4, 6)
    }


def test_saved_or_cached_views_are_not_redrawn_in_a_run(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SnapshotError

    runs = _HeldRuns()
    runs.release.set()
    alone = THREE_VIEWS.split("def b")[0]
    first = _run_service(tmp_path, monkeypatch, runs, alone)
    try:
        assert drawn_view(first, view_cell(first, 2), 2) == {"svg": "<svg>2</svg>"}
    finally:
        first.close()
    first.path.write_text(THREE_VIEWS)
    # Drawings of this service are kept in memory only.
    service = SourcePreviewService(first.path, save_drawings=False)
    try:
        runs.release.clear()
        runs.started.clear()
        _ask(service, 4)
        assert runs.started.wait(10)
        sha = service.snapshot()["sha256"]
        service.cancel_view(sha, 6)
        runs.release.set()
        settle(service)
        with pytest.raises(SnapshotError, match="not being drawn"):
            service.cancel_view(sha, 6)
        # Line 2's drawing is saved, line 4's is in memory: only 6 is drawn.
        assert drawn_view(service, view_cell(service, 6), 6) == {"svg": "<svg>6</svg>"}
        assert _ask(service, 2) == {"svg": "<svg>2</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2], [4, 6], [6]]
    assert set(saved_drawings(service.path)) == {_key(THREE_VIEWS, 2)}


def test_directive_inside_a_command_is_drawn_alone(tmp_path, monkeypatch):
    """Only directives at a command boundary of the trusted lexer join a run.

    The command before the directive must end above it
    (`command_boundaries(text).ends_at(line - 1)`), or only comments may
    precede it. A directive inside the `theorem … := by` block is drawn
    alone, when it is the view asked for, and never in another view's run.
    """
    text = (
        "-- Notes.\n"
        "-- @view circuit a\n"  # only comments before it
        "#check 1\n"
        "def a := 1\n"
        "-- @view circuit a\n"
        "theorem t : True := by\n"
        "-- @view circuit t\n"  # inside the theorem
        "  trivial\n"
        "\n"
        "-- @view circuit t\n"
    )
    runs = _HeldRuns()
    runs.release.set()
    service = _run_service(tmp_path, monkeypatch, runs, text, save_drawings=False)
    try:
        assert drawn_view(service, view_cell(service, 5), 5) == {"svg": "<svg>5</svg>"}
        assert drawn_view(service, view_cell(service, 7), 7) == {"svg": "<svg>7</svg>"}
    finally:
        service.close()
    other = SourcePreviewService(service.path, save_drawings=False)
    try:
        assert drawn_view(other, view_cell(other, 7), 7) == {"svg": "<svg>7</svg>"}
    finally:
        other.close()
    assert runs.lines() == [[2, 5, 10], [7], [7]]


def test_stop_drops_only_that_view(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SnapshotError

    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        assert runs.started.wait(10)
        sha = service.snapshot()["sha256"]
        service.cancel_view(sha, 4)
        cancelled = {"error": source_views.VIEW_CANCELLED}
        # Answered at once; the run goes on for the others.
        assert service.view_job(answers[4]["job"]) == cancelled
        assert service.view_job(answers[2]["job"])["status"] == "drawing"
        with pytest.raises(SnapshotError, match="not being drawn"):
            service.cancel_view(sha, 4)
        runs.release.set()
        settle(service)
        assert service.view_job(answers[2]["job"]) == {"svg": "<svg>2</svg>"}
        assert service.view_job(answers[4]["job"]) == cancelled
        assert service.view_job(answers[6]["job"]) == {"svg": "<svg>6</svg>"}
    finally:
        service.close()
    [(_lines, cancel, _options)] = runs.calls
    assert not cancel.is_set()
    assert set(saved_drawings(service.path)) == {
        _key(THREE_VIEWS, 2),
        _key(THREE_VIEWS, 6),
    }


def test_stopping_every_view_stops_the_run(tmp_path, monkeypatch):

    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        assert runs.started.wait(10)
        sha = service.snapshot()["sha256"]
        [(_lines, cancel, _options)] = runs.calls
        service.cancel_view(sha, 2)
        service.cancel_view(sha, 6)
        assert not cancel.is_set()
        service.cancel_view(sha, 4)
        assert cancel.is_set()
        settle(service)
        for answer in answers.values():
            assert service.view_job(answer["job"]) == {
                "error": source_views.VIEW_CANCELLED
            }
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6]]
    assert saved_drawings(service.path) == {}


def test_failed_run_of_several_views_retries_one_per_run(tmp_path, monkeypatch):
    runs = _HeldRuns({"error": MEMORY})
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        runs.release.set()
        settle(service)
        for line, answer in answers.items():
            assert service.view_job(answer["job"]) == {"svg": f"<svg>{line}</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6], [2], [4], [6]]
    # Each view drawn alone can be stopped alone.
    cancels = [cancel for _lines, cancel, _options in runs.calls]
    assert len({id(cancel) for cancel in cancels}) == 4
    assert len(saved_drawings(service.path)) == 3


@pytest.mark.parametrize(
    "message", ["Project check timed out.", "View drawing cancelled."]
)
def test_timed_out_run_is_not_retried(tmp_path, monkeypatch, message):
    failure = {"error": message, "transient": True}
    runs = _HeldRuns(failure)
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        runs.release.set()
        settle(service)
        for answer in answers.values():
            assert service.view_job(answer["job"]) == failure
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6]]


def test_views_of_a_timed_out_run_are_drawn_alone_when_asked_again(
    tmp_path, monkeypatch
):
    """The same views together would time out again: each is drawn alone."""
    runs = _HeldRuns({"error": "Project check timed out.", "transient": True})
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        _ask(service, 2)
        runs.release.set()
        settle(service)
        assert drawn_view(service, view_cell(service, 4), 4) == {"svg": "<svg>4</svg>"}
        assert drawn_view(service, view_cell(service, 2), 2) == {"svg": "<svg>2</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6], [4], [2]]


def test_views_of_a_cancelled_run_are_drawn_together_again(tmp_path, monkeypatch):
    failure = {"error": "View drawing cancelled.", "transient": True}
    runs = _HeldRuns(failure)
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        _ask(service, 2)
        runs.release.set()
        settle(service)
        assert drawn_view(service, view_cell(service, 4), 4) == {"svg": "<svg>4</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4, 6], [2, 4, 6]]


def test_a_failed_run_of_one_view_is_not_retried(tmp_path, monkeypatch):
    runs = _HeldRuns({"error": MEMORY})
    runs.release.set()
    text = THREE_VIEWS.split("def b")[0]
    service = _run_service(tmp_path, monkeypatch, runs, text)
    try:
        assert drawn_view(service, view_cell(service, 2), 2) == {"error": MEMORY}
    finally:
        service.close()
    assert runs.lines() == [[2]]


def test_views_of_another_provider_wait(tmp_path, monkeypatch):
    from glitnir.providers import ViewProvider

    graph = ViewProvider("graph", frozenset({"graph"}), (), "", "g:", lambda *a: "")
    runs = _HeldRuns()
    text = "def a := 1\n-- @view circuit a\n-- @view graph a\n-- @view circuit a\n"
    both = Providers({"circuit": demo_provider(), "graph": graph}, {}, {}, {})
    service = _run_service(tmp_path, monkeypatch, runs, text, providers=lambda: both)
    try:
        first = _ask(service, 2)
        assert runs.started.wait(10)
        assert _ask(service, 3) == {
            "status": "queued",
            "message": source_views.VIEW_QUEUED,
            "job": first["job"],
        }
        runs.release.set()
        assert drawn_view(service, view_cell(service, 3), 3) == {"svg": "<svg>3</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4], [3]]
    assert [options["provider"].name for *_, options in runs.calls] == [
        "demo",
        "graph",
    ]


def test_close_ends_every_view_of_the_run(tmp_path, monkeypatch):
    from glitnir.service import preview as source_preview

    runs = _HeldRuns()
    service = _run_service(tmp_path, monkeypatch, runs)
    answers = {line: _ask(service, line) for line in (2, 4, 6)}
    assert runs.started.wait(10)
    service.close()
    assert not service._views._view_worker.is_alive()
    assert runs.calls[0][1].is_set()
    for answer in answers.values():
        assert service.view_job(answer["job"]) == {"error": source_preview.CLOSED}


def test_a_failing_run_ends_every_view(tmp_path, monkeypatch):
    import threading

    release = threading.Event()

    def runs(path, **options):
        assert release.wait(10)
        raise RuntimeError("boom")

    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        answers = {line: _ask(service, line) for line in (2, 4, 6)}
        release.set()
        settle(service)
        for answer in answers.values():
            assert service.view_job(answer["job"]) == {
                "error": "View drawing failed: boom"
            }
        assert service._views._view_run is None
    finally:
        service.close()


def test_a_run_draws_no_more_views_than_are_kept(tmp_path, monkeypatch):
    """Every answer of a run must outlast it, so a run is at most the cache size."""
    monkeypatch.setattr(source_views, "MAX_CACHED_VIEWS", 2)
    runs = _HeldRuns()
    runs.release.set()
    service = _run_service(tmp_path, monkeypatch, runs)
    try:
        assert drawn_view(service, view_cell(service, 4), 4) == {"svg": "<svg>4</svg>"}
    finally:
        service.close()
    assert runs.lines() == [[2, 4]]


#: The words every refusal shares after the version sentence.
LEAN_OFF = (
    f"Glitnir checks Lean {supported_version()}, so checking, proof steps, views, "
    "evidence and decisions are off; reading works as usual."
)


UNSUPPORTED_TEXT = (
    "import Demo.View\n\ndef c := 1\n-- @view circuit c\n"
    "-- @evidence simulate c\n-- @decide band : entailment c : c > 0\n--   |- c >= 0\n"
)


def _unsupported_service(tmp_path: Path, monkeypatch, toolchain: str | None):
    """A Lake notebook with every directive; each Lean runner fails the test."""
    import threading

    from glitnir.providers import (
        DecisionProvider,
        DecisionRequest,
        EvidenceProvider,
        EvidenceRequest,
    )
    from glitnir.service import preview as source_preview
    from glitnir.service import proof_steps

    def forbidden(*args, **kwargs):
        pytest.fail("a refused request reached Lean, Lake or a provider")

    evidence = EvidenceProvider(
        name="demo",
        verbs=frozenset({"simulate"}),
        imports=(),
        preamble="",
        marker="m:",
        parse=lambda verb, target, options: EvidenceRequest(verb, target, {}),
        command=forbidden,
        compute=forbidden,
        unavailable=lambda: None,
    )
    decision = DecisionProvider(
        name="demo",
        kinds=frozenset({"entailment"}),
        imports=(),
        parse=lambda name, kind, goal: DecisionRequest(name, kind, goal, {}),
        decide=forbidden,
        unavailable=lambda: None,
    )
    providers = Providers(
        {"circuit": demo_provider()},
        {"simulate": evidence},
        {"entailment": decision},
        {},
    )
    monkeypatch.setattr(source_preview, "load_providers", lambda: providers)
    for module in (source_preview, source_runs, preview_snapshot):
        monkeypatch.setattr(
            module, "environment_fingerprint", lambda *a, **k: {"sha256": FP}
        )
    for name in (
        "check_project",
        "check_candidate",
        "inspect_project",
        "render_project_views",
        "run_provider_lean",
        "provider_imports_error",
    ):
        patch_service_name(monkeypatch, name, forbidden)
    monkeypatch.setattr(proof_steps, "trace_file", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    (tmp_path / "lakefile.toml").write_text('name = "demo"')
    (tmp_path / "lake-manifest.json").write_text("{}")
    if toolchain is not None:
        (tmp_path / "lean-toolchain").write_text(toolchain)
    path = tmp_path / "A.lean"
    path.write_text(UNSUPPORTED_TEXT)
    return SourcePreviewService(path)


def test_the_snapshot_says_whether_the_lean_version_is_supported(
    tmp_path: Path,
) -> None:
    (tmp_path / "lakefile.toml").write_text('name = "demo"')
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\r\n")
    path = tmp_path / "A.lean"
    path.write_text("def c := 1\n")
    assert SourcePreviewService(path).snapshot()["preview"]["lean"] == {
        "supported": True,
        "declared": TOOLCHAIN,
        "toolchain": TOOLCHAIN,
        "message": None,
    }
    (tmp_path / "lean-toolchain").write_text(f"leanprover/lean4:v{OTHER}\n")
    assert SourcePreviewService(path).snapshot()["preview"]["lean"] == {
        "supported": False,
        "declared": f"leanprover/lean4:v{OTHER}",
        "toolchain": TOOLCHAIN,
        "message": f"This project uses Lean {OTHER}. {LEAN_OFF}",
    }


@pytest.mark.parametrize(
    "toolchain, message",
    [
        (f"leanprover/lean4:v{OTHER}\n", f"This project uses Lean {OTHER}. {LEAN_OFF}"),
        (None, f"This project declares no Lean version. {LEAN_OFF}"),
        (
            f"v{OTHER}\n{TOOLCHAIN}\n",
            "This project's lean-toolchain could not be read: expected one "
            f"toolchain declaration. {LEAN_OFF}",
        ),
    ],
)
def test_every_lean_request_is_refused_up_front_on_another_version(
    tmp_path: Path, monkeypatch, toolchain, message
) -> None:
    """Nothing starts a worker, takes a slot or reaches Lake, Lean or a provider."""
    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.notebook.document import NotebookError

    service = _unsupported_service(tmp_path, monkeypatch, toolchain)
    snapshot = service.snapshot()
    assert snapshot["preview"]["lean"]["message"] == message
    sha = snapshot["sha256"]
    (view,) = snapshot["views"]
    assert not view["error"]
    assert [card["line"] for card in snapshot["evidence"]] == [5]
    assert [card["line"] for card in snapshot["decisions"]] == [6]
    with pytest.raises(NotebookError) as refused:
        service.inspect(sha)
    assert str(refused.value) == message
    with pytest.raises(NotebookError) as refused:
        service.check(sha)
    assert str(refused.value) == message
    with pytest.raises(NotebookError) as refused:
        service.check(sha, through_line=3)
    assert str(refused.value) == message
    with pytest.raises(ValueError) as refused:
        service.proof_steps.start(sha)
    assert str(refused.value) == message
    assert service.view(view["cell"], view["line"]) == {"error": message}
    with pytest.raises(SnapshotError) as refused:
        service.run_evidence(sha, 5)
    assert str(refused.value) == message
    with pytest.raises(SnapshotError) as refused:
        service.run_decision(sha, 6)
    assert str(refused.value) == message
    # Each refusal leaves every gate open and nothing reported as running.
    after = service.snapshot()
    assert after["preview"]["check"] is None
    assert after["preview"]["inspection"] is None
    assert service.proof_steps.snapshot() == {"status": "idle", "nodes": []}
    assert service._inspection_lock.acquire(blocking=False)
    service._inspection_lock.release()
    assert service._runs._run_lock.acquire(blocking=False)
    service._runs._run_lock.release()
    assert service._runs._running is None and service._runs._run_errors == {}
    service.close()
