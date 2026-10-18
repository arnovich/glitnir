"""Static export shows saved results as recorded and never checks or writes."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from glitnir.notebook import source_preview, static_export
from glitnir.notebook.check_records import (
    evidence_prefix_sha256,
    records_from_check,
    save_records,
    with_evidence,
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
        "fingerprint": "g" * 64,
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
        "render_project_view",
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
        lambda *_a, **_k: {"sha256": "other", "packages": []},
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


def test_draw_view_waits_for_the_drawing(saved: Path, monkeypatch) -> None:
    monkeypatch.setattr(static_export, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *_a, **_k: {"sha256": "reader", "packages": []},
    )
    drawn = []

    def render(path, **options):
        drawn.append((options["kind"], options["name"], options["line"]))
        return {"svg": "<svg/>"}

    monkeypatch.setattr(source_preview, "render_project_view", render)
    with SavedNotebook(saved) as notebook:
        [view] = notebook.snapshot()["views"]
        assert notebook.draw_view(view["cell"], view["line"]) == {"svg": "<svg/>"}
        # Drawn once; asking again answers from the kept drawing.
        assert notebook.draw_view(view["cell"], view["line"]) == {"svg": "<svg/>"}
        with pytest.raises(KeyError):
            notebook.draw_view(view["cell"], view["line"] + 1)
    assert drawn == [("circuit", "c", 7)]


def test_draw_view_reports_a_failed_drawing(saved: Path, monkeypatch) -> None:
    monkeypatch.setattr(static_export, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        source_preview,
        "environment_fingerprint",
        lambda *_a, **_k: {"sha256": "reader", "packages": []},
    )
    monkeypatch.setattr(
        source_preview,
        "render_project_view",
        lambda path, **options: {"error": "needs import Demo.View"},
    )
    with SavedNotebook(saved) as notebook:
        [view] = notebook.snapshot()["views"]
        assert notebook.draw_view(view["cell"], view["line"]) == {
            "error": "needs import Demo.View"
        }
