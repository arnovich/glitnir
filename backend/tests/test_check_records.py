"""Saved project checks stay current exactly while their inputs are unchanged."""

import copy
import json
from pathlib import Path

import pytest

from glitnir.notebook import check_records
from glitnir.notebook.check_records import (
    DECISION_FAILURE_KINDS,
    EVIDENCE_FAILURE_KINDS,
    MAX_MESSAGE,
    MAX_VIEWS_BYTES,
    RESULTS_TOO_LARGE,
    _serialised,
    claim_views,
    decision_failure_status,
    decision_prefix_sha256,
    decision_status,
    evidence_prefix_sha256,
    evidence_status,
    failure_message,
    load_records,
    records_from_check,
    records_path,
    save_records,
    saved_decision_failures,
    saved_decisions,
    saved_evidence,
    saved_evidence_failures,
    saved_views,
    view_status,
    with_decision,
    with_decision_failure,
    with_evidence,
    with_evidence_failure,
    with_view,
    without_decision,
)
from glitnir.notebook.directives import decision_key, evidence_key
from glitnir.providers.outcomes import checked_decision

# Environment fingerprints are SHA-256 digests; any other string matches nothing.
FP = "f" * 64
GP = "9" * 64
FP_SAVED = "c" * 64
OLD = "1" * 64
NEW = "2" * 64

TEXT = (
    "/-! # Notes -/\n"
    "def base : Nat := 1\n"
    "namespace A\ntheorem result : base = 1 := rfl\nend A\n"
    "namespace B\ntheorem result : True := True.intro\nend B\n"
)
RESULT = {
    "status": "checked_snapshot",
    "source_sha256": "s",
    "environment_sha256": "e",
    "withheld": None,
    "theorems": [
        {
            "name": "A.result",
            "status": "checked",
            "axioms": [],
            "start_line": 4,
            "end_line": 4,
            "proposition_sha256": "p1",
        },
        {
            "name": "B.result",
            "status": "checked",
            "axioms": [],
            "start_line": 7,
            "end_line": 7,
            "proposition_sha256": "p2",
        },
    ],
}


def views(text, fingerprint=FP):
    records = records_from_check(TEXT, RESULT, FP)
    found = claim_views(text, records, fingerprint)
    return {v["name"]: v for v in found}


def test_stage_times_are_not_saved():
    """A check's `stage_ms` is for display and logs; the records hold none."""
    records = records_from_check(TEXT, {**RESULT, "stage_ms": {"total": 5}}, FP)
    assert records == records_from_check(TEXT, RESULT, FP)
    assert "stage_ms" not in json.dumps(records)


def test_same_short_names_keep_their_own_lines():
    found = views(TEXT)
    assert (found["A.result"]["start_line"], found["B.result"]["start_line"]) == (4, 7)
    assert all(v["current"] for v in found.values())


def test_prose_edit_above_marks_results_stale():
    # Lean code can read module-doc prose, so prose is not inert.
    edited = TEXT.replace("/-! # Notes -/\n", "/-! # Other notes, longer. -/\n")
    found = views(edited)
    assert not found["A.result"]["current"] and not found["B.result"]["current"]
    assert found["A.result"]["start_line"] == 4


def test_blank_line_or_grown_prose_above_marks_results_stale():
    blank = TEXT.replace("/-! # Notes -/\n", "/-! # Notes -/\n\n")
    grown = TEXT.replace("/-! # Notes -/\n", "/-! # Other notes\nMore. -/\n")
    for edited in (blank, grown):
        found = views(edited)
        assert found["A.result"]["start_line"] == 5
        assert not found["A.result"]["current"]
        assert not found["B.result"]["current"]


def test_lean_edit_above_marks_later_claims_stale():
    found = views(TEXT.replace("def base : Nat := 1", "def base : Nat := 2"))
    assert not found["A.result"]["current"] and not found["B.result"]["current"]


def test_edit_below_keeps_earlier_claim_current():
    found = views(TEXT.replace("theorem result : True", "theorem result : 1 = 1"))
    assert found["A.result"]["current"]
    assert "B.result" not in found


def test_environment_change_marks_everything_stale():
    found = views(TEXT, fingerprint="other")
    assert not any(v["current"] for v in found.values())


def test_crlf_text_matches_lf_records():
    found = views(TEXT.replace("\n", "\r\n"))
    assert found["A.result"]["current"]


def test_withheld_claims_are_reported_from_their_line():
    result = {
        **RESULT,
        "theorems": RESULT["theorems"][:1],
        "withheld": {"from_line": 6, "message": "type mismatch"},
    }
    records = records_from_check(TEXT, result, FP)
    assert records["withheld"] == {"from_line": 6, "message": "type mismatch"}
    assert [v["name"] for v in claim_views(TEXT, records, FP)] == ["A.result"]


def test_round_trip_and_unknown_formats(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, RESULT, FP)
    save_records(notebook, records)
    assert records_path(notebook).name == "N.glitnir.json"
    assert load_records(notebook) == records
    records_path(notebook).write_text("{not json")
    assert load_records(notebook) is None
    records_path(notebook).write_text('{"format": "other/v9"}')
    assert load_records(notebook) is None
    assert not (tmp_path / "N.forseti.json").exists()


def test_claim_entry_missing_a_key_is_unknown(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, RESULT, FP)
    del records["claims"][0]["prefix_sha256"]
    records_path(notebook).write_text(json.dumps(records))
    assert load_records(notebook) is None


def test_deeply_nested_json_is_unknown(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records_path(notebook).write_text("[" * 100_000 + "]" * 100_000)
    assert load_records(notebook) is None


def _result(*theorems):
    entries = [
        {
            "name": name,
            "status": "checked",
            "axioms": [],
            "start_line": line,
            "end_line": line,
            "proposition_sha256": f"p{line}",
        }
        for name, line in theorems
    ]
    return {**RESULT, "theorems": entries, "withheld": None}


def _current(text, source, result):
    records = records_from_check(source, result, FP)
    return {v["name"]: v["current"] for v in claim_views(text, records, FP)}


def test_identical_declarations_map_to_their_own_lines():
    source = "theorem t : True := trivial\n\ntheorem t : True := trivial\n"
    result = _result(("A.t", 1), ("B.t", 3))
    records = records_from_check(source, result, FP)
    found = claim_views(source, records, FP)
    assert [(v["name"], v["start_line"]) for v in found] == [("A.t", 1), ("B.t", 3)]
    assert all(v["current"] for v in found)
    edited = source.replace(
        "theorem t : True := trivial", "theorem t : True := True.intro", 1
    )
    assert not any(_current(edited, source, result).values())


def test_blank_lines_between_tactics_mark_results_stale():
    source = "theorem a : 1 = 1 := by\n  skip\n  rfl\ntheorem u : 1 = 1 := a\n"
    edited = source.replace("  skip\n", "  skip\n\n   \n")
    assert _current(edited, source, _result(("u", 4))) == {"u": False}
    assert _current(source, edited, _result(("u", 6))) == {"u": False}


# Code can read its own line positions, for example
# ``(← Lean.getFileMap).toPosition p).line`` in a ``by_elab`` tactic, so any
# change in the line structure above a claim must make it stale.
POSITION_SOURCE = (
    "/-! # Notes\nTwo lines. -/\n"
    "def x : Nat := 1\n"
    "\n"
    "theorem u : x = 1 := rfl\n"
)


def test_blank_line_inserted_above_a_claim_is_seen():
    result = _result(("u", 5))
    edited = POSITION_SOURCE.replace("def x", "\ndef x")
    assert _current(edited, POSITION_SOURCE, result) == {"u": False}
    removed = POSITION_SOURCE.replace("1\n\n", "1\n")
    assert _current(removed, POSITION_SOURCE, result) == {"u": False}


def test_prose_block_growing_or_shrinking_a_line_is_seen():
    result = _result(("u", 5))
    grown = POSITION_SOURCE.replace("Two lines. -/", "Two lines.\nThree. -/")
    assert _current(grown, POSITION_SOURCE, result) == {"u": False}
    shrunk = POSITION_SOURCE.replace("# Notes\nTwo lines. -/", "# Notes -/")
    assert _current(shrunk, POSITION_SOURCE, result) == {"u": False}
    same = POSITION_SOURCE.replace("# Notes\nTwo lines.", "# Other\nStill two.")
    assert _current(same, POSITION_SOURCE, result) == {"u": False}


def test_verso_prose_edits_are_seen():
    # With doc.verso, Lean elaborates prose, so prose edits can change output.
    source = (
        "set_option doc.verso true\n"
        "/-! See {name}`Nat.succ`. -/\n"
        "theorem u : True := trivial\n"
    )
    edited = source.replace("Nat.succ", "Nat.nope")
    assert _current(edited, source, _result(("u", 3))) == {"u": False}


def test_saved_records_are_world_readable(tmp_path: Path):
    """The sidecar is shared like the notebook, not private like a temp file."""
    notebook = tmp_path / "Note.lean"
    save_records(notebook, records_from_check(TEXT, RESULT, FP_SAVED))
    assert records_path(notebook).stat().st_mode & 0o777 == 0o644


NOTE = "-- @evidence simulate M (h := 1/100)"
NOTEBOOK = f"/-! # Notes -/\ndef M := 1\n{NOTE}\ndef after := 2\n"
SNAPSHOT = {
    "format": "glitnir.evidence/v1",
    "series": {"times": [0.0, 1.0], "values": [[1.0], [None]], "names": ["x"]},
    "provenance": [["step", "1/100"]],
    "finite": True,
}


def _entry(text=NOTEBOOK, line=3, fingerprint=FP, snapshot=SNAPSHOT):
    return {
        "prefix_sha256": evidence_prefix_sha256(text, line),
        "fingerprint": fingerprint,
        "provider": "demo",
        "created": "2026-10-14T12:00:00Z",
        "snapshot": snapshot,
    }


KEY = evidence_key(NOTE, 0)


def _status(text, line=3, fingerprint=FP):
    return evidence_status(_entry(), text, line, fingerprint)


def test_evidence_is_current_while_text_through_the_directive_is_unchanged():
    assert _status(NOTEBOOK) == "current"
    assert _status(NOTEBOOK.replace("def after := 2", "def after := 3")) == "current"
    assert _status(NOTEBOOK + "more\n") == "current"


def test_edits_above_the_directive_mark_evidence_stale():
    assert _status(NOTEBOOK.replace("# Notes", "# Other")) == "stale"
    assert _status(NOTEBOOK.replace("def M := 1", "def M := 2")) == "stale"
    assert _status("\n" + NOTEBOOK, line=4) == "stale"
    assert _status(NOTEBOOK.replace("def M := 1\n", "def M := 1\n\n"), 4) == "stale"


def test_a_changed_environment_marks_evidence_stale():
    assert _status(NOTEBOOK, fingerprint=GP) == "stale"


def test_evidence_status_reads_crlf_text_like_lf():
    assert _status(NOTEBOOK.replace("\n", "\r\n")) == "current"


def test_with_evidence_starts_a_file_that_holds_only_evidence(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_evidence(None, KEY, _entry(), [KEY])
    assert records["claims"] == []
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert loaded == records
    assert saved_evidence(loaded) == {KEY: _entry()}
    assert claim_views(NOTEBOOK, loaded, FP) == []


def test_with_evidence_prunes_keys_of_directives_that_are_gone():
    other, gone = evidence_key("-- @evidence simulate N", 0), "gone"
    records = with_evidence(None, gone, _entry(), [gone])
    records = with_evidence(records, other, _entry(), [gone, other])
    records = with_evidence(records, KEY, _entry(), [other])
    assert set(saved_evidence(records)) == {other, KEY}


def test_with_evidence_does_not_change_its_input_or_the_claims():
    checked = records_from_check(TEXT, RESULT, FP)
    before = json.dumps(checked, sort_keys=True)
    updated = with_evidence(checked, KEY, _entry(), [KEY])
    assert json.dumps(checked, sort_keys=True) == before
    assert updated["claims"] == checked["claims"]
    assert updated["environment"] == checked["environment"]
    assert saved_evidence(checked) == {}


def test_with_evidence_refuses_a_malformed_entry():
    with pytest.raises(ValueError):
        with_evidence(None, KEY, {**_entry(), "snapshot": {"format": "x"}}, [KEY])
    with pytest.raises(ValueError):
        with_evidence(None, KEY, {**_entry(), "provider": 3}, [KEY])


def test_a_check_keeps_the_saved_evidence():
    previous = with_evidence(None, KEY, _entry(), [KEY])
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert saved_evidence(records) == {KEY: _entry()}
    assert "evidence" not in records_from_check(TEXT, RESULT, FP)


def test_a_check_never_drops_evidence_to_fit_the_file(monkeypatch):
    """Records too large with the saved evidence are refused, never trimmed."""
    previous = with_evidence(None, KEY, _entry(), [KEY])
    claims_only = len(_serialised(records_from_check(TEXT, RESULT, FP)).encode())
    monkeypatch.setattr(check_records, "_MAX_RECORDS", claims_only + 10)
    with pytest.raises(ValueError, match="nothing saved before was dropped"):
        records_from_check(TEXT, RESULT, FP, previous)
    assert RESULTS_TOO_LARGE.startswith("the results file would exceed 16 MiB")


def test_evidence_survives_a_round_trip_beside_claims(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_evidence(records_from_check(TEXT, RESULT, FP), KEY, _entry(), [KEY])
    save_records(notebook, records)
    assert load_records(notebook) == records


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        {KEY: []},
        {KEY: {**_entry(), "extra": 1}},
        {KEY: {k: v for k, v in _entry().items() if k != "created"}},
        {KEY: {**_entry(), "created": "yesterday"}},
        {KEY: {**_entry(), "created": "2026-10-14T12:00:00"}},
        {KEY: {**_entry(), "created": "2026-10-14T12:00:00+02:00"}},
        {KEY: {**_entry(), "created": "2026-13-14T12:00:00Z"}},
        {KEY: {**_entry(), "created": "2026-10-14"}},
        {KEY: {**_entry(), "fingerprint": 1}},
        {KEY: {**_entry(), "snapshot": {**SNAPSHOT, "finite": "yes"}}},
        {KEY: {**_entry(), "snapshot": None}},
    ],
)
def test_a_malformed_evidence_section_makes_the_file_unreadable(tmp_path, evidence):
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, RESULT, FP)
    records_path(notebook).write_text(json.dumps({**records, "evidence": evidence}))
    assert load_records(notebook) is None


