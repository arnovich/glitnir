"""Evidence runs: directives run through providers, with their cards and errors."""

import hashlib
import re
from pathlib import Path

import pytest
from conftest import (
    EVIDENCE_TEXT,
    FP,
    asking_lean_every_time,
    check_and_wait,
    drawn_view,
    evidence_body,
    evidence_card,
    evidence_service,
    evidence_sha,
    fake_result,
    raises,
    recording_evidence_provider,
    run_evidence,
    saved_failures,
    settle,
    view_service,
)

from glitnir.lean.project_inspection import TOOLCHAIN
from glitnir.preview import snapshot as preview_snapshot
from glitnir.service import preview as source_preview
from glitnir.service import runs as source_runs
from glitnir.service.preview import SourcePreviewService


def test_an_evidence_run_waits_for_the_slot_and_counts_its_modules(
    tmp_path, monkeypatch, patch_fingerprint
):
    """The run counts its provider's modules and waits for a busy slot.

    A card saved under that environment reads as current.
    """
    service, calls = evidence_service(tmp_path, monkeypatch)
    imports = service._providers().evidence["simulate"].imports
    assert imports, "the demo provider loads modules of its own"
    asked = []

    def fingerprint(path, text, extra=(), packages=None):
        asked.append(extra)
        named = f"env-{'+'.join(extra)}".encode()
        return {"sha256": hashlib.sha256(named).hexdigest(), "packages": []}

    patch_fingerprint(fingerprint)
    run_evidence(service)
    [(_, options)] = [call for call in calls if call[0] == "lean"]
    assert options["wait"] is True
    assert imports in asked
    card = evidence_card(service)
    assert card["status"] == "current", card
    service.close()


def test_an_evidence_run_saves_a_current_snapshot(tmp_path, monkeypatch):
    import json

    from glitnir.notebook.check_records import records_path
    from glitnir.providers.outcomes import checked_evidence

    service, calls = evidence_service(tmp_path, monkeypatch)
    try:
        key = run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["key"] == key
    assert (card["status"], card["run_error"]) == ("current", None)
    assert card["snapshot"] == checked_evidence(evidence_body())
    [lean, compute] = calls
    assert lean[1]["needs"] == "evidence"
    assert lean[1]["expected_sha256"] == evidence_sha()
    assert (lean[1]["line"], lean[1]["provider_name"]) == (4, "demo")
    assert (lean[1]["imports"], lean[1]["preamble"]) == (
        ("Demo.Evidence",),
        "open Demo",
    )
    assert (lean[1]["command"], lean[1]["marker"]) == (
        "#demo_evidence M",
        "demoEvidence:",
    )
    assert lean[1]["cancel"] is service._cancel
    assert compute[1].target == "M" and compute[2] == {"x": 1}
    saved = json.loads(records_path(service.path).read_text())["evidence"][key]
    assert saved["provider"] == "demo" and saved["fingerprint"] == FP
    assert saved["created"].endswith("+00:00")
    assert service.path.read_text() == EVIDENCE_TEXT


class _ExpectedError(ValueError):
    """What a provider raises for a request it cannot serve."""


@pytest.mark.parametrize(
    "changes, message, kind",
    [
        (
            {"compute": lambda request, payload: 1 / 0},
            "Provider demo failed: ZeroDivisionError: division by zero",
            "provider",
        ),
        (
            {"compute": raises(ValueError("The model has no states."))},
            "The model has no states.",
            "refused",
        ),
        (
            {"compute": raises(_ExpectedError("Option h must be positive."))},
            "Option h must be positive.",
            "refused",
        ),
        (
            {"compute": raises(ValueError())},
            "Provider demo failed: ValueError: ",
            "refused",
        ),
        (
            {"compute": raises(KeyError("x"))},
            "Provider demo failed: KeyError: 'x'",
            "provider",
        ),
        (
            {"command": lambda request: [][0]},
            "Provider demo failed: IndexError: list index out of range",
            "provider",
        ),
        (
            {"compute": lambda request, payload: evidence_body(format="other")},
            "Provider demo returned invalid evidence: "
            "Evidence format must be glitnir.evidence/v1.",
            "invalid",
        ),
    ],
)
def test_provider_failures_become_the_card_run_error(
    tmp_path, monkeypatch, changes, message, kind
):
    """Each is shown at once and saved as a failure of its kind."""
    service, _calls = evidence_service(
        tmp_path, monkeypatch, recording_evidence_provider(**changes)
    )
    try:
        key = run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("failed", message)
    [(saved_key, saved)] = saved_failures(service).items()
    # A crash's text is not written for the reader: only its type is saved.
    if kind == "provider":
        message = message.rsplit(": ", 1)[0]
    assert (saved_key, saved["kind"], saved["message"]) == (key, kind, message)


