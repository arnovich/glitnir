"""Remote jobs bind candidate text, prepared context and checker implementation."""

import hashlib
import json

import pytest

from glitnir.compute.protocol import context_digest, make_request, stage_project


def test_candidate_changes_keep_context_but_import_changes_invalidate(compute_project):
    source = compute_project / "Example.lean"
    before = context_digest(source)
    source.write_text("theorem different : True := by trivial\n")
    assert context_digest(source) == before
    (compute_project / "Helper.lean").write_text(
        "theorem changed : True := by trivial\n"
    )
    assert context_digest(source) != before


def test_snapshot_matches_exact_candidate_and_copies_source(compute_project, tmp_path):
    source = compute_project / "Example.lean"
    text = source.read_text()
    job = make_request(source, "prepared", 60, 4096)
    assert job["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    job["source"] = "theorem revised : True := by trivial\n"
    job["source_sha256"] = hashlib.sha256(job["source"].encode()).hexdigest()
    staged = stage_project(compute_project, job, tmp_path / "stage")
    assert staged.read_text() == job["source"]
    assert source.read_text() == text
    assert (staged.parent / "Helper.lean").read_text() == (
        compute_project / "Helper.lean"
    ).read_text()


def test_context_mismatch_and_traversal_refused(compute_project, tmp_path):
    job = make_request(compute_project / "Example.lean", "prepared", 60, 4096)
    (compute_project / "Helper.lean").write_text("different")
    with pytest.raises(ValueError, match="context"):
        stage_project(compute_project, job, tmp_path / "bad")
    job["path"] = "../Elsewhere.lean"
    with pytest.raises(ValueError, match="path"):
        stage_project(compute_project, job, tmp_path / "escape")


def test_symlink_source_is_never_read_into_job(compute_project):
    source = compute_project / "Link.lean"
    source.symlink_to(compute_project / "Example.lean")
    with pytest.raises((OSError, ValueError)):
        make_request(source, "prepared", 60, 4096)


def test_staging_materializes_and_reuses_prepared_artifacts(compute_project, tmp_path):
    artifacts = compute_project / ".lake/build/lib/lean"
    artifacts.mkdir(parents=True)
    (artifacts / "Helper.olean").write_bytes(b"prepared artifact")
    source = compute_project / "Example.lean"
    request = make_request(source, "project", 60, 1024)
    destination = tmp_path / "staged"
    staged = stage_project(compute_project, request, destination)
    artifact = staged.parent / ".lake/build/lib/lean/Helper.olean"
    assert artifact.resolve().is_relative_to(destination)
    assert artifact.read_bytes() == b"prepared artifact"
    inode = artifact.stat().st_ino
    source.write_text("theorem changed : True := by trivial\n")
    changed = make_request(source, "project", 60, 1024)
    assert (
        stage_project(compute_project, changed, destination).read_text()
        == changed["source"]
    )
    assert artifact.stat().st_ino == inode


def test_dependency_overrides_are_refused(compute_project):
    lake = compute_project / ".lake"
    lake.mkdir()
    (lake / "package-overrides.json").write_text("{}")
    with pytest.raises(ValueError, match="overrides"):
        make_request(compute_project / "Example.lean", "project", 60, 1024)


def test_remote_fingerprint_binds_dependency_bytes(compute_project, monkeypatch):
    """Dirty-to-dirty edits change remote freshness even without a Git revision."""
    from glitnir.lean.fingerprint import environment_fingerprint

    monkeypatch.setenv("GLITNIR_EXECUTION_BACKEND", "taskvalet")
    source = compute_project / "Example.lean"
    before = environment_fingerprint(source, source.read_text())
    assert before["compute_context"] == context_digest(source)
    (compute_project / "Helper.lean").write_text("def helper := 37\n")
    after = environment_fingerprint(source, source.read_text())
    assert after["compute_context"] != before["compute_context"]
    assert after["stable_sha256"] != before["stable_sha256"]


def test_nonstandard_dependency_layout_fails_before_dispatch(compute_project):
    """Only the layout actually materialized by this worker is accepted."""
    dependency = compute_project / "vendor/Dep"
    dependency.mkdir(parents=True)
    (compute_project / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "Dep", "dir": "vendor/Dep"}]})
    )
    with pytest.raises(ValueError, match=".lake/packages"):
        make_request(compute_project / "Example.lean", "prepared", 60, 4096)
