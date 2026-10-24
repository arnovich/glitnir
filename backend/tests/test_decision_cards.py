"""Decision directives become preview cards; listing them never decides anything."""

import hashlib
from pathlib import Path

import pytest

from glitnir.notebook import decision_cards as decision_module
from glitnir.notebook.decision_cards import (
    BAD_NAME,
    INCOMPLETE,
    decision_cards,
    decision_directives,
    decision_key,
)
from glitnir.notebook.source_preview import SourcePreviewService
from glitnir.providers import DecisionProvider, DecisionRequest, Providers
from glitnir.providers.outcomes import checked_decision

# Environment fingerprints are SHA-256 digests; any other string matches nothing.
FP = "f" * 64

HEAD = "-- @decide band : entailment x : x > 0"
MORE = "--   |- x >= 0"


def _cells(text: str) -> list[dict]:
    """One source cell holding all of `text`."""
    return [
        {
            "index": 0,
            "kind": "source",
            "start": 1,
            "end": text.count("\n") + 1,
            "text": text,
        }
    ]


def _found(text: str) -> list[dict]:
    return decision_directives(_cells(text), text)


def _provider(calls=None, **changes) -> DecisionProvider:
    def parse(name: str, kind: str, goal: str) -> DecisionRequest:
        if calls is not None:
            calls.append((name, kind, goal))
        if "bad" in goal:
            raise ValueError("Goals are written `entailment <context> |- <goal>`.")
        return DecisionRequest(name, kind, goal, {"text": goal})

    fields = {
        "name": "demo",
        "kinds": frozenset({"entailment"}),
        "imports": ("Demo.Decide",),
        "parse": parse,
        "decide": lambda request, cancel: {},
        "unavailable": lambda: None,
        **changes,
    }
    return DecisionProvider(**fields)


def _loaded(provider: DecisionProvider | None = None, errors=None) -> Providers:
    decisions = {} if provider is None else {"entailment": provider}
    return Providers({}, {}, decisions, errors or {})


def _cards(text: str, providers: Providers) -> list[dict]:
    return decision_cards(_found(text), providers)


def test_a_directive_and_its_continuations_are_one_entry():
    text = f"def x := 1\n{HEAD}\n{MORE}\n--   and more  \ndef y := 2\n"
    [entry] = _found(text)
    assert entry == {
        "cell": 0,
        "line": 2,
        "end_line": 4,
        "name": "band",
        "kind": "entailment",
        "goal": "entailment x : x > 0 |- x >= 0 and more",
        "key": decision_key(f"{HEAD}\n{MORE}\n--   and more", 0),
        "error": "",
    }


def test_a_directive_without_continuations_ends_on_its_own_line():
    [entry] = _found(f"{HEAD}\n-- a plain comment\n{MORE}\n")
    assert (entry["line"], entry["end_line"]) == (1, 1)


@pytest.mark.parametrize(
    "following",
    [
        "-- one space is a plain comment",
        "--   @evidence simulate M",
        "--    ",
        "  --   indented",
        "",
        "def y := 2",
    ],
)
def test_only_indented_comment_text_continues_a_goal(following):
    [entry] = _found(f"{HEAD}\n{following}\n{MORE}\n")
    assert entry["end_line"] == 1 and entry["goal"] == "entailment x : x > 0"


def test_the_goal_may_start_on_a_continuation_line():
    [entry] = _found(f"-- @decide band :\n--   entailment x : x > 0\n{MORE}\n")
    assert (entry["name"], entry["kind"], entry["error"]) == ("band", "entailment", "")
    assert entry["goal"] == "entailment x : x > 0 |- x >= 0"
    assert entry["end_line"] == 3


@pytest.mark.parametrize(
    "text",
    [
        f"/- a block comment\n{HEAD}\n-/\ndef M := 1\n",
        f"/-- a doc comment\n{HEAD}\n-/\ndef M := 1\n",
        f'def s := "a string\n{HEAD}\n"\n',
        f"def M := 1 {HEAD}\n",
        "-- @decidex band : entailment x\n",
        f"/- never closed\n{HEAD}\n",
    ],
)
def test_text_that_lean_does_not_read_as_a_comment_line_is_no_directive(text):
    assert _found(text) == []


