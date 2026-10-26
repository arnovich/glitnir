"""Saved evidence failures: failed runs are kept and read back on reopening."""

from pathlib import Path

import pytest
from conftest import (
    EVIDENCE_TEXT,
    FP,
    asking_lean_every_time,
    evidence_card,
    evidence_service,
    evidence_sha,
    raises,
    recording_evidence_provider,
    run_evidence,
    saved_failures,
)

from glitnir.lean.project_inspection import TOOLCHAIN
from glitnir.service import runs as source_runs
from glitnir.service.preview import SourcePreviewService


def _reopened(service, line=4):
    """The card a new preview of the same notebook shows, before any run."""
    reopened = SourcePreviewService(service.path)
    try:
        return evidence_card(reopened, line)
    finally:
        reopened.close()


_LEAN_ERROR = {"error": "Line 2: unknown constant 'x'", "lean": True}


_TIMED_OUT = {"error": "Project check timed out.", "transient": True, "timed_out": True}


@pytest.mark.parametrize(
    "result, kind, message",
    [
        (_LEAN_ERROR, "lean", "Line 2: unknown constant 'x'"),
        (
            {"error": "The provider printed no evidence.", "lean": True},
            "lean",
            "The provider printed no evidence.",
        ),
        (
            {"payload": "{not json"},
            "invalid",
            "Provider demo printed evidence that is not JSON.",
        ),
        (_TIMED_OUT, "timeout", "The run reached its time limit of 90 s."),
    ],
)
def test_each_lean_side_failure_is_saved_with_its_kind(
    tmp_path, monkeypatch, result, kind, message
):
    """Saved beside the notebook; a new preview shows it, not "never"."""
    from glitnir.notebook.check_records import evidence_prefix_sha256

    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", "90000")
    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(result)
    )
    try:
        key = run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    # This session's own error is shown once, as the run error.
    assert (card["status"], card["run_error"], card["failure"]) == (
        "failed",
        message,
        None,
    )
    [(saved_key, saved)] = saved_failures(service).items()
    assert saved_key == key
    assert {k: v for k, v in saved.items() if k != "created"} == {
        "prefix_sha256": evidence_prefix_sha256(EVIDENCE_TEXT, 4),
        "fingerprint": FP,
        "provider": "demo",
        "kind": kind,
        "message": message,
    }
    assert saved["created"].endswith("+00:00")
    reopened = _reopened(service)
    assert (reopened["status"], reopened["run_error"], reopened["snapshot"]) == (
        "failed",
        None,
        None,
    )
    assert reopened["failure"] == {
        "kind": kind,
        "message": message,
        "created": saved["created"],
        "status": "current",
    }


def test_a_long_failure_message_is_saved_cut_to_the_limit(tmp_path, monkeypatch):
    from glitnir.notebook.check_records import MAX_MESSAGE

    long = {"error": "Line 2: " + "x" * 3000, "lean": True}
    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(long)
    )
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == long["error"]
    [saved] = saved_failures(service).values()
    assert len(saved["message"]) == MAX_MESSAGE and saved["message"].endswith("…")


@pytest.mark.parametrize(
    "result",
    [
        {"error": "Evidence run cancelled.", "transient": True},
        {"error": "Another Lean run is in progress; retry when it finishes."},
        {"error": "Waiting for another Lean run timed out.", "transient": True},
        {"error": "Evidence needs a Lake project; see the README."},
        {"error": "Project inspection timed out."},
        {"error": "Lake could not read the project configuration."},
        {"error": "The sandbox is not available on this system."},
        {"error": "This evidence needs `import Demo.Evidence` in the notebook."},
        {"error": "Project check exceeded its output limit."},
    ],
)
def test_failures_that_say_nothing_about_the_directive_are_not_saved(
    tmp_path, monkeypatch, result
):
    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(result)
    )
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("never", result["error"])
    assert saved_failures(service) == {}
    assert _reopened(service)["status"] == "never"