def _maximal_snapshot(seed: int) -> dict:
    """A snapshot with 4000 samples of 16 series: about 2.4 MB when saved."""
    return {
        **SNAPSHOT,
        "series": {
            "times": [i + 0.123456789012345 for i in range(4000)],
            "values": [
                [(seed + i) / 7.0 + j * 0.1234567890123 for j in range(16)]
                for i in range(4000)
            ],
            "names": [f"s{j}" for j in range(16)],
        },
    }


def test_saved_evidence_that_would_exceed_the_file_cap_is_refused(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    keys = [f"k{n}" for n in range(12)]
    records = with_evidence(records_from_check(TEXT, RESULT, FP), "k0", _entry(), [])
    saved = 0
    for key in keys:
        entry = _entry(snapshot=_maximal_snapshot(saved))
        try:
            records = with_evidence(records, key, entry, keys, notebook)
        except ValueError as error:
            assert "N.glitnir.json" in str(error) and "16 MiB" in str(error)
            break
        save_records(notebook, records)
        saved += 1
    else:
        pytest.fail("the cap was never reached")
    assert 0 < saved < len(keys)
    before = records_path(notebook).read_bytes()
    assert load_records(notebook) == records  # still readable
    assert len(before) <= 16 * 1024 * 1024
    assert [c["name"] for c in records["claims"]] == ["A.result", "B.result"]
    assert records_path(notebook).read_bytes() == before


def test_a_size_check_measures_what_save_records_writes(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_evidence(None, KEY, _entry(), [KEY], notebook)
    save_records(notebook, records)
    assert records_path(notebook).read_text() == _serialised(records)


@pytest.mark.parametrize("content", ["{not json", '{"format": "other/v9"}'])
def test_an_unreadable_records_file_is_never_started_afresh(tmp_path, content):
    notebook = tmp_path / "N.lean"
    records_path(notebook).write_text(content)
    assert load_records(notebook) is None
    with pytest.raises(ValueError, match="cannot be read"):
        with_evidence(None, KEY, _entry(), [KEY], notebook)
    assert records_path(notebook).read_text() == content
    # With no file on disk, or no path given, a fresh file may start.
    records_path(notebook).unlink()
    with_evidence(None, KEY, _entry(), [KEY], notebook)
    with_evidence(None, KEY, _entry(), [KEY])


# ----- saved decisions ---------------------------------------------------------

DECIDE = "-- @decide two : entailment |- 2 + 2 = 4"
DECIDE_MORE = "--   by arithmetic"
DECIDING = f"/-! # Notes -/\ndef M := 1\n{DECIDE}\n{DECIDE_MORE}\ndef after := 2\n"
BLOCK = "namespace Decided_two\ntheorem two : 2 + 2 = 4 := by decide\nend Decided_two"
PROVEN = {
    "status": "proven",
    "reason": "arithmetic",
    "stages": [["normalise", "ok", "2 + 2 = 4"]],
    "block": BLOCK,
    "theorem": "two",
}
UNKNOWN = {
    "status": "unknown",
    "reason": "no lane applies",
    "stages": [],
    "block": None,
    "theorem": None,
}
CHECKED = {
    "status": "checked",
    "message": "The checker reports `Decided_two.two` as checked.",
    "axioms": ["propext"],
    "type": "2 + 2 = 4",
}
REJECTED = {**CHECKED, "status": "rejected", "message": "`two` is admitted."}
DECISION_KEY = decision_key(f"{DECIDE}\n{DECIDE_MORE}", 0)


def _decision(outcome=PROVEN, verification=CHECKED, text=DECIDING, fingerprint=FP):
    return {
        "prefix_sha256": decision_prefix_sha256(text, 4),
        "fingerprint": fingerprint,
        "provider": "demo",
        "created": "2026-10-14T12:00:00+00:00",
        "outcome": checked_decision(outcome),
        "verification": verification,
    }


def _decided(text, entry=None, fingerprint=FP, end_line=4):
    return decision_status(entry or _decision(), text, end_line, fingerprint)


def _accepted(text=DECIDING, block=BLOCK):
    return text.replace(f"{DECIDE_MORE}\n", f"{DECIDE_MORE}\n{block}\n")


def test_a_saved_decision_reads_as_its_outcome_while_current():
    assert _decided(DECIDING) == "candidate"
    assert _decided(DECIDING, _decision(verification=REJECTED)) == "rejected"
    assert _decided(DECIDING, _decision(UNKNOWN, None)) == "unknown"
    below = DECIDING.replace("def after := 2", "def after := 3")
    assert _decided(below) == "candidate"
    assert _decided(DECIDING.replace("\n", "\r\n")) == "candidate"


def test_edits_through_the_directive_or_a_new_environment_mark_a_decision_stale():
    assert _decided(DECIDING.replace("def M := 1", "def M := 2")) == "stale"
    assert _decided(DECIDING.replace("arithmetic", "arith")) == "stale"
    assert _decided(DECIDING, fingerprint=GP) == "stale"
    unknown = _decision(UNKNOWN, None)
    assert _decided(DECIDING.replace("# Notes", "# Other"), unknown) == "stale"


def test_a_checked_block_right_after_the_directive_is_accepted():
    assert _decided(_accepted()) == "accepted"
    assert _decided(_accepted().replace("\n", "\r\n")) == "accepted"
    # At the end of a file without a final newline.
    assert _decided(f"{DECIDE}\n{DECIDE_MORE}\n{BLOCK}", end_line=2) == "accepted"


def test_an_accepted_block_stays_accepted_when_the_text_above_changes():
    """Review Focus 4: the block is in the file, so the check judges it."""
    edited = _accepted().replace("def M := 1", "def M := 2")
    assert _decided(edited) == "accepted"
    assert _decided(_accepted(), fingerprint=GP) == "accepted"


def test_deleting_an_accepted_block_returns_the_saved_state():
    """Review Focus 4: candidate again while current, stale after edits above."""
    removed = _accepted().replace(f"{BLOCK}\n", "")
    assert removed == DECIDING and _decided(removed) == "candidate"
    edited = _accepted().replace("def M := 1", "def M := 2")
    assert _decided(edited.replace(f"{BLOCK}\n", "")) == "stale"


@pytest.mark.parametrize(
    "text",
    [
        _accepted(block=BLOCK.replace("by decide", "by  decide")),
        _accepted(block=BLOCK.split("\n", 1)[1]),
        _accepted(block=f"\n{BLOCK}"),
        _accepted(block=f"  {BLOCK}"),
        DECIDING.replace("def after := 2", f"def after := 2\n{BLOCK}"),
    ],
)
def test_only_the_exact_block_right_after_the_directive_is_accepted(text):
    assert _decided(text) == "candidate"


def test_a_block_cut_short_by_the_end_of_the_file_is_not_accepted():
    lines = DECIDING.split("\n")
    through_directive = "\n".join(lines[:4])
    partial = "\n".join(BLOCK.split("\n")[:2])
    for ending in ("", "\n"):
        assert _decided(f"{through_directive}\n{partial}{ending}") == "candidate"


def test_a_rejected_block_in_the_text_is_never_accepted():
    """Only a checked block can be accepted; a pasted rejected one is not."""
    assert _decided(_accepted(), _decision(verification=REJECTED)) == "rejected"


def test_with_decision_saves_beside_evidence_and_claims(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_evidence(records_from_check(TEXT, RESULT, FP), KEY, _entry(), [KEY])
    records = with_decision(records, DECISION_KEY, _decision(), [], notebook)
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert loaded == records
    assert saved_decisions(loaded) == {DECISION_KEY: _decision()}
    assert saved_evidence(loaded) == {KEY: _entry()}
    assert [c["name"] for c in loaded["claims"]] == ["A.result", "B.result"]


def test_with_decision_starts_a_file_and_prunes_gone_directives():
    records = with_decision(None, "gone", _decision(), [])
    assert records["claims"] == [] and records["environment"]["fingerprint"] == FP
    records = with_decision(records, "other", _decision(UNKNOWN, None), ["gone"])
    records = with_decision(records, DECISION_KEY, _decision(), ["other"])
    assert set(saved_decisions(records)) == {"other", DECISION_KEY}


def test_with_decision_does_not_change_its_input():
    checked = records_from_check(TEXT, RESULT, FP)
    before = json.dumps(checked, sort_keys=True)
    updated = with_decision(checked, DECISION_KEY, _decision(), [])
    assert json.dumps(checked, sort_keys=True) == before
    assert saved_decisions(checked) == {} and saved_decisions(updated) != {}


@pytest.mark.parametrize(
    "entry",
    [
        {**_decision(), "extra": 1},
        {k: v for k, v in _decision().items() if k != "created"},
        {**_decision(), "created": "2026-10-14T12:00:00"},
        {**_decision(), "created": "2026-10-14T12:00:00-05:00"},
        {**_decision(), "created": "2026-10-14 12:00"},
        {**_decision(), "provider": None},
        {**_decision(), "outcome": {**PROVEN, "status": "maybe"}},
        # Saved outcomes are in normal form: all five keys present.
        {**_decision(), "outcome": {k: v for k, v in PROVEN.items() if k != "stages"}},
        {**_decision(), "verification": None},
        {**_decision(UNKNOWN, None), "verification": CHECKED},
        {**_decision(), "verification": {**CHECKED, "status": "checked "}},
        {**_decision(), "verification": {**CHECKED, "status": True}},
        {**_decision(), "verification": {**CHECKED, "message": "x" * 2001}},
        {**_decision(), "verification": {**CHECKED, "axioms": "propext"}},
        {**_decision(), "verification": {**CHECKED, "axioms": [1]}},
        {**_decision(), "verification": {**CHECKED, "type": 4}},
        {**_decision(), "verification": {**CHECKED, "type": "T" * 20_001}},
        {**_decision(), "verification": {**CHECKED, "extra": 1}},
    ],
)
def test_a_malformed_decision_is_refused_and_makes_the_file_unreadable(tmp_path, entry):
    with pytest.raises(ValueError, match="decision entry is malformed"):
        with_decision(None, DECISION_KEY, entry, [])
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, RESULT, FP)
    records_path(notebook).write_text(
        json.dumps({**records, "decisions": {DECISION_KEY: entry}})
    )
    assert load_records(notebook) is None


@pytest.mark.parametrize("decisions", [[], {DECISION_KEY: []}, {DECISION_KEY: None}])
def test_a_malformed_decisions_section_makes_the_file_unreadable(tmp_path, decisions):
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, RESULT, FP)
    records_path(notebook).write_text(json.dumps({**records, "decisions": decisions}))
    assert load_records(notebook) is None


def test_a_verification_message_of_the_limit_is_kept():
    entry = _decision(verification={**CHECKED, "message": "x" * 2000, "type": None})
    assert saved_decisions(with_decision(None, DECISION_KEY, entry, [])) == {
        DECISION_KEY: entry
    }


def test_a_verification_type_of_the_limit_is_kept():
    entry = _decision(verification={**CHECKED, "type": "T" * 20_000})
    assert saved_decisions(with_decision(None, DECISION_KEY, entry, [])) == {
        DECISION_KEY: entry
    }


def test_without_decision_removes_only_that_decision(tmp_path: Path):
    records = with_decision(None, "other", _decision(UNKNOWN, None), [])
    records = with_decision(records, DECISION_KEY, _decision(), ["other"])
    records = with_evidence(records, KEY, _entry(), [KEY])
    before = json.dumps(records, sort_keys=True)
    removed = without_decision(records, DECISION_KEY)
    assert json.dumps(records, sort_keys=True) == before
    assert set(saved_decisions(removed)) == {"other"}
    assert saved_evidence(removed) == {KEY: _entry()}
    emptied = without_decision(removed, "other")
    assert "decisions" not in emptied and saved_evidence(emptied) == {KEY: _entry()}
    notebook = tmp_path / "N.lean"
    save_records(notebook, emptied)
    assert load_records(notebook) == emptied
    with pytest.raises(ValueError, match="No decision is saved"):
        without_decision(emptied, "other")
    with pytest.raises(ValueError, match="No decision is saved"):
        without_decision(None, "other", notebook.with_name("Missing.lean"))


@pytest.mark.parametrize("content", ["{not json", '{"format": "other/v9"}'])
def test_decisions_never_replace_an_unreadable_records_file(tmp_path, content):
    notebook = tmp_path / "N.lean"
    records_path(notebook).write_text(content)
    with pytest.raises(ValueError, match="cannot be read.*saving decisions"):
        with_decision(None, DECISION_KEY, _decision(), [], notebook)
    with pytest.raises(ValueError, match="cannot be read.*discarding a decision"):
        without_decision(None, DECISION_KEY, notebook)
    assert records_path(notebook).read_text() == content


def test_saved_decisions_that_would_exceed_the_file_cap_are_refused(
    tmp_path: Path, monkeypatch
):
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, RESULT, FP)
    one = with_decision(records, "k0", _decision(), [])
    monkeypatch.setattr(
        check_records, "_MAX_RECORDS", len(_serialised(one).encode()) + 10
    )
    save_records(notebook, with_decision(records, "k0", _decision(), [], notebook))
    before = records_path(notebook).read_bytes()
    with pytest.raises(ValueError, match="Saved decisions would make N.glitnir.json"):
        with_decision(load_records(notebook), "k1", _decision(), ["k0"], notebook)
    assert records_path(notebook).read_bytes() == before