def test_directives_in_prose_cells_are_not_listed():
    text = f"/-!\n{HEAD}\n-/\n{HEAD}\n"
    cells = [
        {"index": 0, "kind": "prose", "start": 1, "end": 3, "text": ""},
        {"index": 1, "kind": "source", "start": 4, "end": 5, "text": ""},
    ]
    found = decision_directives(cells, text)
    assert [(entry["cell"], entry["line"]) for entry in found] == [(1, 4)]


def test_a_goal_does_not_continue_into_the_next_cell():
    text = f"{HEAD}\n{MORE}\n"
    cells = [
        {"index": 0, "kind": "source", "start": 1, "end": 1, "text": ""},
        {"index": 1, "kind": "source", "start": 2, "end": 3, "text": ""},
    ]
    [entry] = decision_directives(cells, text)
    assert (entry["line"], entry["end_line"]) == (1, 1)


def test_incomplete_indented_and_misnamed_directives_are_errors():
    text = (
        "-- @decide\n"
        "-- @decide band entailment x\n"
        "-- @decide band :\n"
        "  -- @decide indented : entailment x\n"
        "-- @decide 1band : entailment x\n"
        "-- @decide N.band : entailment x\n"
        "-- @decide band! : entailment x\n"
    )
    found = _found(text)
    assert [(e["name"], e["kind"], e["error"]) for e in found] == [
        ("", "", INCOMPLETE),
        ("band", "", INCOMPLETE),
        ("band", "", INCOMPLETE),
        ("indented", "entailment", INCOMPLETE),
        ("1band", "entailment", BAD_NAME),
        ("N.band", "entailment", BAD_NAME),
        ("band!", "entailment", BAD_NAME),
    ]


def test_a_repeated_name_is_an_error_on_the_later_directive():
    text = (
        "-- @decide band\n"  # in error: does not take the name
        f"{HEAD}\n"
        "-- @decide band : entailment y : y > 0\n"
        "-- @decide other : entailment y : y > 0\n"
    )
    found = _found(text)
    assert [entry["error"] for entry in found] == [
        INCOMPLETE,
        "",
        "Another `@decide` directive above already names `band`.",
        "",
    ]


def test_identical_directives_get_separate_keys_by_occurrence():
    text = f"{HEAD}\n{MORE}\ndef y := 1\n{HEAD}   \n{MORE}\n{HEAD}\n"
    first, second, third = _found(text)
    assert first["key"] == decision_key(f"{HEAD}\n{MORE}", 0)
    assert second["key"] == decision_key(f"{HEAD}\n{MORE}", 1)
    assert third["key"] == decision_key(HEAD, 0)
    expected = hashlib.sha256(f"{HEAD}\n{MORE}\n1".encode()).hexdigest()
    assert decision_key(f"{HEAD} \t\n{MORE}  ", 1) == expected


def test_cards_pass_the_name_kind_and_goal_to_the_provider():
    calls: list = []
    [card] = _cards(f"{HEAD}\n{MORE}\n", _loaded(_provider(calls)))
    assert calls == [("band", "entailment", "entailment x : x > 0 |- x >= 0")]
    assert card == {
        "cell": 0,
        "line": 1,
        "end_line": 2,
        "name": "band",
        "kind": "entailment",
        "goal": "entailment x : x > 0 |- x >= 0",
        "key": decision_key(f"{HEAD}\n{MORE}", 0),
        "provider": "demo",
        "error": "",
        "unavailable": None,
        "missing_imports": [],
        "status": "never",
        "run_error": None,
        "outcome": None,
        "verification": None,
    }


