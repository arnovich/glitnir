"""Running, stopping and discarding decisions; only an explicit run decides."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import evidence_provider, patch_service_name, raises, settle, sha

from glitnir.lean import project_checking
from glitnir.lean.observer import TIMED_OUT
from glitnir.lean.project_inspection import TOOLCHAIN
from glitnir.lean.project_snapshot import SnapshotError
from glitnir.notebook.check_records import (
    decision_prefix_sha256,
    load_records,
    records_path,
    saved_decision_failures,
)
from glitnir.preview import snapshot as preview_snapshot
from glitnir.providers import (
    DecisionProvider,
    DecisionRequest,
    Providers,
)
from glitnir.service import runs as source_runs
from glitnir.service.preview import SourcePreviewService
from glitnir.service.runs import (
    BYTE_ORDER_MARK,
    CHANGED_DURING_RUN,
    DECISION_CANCELLED,
    DECISION_RUNNING,
    NO_DECISION,
    NOT_ACCEPTABLE,
    NOT_DECIDED,
    NOTHING_TO_STOP,
    RUN_BUSY,
    SOURCE_CHANGED,
)

# Environment fingerprints are SHA-256 digests; any other string matches nothing.
FP = "f" * 64

TEXT = (
    "import Demo.Decide\n\ndef x := 1\n"
    "-- @decide band : entailment x : x > 0\n--   |- x >= 0\n"
    "def after := 2\n-- @evidence simulate x\n"
)
LINE, END_LINE = 4, 5
BLOCK = "namespace Decided_band\ntheorem band : True := trivial\nend Decided_band"
# The real checker's verdict for BLOCK named `Decided_band.band`: a qualified
# name matches exactly that declaration (see test_candidate_checking).
CHECKED = {
    "status": "checked",
    "message": "The checker reports `Decided_band.band` as checked.",
    "axioms": [],
    "type": "True",
}


def _outcome(**changes):
    outcome = {
        "status": "proven",
        "reason": "trivial",
        "stages": [["decide", "proven", ""]],
        "block": BLOCK,
        "theorem": "Decided_band.band",
    }
    return {**outcome, **changes}


def _parse(name: str, kind: str, goal: str) -> DecisionRequest:
    return DecisionRequest(name, kind, goal, {})


def _decision_provider(**changes) -> DecisionProvider:
    fields = {
        "name": "demo",
        "kinds": frozenset({"entailment"}),
        "imports": ("Demo.Decide",),
        "parse": _parse,
        "decide": lambda request, cancel: _outcome(),
        "unavailable": lambda: None,
        **changes,
    }
    return DecisionProvider(**fields)


def _service(tmp_path, monkeypatch, provider=None, check=None, lean=None):
    """A notebook with a decision directive; Lean and the checker are stand-ins."""
    from glitnir.service import preview as source_preview

    calls: list = []

    def checked(source, **options):
        calls.append(("check", options))
        return dict(CHECKED)

    def imports_ok(source, **options):
        calls.append(("imports", options))
        return None

    providers = Providers(
        {},
        {"simulate": evidence_provider()},
        {"entailment": provider or _decision_provider()},
        {},
    )
    monkeypatch.setattr(source_preview, "load_providers", lambda: providers)
    for module in (source_preview, source_runs, preview_snapshot):
        monkeypatch.setattr(
            module, "environment_fingerprint", lambda *a, **k: {"sha256": FP}
        )
    monkeypatch.setattr(source_runs, "check_candidate", check or checked)
    monkeypatch.setattr(source_runs, "provider_imports_error", imports_ok)
    monkeypatch.setattr(
        source_runs,
        "run_provider_lean",
        lean or (lambda source, **options: {"error": "Lean is not here."}),
    )
    path = tmp_path / "A.lean"
    path.write_text(TEXT)
    return SourcePreviewService(path), calls


def _run(service, line=LINE) -> str:
    key = service.run_decision(sha(TEXT), line)
    settle(service)
    assert not service._runs._decision_worker.is_alive()
    return key


def _card(service) -> dict:
    [card] = service.snapshot()["decisions"]
    return card


def _saved(path: Path) -> dict:
    return (load_records(path) or {}).get("decisions", {})


def _saved_failures(path: Path) -> dict:
    """The failed runs saved beside the notebook, as a reader reads them."""
    return saved_decision_failures(load_records(path))


def _reopened(service) -> dict:
    """The card a new preview of the same notebook shows, before any run."""
    reopened = SourcePreviewService(service.path)
    try:
        return _card(reopened)
    finally:
        reopened.close()


def test_a_proven_decision_is_checked_and_saved_as_a_candidate(tmp_path, monkeypatch):
    seen: list = []

    def decide(request, cancel):
        seen.append((request, cancel))
        return _outcome()

    service, calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        key = _run(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["key"], card["status"], card["run_error"]) == (key, "candidate", None)
    assert card["outcome"]["block"] == BLOCK
    assert card["verification"] == CHECKED
    [(request, cancel)] = seen
    assert (request.name, request.kind) == ("band", "entailment")
    assert isinstance(cancel, threading.Event) and cancel is not service._cancel
    [imports, check] = calls
    assert imports[1]["imports"] == ("Demo.Decide",)
    assert imports[1]["needs"] == "decision"
    assert imports[1]["cancel"] is cancel
    assert check[1] == {
        "expected_sha256": sha(TEXT),
        "insert_after_line": END_LINE,
        "block": BLOCK,
        "theorem": "Decided_band.band",
        "cancel": cancel,
    }
    saved = json.loads(records_path(service.path).read_text())["decisions"][key]
    assert (saved["provider"], saved["fingerprint"]) == ("demo", FP)
    assert saved["created"].endswith("+00:00")
    assert service.path.read_text() == TEXT


def test_a_proven_decision_with_axioms_is_saved_with_their_names(tmp_path, monkeypatch):
    """The real verdict path: the checker's axiom records reach the card as names."""
    axioms = [
        {"name": "propext", "module": "Init.Core"},
        {"name": "Classical.choice", "module": "Init.Core"},
    ]
    seen: list = []

    def check_text(source, text, *, saved_sha256, cancel, progress, stages):
        seen.append(text)
        theorem = {
            "name": "Decided_band.band",
            "type": "True",
            "axioms": axioms,
            "status": "checked",
            "start_line": END_LINE + 2,
            "end_line": END_LINE + 2,
        }
        return {"status": "checked_snapshot", "theorems": [theorem]}

    monkeypatch.setattr(project_checking, "_check_text", check_text)
    service, _ = _service(tmp_path, monkeypatch, check=project_checking.check_candidate)
    try:
        key = _run(service)
        card = _card(service)
    finally:
        service.close()
    assert seen and seen[0].endswith(BLOCK + "\n")
    assert (card["status"], card["run_error"]) == ("candidate", None)
    assert card["verification"]["status"] == "checked"
    assert card["verification"]["axioms"] == ["propext", "Classical.choice"]
    saved = _saved(service.path)[key]
    assert saved["verification"]["axioms"] == ["propext", "Classical.choice"]


