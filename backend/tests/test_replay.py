"""What `ProjectReplay.lean` reported, validated against the predicted closure."""

import json

import pytest

from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.replay import replay_report

BASE = {
    "replay": "imports incremental",
    "imports_replayed": True,
    "extendable": True,
    "fresh": None,
    "imports": [],
    "modules": [],
    "theorems": [],
}


def test_replay_report_requires_incremental_and_base_to_agree():
    """Only an incremental replay names a base, and an incremental one always does."""
    for report in (
        {**BASE, "base_modules": None},
        {**BASE, "replay": "imports only", "base_modules": 0},
    ):
        with pytest.raises(SnapshotError, match="kernel replay report is malformed"):
            replay_report(json.dumps(report), [], [])
    assert (
        replay_report(json.dumps({**BASE, "base_modules": 0}), [], [])["replay"]
        == "imports incremental"
    )
