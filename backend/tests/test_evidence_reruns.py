"""Lean output remembered for a rerun, so an unchanged request is not asked again."""

import hashlib

import pytest
from conftest import (
    EVIDENCE_TEXT,
    FP,
    evidence_body,
    evidence_service,
    recording_evidence_provider,
    saved_failures,
    settle,
)


def _refusing_bad_options(calls):
    """A demo provider whose `compute` refuses options that name `bad`."""

    def compute(request, payload):
        calls.append(("compute", request, payload))
        if "bad" in request.options["options"]:
            raise ValueError("The circuit maps 1 port to 1, not 2.")
        return evidence_body()

    return recording_evidence_provider(compute=compute)


def _rerun(service, text, line=4):
    """Save `text` as the notebook, run the directive at `line`; return its card."""
    service.path.write_text(text)
    service.run_evidence(hashlib.sha256(text.encode()).hexdigest(), line)
    settle(service)
    assert not service._runs._evidence_worker.is_alive()
    return next(c for c in service.snapshot()["evidence"] if c["line"] == line)


def _lean_calls(calls):
    return [call for call in calls if call[0] == "lean"]


def _saved_snapshots(service):
    from glitnir.notebook.check_records import load_records

    return load_records(service.path)["evidence"]


BAD_TEXT = EVIDENCE_TEXT.replace("(h := 1/100)", "(h := bad)")


def test_a_first_run_still_runs_lean_and_saves_the_late_refusal(tmp_path, monkeypatch):
    """Nothing was remembered: Lean runs, and `compute`'s refusal is saved."""
    calls: list = []
    service, lean = evidence_service(
        tmp_path, monkeypatch, _refusing_bad_options(calls)
    )
    try:
        card = _rerun(service, BAD_TEXT)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 1
    assert (card["status"], card["run_error"]) == (
        "failed",
        "The circuit maps 1 port to 1, not 2.",
    )
    [saved] = saved_failures(service).values()
    assert (saved["kind"], saved["message"]) == (
        "refused",
        "The circuit maps 1 port to 1, not 2.",
    )


def test_a_rerun_after_an_options_edit_is_refused_without_lean(tmp_path, monkeypatch):
    """Lean's output for the unchanged text above is remembered and reused.

    The refusal is the same late check, on the same payload, saved as
    `refused` like any other.
    """
    calls: list = []
    service, lean = evidence_service(
        tmp_path, monkeypatch, _refusing_bad_options(calls)
    )
    try:
        first = _rerun(service, EVIDENCE_TEXT)
        second = _rerun(service, BAD_TEXT)
        third = _rerun(service, EVIDENCE_TEXT.replace("1/100", "1/50"))
    finally:
        service.close()
    assert first["status"] == "current"
    assert len(_lean_calls(lean)) == 1
    assert [call[2] for call in calls] == [{"x": 1}] * 3
    assert (second["status"], second["run_error"]) == (
        "failed",
        "The circuit maps 1 port to 1, not 2.",
    )
    assert third["status"] == "current" and third["run_error"] is None
    [saved] = _saved_snapshots(service).values()
    assert saved["fingerprint"] == FP


def test_lines_below_the_directive_do_not_need_lean_again(tmp_path, monkeypatch):
    service, lean = evidence_service(tmp_path, monkeypatch)
    try:
        _rerun(service, EVIDENCE_TEXT)
        card = _rerun(service, EVIDENCE_TEXT + "\ndef Later := 2\n")
    finally:
        service.close()
    assert card["status"] == "current"
    assert len(_lean_calls(lean)) == 1


@pytest.mark.parametrize(
    "edited",
    [
        EVIDENCE_TEXT.replace("def M := 1", "def M := 2"),  # the text above
        "\n" + EVIDENCE_TEXT,  # a line inserted above
    ],
)
def test_an_edit_above_the_directive_runs_lean_again(tmp_path, monkeypatch, edited):
    service, lean = evidence_service(tmp_path, monkeypatch)
    line = edited.split("\n").index("-- @evidence simulate M (h := 1/100)") + 1
    try:
        _rerun(service, EVIDENCE_TEXT)
        _rerun(service, edited, line)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 2


def test_another_command_runs_lean_again(tmp_path, monkeypatch):
    """The command is part of what Lean saw: another target is another run."""
    service, lean = evidence_service(tmp_path, monkeypatch)
    try:
        _rerun(service, EVIDENCE_TEXT)
        _rerun(service, EVIDENCE_TEXT.replace("simulate M", "simulate N"))
    finally:
        service.close()
    assert [call[1]["command"] for call in _lean_calls(lean)] == [
        "#demo_evidence M",
        "#demo_evidence N",
    ]


@pytest.mark.parametrize(
    "environments",
    [
        # The environment changed between the runs.
        [{"sha256": FP, "packages": []}, {"sha256": "e" * 64, "packages": []}],
        # An unknown environment's output is never remembered or reused.
        [{"sha256": FP, "unknown": True, "packages": []}] * 2,
    ],
)
def test_another_or_an_unknown_environment_runs_lean_again(
    tmp_path, monkeypatch, patch_fingerprint, environments
):
    service, lean = evidence_service(tmp_path, monkeypatch)
    given = iter(environments)
    current: dict = {}

    def fingerprint(*args, **kwargs):
        return current

    patch_fingerprint(fingerprint)
    try:
        for environment in given:
            current.clear()
            current.update(environment)
            _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 2