def test_a_refutation_must_name_the_refuted_theorem(tmp_path, monkeypatch):
    block = "theorem band_refuted : ¬ False := id"
    refuted = _outcome(status="refuted", block=block, theorem="band_refuted")
    verdict = {
        **CHECKED,
        "message": "The checker reports `band_refuted` as checked.",
        "type": "Not False",
    }

    def checked(source, **options):
        calls.append(("check", options))
        return dict(verdict)

    service, calls = _service(
        tmp_path,
        monkeypatch,
        _decision_provider(decide=lambda request, cancel: refuted),
        check=checked,
    )
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("candidate", None)
    assert calls[-1][1]["theorem"] == "band_refuted"
    assert calls[-1][1]["block"] == block
    assert card["verification"] == verdict


def test_an_unknown_outcome_is_saved_without_a_check(tmp_path, monkeypatch):
    def forbidden(source, **options):
        pytest.fail("an unknown outcome was checked")

    unknown = {"status": "unknown", "reason": "no certificate"}
    service, _calls = _service(
        tmp_path,
        monkeypatch,
        _decision_provider(decide=lambda request, cancel: unknown),
        check=forbidden,
    )
    try:
        key = _run(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("unknown", None)
    assert card["outcome"]["reason"] == "no certificate"
    assert card["verification"] is None
    assert _saved(service.path)[key]["verification"] is None


def test_a_rejected_candidate_is_saved_with_a_bounded_message(tmp_path, monkeypatch):
    def rejected(source, **options):
        return {"status": "rejected", "message": "m" * 5000, "axioms": [], "type": None}

    service, _calls = _service(tmp_path, monkeypatch, check=rejected)
    try:
        key = _run(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("rejected", None)
    assert card["verification"]["message"] == "m" * 2000
    assert _saved(service.path)[key]["verification"]["message"] == "m" * 2000


def test_a_long_verified_type_is_saved_visibly_shortened(tmp_path, monkeypatch):
    def checked(source, **options):
        return {**CHECKED, "type": "T" * 50_000}

    service, _calls = _service(tmp_path, monkeypatch, check=checked)
    try:
        key = _run(service)
        card = _card(service)
    finally:
        service.close()
    shortened = "T" * 19_999 + "…"
    assert (card["status"], card["run_error"]) == ("candidate", None)
    assert card["verification"]["type"] == shortened
    assert _saved(service.path)[key]["verification"]["type"] == shortened


@pytest.mark.parametrize(
    "outcome, message",
    [
        (_outcome(theorem="other"), "The provider proposed `other`, expected `band`."),
        (
            _outcome(theorem="band.lemma"),
            "The provider proposed `band.lemma`, expected `band`.",
        ),
        (
            _outcome(status="refuted"),
            "The provider proposed `Decided_band.band`, expected `band_refuted`.",
        ),
        (
            _outcome(theorem="band_refuted"),
            "The provider proposed `band_refuted`, expected `band`.",
        ),
    ],
)
def test_a_theorem_named_for_another_goal_is_refused(
    tmp_path, monkeypatch, outcome, message
):
    def forbidden(source, **options):
        pytest.fail("a misnamed candidate was checked")

    service, _calls = _service(
        tmp_path,
        monkeypatch,
        _decision_provider(decide=lambda request, cancel: outcome),
        check=forbidden,
    )
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("failed", message)
    assert _saved(service.path) == {}
    [saved] = _saved_failures(service.path).values()
    assert (saved["kind"], saved["message"]) == ("invalid", message)


class _ExpectedError(ValueError):
    """What a provider raises for a goal it cannot decide."""


@pytest.mark.parametrize(
    "decide, message, kind, saved_message",
    [
        (
            lambda request, cancel: 1 / 0,
            "Provider demo failed: ZeroDivisionError: division by zero",
            "provider",
            "Provider demo failed: ZeroDivisionError",
        ),
        (
            raises(ValueError("The goal has no variables.")),
            "The goal has no variables.",
            "refused",
            None,
        ),
        (
            raises(_ExpectedError("Bounds are numbers.")),
            "Bounds are numbers.",
            "refused",
            None,
        ),
        (
            raises(ValueError()),
            "Provider demo failed: ValueError: ",
            "refused",
            None,
        ),
        (
            raises(KeyError("x")),
            "Provider demo failed: KeyError: 'x'",
            "provider",
            "Provider demo failed: KeyError",
        ),
        (
            lambda request, cancel: {"status": "maybe"},
            "Provider demo returned an invalid decision: "
            "status must be proven, refuted or unknown.",
            "invalid",
            None,
        ),
        (
            lambda request, cancel: _outcome(block=f"{BLOCK}\n"),
            "Provider demo returned an invalid decision: "
            "The block may not end with a newline.",
            "invalid",
            None,
        ),
    ],
)
def test_provider_failures_become_the_card_run_error_and_are_saved(
    tmp_path, monkeypatch, decide, message, kind, saved_message
):
    """Saved beside the notebook; a new preview shows it, not "never"."""
    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        key = _run(service)
        card = _card(service)
    finally:
        service.close()
    # This session's own error is shown once, as the run error.
    assert (card["status"], card["run_error"], card["failure"]) == (
        "failed",
        message,
        None,
    )
    assert _saved(service.path) == {}
    [(saved_key, saved)] = _saved_failures(service.path).items()
    assert saved_key == key
    assert {k: v for k, v in saved.items() if k != "created"} == {
        "prefix_sha256": decision_prefix_sha256(TEXT, END_LINE),
        "fingerprint": FP,
        "provider": "demo",
        "kind": kind,
        "message": saved_message or message,
    }
    assert saved["created"].endswith("+00:00")
    reopened = _reopened(service)
    assert (reopened["status"], reopened["run_error"], reopened["outcome"]) == (
        "failed",
        None,
        None,
    )
    assert reopened["failure"] == {
        "kind": kind,
        "message": saved_message or message,
        "created": saved["created"],
        "status": "current",
    }


def test_a_candidate_check_reaching_its_time_limit_is_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", "90000")

    def timed_out(source, **options):
        raise SnapshotError(TIMED_OUT)

    service, _calls = _service(tmp_path, monkeypatch, check=timed_out)
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    message = "The candidate check reached its time limit of 90 s."
    assert (card["status"], card["run_error"]) == ("failed", message)
    assert _saved(service.path) == {}
    [saved] = _saved_failures(service.path).values()
    assert (saved["kind"], saved["message"]) == ("timeout", message)
    assert _reopened(service)["status"] == "failed"


@pytest.mark.parametrize(
    "message",
    [
        "Another Lean run is in progress; retry when it finishes.",
        "Waiting for another Lean run timed out.",
        "Preparing the project snapshot timed out.",
        "Kernel replay failed: unknown constant.",
        "Project check exceeded its output limit.",
        "The sandbox is not available on this system.",
        "Saved source changed; refresh and retry.",
    ],
)
def test_checker_failures_are_run_errors_not_verdicts(tmp_path, monkeypatch, message):
    def failing(source, **options):
        raise SnapshotError(message)

    service, _calls = _service(tmp_path, monkeypatch, check=failing)
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("never", message)
    assert not records_path(service.path).exists()
    assert _reopened(service)["status"] == "never"


def test_stop_during_decide_saves_nothing(tmp_path, monkeypatch):
    """Review Focus 5: Stop cancels the run and leaves no partial record."""
    started = threading.Event()

    def decide(request, cancel):
        started.set()
        assert cancel.wait(10)
        return _outcome()  # a provider that ignores the cancel still saves nothing

    def forbidden(source, **options):
        pytest.fail("a stopped decision was checked")

    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=decide), check=forbidden
    )
    try:
        service.run_decision(sha(TEXT), LINE)
        assert started.wait(10)
        assert _card(service)["status"] == "running"
        service.stop_decision()
        settle(service)
        card = _card(service)
        with pytest.raises(SnapshotError, match=f"^{NOTHING_TO_STOP}$"):
            service.stop_decision()
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("never", DECISION_CANCELLED)
    assert not records_path(service.path).exists()


