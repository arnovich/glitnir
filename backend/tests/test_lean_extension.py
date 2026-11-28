"""Live integration of the optional Lean explanation package."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from glitnir.lean.project_checking import check_project
from glitnir.lean.project_inspection import TOOLCHAIN
from glitnir.lean.proof_trace import trace_file

PACKAGE = Path(__file__).resolve().parents[2] / "lean-extension"


def test_live_explanation_package_builds_checks_and_traces(tmp_path, project_toolchain):
    """One annotated fact remains in scope and has an authored trace group."""
    project = tmp_path / "annotated"
    project.mkdir()
    package = project / "glitnir_extension"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns(".lake"))
    (project / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (project / "lakefile.toml").write_text(
        'name = "annotated"\n'
        '[[require]]\nname = "glitnir_extension"\npath = "glitnir_extension"\n'
        '[[lean_lib]]\nname = "Annotated"\n'
    )
    source = project / "Annotated.lean"
    source.write_text(
        "import Glitnir\n"
        "theorem reasoningExample (P Q R : Prop) (hP : P) "
        "(hPQ : P → Q) (hQR : Q → R) : R := by\n"
        '  explain "derive-q" "Applying the first implication gives Q." =>\n'
        "    have hQ : Q := hPQ hP\n"
        '  explain "derive-r" "The second implication gives R." =>\n'
        "    exact hQR hQ\n"
    )
    subprocess.run(
        [str(project_toolchain / "bin/lake"), "--keep-toolchain", "build", "Annotated"],
        cwd=project,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(project_toolchain)},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    trace = trace_file(source, expected_sha256=digest)
    assert trace["status"] == "ready", trace.get("message")
    assert [group["authored_id"] for group in trace["explanations"]] == [
        "derive-q",
        "derive-r",
    ]
    first_group = trace["explanations"][0]
    first_node = trace["nodes"][first_group["parent"]]
    assert first_node["kind"] == "Glitnir.explain"
    assert (first_node["start"], first_node["end"]) == (
        first_group["start"],
        first_group["end"],
    )
    assert first_node["introduced_local"] == {"name": "hQ", "type": "Q"}
    checked = check_project(source, expected_sha256=digest)
    assert checked["status"] == "checked_snapshot", checked
    assert [(item["name"], item["status"]) for item in checked["theorems"]] == [
        ("reasoningExample", "checked")
    ]

    # The representative existing Counter proof now uses the same annotation
    # in an induction branch, with a named local fact shared by two groups.
    counter = project / "Counter.lean"
    counter.write_bytes((PACKAGE / "Examples" / "Counter.lean").read_bytes())
    counter_digest = hashlib.sha256(counter.read_bytes()).hexdigest()
    counter_trace = trace_file(counter, expected_sha256=counter_digest)
    assert counter_trace["status"] == "ready", counter_trace.get("message")
    assert [group["authored_id"] for group in counter_trace["explanations"]] == [
        "recurrence",
        "induction-step",
    ]
    recurrence = counter_trace["explanations"][0]
    recurrence_node = counter_trace["nodes"][recurrence["parent"]]
    assert recurrence_node["introduced_local"] == {
        "name": "hStep",
        "type": "ticks (n + 1) = ticks n + 2",
    }
    counter_check = check_project(counter, expected_sha256=counter_digest)
    assert counter_check["status"] == "checked_snapshot", counter_check
    assert [(item["name"], item["status"]) for item in counter_check["theorems"]] == [
        ("Counter.ticks_step", "checked"),
        ("Counter.double_step", "checked"),
        ("Counter.ticks_eq_double", "checked"),
    ]
