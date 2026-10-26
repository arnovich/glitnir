"""The shared provider Lean runner serves views and, later, evidence."""

import json
import threading
from pathlib import Path

import pytest
from conftest import lake, settle, sha

NOTEBOOK = "import Demo.View\n\ndef c := 1\n-- @view circuit c\ndef later := 2\n"


def _reporting(monkeypatch, report):
    """Replace the sandbox with one that reports `report` as the only view's."""
    from glitnir.lean import provider_lean

    def run(runtime, arguments, **options):
        view = {"proofs_skipped": False, **report}
        Path(arguments[-1]).write_text(json.dumps({"views": [view]}))

    monkeypatch.setattr(provider_lean, "run_observer", run)


def _answering(monkeypatch, report, requests=None):
    """Replace the sandbox with one that writes `report` as the whole REPORT.

    Each run's REQUEST, its SOURCE and its arguments are appended to `requests`.
    """
    from glitnir.lean import provider_lean

    def run(runtime, arguments, **options):
        request = json.loads(Path(arguments[-2]).read_text())
        if requests is not None:
            requests.append((request, Path(request["source"]).read_text(), arguments))
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
        "expected_sha256": sha(NOTEBOOK),
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


def test_source_puts_provider_imports_first_and_ends_at_the_last_view():
    from glitnir.lean.provider_lean import _provider_source

    source, offsets = _provider_source(NOTEBOOK, [3, 4], ("Demo.View",), needs="view")
    assert source == "import Demo.View\nimport Demo.View\n\ndef c := 1\n"
    assert offsets == [len("import Demo.View\nimport Demo.View\n\n"), len(source)]


@pytest.mark.parametrize("line", [0, 7])
@pytest.mark.parametrize("needs, word", [("view", "View"), ("evidence", "Evidence")])
def test_source_rejects_a_line_outside_the_notebook(line, needs, word):
    from glitnir.lean.provider_lean import _provider_source

    message = f"^{word} directive line {line} is outside the notebook.$"
    with pytest.raises(ValueError, match=message):
        _provider_source(NOTEBOOK, [4, line], (), needs=needs)


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
        (None, "Line 4: unknown c\nmore detail"),  # the provider's own
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

    # Lean's message (Lean/Elab/App.lean, unchanged in 4.35.0) for a preamble line.
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
            "Line 4: " + "\n".join(f"line {n}" for n in range(12)) + "\n…",
        ),
        ("x" * 5000, "Line 4: " + "x" * 2000 + "\n…"),
        ("first\n" + "y" * 3000, "Line 4: first\n" + "y" * 1994 + "\n…"),
        (" \n\t\n", "Line 4: error"),
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
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _reporting(monkeypatch, {"information": ['demoMark:{"a":1}'], "errors": []})
    assert _run(source) == {"payload": '{"a":1}', "proofs_skipped": False}


@pytest.mark.parametrize(
    "needs, message",
    [
        ("view", "This view needs the module `Demo.View`, which no package"),
        ("evidence", "This evidence needs the module `Demo.View`, which no package"),
    ],
)
def test_missing_import_is_worded_for_the_caller(tmp_path, monkeypatch, needs, message):
    source, _ = lake(tmp_path, monkeypatch, {})
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
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _forbid_lean(monkeypatch)
    result = _run(source, preamble=None)
    assert result["error"].startswith("Provider demo failed: TypeError: ")


def test_invalid_import_names_are_rejected_before_composing(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {})
    _forbid_lean(monkeypatch)
    assert _run(source, imports=("Lean.Elab\n#eval 1",)) == {
        "error": "The provider declares an invalid import `Lean.Elab\n#eval 1`."
    }


