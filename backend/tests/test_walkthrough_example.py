"""Exercise the skill's worked example against real Lean and source mappings."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from glitnir.lean.project_checking import check_project
from glitnir.lean.project_inspection import TOOLCHAIN

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "skills/glitnir-notebooks/assets/counter-example"
)


def test_live_counter_example_source(tmp_path: Path, project_toolchain: Path) -> None:
    """Build and independently check the teaching formalization."""
    root = tmp_path / "counter"
    shutil.copytree(EXAMPLE / "counter", root)
    assert (root / "lean-toolchain").read_text().strip() == TOOLCHAIN
    subprocess.run(
        [str(project_toolchain / "bin/lake"), "--keep-toolchain", "build"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(project_toolchain)},
    )
    source = root / "Counter.lean"
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    claims = {claim["name"]: claim for claim in result["theorems"]}
    assert set(claims) == {
        "Counter.ticks_step",
        "Counter.double_step",
        "Counter.ticks_eq_double",
    }
    assert all(claim["status"] == "checked" for claim in claims.values())
    assert all(
        axiom["name"] in {"propext", "Quot.sound", "Classical.choice"}
        for claim in claims.values()
        for axiom in claim["axioms"]
    )


def test_worked_walkthrough_maps_real_source(walkthrough_example: Path) -> None:
    """The shipped claims and atlas bind to the actual bundled source headers."""
    from glitnir.preview.walkthrough import load_walkthrough
    from glitnir.service.static_export import SavedNotebook

    for folder in ("counter", "counter-walkthrough"):
        assert (
            walkthrough_example / folder / "lean-toolchain"
        ).read_text().strip() == TOOLCHAIN
    notebook = walkthrough_example / "counter-walkthrough/Walkthrough.lean"
    helper = EXAMPLE.parents[1] / "scripts/walkthrough.py"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(helper),
            "check",
            str(notebook.with_name("outline.json")),
            "--notebook",
            str(notebook),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    mapped = load_walkthrough(notebook)
    assert mapped is not None
    assert not mapped["problems"]
    assert len(mapped["atlas"]["nodes"]) == 4
    with SavedNotebook(notebook) as saved:
        snapshot = saved.snapshot()
    assert len(snapshot["walkthrough"]["nodes"]) == 4
    assert snapshot["walkthrough"]["formalization"]["supported"]


def test_live_worked_walkthrough_checks_public_module(
    walkthrough_example: Path, project_toolchain: Path
) -> None:
    """A separate ordinary notebook can import and restate a public module."""
    root = walkthrough_example / "counter-walkthrough"
    subprocess.run(
        [str(project_toolchain / "bin/lake"), "--keep-toolchain", "build"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(project_toolchain)},
    )
    source = root / "Walkthrough.lean"
    refused = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert refused["status"] == "unavailable", refused
    assert "outside the project" in refused["message"], refused
    (walkthrough_example / "counter/lake-manifest.json").unlink(missing_ok=True)
    original = walkthrough_example / "counter"
    local = root / "vendor-counter"
    subprocess.run(
        [
            sys.executable,
            "-B",
            str(EXAMPLE.parents[1] / "scripts/walkthrough.py"),
            "vendor",
            str(root / "outline.json"),
            "--out",
            str(local),
            "--source-repository",
            str(EXAMPLE.parents[3]),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    for name in ("Counter.lean", "lean-toolchain", "lakefile.toml"):
        assert (original / name).read_bytes() == (local / name).read_bytes()
    lakefile = root / "lakefile.toml"
    lakefile.write_text(lakefile.read_text().replace("../counter", "vendor-counter"))
    (root / "lake-manifest.json").unlink()
    subprocess.run(
        [str(project_toolchain / "bin/lake"), "--keep-toolchain", "build"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(project_toolchain)},
    )
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    claims = {claim["name"]: claim for claim in result["theorems"]}
    assert set(claims) == {
        "CounterWalkthrough.recurrence",
        "CounterWalkthrough.arithmetic_bridge",
        "CounterWalkthrough.result",
    }
    assert all(claim["status"] == "checked" for claim in claims.values())


def test_live_named_binder_wrappers(
    binder_wrapper_project: Path, project_toolchain: Path
) -> None:
    """Check strict, grouped, dependent and generic binders with kernel replay."""
    subprocess.run(
        [str(project_toolchain / "bin/lake"), "--keep-toolchain", "build"],
        cwd=binder_wrapper_project,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(project_toolchain)},
    )
    source = binder_wrapper_project / "Wrappers.lean"
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    claims = {claim["name"]: claim for claim in result["theorems"]}
    assert len(claims) == 8
    assert all(claim["status"] == "checked" for claim in claims.values()), result