def test_a_setup_error_naming_a_local_path_is_never_saved(tmp_path, monkeypatch):
    """Lake's "build these modules" advice names the project root: not saved."""
    from glitnir.lean import provider_lean
    from glitnir.notebook.check_records import records_path

    service, _calls = evidence_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        source_runs, "run_provider_lean", provider_lean.run_provider_lean
    )
    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    (tmp_path / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setattr(provider_lean, "_toolchain", lambda: toolchain)
    own = {
        "name": "A",
        "isModule": False,
        "importArts": {},
        "plugins": [],
        "dynlibs": [],
        "options": {},
    }

    def inspect(path, *, expected_sha256, cancel, imports=()):
        if imports:  # the provider's module is not built
            return {"status": "unresolved", "needs_build": True, "message": "x"}
        return {"status": "resolved", "setup": own, "resolution": {"inputs": []}}

    monkeypatch.setattr(provider_lean, "inspect_project", inspect)
    monkeypatch.setattr(
        provider_lean, "run_observer", lambda *a, **k: pytest.fail("Lean ran")
    )
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    root = str(tmp_path.resolve())
    assert root in card["run_error"] and "lake build Demo.Evidence" in card["run_error"]
    assert not records_path(service.path).exists()


@pytest.mark.parametrize(
    "where",
    [
        lambda tmp_path: str(tmp_path / "data.csv"),
        lambda tmp_path: str(tmp_path.resolve() / "data.csv"),
        lambda tmp_path: str(Path.home() / "secret" / "data.csv"),
    ],
)
def test_a_failure_naming_a_local_path_is_shown_but_never_saved(
    tmp_path, monkeypatch, where
):
    """A provider's message is saved into committed records, so no path is."""
    path = where(tmp_path)
    provider = recording_evidence_provider(
        compute=raises(ValueError(f"Cannot read {path}."))
    )
    service, _calls = evidence_service(tmp_path, monkeypatch, provider)
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == f"Cannot read {path}."
    assert saved_failures(service) == {}


@pytest.mark.parametrize(
    "message, saved",
    [
        ("Cannot read /srv/data/x.csv.", False),
        ("Cannot read C:\\data\\x.csv.", False),
        ("Cannot read ~/data/x.csv.", False),
        ("The step 1/100 is too large for 3/4 of the run.", True),
    ],
)
def test_an_evidence_refusal_is_saved_only_without_an_absolute_path(
    tmp_path, monkeypatch, message, saved
):
    provider = recording_evidence_provider(compute=raises(ValueError(message)))
    service, _calls = evidence_service(tmp_path, monkeypatch, provider)
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == message
    failures = saved_failures(service)
    assert [f["message"] for f in failures.values()] == ([message] if saved else [])


@pytest.mark.parametrize(
    "environment",
    [
        {"sha256": FP, "unknown": True, "packages": []},
        {"sha256": "", "packages": []},
    ],
)
def test_a_failure_in_an_unknown_environment_is_not_saved(
    tmp_path, monkeypatch, environment, patch_fingerprint
):

    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    patch_fingerprint(lambda *a: environment)
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == _LEAN_ERROR["error"]
    assert saved_failures(service) == {}


def test_a_failure_while_the_preview_closes_is_not_saved(tmp_path, monkeypatch):
    def run(source, **options):
        options["cancel"].set()
        return dict(_LEAN_ERROR)

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        run_evidence(service)
    finally:
        service.close()
    assert saved_failures(service) == {}


def test_a_refused_start_saves_nothing(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.notebook.check_records import records_path
    from glitnir.service.runs import RUN_BUSY

    service, _calls = evidence_service(tmp_path, monkeypatch)
    try:
        with service._runs._run_lock:
            with pytest.raises(SnapshotError, match=RUN_BUSY):
                service.run_evidence(evidence_sha(), 4)
    finally:
        service.close()
    assert not records_path(service.path).exists()


def test_a_failure_whose_source_changed_during_the_run_is_not_saved(
    tmp_path, monkeypatch
):
    path = tmp_path / "A.lean"

    def run(source, **options):
        path.write_text(EVIDENCE_TEXT + "-- edited\n")
        return dict(_LEAN_ERROR)

    service, _calls = evidence_service(tmp_path, monkeypatch, runner=run)
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == _LEAN_ERROR["error"]
    assert saved_failures(service) == {}


def test_a_failure_that_cannot_be_saved_still_shows_its_own_error(
    tmp_path, monkeypatch
):
    from glitnir.notebook.check_records import records_path

    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    sidecar = records_path(service.path)
    sidecar.write_text("not json")
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == _LEAN_ERROR["error"]
    assert sidecar.read_text() == "not json"


def test_a_saved_failure_goes_out_of_date_with_the_text_above_or_the_environment(
    tmp_path, monkeypatch, patch_fingerprint
):

    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    try:
        run_evidence(service)
    finally:
        service.close()
    assert _reopened(service)["status"] == "failed"
    service.path.write_text(EVIDENCE_TEXT + "def later := 2\n")
    assert _reopened(service)["status"] == "failed", "edits below change nothing"
    service.path.write_text(EVIDENCE_TEXT.replace("def M := 1", "def M := 2"))
    card = _reopened(service)
    assert (card["status"], card["failure"]["status"]) == ("stale", "stale")
    service.path.write_text(EVIDENCE_TEXT)
    patch_fingerprint(lambda *a: {"sha256": "9" * 64})
    card = _reopened(service)
    assert (card["status"], card["failure"]["status"]) == ("stale", "stale")


def test_a_failure_after_a_success_keeps_the_snapshot_and_a_success_clears_it(
    tmp_path, monkeypatch
):
    from glitnir.notebook.check_records import load_records, saved_evidence

    outcomes = iter([{"payload": "{}"}, dict(_LEAN_ERROR), {"payload": "{}"}])
    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(outcomes)
    )
    asking_lean_every_time(monkeypatch)
    try:
        key = run_evidence(service)
        run_evidence(service)
        failed = _reopened(service)
        run_evidence(service)
        cleared = _reopened(service)
    finally:
        service.close()
    assert (failed["status"], failed["snapshot"] is not None) == ("current", True)
    assert failed["failure"]["status"] == "current"
    assert failed["failure"]["message"] == _LEAN_ERROR["error"]
    assert (cleared["status"], cleared["failure"]) == ("current", None)
    assert key in saved_evidence(load_records(service.path))
    assert saved_failures(service) == {}


def test_an_out_of_date_failure_is_hidden_behind_a_current_snapshot(
    tmp_path, monkeypatch, patch_fingerprint
):

    outcomes = iter([{"payload": "{}"}, dict(_LEAN_ERROR)])
    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(outcomes)
    )
    other = {"sha256": "9" * 64}
    try:
        run_evidence(service)  # current in FP
        patch_fingerprint(lambda *a: other)
        run_evidence(service)  # fails in another environment
        in_other = _reopened(service)
        patch_fingerprint(lambda *a: {"sha256": FP})
        in_first = _reopened(service)
    finally:
        service.close()
    assert (in_other["status"], in_other["failure"]["status"]) == ("stale", "current")
    assert (in_first["status"], in_first["failure"]) == ("current", None)