def test_stop_during_the_candidate_check_saves_nothing(tmp_path, monkeypatch):
    started = threading.Event()

    def check(source, **options):
        started.set()
        assert options["cancel"].wait(10)
        raise SnapshotError("Project check cancelled.")

    service, _calls = _service(tmp_path, monkeypatch, check=check)
    try:
        service.run_decision(sha(TEXT), LINE)
        assert started.wait(10)
        service.stop_decision()
        settle(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("never", DECISION_CANCELLED)
    assert not records_path(service.path).exists()


@pytest.mark.parametrize("error", [RuntimeError("stopped"), ValueError("stopped")])
def test_a_provider_that_raises_on_cancel_reads_as_cancelled(
    tmp_path, monkeypatch, error
):
    started = threading.Event()

    def decide(request, cancel):
        started.set()
        assert cancel.wait(10)
        raise error

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        service.run_decision(sha(TEXT), LINE)
        assert started.wait(10)
        service.stop_decision()
        settle(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == DECISION_CANCELLED


def test_stop_after_the_run_commits_to_saving_finds_nothing_to_stop(
    tmp_path, monkeypatch
):

    saving, release = threading.Event(), threading.Event()
    save = source_runs.save_records

    def slow_save(path, records):
        saving.set()
        assert release.wait(10)
        save(path, records)

    monkeypatch.setattr(source_runs, "save_records", slow_save)
    service, _calls = _service(tmp_path, monkeypatch)
    try:
        service.run_decision(sha(TEXT), LINE)
        assert saving.wait(10)
        with pytest.raises(SnapshotError, match=f"^{NOTHING_TO_STOP}$"):
            service.stop_decision()
        release.set()
        settle(service)
        card = _card(service)
    finally:
        release.set()
        service.close()
    assert (card["status"], card["run_error"]) == ("candidate", None)


def test_stop_never_cancels_an_evidence_run(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def lean(source, **options):
        started.set()
        assert release.wait(10)
        return {"error": "Lean is not here."}

    service, _calls = _service(tmp_path, monkeypatch, lean=lean)
    try:
        service.run_evidence(sha(TEXT), 7)
        assert started.wait(10)
        with pytest.raises(SnapshotError, match=f"^{NOTHING_TO_STOP}$"):
            service.stop_decision()
        assert not service._cancel.is_set()
    finally:
        release.set()
        service.close()


def test_one_provider_run_at_a_time_across_evidence_and_decisions(
    tmp_path, monkeypatch
):
    started, release = threading.Event(), threading.Event()

    def lean(source, **options):
        started.set()
        assert release.wait(10)
        return {"error": "Lean is not here."}

    def decide(request, cancel):
        started.set()
        assert release.wait(10)
        return _outcome()

    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=decide), lean=lean
    )
    try:
        service.run_evidence(sha(TEXT), 7)
        assert started.wait(10)
        with pytest.raises(SnapshotError, match=f"^{RUN_BUSY}$"):
            service.run_decision(sha(TEXT), LINE)
        release.set()
        settle(service)
        release.clear()
        started.clear()
        service.run_decision(sha(TEXT), LINE)
        assert started.wait(10)
        with pytest.raises(SnapshotError, match=f"^{RUN_BUSY}$"):
            service.run_evidence(sha(TEXT), 7)
        with pytest.raises(SnapshotError, match=f"^{RUN_BUSY}$"):
            service.run_decision(sha(TEXT), LINE)
        release.set()
        settle(service)
        card = _card(service)
    finally:
        release.set()
        service.close()
    assert (card["status"], card["run_error"]) == ("candidate", None)


@pytest.mark.parametrize(
    "digest, line, message",
    [
        ("0" * 64, LINE, SOURCE_CHANGED),
        (None, 3, "No decision directive at line 3."),
        (None, END_LINE, "No decision directive at line 5."),
    ],
)
def test_runs_are_refused_without_deciding(
    tmp_path, monkeypatch, digest, line, message
):
    def forbidden(request, cancel):
        pytest.fail("the provider decided")

    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=forbidden)
    )
    try:
        with pytest.raises(SnapshotError) as raised:
            service.run_decision(digest or sha(TEXT), line)
    finally:
        service.close()
    assert str(raised.value) == message
    assert service._runs._decision_worker is None