def test_a_rejected_goal_shows_the_provider_message():
    [card] = _cards("-- @decide band : entailment bad\n", _loaded(_provider()))
    assert card["error"] == "Goals are written `entailment <context> |- <goal>`."
    assert card["provider"] == "demo"


def test_a_rejection_without_a_message_still_says_why():
    [card] = _cards(f"{HEAD}\n", _loaded(_provider(parse=_raises(ValueError()))))
    assert card["error"] == "The provider rejected this directive."


def test_only_lines_after_a_head_are_checked_as_continuations(monkeypatch):
    """Indented comments elsewhere never reach the (possibly slow) lexer check."""
    asked: list[list[int]] = []
    real = decision_module.directive_lines

    def recording(text, candidates):
        asked.append(list(candidates))
        return real(text, candidates)

    monkeypatch.setattr(decision_module, "directive_lines", recording)
    text = f"--   note\n{MORE}\n{HEAD}\n{MORE}\n{MORE}\ndef y := 1\n{MORE}\n"
    [entry] = _found(text)
    assert asked == [[2, 3, 4]]
    assert (entry["line"], entry["end_line"]) == (3, 5)


def test_a_missing_dependency_is_reported_as_unavailable():
    provider = _provider(unavailable=lambda: "Install the z3 extra.")
    [card] = _cards(f"{HEAD}\n", _loaded(provider))
    assert card["unavailable"] == "Install the z3 extra."
    assert card["error"] == ""


def test_a_kind_without_a_provider_names_failed_providers():
    [card] = _cards(f"{HEAD}\n", _loaded(errors={"broken": "ImportError: x"}))
    assert card["provider"] is None and card["unavailable"] is None
    assert card["error"] == (
        "No installed provider decides `entailment` goals."
        " (Provider broken failed to load: ImportError: x)"
    )


def test_a_directive_error_is_kept_and_never_parsed():
    calls: list = []
    text = f"{HEAD}\n-- @decide band : entailment y\n-- @decide b! : entailment\n"
    cards = _cards(text, _loaded(_provider(calls)))
    assert [card["error"] for card in cards] == [
        "",
        "Another `@decide` directive above already names `band`.",
        BAD_NAME,
    ]
    assert [call[0] for call in calls] == ["band"]


def _raises(error: BaseException):
    def call(*_arguments):
        raise error

    return call


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"parse": _raises(RuntimeError("boom"))}, "RuntimeError: boom"),
        ({"parse": _raises(SystemExit(3))}, "SystemExit: 3"),
        ({"unavailable": _raises(KeyError("x"))}, "KeyError: 'x'"),
    ],
)
def test_provider_failures_become_card_errors(changes, message):
    [card] = _cards(f"{HEAD}\n", _loaded(_provider(**changes)))
    assert card["error"] == f"Provider demo failed: {message}"


