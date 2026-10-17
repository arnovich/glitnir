"""Project-mode views elaborate a composed source in the existing sandbox."""

import hashlib
import json
import time
from pathlib import Path

import pytest

from glitnir.providers import ViewProvider

NOTEBOOK = "import Demo.View\n\ndef c := 1\n-- @view circuit c\ndef later := 2\n"


def _provider(imports: tuple[str, ...] = ("Demo.View",)) -> ViewProvider:
    return ViewProvider(
        name="demo",
        kinds=frozenset({"circuit"}),
        imports=imports,
        preamble="open Demo",
        marker="demoView:",
        command=lambda kind, target, using: " ".join(
            ("#demo_view", kind, target, *using)
        ),
    )


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

    def inspect(path, *, expected_sha256, cancel):
        assert path == source and expected_sha256 == _sha(NOTEBOOK)
        return {
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
        }

    monkeypatch.setattr(provider_lean, "inspect_project", inspect)
    return source, toolchain


def _render(source, provider=None, **overrides):
    import threading

    from glitnir.lean.project_views import render_project_view

    arguments = {
        "expected_sha256": _sha(NOTEBOOK),
        "line": 4,
        "provider": provider or _provider(),
        "kind": "circuit",
        "name": "c",
        "using": (),
        "cancel": threading.Event(),
        **overrides,
    }
    return render_project_view(source, **arguments)


def _reporting(monkeypatch, report, calls=None):
    """Replace the sandbox with one that writes `report` to the report path."""
    from glitnir.lean import provider_lean

    def run(runtime, arguments, **options):
        if calls is not None:
            inputs = {Path(a).name: Path(a).read_text() for a in arguments[3:6]}
            calls.append((runtime, arguments, options, inputs))
        Path(arguments[-1]).write_text(json.dumps(report))

    monkeypatch.setattr(provider_lean, "run_observer", run)