def test_unreadable_payloads_and_runner_errors_become_run_errors(tmp_path, monkeypatch):
    results = iter([{"payload": "{not json"}, {"error": "Line 2: unknown constant"}])
    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(results)
    )
    asking_lean_every_time(monkeypatch)
    try:
        run_evidence(service)
        first = evidence_card(service)["run_error"]
        run_evidence(service)
        second = evidence_card(service)["run_error"]
    finally:
        service.close()
    assert first == "Provider demo printed evidence that is not JSON."
    assert second == "Line 2: unknown constant"


@pytest.mark.parametrize(
    "sha, line, message",
    [
        ("0" * 64, 4, "Saved source changed; refresh and retry."),
        (None, 1, "No evidence directive at line 1."),
        (None, 5, "No installed provider runs `@evidence iterate`."),
    ],
)
def test_runs_are_refused_without_lean(tmp_path, monkeypatch, sha, line, message):
    from glitnir.lean.project_snapshot import SnapshotError

    def forbidden(*args, **kwargs):
        pytest.fail("Lean ran")

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=forbidden)
    try:
        with pytest.raises(SnapshotError) as raised:
            service.run_evidence(sha or evidence_sha(), line)
    finally:
        service.close()
    assert str(raised.value) == message
    assert service._runs._evidence_worker is None
    assert saved_failures(service) == {}


def _rejecting(verb, target, options):
    raise ValueError("Options are written (h := 1/100).")


@pytest.mark.parametrize(
    "changes, message",
    [
        (
            {"unavailable": lambda: "Install demo[simulation]."},
            "Install demo[simulation].",
        ),
        ({"parse": _rejecting}, "Options are written (h := 1/100)."),
    ],
)
def test_unavailable_or_rejected_directives_are_refused(
    tmp_path, monkeypatch, changes, message
):
    from glitnir.lean.project_snapshot import SnapshotError

    def forbidden(*args, **kwargs):
        pytest.fail("Lean ran")

    service, _calls = evidence_service(
        tmp_path, monkeypatch, recording_evidence_provider(**changes), runner=forbidden
    )
    try:
        with pytest.raises(SnapshotError, match=r"^" + re.escape(message) + r"$"):
            service.run_evidence(evidence_sha(), 4)
    finally:
        service.close()


def test_one_run_at_a_time_shown_as_running(tmp_path, monkeypatch):
    import threading

    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.service.runs import RUN_BUSY

    started, release = threading.Event(), threading.Event()
    outcomes = iter([{"error": "first failed"}, {"payload": "{}"}])

    def run(source, **options):
        started.set()
        assert release.wait(10)
        return next(outcomes)

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        release.set()
        run_evidence(service)
        assert evidence_card(service)["run_error"] == "first failed"
        release.clear()
        started.clear()
        service.run_evidence(evidence_sha(), 4)
        assert started.wait(10)
        running = evidence_card(service)
        with pytest.raises(SnapshotError, match=RUN_BUSY):
            service.run_evidence(evidence_sha(), 4)
        release.set()
        settle(service)
        finished = evidence_card(service)
    finally:
        release.set()
        service.close()
    # The next run of a card clears its last error as it starts.
    assert (running["status"], running["run_error"]) == ("running", None)
    assert (finished["status"], finished["run_error"]) == ("current", None)


def test_source_saved_during_the_run_is_not_saved(tmp_path, monkeypatch):
    from glitnir.notebook.check_records import records_path
    from glitnir.service.runs import CHANGED_DURING_RUN

    path = tmp_path / "A.lean"

    def run(source, **options):
        path.write_text(EVIDENCE_TEXT + "-- edited\n")
        return {"payload": "{}"}

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        run_evidence(service)
        [card] = [c for c in service.snapshot()["evidence"] if c["line"] == 4]
    finally:
        service.close()
    assert card["run_error"] == CHANGED_DURING_RUN
    assert not records_path(path).exists()


