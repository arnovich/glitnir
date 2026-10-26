"""The release pin is generated from a built release, and verifies against it.

What these pin is that the generator writes exactly what `verify_project`
checks: a pin it produces for a project is one that project satisfies, and the
committed pin is what it produces for the release it names.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from glitnir_forseti import lean_release as release

_TOOL = Path(__file__).resolve().parents[1] / "tools/pin_lean_release.py"
_SPEC = importlib.util.spec_from_file_location("pin_lean_release", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
pin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pin)


def _project(tmp_path: Path) -> Path:
    """Lay out a release checkout from the committed lockfile, with every file."""
    project = tmp_path / "release"
    project.mkdir()
    (project / "lean-toolchain").write_text(release.TOOLCHAIN + "\n")
    (project / "lake-manifest.json").write_bytes(release.LOCKFILE_BYTES)
    manifest = json.loads(release.LOCKFILE_BYTES)
    roots = release.package_roots(project, manifest)
    files = list(pin.CORE_SOURCES) + [("forseti", "lakefile.toml")]
    files += [(package["name"], "lakefile.toml") for package in manifest["packages"]]
    for package, relative in files:
        path = roots[package] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture {package}/{relative}\n")
    return project


def test_the_generated_pin_records_what_it_read(tmp_path) -> None:
    """Every hash and revision in the pin comes from the project's own bytes."""
    project = _project(tmp_path)
    document, lockfile = pin.release_pin(project)
    generated = json.loads(document)
    manifest = json.loads(lockfile)
    assert lockfile == (project / "lake-manifest.json").read_bytes()
    assert generated["lockfile_sha256"] == hashlib.sha256(lockfile).hexdigest()
    assert generated["dependencies"] == [
        {"name": package["name"], "revision": package["rev"]}
        for package in manifest["packages"]
    ]
    roots = release.package_roots(project, manifest)
    for entry in generated["core_sources"] + generated["build_configs"]:
        content = (roots[entry["package"]] / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    checker = next(
        entry
        for entry in generated["core_sources"]
        if entry["path"] == "Executables/Checker.lean"
    )
    assert generated["checker_sha256"] == checker["sha256"]


def test_a_changed_source_changes_the_pin(tmp_path) -> None:
    """The pin is sensitive to every pinned file, so drift cannot pass."""
    project = _project(tmp_path)
    before, _ = pin.release_pin(project)
    (project / "Gimle/Forseti.lean").write_text("changed")
    after, _ = pin.release_pin(project)
    assert before != after


def test_a_source_under_a_symlinked_directory_is_refused(tmp_path) -> None:
    """A pinned path may not leave its package through a linked directory."""
    project = _project(tmp_path)
    outside = tmp_path / "outside"
    (project / "Gimle").rename(outside)
    (project / "Gimle").symlink_to(outside)
    with pytest.raises(SystemExit, match="escapes its package"):
        pin.release_pin(project)


def test_a_symlinked_source_is_refused(tmp_path) -> None:
    """The generator reads only regular files, as `verify_project` does."""
    project = _project(tmp_path)
    source = project / "Gimle/Forseti.lean"
    target = tmp_path / "elsewhere.lean"
    target.write_text("elsewhere")
    source.unlink()
    source.symlink_to(target)
    with pytest.raises(SystemExit, match="not a regular file"):
        pin.release_pin(project)


@pytest.mark.skipif(
    not os.environ.get("FORSETI_LEAN_PROJECT"),
    reason="needs a built standalone Lean release checkout",
)
def test_the_committed_pin_is_generated_from_the_release() -> None:
    """The packaged pin is exactly what the generator writes for the release."""
    project = Path(os.environ["FORSETI_LEAN_PROJECT"])
    document, lockfile = pin.release_pin(project)
    assert document == release.RELEASE_BYTES
    assert lockfile == release.LOCKFILE_BYTES


def test_the_support_version_counts_up() -> None:
    assert (
        pin.next_support_version("gimle.forseti.lean-support/v21")
        == "gimle.forseti.lean-support/v22"
    )
    with pytest.raises(SystemExit, match="not a numbered support version"):
        pin.next_support_version("gimle.forseti.lean-support")


def test_the_support_version_moves_only_with_the_content(tmp_path) -> None:
    """A re-pin of the same release keeps the version; a changed one bumps it."""
    project = _project(tmp_path)
    document, _ = pin.release_pin(project)
    assert pin.bumped_support_version(document, document) is None
    committed = json.loads(document)
    committed["lockfile_sha256"] = "0" * 64
    moved = json.dumps(committed).encode()
    assert pin.bumped_support_version(document, moved) == pin.next_support_version(
        pin.SUPPORT_VERSION
    )


def test_a_support_version_bumped_by_hand_is_kept(tmp_path) -> None:
    """A constant already bumped by hand is not bumped a second time."""
    project = _project(tmp_path)
    document, _ = pin.release_pin(project)
    committed = json.loads(document)
    committed["support_version"] = "gimle.forseti.lean-support/v1"
    committed["lockfile_sha256"] = "0" * 64
    assert pin.bumped_support_version(document, json.dumps(committed).encode()) is None


def test_the_tool_rewrites_only_its_support_constant() -> None:
    source = 'X = "a"\nSUPPORT_VERSION = "old/v1"\nY = "old/v1"\n'
    assert pin.with_support_version(source, "old/v1", "old/v2") == (
        'X = "a"\nSUPPORT_VERSION = "old/v2"\nY = "old/v1"\n'
    )
    with pytest.raises(SystemExit, match="exactly one"):
        pin.with_support_version(source, "other/v1", "other/v2")


def _run_main(monkeypatch, tmp_path: Path, project: Path, *flags: str) -> int:
    """Run the tool's entry point against copies of every file it writes."""
    files = {
        "RELEASE_PIN": tmp_path / "out/lean-release.v1.json",
        "LOCKFILE_PIN": tmp_path / "out/lean-lockfile.v1.json",
        "ORIGIN_RECORD": tmp_path / "out/lean-release-origin.v1.json",
        "TOOL": tmp_path / "out/pin_lean_release.py",
    }
    if not files["TOOL"].exists():
        files["TOOL"].parent.mkdir(parents=True, exist_ok=True)
        files["TOOL"].write_bytes(_TOOL.read_bytes())
        committed = json.loads(pin.release_pin(project)[0])
        committed["lockfile_sha256"] = "0" * 64  # the release moved since
        files["RELEASE_PIN"].write_text(json.dumps(committed))
        files["LOCKFILE_PIN"].write_text("{}")
        files["ORIGIN_RECORD"].write_text("{}")
    monkeypatch.setattr(pin, "REPOSITORY", tmp_path)
    for name, path in files.items():
        monkeypatch.setattr(pin, name, path)
    monkeypatch.setattr(
        "sys.argv",
        ["pin_lean_release.py", str(project), "--forseti-tag", "v1.0.0"]
        + ["--asgard-tag", "v2.0.0", "--verified-on", "2026-01-01", *flags],
    )
    return pin.main()


def test_bump_support_writes_the_bumped_version_everywhere(
    monkeypatch, tmp_path
) -> None:
    """The pin and the tool's constant move together, and a re-run is current."""
    project = _project(tmp_path)
    bumped = pin.next_support_version(pin.SUPPORT_VERSION)
    assert _run_main(monkeypatch, tmp_path, project, "--bump-support", "--check") == 1
    assert _run_main(monkeypatch, tmp_path, project, "--bump-support") == 0
    written = json.loads((tmp_path / "out/lean-release.v1.json").read_bytes())
    assert written["support_version"] == bumped
    tool = (tmp_path / "out/pin_lean_release.py").read_text()
    assert f'SUPPORT_VERSION = "{bumped}"' in tool
    origin = json.loads((tmp_path / "out/lean-release-origin.v1.json").read_bytes())
    assert (origin["forseti_lean_tag"], origin["asgard_lean_tag"]) == (
        "v1.0.0",
        "v2.0.0",
    )


def test_without_bump_support_the_version_stays(monkeypatch, tmp_path) -> None:
    project = _project(tmp_path)
    assert _run_main(monkeypatch, tmp_path, project) == 0
    written = json.loads((tmp_path / "out/lean-release.v1.json").read_bytes())
    assert written["support_version"] == pin.SUPPORT_VERSION
    assert (tmp_path / "out/pin_lean_release.py").read_bytes() == _TOOL.read_bytes()