def test_a_check_keeps_saved_decisions():
    previous = with_decision(None, DECISION_KEY, _decision(), [])
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert saved_decisions(records) == {DECISION_KEY: _decision()}


@pytest.mark.parametrize(
    "sections", [("evidence",), ("decisions",), ("evidence", "decisions")]
)
def test_a_check_refuses_records_too_large_for_its_saved_results(monkeypatch, sections):
    previous = None
    if "evidence" in sections:
        previous = with_evidence(previous, KEY, _entry(), [KEY])
    if "decisions" in sections:
        previous = with_decision(previous, DECISION_KEY, _decision(), [])
    claims_only = len(_serialised(records_from_check(TEXT, RESULT, FP)).encode())
    monkeypatch.setattr(check_records, "_MAX_RECORDS", claims_only + 10)
    with pytest.raises(ValueError, match="16 MiB"):
        records_from_check(TEXT, RESULT, FP, previous)
    # Claims alone over the limit are refused too.
    monkeypatch.setattr(check_records, "_MAX_RECORDS", 10)
    with pytest.raises(ValueError, match="16 MiB"):
        records_from_check(TEXT, RESULT, FP)


@pytest.mark.parametrize(
    "created", ["2026-10-14T12:00:00+00:00", "2026-10-14T12:00:00Z"]
)
def test_created_is_read_as_utc_in_either_spelling(tmp_path, created):
    notebook = tmp_path / "N.lean"
    records = with_evidence(None, KEY, {**_entry(), "created": created}, [KEY])
    records = with_decision(
        records, DECISION_KEY, {**_decision(), "created": created}, [KEY]
    )
    save_records(notebook, records)
    assert load_records(notebook) == records


def test_saved_entries_are_copies_of_what_was_given():
    evidence, decision = copy.deepcopy(_entry()), copy.deepcopy(_decision())
    records = with_evidence(None, KEY, evidence, [KEY])
    records = with_decision(records, DECISION_KEY, decision, [KEY])
    before = json.dumps(records, sort_keys=True)
    evidence["provider"] = "changed"
    evidence["snapshot"]["provenance"].append(["late", "edit"])
    decision["outcome"]["stages"].append(["late", "ok", ""])
    decision["verification"]["axioms"].append("sorryAx")
    assert json.dumps(records, sort_keys=True) == before


EXAMPLES = (
    "def base : Nat := 1\n"
    "example : base = 1 := rfl\n"
    "theorem t : True := True.intro\n"
    "example : base = 1 := rfl\n"
)


def _example_result():
    def theorem(name, line, example):
        return {
            "name": name,
            "status": "checked",
            "axioms": [],
            "start_line": line,
            "end_line": line,
            "example": example,
            "type": f"Eq base {line}",
            "statement": f"base = {line}",
            "proposition_sha256": f"p{line}",
        }

    return {
        **RESULT,
        "theorems": [
            theorem("_private.M.0._glitnir_example_20", 2, True),
            theorem("t", 3, False),
            theorem("_private.M.0._glitnir_example_77", 4, True),
        ],
    }


def test_examples_are_saved_as_marked_claims(tmp_path):
    """Examples keep their mark through saving; other claims carry none."""
    notebook = tmp_path / "A.lean"
    records = records_from_check(EXAMPLES, _example_result(), FP)
    assert [c.get("example") for c in records["claims"]] == [True, None, True]
    assert [c.get("statement") for c in records["claims"]] == [
        "base = 2",
        None,
        "base = 4",
    ]
    assert [c.get("kernel_statement") for c in records["claims"]] == [
        "Eq base 2",
        None,
        "Eq base 4",
    ]
    assert records["format"] == "glitnir.project-checks/v2"
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert loaded == records
    found = claim_views(EXAMPLES, loaded, FP)
    assert [(v["start_line"], v.get("example"), v["current"]) for v in found] == [
        (2, True, True),
        (3, None, True),
        (4, True, True),
    ]


@pytest.mark.parametrize("mark", [False, "true", 1, None])
def test_a_malformed_example_mark_is_unreadable(tmp_path, mark):
    """Only `"example": true` marks an example; any other value is refused."""
    notebook = tmp_path / "A.lean"
    records = records_from_check(EXAMPLES, _example_result(), FP)
    records["claims"][0]["example"] = mark
    save_records(notebook, records)
    assert load_records(notebook) is None


def test_identical_examples_keep_their_own_results():
    """Equal example texts are matched in order; an edit between stales the later."""
    records = records_from_check(EXAMPLES, _example_result(), FP)
    edited = EXAMPLES.replace("theorem t", "theorem t'")
    found = claim_views(edited, records, FP)
    assert [(v["name"], v["start_line"], v["current"]) for v in found] == [
        ("_private.M.0._glitnir_example_20", 2, True),
        ("_private.M.0._glitnir_example_77", 4, False),
    ]
    found = claim_views("\n" + EXAMPLES, records, FP)
    assert [(v["start_line"], v["current"]) for v in found] == [
        (3, False),
        (4, False),
        (5, False),
    ]


def test_a_long_checked_statement_is_saved_shortened(tmp_path):
    """The checked statement is kept within the saved length, marked when cut."""
    result = _example_result()
    result["theorems"][0]["type"] = "x" * (check_records.MAX_TYPE + 5)
    result["theorems"][2]["statement"] = "y" * (check_records.MAX_TYPE + 1)
    records = records_from_check(EXAMPLES, result, FP)
    kernel = records["claims"][0]["kernel_statement"]
    assert len(kernel) == check_records.MAX_TYPE and kernel.endswith("…")
    # Each form says for itself whether it was cut.
    assert records["claims"][0]["statement"] == "base = 2"
    assert records["claims"][0]["kernel_statement_shortened"] is True
    assert "statement_shortened" not in records["claims"][0]
    printed = records["claims"][2]["statement"]
    assert len(printed) == check_records.MAX_TYPE and printed.endswith("…")
    assert records["claims"][2]["statement_shortened"] is True
    assert "kernel_statement_shortened" not in records["claims"][2]
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") == records
    first, _, last = claim_views(EXAMPLES, records, FP)
    assert first["kernel_statement"] == kernel
    assert first["kernel_statement_shortened"] is True
    assert "statement_shortened" not in first
    assert last["statement"] == printed and last["statement_shortened"] is True
    result["theorems"][2]["statement"] = "base = 4"
    unshortened = claim_views(EXAMPLES, records_from_check(EXAMPLES, result, FP), FP)
    assert "statement_shortened" not in unshortened[2]


@pytest.mark.parametrize("key", ["statement_shortened", "kernel_statement_shortened"])
@pytest.mark.parametrize("mark", [False, "true", None, 1])
def test_a_malformed_shortened_mark_is_unreadable(tmp_path, mark, key):
    """Only `true` marks a shortened statement."""
    records = records_from_check(EXAMPLES, _example_result(), FP)
    records["claims"][0][key] = mark
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") is None


@pytest.mark.parametrize("key", ["statement", "kernel_statement"])
@pytest.mark.parametrize("statement", [None, 3, "x" * (check_records.MAX_TYPE + 1)])
def test_a_malformed_checked_statement_is_unreadable(tmp_path, statement, key):
    """Only a string within the saved length is a checked statement."""
    records = records_from_check(EXAMPLES, _example_result(), FP)
    records["claims"][0][key] = statement
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") is None


def test_a_check_without_a_printed_statement_saves_the_kernel_form():
    """A statement the reader could not print leaves only the kernel form."""
    result = _example_result()
    del result["theorems"][0]["statement"]
    result["theorems"][2]["statement"] = None
    records = records_from_check(EXAMPLES, result, FP)
    first, _, last = claim_views(EXAMPLES, records, FP)
    for view, line in ((first, 2), (last, 4)):
        assert "statement" not in view
        assert view["kernel_statement"] == f"Eq base {line}"


def _v1(records):
    """Write ``records`` as Glitnir saved them before format v2."""
    old = copy.deepcopy(records)
    old["format"] = "glitnir.project-checks/v1"
    for claim in old["claims"]:
        claim.pop("statement", None)
        if "kernel_statement" in claim:
            # v1 saved the kernel form as the one statement.
            claim["statement"] = claim.pop("kernel_statement")
    return old


def test_v1_records_stay_readable_with_their_kernel_statements(tmp_path):
    """An old example statement is the kernel form; claims stay current."""
    notebook = tmp_path / "A.lean"
    records = records_from_check(EXAMPLES, _example_result(), FP)
    old = _v1(records)
    records_path(notebook).write_text(json.dumps(old))
    loaded = load_records(notebook)
    assert loaded is not None and loaded["format"] == check_records.FORMAT
    found = claim_views(EXAMPLES, loaded, FP)
    assert [(v.get("statement"), v.get("kernel_statement")) for v in found] == [
        (None, "Eq base 2"),
        (None, None),
        (None, "Eq base 4"),
    ]
    assert all(v["current"] for v in found)
    # Saving anything over v1 records writes the current format.
    saved = with_evidence(loaded, KEY, _entry(), [KEY])
    assert saved["format"] == check_records.FORMAT
    assert saved["claims"] == loaded["claims"]


