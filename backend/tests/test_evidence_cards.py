"""Evidence directives become preview cards; listing them never runs anything."""

import hashlib
from pathlib import Path

import pytest

from glitnir.notebook.check_records import (
    evidence_prefix_sha256,
    records_path,
    save_records,
    with_evidence,
)
from glitnir.notebook.directives import (
    BAD_TARGET,
    INCOMPLETE_EVIDENCE,
    evidence_directives,
    evidence_key,
)
from glitnir.notebook.source_preview import SourcePreviewService
from glitnir.preview import snapshot as preview_snapshot
from glitnir.preview.cards import evidence_cards
from glitnir.providers import EvidenceProvider, EvidenceRequest, Providers
from glitnir.providers.outcomes import checked_evidence

# Environment fingerprints are SHA-256 digests; any other string matches nothing.
FP = "f" * 64
GP = "9" * 64

DIRECTIVE = "-- @evidence simulate M (h := 1/100)"


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


def _lines(text: str) -> list[int]:
    return [entry["line"] for entry in evidence_directives(_cells(text), text)]


def _provider(calls=None, **changes) -> EvidenceProvider:
    def parse(verb: str, target: str, options: str) -> EvidenceRequest:
        if calls is not None:
            calls.append((verb, target, options))
        if "bad" in options:
            raise ValueError("Options are written (h := 1/100).")
        return EvidenceRequest(verb, target, {"text": options})

    fields = {
        "name": "demo",
        "verbs": frozenset({"simulate"}),
        "imports": ("Demo.Evidence",),
        "preamble": "",
        "marker": "demoEvidence:",
        "parse": parse,
        "command": lambda request: "",
        "compute": lambda request, payload: {},
        "unavailable": lambda: None,
        **changes,
    }
    return EvidenceProvider(**fields)


def _loaded(provider: EvidenceProvider | None = None, errors=None) -> Providers:
    evidence = {} if provider is None else {"simulate": provider}
    return Providers({}, evidence, {}, errors or {})


def _cards(text: str, providers: Providers) -> list[dict]:
    return evidence_cards(evidence_directives(_cells(text), text), providers)


def test_directives_on_standalone_comment_lines_are_found():
    text = f"def M := 1\n{DIRECTIVE}\ndef x := 2\n"
    [entry] = evidence_directives(_cells(text), text)
    assert entry == {
        "cell": 0,
        "line": 2,
        "verb": "simulate",
        "target": "M",
        "options": "(h := 1/100)",
        "key": evidence_key(DIRECTIVE, 0),
        "error": "",
    }


@pytest.mark.parametrize(
    "text",
    [
        f"/- a block comment\n{DIRECTIVE}\n-/\ndef M := 1\n",
        f"/-- a doc comment\n{DIRECTIVE}\n-/\ndef M := 1\n",
        f'def s := "a string\n{DIRECTIVE}\n"\n',
        f"def M := 1 {DIRECTIVE}\n",
        "-- @evidencex simulate M\n",
        f"/- never closed\n{DIRECTIVE}\n",
    ],
)
def test_text_that_lean_does_not_read_as_a_comment_line_is_no_directive(text):
    assert _lines(text) == []


def test_an_unsure_line_is_still_read_as_a_directive():
    # A string holding `{` may be interpolated: the scan cannot settle.
    text = f'def s := s!"{{1}}"\n{DIRECTIVE}\n'
    assert _lines(text) == [2]


def test_directives_in_prose_cells_are_not_listed():
    text = f"/-!\n{DIRECTIVE}\n-/\n{DIRECTIVE}\n"
    cells = [
        {"index": 0, "kind": "prose", "start": 1, "end": 3, "text": ""},
        {"index": 1, "kind": "source", "start": 4, "end": 5, "text": ""},
    ]
    found = evidence_directives(cells, text)
    assert [(entry["cell"], entry["line"]) for entry in found] == [(1, 4)]


def test_incomplete_indented_and_misnamed_directives_are_errors():
    text = (
        "-- @evidence\n"
        "-- @evidence simulate\n"
        "  -- @evidence simulate M\n"
        "-- @evidence simulate M! (h := 1)\n"
    )
    found = evidence_directives(_cells(text), text)
    assert [(e["verb"], e["target"], e["error"]) for e in found] == [
        ("", "", INCOMPLETE_EVIDENCE),
        ("simulate", "", INCOMPLETE_EVIDENCE),
        ("simulate", "M", INCOMPLETE_EVIDENCE),
        ("simulate", "M!", BAD_TARGET),
    ]