def test_a_keyboard_interrupt_in_a_provider_propagates():
    provider = _provider(parse=_raises(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _cards(f"{HEAD}\n", _loaded(provider))


def test_unreadable_provider_answers_become_card_errors():
    provider = _provider(parse=lambda *_: {"name": "band"})
    [card] = _cards(f"{HEAD}\n", _loaded(provider))
    assert card["error"] == "Provider demo returned an unreadable request."
    provider = _provider(unavailable=lambda: 3)
    [card] = _cards(f"{HEAD}\n", _loaded(provider))
    assert card["error"] == "Provider demo reported an unreadable availability."


def test_snapshot_lists_decisions_without_processes_or_deciding(
    tmp_path: Path, monkeypatch
):
    import subprocess

    from glitnir.notebook import source_preview

    def forbidden(*args, **kwargs):
        pytest.fail("snapshot executed a process")

    def decide(request, cancel):
        pytest.fail("snapshot decided a goal")

    provider = _provider(decide=decide, kinds=frozenset({"entailment", "stream"}))
    loaded = Providers({}, {}, {"stream": provider, "entailment": provider}, {})
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(source_preview, "load_providers", lambda: loaded)
    monkeypatch.setattr(source_preview, "run_provider_lean", forbidden)
    path = tmp_path / "A.lean"
    path.write_text(f"def x := 1\n\n{HEAD}\n{MORE}\n-- @decide t : trajectory m\n")
    snapshot = SourcePreviewService(path).snapshot()
    assert snapshot["directive_providers"] == {
        "evidence": [],
        "decide": ["entailment", "stream"],
    }
    found, missing = snapshot["decisions"]
    assert (found["line"], found["end_line"], found["status"]) == (3, 4, "never")
    assert (found["provider"], found["error"]) == ("demo", "")
    cell = snapshot["cells"][found["cell"]]
    assert cell["start"] <= 3 <= cell["end"]
    assert missing["provider"] is None
    assert missing["error"] == "No installed provider decides `trajectory` goals."


def test_a_decision_directive_alone_loads_providers(tmp_path: Path, monkeypatch):
    from glitnir.notebook import source_preview

    loads: list[int] = []
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: loads.append(1) or Providers({}, {}, {}, {}),
    )
    path = tmp_path / "A.lean"
    path.write_text(f"{HEAD}\n")
    snapshot = SourcePreviewService(path).snapshot()
    assert loads == [1]
    assert snapshot["directive_providers"] == {"evidence": [], "decide": []}
    [card] = snapshot["decisions"]
    assert card["error"] == "No installed provider decides `entailment` goals."


# ----- provider outcomes -------------------------------------------------------

BLOCK = "namespace Decided_band\ntheorem band : True := trivial\nend Decided_band"


def _outcome(**changes):
    outcome = {
        "status": "proven",
        "reason": "trivial",
        "stages": [["read", "ok", "x > 0"], ["decide", "proven", ""]],
        "block": BLOCK,
        "theorem": "band",
    }
    return {**outcome, **changes}


def test_a_proven_outcome_is_returned_as_a_new_normal_copy():
    outcome = _outcome()
    checked = checked_decision(outcome)
    assert checked == outcome
    assert checked is not outcome and checked["stages"] is not outcome["stages"]
    assert checked["stages"][0] is not outcome["stages"][0]


def test_optional_fields_take_their_empty_values():
    assert checked_decision({"status": "unknown"}) == {
        "status": "unknown",
        "reason": "",
        "stages": [],
        "block": None,
        "theorem": None,
    }
    refuted = {"status": "refuted", "block": BLOCK, "theorem": "band_refuted"}
    assert checked_decision(refuted) == {**refuted, "reason": "", "stages": []}
    unknown = {"status": "unknown", "block": None, "theorem": None}
    assert checked_decision(unknown)["block"] is None


@pytest.mark.parametrize(
    "theorem", ["band", "Decided_band.band", "A.b.c'", "_x", "band_refuted"]
)
def test_qualified_lean_identifiers_name_the_theorem(theorem):
    assert checked_decision(_outcome(theorem=theorem))["theorem"] == theorem


def test_blocks_are_measured_in_utf8_bytes_and_kept_exactly():
    exact = "x" * 200_000
    assert checked_decision(_outcome(block=exact))["block"] == exact
    spaced = f"\n  {BLOCK}\n\n  -- a later comment"
    assert checked_decision(_outcome(block=spaced))["block"] == spaced
    with pytest.raises(ValueError, match="200000 bytes"):
        checked_decision(_outcome(block="\u00ac" * 100_001))


@pytest.mark.parametrize(
    "block, message",
    [
        # Inserted after the directive, these would continue its goal.
        (f"-- proof\n{BLOCK}", "may not start with a comment line"),
        (f"  --   proof\n{BLOCK}", "may not start with a comment line"),
        (f"--\n{BLOCK}", "may not start with a comment line"),
        # Editors may trim these on save, so the saved block would differ.
        (f"{BLOCK}\n", "may not end with a newline"),
        (f"{BLOCK}\n\n", "may not end with a newline"),
        (BLOCK.replace("trivial", "trivial "), "may not end a line with whitespace"),
        (f"{BLOCK}\t", "may not end a line with whitespace"),
        (f"{BLOCK}\n  \nend", "may not end a line with whitespace"),
    ],
)
def test_blocks_an_editor_or_the_scan_would_change_are_refused(block, message):
    with pytest.raises(ValueError, match=message):
        checked_decision(_outcome(block=block))


@pytest.mark.parametrize(
    "block, line",
    [
        (f"{BLOCK}\n-- @decide other : entailment x : x > 0", 4),
        (f"{BLOCK}\n  --@evidence simulate x", 4),
        (f"namespace N\n--   @view circuit c\n{BLOCK}", 2),
        ("theorem t : True := trivial\n-- @", 2),
    ],
)
def test_blocks_with_directive_comments_are_refused(block, line):
    """Once accepted, such a line would be read as a new directive."""
    message = f"^The block may not contain directive comments \\(line {line}\\).$"
    with pytest.raises(ValueError, match=message):
        checked_decision(_outcome(block=block))


def test_comments_that_are_not_directives_are_kept():
    block = f"{BLOCK}\n-- see @band\ntheorem t : True := trivial -- @decide"
    assert checked_decision(_outcome(block=block))["block"] == block


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        [],
        "proven",
        _outcome(extra=1),
        {**_outcome(), 3: "x"},
        _outcome(status="Proven"),
        _outcome(status=True),
        _outcome(status=None),
        {k: v for k, v in _outcome().items() if k != "status"},
        _outcome(reason=None),
        _outcome(reason=False),
        _outcome(reason="x" * 201),
        _outcome(stages=None),
        _outcome(stages="read ok"),
        _outcome(stages=[["read", "ok"]]),
        _outcome(stages=[("read", "ok", "x")]),
        _outcome(stages=[["read", "ok", None]]),
        _outcome(stages=[["read", True, "x"]]),
        _outcome(stages=[["read", "ok", "x" * 501]]),
        _outcome(stages=[["s", "o", "d"]] * 33),
        {k: v for k, v in _outcome().items() if k != "block"},
        _outcome(block=None),
        _outcome(block=""),
        _outcome(block=" \n\t"),
        _outcome(block=["theorem band : True := trivial"]),
        _outcome(block=BLOCK.replace("\n", "\r\n")),
        _outcome(block=f"{BLOCK}\x00"),
        _outcome(block="\ud800"),
        _outcome(block="x" * 200_001),
        {k: v for k, v in _outcome().items() if k != "theorem"},
        _outcome(theorem=None),
        _outcome(theorem=True),
        _outcome(theorem=""),
        _outcome(theorem="1band"),
        _outcome(theorem="band.."),
        _outcome(theorem=".band"),
        _outcome(theorem="band x"),
        _outcome(theorem="band\n"),
        _outcome(theorem="«band»"),
        _outcome(theorem="b" * 201),
        _outcome(status="unknown"),
        _outcome(status="unknown", block=None),
        _outcome(status="unknown", theorem=None),
        {"status": "unknown", "block": ""},
    ],
)
def test_malformed_outcomes_are_refused(outcome):
    with pytest.raises(ValueError) as error:
        checked_decision(outcome)
    assert str(error.value)