def test_v1_records_with_a_v2_field_are_unreadable(tmp_path):
    """A v1 record never held a kernel statement of its own."""
    notebook = tmp_path / "A.lean"
    old = _v1(records_from_check(EXAMPLES, _example_result(), FP))
    old["claims"][0]["kernel_statement"] = "Eq base 2"
    records_path(notebook).write_text(json.dumps(old))
    assert load_records(notebook) is None


NON_CLAIMS = "def base : Nat := 1\nexample : Nat := 3\nexample := base\n"


def test_examples_without_a_proposition_are_saved_as_non_claims(tmp_path):
    """They are found again by their text while everything above is unchanged."""
    from glitnir.notebook.check_records import non_claim_views

    result = {
        **RESULT,
        "theorems": [],
        "non_claims": [
            {"start_line": 2, "end_line": 2},
            {"start_line": 3, "end_line": 3},
        ],
    }
    records = records_from_check(NON_CLAIMS, result, FP)
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") == records
    lines = [
        (v["start_line"], v["end_line"])
        for v in non_claim_views(NON_CLAIMS, records, FP)
    ]
    assert lines == [(2, 2), (3, 3)]
    assert non_claim_views(NON_CLAIMS, records, "other") == []
    edited = NON_CLAIMS.replace("example : Nat := 3", "example : Nat := 4")
    assert [v["start_line"] for v in non_claim_views(edited, records, FP)] == []
    assert non_claim_views(NON_CLAIMS + "-- below\n", records, FP) == [
        {"start_line": 2, "end_line": 2},
        {"start_line": 3, "end_line": 3},
    ]
    assert "non_claims" not in records_from_check(TEXT, RESULT, FP)


@pytest.mark.parametrize(
    "non_claims",
    [
        {},
        [{"start_line": 2, "end_line": 2}],
        [
            {
                "start_line": "2",
                "end_line": 2,
                "declaration_sha256": "",
                "prefix_sha256": "",
            }
        ],
    ],
)
def test_malformed_non_claims_are_unreadable(tmp_path, non_claims):
    records = records_from_check(TEXT, RESULT, FP)
    records["non_claims"] = non_claims
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") is None


USES = [
    {"module": "M", "name": "A.result", "in_notebook": True},
    {"module": "Other", "name": "Other.lemma", "in_notebook": False},
]


def _with_uses(uses=USES):
    result = copy.deepcopy(RESULT)
    result["theorems"][1]["uses"] = uses
    result["theorems"][0]["uses"] = []
    return result


def test_claims_save_the_claims_they_use(tmp_path):
    """Uses are saved per claim and shown with it, current or not."""
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, _with_uses(), FP)
    assert [c["uses"] for c in records["claims"]] == [[], USES]
    save_records(notebook, records)
    assert load_records(notebook) == records
    found = {v["name"]: v for v in claim_views(TEXT, records, "other")}
    assert found["A.result"]["uses"] == [] and found["B.result"]["uses"] == USES
    assert not found["B.result"]["current"], "uses never make a result current"


def test_a_check_without_uses_records_none():
    """No uses reported is not the same as using nothing: nothing is saved."""
    records = records_from_check(TEXT, RESULT, FP)
    assert all("uses" not in claim for claim in records["claims"])
    assert all("uses" not in view for view in claim_views(TEXT, records, FP))


@pytest.mark.parametrize(
    "uses",
    [
        None,
        {"module": "M", "name": "A.result", "in_notebook": True},
        [{"module": "M", "name": "A.result"}],
        [{"module": "M", "name": "A.result", "in_notebook": 1}],
        [{"module": "M", "name": 3, "in_notebook": True}],
        [{"module": "M", "name": "A", "in_notebook": True, "path": "M.lean"}],
    ],
)
def test_malformed_uses_are_unreadable(tmp_path, uses):
    """Only a list of exact `{module, name, in_notebook}` entries is saved uses."""
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, _with_uses(), FP)
    records["claims"][1]["uses"] = uses
    save_records(notebook, records)
    assert load_records(notebook) is None


def test_v1_records_with_uses_are_unreadable(tmp_path):
    """Format v1 recorded no uses."""
    notebook = tmp_path / "N.lean"
    old = _v1(records_from_check(TEXT, _with_uses(), FP))
    records_path(notebook).write_text(json.dumps(old))
    assert load_records(notebook) is None
    for claim in old["claims"]:
        del claim["uses"]
    records_path(notebook).write_text(json.dumps(old))
    assert load_records(notebook) is not None


THREE = (
    "def base : Nat := 1\n"
    "theorem one : base = 1 := rfl\n"
    "example : Nat := 3\n"
    "theorem two : True := trivial\n"
    "example : Nat := 4\n"
    "theorem three : True := trivial\n"
)


def _theorem(name, line, status="checked"):
    return {
        "name": name,
        "status": status,
        "axioms": [],
        "start_line": line,
        "end_line": line,
        "proposition_sha256": f"p{line}",
        "uses": [],
    }


def _full_check(text=THREE):
    return {
        **RESULT,
        "theorems": [_theorem("one", 2), _theorem("two", 4), _theorem("three", 6)],
        "non_claims": [
            {"start_line": 3, "end_line": 3},
            {"start_line": 5, "end_line": 5},
        ],
    }


def _through(line, theorems, non_claims=()):
    """A check through ``line``, as `check_project` reports it."""
    return {
        **RESULT,
        "theorems": theorems,
        "non_claims": list(non_claims),
        "withheld": {
            "from_line": line + 1,
            "message": f"Checked through line {line} on request",
            "through_line": line,
        },
    }


def _views(text, records, fingerprint):
    return {v["name"]: v for v in claim_views(text, records, fingerprint)}


def test_a_check_through_a_claim_keeps_later_results_not_rechecked(tmp_path):
    """(a) claims through the cut are fresh; (b) later ones keep their results.

    (c) The records say the check covered lines 1 to N.
    """
    previous = records_from_check(THREE, _full_check(), FP)
    partial = _through(2, [_theorem("one", 2, "admitted")])
    records = records_from_check(THREE, partial, FP, previous)
    assert records["withheld"] == partial["withheld"]
    by_name = {c["name"]: c for c in records["claims"]}
    assert (
        by_name["one"]["status"] == "admitted" and "fingerprint" not in by_name["one"]
    )
    for name in ("two", "three"):
        kept = {k: v for k, v in by_name[name].items() if k != "fingerprint"}
        assert kept == {c["name"]: c for c in previous["claims"]}[name]
        assert by_name[name]["fingerprint"] == FP
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") == records
    found = _views(THREE, records, FP)
    assert found["one"]["current"] and "not_rechecked" not in found["one"]
    # Same environment and source: the earlier results still describe them.
    assert found["two"]["current"] and found["two"]["not_rechecked"] is True
    assert found["three"]["current"] and found["three"]["not_rechecked"] is True


def test_later_results_are_never_made_current_by_a_check_through_a_claim():
    """(b) A later result from another environment or text stays out of date."""
    previous = records_from_check(THREE, _full_check(), OLD)
    records = records_from_check(
        THREE, _through(2, [_theorem("one", 2)]), NEW, previous
    )
    found = _views(THREE, records, NEW)
    assert found["one"]["current"]
    assert not found["two"]["current"] and not found["three"]["current"]
    assert found["two"]["not_rechecked"] is True
    # The text above `two` changed since its result: out of date, kept as such.
    edited = THREE.replace("def base : Nat := 1", "def base : Nat := 1 -- now")
    previous = records_from_check(THREE, _full_check(), FP)
    records = records_from_check(
        edited, _through(2, [_theorem("one", 2)]), FP, previous
    )
    found = _views(edited, records, FP)
    assert found["one"]["current"]
    assert not found["two"]["current"] and not found["three"]["current"]


def test_not_rechecked_results_keep_the_environment_they_were_checked_in():
    """A second check through a later claim carries the first one's results on."""
    previous = records_from_check(THREE, _full_check(), OLD)
    first = records_from_check(THREE, _through(2, [_theorem("one", 2)]), NEW, previous)
    second = records_from_check(
        THREE, _through(4, [_theorem("one", 2), _theorem("two", 4)]), NEW, first
    )
    by_name = {c["name"]: c for c in second["claims"]}
    assert "fingerprint" not in by_name["two"]
    assert by_name["three"]["fingerprint"] == OLD
    found = _views(THREE, second, NEW)
    assert found["one"]["current"] and found["two"]["current"]
    assert not found["three"]["current"]


def test_a_whole_check_replaces_every_result():
    """(d) A later check of the whole file supersedes everything."""
    previous = records_from_check(THREE, _full_check(), FP)
    partial = records_from_check(THREE, _through(2, [_theorem("one", 2)]), FP, previous)
    full = records_from_check(THREE, _full_check(), FP, partial)
    assert full["withheld"] is None
    assert all("fingerprint" not in c for c in full["claims"])
    assert all("not_rechecked" not in v for v in claim_views(THREE, full, FP))
    # One that stops at a failing command keeps nothing after it either.
    failing = {
        **RESULT,
        "theorems": [_theorem("one", 2)],
        "withheld": {"from_line": 3, "message": "bad"},
    }
    stopped = records_from_check(THREE, failing, FP, partial)
    assert [c["name"] for c in stopped["claims"]] == ["one"]


def test_a_check_through_a_claim_keeps_later_non_claims_only_in_the_same_environment():
    """Examples that claim nothing are notes shown while the records match."""
    from glitnir.notebook.check_records import non_claim_views

    previous = records_from_check(THREE, _full_check(), FP)
    partial = _through(3, [_theorem("one", 2)], [{"start_line": 3, "end_line": 3}])
    same = records_from_check(THREE, partial, FP, previous)
    assert non_claim_views(THREE, same, FP) == [
        {"start_line": 3, "end_line": 3},
        {"start_line": 5, "end_line": 5},
    ]
    other = records_from_check(THREE, partial, GP, previous)
    assert non_claim_views(THREE, other, GP) == [{"start_line": 3, "end_line": 3}]


def test_a_check_through_a_claim_without_earlier_records_keeps_nothing_later():
    records = records_from_check(THREE, _through(2, [_theorem("one", 2)]), FP)
    assert [c["name"] for c in records["claims"]] == ["one"]


@pytest.mark.parametrize(
    "withheld",
    [
        {"from_line": 3},
        {"from_line": 0, "message": "x"},
        {"from_line": "3", "message": "x"},
        {"from_line": 3, "message": 3},
        {"from_line": 3, "message": "x", "other": 1},
        {"from_line": 3, "message": "x", "through_line": 0},
        {"from_line": 4, "message": "x", "through_line": 2},
        {"from_line": 3, "message": "x", "through_line": "2"},
        "line 3",
    ],
)
def test_malformed_withheld_records_are_unreadable(tmp_path, withheld):
    records = records_from_check(THREE, _full_check(), FP)
    records["withheld"] = withheld
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") is None


def test_a_not_rechecked_mark_is_a_fingerprint_and_v1_has_none(tmp_path):
    previous = records_from_check(THREE, _full_check(), FP)
    records = records_from_check(THREE, _through(2, [_theorem("one", 2)]), FP, previous)
    records["claims"][1]["fingerprint"] = 7
    save_records(tmp_path / "A.lean", records)
    assert load_records(tmp_path / "A.lean") is None
    old = _v1(records_from_check(THREE, _through(2, [_theorem("one", 2)]), FP))
    for claim in old["claims"]:
        claim.pop("uses", None)
    records_path(tmp_path / "A.lean").write_text(json.dumps(old))
    assert load_records(tmp_path / "A.lean") is None, "v1 checked whole files only"


C1 = "def h : 1 = 1 := rfl\ntheorem foo : 1 = 1 := h\n"


def _c1_records():
    return records_from_check(C1, {**RESULT, "theorems": [_theorem("foo", 2)]}, FP)


