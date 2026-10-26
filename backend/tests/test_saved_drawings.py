"""Saved drawings: a view's drawing is saved with the notebook and reused."""

from pathlib import Path

from conftest import (
    FP,
    VIEW_TEXT,
    demo_provider,
    drawn_view,
    saved_drawings,
    view_cell,
    view_service,
)

from glitnir.preview import snapshot as preview_snapshot
from glitnir.providers import Providers
from glitnir.service import preview as source_preview
from glitnir.service import runs as source_runs
from glitnir.service.preview import SourcePreviewService


def _saving_view_service(
    tmp_path: Path,
    monkeypatch,
    draw_each_alone,
    drawing=None,
    unknown=False,
    text=VIEW_TEXT,
):
    """A view service with a fixed fingerprint and a counted, faked renderer."""
    renders: list[dict] = []
    answer = {"svg": "<svg/>"} if drawing is None else drawing
    draw_each_alone(lambda path, **options: renders.append(options) or dict(answer))
    for module in (source_preview, source_runs, preview_snapshot):
        monkeypatch.setattr(
            module,
            "environment_fingerprint",
            lambda *a, **k: {
                "sha256": FP,
                "stable_sha256": FP,
                "unknown": unknown,
                "packages": [],
            },
        )
    return view_service(tmp_path, monkeypatch, text), renders


