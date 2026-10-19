"""The shared provider Lean runner serves views and, later, evidence."""

import hashlib
import json
import threading
from pathlib import Path

import pytest

NOTEBOOK = "import Demo.View\n\ndef c := 1\n-- @view circuit c\ndef later := 2\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _lake(tmp_path, monkeypatch, import_arts):
    """A Lake notebook whose inspected setup provides `import_arts`."""
    from glitnir.lean import provider_lean

    (tmp_path / "lakefile.toml").write_text('name = "demo"\n')
    source = tmp_path / "Note.lean"
    source.write_text(NOTEBOOK)
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setattr(provider_lean, "_toolchain", lambda: toolchain)
    monkeypatch.setattr(
        provider_lean,
        "inspect_project",
        lambda path, *, expected_sha256, cancel, imports=(): {
            "status": "resolved",
            "setup": {
                "name": "Note",
                "isModule": False,
                "importArts": import_arts,
                "plugins": [],
                "dynlibs": [],
                "options": {},
            },
            "resolution": {"inputs": []},
        },
    )
    return source


def _reporting(monkeypatch, report):
    """Replace the sandbox with one that writes `report` to the report path."""
    from glitnir.lean import provider_lean

    def run(runtime, arguments, **options):
        Path(arguments[-1]).write_text(json.dumps(report))

    monkeypatch.setattr(provider_lean, "run_observer", run)


def _forbid_lean(monkeypatch):
    from glitnir.lean import provider_lean

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run")

    monkeypatch.setattr(provider_lean, "run_observer", run)


def _run(source, **overrides):
    from glitnir.lean.provider_lean import run_provider_lean

    arguments = {
        "expected_sha256": _sha(NOTEBOOK),
        "line": 4,
        "provider_name": "demo",
        "imports": ("Demo.View",),
        "preamble": "open Demo",
        "command": "#demo_view circuit c",
        "marker": "demoMark:",
        "needs": "evidence",
        "cancel": threading.Event(),
        **overrides,
    }
    return run_provider_lean(source, **arguments)


def test_compose_puts_provider_imports_first_and_command_last():
    from glitnir.lean.provider_lean import provider_source_parts

    notebook, preamble, command = provider_source_parts(
        NOTEBOOK,
        4,
        ("Demo.View",),
        "open Demo",
        "#demo_view circuit c a",
        needs="evidence",
    )
    assert notebook == "import Demo.View\nimport Demo.View\n\ndef c := 1\n\n"
    assert preamble == "open Demo\n"
    assert command == "#demo_view circuit c a\n"


@pytest.mark.parametrize("line", [0, 7])
@pytest.mark.parametrize("needs, word", [("view", "View"), ("evidence", "Evidence")])
def test_compose_rejects_a_line_outside_the_notebook(line, needs, word):
    from glitnir.lean.provider_lean import provider_source_parts

    message = f"^{word} directive line {line} is outside the notebook.$"
    with pytest.raises(ValueError, match=message):
        provider_source_parts(NOTEBOOK, line, (), "", "", needs=needs)


def test_marker_payload_is_the_last_marked_message_and_unvalidated():
    from glitnir.lean.provider_lean import _marker_payload

    report = {"information": ["m:one", "other", "m:<html/>"], "errors": []}
    assert _marker_payload(report, "m:", 0, 4, "view") == {"payload": "<html/>"}


def test_marker_payload_wins_over_errors():
    from glitnir.lean.provider_lean import _marker_payload

    report = {"information": ["m:ok"], "errors": [{"line": 3, "text": "x"}]}
    assert _marker_payload(report, "m:", 1, 4, "view") == {"payload": "ok"}


@pytest.mark.parametrize(
    "needs, message",
    [
        ("view", "The provider printed no drawing."),
        ("evidence", "The provider printed no evidence."),
    ],
)
def test_no_marker_and_no_errors_is_an_error(needs, message):
    from glitnir.lean.provider_lean import _marker_payload

    report = {"information": ["hello"], "errors": []}
    assert _marker_payload(report, "m:", 0, 4, needs) == {"error": message}


@pytest.mark.parametrize(
    "position, expected",
    [
        (3, "Line 2: unknown c\nmore detail"),  # after one provider import
        (7, "Line 4: unknown c\nmore detail"),  # the provider's: the directive
        (None, "unknown c\nmore detail"),
    ],
)
def test_first_error_names_its_notebook_line(position, expected):
    from glitnir.lean.provider_lean import _marker_payload

    error = {"text": "unknown c\nmore detail"}
    if position is not None:
        error["line"] = position
    report = {"information": [], "errors": [error, {"line": 1, "text": "later"}]}
    assert _marker_payload(report, "m:", 1, 4, "view") == {"error": expected}


