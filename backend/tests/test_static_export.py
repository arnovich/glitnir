"""Static export shows saved results as recorded and never checks or writes."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from glitnir.lean.project_views import view_provider_sha256
from glitnir.notebook import source_preview, static_export
from glitnir.notebook.check_records import (
    evidence_prefix_sha256,
    load_records,
    records_from_check,
    records_path,
    save_records,
    with_evidence,
    with_view,
)
from glitnir.notebook.evidence_cards import checked_evidence, evidence_key
from glitnir.notebook.source_preview import SourcePreviewService
from glitnir.notebook.static_export import RECORDED_NOTICE, SavedNotebook
from glitnir.providers import (
    EvidenceProvider,
    EvidenceRequest,
    Providers,
    ViewProvider,
)

DIRECTIVE = "-- @evidence simulate c (h := 1/10)"
TEXT = (
    "import Demo.Base\n\n"
    "def c : Nat := 1\n"
    "theorem result : True := by\n"
    "  trivial\n"
    f"{DIRECTIVE}\n"
    "-- @view circuit c\n"
)
RECORDED = "f" * 64


def _evidence_provider() -> EvidenceProvider:
    return EvidenceProvider(
        name="demo",
        verbs=frozenset({"simulate"}),
        imports=("Demo.Evidence",),
        preamble="",
        marker="m:",
        parse=lambda verb, target, options: EvidenceRequest(verb, target, {}),
        command=lambda request: "",
        compute=lambda request, payload: {},
        unavailable=lambda: "Install the demo extra.",
    )


def _view_provider() -> ViewProvider:
    return ViewProvider(
        name="demo",
        kinds=frozenset({"circuit"}),
        imports=("Demo.View",),
        preamble="",
        marker="m:",
        command=lambda kind, name, using: "",
    )


def _result(sha256: str) -> dict:
    return {
        "status": "checked_snapshot",
        "source_sha256": sha256,
        "environment_sha256": "e",
        "withheld": None,
        "message": "ok",
        "theorems": [
            {
                "name": "A.result",
                "status": "checked",
                "axioms": [],
                "start_line": 4,
                "end_line": 5,
                "proposition_sha256": "p",
            }
        ],
    }


def _evidence_body() -> dict:
    return checked_evidence(
        {
            "format": "glitnir.evidence/v1",
            "series": {"times": [0.0, 1.0], "values": [[1.0], [2.0]], "names": ["x"]},
            "finite": True,
            "provenance": [["solver", "demo"]],
        }
    )


@pytest.fixture
def saved(tmp_path: Path, monkeypatch) -> Path:
    """A notebook with a checked claim and evidence, saved in another environment.

    The evidence was recorded with its provider's modules, so its fingerprint
    differs from the claims'. The reader's environment is never fingerprinted.
    """
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers(
            {"circuit": _view_provider()}, {"simulate": _evidence_provider()}, {}, {}
        ),
    )
    path = tmp_path / "A.lean"
    path.write_text(TEXT)
    sha256 = hashlib.sha256(TEXT.encode()).hexdigest()
    records = records_from_check(TEXT, _result(sha256), RECORDED)
    entry = {
        "prefix_sha256": evidence_prefix_sha256(TEXT, 6),
        "fingerprint": "9" * 64,
        "provider": "demo",
        "created": "2026-10-14T12:00:00Z",
        "snapshot": _evidence_body(),
    }
    key = evidence_key(DIRECTIVE, 0)
    save_records(path, with_evidence(records, key, entry, [key]))
    return path


@pytest.fixture
def nothing_runs(monkeypatch):
    """Fail on any process, fingerprint, check, provider run or records write."""

    def forbidden(*_arguments, **_options):
        pytest.fail("a static export ran, fingerprinted or wrote something")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name in (
        "environment_fingerprint",
        "check_project",
        "check_candidate",
        "inspect_project",
        "run_provider_lean",
        "render_project_views",
        "save_records",
    ):
        monkeypatch.setattr(source_preview, name, forbidden)


def _tree(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_saved_results_are_shown_as_recorded(saved: Path, nothing_runs) -> None:
    with SavedNotebook(saved) as notebook:
        snapshot = notebook.snapshot()
    assert snapshot["preview"]["recorded"] is True
    assert snapshot["preview"]["notice"] == RECORDED_NOTICE
    assert RECORDED_NOTICE.startswith("Saved results, shown as recorded.")
    assert "not with the reader's Lean environment" in RECORDED_NOTICE
    [claim] = snapshot["project_claims"]
    assert (claim["name"], claim["status"], claim["current"]) == (
        "A.result",
        "checked",
        True,
    )
    [card] = snapshot["evidence"]
    # Recorded with the provider's modules: compared with its own fingerprint.
    assert card["status"] == "current"
    assert card["snapshot"] == _evidence_body()


def test_the_editor_preview_compares_the_same_results_with_its_environment(
    saved: Path, monkeypatch
) -> None:
    """The contrast: in another environment the live preview says out of date."""
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *_a, **_k: {"sha256": "1" * 64, "packages": []},
    )
    snapshot = SourcePreviewService(saved).snapshot()
    assert "recorded" not in snapshot["preview"]
    assert [claim["current"] for claim in snapshot["project_claims"]] == [False]
    assert [card["status"] for card in snapshot["evidence"]] == ["stale"]


def test_a_changed_source_is_still_out_of_date(saved: Path, nothing_runs) -> None:
    saved.write_text(TEXT.replace("c : Nat := 1", "c : Nat := 2"))
    with SavedNotebook(saved) as notebook:
        snapshot = notebook.snapshot()
    assert [claim["current"] for claim in snapshot["project_claims"]] == [False]
    assert [card["status"] for card in snapshot["evidence"]] == ["stale"]


def test_cards_show_saved_results_without_run_prompts(
    saved: Path, nothing_runs
) -> None:
    with SavedNotebook(saved) as notebook:
        quiet = notebook.snapshot()
        prompted = notebook.snapshot(run_prompts=True)
    assert [card["unavailable"] for card in quiet["evidence"]] == [None]
    assert quiet["directive_unavailable"] == {"evidence": {}, "decide": {}}
    assert [card["unavailable"] for card in prompted["evidence"]] == [
        "Install the demo extra."
    ]
    assert prompted["directive_unavailable"]["evidence"] == {
        "simulate": "Install the demo extra."
    }


def test_reading_for_a_static_page_writes_nothing(
    saved: Path, tmp_path: Path, nothing_runs
) -> None:
    (tmp_path / "assets").mkdir()
    figure = tmp_path / "assets" / "a.svg"
    figure.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    saved.write_text("/-! # A\n\n![Figure](assets/a.svg)\n-/\n" + saved.read_text())
    before = _tree(tmp_path)
    with SavedNotebook(saved) as notebook:
        snapshot = notebook.snapshot()
        [asset] = snapshot["assets"]
        body, media_type = notebook.asset(asset["path"], asset["sha256"])
        imports = notebook.view_imports()
    assert (body, media_type) == (figure.read_bytes(), "image/svg+xml")
    assert imports == ["Demo.Base", "Demo.View"]
    assert _tree(tmp_path) == before


def test_view_imports_need_a_drawable_view(tmp_path: Path, nothing_runs) -> None:
    path = tmp_path / "A.lean"
    path.write_text("import Demo.Base\n\ndef c : Nat := 1\n")
    with SavedNotebook(path) as notebook:
        assert notebook.view_imports() == []


def test_draw_view_waits_for_the_drawing(
    saved: Path, monkeypatch, draw_each_alone
) -> None:
    monkeypatch.setattr(static_export, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *_a, **_k: {"sha256": "2" * 64, "packages": []},
    )
    drawn = []

    def render(path, **options):
        drawn.append((options["kind"], options["name"], options["line"]))
        return {"svg": "<svg/>"}

    draw_each_alone(render)
    with SavedNotebook(saved) as notebook:
        [view] = notebook.snapshot()["views"]
        assert notebook.draw_view(view["cell"], view["line"]) == {"svg": "<svg/>"}
        # Drawn once; asking again answers from the kept drawing.
        assert notebook.draw_view(view["cell"], view["line"]) == {"svg": "<svg/>"}
        with pytest.raises(KeyError):
            notebook.draw_view(view["cell"], view["line"] + 1)
    assert drawn == [("circuit", "c", 7)]


def _with_saved_drawing(path: Path, text: str = TEXT) -> dict:
    """Save a drawing of the view at line 7, recorded in another environment."""
    entry = {
        "prefix_sha256": evidence_prefix_sha256(text, 7),
        "fingerprint": "8" * 64,
        "provider": "demo",
        "provider_sha256": view_provider_sha256(_view_provider(), "circuit", "c", ()),
        "created": "2026-10-14T12:00:00Z",
        "drawing": {"svg": "<svg><g/></svg>"},
    }
    key = evidence_key("-- @view circuit c", 0)
    save_records(path, with_view(load_records(path), key, entry, [key]))
    return entry


def test_draw_view_uses_saved_drawing_as_recorded(saved: Path, nothing_runs) -> None:
    """Shown as recorded: no fingerprint is taken and nothing is drawn."""
    entry = _with_saved_drawing(saved)
    before = _tree(saved.parent)
    with SavedNotebook(saved) as notebook:
        [view] = notebook.snapshot()["views"]
        assert notebook.draw_view(view["cell"], view["line"]) == entry["drawing"]
    assert _tree(saved.parent) == before


def test_draw_view_draws_when_saved_prefix_differs(
    saved: Path, monkeypatch, draw_each_alone
) -> None:
    _with_saved_drawing(saved, TEXT.replace("c : Nat := 1", "c : Nat := 0"))
    monkeypatch.setattr(static_export, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *_a, **_k: {"sha256": "8" * 64, "packages": []},
    )
    drawn = []

    def render(path, **options):
        drawn.append(options["line"])
        return {"svg": "<svg/>"}

    draw_each_alone(render)
    before = records_path(saved).read_bytes()
    with SavedNotebook(saved) as notebook:
        [view] = notebook.snapshot()["views"]
        assert notebook.draw_view(view["cell"], view["line"]) == {"svg": "<svg/>"}
    assert drawn == [7]
    # A static export saves no drawing of its own.
    assert records_path(saved).read_bytes() == before


def test_draw_view_reports_a_failed_drawing(
    saved: Path, monkeypatch, draw_each_alone
) -> None:
    monkeypatch.setattr(static_export, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *_a, **_k: {"sha256": "2" * 64, "packages": []},
    )
    draw_each_alone(lambda path, **options: {"error": "needs import Demo.View"})
    with SavedNotebook(saved) as notebook:
        [view] = notebook.snapshot()["views"]
        assert notebook.draw_view(view["cell"], view["line"]) == {
            "error": "needs import Demo.View"
        }


def test_draw_view_reports_a_failure_to_read_for_it(saved: Path, monkeypatch) -> None:
    """Reading the notebook or its environment fails: answered, never raised."""

    def gone(*_arguments, **_options):
        raise OSError("environment unreadable")

    monkeypatch.setattr(source_preview, "environment_fingerprint", gone)
    with SavedNotebook(saved) as notebook:
        [view] = notebook.snapshot()["views"]
        assert notebook.draw_view(view["cell"], view["line"]) == {
            "error": "View drawing failed: OSError: environment unreadable"
        }


def test_the_snapshot_names_no_local_project_path(tmp_path: Path, nothing_runs):
    import json

    project = tmp_path / "project"
    (project / "examples").mkdir(parents=True)
    (project / "lakefile.toml").write_text('name = "demo"\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.1\n")
    path = project / "examples" / "A.lean"
    path.write_text("def c : Nat := 1\n")
    with SavedNotebook(path) as notebook:
        snapshot = notebook.snapshot()
    assert snapshot["preview"]["project"] == {
        "kind": "lake",
        "root": "..",
        "toolchain": "leanprover/lean4:v4.32.1",
    }
    assert str(tmp_path) not in json.dumps(snapshot)


# ----- the editor's preview never shows results as recorded -------------------


def test_the_editor_preview_and_linked_notebooks_are_never_as_recorded(
    saved: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *_a, **_k: {"sha256": RECORDED, "packages": []},
    )
    (saved.parent / "B.lean").write_text("def b : Nat := 2\n")
    service = SourcePreviewService(saved)
    try:
        snapshots = [service.snapshot(), service.linked("B.lean")]
    finally:
        service.close()
    for snapshot in snapshots:
        assert "recorded" not in snapshot["preview"]
        assert snapshot["preview"]["notice"] != RECORDED_NOTICE
    # In the recorded environment the editor's own comparison finds it current.
    assert [claim["current"] for claim in snapshots[0]["project_claims"]] == [True]


def _empty(*_arguments, **_options):
    return {"sha256": "", "packages": []}


def _failing(*_arguments, **_options):
    raise OSError("no environment")


@pytest.mark.parametrize("fingerprint", [_empty, _failing])
def test_an_unknown_environment_shows_saved_results_out_of_date(
    saved: Path, monkeypatch, fingerprint
) -> None:
    monkeypatch.setattr(source_preview, "environment_fingerprint", fingerprint)
    snapshot = SourcePreviewService(saved).snapshot()
    assert [claim["current"] for claim in snapshot["project_claims"]] == [False]
    assert [card["status"] for card in snapshot["evidence"]] == ["stale"]


def test_results_saved_under_an_empty_fingerprint_are_never_current(
    saved: Path, monkeypatch
) -> None:
    """A record saved with '' must not match an environment that reads as ''."""
    import json

    from glitnir.notebook.check_records import records_path

    file = records_path(saved)
    records = json.loads(file.read_text())
    records["environment"]["fingerprint"] = ""
    for entry in records["evidence"].values():
        entry["fingerprint"] = ""
    file.write_text(json.dumps(records))
    monkeypatch.setattr(source_preview, "environment_fingerprint", _empty)
    snapshot = SourcePreviewService(saved).snapshot()
    assert [claim["current"] for claim in snapshot["project_claims"]] == [False]
    assert [card["status"] for card in snapshot["evidence"]] == ["stale"]
    with SavedNotebook(saved) as notebook:
        recorded = notebook.snapshot()
    assert [claim["current"] for claim in recorded["project_claims"]] == [False]
    assert [card["status"] for card in recorded["evidence"]] == ["stale"]


def test_a_check_in_an_unknown_environment_saves_nothing(
    saved: Path, monkeypatch
) -> None:
    from glitnir.notebook.check_records import records_path

    before = records_path(saved).read_bytes()
    monkeypatch.setattr(source_preview, "environment_fingerprint", _empty)
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: _result(k["expected_sha256"])
    )
    service = SourcePreviewService(saved)
    try:
        sha256 = service.snapshot()["sha256"]
        service.check(sha256)
        service._worker.join(timeout=10)
        check = service.snapshot()["preview"]["check"]
    finally:
        service.close()
    assert check["status"] == "checked_snapshot"
    assert "Results could not be saved" in check["message"]
    assert "fingerprint is unknown" in check["message"]
    assert records_path(saved).read_bytes() == before