def test_uncompared_results_mark_saved_failures_uncompared():
    from glitnir.preview.snapshot import uncompared as _uncompared

    failure = {"kind": "lean", "message": "m", "created": "c", "status": "stale"}
    card = {"status": "stale", "snapshot": None, "failure": dict(failure)}
    running = {"status": "running", "snapshot": None, "failure": dict(failure)}
    snapshot = _uncompared(
        {"project_claims": [], "evidence": [card, running], "decisions": []}
    )
    assert [c["status"] for c in snapshot["evidence"]] == ["uncompared", "running"]
    assert [c["failure"]["status"] for c in snapshot["evidence"]] == ["uncompared"] * 2


@pytest.mark.parametrize(
    "error",
    [
        OSError("cannot open /opt/x/data.csv"),
        FileNotFoundError(2, "No such file or directory", "/Volumes/y/model.json"),
        RuntimeError("GET https://api.example.test/v1/runs?token=s3cr3t failed"),
    ],
)
@pytest.mark.parametrize("where", ["command", "compute"])
def test_a_provider_crash_saves_only_its_exception_type(
    tmp_path, monkeypatch, error, where
):
    """The crash's text, which may name any path or URL, is never written."""
    from glitnir.notebook.check_records import records_path

    def crash(*_arguments):
        raise error

    service, _calls = evidence_service(
        tmp_path, monkeypatch, recording_evidence_provider(**{where: crash})
    )
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    kind = type(error).__name__
    assert card["run_error"] == f"Provider demo failed: {kind}: {error}"
    [saved] = saved_failures(service).values()
    assert (saved["kind"], saved["message"]) == (
        "provider",
        f"Provider demo failed: {kind}",
    )
    written = records_path(service.path).read_text()
    for secret in ("/opt/x", "/Volumes/y", "s3cr3t", "api.example.test"):
        assert secret not in written