def test_an_ambiguous_names_interpretations_are_shown():
    """Lean's first line hides the cause; blank lines are left out."""
    from glitnir.lean.provider_lean import _marker_payload

    # Lean 4.32.1's message (Lean/Elab/App.lean) for a preamble line.
    ambiguous = (
        "Ambiguous term\n  Circuit\nPossible interpretations:\n"
        "  Dynamics.Circuit n m : Type\n  \n  Gimle.Asgard.Circuit n m : Type"
    )
    report = {"information": [], "errors": [{"line": 99, "text": ambiguous}]}
    assert _marker_payload(report, "m:", 3, 69, "view") == {
        "error": "Line 69: Ambiguous term\n  Circuit\nPossible interpretations:\n"
        "  Dynamics.Circuit n m : Type\n  Gimle.Asgard.Circuit n m : Type"
    }


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "\n".join(f"line {n}" for n in range(30)),
            "\n".join(f"line {n}" for n in range(12)) + "\n…",
        ),
        ("x" * 5000, "x" * 2000 + "\n…"),
        ("first\n" + "y" * 3000, "first\n" + "y" * 1994 + "\n…"),
        (" \n\t\n", "error"),
    ],
)
def test_an_error_is_cut_to_a_bounded_text(text, expected):
    from glitnir.lean.provider_lean import (
        ERROR_CHARACTERS,
        ERROR_LINES,
        _marker_payload,
    )

    assert (ERROR_LINES, ERROR_CHARACTERS) == (12, 2000)
    report = {"information": [], "errors": [text]}
    assert _marker_payload(report, "m:", 0, 4, "evidence") == {"error": expected}


@pytest.mark.parametrize(
    "errors", [[{"line": 1}], [{"line": "1", "text": "x"}], [3], "x"]
)
@pytest.mark.parametrize("needs", ["view", "evidence"])
def test_malformed_error_entries_are_rejected(errors, needs):
    from glitnir.lean.provider_lean import _marker_payload

    report = {"information": [], "errors": errors}
    with pytest.raises(ValueError, match=f"^Malformed {needs} report.$"):
        _marker_payload(report, "m:", 0, 4, needs)


@pytest.mark.parametrize("needs", ["view", "evidence"])
def test_malformed_information_is_rejected(needs):
    from glitnir.lean.provider_lean import _marker_payload

    with pytest.raises(ValueError, match=f"^Malformed {needs} report.$"):
        _marker_payload({"information": "m:x", "errors": []}, "m:", 0, 4, needs)


def test_runner_returns_the_payload_for_the_caller_to_validate(tmp_path, monkeypatch):
    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    _reporting(monkeypatch, {"information": ['demoMark:{"a":1}'], "errors": []})
    assert _run(source) == {"payload": '{"a":1}'}


@pytest.mark.parametrize(
    "needs, message",
    [
        ("view", "This view needs the module `Demo.View`, which no package"),
        ("evidence", "This evidence needs the module `Demo.View`, which no package"),
    ],
)
def test_missing_import_is_worded_for_the_caller(tmp_path, monkeypatch, needs, message):
    source = _lake(tmp_path, monkeypatch, {})
    _forbid_lean(monkeypatch)
    assert _run(source, needs=needs)["error"].startswith(message)


@pytest.mark.parametrize(
    "needs, message",
    [
        ("view", "Views need a Lake project; see the README."),
        ("evidence", "Evidence needs a Lake project; see the README."),
    ],
)
def test_loose_files_need_a_lake_project(tmp_path, monkeypatch, needs, message):
    from glitnir.lean import provider_lean

    source = tmp_path / "Note.lean"
    source.write_text(NOTEBOOK)
    monkeypatch.setattr(provider_lean, "_toolchain", lambda: tmp_path)
    _forbid_lean(monkeypatch)
    assert _run(source, needs=needs)["error"].startswith(message)


def test_composing_failures_name_the_provider(tmp_path, monkeypatch):
    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    _forbid_lean(monkeypatch)
    result = _run(source, preamble=None)
    assert result["error"].startswith("Provider demo failed: TypeError: ")


def test_invalid_import_names_are_rejected_before_composing(tmp_path, monkeypatch):
    source = _lake(tmp_path, monkeypatch, {})
    _forbid_lean(monkeypatch)
    assert _run(source, imports=("Lean.Elab\n#eval 1",)) == {
        "error": "The provider declares an invalid import `Lean.Elab\n#eval 1`."
    }


def test_changed_source_never_launches_lean(tmp_path, monkeypatch):
    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    _forbid_lean(monkeypatch)
    assert "changed" in _run(source, expected_sha256=_sha("other"))["error"]