def test_qualified_targets_and_no_options_are_accepted():
    text = "-- @evidence iterate N.m'  \n"
    [entry] = evidence_directives(_cells(text), text)
    assert (entry["target"], entry["options"], entry["error"]) == ("N.m'", "", "")


def test_identical_directives_get_separate_keys_by_occurrence():
    text = f"{DIRECTIVE}\ndef y := 1\n{DIRECTIVE}   \n-- @evidence simulate N\n"
    first, second, other = evidence_directives(_cells(text), text)
    assert first["key"] == evidence_key(DIRECTIVE, 0)
    assert second["key"] == evidence_key(DIRECTIVE, 1)
    assert other["key"] == evidence_key("-- @evidence simulate N", 0)
    expected = hashlib.sha256(f"{DIRECTIVE}\n1".encode()).hexdigest()
    assert evidence_key(DIRECTIVE + " \t", 1) == expected


def test_cards_pass_the_option_text_to_the_provider():
    calls: list = []
    [card] = _cards(f"{DIRECTIVE}\n", _loaded(_provider(calls)))
    assert calls == [("simulate", "M", "(h := 1/100)")]
    assert card == {
        "cell": 0,
        "line": 1,
        "verb": "simulate",
        "target": "M",
        "key": evidence_key(DIRECTIVE, 0),
        "provider": "demo",
        "error": "",
        "unavailable": None,
        "status": "never",
        "run_error": None,
        "snapshot": None,
        "failure": None,
    }


def test_a_rejected_directive_shows_the_provider_message():
    [card] = _cards("-- @evidence simulate M (bad)\n", _loaded(_provider()))
    assert card["error"] == "Options are written (h := 1/100)."
    assert card["provider"] == "demo"


def test_a_missing_dependency_is_reported_as_unavailable():
    provider = _provider(unavailable=lambda: "Install the simulation extra.")
    [card] = _cards(f"{DIRECTIVE}\n", _loaded(provider))
    assert card["unavailable"] == "Install the simulation extra."
    assert card["error"] == ""


def test_snapshot_says_which_listed_verbs_cannot_run(tmp_path: Path, monkeypatch):
    """A verb whose provider lacks a dependency is listed with the reason."""
    from glitnir.notebook import source_preview
    from glitnir.providers import Providers

    ready = _provider()
    lacking = _provider(unavailable=lambda: "Needs Python Asgard.")
    loaded = Providers({}, {"simulate": ready, "iterate": lacking}, {}, {})
    monkeypatch.setattr(source_preview, "load_providers", lambda: loaded)
    path = tmp_path / "A.lean"
    path.write_text(f"def M := 1\n\n{DIRECTIVE}\n-- @evidence iterate M\n")
    snapshot = SourcePreviewService(path).snapshot()
    assert snapshot["directive_providers"]["evidence"] == ["iterate", "simulate"]
    assert snapshot["directive_unavailable"] == {
        "evidence": {"iterate": "Needs Python Asgard."},
        "decide": {},
    }
    simulate, iterate = snapshot["evidence"]
    assert simulate["unavailable"] is None
    assert (iterate["unavailable"], iterate["error"]) == ("Needs Python Asgard.", "")


def test_a_verb_without_a_provider_names_failed_providers():
    [card] = _cards(f"{DIRECTIVE}\n", _loaded(errors={"broken": "ImportError: x"}))
    assert card["provider"] is None and card["unavailable"] is None
    assert card["error"] == (
        "No installed provider runs `@evidence simulate`."
        " (Provider broken failed to load: ImportError: x)"
    )


def test_a_directive_error_is_kept_and_never_parsed():
    calls: list = []
    [card] = _cards("-- @evidence simulate M!\n", _loaded(_provider(calls)))
    assert card["error"] == BAD_TARGET and calls == []


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
    [card] = _cards(f"{DIRECTIVE}\n", _loaded(_provider(**changes)))
    assert card["error"] == f"Provider demo failed: {message}"


def test_unreadable_provider_answers_become_card_errors():
    provider = _provider(parse=lambda *_: {"verb": "simulate"})
    [card] = _cards(f"{DIRECTIVE}\n", _loaded(provider))
    assert card["error"] == "Provider demo returned an unreadable request."
    provider = _provider(unavailable=lambda: 3)
    [card] = _cards(f"{DIRECTIVE}\n", _loaded(provider))
    assert card["error"] == "Provider demo reported an unreadable availability."