def test_limits_are_inclusive():
    outcome = _outcome(
        reason="r" * 200,
        stages=[["s" * 500, "o" * 500, "d" * 500]] * 32,
        theorem="t" * 200,
    )
    assert checked_decision(outcome) == outcome


# ----- saved decisions in the snapshot -----------------------------------------

DECIDING = f"def x := 1\n\n{HEAD}\n{MORE}\ndef after := 2\n"


def _saved_decision(path: Path, verification=None, **changes):
    from glitnir.notebook.check_records import (
        decision_prefix_sha256,
        save_records,
        with_decision,
    )

    outcome = checked_decision(_outcome(**changes))
    if verification is None and outcome["status"] != "unknown":
        verification = {
            "status": "checked",
            "message": "The checker reports `Decided_band.band` as checked.",
            "axioms": [],
            "type": "True",
        }
    entry = {
        "prefix_sha256": decision_prefix_sha256(DECIDING, 4),
        "fingerprint": FP,
        "provider": "demo",
        "created": "2026-10-14T12:00:00+00:00",
        "outcome": outcome,
        "verification": verification,
    }
    key = decision_key(f"{HEAD}\n{MORE}", 0)
    save_records(path, with_decision(None, key, entry, [key], path))
    return entry


def _decision_snapshot(tmp_path, monkeypatch, fingerprints=None):
    from glitnir.notebook import source_preview

    def fingerprint(*args):
        if fingerprints is not None:
            fingerprints.append(1)
        return {"sha256": FP}

    monkeypatch.setattr(source_preview, "load_providers", lambda: _loaded(_provider()))
    monkeypatch.setattr(source_preview, "environment_fingerprint", fingerprint)
    path = tmp_path / "A.lean"
    path.write_text(DECIDING)
    return path, SourcePreviewService(path)