def _rejecting(name, kind, goal):
    raise ValueError("Goals are written `entailment <context> |- <goal>`.")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"unavailable": lambda: "Install demo[z3]."}, "Install demo[z3]."),
        ({"parse": _rejecting}, "Goals are written `entailment <context> |- <goal>`."),
        (
            {"kinds": frozenset({"trajectory"})},
            "No installed provider decides `entailment` goals.",
        ),
    ],
)
def test_unavailable_or_rejected_directives_are_refused(
    tmp_path, monkeypatch, changes, message
):
    from glitnir.service import preview as source_preview

    provider = _decision_provider(**changes)
    service, _calls = _service(tmp_path, monkeypatch, provider)
    decisions = {kind: provider for kind in provider.kinds}
    monkeypatch.setattr(
        source_preview, "load_providers", lambda: Providers({}, {}, decisions, {})
    )
    try:
        with pytest.raises(SnapshotError) as raised:
            service.run_decision(sha(TEXT), LINE)
    finally:
        service.close()
    assert str(raised.value) == message


def test_source_saved_during_the_run_is_not_saved(tmp_path, monkeypatch):
    path = tmp_path / "A.lean"

    def decide(request, cancel):
        path.write_text(TEXT + "-- edited\n")
        return _outcome(status="unknown", block=None, theorem=None)

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        _run(service)
        path.write_text(TEXT)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == CHANGED_DURING_RUN
    assert not records_path(path).exists()


def test_a_provider_without_imports_decides_without_a_setup_step(tmp_path, monkeypatch):
    """The real import check prepares nothing when there is nothing to import."""
    from glitnir.lean import provider_lean

    def forbidden(*args, **kwargs):
        pytest.fail("the setup was prepared")

    service, calls = _service(tmp_path, monkeypatch, _decision_provider(imports=()))
    monkeypatch.setattr(
        source_runs, "provider_imports_error", provider_lean.provider_imports_error
    )
    monkeypatch.setattr(provider_lean, "inspect_project", forbidden)
    monkeypatch.setattr(provider_lean, "describe_project", forbidden)
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("candidate", None)
    assert [name for name, _options in calls] == ["check"]


def test_a_missing_provider_import_refuses_before_deciding(tmp_path, monkeypatch):
    """The real setup step refuses; neither the provider nor Lean runs."""
    from glitnir.lean import provider_lean

    def forbidden(*args, **kwargs):
        pytest.fail("the decision went ahead")

    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=forbidden), check=forbidden
    )
    monkeypatch.setattr(
        source_runs, "provider_imports_error", provider_lean.provider_imports_error
    )
    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    monkeypatch.setattr(
        provider_lean,
        "inspect_project",
        lambda path, *, expected_sha256, cancel, imports=(): {
            "status": "resolved",
            "setup": {
                "name": "A",
                "isModule": False,
                "importArts": {"Other": [["Other.olean"]]},
                "plugins": [],
                "dynlibs": [],
                "options": {},
            },
            "resolution": {"inputs": []},
        },
    )
    monkeypatch.setattr(provider_lean, "run_observer", forbidden)
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == (
        "This decision needs `import Demo.Decide` in the notebook."
    )
    assert not records_path(service.path).exists()