def test_changed_source_never_launches_lean(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _forbid_lean(monkeypatch)
    assert "changed" in _run(source, expected_sha256=sha("other"))["error"]


def test_source_edited_during_the_run_is_an_error(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    def run(runtime, arguments, **options):
        view = {"information": ["demoMark:x"], "errors": [], "proofs_skipped": True}
        Path(arguments[-1]).write_text(json.dumps({"views": [view]}))
        source.write_text(NOTEBOOK + "-- edited\n")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert "changed" in _run(source)["error"]


def test_cancellation_is_transient_and_worded_for_the_caller(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
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


# Ten lines; the second is not ASCII, so its bytes are not its characters.
TEN = "def l1 := 1\n-- “quoted” Navier–Stokes\n" + "".join(
    f"def l{n} := {n}\n" for n in range(3, 11)
)


def _batch(source, **overrides):
    from glitnir.lean.provider_lean import run_provider_batch

    arguments = {
        "expected_sha256": sha(TEN),
        "provider_name": "demo",
        "imports": ("A",),
        "preamble": "open Demo",
        "marker": "demoMark:",
        "needs": "view",
        "views": [(4, "#demo four"), (9, "#demo nine")],
        "cancel": threading.Event(),
        **overrides,
    }
    return run_provider_batch(source, **arguments)


def _ten(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {"A": [["A.olean"]]})
    source.write_text(TEN)
    return source


def test_batch_request_splices_each_view_at_its_line(tmp_path, monkeypatch):
    source = _ten(tmp_path, monkeypatch)
    requests: list = []
    view = {"information": [], "errors": [], "proofs_skipped": True}
    _answering(monkeypatch, {"views": [view, view]}, requests)
    _batch(source)
    [(request, text, arguments)] = requests
    lines = [line + "\n" for line in TEN.split("\n")]
    assert text == "import A\n" + "".join(lines[:8])
    assert [view["at"] for view in request["views"]] == [
        len(("import A\n" + "".join(lines[:3])).encode()),
        len(("import A\n" + "".join(lines[:8])).encode()),
    ]
    assert [view["command"] for view in request["views"]] == [
        "#demo four",
        "#demo nine",
    ]
    assert [view["preamble"] for view in request["views"]] == ["open Demo"] * 2
    assert request["marker"] == "demoMark:"
    assert Path(request["setup"]).parent == Path(request["source"]).parent
    assert Path(request["source"]).name == "Source.lean"
    assert Path(arguments[-2]) == Path(request["source"]).with_name("request.json")
    assert arguments[:3] == ["lean", arguments[1], "--run"]
    assert arguments[1].startswith("-M")
    assert Path(arguments[3]).name == "ViewElab.lean" and len(arguments) == 6


def test_batch_maps_results_in_order(tmp_path, monkeypatch):
    source = _ten(tmp_path, monkeypatch)
    report = {
        "views": [
            {
                "information": ["other", "demoMark:<svg/>"],
                "errors": [{"line": 3, "text": "ignored"}],
                "proofs_skipped": True,
            },
            {
                "information": [],
                "errors": [{"line": 7, "text": "boom"}],
                "proofs_skipped": False,
            },
        ]
    }
    _answering(monkeypatch, report)
    assert _batch(source) == {
        "views": [
            {"payload": "<svg/>", "proofs_skipped": True},
            {"error": "Line 6: boom"},
        ]
    }


def test_error_without_line_is_reported_at_the_directive(tmp_path, monkeypatch):
    source = _ten(tmp_path, monkeypatch)
    empty = {"information": [], "errors": [], "proofs_skipped": False}
    bad = {"information": [], "errors": [{"text": "bad"}], "proofs_skipped": False}
    _answering(monkeypatch, {"views": [empty, bad]})
    assert _batch(source) == {
        "views": [
            {"error": "The provider printed no drawing."},
            {"error": "Line 9: bad"},
        ]
    }


def test_whole_run_failure_is_one_error(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import SnapshotError

    source = _ten(tmp_path, monkeypatch)
    message = "lean did not complete successfully (exit 1).\n…"

    def run(*_arguments, **_options):
        raise SnapshotError(message)

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert _batch(source) == {"error": message}


@pytest.mark.parametrize(
    "views",
    [
        [{"information": [], "errors": [], "proofs_skipped": True}],
        [{"information": [], "errors": [], "proofs_skipped": 1}] * 2,
        [{"information": [], "errors": []}] * 2,
        "x",
    ],
)
def test_malformed_batch_reports_are_rejected(tmp_path, monkeypatch, views):
    source = _ten(tmp_path, monkeypatch)
    _answering(monkeypatch, {"views": views})
    assert _batch(source) == {"error": "Malformed view report."}


@pytest.mark.parametrize("lines", [(9, 4), (4, 12)])
def test_batch_views_must_be_in_order_and_in_the_notebook(tmp_path, monkeypatch, lines):
    source = _ten(tmp_path, monkeypatch)
    _forbid_lean(monkeypatch)
    result = _batch(source, views=[(line, "#demo") for line in lines])
    assert set(result) == {"error"}


def test_a_batch_of_no_views_runs_nothing(tmp_path, monkeypatch):
    source = _ten(tmp_path, monkeypatch)
    _forbid_lean(monkeypatch)
    assert _batch(source, views=[]) == {"views": []}


def _imports_error(source, **overrides):
    from glitnir.lean.provider_lean import provider_imports_error

    arguments = {
        "expected_sha256": sha(NOTEBOOK),
        "imports": ("Demo.View",),
        "needs": "decision",
        "cancel": threading.Event(),
        **overrides,
    }
    return provider_imports_error(source, **arguments)


def test_imports_the_setup_provides_are_no_error(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _forbid_lean(monkeypatch)
    assert _imports_error(source, imports=("Demo.View", "Lean.Elab")) is None


def test_missing_imports_are_named_before_anything_runs(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {})
    _forbid_lean(monkeypatch)
    assert _imports_error(source) == {
        "error": "This decision needs `import Demo.View` in the notebook."
    }
    assert _imports_error(source, imports=("Demo.View\n#eval 1",)) == {
        "error": "The provider declares an invalid import `Demo.View\n#eval 1`."
    }


def test_import_checks_need_a_lake_project_and_a_resolved_setup(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

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

    source, _ = lake(tmp_path, monkeypatch, {})
    monkeypatch.setattr(provider_lean, "inspect_project", forbidden)
    monkeypatch.setattr(provider_lean, "describe_project", forbidden)
    _forbid_lean(monkeypatch)
    (tmp_path / "lakefile.toml").unlink()
    with snapshot_slot():
        assert _imports_error(source, imports=()) is None


def test_a_busy_slot_is_a_transient_import_error(tmp_path, monkeypatch):
    from glitnir.lean.project_snapshot import SLOT_BUSY, snapshot_slot

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    with snapshot_slot():
        assert _imports_error(source) == {"error": SLOT_BUSY, "transient": True}


def test_live_evidence_run_saves_a_current_snapshot(prepared_lean_project, monkeypatch):
    """A real Lake notebook runs a demo evidence provider in the sandbox."""
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain
    from glitnir.notebook.check_records import load_records
    from glitnir.providers import EvidenceProvider, EvidenceRequest, Providers
    from glitnir.service import preview as source_preview

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
        key = service.run_evidence(sha(text), 4)
        settle(service, timeout=300)
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


ENVIRONMENT = "a" * 64


@pytest.fixture(autouse=True)
def _no_cached_setups():
    from glitnir.lean import provider_lean

    provider_lean._SETUPS.clear()
    yield
    provider_lean._SETUPS.clear()


def _counted(tmp_path, monkeypatch):
    """Return a Lake notebook and the list of its inspections, one per setup."""
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["V.olean"]]})
    inspect = provider_lean.inspect_project
    calls: list[tuple[str, ...]] = []

    def counting(path, *, expected_sha256, cancel, imports=()):
        calls.append(tuple(imports))
        return inspect(
            path, expected_sha256=expected_sha256, cancel=cancel, imports=imports
        )

    monkeypatch.setattr(provider_lean, "inspect_project", counting)
    _reporting(monkeypatch, {"information": ["demoMark:x"], "errors": []})
    return source, calls


def test_setup_reused_for_same_environment(tmp_path, monkeypatch):
    source, calls = _counted(tmp_path, monkeypatch)
    for _ in range(2):
        result = _run(source, environment_sha256=ENVIRONMENT)
        assert "error" not in result
    assert len(calls) == 1


def test_setup_not_reused_when_environment_changes(tmp_path, monkeypatch):
    source, calls = _counted(tmp_path, monkeypatch)
    _run(source, environment_sha256=ENVIRONMENT)
    _run(source, environment_sha256="b" * 64)
    assert len(calls) == 2


@pytest.mark.parametrize("environment", [None, "", "abc", "A" * 64, "g" * 64])
def test_setup_not_cached_without_environment(tmp_path, monkeypatch, environment):
    source, calls = _counted(tmp_path, monkeypatch)
    _run(source, environment_sha256=environment)
    _run(source, environment_sha256=environment)
    assert len(calls) == 2


def test_setup_dropped_when_configuration_changed(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import SnapshotError

    source, calls = _counted(tmp_path, monkeypatch)
    _run(source, environment_sha256=ENVIRONMENT)
    check = provider_lean._check_configuration

    def changed(*_arguments):
        raise SnapshotError("Project configuration changed; refresh proof steps.")

    monkeypatch.setattr(provider_lean, "_check_configuration", changed)
    result = _run(source, environment_sha256=ENVIRONMENT)
    assert "configuration changed" in result["error"]
    assert len(calls) == 1
    monkeypatch.setattr(provider_lean, "_check_configuration", check)
    _run(source, environment_sha256=ENVIRONMENT)
    assert len(calls) == 2


def test_setup_cache_key_includes_provider_imports(tmp_path, monkeypatch):
    source, calls = _counted(tmp_path, monkeypatch)
    _run(source, environment_sha256=ENVIRONMENT)
    _run(source, environment_sha256=ENVIRONMENT, imports=("Demo.View", "Demo.More"))
    assert len(calls) == 2


def test_setup_cache_is_bounded(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, calls = _counted(tmp_path, monkeypatch)
    for number in range(provider_lean.MAX_SETUPS + 2):
        _run(source, environment_sha256=f"{number:064x}")
    assert len(provider_lean._SETUPS) == provider_lean.MAX_SETUPS


@pytest.mark.parametrize(
    "changed",
    [
        "module\nimport Demo.View\n\ndef c := 1\n",
        "import all Demo.View\n\ndef c := 1\n",
        "public import Demo.View\n\ndef c := 1\n",
        "prelude\nimport Demo.View\n\ndef c := 1\n",
    ],
)
def test_setup_cache_key_includes_the_whole_header(tmp_path, monkeypatch, changed):
    source, calls = _counted(tmp_path, monkeypatch)
    plain = "import Demo.View\n\ndef c := 1\n-- @view circuit c\n"
    other = changed + "-- @view circuit c\n"
    assert plain != other
    source.write_text(plain)
    _run(source, environment_sha256=ENVIRONMENT, expected_sha256=sha(plain), line=4)
    source.write_text(other)
    _run(
        source,
        environment_sha256=ENVIRONMENT,
        expected_sha256=sha(other),
        line=4,
    )
    assert len(calls) == 2


def test_a_provider_run_is_kept_awake(tmp_path, monkeypatch, awake_depth):
    """A view or evidence run holds the assertion from its slot to its end."""
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    depth = awake_depth(provider_lean)
    held: list[int] = []

    def run(runtime, arguments, **options):
        held.append(depth[0])
        view = {"proofs_skipped": False, "information": ["demoMark:1"], "errors": []}
        Path(arguments[-1]).write_text(json.dumps({"views": [view]}))

    monkeypatch.setattr(provider_lean, "run_observer", run)
    messages: list[str] = []
    assert _run(source, progress=messages.append) == {
        "payload": "1",
        "proofs_skipped": False,
    }
    assert held == [1] and depth == [0]
    assert messages


def test_errors_of_the_directive_itself_are_marked_as_lean_s(tmp_path, monkeypatch):
    """Only the directive's own errors are marked; the caller may save those."""
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _reporting(monkeypatch, {"information": [], "errors": [{"text": "unknown c"}]})
    assert _run(source) == {"error": "Line 4: unknown c", "lean": True}
    _reporting(monkeypatch, {"information": [], "errors": []})
    assert _run(source) == {"error": "The provider printed no evidence.", "lean": True}


def test_reaching_the_run_time_limit_is_marked(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean
    from glitnir.lean.project_checking import TIMED_OUT
    from glitnir.lean.project_snapshot import SnapshotError

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    def run(*_arguments, **_options):
        raise SnapshotError(TIMED_OUT)

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert _run(source) == {"error": TIMED_OUT, "transient": True, "timed_out": True}
    # A batch of views reports the same failure unmarked.
    source.write_text(TEN)
    assert _batch(source, imports=("Demo.View",)) == {
        "error": TIMED_OUT,
        "transient": True,
    }


def test_setup_failures_are_not_marked(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {})
    _forbid_lean(monkeypatch)
    result = _run(source)
    assert set(result) == {"error"}
