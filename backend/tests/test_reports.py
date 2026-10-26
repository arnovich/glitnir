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