@pytest.mark.parametrize(
    "continuation",
    [
        "  |>.trans sorry\n",
        "|>.trans sorry\n",
        "where aux := 1\n",
        "termination_by 0\n",
        "decreasing_by simp\n",
        "  <;> rfl\n",
        "-- a note\n  |>.trans sorry\n",
        "\n/- a note -/\n.trans sorry\n",
    ],
)
def test_a_claim_whose_command_now_runs_on_is_out_of_date(continuation):
    """Text continuing the claim's command below it makes it out of date.

    The claim is then shown through the continuation, so the added lines are
    part of its cell rather than a cell of their own.
    """
    [view] = claim_views(C1 + continuation, _c1_records(), FP)
    assert view["current"] is False
    assert view["start_line"] == 2
    assert view["end_line"] == 2 + continuation.rstrip("\n").count("\n") + 1


def test_a_column_0_term_continuation_is_out_of_date():
    text = "def h : 1 = 1 := rfl\ntheorem foo : 1 = 1 :=\nh\n"
    result = {**RESULT, "theorems": [_theorem("foo", 2)]}
    # Recorded as if the reader had said the command ends on line 2.
    records = records_from_check(text, result, FP)
    [view] = claim_views(text, records, FP)
    assert view["current"] is False and view["end_line"] == 3


@pytest.mark.parametrize(
    "below",
    [
        "theorem bar : True := trivial\n",
        "\n-- a note\n/-- doc -/\ntheorem bar : True := trivial\n",
        "#print axioms foo\n",
        "",
    ],
)
def test_a_claim_followed_by_another_command_stays_current(below):
    [view] = claim_views(C1 + below, _c1_records(), FP)
    assert view["current"] is True and view["end_line"] == 2


def test_non_claims_that_now_run_on_are_not_shown():
    from glitnir.notebook.check_records import non_claim_views

    text = "example : Nat := 3\n"
    records = records_from_check(
        text,
        {**RESULT, "theorems": [], "non_claims": [{"start_line": 1, "end_line": 1}]},
        FP,
    )
    assert non_claim_views(text, records, FP) == [{"start_line": 1, "end_line": 1}]
    assert non_claim_views(text + "  + 1\n", records, FP) == []


def test_uses_are_capped_and_marked_truncated(tmp_path):
    """At most MAX_USES uses are saved per claim; more are marked."""
    from glitnir.lean.project_checking import MAX_USES

    many = [
        {"module": "M", "name": f"x{i}", "in_notebook": True}
        for i in range(MAX_USES + 5)
    ]
    records = records_from_check(TEXT, _with_uses(many), FP)
    claim = records["claims"][1]
    assert len(claim["uses"]) == MAX_USES and claim["uses_truncated"] is True
    assert "uses_truncated" not in records["claims"][0]
    save_records(tmp_path / "N.lean", records)
    assert load_records(tmp_path / "N.lean") == records
    assert claim_views(TEXT, records, FP)[1]["uses_truncated"] is True
    for bad in (False, 1):
        records["claims"][1]["uses_truncated"] = bad
        save_records(tmp_path / "N.lean", records)
        assert load_records(tmp_path / "N.lean") is None
    records["claims"][1]["uses_truncated"] = True
    records["claims"][1]["uses"] = many
    save_records(tmp_path / "N.lean", records)
    assert load_records(tmp_path / "N.lean") is None, "over the cap"
    del records["claims"][1]["uses"]
    save_records(tmp_path / "N.lean", records)
    assert load_records(tmp_path / "N.lean") is None, "a mark without uses"


INDENTED = (
    "namespace Foo\n"
    "  theorem a : True := trivial\n"
    "  theorem b : True := trivial\n"
    "end Foo\n"
)


def test_claims_before_indented_claims_stay_current():
    """Indented commands inside a namespace are command starts too."""
    result = {**RESULT, "theorems": [_theorem("Foo.a", 2), _theorem("Foo.b", 3)]}
    records = records_from_check(INDENTED, result, FP)
    found = claim_views(INDENTED, records, FP)
    assert [
        (v["name"], v["start_line"], v["end_line"], v["current"]) for v in found
    ] == [
        ("Foo.a", 2, 2, True),
        ("Foo.b", 3, 3, True),
    ]


def test_an_extended_claim_never_takes_in_the_next_claim():
    """Two claims, the first continued below: it stops before the second."""
    text = (
        "theorem a : True := by\n  trivial\n"
        "  <;> trivial\n"
        "theorem b : True := trivial\n"
    )
    saved = "theorem a : True := by\n  trivial\ntheorem b : True := trivial\n"
    result = {**RESULT, "theorems": [_theorem("a", 2), _theorem("b", 3)]}
    result["theorems"][0]["start_line"] = 1
    records = records_from_check(saved, result, FP)
    a, b = claim_views(text, records, FP)
    assert (a["start_line"], a["end_line"], a["current"]) == (1, 3, False)
    assert (b["start_line"], b["end_line"]) == (4, 4)
    # Even where no command end is found before the next claim, the first
    # claim's lines stop at it.

    class Nowhere:
        def ends_at(self, line):
            return False

        def end_from(self, line):
            return 99

    import glitnir.notebook.check_records as module

    original = module.command_boundaries
    module.command_boundaries = lambda text: Nowhere()
    try:
        a, b = claim_views(text, records, FP)
    finally:
        module.command_boundaries = original
    assert (a["end_line"], b["start_line"], b["end_line"]) == (3, 4, 5)


@pytest.mark.parametrize("opener", ["@[simp]", "/-- d -/"])
def test_a_where_declaration_added_below_makes_the_claim_out_of_date(opener):
    """An attribute or doc comment opening a `where` declaration is no command."""
    saved = "theorem foo : True := bar\nwhere\nbar : True := baz\n"
    result = {**RESULT, "theorems": [{**_theorem("foo", 3), "start_line": 1}]}
    records = records_from_check(saved, result, FP)
    [view] = claim_views(saved + f"{opener}\nbaz : True := sorry\n", records, FP)
    assert view["current"] is False and view["end_line"] == 5
    [control] = claim_views(
        saved + "@[simp]\ntheorem baz : True := trivial\n", records, FP
    )
    assert control["current"] is True and control["end_line"] == 3


# ----- what a fingerprint may match, and comparing as recorded ---------------


def _saved_under(records, fingerprint):
    """Records whose environment says `fingerprint`, as an edited file might."""
    environment = {**records["environment"], "fingerprint": fingerprint}
    return {**records, "environment": environment}


@pytest.mark.parametrize("current", [None, "", "f", "F" * 64, "f" * 63, "g" * 64])
def test_a_fingerprint_that_is_not_a_digest_is_never_current(current):
    records = records_from_check(TEXT, RESULT, FP)
    assert [v["current"] for v in claim_views(TEXT, records, current)] == [False] * 2
    assert evidence_status(_entry(), NOTEBOOK, 3, current) == "stale"
    assert _decided(DECIDING, fingerprint=current) == "stale"


@pytest.mark.parametrize("saved", ["", "f", "F" * 64, "g" * 64])
def test_a_result_saved_under_no_digest_never_matches(saved):
    """A failed fingerprint reads as '' and must not match a saved ''."""
    from glitnir.notebook.check_records import non_claim_views

    records = _saved_under(records_from_check(TEXT, RESULT, FP), saved)
    result = {
        **RESULT,
        "theorems": [],
        "non_claims": [{"start_line": 2, "end_line": 2}],
    }
    examples = _saved_under(records_from_check(NON_CLAIMS, result, FP), saved)
    entry, decision = _entry(fingerprint=saved), _decision(fingerprint=saved)
    for compare in (True, False):
        found = claim_views(TEXT, records, saved, compare_environment=compare)
        assert [v["current"] for v in found] == [False] * 2
        assert (
            non_claim_views(NON_CLAIMS, examples, saved, compare_environment=compare)
            == []
        )
        assert (
            evidence_status(entry, NOTEBOOK, 3, saved, compare_environment=compare)
            == "stale"
        )
        assert (
            decision_status(decision, DECIDING, 4, saved, compare_environment=compare)
            == "stale"
        )


def test_comparing_as_recorded_compares_only_the_source():
    """Each result matches the environment it was saved in, whatever is given."""
    from glitnir.notebook.check_records import non_claim_views

    records = records_from_check(TEXT, RESULT, FP)
    found = claim_views(TEXT, records, "", compare_environment=False)
    assert [v["current"] for v in found] == [True, True]
    edited = TEXT.replace("def base : Nat := 1", "def base : Nat := 2")
    found = claim_views(edited, records, "", compare_environment=False)
    assert [v["current"] for v in found] == [False, False]
    result = {
        **RESULT,
        "theorems": [],
        "non_claims": [{"start_line": 2, "end_line": 2}],
    }
    saved = records_from_check(NON_CLAIMS, result, FP)
    assert non_claim_views(NON_CLAIMS, saved, GP, compare_environment=False) == [
        {"start_line": 2, "end_line": 2}
    ]
    entry = _entry()
    assert evidence_status(entry, NOTEBOOK, 3, GP, compare_environment=False) == (
        "current"
    )
    moved = NOTEBOOK.replace("def M := 1", "def M := 2")
    assert evidence_status(entry, moved, 3, GP, compare_environment=False) == "stale"
    assert (
        decision_status(_decision(), DECIDING, 4, GP, compare_environment=False)
        == "candidate"
    )


def test_the_environment_is_compared_by_default():
    records = records_from_check(TEXT, RESULT, FP)
    assert [v["current"] for v in claim_views(TEXT, records, GP)] == [False, False]
    assert evidence_status(_entry(), NOTEBOOK, 3, GP) == "stale"
    assert _decided(DECIDING, fingerprint=GP) == "stale"


@pytest.mark.parametrize("fingerprint", ["", "f", "F" * 64, "f" * 65])
def test_nothing_is_saved_under_a_fingerprint_that_is_not_a_digest(fingerprint):
    with pytest.raises(ValueError, match="fingerprint is unknown"):
        records_from_check(TEXT, RESULT, fingerprint)
    with pytest.raises(ValueError, match="fingerprint is unknown"):
        with_evidence(None, KEY, _entry(fingerprint=fingerprint), [KEY])
    decision = _decision(fingerprint=fingerprint)
    with pytest.raises(ValueError, match="fingerprint is unknown"):
        with_decision(None, "d", decision, ["d"])


# ----- saved drawings ----------------------------------------------------------

VIEW = "-- @view circuit M"
VIEWING = f"/-! # Notes -/\ndef M := 1\n{VIEW}\ndef after := 2\n"
VIEW_KEY = evidence_key(VIEW, 0)
PROVIDER_SHA = "a" * 64
DRAWING = {"svg": "<svg/>"}


def _view(text=VIEWING, line=3, fingerprint=FP, drawing=DRAWING):
    return {
        "prefix_sha256": evidence_prefix_sha256(text, line),
        "fingerprint": fingerprint,
        "provider": "demo",
        "provider_sha256": PROVIDER_SHA,
        "created": "2026-10-14T12:00:00Z",
        "drawing": drawing,
    }


def _drawing_status(text, line=3, fingerprint=FP, provider=PROVIDER_SHA, **options):
    return view_status(_view(), text, line, fingerprint, provider, **options)