def test_snapshot_lists_evidence_without_processes(tmp_path: Path, monkeypatch):
    import subprocess

    from glitnir.notebook import source_preview

    def forbidden(*args, **kwargs):
        pytest.fail("snapshot executed a process")

    def compute(request, payload):
        pytest.fail("snapshot computed evidence")

    provider = _provider(compute=compute)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(source_preview, "load_providers", lambda: _loaded(provider))
    path = tmp_path / "A.lean"
    path.write_text(f"def M := 1\n\n{DIRECTIVE}\n-- @evidence iterate M\n")
    snapshot = SourcePreviewService(path).snapshot()
    assert snapshot["directive_providers"] == {"evidence": ["simulate"], "decide": []}
    assert "simulations" not in snapshot
    found, missing = snapshot["evidence"]
    assert (found["line"], found["status"], found["error"]) == (3, "never", "")
    cell = snapshot["cells"][found["cell"]]
    assert cell["start"] <= 3 <= cell["end"]
    assert missing["error"] == "No installed provider runs `@evidence iterate`."


def _evidence(**changes) -> dict:
    body = {
        "format": "glitnir.evidence/v1",
        "series": {
            "times": [0, 0.5, 1.0],
            "values": [[1, None], [2.0, 3.0], [4.0, 5.0]],
            "names": ["x", "y"],
        },
        "plane": [0, 1],
        "provenance": [["step", "1/100"], ["solver", "euler"]],
        "finite": True,
    }
    return {**body, **changes}


def _with_series(**changes) -> dict:
    return _evidence(series={**_evidence()["series"], **changes})


def test_checked_evidence_returns_a_normalised_copy():
    body = _evidence()
    checked = checked_evidence(body)
    assert checked is not body and checked["series"] is not body["series"]
    assert checked["series"]["times"] == [0.0, 0.5, 1.0]
    assert all(isinstance(t, float) for t in checked["series"]["times"])
    assert checked["series"]["values"][0] == [1.0, None]
    assert checked["plane"] == [0, 1] and checked["finite"] is True
    assert checked_evidence(checked) == checked


def test_checked_evidence_plane_is_optional():
    body = _evidence()
    del body["plane"]
    assert "plane" not in checked_evidence(body)


def _rejected(body, fragment: str | None = None) -> None:
    with pytest.raises(ValueError, match=fragment):
        checked_evidence(body)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        _evidence(format="glitnir.evidence/v2"),
        _evidence(extra=1),
        _evidence(finite="yes"),
        _evidence(finite=1),
        _evidence(series=[]),
        _evidence(series={"times": [], "values": []}),
        _evidence(series={**_evidence()["series"], "more": []}),
        _with_series(times=[0.0, 1.0]),
        _with_series(times="abc"),
        _with_series(names=["x"]),
        _with_series(values=[[1, 2], [1, 2, 3], [1, 2]]),
        _with_series(values=[1, 2, 3]),
        _with_series(names=["x", 3]),
        _with_series(names=["x", "y" * 201]),
        _evidence(plane=[0, 2]),
        _evidence(plane=[-1, 1]),
        _evidence(plane=[0]),
        _evidence(plane=[0.0, 1]),
        _evidence(plane=[True, 1]),
        _evidence(plane="01"),
        _evidence(provenance=[["a"]]),
        _evidence(provenance=[["a", 1]]),
        _evidence(provenance=[["a", "b" * 201]]),
        _evidence(provenance=[["a", "b"]] * 33),
        _evidence(provenance="x"),
    ],
)
def test_checked_evidence_rejects_a_malformed_body(body):
    _rejected(body)


def test_checked_evidence_needs_every_required_key():
    for key in ("format", "series", "provenance", "finite"):
        body = _evidence()
        del body[key]
        _rejected(body)


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), -float("inf"), True, "1", [1], 10**400]
)
def test_checked_evidence_rejects_non_numbers_and_non_finite_numbers(bad):
    _rejected(_with_series(times=[0.0, 0.5, bad]))
    _rejected(_with_series(values=[[1, 2], [1, 2], [1, bad]]))


def test_checked_evidence_allows_null_values_but_not_null_times():
    checked_evidence(_with_series(values=[[None, None]] * 3))
    _rejected(_with_series(times=[0.0, None, 1.0]))


def test_checked_evidence_bounds_samples_and_series():
    def series(n: int, k: int) -> dict:
        return _with_series(
            times=[float(i) for i in range(n)],
            values=[[0.0] * k for _ in range(n)],
            names=[f"s{i}" for i in range(k)],
        )

    checked_evidence(series(4000, 16))
    _rejected(series(4001, 1), "at most 4000")
    _rejected(series(1, 17), "at most 16")


