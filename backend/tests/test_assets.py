"""Images in prose: resolved only inside the notebook's assets folder."""

import hashlib
import os
from pathlib import Path

import pytest

from glitnir.preview import assets
from glitnir.preview.assets import AssetError, read, references, resolve, statuses

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'


@pytest.fixture
def notebook(tmp_path: Path) -> Path:
    path = tmp_path / "Note.lean"
    path.write_text("/-! ![Plot](assets/plot.png) -/\n", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/plot.png").write_bytes(PNG)
    return path


def test_references_are_whole_image_lines_in_file_order_once_each() -> None:
    found = references(
        [
            "# Title\n\n![Plot](assets/plot.png)\n  ![](assets/b.svg)  ",
            "Inline ![not a figure](assets/inline.png) in a sentence.\n"
            "![Plot again](assets/plot.png)\n![External](https://x.invalid/a.png)",
        ]
    )
    assert found == ("assets/plot.png", "assets/b.svg", "https://x.invalid/a.png")


@pytest.mark.parametrize(
    "reference", ["assets/plot.png", "assets/sub/plot.PNG", "assets/a.jpeg"]
)
def test_relative_image_paths_inside_the_folder_resolve(notebook, reference) -> None:
    target = resolve(notebook, reference)
    assert target.parent.is_relative_to((notebook.parent / "assets").resolve())


@pytest.mark.parametrize(
    "reference",
    [
        "assets/../Note.lean",
        "assets/../secret.png",
        "assets/sub/../../x.png",
        "/etc/passwd.png",
        "figure.png",
        "other/figure.png",
        "assets",
        "assets/",
        "assets/notes.txt",
        "assets/script.js",
        "assets\\plot.png",
        "https://example.invalid/a.png",
        "",
    ],
)
def test_paths_outside_the_folder_or_not_images_are_invalid(
    notebook, reference
) -> None:
    with pytest.raises(AssetError) as error:
        resolve(notebook, reference)
    assert error.value.status == "invalid"


def test_symlinks_leaving_the_folder_are_invalid(notebook, tmp_path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (tmp_path / "assets/escape.png").symlink_to(outside)
    with pytest.raises(AssetError, match="leaves"):
        resolve(notebook, "assets/escape.png")


def test_a_symlinked_assets_folder_is_invalid(tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.png").write_bytes(PNG)
    home = tmp_path / "home"
    home.mkdir()
    (home / "assets").symlink_to(elsewhere)
    with pytest.raises(AssetError) as error:
        resolve(home / "Note.lean", "assets/a.png")
    assert error.value.status == "invalid"


def test_a_symlink_to_a_non_image_inside_the_folder_is_invalid(notebook) -> None:
    folder = notebook.parent / "assets"
    (folder / "notes.txt").write_text("secret")
    (folder / "disguised.png").symlink_to(folder / "notes.txt")
    with pytest.raises(AssetError) as error:
        resolve(notebook, "assets/disguised.png")
    assert error.value.status == "invalid"


def test_statuses_report_hash_missing_invalid_and_too_large(
    notebook, monkeypatch
) -> None:
    folder = notebook.parent / "assets"
    (folder / "big.svg").write_bytes(SVG + b" " * 64)
    os.mkfifo(folder / "pipe.png")
    monkeypatch.setattr(assets, "MAX_ASSET_BYTES", len(SVG) + 8)
    report = statuses(
        notebook,
        (
            "assets/plot.png",
            "assets/gone.png",
            "assets/../Note.lean",
            "assets/big.svg",
            "assets/pipe.png",
        ),
    )
    ok, missing, invalid, large, pipe = report
    assert ok == {
        "path": "assets/plot.png",
        "status": "ok",
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    assert missing["status"] == "missing" and "missing" in missing["message"]
    assert invalid["status"] == "invalid" and invalid["message"]
    assert large["status"] == "too_large" and "large" in large["message"]
    assert pipe["status"] == "invalid"
    assert all("sha256" not in entry for entry in report[1:])


def test_statuses_follow_a_changed_file(notebook) -> None:
    [before] = statuses(notebook, ("assets/plot.png",))
    target = notebook.parent / "assets/plot.png"
    target.write_bytes(PNG + b"changed")
    os.utime(target, ns=(1, 1))
    [after] = statuses(notebook, ("assets/plot.png",))
    assert after["sha256"] == hashlib.sha256(PNG + b"changed").hexdigest()
    assert after["sha256"] != before["sha256"]


def test_read_returns_bytes_and_type_only_for_the_expected_version(
    notebook,
) -> None:
    digest = hashlib.sha256(PNG).hexdigest()
    refs = ("assets/plot.png",)
    assert read(notebook, refs, "assets/plot.png", digest) == (PNG, "image/png")
    with pytest.raises(AssetError) as error:
        read(notebook, refs, "assets/plot.png", "0" * 64)
    assert error.value.status == "changed"


def test_read_refuses_files_the_prose_does_not_name(notebook) -> None:
    (notebook.parent / "assets/other.png").write_bytes(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    with pytest.raises(AssetError) as error:
        read(notebook, ("assets/plot.png",), "assets/other.png", digest)
    assert error.value.status == "unreferenced"


def test_read_refuses_missing_and_oversized_files(notebook, monkeypatch) -> None:
    digest = hashlib.sha256(PNG).hexdigest()
    with pytest.raises(AssetError) as error:
        read(notebook, ("assets/gone.png",), "assets/gone.png", digest)
    assert error.value.status == "missing"
    monkeypatch.setattr(assets, "MAX_ASSET_BYTES", 4)
    with pytest.raises(AssetError) as error:
        read(notebook, ("assets/plot.png",), "assets/plot.png", digest)
    assert error.value.status == "too_large"


def test_svg_is_served_as_svg(notebook) -> None:
    (notebook.parent / "assets/a.svg").write_bytes(SVG)
    digest = hashlib.sha256(SVG).hexdigest()
    assert read(notebook, ("assets/a.svg",), "assets/a.svg", digest)[1] == (
        "image/svg+xml"
    )


def test_control_characters_are_invalid_without_breaking_the_report(notebook) -> None:
    report = statuses(
        notebook, ("assets/x\x00.png", "assets/a\nb.png", "assets/plot.png")
    )
    assert [entry["status"] for entry in report] == ["invalid", "invalid", "ok"]
    assert "control characters" in report[0]["message"]


def test_messages_never_name_local_absolute_paths(notebook, tmp_path) -> None:
    folder = notebook.parent / "assets"
    (folder / "loop.png").symlink_to(folder / "loop.png")
    (folder / "dir.png").mkdir()
    locked = folder / "locked.png"
    locked.write_bytes(PNG)
    locked.chmod(0)
    try:
        report = statuses(
            notebook, ("assets/loop.png", "assets/dir.png", "assets/locked.png")
        )
    finally:
        locked.chmod(0o600)
    assert [entry["status"] for entry in report] == ["invalid"] * 3
    for entry in report:
        assert str(tmp_path) not in entry["message"]
        assert str(tmp_path.resolve()) not in entry["message"]


def test_a_directory_swapped_for_a_link_after_resolving_is_refused(
    notebook, tmp_path
) -> None:
    folder = notebook.parent / "assets"
    (folder / "sub").mkdir()
    (folder / "sub/plot.png").write_bytes(PNG)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "plot.png").write_bytes(b"secret")
    real_folder, target = assets._locate(notebook, "assets/sub/plot.png")
    (folder / "sub/plot.png").unlink()
    (folder / "sub").rmdir()
    (folder / "sub").symlink_to(outside)
    with pytest.raises(AssetError) as error:
        assets._read(real_folder, target, "assets/sub/plot.png")
    assert error.value.status == "invalid"