def test_records_with_views_round_trip(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_view(
        records_from_check(TEXT, RESULT, FP), VIEW_KEY, _view(), [VIEW_KEY], notebook
    )
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert loaded == records
    assert saved_views(loaded) == {VIEW_KEY: _view()}
    assert [c["name"] for c in loaded["claims"]] == ["A.result", "B.result"]
    assert saved_views(None) == {}


@pytest.mark.parametrize(
    "views",
    [
        [],
        {VIEW_KEY: []},
        {VIEW_KEY: {**_view(), "extra": 1}},
        {VIEW_KEY: {k: v for k, v in _view().items() if k != "provider_sha256"}},
        {VIEW_KEY: {**_view(), "created": "yesterday"}},
        {VIEW_KEY: {**_view(), "provider_sha256": 1}},
        {VIEW_KEY: _view(drawing={"svg": "not svg"})},
        {VIEW_KEY: _view(drawing={"error": "the drawing is too large to show"})},
        {VIEW_KEY: _view(drawing={**DRAWING, "uncertain": True})},
        {VIEW_KEY: _view(drawing={**DRAWING, "proofs_skipped": True})},
        {VIEW_KEY: _view(drawing="<svg/>")},
    ],
)
def test_view_entry_with_invalid_drawing_makes_records_unknown(tmp_path, views):
    """As for evidence and decisions: one bad entry and the file is unreadable."""
    notebook = tmp_path / "N.lean"
    records = records_from_check(TEXT, RESULT, FP)
    records_path(notebook).write_text(json.dumps({**records, "views": views}))
    assert load_records(notebook) is None
    with pytest.raises(ValueError, match="cannot be read"):
        with_view(None, VIEW_KEY, _view(), [VIEW_KEY], notebook)


def test_records_without_views_still_load(tmp_path: Path):
    """Records written before drawings were saved (0.1.2) read unchanged."""
    notebook = tmp_path / "N.lean"
    records = with_evidence(records_from_check(TEXT, RESULT, FP), KEY, _entry(), [KEY])
    records_path(notebook).write_text(json.dumps(records))
    loaded = load_records(notebook)
    assert loaded == records and "views" not in loaded
    assert saved_views(loaded) == {}
    updated = with_view(loaded, VIEW_KEY, _view(), [VIEW_KEY], notebook)
    assert saved_evidence(updated) == {KEY: _entry()}
    assert updated["claims"] == records["claims"]


def test_with_view_prunes_missing_directives():
    other, gone = evidence_key("-- @view circuit N", 0), "gone"
    records = with_view(None, gone, _view(), [gone])
    records = with_view(records, other, _view(), [gone, other])
    records = with_view(records, VIEW_KEY, _view(), [other])
    assert set(saved_views(records)) == {other, VIEW_KEY}
    assert records["claims"] == []


@pytest.mark.parametrize("fingerprint", ["", "unknown", "F" * 64])
def test_with_view_refuses_unknown_fingerprint(fingerprint):
    with pytest.raises(ValueError, match="fingerprint is unknown"):
        with_view(None, VIEW_KEY, _view(fingerprint=fingerprint), [VIEW_KEY])


def test_with_view_refuses_a_malformed_entry():
    with pytest.raises(ValueError, match="malformed"):
        with_view(None, VIEW_KEY, _view(drawing={"svg": "x"}), [VIEW_KEY])
    with pytest.raises(ValueError, match="malformed"):
        with_view(None, VIEW_KEY, {**_view(), "provider": 3}, [VIEW_KEY])


def _large_drawing(seed: int) -> dict:
    """An SVG drawing just under the 1 MB a drawing may have."""
    return {"svg": f"<svg><!-- {seed} {'x' * 950_000} --></svg>"}


def test_with_view_refuses_oversized_section(tmp_path: Path):
    assert MAX_VIEWS_BYTES == 4 * 1024 * 1024
    notebook = tmp_path / "N.lean"
    keys = [f"k{n}" for n in range(6)]
    records = records_from_check(TEXT, RESULT, FP)
    saved = 0
    for key in keys:
        entry = _view(drawing=_large_drawing(saved))
        try:
            records = with_view(records, key, entry, keys, notebook)
        except ValueError as error:
            assert "N.glitnir.json" in str(error) and "4 MiB" in str(error)
            break
        save_records(notebook, records)
        saved += 1
    else:
        pytest.fail("the cap was never reached")
    assert saved == 4
    before = records_path(notebook).read_bytes()
    assert load_records(notebook) == records
    assert records_path(notebook).read_bytes() == before


def test_records_from_check_keeps_views():
    previous = with_view(None, VIEW_KEY, _view(), [VIEW_KEY])
    previous = with_evidence(previous, KEY, _entry(), [KEY])
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert saved_views(records) == {VIEW_KEY: _view()}
    assert saved_evidence(records) == {KEY: _entry()}
    assert "views" not in records_from_check(TEXT, RESULT, FP)


def _size(records: dict) -> int:
    return len(_serialised(records).encode())


def test_a_check_drops_saved_drawings_rather_than_its_results(monkeypatch):
    """Drawings are a cache: they never stop a check's results from saving."""
    previous = with_evidence(None, KEY, _entry(), [KEY])
    previous = with_view(previous, VIEW_KEY, _view(), [VIEW_KEY])
    without_views = records_from_check(
        TEXT, RESULT, FP, {k: v for k, v in previous.items() if k != "views"}
    )
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(without_views) + 10)
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert "views" not in records
    assert records == without_views
    assert saved_evidence(records) == {KEY: _entry()}
    assert [c["name"] for c in records["claims"]] == ["A.result", "B.result"]


def test_a_check_too_large_without_drawings_is_still_refused(monkeypatch):
    previous = with_evidence(None, KEY, _entry(), [KEY])
    previous = with_view(previous, VIEW_KEY, _view(), [VIEW_KEY])
    claims_only = _size(records_from_check(TEXT, RESULT, FP))
    monkeypatch.setattr(check_records, "_MAX_RECORDS", claims_only + 10)
    with pytest.raises(ValueError, match="nothing saved before was dropped"):
        records_from_check(TEXT, RESULT, FP, previous)


@pytest.mark.parametrize("section", ["evidence", "decisions"])
def test_saving_a_result_drops_saved_drawings_rather_than_fail(
    tmp_path: Path, monkeypatch, section
):
    notebook = tmp_path / "N.lean"
    checked = records_from_check(TEXT, RESULT, FP)
    with_drawing = with_view(checked, VIEW_KEY, _view(), [VIEW_KEY], notebook)

    def saved(records):
        if section == "evidence":
            return with_evidence(records, KEY, _entry(), [KEY], notebook)
        return with_decision(records, DECISION_KEY, _decision(), [], notebook)

    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(saved(checked)) + 10)
    records = saved(with_drawing)
    assert "views" not in records
    assert records == saved(checked)
    # Too large even without the drawings: refused, naming the section saved.
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(checked) + 10)
    with pytest.raises(ValueError, match=f"Saved {section} would make N.glitnir"):
        saved(with_drawing)


def test_a_drawing_never_drops_other_drawings_to_fit_the_file(
    tmp_path: Path, monkeypatch
):
    notebook = tmp_path / "N.lean"
    other = evidence_key("-- @view circuit other", 0)
    records = with_view(None, other, _view(), [other])
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(records) + 10)
    with pytest.raises(ValueError, match="Saved views would make N.glitnir.json"):
        with_view(records, VIEW_KEY, _view(), [other, VIEW_KEY], notebook)


def test_view_status_stale_when_provider_changes():
    assert _drawing_status(VIEWING) == "current"
    assert _drawing_status(VIEWING, provider="b" * 64) == "stale"
    assert _drawing_status(VIEWING, provider=None) == "stale"


def test_view_status_follows_the_text_through_the_directive_and_environment():
    assert _drawing_status(VIEWING + "more\n") == "current"
    assert _drawing_status(VIEWING.replace("def after", "def later")) == "current"
    assert _drawing_status(VIEWING.replace("def M := 1", "def M := 2")) == "stale"
    assert _drawing_status(VIEWING, fingerprint=GP) == "stale"
    assert _drawing_status(VIEWING, fingerprint="") == "stale"
    # As recorded: only the source and the provider are compared.
    assert _drawing_status(VIEWING, fingerprint="", compare_environment=False) == (
        "current"
    )
    assert (
        _drawing_status(
            VIEWING, fingerprint="", provider="b" * 64, compare_environment=False
        )
        == "stale"
    )


# ----- saved evidence failures --------------------------------------------------

OTHER_NOTE = "-- @evidence simulate N"
OTHER_KEY = evidence_key(OTHER_NOTE, 0)


def _failure(text=NOTEBOOK, line=3, fingerprint=FP, **changes):
    return {
        "prefix_sha256": evidence_prefix_sha256(text, line),
        "fingerprint": fingerprint,
        "provider": "demo",
        "created": "2026-10-15T12:00:00Z",
        "kind": "lean",
        "message": "Line 2: unknown identifier 'x'",
        **changes,
    }


def _write_raw(notebook: Path, records: dict) -> None:
    records_path(notebook).write_text(json.dumps(records))