def test_checked_evidence_bounds_the_serialized_size(monkeypatch):
    # The sample and series bounds keep real bodies under the limit; this is
    # the backstop should either bound be raised.
    from glitnir.providers import outcomes

    monkeypatch.setattr(outcomes, "MAX_BYTES", 100)
    _rejected(_evidence(), "bytes")


def _saved_entry(text: str, line: int, fingerprint: str) -> dict:
    return {
        "prefix_sha256": evidence_prefix_sha256(text, line),
        "fingerprint": fingerprint,
        "provider": "demo",
        "created": "2026-10-14T12:00:00Z",
        "snapshot": checked_evidence(_evidence()),
    }


def _saved_service(tmp_path: Path, monkeypatch, fingerprint: str):
    from glitnir.notebook import source_preview

    monkeypatch.setattr(source_preview, "load_providers", lambda: _loaded(_provider()))
    calls: list[int] = []

    def environment(*_arguments):
        calls.append(1)
        return {"sha256": fingerprint}

    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(module, "environment_fingerprint", environment)
    text = f"def M := 1\n{DIRECTIVE}\n"
    path = tmp_path / "A.lean"
    path.write_text(text)
    entry = _saved_entry(text, 2, FP)
    save_records(path, with_evidence(None, evidence_key(DIRECTIVE, 0), entry, []))
    return path, text, calls, entry


def test_snapshot_marks_saved_evidence_current(tmp_path: Path, monkeypatch):
    path, _text, calls, entry = _saved_service(tmp_path, monkeypatch, FP)
    [card] = SourcePreviewService(path).snapshot()["evidence"]
    assert (card["status"], card["snapshot"]) == ("current", entry["snapshot"])
    # The claims' fingerprint, then the run's, which counts the provider's
    # modules too.
    assert calls == [1, 1]


def test_snapshot_marks_saved_evidence_stale_for_other_text_or_environment(
    tmp_path: Path, monkeypatch
):
    path, text, _calls, entry = _saved_service(tmp_path, monkeypatch, GP)
    [card] = SourcePreviewService(path).snapshot()["evidence"]
    assert (card["status"], card["snapshot"]) == ("stale", entry["snapshot"])
    path.write_text(text.replace("M := 1", "M := 2"))
    [card] = SourcePreviewService(path).snapshot()["evidence"]
    assert card["status"] == "stale"


def test_snapshot_without_a_sidecar_pays_no_fingerprint(tmp_path: Path, monkeypatch):
    path, _text, calls, _entry = _saved_service(tmp_path, monkeypatch, FP)
    records_path(path).unlink()
    [card] = SourcePreviewService(path).snapshot()["evidence"]
    assert (card["status"], card["snapshot"]) == ("never", None)
    assert calls == []


def test_snapshot_with_claims_but_no_evidence_fingerprints_once(
    tmp_path: Path, monkeypatch
):
    path, _text, calls, _entry = _saved_service(tmp_path, monkeypatch, FP)
    path.write_text(path.read_text().replace("simulate M", "simulate N"))
    [card] = SourcePreviewService(path).snapshot()["evidence"]
    assert (card["status"], card["snapshot"]) == ("never", None)
    assert calls == [1]  # the sidecar's claims need it; evidence adds nothing


def test_snapshot_reads_saved_evidence_as_stale_when_the_fingerprint_fails(
    tmp_path: Path, monkeypatch, patch_fingerprint
):

    path, _text, _calls, entry = _saved_service(tmp_path, monkeypatch, FP)

    def boom(*_arguments):
        raise OSError("no environment")

    patch_fingerprint(boom)
    [card] = SourcePreviewService(path).snapshot()["evidence"]
    assert (card["status"], card["snapshot"]) == ("stale", entry["snapshot"])


def test_snapshot_serves_the_normalised_copy_of_saved_evidence(
    tmp_path: Path, monkeypatch
):
    path, _text, _calls, entry = _saved_service(tmp_path, monkeypatch, FP)
    [card] = SourcePreviewService(path).snapshot()["evidence"]
    assert card["snapshot"] == entry["snapshot"]
    assert card["snapshot"] is not entry["snapshot"]


def test_directives_module_parses_all_three_kinds() -> None:
    from glitnir.notebook.directives import (
        decision_directives,
        evidence_directives,
        view_directives,
    )

    text = "-- @evidence simulate f\n-- @decide g : 1 = 1\n-- @view circuit h\n"
    cells = [
        {"kind": "source", "index": 0, "start": 1, "end": 3, "text": text.rstrip("\n")}
    ]
    assert [d["line"] for d in evidence_directives(cells, text)] == [1]
    assert [d["line"] for d in decision_directives(cells, text)] == [2]
    assert [d["line"] for d in view_directives(cells, text)] == [3]