def _status(service) -> str:
    [card] = service.snapshot()["decisions"]
    return card["status"]


def test_the_snapshot_shows_a_saved_candidate_and_its_verification(
    tmp_path, monkeypatch
):
    fingerprints: list[int] = []
    path, service = _decision_snapshot(tmp_path, monkeypatch, fingerprints)
    assert _status(service) == "never" and fingerprints == []
    entry = _saved_decision(path)
    [card] = service.snapshot()["decisions"]
    assert card["status"] == "candidate"
    assert card["outcome"] == entry["outcome"]
    assert card["verification"] == entry["verification"]
    assert (card["run_error"], card["error"]) == (None, "")


@pytest.mark.parametrize(
    "changes, status",
    [
        ({"status": "unknown", "block": None, "theorem": None}, "unknown"),
        ({}, "rejected"),
    ],
)
def test_the_snapshot_shows_unknown_and_rejected_outcomes(
    tmp_path, monkeypatch, changes, status
):
    path, service = _decision_snapshot(tmp_path, monkeypatch)
    rejected = {
        "status": "rejected",
        "message": "`band` is admitted: its proof depends on `sorry`.",
        "axioms": [],
        "type": None,
    }
    _saved_decision(path, rejected if status == "rejected" else None, **changes)
    assert _status(service) == status


def test_accepting_and_editing_around_a_decision(tmp_path, monkeypatch):
    """Review Focus 4, through the snapshot.

    An accepted block stays accepted while it is in the file; deleting it
    shows the saved candidate again, or stale once the text above changed.
    """
    path, service = _decision_snapshot(tmp_path, monkeypatch)
    _saved_decision(path)
    accepted = DECIDING.replace(f"{MORE}\n", f"{MORE}\n{BLOCK}\n")
    path.write_text(accepted)
    assert _status(service) == "accepted"
    path.write_text(DECIDING)
    assert _status(service) == "candidate"
    path.write_text(accepted.replace("def x := 1", "def x := 2"))
    assert _status(service) == "accepted"
    path.write_text(DECIDING.replace("def x := 1", "def x := 2"))
    assert _status(service) == "stale"


def test_a_failing_fingerprint_shows_saved_decisions_as_stale(tmp_path, monkeypatch):
    from glitnir.notebook import source_preview

    path, service = _decision_snapshot(tmp_path, monkeypatch)
    _saved_decision(path)

    def boom(*args):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(source_preview, "environment_fingerprint", boom)
    assert _status(service) == "stale"


def test_a_corrupt_records_file_leaves_decision_cards_unrun(tmp_path, monkeypatch):
    from glitnir.notebook.check_records import records_path

    path, service = _decision_snapshot(tmp_path, monkeypatch)
    records_path(path).write_text('{"format": "glitnir.project-checks/v1"}')
    [card] = service.snapshot()["decisions"]
    assert (card["status"], card["outcome"], card["verification"]) == (
        "never",
        None,
        None,
    )
