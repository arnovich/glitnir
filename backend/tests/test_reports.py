"""What `ProjectReport.lean` reported about a module, validated and compared."""

import pytest

from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.reports import compare_reports


def _report():
    return {
        "module": "M",
        "imports": [],
        "constants": [],
        "theorems": [{"name": "a", "canonical": "a", "type": "T", "axioms": []}],
    }


def test_compare_reports_refuses_repeated_or_unknown_names():
    """The source's declaration names must each name one reference theorem."""
    for names in (["a", "a"], ["b"]):
        with pytest.raises(SnapshotError, match="Source declaration identities"):
            compare_reports(_report(), _report(), names)
    assert compare_reports(_report(), _report(), ["a"]) == [_report()["theorems"][0]]


def test_project_modules_are_the_ones_the_project_builds(tmp_path):
    """Modules compiled in the project are notebooks; packages' are not."""
    from glitnir.lean.reports import project_modules

    root = tmp_path / "project"
    build = root / ".lake/build/lib/lean"
    package = root / ".lake/packages/mathlib/.lake/build/lib/lean"
    for directory in (build, package):
        directory.mkdir(parents=True)
    (build / "Sibling.olean").write_bytes(b"")
    (package / "Mathlib.olean").write_bytes(b"")
    outside = tmp_path / "toolchain/lib/lean/Init.olean"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"")
    setup = {
        "importArts": {
            "Sibling": [[".lake/build/lib/lean/Sibling.olean"]],
            "Absolute": [[str(build / "Sibling.olean")]],
            "Mathlib": [[".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean"]],
            "Init": [[str(outside)]],
        }
    }
    assert project_modules(setup, root.resolve()) == {"Sibling", "Absolute"}
    assert project_modules(setup, None) == set()


def test_claim_uses_keep_claims_of_this_and_other_notebooks():
    """Of the theorems named, only claims here and other notebooks' count."""
    from glitnir.lean.reports import claim_uses

    reported = [
        {"name": "A.one", "module": "A"},
        {"name": "A.one.eq_1", "module": "A"},
        {"name": "B.two", "module": "B"},
        {"name": "_private.B.0.hidden", "module": "B"},
        {"name": "Nat.add_zero", "module": "Init.Core"},
        {"name": "Mathlib.thing", "module": "Mathlib"},
    ]
    assert claim_uses(reported, "A", {"A.one", "A.three"}, {"B"}) == [
        {"module": "A", "name": "A.one", "in_notebook": True},
        {"module": "B", "name": "B.two", "in_notebook": False},
    ]
