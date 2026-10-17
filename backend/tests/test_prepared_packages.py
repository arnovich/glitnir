"""Prepared package reports bind the exact inputs used by Lake resolution."""

import hashlib

import pytest

from glitnir.lean.prepared_packages import validate_resolution


def test_resolution_binds_inputs_and_absence(tmp_path):
    """An absent override and captured manifest bytes are both part of identity."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
    package = tmp_path / "vendor/dep"
    package.mkdir(parents=True)
    value = {
        "kind": "prepared_local",
        "packages": [{"name": "dep", "path": str(package), "entry": {"type": "git"}}],
        "inputs": [
            {"path": str(manifest), "contents": "{}\n"},
            {"path": str(tmp_path / "overrides.json"), "contents": None},
        ],
    }
    result = validate_resolution(value, tmp_path)
    assert result["inputs"][0]["sha256"] == hashlib.sha256(b"{}\n").hexdigest()
    assert result["inputs"][1]["sha256"] is None
    assert result["packages"][0]["path"] == "vendor/dep"
    (tmp_path / "overrides.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        validate_resolution(value, tmp_path)


def test_resolution_rejects_changed_or_escaping_inputs(tmp_path):
    """A report cannot bind stale content or authorize reads outside its project."""
    source = tmp_path / "manifest.json"
    source.write_text("new")
    value = {
        "kind": "prepared_local",
        "packages": [],
        "inputs": [{"path": str(source), "contents": "old"}],
    }
    with pytest.raises(ValueError, match="changed"):
        validate_resolution(value, tmp_path)
    value["inputs"][0]["path"] = str(tmp_path.parent / "outside")
    with pytest.raises(ValueError, match="outside"):
        validate_resolution(value, tmp_path)


def test_live_prepared_git_setup_matches_lake(prepared_git_project):
    """Prepared local inspection preserves custom directories and transitive imports."""
    import json
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain, inspect_project

    root = prepared_git_project
    source = root / "Example.lean"
    normal = subprocess.run(
        [
            str(_toolchain() / "bin/lake"),
            "--keep-toolchain",
            "--no-build",
            "--no-cache",
            "setup-file",
            str(source),
        ],
        cwd=root,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
        check=True,
        capture_output=True,
        timeout=180,
    )
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = inspect_project(source)
    assert result["status"] == "resolved", result
    assert result["setup"] == json.loads(normal.stdout)
    packages = {item["name"]: item for item in result["resolution"]["packages"]}
    assert packages["dep"]["path"] == "vendor/dep/nested"
    assert packages["leaf"]["path"] == "vendor/leaf"
    assert packages["dep"]["entry"]["type"] == "git"
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_live_override_and_custom_configuration(prepared_git_project):
    """Overrides, custom configuration and optional absences survive."""
    import json
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain, inspect_project

    root = prepared_git_project
    manifest_path = root / "lake-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(p for p in manifest["packages"] if p["name"] == "dep")
    entry["configFile"] = "custom.toml"
    (root / "vendor/dep/nested/lakefile.toml").rename(
        root / "vendor/dep/nested/custom.toml"
    )
    manifest_path.write_text(json.dumps(manifest))
    alternate = root / "alternate"
    alternate.mkdir()
    (alternate / "custom.toml").write_text(
        'name = "leaf"\n[[lean_lib]]\nname = "Leaf"\n'
    )
    (alternate / "Leaf.lean").write_text("def leafValue : Nat := 7\n")
    override = {
        "name": "leaf",
        "type": "path",
        "dir": "alternate",
        "inherited": True,
        "scope": "",
        "configFile": "custom.toml",
        "manifestFile": None,
    }
    (root / ".lake/package-overrides.json").write_text(
        json.dumps({"schemaVersion": "1.1.0", "packages": [override]})
    )
    subprocess.run(
        [str(_toolchain() / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
        check=True,
        capture_output=True,
        timeout=180,
    )
    result = inspect_project(root / "Example.lean")
    assert result["status"] == "resolved", result
    packages = {p["name"]: p for p in result["resolution"]["packages"]}
    assert packages["leaf"]["path"] == "alternate"
    assert packages["leaf"]["entry"]["manifestFile"] == "lake-manifest.json"
    assert any(
        p["path"] == "alternate/lake-manifest.json" and p["sha256"] is None
        for p in result["resolution"]["inputs"]
    )
    assert packages["dep"]["entry"]["configFile"] == "custom.toml"
    assert any("alternate" in p for p in result["setup"]["importArts"]["Leaf"])


def test_live_revision_is_descriptive_and_new_override_is_detected(
    prepared_git_project, monkeypatch
):
    """Use local contents without asserting that the declared revision was checked."""
    import json

    from glitnir.lean import project_inspection as inspection

    root = prepared_git_project
    manifest_path = root / "lake-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    next(p for p in manifest["packages"] if p["name"] == "dep")["rev"] = "f" * 40
    manifest_path.write_text(json.dumps(manifest))
    result = inspection.inspect_project(root / "Example.lean")
    assert result["status"] == "resolved", result
    assert result["resolution"]["kind"] == "prepared_local"
    dep = next(p for p in result["resolution"]["packages"] if p["name"] == "dep")
    assert dep["entry"]["rev"] == "f" * 40
    original = inspection._run

    def change(arguments, *args, **kwargs):
        answer = original(arguments, *args, **kwargs)
        if "setup-file" in arguments:
            (root / ".lake/package-overrides.json").write_text("{}")
        return answer

    monkeypatch.setattr(inspection, "_run", change)
    assert "changed" in inspection.inspect_project(root / "Example.lean")["message"]


def test_absent_input_with_escaping_symlink_parent(tmp_path):
    """An optional absent file still cannot bind a path through an external parent."""
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    value = {
        "kind": "prepared_local",
        "packages": [],
        "inputs": [
            {"path": str(tmp_path / "link/missing-overrides"), "contents": None}
        ],
    }
    with pytest.raises(ValueError, match="outside"):
        validate_resolution(value, tmp_path)


def test_snapshot_binds_custom_resolution_inputs(tmp_path, project_snapshot_inputs):
    """Copied custom manifests must match the bytes inspected, including absences."""
    import threading

    from glitnir.lean.project_snapshot import SnapshotError, capture

    root, toolchain, source, _, setup = project_snapshot_inputs
    custom = root / "custom.json"
    custom.write_text("original")
    configuration = {
        "custom.json": hashlib.sha256(b"original").hexdigest(),
        "absent.json": None,
    }
    first = tmp_path / "first"
    first.mkdir()
    result = capture(
        first,
        source,
        setup,
        toolchain,
        root,
        threading.Event(),
        configuration=configuration,
    )
    # Bound by digest only: configuration is never stored or exposed.
    assert result.digests["resolution-inputs/custom.json"] == (
        hashlib.sha256(b"original").hexdigest()
    )
    assert result.digests["resolution-inputs/absent.json"] == "absent"
    assert not (result.root / "resolution-inputs").exists()
    custom.write_text("changed")
    second = tmp_path / "second"
    second.mkdir()
    with pytest.raises(SnapshotError, match="changed"):
        capture(
            second,
            source,
            setup,
            toolchain,
            root,
            threading.Event(),
            configuration=configuration,
        )