@pytest.mark.parametrize(
    "failed",
    [
        {"error": "Line 2: unknown constant 'x'", "lean": True},
        {"error": "timed out.", "timed_out": True},
        {"error": "Lake failed."},
    ],
)
def test_only_printed_output_is_remembered(tmp_path, monkeypatch, failed):
    """A Lean error, time-out or setup failure is tried again on the next run."""
    results = iter([dict(failed), {"payload": '{"x": 1}'}, {"payload": "{}"}])
    asked: list = []

    def run(source, **options):
        asked.append(options)
        return next(results)

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        _rerun(service, EVIDENCE_TEXT)
        _rerun(service, EVIDENCE_TEXT)
        card = _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert len(asked) == 2
    assert card["status"] == "current"


def test_remembered_output_is_bounded_by_count_and_size():
    from glitnir.service.runs import _Emissions

    kept = _Emissions(entries=2, size=10)
    kept.keep(("a",), "1234")
    kept.keep(("b",), "5678")
    kept.keep(("c",), "90")
    assert (kept.get(("a",)), kept.get(("b",)), kept.get(("c",))) == (
        None,
        "5678",
        "90",
    )
    kept.keep(("d",), "123456789")  # over the size with "90": the oldest go first
    assert (kept.get(("b",)), kept.get(("c",)), kept.get(("d",))) == (
        None,
        None,
        "123456789",
    )
    kept.keep(("e",), "12345678901")  # larger than the whole bound: not kept
    assert kept.get(("e",)) is None and kept.get(("d",)) == "123456789"


def test_output_is_not_kept_when_the_environment_changed_during_the_run(
    tmp_path, monkeypatch, patch_fingerprint
):
    """Lean may have loaded the other build: its output describes neither for sure.

    The run's own result is handled as before; only the next run asks Lean again.
    """
    current = {"sha256": FP, "packages": []}

    def run(source, **options):
        lean.append(options)
        current["sha256"] = "e" * 64  # rebuilt while Lean ran
        return {"payload": '{"x": 1}'}

    lean: list = []
    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    patch_fingerprint(lambda *args: dict(current))
    try:
        first = _rerun(service, EVIDENCE_TEXT)
        current["sha256"] = FP  # and back
        _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert first["run_error"] is None
    assert len(lean) == 2


def test_output_kept_before_an_environment_change_is_used_after_it_is_undone(
    tmp_path, monkeypatch, patch_fingerprint
):
    current = {"sha256": FP, "packages": []}
    service, lean = evidence_service(tmp_path, monkeypatch)
    patch_fingerprint(lambda *args: dict(current))
    try:
        _rerun(service, EVIDENCE_TEXT)
        current["sha256"] = "e" * 64
        _rerun(service, EVIDENCE_TEXT)
        current["sha256"] = FP
        card = _rerun(service, EVIDENCE_TEXT)
    finally:
        service.close()
    assert len(_lean_calls(lean)) == 2
    assert card["status"] == "current"


def _key_parts(**changes):
    """The arguments of `_emission_key` for a demo job, with `changes`."""
    from glitnir.service.runs import _EvidenceJob

    parts = {
        "text": EVIDENCE_TEXT,
        "line": 4,
        "imports": ("Demo.Evidence",),
        "preamble": "open Demo",
        "marker": "demoEvidence:",
        "command": "#demo_evidence M",
        "environment": {"sha256": FP},
        **changes,
    }
    job = _EvidenceJob(
        text=parts["text"],
        sha256=hashlib.sha256(parts["text"].encode()).hexdigest(),
        line=parts["line"],
        key="k",
        keep=frozenset({"k"}),
        provider=recording_evidence_provider(),
        request=None,
    )
    return (
        job,
        parts["imports"],
        parts["preamble"],
        parts["marker"],
        parts["command"],
        parts["environment"],
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"imports": ("Demo.Evidence", "Demo.More")},
        {"preamble": "open Demo Other"},
        {"marker": "otherEvidence:"},
        {"command": "#demo_evidence N"},
        {"line": 5},
        {"text": EVIDENCE_TEXT.replace("def M := 1", "def M := 2")},
        {"text": "module\n" + EVIDENCE_TEXT, "line": 5},
        {"environment": {"sha256": "e" * 64}},
    ],
)
def test_everything_lean_is_given_is_part_of_the_key(changes):
    from glitnir.service.runs import _emission_key

    assert _emission_key(*_key_parts(**changes)) != _emission_key(*_key_parts())


def test_the_header_below_a_directive_is_part_of_the_key():
    """Lake's setup reads the whole header, even lines after the directive."""
    from glitnir.service.runs import _emission_key

    text = "-- @evidence simulate M (h := 1/100)\nimport Demo.Evidence\n"
    other = "-- @evidence simulate M (h := 1/100)\nmodule\nimport Demo.Evidence\n"
    first = _emission_key(*_key_parts(text=text, line=1))
    assert first is not None
    assert first != _emission_key(*_key_parts(text=other, line=1))


@pytest.mark.parametrize(
    "environment",
    [
        {"sha256": FP, "unknown": True},
        {"sha256": ""},
        {"sha256": "not a digest"},
        {},
    ],
)
def test_no_key_for_an_unknown_or_malformed_environment(environment):
    from glitnir.service.runs import _emission_key

    assert _emission_key(*_key_parts(environment=environment)) is None
