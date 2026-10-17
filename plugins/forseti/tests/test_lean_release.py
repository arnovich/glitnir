"""A Python-selected release pins the complete Lean support installation."""

import json

import pytest

from glitnir_forseti import lean_release as release


def test_project_selection_is_explicit_and_option_takes_precedence(
    tmp_path, monkeypatch
):
    """No sibling or repository-local project is silently selected."""
    monkeypatch.delenv("FORSETI_LEAN_PROJECT", raising=False)
    assert release.select_project(None) is None
    monkeypatch.setenv("FORSETI_LEAN_PROJECT", str(tmp_path / "configured"))
    assert release.select_project(None) == (tmp_path / "configured").resolve()
    assert (
        release.select_project(tmp_path / "explicit")
        == (tmp_path / "explicit").resolve()
    )
    with pytest.raises(TypeError):
        release.select_project("not a Path")


def test_verified_sources_are_captured_before_later_mutation(lean_release_project):
    """Enrollment receives verified owned bytes rather than rereading mutable paths."""
    verified = release.verify_project(lean_release_project)
    original = verified.sources
    (lean_release_project / "Executables/Checker.lean").write_text("changed later")
    assert verified.sources == original
    assert (
        verified.source_bytes("forseti", "Executables/Checker.lean")
        == b"fixture forseti/Executables/Checker.lean\n"
    )


@pytest.mark.parametrize("package,relative,_", release.SOURCE_PINS)
@pytest.mark.parametrize("mutation", ["modify", "delete"])
def test_every_source_and_build_configuration_is_pinned(
    lean_release_project, package, relative, _, mutation
):
    """A changed Asgard core or executable Lake config invalidates the installation."""
    roots = release.package_roots(
        lean_release_project, json.loads(release.LOCKFILE_BYTES)
    )
    path = roots[package] / relative
    if mutation == "delete":
        path.unlink()
    else:
        path.write_text("untrusted")
    # A checkout's self-description cannot authorize the altered bytes.
    (lean_release_project / "lean-release.v1.json").write_text(
        '{"authority":"trusted"}'
    )
    with pytest.raises(release.LeanReleaseError):
        release.verify_project(lean_release_project)


@pytest.mark.parametrize("name", ["lean-toolchain", "lake-manifest.json"])
def test_toolchain_and_lock_are_pinned(lean_release_project, name):
    """Versions and complete dependency resolution are part of the release identity."""
    (lean_release_project / name).write_text("changed")
    with pytest.raises(release.LeanReleaseError):
        release.verify_project(lean_release_project)


def test_nested_package_root_and_path_rejection(tmp_path):
    """Resolve subDir source/build roots without escaping the dependency clone."""
    lock = {
        "name": "consumer",
        "packagesDir": ".lake/packages",
        "packages": [{"name": "nested", "type": "git", "subDir": "lean/core"}],
    }
    roots = release.package_roots(tmp_path, lock)
    assert roots["nested"] == tmp_path / ".lake/packages/nested/lean/core"
    for bad in ["../outside", "/outside"]:
        lock["packages"][0]["subDir"] = bad
        with pytest.raises(release.LeanReleaseError):
            release.package_roots(tmp_path, lock)
    lock["packages"][0]["subDir"] = None
    lock["packages"].append(dict(lock["packages"][0]))
    with pytest.raises(release.LeanReleaseError):
        release.package_roots(tmp_path, lock)


def test_special_and_oversized_source_files_fail_closed(
    lean_release_project, monkeypatch
):
    """Source checks cannot block on a FIFO or read an unlimited input."""
    import os

    path = lean_release_project / "Executables/Checker.lean"
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(release.LeanReleaseError):
        release.verify_project(lean_release_project)
    path.unlink()
    path.write_bytes(b"x" * 100)
    monkeypatch.setattr(release, "MAX_SOURCE_BYTES", 50)
    with pytest.raises(release.LeanReleaseError):
        release.verify_project(lean_release_project)


def test_packaged_pin_files_agree_with_each_other():
    """A half-done re-pin must not authorize against a different lock than it resolves.

    `verify_project` compares the project's manifest against `lockfile_sha256`
    from the release, while `package_roots` parses the separately packaged
    lockfile. Nothing previously asserted the two describe the same lock, so
    editing one file and forgetting the other would leave the trusted base
    internally inconsistent with a fully green suite.
    """
    import hashlib

    assert hashlib.sha256(release.LOCKFILE_BYTES).hexdigest() == release.LOCKFILE_SHA256

    packages = json.loads(release.LOCKFILE_BYTES)["packages"]
    revisions = {package["name"]: package.get("rev") for package in packages}
    assert dict(release.IMPORT_CLOSURE) == {
        name: revisions[name] for name, _ in release.IMPORT_CLOSURE
    }, "release dependencies disagree with the packaged lockfile"


def test_every_pinned_package_belongs_to_the_packaged_lock():
    """`package_roots` raises a release error rather than a bare KeyError."""
    names = {
        package["name"] for package in json.loads(release.LOCKFILE_BYTES)["packages"]
    }
    names.add(json.loads(release.LOCKFILE_BYTES)["name"])
    missing = sorted({package for package, _, _ in release.SOURCE_PINS} - names)
    assert not missing, f"pinned sources name packages absent from the lock: {missing}"


def test_lock_mismatch_names_the_drifting_dependency():
    """Checkout drift and a stale pin fail identically; the message must not."""
    lock = json.loads(release.LOCKFILE_BYTES)
    for package in lock["packages"]:
        if package["name"] == "asgard":
            package["rev"] = "0" * 40
    detail = release._lock_mismatch_detail(json.dumps(lock).encode())
    assert "asgard" in detail and "000000000000" in detail

    assert "not the pinned document" in release._lock_mismatch_detail(b"{ not json")
    missing = {"name": "forseti", "packages": []}
    assert "omits the pinned dependency" in release._lock_mismatch_detail(
        json.dumps(missing).encode()
    )


def test_release_records_which_checkout_satisfies_it():
    """The pin is by content; without this, 'which revision?' lives nowhere.

    Confirming the tags are still *fetchable* needs the network and is not done
    here. This catches a re-pin that forgets to record what it cut from.
    """
    from pathlib import Path as _Path

    origin = json.loads(
        (_Path(__file__).parent / "fixtures/lean-release-origin.v1.json").read_text()
    )
    assert (
        origin["lockfile_sha256"] == release.LOCKFILE_SHA256
    ), "the recorded origin does not describe the packaged pin; re-cut both"
    assert origin["asgard_revision"] == dict(release.IMPORT_CLOSURE)["asgard"]
    for key in ("forseti_lean_tag", "asgard_lean_tag"):
        assert origin[key].startswith("v"), f"{key} must name a tag, not a commit"