def test_an_unreadable_records_file_is_never_overwritten(tmp_path, monkeypatch):
    from glitnir.notebook.check_records import records_path

    service, _calls = evidence_service(tmp_path, monkeypatch)
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == (
        "A.glitnir.json exists but cannot be read; fix or remove it "
        "before saving evidence."
    )
    assert sidecar.read_text() == "not json"


def test_evidence_saves_wait_for_other_records_writes(tmp_path, monkeypatch):
    """A check and an evidence run never interleave their records writes."""
    import threading

    from glitnir.notebook.check_records import records_path

    ran = threading.Event()

    def run(source, **options):
        ran.set()
        return {"payload": "{}"}

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        with service._records_lock:
            service.run_evidence(evidence_sha(), 4)
            assert ran.wait(10)
            service._runs._evidence_worker.join(timeout=0.2)
            assert service._runs._evidence_worker.is_alive()
            assert not records_path(service.path).exists()
        settle(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["status"] == "current"


def test_a_check_keeps_evidence_saved_before_it(tmp_path, monkeypatch):
    from glitnir.service import preview as source_preview

    service, _calls = evidence_service(tmp_path, monkeypatch)
    sha = evidence_sha()
    result = {**fake_result(sha), "theorems": []}
    monkeypatch.setattr(source_preview, "check_project", lambda *a, **k: result)
    try:
        key = run_evidence(service)
        check_and_wait(service, sha)
        card = evidence_card(service)
    finally:
        service.close()
    assert (card["key"], card["status"]) == (key, "current")


def test_missing_provider_import_is_a_run_error_without_lean(tmp_path, monkeypatch):
    """Review Focus 4: the real runner refuses before the sandbox starts."""
    from glitnir.lean import provider_lean

    service, _calls = evidence_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_runs, "run_provider_lean", provider_lean.run_provider_lean
    )
    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setattr(provider_lean, "_toolchain", lambda: toolchain)
    monkeypatch.setattr(
        provider_lean,
        "inspect_project",
        lambda path, *, expected_sha256, cancel, imports=(): {
            "status": "resolved",
            "setup": {
                "name": "A",
                "isModule": False,
                "importArts": {},
                "plugins": [],
                "dynlibs": [],
                "options": {},
            },
            "resolution": {"inputs": []},
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("run_observer was called")

    monkeypatch.setattr(provider_lean, "run_observer", forbidden)
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"].startswith(
        "This evidence needs the module `Demo.Evidence`, which no package"
    )


def test_close_cancels_the_run_and_refuses_new_ones(tmp_path, monkeypatch):
    import threading

    from glitnir.lean.project_snapshot import SnapshotError

    started = threading.Event()

    def run(source, **options):
        started.set()
        assert options["cancel"].wait(10)
        return {"error": "Evidence run cancelled.", "transient": True}

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    service.run_evidence(evidence_sha(), 4)
    assert started.wait(10)
    service.close()
    assert not service._runs._evidence_worker.is_alive()
    with pytest.raises(SnapshotError, match="closed"):
        service.run_evidence(evidence_sha(), 4)


_UNKNOWN_GIT = {
    "sha256": FP,
    "packages": [{"name": "mathlib", "reason": "fatal: detected dubious ownership"}],
}


_UNKNOWN_NOTE = (
    "Could not read the git state of mathlib: fatal: detected dubious "
    "ownership; results are saved as out of date."
)


def test_check_says_when_dependency_git_state_is_unknown(
    tmp_path, monkeypatch, patch_fingerprint
):
    """Results saved under an unknown git state say why they read out of date."""
    import hashlib

    from glitnir.service import preview as source_preview

    path = tmp_path / "A.lean"
    path.write_text("theorem result : True := by\n  trivial\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    phases: list = []
    service = source_preview.SourcePreviewService(path)

    def fingerprint(*args):
        phases.append(service._check["message"])
        return _UNKNOWN_GIT

    patch_fingerprint(fingerprint)
    monkeypatch.setattr(
        source_preview, "check_project", lambda *a, **k: fake_result(sha)
    )
    try:
        check_and_wait(service, sha)
        check = service.snapshot()["preview"]["check"]
    finally:
        service.close()
    assert phases[0] == "Reading dependency state"
    assert check["message"] == f"ok {_UNKNOWN_NOTE}"
    assert (tmp_path / "A.glitnir.json").is_file()


def test_evidence_saved_under_unknown_git_state_explains_it(
    tmp_path, monkeypatch, patch_fingerprint
):
    """The evidence is saved; the card's run error says why it is out of date."""
    from glitnir.notebook.check_records import load_records

    service, _ = evidence_service(tmp_path, monkeypatch)
    patch_fingerprint(lambda *a: _UNKNOWN_GIT)
    try:
        key = run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == _UNKNOWN_NOTE
    assert key in (load_records(tmp_path / "A.lean") or {}).get("evidence", {})


def test_a_check_through_a_claim_shows_later_claims_not_rechecked(
    tmp_path, monkeypatch
):
    """The preview asks for lines 1..N; later saved results stay, marked."""
    import hashlib

    from glitnir.service import preview as source_preview

    path = tmp_path / "A.lean"
    text = "theorem a : True := trivial\ntheorem b : True := trivial\n"
    path.write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    asked = []

    def theorem(name, line):
        return {
            "name": name,
            "status": "checked",
            "axioms": [],
            "start_line": line,
            "end_line": line,
            "proposition_sha256": name,
            "uses": [],
        }

    def check(source, *, through_line=None, **_):
        asked.append(through_line)
        result = {**fake_result(sha), "theorems": [theorem("a", 1)]}
        if through_line is None:
            result["theorems"].append(theorem("b", 2))
        else:
            result["withheld"] = {
                "from_line": through_line + 1,
                "message": f"Checked through line {through_line} on request",
                "through_line": through_line,
            }
        return result

    monkeypatch.setattr(source_preview, "check_project", check)
    service = source_preview.SourcePreviewService(path)
    try:
        check_and_wait(service, sha)
        service.check(sha, through_line=1)
        assert service.snapshot()["preview"]["check"]["through_line"] == 1
        settle(service)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert asked == [None, 1]
    assert snapshot["preview"]["check"]["through_line"] == 1
    assert snapshot["project_withheld"] == {
        "from_line": 2,
        "message": "Checked through line 1 on request",
        "through_line": 1,
    }
    a, b = snapshot["project_claims"]
    assert a["current"] and "not_rechecked" not in a
    assert b["current"] and b["not_rechecked"] is True


def test_a_check_through_the_last_line_is_a_check_of_the_whole_file(
    tmp_path, monkeypatch
):
    """Nothing after the line: the request is dropped, not shown as partial."""
    import hashlib

    from glitnir.service import preview as source_preview

    path = tmp_path / "A.lean"
    text = "theorem a : True := trivial\ntheorem b : True := trivial\n"
    path.write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    asked = []

    def check(source, *, through_line=None, **_):
        asked.append(through_line)
        return fake_result(sha)

    monkeypatch.setattr(source_preview, "check_project", check)
    service = source_preview.SourcePreviewService(path)
    try:
        for line in (2, 3, 1):
            service.check(sha, through_line=line)
            settle(service)
        snapshot = service.snapshot()
    finally:
        service.close()
    assert asked == [None, None, 1]
    # The result reports no cut, so neither does the observation.
    assert snapshot["preview"]["check"]["through_line"] is None


def test_a_claim_cell_takes_in_a_continuation_of_its_command(tmp_path):
    """C1: a line continuing a saved claim's command joins its cell."""
    from glitnir.notebook.check_records import records_from_check, save_records
    from glitnir.service.preview import SourcePreviewService

    path = tmp_path / "A.lean"
    text = "def h : 1 = 1 := rfl\ntheorem foo : 1 = 1 := h\n"
    path.write_text(text)
    result = {
        **fake_result("s"),
        "theorems": [
            {
                "name": "foo",
                "status": "checked",
                "axioms": [],
                "start_line": 2,
                "end_line": 2,
                "proposition_sha256": "p",
            }
        ],
    }
    service = SourcePreviewService(path)
    try:
        save_records(
            path, records_from_check(text, result, service._fingerprint(text)())
        )
        assert service.snapshot()["project_claims"][0]["current"] is True
        path.write_text(text + "  |>.trans sorry\n")
        snapshot = service.snapshot()
    finally:
        service.close()
    [claim] = snapshot["project_claims"]
    assert (claim["current"], claim["end_line"]) == (False, 3)
    [cell] = [c for c in snapshot["cells"] if c.get("claim") == "foo"]
    assert (cell["start"], cell["end"]) == (2, 3)
    assert "|>.trans sorry" in cell["text"]


def test_indented_claims_keep_their_own_cells(tmp_path):
    """N1: claims indented in a namespace are cells of their own, current."""
    from glitnir.notebook.check_records import records_from_check, save_records
    from glitnir.service.preview import SourcePreviewService

    path = tmp_path / "A.lean"
    text = (
        "namespace Foo\n"
        "  theorem a : True := trivial\n"
        "  theorem b : True := trivial\n"
        "end Foo\n"
    )
    path.write_text(text)

    def theorem(name, line):
        return {
            "name": name,
            "status": "checked",
            "axioms": [],
            "start_line": line,
            "end_line": line,
            "proposition_sha256": name,
        }

    result = {
        **fake_result("s"),
        "theorems": [theorem("Foo.a", 2), theorem("Foo.b", 3)],
    }
    service = SourcePreviewService(path)
    try:
        save_records(
            path, records_from_check(text, result, service._fingerprint(text)())
        )
        snapshot = service.snapshot()
    finally:
        service.close()
    assert [c["current"] for c in snapshot["project_claims"]] == [True, True]
    cells = {
        c["claim"]: (c["start"], c["end"]) for c in snapshot["cells"] if c.get("claim")
    }
    assert cells == {"Foo.a": (2, 2), "Foo.b": (3, 3)}


@pytest.mark.parametrize("unknown, expected", [(False, "d" * 64), (True, None)])
def test_a_view_passes_its_environment_digest_to_the_renderer(
    tmp_path: Path,
    monkeypatch,
    unknown,
    expected,
    draw_each_alone,
    patch_fingerprint,
) -> None:

    calls = []
    draw_each_alone(lambda path, **options: calls.append(options) or {"svg": "<svg/>"})
    service = view_service(tmp_path, monkeypatch)
    cell = service.snapshot()["views"][0]["cell"]
    for module in (source_preview, source_runs, preview_snapshot):
        monkeypatch.setattr(
            module,
            "environment_fingerprint",
            lambda *a, **k: {"sha256": "d" * 64, "unknown": unknown},
        )
    drawn_view(service, cell, 4)
    assert calls[0]["environment_sha256"] == expected


@pytest.mark.parametrize("unknown, expected", [(False, FP), (True, None)])
def test_an_evidence_run_passes_its_environment_digest(
    tmp_path, monkeypatch, unknown, expected, patch_fingerprint
):

    seen = []

    def run(source, **options):
        seen.append(options["environment_sha256"])
        return {"payload": '{"x": 1}'}

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    patch_fingerprint(lambda *a: {"sha256": FP, "unknown": unknown})
    run_evidence(service)
    assert seen == [expected]


def test_components_share_the_service_locks_and_cancel(tmp_path: Path) -> None:
    from glitnir.service.runs import DirectiveRuns
    from glitnir.service.views import ViewDrawing

    path = tmp_path / "A.lean"
    path.write_text("theorem t : True := by\n  trivial\n")
    service = SourcePreviewService(path)
    try:
        assert isinstance(service._runs, DirectiveRuns)
        assert isinstance(service._views, ViewDrawing)
        for component in (service._runs, service._views):
            assert component._context.state_lock is service._state_lock
            assert component._context.records_lock is service._records_lock
            assert component._context.cancel is service._cancel
    finally:
        service.close()


def test_snapshot_shows_a_run_in_progress(tmp_path: Path, monkeypatch) -> None:
    import threading

    started, finish = threading.Event(), threading.Event()

    def run(source, **options):
        started.set()
        finish.wait(5)
        return {"payload": '{"x": 1}'}

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        key = service.run_evidence(evidence_sha(), 4)
        assert started.wait(5)
        assert evidence_card(service)["status"] == "running"
        running, errors, saved = service._runs.state()
        assert running == key and errors == {} and saved == frozenset()
    finally:
        finish.set()
        settle(service)
        service.close()


def test_a_second_run_of_either_kind_is_refused_while_one_runs(
    tmp_path: Path, monkeypatch
) -> None:
    import threading

    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.service.runs import RUN_BUSY

    started, finish = threading.Event(), threading.Event()

    def run(source, **options):
        started.set()
        finish.wait(5)
        return {"payload": '{"x": 1}'}

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        service.run_evidence(evidence_sha(), 4)
        assert started.wait(5)
        with pytest.raises(SnapshotError, match=RUN_BUSY):
            service.run_decision(evidence_sha(), 4)
        with pytest.raises(SnapshotError, match=RUN_BUSY):
            service.run_evidence(evidence_sha(), 4)
    finally:
        finish.set()
        settle(service)
        service.close()