def test_discard_removes_only_the_saved_decision(tmp_path, monkeypatch):
    service, _calls = _service(tmp_path, monkeypatch)
    try:
        key = _run(service)
        assert _card(service)["status"] == "candidate"
        with pytest.raises(SnapshotError, match=f"^{SOURCE_CHANGED}$"):
            service.discard_decision("0" * 64, LINE)
        with pytest.raises(SnapshotError, match="^No decision directive at line 6.$"):
            service.discard_decision(sha(TEXT), 6)
        assert key in _saved(service.path)
        assert service.discard_decision(sha(TEXT), LINE) == key
        card = _card(service)
        with pytest.raises(
            SnapshotError, match="^No decision is saved for this directive.$"
        ):
            service.discard_decision(sha(TEXT), LINE)
    finally:
        service.close()
    assert (card["status"], card["outcome"], card["verification"]) == (
        "never",
        None,
        None,
    )
    assert "decisions" not in load_records(service.path)
    assert service.path.read_text() == TEXT


def test_a_running_decision_is_not_discarded(tmp_path, monkeypatch):
    started = threading.Event()
    answers = iter(["first", "second"])

    def decide(request, cancel):
        if next(answers) == "second":
            started.set()
            assert cancel.wait(10)
        return _outcome()

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        key = _run(service)
        before = records_path(service.path).read_text()
        service.run_decision(sha(TEXT), LINE)
        assert started.wait(10)
        with pytest.raises(SnapshotError, match=f"^{DECISION_RUNNING}$"):
            service.discard_decision(sha(TEXT), LINE)
        assert records_path(service.path).read_text() == before
        service.stop_decision()
        settle(service)
        assert records_path(service.path).read_text() == before
        assert service.discard_decision(sha(TEXT), LINE) == key
    finally:
        service.close()
    assert key not in _saved(service.path)


def test_discard_never_overwrites_an_unreadable_records_file(tmp_path, monkeypatch):
    service, _calls = _service(tmp_path, monkeypatch)
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    try:
        with pytest.raises(SnapshotError, match="cannot be read"):
            service.discard_decision(sha(TEXT), LINE)
    finally:
        service.close()
    assert sidecar.read_text() == "not json"


def test_close_cancels_the_decision_and_refuses_new_runs(tmp_path, monkeypatch):
    started = threading.Event()

    def decide(request, cancel):
        started.set()
        assert cancel.wait(10)
        return _outcome()

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    service.run_decision(sha(TEXT), LINE)
    assert started.wait(10)
    service.close()
    assert not service._runs._decision_worker.is_alive()
    assert not records_path(service.path).exists()
    with pytest.raises(SnapshotError, match="closed"):
        service.run_decision(sha(TEXT), LINE)
    with pytest.raises(SnapshotError, match="closed"):
        service.discard_decision(sha(TEXT), LINE)


def test_close_waits_for_all_workers_within_one_deadline(tmp_path, monkeypatch):

    class Slow:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def join(self, timeout: float) -> None:
            self.timeouts.append(timeout)

    clock = iter([100.0, 140.0, 160.0, 170.0])
    patch_service_name(
        monkeypatch, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    service, _calls = _service(tmp_path, monkeypatch)
    workers = [Slow(), Slow(), Slow()]
    service._worker, service._runs._evidence_worker, service._runs._decision_worker = (
        workers
    )
    service.close()
    assert [w.timeouts for w in workers] == [[15.0], [0.0], [0.0]]


def test_a_failing_fingerprint_is_computed_once_per_snapshot(
    tmp_path, monkeypatch, patch_fingerprint
):

    service, _calls = _service(tmp_path, monkeypatch)
    try:
        _run(service)
        calls: list[int] = []

        def failing(*args):
            calls.append(1)
            raise RuntimeError("git exploded")

        patch_fingerprint(failing)
        card = _card(service)
    finally:
        service.close()
    assert card["status"] == "stale"
    assert calls == [1]


def _refusal(service, key) -> str:
    with pytest.raises(SnapshotError) as raised:
        service.decision_candidate(key)
    return str(raised.value)


def test_only_a_current_checked_candidate_is_served_for_accepting(
    tmp_path, monkeypatch
):
    service, _calls = _service(tmp_path, monkeypatch)
    try:
        assert _refusal(service, "0" * 64) == NO_DECISION
        key = _card(service)["key"]
        assert _refusal(service, key) == NOT_DECIDED
        _run(service)
        served = service.decision_candidate(key)
        assert served == {
            "sha256": sha(TEXT),
            "insert_after_line": END_LINE,
            "block": BLOCK,
        }
        lines = TEXT.split("\n")
        accepted = "\n".join([*lines[:END_LINE], BLOCK, *lines[END_LINE:]])
        service.path.write_text(accepted)
        assert _card(service)["status"] == "accepted"
        assert _refusal(service, key) == NOT_ACCEPTABLE["accepted"]
        # Deleting the block makes it acceptable again; editing above it does not.
        service.path.write_text(TEXT)
        assert service.decision_candidate(key) == served
        service.path.write_text("-- note\n" + TEXT)
        assert _card(service)["status"] == "stale"
        assert _refusal(service, key) == NOT_ACCEPTABLE["stale"]
    finally:
        service.close()


def test_a_notebook_with_a_byte_order_mark_is_refused_for_accepting(
    tmp_path, monkeypatch
):
    # The editor's text drops the mark the saved digest includes, so no
    # candidate could ever match it; say why instead of "changed".
    service, _calls = _service(tmp_path, monkeypatch)
    try:
        marked = "\ufeff" + TEXT
        service.path.write_text(marked)
        key = _card(service)["key"]
        service.run_decision(sha(marked), LINE)
        settle(service)
        assert _card(service)["status"] == "candidate"
        assert _refusal(service, key) == BYTE_ORDER_MARK
    finally:
        service.close()


@pytest.mark.parametrize(
    "decide, check, status",
    [
        (lambda request, cancel: {"status": "unknown"}, None, "unknown"),
        (
            lambda request, cancel: _outcome(),
            lambda source, **options: {
                "status": "rejected",
                "message": "no",
                "axioms": [],
                "type": None,
            },
            "rejected",
        ),
    ],
)
def test_unknown_or_rejected_decisions_are_not_served(
    tmp_path, monkeypatch, decide, check, status
):
    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=decide), check=check
    )
    try:
        key = _run(service)
        assert _card(service)["status"] == status
        assert _refusal(service, key) == NOT_ACCEPTABLE[status]
    finally:
        service.close()
    assert service.path.read_text() == TEXT


