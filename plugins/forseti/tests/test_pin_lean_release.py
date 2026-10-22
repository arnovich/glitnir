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
