"""Exercise the skill's worked example against real Lean and source mappings."""

import hashlib
import os
import shutil
import subprocess
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