def test_a_failure_round_trips_and_prunes_directives_that_are_gone(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_evidence_failure(None, "gone", _failure(), ["gone"])
    records = with_evidence_failure(records, OTHER_KEY, _failure(), ["gone", OTHER_KEY])
    records = with_evidence_failure(records, KEY, _failure(), [OTHER_KEY], notebook)
    assert set(saved_evidence_failures(records)) == {OTHER_KEY, KEY}
    assert records["claims"] == [] and "evidence" not in records
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert loaded == records
    assert saved_evidence_failures(loaded)[KEY] == _failure()


@pytest.mark.parametrize("kind", sorted(EVIDENCE_FAILURE_KINDS))
def test_each_failure_kind_is_saved(kind):
    records = with_evidence_failure(None, KEY, _failure(kind=kind), [KEY])
    assert saved_evidence_failures(records)[KEY]["kind"] == kind


def test_the_failure_kinds_are_those_a_run_saves():
    assert EVIDENCE_FAILURE_KINDS == {
        "lean",
        "provider",
        "refused",
        "invalid",
        "timeout",
    }


@pytest.mark.parametrize(
    "entry",
    [
        {**_failure(), "extra": 1},
        {k: v for k, v in _failure().items() if k != "message"},
        _failure(kind="cancelled"),
        _failure(kind=None),
        _failure(message=3),
        _failure(message="x" * (MAX_MESSAGE + 1)),
        _failure(created="yesterday"),
        _failure(provider=3),
        _failure(prefix_sha256=None),
    ],
)
def test_a_malformed_failure_is_refused(entry):
    with pytest.raises(ValueError, match="malformed"):
        with_evidence_failure(None, KEY, entry, [KEY])


def test_a_message_of_the_limit_is_saved():
    message = "x" * MAX_MESSAGE
    records = with_evidence_failure(None, KEY, _failure(message=message), [KEY])
    assert saved_evidence_failures(records)[KEY]["message"] == message


def test_failure_message_cuts_a_long_message_to_the_limit():
    assert failure_message("short") == "short"
    cut = failure_message("x" * (MAX_MESSAGE + 50))
    assert len(cut) == MAX_MESSAGE and cut.endswith("…")
    assert failure_message("x" * MAX_MESSAGE) == "x" * MAX_MESSAGE


@pytest.mark.parametrize("fingerprint", ["", "unknown", "F" * 64])
def test_a_failure_in_an_unknown_environment_is_never_saved(fingerprint):
    with pytest.raises(ValueError, match="unknown"):
        with_evidence_failure(None, KEY, _failure(fingerprint=fingerprint), [KEY])


def test_a_success_removes_the_failure_and_prunes_failures_of_gone_directives():
    records = with_evidence_failure(None, KEY, _failure(), [KEY, OTHER_KEY, "gone"])
    records = with_evidence_failure(records, OTHER_KEY, _failure(), [KEY, OTHER_KEY])
    records = with_evidence_failure(records, "gone", _failure(), [KEY, OTHER_KEY])
    records = with_evidence(records, KEY, _entry(), [OTHER_KEY])
    assert saved_evidence(records) == {KEY: _entry()}
    assert set(saved_evidence_failures(records)) == {OTHER_KEY}
    records = with_evidence(records, OTHER_KEY, _entry(), [KEY])
    assert "evidence_failures" not in records


def test_a_failure_after_a_success_keeps_both(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_evidence(None, KEY, _entry(), [KEY])
    later = _failure(created="2026-10-16T09:30:00+00:00")
    records = with_evidence_failure(records, KEY, later, [KEY], notebook)
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert saved_evidence(loaded) == {KEY: _entry()}
    assert saved_evidence_failures(loaded) == {KEY: later}


def test_a_check_carries_the_saved_failures_over():
    previous = with_evidence_failure(None, KEY, _failure(), [KEY])
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert saved_evidence_failures(records) == {KEY: _failure()}
    assert "evidence_failures" not in records_from_check(TEXT, RESULT, FP)


@pytest.mark.parametrize(
    "failures",
    [
        [],
        "broken",
        {KEY: []},
        {KEY: {**_failure(), "extra": 1}},
        {KEY: _failure(kind="from-a-later-version")},
        {KEY: _failure(message="x" * (MAX_MESSAGE + 1))},
        {KEY: _failure(created="2026-10-15")},
        {KEY: _failure(fingerprint=1)},
    ],
)
def test_a_malformed_failure_is_dropped_while_claims_and_evidence_load(
    tmp_path, failures
):
    notebook = tmp_path / "N.lean"
    records = with_evidence(records_from_check(TEXT, RESULT, FP), KEY, _entry(), [KEY])
    _write_raw(notebook, {**records, "evidence_failures": failures})
    loaded = load_records(notebook)
    assert loaded is not None
    assert [c["name"] for c in loaded["claims"]] == ["A.result", "B.result"]
    assert saved_evidence(loaded) == {KEY: _entry()}
    assert saved_evidence_failures(loaded) == {}
    assert "evidence_failures" not in loaded


def test_only_the_malformed_failures_are_dropped(tmp_path):
    notebook = tmp_path / "N.lean"
    failures = {KEY: _failure(), OTHER_KEY: _failure(kind="from-a-later-version")}
    _write_raw(
        notebook,
        {**with_evidence(None, KEY, _entry(), [KEY]), "evidence_failures": failures},
    )
    assert saved_evidence_failures(load_records(notebook)) == {KEY: _failure()}


def test_a_failure_older_than_the_saved_evidence_is_ignored_then_dropped(tmp_path):
    """An older Glitnir saved a success without removing the failure before it."""
    notebook = tmp_path / "N.lean"
    records = with_evidence(None, KEY, _entry(), [KEY, OTHER_KEY])  # 12:00 on 14th
    older = _failure(created="2026-10-14T11:59:59Z")
    tie = _failure(created="2026-10-14T12:00:00+00:00")
    _write_raw(
        notebook,
        {**records, "evidence_failures": {KEY: older, OTHER_KEY: _failure()}},
    )
    loaded = load_records(notebook)
    assert saved_evidence_failures(loaded) == {OTHER_KEY: _failure()}
    # Dropped at the next write, whatever that write saves.
    written = records_from_check(TEXT, RESULT, FP, loaded)
    assert saved_evidence_failures(written) == {OTHER_KEY: _failure()}
    # On a tie the failure counts as newer.
    _write_raw(notebook, {**records, "evidence_failures": {KEY: tie}})
    assert saved_evidence_failures(load_records(notebook)) == {KEY: tie}


def test_saved_failures_follow_the_evidence_currency_rule():
    entry = _failure()
    assert evidence_status(entry, NOTEBOOK, 3, FP) == "current"
    assert evidence_status(entry, NOTEBOOK + "more\n", 3, FP) == "current"
    assert evidence_status(
        entry, NOTEBOOK.replace("def M := 1", "def M := 2"), 3, FP
    ) == ("stale")
    assert evidence_status(entry, NOTEBOOK, 3, GP) == "stale"


def test_saving_drops_drawings_then_failures_before_refusing(monkeypatch):
    """Drawings go first, then saved failures, before anything is refused."""
    previous = with_evidence(None, KEY, _entry(), [KEY, OTHER_KEY])
    previous = with_evidence_failure(previous, OTHER_KEY, _failure(), [KEY, OTHER_KEY])
    previous = with_view(previous, VIEW_KEY, _view(), [VIEW_KEY])
    no_views = {k: v for k, v in previous.items() if k != "views"}
    neither = {k: v for k, v in no_views.items() if k != "evidence_failures"}
    monkeypatch.setattr(
        check_records,
        "_MAX_RECORDS",
        _size(records_from_check(TEXT, RESULT, FP, no_views)) + 10,
    )
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert "views" not in records
    assert saved_evidence_failures(records) == {OTHER_KEY: _failure()}
    monkeypatch.setattr(
        check_records,
        "_MAX_RECORDS",
        _size(records_from_check(TEXT, RESULT, FP, neither)) + 10,
    )
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert "views" not in records and "evidence_failures" not in records
    assert saved_evidence(records) == {KEY: _entry()}


def test_a_failure_that_does_not_fit_is_not_saved(tmp_path, monkeypatch):
    """Refused, never saved by dropping the other saved failures to fit it."""
    notebook = tmp_path / "N.lean"
    previous = with_evidence(None, KEY, _entry(), [KEY, OTHER_KEY])
    previous = with_evidence_failure(previous, OTHER_KEY, _failure(), [KEY, OTHER_KEY])
    long = _failure(message="x" * MAX_MESSAGE)
    # It fits only without the saved failures, which would then be lost.
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(previous) + 10)
    with pytest.raises(ValueError) as raised:
        with_evidence_failure(previous, KEY, long, [KEY, OTHER_KEY], notebook)
    assert str(raised.value) == (
        "Saved evidence failures would make the records file too large; "
        "the failure is shown but not saved."
    )
    # It does not fit even without them.
    evidence_only = {k: v for k, v in previous.items() if k != "evidence_failures"}
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(evidence_only) - 1)
    with pytest.raises(ValueError) as raised:
        with_evidence_failure(previous, KEY, long, [KEY, OTHER_KEY], notebook)
    assert str(raised.value) == (
        "Saved evidence_failures would make N.glitnir.json larger than 16 MiB; "
        "the failure is shown but not saved."
    )


def test_records_with_failures_keep_what_older_readers_need(tmp_path):
    """Glitnir 0.1.2 to 0.1.6 read these records only while this holds.

    They require the exact format, fail the whole file on an evidence entry of
    any other key set, and ignore top-level sections they do not know.
    """
    notebook = tmp_path / "N.lean"
    records = with_evidence(
        records_from_check(TEXT, RESULT, FP), KEY, _entry(), [KEY, OTHER_KEY]
    )
    records = with_evidence_failure(records, KEY, _failure(), [KEY, OTHER_KEY])
    records = with_evidence_failure(records, OTHER_KEY, _failure(), [KEY, OTHER_KEY])
    save_records(notebook, records)
    saved = json.loads(records_path(notebook).read_text())
    assert check_records.FORMAT == "glitnir.project-checks/v2"
    assert saved["format"] == "glitnir.project-checks/v2"
    assert [set(entry) for entry in saved["evidence"].values()] == [
        {"prefix_sha256", "fingerprint", "provider", "created", "snapshot"}
    ]
    assert set(saved["evidence_failures"]) == {KEY, OTHER_KEY}
    assert "evidence_failures" in check_records._RESULT_SECTIONS


# ----- saved decision failures --------------------------------------------------

OTHER_DECIDE = "-- @decide three : entailment |- 3 = 3"
OTHER_DECISION_KEY = decision_key(OTHER_DECIDE, 0)


def _decision_failure(text=DECIDING, end_line=4, fingerprint=FP, **changes):
    return {
        "prefix_sha256": decision_prefix_sha256(text, end_line),
        "fingerprint": fingerprint,
        "provider": "demo",
        "created": "2026-10-15T12:00:00Z",
        "kind": "refused",
        "message": "The goal has no variables.",
        **changes,
    }


def test_a_decision_failure_round_trips_and_prunes_directives_that_are_gone(
    tmp_path: Path,
):
    notebook = tmp_path / "N.lean"
    records = with_decision_failure(None, "gone", _decision_failure(), ["gone"])
    records = with_decision_failure(
        records, OTHER_DECISION_KEY, _decision_failure(), ["gone", OTHER_DECISION_KEY]
    )
    records = with_decision_failure(
        records, DECISION_KEY, _decision_failure(), [OTHER_DECISION_KEY], notebook
    )
    assert set(saved_decision_failures(records)) == {OTHER_DECISION_KEY, DECISION_KEY}
    assert records["claims"] == [] and "decisions" not in records
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert loaded == records
    assert saved_decision_failures(loaded)[DECISION_KEY] == _decision_failure()
    assert saved_evidence_failures(loaded) == {}


@pytest.mark.parametrize("kind", sorted(DECISION_FAILURE_KINDS))
def test_each_decision_failure_kind_is_saved(kind):
    records = with_decision_failure(
        None, DECISION_KEY, _decision_failure(kind=kind), [DECISION_KEY]
    )
    assert saved_decision_failures(records)[DECISION_KEY]["kind"] == kind


def test_the_decision_failure_kinds_are_those_a_run_saves():
    """A block the kernel rejects is a saved decision, so there is no `lean`."""
    assert DECISION_FAILURE_KINDS == {"provider", "refused", "invalid", "timeout"}


@pytest.mark.parametrize(
    "entry",
    [
        {**_decision_failure(), "extra": 1},
        {k: v for k, v in _decision_failure().items() if k != "message"},
        _decision_failure(kind="lean"),
        _decision_failure(kind="cancelled"),
        _decision_failure(message=3),
        _decision_failure(message="x" * (MAX_MESSAGE + 1)),
        _decision_failure(created="yesterday"),
        _decision_failure(provider=None),
    ],
)
def test_a_malformed_decision_failure_is_refused(entry):
    with pytest.raises(ValueError, match="malformed"):
        with_decision_failure(None, DECISION_KEY, entry, [DECISION_KEY])


@pytest.mark.parametrize("fingerprint", ["", "unknown", "F" * 64])
def test_a_decision_failure_in_an_unknown_environment_is_never_saved(fingerprint):
    with pytest.raises(ValueError, match="unknown"):
        with_decision_failure(
            None,
            DECISION_KEY,
            _decision_failure(fingerprint=fingerprint),
            [DECISION_KEY],
        )


def test_a_decision_removes_its_failure_and_prunes_failures_of_gone_directives():
    keep = [DECISION_KEY, OTHER_DECISION_KEY]
    records = with_decision_failure(
        None, DECISION_KEY, _decision_failure(), [*keep, "gone"]
    )
    records = with_decision_failure(
        records, OTHER_DECISION_KEY, _decision_failure(), keep
    )
    records = with_decision_failure(records, "gone", _decision_failure(), keep)
    records = with_evidence_failure(records, KEY, _failure(), [KEY])
    records = with_decision(records, DECISION_KEY, _decision(), [OTHER_DECISION_KEY])
    assert saved_decisions(records) == {DECISION_KEY: _decision()}
    assert set(saved_decision_failures(records)) == {OTHER_DECISION_KEY}
    # Evidence failures are another directive's business.
    assert saved_evidence_failures(records) == {KEY: _failure()}
    records = with_decision(records, OTHER_DECISION_KEY, _decision(), [DECISION_KEY])
    assert "decision_failures" not in records


def test_a_decision_failure_after_a_decision_keeps_both(tmp_path: Path):
    notebook = tmp_path / "N.lean"
    records = with_decision(None, DECISION_KEY, _decision(), [DECISION_KEY])
    later = _decision_failure(created="2026-10-16T09:30:00+00:00")
    records = with_decision_failure(
        records, DECISION_KEY, later, [DECISION_KEY], notebook
    )
    save_records(notebook, records)
    loaded = load_records(notebook)
    assert saved_decisions(loaded) == {DECISION_KEY: _decision()}
    assert saved_decision_failures(loaded) == {DECISION_KEY: later}
    # The candidate is still what it was.
    assert _decided(DECIDING, saved_decisions(loaded)[DECISION_KEY]) == "candidate"


def test_discarding_a_decision_removes_its_failure_too():
    keep = [DECISION_KEY, OTHER_DECISION_KEY]
    records = with_decision(None, DECISION_KEY, _decision(), keep)
    later = _decision_failure(created="2026-10-16T09:30:00+00:00")
    records = with_decision_failure(records, DECISION_KEY, later, keep)
    records = with_decision_failure(records, OTHER_DECISION_KEY, later, keep)
    removed = without_decision(records, DECISION_KEY)
    assert "decisions" not in removed
    assert saved_decision_failures(removed) == {OTHER_DECISION_KEY: later}
    only = with_decision_failure(None, DECISION_KEY, later, [DECISION_KEY])
    # A failure alone is not a saved decision: Discard's contract is unchanged.
    with pytest.raises(ValueError, match="No decision is saved"):
        without_decision(only, DECISION_KEY)


def test_a_check_carries_the_saved_decision_failures_over():
    previous = with_decision_failure(
        None, DECISION_KEY, _decision_failure(), [DECISION_KEY]
    )
    records = records_from_check(TEXT, RESULT, FP, previous)
    assert saved_decision_failures(records) == {DECISION_KEY: _decision_failure()}
    assert "decision_failures" not in records_from_check(TEXT, RESULT, FP)