def test_a_running_decision_is_not_served(tmp_path, monkeypatch):
    started = threading.Event()
    answers = iter(["first", "second"])

    def decide(request, cancel):
        if next(answers) == "second":
            started.set()
            assert cancel.wait(10)
        return _outcome()

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        key = _run(service)
        service.run_decision(sha(TEXT), LINE)
        assert started.wait(10)
        assert _refusal(service, key) == DECISION_RUNNING
        service.stop_decision()
        settle(service)
        assert service.decision_candidate(key)["block"] == BLOCK
    finally:
        service.close()


def test_live_decision_run_saves_a_checked_candidate(
    prepared_lean_project, monkeypatch
):
    """A real Lake notebook checks a demo provider's block in the sandbox."""
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain
    from glitnir.service import preview as source_preview

    root = prepared_lean_project
    toolchain = _toolchain()
    source = root / "src/Example.lean"
    text = "import Example.Helper\n\ndef c : Nat := helper\n-- @decide four : sum\n"
    source.write_text(text)
    subprocess.run(
        [str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(toolchain)},
    )

    def decide(request, cancel):
        return {
            "status": "proven",
            "block": f"theorem {request.name} : 2 + 2 = 4 := by decide",
            "theorem": request.name,
        }

    provider = DecisionProvider(
        name="demo",
        kinds=frozenset({"sum"}),
        imports=(),
        parse=_parse,
        decide=decide,
        unavailable=lambda: None,
    )
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({}, {}, {"sum": provider}, {}),
    )
    service = source_preview.SourcePreviewService(source)
    try:
        key = service.run_decision(sha(text), 4)
        settle(service, timeout=300)
        [card] = service.snapshot()["decisions"]
    finally:
        service.close()
    assert card["run_error"] is None, card
    assert (card["key"], card["status"]) == (key, "candidate")
    assert card["verification"]["status"] == "checked"
    assert card["verification"]["type"] == "Eq (instHAdd.hAdd 2 2) 4"
    assert key in load_records(source)["decisions"]
    assert source.read_text() == text


_UNKNOWN_GIT = {
    "sha256": FP,
    "packages": [{"name": "mathlib", "reason": "fatal: detected dubious ownership"}],
}
_UNKNOWN_NOTE = (
    "Could not read the git state of mathlib: fatal: detected dubious "
    "ownership; results are saved as out of date."
)


def test_decision_saved_under_unknown_git_state_explains_it(
    tmp_path, monkeypatch, patch_fingerprint
):
    """The decision is saved; the card's run error says why it is out of date."""
    service, _ = _service(tmp_path, monkeypatch)
    patch_fingerprint(lambda *a: _UNKNOWN_GIT)
    try:
        key = _run(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _UNKNOWN_NOTE
    assert key in _saved(tmp_path / "A.lean")


def test_a_decision_run_is_kept_awake_while_deciding_and_checking(
    tmp_path, monkeypatch, awake_depth
):
    """Deciding can take minutes before the candidate check: one held run."""
    depth = awake_depth(source_runs)
    held: list[tuple[str, int]] = []

    def decide(request, cancel):
        held.append(("decide", depth[0]))
        return _outcome()

    def check(source, **options):
        held.append(("check", depth[0]))
        return dict(CHECKED)

    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=decide), check=check
    )
    try:
        _run(service)
        assert _card(service)["status"] == "candidate"
    finally:
        service.close()
    assert held == [("decide", 1), ("check", 1)]
    assert depth == [0]


# ----- saved decision failures --------------------------------------------------

_REFUSAL = "The goal has no variables."


def _refusing(**changes) -> DecisionProvider:
    return _decision_provider(decide=raises(ValueError(_REFUSAL)), **changes)


@pytest.mark.parametrize(
    "error",
    [
        OSError("cannot open /opt/x/data.csv"),
        RuntimeError("GET https://api.example.test/v1/runs?token=s3cr3t failed"),
    ],
)
def test_a_provider_crash_saves_only_its_exception_type(tmp_path, monkeypatch, error):
    """The crash's text, which may name any path or URL, is never written."""
    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=raises(error))
    )
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    name = type(error).__name__
    assert card["run_error"] == f"Provider demo failed: {name}: {error}"
    [saved] = _saved_failures(service.path).values()
    assert (saved["kind"], saved["message"]) == (
        "provider",
        f"Provider demo failed: {name}",
    )
    written = records_path(service.path).read_text()
    for secret in ("/opt/x", "s3cr3t", "api.example.test"):
        assert secret not in written


def test_a_missing_or_broken_dependency_is_shown_but_not_saved(tmp_path, monkeypatch):
    """An ImportError (Z3 gone, say) is a setup failure, not the directive's."""
    missing = ModuleNotFoundError("No module named 'z3'")
    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=raises(missing))
    )
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == (
        "Provider demo failed: ModuleNotFoundError: No module named 'z3'"
    )
    assert (card["status"], _saved_failures(service.path)) == ("never", {})