def test_drawing_is_saved_and_reused_without_lean(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.lean.project_views import view_provider_sha256
    from glitnir.notebook.check_records import evidence_prefix_sha256
    from glitnir.notebook.directives import evidence_key

    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        assert drawn_view(service, view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert len(renders) == 1
    [(key, entry)] = saved_drawings(service.path).items()
    assert key == evidence_key("-- @view circuit c using a, b", 0)
    assert entry["prefix_sha256"] == evidence_prefix_sha256(VIEW_TEXT, 4)
    assert entry["fingerprint"] == FP
    assert entry["provider"] == "demo"
    assert entry["provider_sha256"] == view_provider_sha256(
        demo_provider(), "circuit", "c", ("a", "b")
    )
    assert entry["drawing"] == {"svg": "<svg/>"}
    again = SourcePreviewService(service.path)
    try:
        assert again.view(view_cell(again), 4) == {"svg": "<svg/>"}
        assert again.view(view_cell(again), 4) == {"svg": "<svg/>"}
    finally:
        again.close()
    assert len(renders) == 1


def test_uncertain_drawing_is_not_saved(tmp_path, monkeypatch, draw_each_alone):
    from glitnir.notebook.check_records import records_path

    service, renders = _saving_view_service(
        tmp_path, monkeypatch, draw_each_alone, unknown=True
    )
    try:
        answer = drawn_view(service, view_cell(service), 4)
    finally:
        service.close()
    assert answer == {"svg": "<svg/>", "uncertain": True}
    assert not records_path(service.path).exists()


def test_a_drawing_with_an_unhashable_provider_is_not_saved(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.notebook.check_records import records_path
    from glitnir.providers import ViewProvider

    def command(kind, name, using):
        raise RuntimeError("no command")

    _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    provider = ViewProvider("demo", frozenset({"circuit"}), (), "", "m:", command)
    service = SourcePreviewService(
        tmp_path / "A.lean",
        providers=lambda: Providers({"circuit": provider}, {}, {}, {}),
    )
    try:
        assert drawn_view(service, view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert not records_path(service.path).exists()


def test_saved_drawing_redrawn_after_edit_above(tmp_path, monkeypatch, draw_each_alone):
    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        drawn_view(service, view_cell(service), 4)
    finally:
        service.close()
    # An edit below the directive keeps the saved drawing current.
    service.path.write_text(VIEW_TEXT + "def e := 3\n")
    below = SourcePreviewService(service.path)
    try:
        assert below.view(view_cell(below), 4) == {"svg": "<svg/>"}
    finally:
        below.close()
    assert len(renders) == 1
    service.path.write_text(VIEW_TEXT.replace("def c := 1", "def c := 2"))
    above = SourcePreviewService(service.path)
    try:
        assert drawn_view(above, view_cell(above), 4) == {"svg": "<svg/>"}
    finally:
        above.close()
    assert len(renders) == 2


def test_saved_drawing_redrawn_when_the_provider_changes(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.providers import ViewProvider

    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        drawn_view(service, view_cell(service), 4)
    finally:
        service.close()
    provider = ViewProvider(
        "demo", frozenset({"circuit"}), (), "", "m:", lambda *a: "#draw"
    )
    changed = SourcePreviewService(
        service.path, providers=lambda: Providers({"circuit": provider}, {}, {}, {})
    )
    try:
        assert drawn_view(changed, view_cell(changed), 4) == {"svg": "<svg/>"}
    finally:
        changed.close()
    assert len(renders) == 2


def test_drawing_shown_when_saving_fails(tmp_path, monkeypatch, draw_each_alone):
    from glitnir.notebook.check_records import records_path

    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    try:
        cell = view_cell(service)
        assert drawn_view(service, cell, 4) == {"svg": "<svg/>"}
        # Kept in memory all the same: asking again draws nothing.
        assert service.view(cell, 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert len(renders) == 1
    assert sidecar.read_text() == "not json"


def test_a_drawing_too_large_to_save_is_still_shown(
    tmp_path, monkeypatch, draw_each_alone
):
    from glitnir.notebook import check_records

    monkeypatch.setattr(check_records, "MAX_VIEWS_BYTES", 10)
    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)
    try:
        assert drawn_view(service, view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert saved_drawings(service.path) == {}


def test_a_drawing_is_not_saved_when_the_source_changed_during_it(
    tmp_path,
    monkeypatch,
    draw_each_alone,
):
    service, renders = _saving_view_service(tmp_path, monkeypatch, draw_each_alone)

    def render(path, **options):
        path.write_text(VIEW_TEXT + "def e := 3\n")
        return {"svg": "<svg/>"}

    draw_each_alone(render)
    try:
        assert drawn_view(service, view_cell(service), 4) == {"svg": "<svg/>"}
    finally:
        service.close()
    assert saved_drawings(service.path) == {}


def test_saved_drawing_has_no_transient_fields(tmp_path, monkeypatch, draw_each_alone):
    drawn = {"svg": "<svg/>", "proofs_skipped": True}
    service, renders = _saving_view_service(
        tmp_path, monkeypatch, draw_each_alone, drawing=drawn
    )
    try:
        assert drawn_view(service, view_cell(service), 4) == drawn
    finally:
        service.close()
    [entry] = saved_drawings(service.path).values()
    assert entry["drawing"] == {"svg": "<svg/>"}
    assert "uncertain" not in entry["drawing"]
    assert "proofs_skipped" not in entry["drawing"]


def test_saved_drawings_of_identical_directives_keep_their_own_keys(
    tmp_path,
    monkeypatch,
    draw_each_alone,
):
    from glitnir.notebook.directives import evidence_key

    text = "def c := 1\n-- @view circuit c\ndef d := 2\n-- @view circuit c\n"
    service, renders = _saving_view_service(
        tmp_path, monkeypatch, draw_each_alone, text=text
    )
    try:
        drawn_view(service, view_cell(service, 2), 2)
        drawn_view(service, view_cell(service, 4), 4)
    finally:
        service.close()
    assert set(saved_drawings(service.path)) == {
        evidence_key("-- @view circuit c", 0),
        evidence_key("-- @view circuit c", 1),
    }
    # Removing the second directive drops its drawing at the next save.
    service.path.write_text("def c := 2\n-- @view circuit c\n")
    again = SourcePreviewService(service.path)
    try:
        drawn_view(again, view_cell(again, 2), 2)
    finally:
        again.close()
    assert set(saved_drawings(service.path)) == {evidence_key("-- @view circuit c", 0)}