@pytest.mark.parametrize(
    "failures",
    [
        [],
        "broken",
        {DECISION_KEY: []},
        {DECISION_KEY: {**_decision_failure(), "extra": 1}},
        {DECISION_KEY: _decision_failure(kind="from-a-later-version")},
        {DECISION_KEY: _decision_failure(kind="lean")},
        {DECISION_KEY: _decision_failure(message="x" * (MAX_MESSAGE + 1))},
        {DECISION_KEY: _decision_failure(created="2026-10-15")},
        {DECISION_KEY: _decision_failure(fingerprint=1)},
    ],
)
def test_a_malformed_decision_failure_is_dropped_while_everything_else_loads(
    tmp_path, failures
):
    notebook = tmp_path / "N.lean"
    records = with_evidence(records_from_check(TEXT, RESULT, FP), KEY, _entry(), [KEY])
    records = with_decision(records, DECISION_KEY, _decision(), [DECISION_KEY])
    _write_raw(notebook, {**records, "decision_failures": failures})
    loaded = load_records(notebook)
    assert loaded is not None
    assert [c["name"] for c in loaded["claims"]] == ["A.result", "B.result"]
    assert saved_evidence(loaded) == {KEY: _entry()}
    assert saved_decisions(loaded) == {DECISION_KEY: _decision()}
    assert saved_decision_failures(loaded) == {}
    assert "decision_failures" not in loaded


def test_a_decision_failure_older_than_the_saved_decision_is_ignored_then_dropped(
    tmp_path,
):
    """An older Glitnir saved a decision without removing the failure before it."""
    notebook = tmp_path / "N.lean"
    keep = [DECISION_KEY, OTHER_DECISION_KEY]
    records = with_decision(None, DECISION_KEY, _decision(), keep)  # 12:00 on 14th
    older = _decision_failure(created="2026-10-14T11:59:59Z")
    tie = _decision_failure(created="2026-10-14T12:00:00+00:00")
    _write_raw(
        notebook,
        {
            **records,
            "decision_failures": {
                DECISION_KEY: older,
                OTHER_DECISION_KEY: _decision_failure(),
            },
        },
    )
    loaded = load_records(notebook)
    assert saved_decision_failures(loaded) == {OTHER_DECISION_KEY: _decision_failure()}
    written = records_from_check(TEXT, RESULT, FP, loaded)
    assert saved_decision_failures(written) == {OTHER_DECISION_KEY: _decision_failure()}
    _write_raw(notebook, {**records, "decision_failures": {DECISION_KEY: tie}})
    assert saved_decision_failures(load_records(notebook)) == {DECISION_KEY: tie}


def test_saved_decision_failures_follow_the_decision_currency_rule():
    entry = _decision_failure()
    assert decision_failure_status(entry, DECIDING, 4, FP) == "current"
    # Below the directive's last line is not part of it.
    assert decision_failure_status(entry, DECIDING + "more\n", 4, FP) == "current"
    edited = DECIDING.replace("def M := 1", "def M := 2")
    assert decision_failure_status(entry, edited, 4, FP) == "stale"
    continued = DECIDING.replace(DECIDE_MORE, DECIDE_MORE + " and more")
    assert decision_failure_status(entry, continued, 4, FP) == "stale"
    assert decision_failure_status(entry, DECIDING, 4, GP) == "stale"
    # Even with a checked block below it: only a decision can be accepted.
    assert decision_failure_status(entry, _accepted(), 4, GP) == "stale"
    assert (
        decision_failure_status(entry, DECIDING, 4, "", compare_environment=False)
        == "current"
    )


def test_saving_drops_drawings_then_each_failure_section_before_refusing(
    monkeypatch,
):
    """Drawings go first, then evidence failures, then decision failures."""
    keep = [KEY, OTHER_KEY]
    previous = with_evidence(None, KEY, _entry(), keep)
    previous = with_evidence_failure(previous, OTHER_KEY, _failure(), keep)
    previous = with_decision_failure(
        previous, DECISION_KEY, _decision_failure(), [DECISION_KEY]
    )
    previous = with_view(previous, VIEW_KEY, _view(), [VIEW_KEY])
    no_views = {k: v for k, v in previous.items() if k != "views"}
    no_evidence_failures = {
        k: v for k, v in no_views.items() if k != "evidence_failures"
    }
    neither = {
        k: v for k, v in no_evidence_failures.items() if k != "decision_failures"
    }
    for fits, gone in (
        (no_views, {"views"}),
        (no_evidence_failures, {"views", "evidence_failures"}),
        (neither, {"views", "evidence_failures", "decision_failures"}),
    ):
        monkeypatch.setattr(
            check_records,
            "_MAX_RECORDS",
            _size(records_from_check(TEXT, RESULT, FP, fits)) + 10,
        )
        records = records_from_check(TEXT, RESULT, FP, previous)
        sections = {"views", "evidence_failures", "decision_failures"}
        assert sections - set(records) == gone
        assert saved_evidence(records) == {KEY: _entry()}


def test_a_failure_save_never_drops_the_other_failure_section(tmp_path, monkeypatch):
    """Either failure is refused rather than saved by losing the other section."""
    notebook = tmp_path / "N.lean"
    previous = with_evidence_failure(None, KEY, _failure(), [KEY])
    previous = with_decision_failure(
        previous, DECISION_KEY, _decision_failure(), [DECISION_KEY]
    )
    long = "x" * MAX_MESSAGE
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(previous) + 10)
    with pytest.raises(ValueError, match="shown but not saved"):
        with_decision_failure(
            previous,
            OTHER_DECISION_KEY,
            _decision_failure(message=long),
            [DECISION_KEY, OTHER_DECISION_KEY],
            notebook,
        )
    with pytest.raises(ValueError, match="shown but not saved"):
        with_evidence_failure(
            previous, OTHER_KEY, _failure(message=long), [KEY, OTHER_KEY], notebook
        )


def test_records_with_decision_failures_keep_what_older_readers_need(tmp_path):
    """Glitnir 0.1.2 to 0.1.8 read these records only while this holds.

    They require the exact format, fail the whole file on a decision entry of
    any other key set, and ignore top-level sections they do not know.
    """
    notebook = tmp_path / "N.lean"
    keep = [DECISION_KEY, OTHER_DECISION_KEY]
    records = with_decision(
        records_from_check(TEXT, RESULT, FP), DECISION_KEY, _decision(), keep
    )
    later = _decision_failure(created="2026-10-16T09:30:00+00:00")
    records = with_decision_failure(records, DECISION_KEY, later, keep)
    records = with_decision_failure(records, OTHER_DECISION_KEY, later, keep)
    save_records(notebook, records)
    saved = json.loads(records_path(notebook).read_text())
    assert saved["format"] == "glitnir.project-checks/v2"
    assert [set(entry) for entry in saved["decisions"].values()] == [
        {"prefix_sha256", "fingerprint", "provider", "created"}
        | {"outcome", "verification"}
    ]
    assert set(saved["decision_failures"]) == {DECISION_KEY, OTHER_DECISION_KEY}
    assert "decision_failures" in check_records._RESULT_SECTIONS


def test_a_decision_failure_save_never_drops_the_evidence_failures(monkeypatch):
    """Review I2: it would fit once the evidence failures were dropped: refused.

    `_fitted` drops `evidence_failures` before `decision_failures`, so only the
    guard against losing the other section stops this save.
    """
    keys = [evidence_key(f"-- @evidence simulate N{i}", 0) for i in range(20)]
    previous = None
    for key in keys:
        previous = with_evidence_failure(
            previous, key, _failure(message="x" * 1900), keys
        )
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(previous) + 50)
    with pytest.raises(ValueError) as raised:
        with_decision_failure(
            previous, DECISION_KEY, _decision_failure(), [DECISION_KEY]
        )
    assert str(raised.value) == (
        "Saved decision failures would make the records file too large; "
        "the failure is shown but not saved."
    )
    # A short one that fits beside them is saved, and keeps them.
    monkeypatch.setattr(check_records, "_MAX_RECORDS", _size(previous) + 1000)
    records = with_decision_failure(
        previous, DECISION_KEY, _decision_failure(), [DECISION_KEY]
    )
    assert set(saved_evidence_failures(records)) == set(keys)
    assert set(saved_decision_failures(records)) == {DECISION_KEY}


def test_records_schema_validates_what_load_records_accepts(tmp_path: Path) -> None:
    from glitnir.notebook import records_schema

    path = tmp_path / "A.lean"
    path.write_text(TEXT)
    records = records_from_check(TEXT, RESULT, FP)
    save_records(path, records)
    loaded = load_records(path)
    assert loaded is not None
    assert records_schema.well_formed(loaded)
    assert not records_schema.well_formed({**loaded, "claims": "not a list"})


def test_checked_type_survives_records_and_claim_views() -> None:
    result = copy.deepcopy(RESULT)
    result["theorems"][0]["type"] = "base = 1"
    records = records_from_check(TEXT, result, FP)
    assert records["claims"][0]["type"] == "base = 1"
    assert records["claims"][0]["type_sha256"] == check_records._sha("base = 1")
    assert claim_views(TEXT, records, FP)[0]["type"] == "base = 1"


def test_long_checked_type_keeps_exact_identity_digest() -> None:
    result = copy.deepcopy(RESULT)
    exact_type = "True ∧ " * 5_000 + "True"
    result["theorems"][0]["type"] = exact_type
    records = records_from_check(TEXT, result, FP)
    claim = records["claims"][0]
    assert claim["type"].endswith("…")
    assert claim["type_shortened"] is True
    assert claim["type_sha256"] == check_records._sha(exact_type)


def test_dropping_sections_never_modifies_the_argument(monkeypatch):
    """The helpers that drop a section return new records and leave theirs alone."""
    from glitnir.notebook.records_schema import with_lenient_failures

    records = with_decision(None, DECISION_KEY, _decision(), [DECISION_KEY])
    records = with_view(records, "view", _view(), ["view"])
    records = with_evidence_failure(records, KEY, _failure(), [KEY])
    records = with_decision_failure(records, "failed", _decision_failure(), ["failed"])
    sections = {"decisions", "views", "evidence_failures", "decision_failures"}
    assert sections <= set(records)
    before = copy.deepcopy(records)
    with monkeypatch.context() as patched:
        patched.setattr(check_records, "_MAX_RECORDS", 10)
        assert "views" not in check_records._fitted(records)
    assert "decisions" not in without_decision(records, DECISION_KEY)
    assert "evidence_failures" not in check_records._without_failure(
        records, "evidence_failures", KEY, []
    )
    assert with_lenient_failures(records) == records
    assert records == before


def test_records_from_check_accepts_theorems_without_optional_keys():
    """Theorems with only the required keys save claims without the optional ones."""
    records = records_from_check(NOTEBOOK, _result(("A.t", 2)), FP)
    (claim,) = records["claims"]
    optional = {
        "uses",
        "uses_truncated",
        "example",
        "statement",
        "kernel_statement",
        "type",
        "type_sha256",
        "type_shortened",
    }
    assert not optional & set(claim)


def test_named_statement_expansion_survives_records_and_freshness():
    """Display data follows its saved claim, including historical observations."""
    result = copy.deepcopy(RESULT)
    expansion = {"text": "∀ n : Nat, n = n", "kind": "definition", "truncated": False}
    result["theorems"][0]["statement_expansion"] = expansion
    records = records_from_check(TEXT, result, FP)
    assert records["claims"][0]["statement_expansion"] == expansion
    views = claim_views(TEXT, records, "different")
    assert views[0]["statement_expansion"] == expansion
    assert views[0]["current"] is False
    assert "statement_expansion" not in views[1]


@pytest.mark.parametrize(
    "expansion",
    [
        None,
        {"text": "x", "kind": "definition"},
        {"text": 1, "kind": "definition", "truncated": False},
        {"text": "x" * 4097, "kind": "definition", "truncated": False},
        {"text": "x", "kind": "proof", "truncated": False},
        {"text": "x", "kind": "definition", "truncated": 1},
    ],
)
def test_malformed_statement_displays_are_rejected(expansion):
    """Untrusted saved display fields obey the same bound and shape as reports."""
    records = records_from_check(TEXT, RESULT, FP)
    records["claims"][0]["statement_expansion"] = expansion
    assert not check_records.well_formed(records)