class _Imports:
    """A provider whose `imports` raises: provider code, before any run."""

    name = "demo"
    kinds = frozenset({"entailment"})
    parse = staticmethod(_parse)
    decide = staticmethod(lambda request, cancel: _outcome())
    unavailable = staticmethod(lambda: None)

    @property
    def imports(self):
        raise RuntimeError("no imports today")


def test_setup_failures_before_deciding_are_not_saved(tmp_path, monkeypatch):
    """A missing provider import, or `imports` raising, is shown only."""
    service, _calls = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_runs,
        "provider_imports_error",
        lambda source, **options: {
            "error": f"Run `lake build Demo.Decide` in {tmp_path}.",
            "transient": True,
        },
    )
    try:
        _run(service)
        first = _card(service)
        job = service._runs._decision_job(sha(TEXT), LINE)
        broken = SimpleNamespace(**{**job.__dict__, "provider": _Imports()})
        error, saved = service._runs._decision_run(broken, threading.Event())
    finally:
        service.close()
    assert first["run_error"] == f"Run `lake build Demo.Decide` in {tmp_path}."
    assert (error, saved) == (
        "Provider demo failed: RuntimeError: no imports today",
        False,
    )
    assert not records_path(service.path).exists()


@pytest.mark.parametrize(
    "where",
    [
        lambda tmp_path: str(tmp_path / "goal.txt"),
        lambda tmp_path: str(tmp_path.resolve() / "goal.txt"),
        lambda tmp_path: str(Path.home() / "secret" / "goal.txt"),
    ],
)
def test_a_refusal_naming_a_local_path_is_shown_but_never_saved(
    tmp_path, monkeypatch, where
):
    path = where(tmp_path)
    provider = _decision_provider(decide=raises(ValueError(f"Cannot read {path}.")))
    service, _calls = _service(tmp_path, monkeypatch, provider)
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == f"Cannot read {path}."
    assert not records_path(service.path).exists()


@pytest.mark.parametrize(
    "environment",
    [{"sha256": FP, "unknown": True}, {"sha256": ""}],
)
def test_a_decision_failure_in_an_unknown_environment_is_not_saved(
    tmp_path, monkeypatch, environment, patch_fingerprint
):
    service, _calls = _service(tmp_path, monkeypatch, _refusing())
    patch_fingerprint(lambda *a: environment)
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _REFUSAL
    assert not records_path(service.path).exists()


def test_a_failure_whose_source_changed_during_the_run_is_not_saved(
    tmp_path, monkeypatch
):
    path = tmp_path / "A.lean"

    def decide(request, cancel):
        path.write_text(TEXT + "-- edited\n")
        raise ValueError(_REFUSAL)

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        _run(service)
        path.write_text(TEXT)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _REFUSAL
    assert not records_path(path).exists()