def test_source_edited_during_the_run_is_an_error(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    def run(runtime, arguments, **options):
        Path(arguments[-1]).write_text(
            json.dumps({"information": ["demoMark:x"], "errors": []})
        )
        source.write_text(NOTEBOOK + "-- edited\n")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert "changed" in _run(source)["error"]


def test_cancellation_is_transient_and_worded_for_the_caller(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    cancel = threading.Event()

    def run(runtime, arguments, **options):
        cancel.set()

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert _run(source, cancel=cancel) == {
        "error": "Evidence run cancelled.",
        "transient": True,
    }
    assert _run(source, needs="view", cancel=cancel) == {
        "error": "View drawing cancelled.",
        "transient": True,
    }


def _imports_error(source, **overrides):
    from glitnir.lean.provider_lean import provider_imports_error

    arguments = {
        "expected_sha256": _sha(NOTEBOOK),
        "imports": ("Demo.View",),
        "needs": "decision",
        "cancel": threading.Event(),
        **overrides,
    }
    return provider_imports_error(source, **arguments)


def test_imports_the_setup_provides_are_no_error(tmp_path, monkeypatch):
    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    _forbid_lean(monkeypatch)
    assert _imports_error(source, imports=("Demo.View", "Lean.Elab")) is None


def test_missing_imports_are_named_before_anything_runs(tmp_path, monkeypatch):
    source = _lake(tmp_path, monkeypatch, {})
    _forbid_lean(monkeypatch)
    assert _imports_error(source) == {
        "error": "This decision needs `import Demo.View` in the notebook."
    }
    assert _imports_error(source, imports=("Demo.View\n#eval 1",)) == {
        "error": "The provider declares an invalid import `Demo.View\n#eval 1`."
    }


def test_import_checks_need_a_lake_project_and_a_resolved_setup(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    def inspect(path, *, expected_sha256, cancel, imports=()):
        return {"status": "unavailable", "message": "Prepare the project."}

    monkeypatch.setattr(provider_lean, "inspect_project", inspect)
    assert _imports_error(source) == {"error": "Prepare the project."}
    (tmp_path / "lakefile.toml").unlink()
    assert _imports_error(source) == {
        "error": "Decisions need a Lake project; see the README."
    }


def test_a_provider_without_imports_needs_no_setup(tmp_path, monkeypatch):
    """Nothing to load: no project inspection, no slot, not even a Lake project."""
    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import snapshot_slot

    def forbidden(*args, **kwargs):
        pytest.fail("the setup was prepared")

    source = _lake(tmp_path, monkeypatch, {})
    monkeypatch.setattr(provider_lean, "inspect_project", forbidden)
    monkeypatch.setattr(provider_lean, "describe_project", forbidden)
    _forbid_lean(monkeypatch)
    (tmp_path / "lakefile.toml").unlink()
    with snapshot_slot():
        assert _imports_error(source, imports=()) is None


def test_a_busy_slot_is_a_transient_import_error(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SLOT_BUSY, snapshot_slot

    source = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    with snapshot_slot():
        assert _imports_error(source) == {"error": SLOT_BUSY, "transient": True}


def test_live_evidence_run_saves_a_current_snapshot(prepared_lean_project, monkeypatch):
    """A real Lake notebook runs a demo evidence provider in the sandbox."""
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain
    from glitnir.notebook import source_preview
    from glitnir.notebook.check_records import load_records
    from glitnir.providers import EvidenceProvider, EvidenceRequest, Providers

    root = prepared_lean_project
    toolchain = _toolchain()
    source = root / "src/Example.lean"
    text = "import Example.Helper\n\ndef c : Nat := helper\n-- @evidence demo c\n"
    source.write_text(text)
    subprocess.run(
        [str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(toolchain)},
    )

    def compute(request, payload):
        # The demo prints one column per series; evidence holds one row per time.
        return {
            "format": "glitnir.evidence/v1",
            "series": {
                "times": payload["times"],
                "values": [list(row) for row in zip(*payload["values"])],
                "names": [request.target],
            },
            "provenance": [["provider", "demo"]],
            "finite": True,
        }

    provider = EvidenceProvider(
        name="demo",
        verbs=frozenset({"demo"}),
        imports=(),
        preamble="",
        marker="demoEvidence:",
        parse=lambda verb, target, options: EvidenceRequest(verb, target, {}),
        command=lambda request: (
            '#eval IO.println ("demoEvidence:" ++ '
            '"{\\"times\\":[0,1],\\"values\\":[[0,1]]}")'
        ),
        compute=compute,
        unavailable=lambda: None,
    )
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({}, {"demo": provider}, {}, {}),
    )
    service = source_preview.SourcePreviewService(source)
    try:
        key = service.run_evidence(_sha(text), 4)
        service._evidence_worker.join(timeout=300)
        [card] = service.snapshot()["evidence"]
    finally:
        service.close()
    assert card["run_error"] is None, card
    assert (card["key"], card["status"]) == (key, "current")
    assert card["snapshot"]["series"] == {
        "times": [0.0, 1.0],
        "values": [[0.0], [1.0]],
        "names": ["c"],
    }
    assert key in load_records(source)["evidence"]
