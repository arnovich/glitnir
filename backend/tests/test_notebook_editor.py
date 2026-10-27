"""The editor consumes installed renderer assets without a source checkout."""

import json
from pathlib import Path

import pytest

from glitnir.preview import editor
from glitnir.preview.editor import (
    export_renderer,
    renderer_directory,
    runtime_info,
)


def test_renderer_export_is_complete_and_matches_runtime(tmp_path: Path) -> None:
    """Ship the canonical UI, fonts and notices with a compatibility identity."""
    target = tmp_path / "media"
    exported = export_renderer(target)
    assert exported == runtime_info()
    assert exported["apiVersion"] == 2
    assert len(exported["rendererSha256"]) == 64
    assert json.loads((target / "editor-runtime.json").read_text()) == exported
    for name in ("index.html", "workbench.js", "cards.js", "katex/LICENSE"):
        assert (target / name).is_file()
    assert list((target / "katex/fonts").glob("*.woff2"))


def test_export_refuses_to_overwrite_existing_files(tmp_path: Path) -> None:
    """A build exporter must never erase an arbitrary existing destination."""
    marker = tmp_path / "keep.txt"
    marker.write_text("owner data")
    with pytest.raises(FileExistsError):
        export_renderer(tmp_path)
    assert marker.read_text() == "owner data"


def test_identity_tracks_content_and_paths_but_ignores_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relocation is harmless; changed UI bytes or filenames need a rebuild."""
    monkeypatch.setattr(editor, "_APP", tmp_path)
    asset = tmp_path / "app.js"
    asset.write_text("first")
    original = runtime_info()
    (tmp_path / ".backup").write_text("local")
    (tmp_path / "app.js~").write_text("backup")
    assert runtime_info() == original
    asset.write_text("second")
    changed = runtime_info()
    assert changed != original
    asset.rename(tmp_path / "renamed.js")
    assert runtime_info() != changed


def test_cli_exports_the_installed_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build command emits JSON and writes the same contract beside assets."""
    destination = tmp_path / "assets"
    monkeypatch.setattr("sys.argv", ["editor", "--export", str(destination)])
    editor.main()
    assert json.loads(capsys.readouterr().out) == runtime_info()
    assert (destination / "index.html").is_file()


def test_renderer_directory_locates_the_assets_a_gallery_needs() -> None:
    """The public accessor points at the packaged renderer, not a copy."""
    directory = renderer_directory()
    assert directory == editor._APP
    for name in ("index.html", "workbench.js", "app.css", "katex/katex.min.css"):
        assert (directory / name).is_file()