def test_missing_provider_import_is_reported_without_lean(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = _lake(tmp_path, monkeypatch, {})

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run without the provider's imports")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source)
    assert result == {"error": "This view needs `import Demo.View` in the notebook."}


def test_invalid_provider_import_is_rejected_before_composing(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = _lake(tmp_path, monkeypatch, {})

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source, _provider(("Lean.Elab\n#eval 1",)))
    assert result == {
        "error": "The provider declares an invalid import `Lean.Elab\n#eval 1`."
    }


@pytest.mark.parametrize("imports", [("Demo.View",), ("Lean",), ()])
def test_loose_files_need_a_lake_project(tmp_path, monkeypatch, imports):
    from glitnir.lean import provider_lean

    source = tmp_path / "Note.lean"
    source.write_text(NOTEBOOK)
    monkeypatch.setattr(provider_lean, "_toolchain", lambda: tmp_path)

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run for a loose file")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source, provider=_provider(imports=imports))
    assert result == {"error": "Views need a Lake project; see the README."}


def test_marker_message_becomes_a_validated_drawing(tmp_path, monkeypatch):
    from glitnir.lean.provider_lean import compose_provider_source

    source, toolchain = _lake(
        tmp_path, monkeypatch, {"Demo.View": [".lake/build/Demo/View.olean"]}
    )
    calls: list = []
    _reporting(
        monkeypatch,
        {"information": ["unrelated", "demoView:<svg/>"], "errors": []},
        calls,
    )
    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", "42000")
    result = _render(source)
    assert result == {"svg": "<svg/>"}

    [(runtime, arguments, options, inputs)] = calls
    assert runtime == toolchain
    assert arguments[:3] == ["lean", arguments[1], "--run"]
    assert arguments[1].startswith("-M")
    helper, composed, setup, report = map(Path, arguments[3:])
    assert helper.name == "ViewElab.lean"
    assert composed != source
    assert inputs["ViewElab.lean"].startswith("/- Elaborate a composed view")
    assert inputs[composed.name] == compose_provider_source(
        NOTEBOOK,
        4,
        ("Demo.View",),
        "open Demo",
        "#demo_view circuit c",
        needs="view",
    )
    written = json.loads(inputs[setup.name])
    assert written["importArts"]["Demo.View"] == [
        str(tmp_path.resolve() / ".lake/build/Demo/View.olean")
    ]
    assert report.parent == options["cwd"]
    assert options["writable"] is True
    assert set(options["readable"]) == {
        helper.parent,
        toolchain,
        tmp_path.resolve(),
    }
    assert 40 < options["deadline"] - time.monotonic() <= 42
    assert source.read_text() == NOTEBOOK
    assert not source.with_suffix(".glitnir.json").exists()


def test_lean_errors_and_missing_marker_are_errors(tmp_path, monkeypatch):
    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    _reporting(
        monkeypatch,
        {"information": ["hello"], "errors": ["unknown c\nmore detail", "second"]},
    )
    assert _render(source) == {"error": "unknown c"}

    _reporting(monkeypatch, {"information": ["hello"], "errors": []})
    assert _render(source) == {"error": "The provider printed no drawing."}


def test_invalid_drawings_and_reports_are_errors(tmp_path, monkeypatch):
    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    _reporting(monkeypatch, {"information": ["demoView:<html/>"], "errors": []})
    assert "error" in _render(source)

    _reporting(monkeypatch, {"information": "demoView:<svg/>", "errors": []})
    assert "error" in _render(source)


def test_changed_source_never_launches_lean(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run for a stale source")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source, expected_sha256=_sha("something else"))
    assert "changed" in result["error"]


def test_unavailable_sandbox_is_an_error(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean
    from glitnir.lean.sandbox import SandboxUnavailable

    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    def run(*_arguments, **_options):
        raise SandboxUnavailable("no sandbox here")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert _render(source) == {"error": "no sandbox here"}


def test_unresolved_project_is_an_error(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = _lake(tmp_path, monkeypatch, {})
    monkeypatch.setattr(
        provider_lean,
        "inspect_project",
        lambda *_a, **_k: {"status": "unbuilt", "message": "Run lake build."},
    )
    assert _render(source) == {"error": "Run lake build."}


@pytest.mark.parametrize("line", [0, 7])
def test_directive_line_must_be_in_the_notebook(tmp_path, monkeypatch, line):
    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    _reporting(monkeypatch, {"information": ["demoView:<svg/>"], "errors": []})
    assert "error" in _render(source, line=line)


@pytest.mark.parametrize("module", ["Lean", "Lean.Elab.Command", "Std", "Init"])
def test_toolchain_modules_need_no_import_artifacts(tmp_path, monkeypatch, module):
    source, _ = _lake(tmp_path, monkeypatch, {})
    calls: list = []
    _reporting(monkeypatch, {"information": ["demoView:<svg/>"], "errors": []}, calls)
    assert _render(source, provider=_provider(imports=(module,))) == {"svg": "<svg/>"}
    assert len(calls) == 1


def test_lookalike_modules_still_need_import_artifacts(tmp_path, monkeypatch):
    source, _ = _lake(tmp_path, monkeypatch, {})
    result = _render(source, provider=_provider(imports=("LeanDemo.View",)))
    assert result == {
        "error": "This view needs `import LeanDemo.View` in the notebook."
    }


def test_live_project_view_draws_a_demo_provider(prepared_lean_project):
    """A real Lake notebook draws through a provider in the sandbox."""
    import os
    import subprocess
    import threading

    from glitnir.lean.project_inspection import _toolchain
    from glitnir.lean.project_views import render_project_view

    root = prepared_lean_project
    toolchain = _toolchain()
    source = root / "src/Example.lean"
    text = (
        "import Lean\nimport Example.Helper\n\ndef c : Nat := helper\n-- @view demo c\n"
    )
    source.write_text(text)
    subprocess.run(
        [str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(toolchain)},
    )
    provider = ViewProvider(
        name="demo",
        kinds=frozenset({"demo"}),
        imports=("Lean",),
        preamble=(
            "open Lean Elab Command in\n"
            'elab "#demo_view " n:ident : command => logInfo m!"demoView:'
            '<svg xmlns=\\"http://www.w3.org/2000/svg\\">'
            '<title>{n.getId}</title></svg>"'
        ),
        marker="demoView:",
        command=lambda kind, name, using: f"#demo_view {name}",
    )
    result = render_project_view(
        source,
        expected_sha256=_sha(text),
        line=5,
        provider=provider,
        kind="demo",
        name="c",
        using=(),
        cancel=threading.Event(),
    )
    assert set(result) == {"svg"}, result
    assert "<title>c</title>" in result["svg"]


def test_busy_slot_is_a_transient_error(tmp_path, monkeypatch):
    import contextlib

    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import SLOT_BUSY, SnapshotError

    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    @contextlib.contextmanager
    def busy():
        raise SnapshotError(SLOT_BUSY)
        yield

    monkeypatch.setattr(provider_lean, "snapshot_slot", busy)
    result = _render(source)
    assert result == {"error": SLOT_BUSY, "transient": True}


@pytest.mark.parametrize(
    "message", ["Project check cancelled.", "Project check timed out."]
)
def test_cancelled_or_timed_out_drawing_is_transient(tmp_path, monkeypatch, message):
    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import SnapshotError

    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    def run(*_arguments, **_options):
        raise SnapshotError(message)

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert _render(source) == {"error": message, "transient": True}


def test_lasting_errors_are_not_transient(tmp_path, monkeypatch):
    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    _reporting(monkeypatch, {"information": [], "errors": ["unknown c"]})
    assert "transient" not in _render(source)


@pytest.mark.parametrize(
    "changes",
    [
        {"command": lambda kind, name, using: 1 / 0},
        {"command": lambda kind, name, using: None},
        {"preamble": None},
        {"imports": (None,)},
    ],
)
def test_provider_code_failures_become_errors(tmp_path, monkeypatch, changes):
    import dataclasses

    from glitnir.lean import provider_lean

    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source, dataclasses.replace(_provider(), **changes))
    assert set(result) == {"error"}
    assert result["error"].startswith("Provider demo failed: ")


def test_a_drawing_wins_over_unrelated_errors(tmp_path, monkeypatch):
    source, _ = _lake(tmp_path, monkeypatch, {"Demo.View": ["View.olean"]})
    _reporting(
        monkeypatch,
        {"information": ["demoView:<svg/>"], "errors": [{"line": 3, "text": "x"}]},
    )
    assert _render(source) == {"svg": "<svg/>"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("import Missing.Module\ndef c : Nat := 1\n", "unknown module"),
        ("def b : Nat := (\ndef c : Nat := 1\n", "expected"),
    ],
)
def test_live_view_elaboration_keeps_header_and_parse_errors(
    tmp_path, project_toolchain, text, expected
):
    """Import and parse failures surface instead of an empty, successful report."""
    import threading

    from glitnir.lean.project_checking import run_observer
    from glitnir.lean.provider_lean import PROGRAM

    inputs, output = tmp_path / "inputs", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    source = inputs / "Source.lean"
    source.write_text(text)
    setup = {
        "name": "Source",
        "isModule": False,
        "importArts": {},
        "plugins": [],
        "dynlibs": [],
        "options": {},
    }
    (inputs / "setup.json").write_text(json.dumps(setup))
    helper = inputs / PROGRAM.name
    helper.write_bytes(PROGRAM.read_bytes())
    report = output / "view.json"
    run_observer(
        project_toolchain,
        ["lean", "--run", str(helper), str(source), str(inputs / "setup.json")]
        + [str(report)],
        cwd=output,
        readable=[inputs, project_toolchain],
        writable=True,
        cancel=threading.Event(),
        deadline=time.monotonic() + 90,
    )
    errors = json.loads(report.read_text())["errors"]
    assert any(expected in error["text"] for error in errors), errors


@pytest.mark.parametrize(
    "configured, expected", [(None, "-M8192"), ("12288", "-M12288")]
)
def test_provider_lean_uses_the_configured_memory_limit(
    tmp_path, monkeypatch, configured, expected
):
    """Provider Lean runs with `GLITNIR_MEMORY_LIMIT_MB`, or 8192 MB by default."""
    source, _ = _lake(
        tmp_path, monkeypatch, {"Demo.View": [".lake/build/Demo/View.olean"]}
    )
    calls: list = []
    _reporting(monkeypatch, {"information": ["demoView:<svg/>"], "errors": []}, calls)
    if configured is None:
        monkeypatch.delenv("GLITNIR_MEMORY_LIMIT_MB", raising=False)
    else:
        monkeypatch.setenv("GLITNIR_MEMORY_LIMIT_MB", configured)
    assert _render(source) == {"svg": "<svg/>"}
    [(_, arguments, _, _)] = calls
    assert [a for a in arguments if a.startswith("-M")] == [expected]