def test_a_failure_that_cannot_be_saved_still_shows_its_own_error(
    tmp_path, monkeypatch, caplog
):
    import logging

    service, _calls = _service(tmp_path, monkeypatch, _refusing())
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    caplog.set_level(logging.DEBUG, logger="glitnir.service.runs")
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == _REFUSAL
    assert sidecar.read_text() == "not json"
    [record] = [r for r in caplog.records if "not saved" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "decision" in record.getMessage() and "ValueError" in record.getMessage()
    assert _REFUSAL not in record.getMessage()


def test_a_failure_while_the_preview_closes_is_not_saved(tmp_path, monkeypatch):
    def decide(request, cancel):
        service._cancel.set()
        raise RuntimeError("boom")

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        _run(service)
    finally:
        service.close()
    assert not records_path(service.path).exists()


def test_stop_before_a_failure_is_saved_saves_nothing(tmp_path, monkeypatch):
    """Stop either wins or finds nothing to stop, as for a saved decision."""
    shortened = source_runs.failure_message

    def stop_first(message):
        service.stop_decision()
        return shortened(message)

    service, _calls = _service(tmp_path, monkeypatch, _refusing())
    monkeypatch.setattr(source_runs, "failure_message", stop_first)
    try:
        _run(service)
        card = _card(service)
        with pytest.raises(SnapshotError, match=f"^{NOTHING_TO_STOP}$"):
            service.stop_decision()
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("never", DECISION_CANCELLED)
    assert not records_path(service.path).exists()


def test_a_saved_decision_failure_goes_out_of_date_with_the_text_above_or_environment(
    tmp_path, monkeypatch, patch_fingerprint
):
    service, _calls = _service(tmp_path, monkeypatch, _refusing())
    try:
        _run(service)
    finally:
        service.close()
    assert _reopened(service)["status"] == "failed"
    service.path.write_text(TEXT + "def later := 2\n")
    assert _reopened(service)["status"] == "failed", "edits below change nothing"
    service.path.write_text(TEXT.replace("def x := 1", "def x := 2"))
    card = _reopened(service)
    assert (card["status"], card["failure"]["status"]) == ("stale", "stale")
    service.path.write_text(TEXT)
    patch_fingerprint(lambda *a: {"sha256": "9" * 64})
    card = _reopened(service)
    assert (card["status"], card["failure"]["status"]) == ("stale", "stale")


def test_a_failure_after_a_decision_keeps_the_candidate_and_a_decision_clears_it(
    tmp_path, monkeypatch
):
    answers = iter(["decide", "refuse", "decide"])

    def decide(request, cancel):
        if next(answers) == "refuse":
            raise ValueError(_REFUSAL)
        return _outcome()

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        key = _run(service)
        _run(service)
        failed = _reopened(service)
        # The checked candidate is still the one the editor may insert.
        served = service.decision_candidate(key)
        _run(service)
        cleared = _reopened(service)
    finally:
        service.close()
    assert (failed["status"], failed["outcome"]["block"]) == ("candidate", BLOCK)
    assert (failed["failure"]["status"], failed["failure"]["message"]) == (
        "current",
        _REFUSAL,
    )
    assert served["block"] == BLOCK
    assert (cleared["status"], cleared["failure"]) == ("candidate", None)
    assert _saved_failures(service.path) == {}


def test_an_out_of_date_failure_is_hidden_behind_a_current_decision(
    tmp_path, monkeypatch, patch_fingerprint
):
    answers = iter(["decide", "refuse"])

    def decide(request, cancel):
        if next(answers) == "refuse":
            raise ValueError(_REFUSAL)
        return _outcome()

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        _run(service)  # a candidate in FP
        patch_fingerprint(lambda *a: {"sha256": "9" * 64})
        _run(service)  # refused in another environment
        in_other = _reopened(service)
        patch_fingerprint(lambda *a: {"sha256": FP})
        in_first = _reopened(service)
    finally:
        service.close()
    assert (in_other["status"], in_other["failure"]["status"]) == ("stale", "current")
    assert (in_first["status"], in_first["failure"]) == ("candidate", None)


def test_discard_clears_the_saved_failure_with_the_decision(tmp_path, monkeypatch):
    answers = iter(["decide", "refuse"])

    def decide(request, cancel):
        if next(answers) == "refuse":
            raise ValueError(_REFUSAL)
        return _outcome()

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        key = _run(service)
        _run(service)
        assert set(_saved_failures(service.path)) == {key}
        assert service.discard_decision(sha(TEXT), LINE) == key
        card = _card(service)
    finally:
        service.close()
    assert (card["status"], card["failure"], card["run_error"]) == ("never", None, None)
    assert _saved_failures(service.path) == {}


def test_an_unsaved_failure_keeps_the_saved_one_readable_as_earlier(
    tmp_path, monkeypatch
):
    """A busy checker after a saved failure says nothing new about the directive."""
    busy = "Another Lean run is in progress; retry when it finishes."
    answers = iter(["refuse", "busy"])

    def decide(request, cancel):
        if next(answers) == "refuse":
            raise ValueError(_REFUSAL)
        return _outcome()

    def check(source, **options):
        raise SnapshotError(busy)

    service, _calls = _service(
        tmp_path, monkeypatch, _decision_provider(decide=decide), check=check
    )
    try:
        _run(service)
        saved_now = _card(service)
        _run(service)
        after_busy = _card(service)
    finally:
        service.close()
    assert (saved_now["run_error"], saved_now["failure"]) == (_REFUSAL, None)
    assert (after_busy["status"], after_busy["run_error"]) == ("failed", busy)
    assert after_busy["failure"]["message"] == _REFUSAL
    assert after_busy["failure"]["earlier"] is True


def test_an_accepted_decision_hides_a_failure_saved_after_its_candidate(
    tmp_path, monkeypatch
):
    """Review I1: candidate, a failing rerun, then Accept: no failure note."""
    from glitnir.service.static_export import SavedNotebook

    answers = iter(["decide", "refuse"])

    def decide(request, cancel):
        if next(answers) == "refuse":
            raise ValueError(_REFUSAL)
        return _outcome()

    service, _calls = _service(tmp_path, monkeypatch, _decision_provider(decide=decide))
    try:
        key = _run(service)
        _run(service)
        assert _card(service)["status"] == "candidate"
        served = service.decision_candidate(key)
        lines = TEXT.split("\n")
        accepted = "\n".join([*lines[:END_LINE], served["block"], *lines[END_LINE:]])
        service.path.write_text(accepted)
        live = _card(service)
    finally:
        service.close()
    reopened = _reopened(service)
    with SavedNotebook(service.path) as notebook:
        [recorded] = notebook.snapshot()["decisions"]
    assert set(_saved_failures(service.path)) == {key}, "the failure stays saved"
    for card in (live, reopened, recorded):
        assert (card["status"], card["failure"]) == ("accepted", None)


@pytest.mark.parametrize(
    "message",
    [
        "Cannot load /opt/homebrew/lib/libz3.dylib.",
        "Cannot read '/usr/local/share/goal.txt'.",
        "Cannot read C:\\Users\\me\\goal.txt.",
        "Cannot read D:/data/goal.txt.",
        "Cannot read ~/goals/goal.txt.",
        "Cannot read file:///srv/goals/goal.txt.",
    ],
)
def test_a_refusal_naming_any_absolute_path_is_shown_but_never_saved(
    tmp_path, monkeypatch, message
):
    """Not only the directories Glitnir knows: no path reaches committed records."""
    provider = _decision_provider(decide=raises(ValueError(message)))
    service, _calls = _service(tmp_path, monkeypatch, provider)
    try:
        _run(service)
        card = _card(service)
    finally:
        service.close()
    assert card["run_error"] == message
    assert not records_path(service.path).exists()


@pytest.mark.parametrize(
    "message",
    [
        "Bounds are 1/2 and 3/4, not 1/2/3.",
        "x / y / z is not linear.",
        "See https://example.test/docs/decide for the goal syntax.",
        "Write (a/b)/c as a/(b*c).",
    ],
)
def test_a_refusal_without_a_path_is_saved_unchanged(tmp_path, monkeypatch, message):
    provider = _decision_provider(decide=raises(ValueError(message)))
    service, _calls = _service(tmp_path, monkeypatch, provider)
    try:
        _run(service)
    finally:
        service.close()
    [saved] = _saved_failures(service.path).values()
    assert (saved["kind"], saved["message"]) == ("refused", message)