@pytest.mark.parametrize("where", ["command", "compute"])
def test_a_missing_or_broken_dependency_is_shown_but_not_saved(
    tmp_path, monkeypatch, where
):
    """An ImportError is a setup failure: it says nothing about the directive."""

    def missing(*_arguments):
        raise ModuleNotFoundError("No module named 'gimle.asgard'")

    service, _calls = evidence_service(
        tmp_path, monkeypatch, recording_evidence_provider(**{where: missing})
    )
    try:
        run_evidence(service)
        card = evidence_card(service)
    finally:
        service.close()
    assert card["run_error"] == (
        "Provider demo failed: ModuleNotFoundError: No module named 'gimle.asgard'"
    )
    assert (card["status"], saved_failures(service)) == ("never", {})


def test_an_unsaved_failure_is_logged_without_its_message(
    tmp_path, monkeypatch, caplog
):
    import logging

    from glitnir.notebook.check_records import records_path

    secret = str(tmp_path / "private.csv")
    provider = recording_evidence_provider(
        compute=raises(ValueError(f"Cannot read {secret}"))
    )
    service, _calls = evidence_service(tmp_path, monkeypatch, provider)
    caplog.set_level(logging.DEBUG, logger="glitnir.service.runs")
    try:
        run_evidence(service)
    finally:
        service.close()
    assert not records_path(service.path).exists()
    [record] = [r for r in caplog.records if "not saved" in r.getMessage()]
    assert "local path" in record.getMessage()
    assert all(secret not in r.getMessage() for r in caplog.records)


def test_a_failure_that_cannot_be_written_is_logged_by_its_error_type(
    tmp_path, monkeypatch, caplog
):
    import logging

    from glitnir.notebook.check_records import records_path

    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: dict(_LEAN_ERROR)
    )
    records_path(service.path).write_text("not json")
    caplog.set_level(logging.DEBUG, logger="glitnir.service.runs")
    try:
        run_evidence(service)
    finally:
        service.close()
    [record] = [r for r in caplog.records if "not saved" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "ValueError" in record.getMessage()
    assert "A.glitnir.json" not in record.getMessage()


def test_an_unsaved_failure_keeps_the_saved_one_readable_as_earlier(
    tmp_path, monkeypatch
):
    """A busy slot after a saved failure says nothing new about the directive."""
    busy = {"error": "Another Lean run is in progress; retry when it finishes."}
    outcomes = iter([dict(_LEAN_ERROR), busy, dict(_LEAN_ERROR), busy])
    service, _calls = evidence_service(
        tmp_path, monkeypatch, runner=lambda source, **options: next(outcomes)
    )
    try:
        run_evidence(service)
        saved_now = evidence_card(service)
        run_evidence(service)
        after_busy = evidence_card(service)
    finally:
        service.close()
    # Saved by this session's run: shown once, as the run error.
    assert (saved_now["run_error"], saved_now["failure"]) == (
        _LEAN_ERROR["error"],
        None,
    )
    # Not saved: the session's error is the latest, the saved one is earlier.
    assert (after_busy["status"], after_busy["run_error"]) == ("failed", busy["error"])
    assert after_busy["failure"]["message"] == _LEAN_ERROR["error"]
    assert after_busy["failure"]["earlier"] is True
    reopened = SourcePreviewService(service.path)
    try:
        run_evidence(reopened)  # saves the same failure again
        assert evidence_card(reopened)["failure"] is None
        run_evidence(reopened)  # busy
        assert evidence_card(reopened)["failure"]["earlier"] is True
    finally:
        reopened.close()
