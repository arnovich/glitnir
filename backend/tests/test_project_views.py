"""Project-mode views elaborate a composed source in the existing sandbox."""

import json
import time
from functools import partial
from pathlib import Path

import pytest
from conftest import lake as shared_lake
from conftest import sha

from glitnir.providers import ViewProvider

NOTEBOOK = "import Demo.View\n\ndef c := 1\n-- @view circuit c\ndef later := 2\n"

#: `shared_lake` for this module's notebook, whose digest each inspection must get.
lake = partial(shared_lake, required_sha256=sha(NOTEBOOK))


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


def _render_one(
    source,
    *,
    expected_sha256,
    line,
    provider,
    kind,
    name,
    using,
    cancel,
    progress=None,
    environment_sha256=None,
):
    """Draw one view with `render_project_views`: its drawing or the failure."""
    from glitnir.lean.project_views import render_project_views

    result = render_project_views(
        source,
        expected_sha256=expected_sha256,
        provider=provider,
        views=[(line, kind, name, using)],
        cancel=cancel,
        progress=progress,
        environment_sha256=environment_sha256,
    )
    return result["views"][0] if "views" in result else result


def _render(source, provider=None, **overrides):
    import threading

    arguments = {
        "expected_sha256": sha(NOTEBOOK),
        "line": 4,
        "provider": provider or _provider(),
        "kind": "circuit",
        "name": "c",
        "using": (),
        "cancel": threading.Event(),
        **overrides,
    }
    return _render_one(source, **arguments)


def _reporting(monkeypatch, report, calls=None, proofs_skipped=False):
    """Replace the sandbox with one that reports `report` as the only view's.

    Each run's arguments and the text of its inputs, by file name, are
    appended to `calls`.
    """
    from glitnir.lean import provider_lean

    def run(runtime, arguments, **options):
        if calls is not None:
            helper, request = map(Path, arguments[3:5])
            written = json.loads(request.read_text())
            inputs = {
                path.name: path.read_text()
                for path in (helper, request, Path(written["source"]))
                + (Path(written["setup"]),)
            }
            calls.append((runtime, arguments, options, inputs))
        view = {"proofs_skipped": proofs_skipped, **report}
        Path(arguments[-1]).write_text(json.dumps({"views": [view]}))

    monkeypatch.setattr(provider_lean, "run_observer", run)


def test_missing_provider_import_is_reported_without_lean(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {})

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run without the provider's imports")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source)
    assert result == {
        "error": "This view needs the module `Demo.View`, which no package of this "
        "Lake project provides. Add the package that provides it to the project's "
        "dependencies."
    }


def test_provider_imports_are_set_up_from_the_project_build(tmp_path, monkeypatch):
    """The notebook need not import the provider's modules; Lake sets them up."""
    asked: list[tuple[str, ...]] = []
    source, _ = lake(
        tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]}, asked=asked
    )
    calls: list = []
    _reporting(monkeypatch, {"information": ["demoView:{}"], "errors": []}, calls)
    _render(source, provider=_provider(("Lean", "Demo.View")))
    # Toolchain libraries need no Lake setup; project modules are added to it.
    assert asked == [("Demo.View",)]
    assert len(calls) == 1


def _needs_build(tmp_path, monkeypatch, own_resolves):
    """A notebook whose setup with the provider's modules finds one unbuilt."""
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {})
    asked: list[tuple[str, ...]] = []

    def inspect(path, *, expected_sha256, cancel, imports=()):
        asked.append(imports)
        if imports or not own_resolves:
            return {
                "status": "unavailable",
                "message": "Lake could not inspect this prepared project.",
                "needs_build": True,
            }
        return {
            "status": "resolved",
            "setup": {"name": "Note", "importArts": {"Other": [["O.olean"]]}},
            "resolution": {"inputs": []},
        }

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run")

    monkeypatch.setattr(provider_lean, "inspect_project", inspect)
    monkeypatch.setattr(provider_lean, "run_observer", run)
    return source, asked


def test_an_unbuilt_provider_module_names_the_build_to_run(tmp_path, monkeypatch):
    source, asked = _needs_build(tmp_path, monkeypatch, own_resolves=True)
    result = _render(source)
    assert result == {
        "error": "This view needs `Demo.View`, which this project has not built or "
        f"has out of date. Run `lake build Demo.View` in {tmp_path.resolve()}, "
        "then try again."
    }
    assert asked == [("Demo.View",), ()]


def test_an_unbuilt_notebook_import_keeps_lakes_message(tmp_path, monkeypatch):
    source, asked = _needs_build(tmp_path, monkeypatch, own_resolves=False)
    result = _render(source)
    assert result == {"error": "Lake could not inspect this prepared project."}
    assert asked == [("Demo.View",), ()]


def test_drawing_waits_for_a_busy_slot_and_says_so(tmp_path, monkeypatch):
    """A view waits for another Lean run rather than failing at once."""
    import threading

    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _reporting(monkeypatch, {"information": ["demoView:{}"], "errors": []})
    waited: list[dict] = []
    messages: list[str] = []
    monkeypatch.setattr(provider_lean, "snapshot_slot", _waiting_slot(waited))
    _render(source, progress=messages.append, cancel=threading.Event())
    assert len(waited) == 1 and waited[0]["cancel"] is not None
    assert messages == [
        provider_lean.WAITING,
        provider_lean.PREPARING,
        provider_lean.RUNNING["view"],
    ]


def _waiting_slot(calls):
    """A slot that reports it was busy, then is taken at once."""
    from contextlib import contextmanager

    @contextmanager
    def slot(**options):
        calls.append(options)
        if options.get("waiting") is not None:
            options["waiting"]()
        yield

    return slot


def test_invalid_provider_import_is_rejected_before_composing(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {})

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
    source, toolchain = lake(
        tmp_path, monkeypatch, {"Demo.View": [[".lake/build/Demo/View.olean"]]}
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
    helper, request, report = map(Path, arguments[3:])
    assert helper.name == "ViewElab.lean"
    assert inputs["ViewElab.lean"].startswith("/- Elaborate a composed view")
    # The program elaborates the preamble outside the notebook's scope and
    # the command in it; `test_live_view_preamble_scope_is_pinned` runs it.
    written_request = json.loads(inputs[request.name])
    composed = Path(written_request["source"])
    assert composed != source and composed.parent == helper.parent
    assert inputs[composed.name] == "import Demo.View\nimport Demo.View\n\ndef c := 1\n"
    assert written_request["marker"] == "demoView:"
    assert written_request["views"] == [
        {
            "at": len(inputs[composed.name].encode()),
            "preamble": "open Demo",
            "command": "#demo_view circuit c",
        }
    ]
    setup = Path(written_request["setup"])
    assert setup.parent == helper.parent
    written = json.loads(inputs[setup.name])
    assert written["importArts"]["Demo.View"] == [
        [str(tmp_path.resolve() / ".lake/build/Demo/View.olean")]
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
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    _reporting(
        monkeypatch,
        {"information": ["hello"], "errors": ["unknown c\nmore detail", "second"]},
    )
    assert _render(source) == {"error": "Line 4: unknown c\nmore detail"}

    _reporting(monkeypatch, {"information": ["hello"], "errors": []})
    assert _render(source) == {"error": "The provider printed no drawing."}


def test_invalid_drawings_and_reports_are_errors(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    _reporting(monkeypatch, {"information": ["demoView:<html/>"], "errors": []})
    assert "error" in _render(source)

    _reporting(monkeypatch, {"information": "demoView:<svg/>", "errors": []})
    assert "error" in _render(source)


def test_changed_source_never_launches_lean(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run for a stale source")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source, expected_sha256=sha("something else"))
    assert "changed" in result["error"]


def test_unavailable_sandbox_is_an_error(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean
    from glitnir.lean.sandbox import SandboxUnavailable

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    def run(*_arguments, **_options):
        raise SandboxUnavailable("no sandbox here")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert _render(source) == {"error": "no sandbox here"}


def test_unresolved_project_is_an_error(tmp_path, monkeypatch):
    from glitnir.lean import provider_lean

    source, _ = lake(tmp_path, monkeypatch, {})
    monkeypatch.setattr(
        provider_lean,
        "inspect_project",
        lambda *_a, **_k: {"status": "unbuilt", "message": "Run lake build."},
    )
    assert _render(source) == {"error": "Run lake build."}


@pytest.mark.parametrize("line", [0, 7])
def test_directive_line_must_be_in_the_notebook(tmp_path, monkeypatch, line):
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _reporting(monkeypatch, {"information": ["demoView:<svg/>"], "errors": []})
    assert "error" in _render(source, line=line)


@pytest.mark.parametrize("module", ["Lean", "Lean.Elab.Command", "Std", "Init"])
def test_toolchain_modules_need_no_import_artifacts(tmp_path, monkeypatch, module):
    source, _ = lake(tmp_path, monkeypatch, {})
    calls: list = []
    _reporting(monkeypatch, {"information": ["demoView:<svg/>"], "errors": []}, calls)
    assert _render(source, provider=_provider(imports=(module,))) == {"svg": "<svg/>"}
    assert len(calls) == 1


def test_lookalike_modules_still_need_import_artifacts(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {})
    result = _render(source, provider=_provider(imports=("LeanDemo.View",)))
    assert result["error"].startswith(
        "This view needs the module `LeanDemo.View`, which no package"
    )


def test_live_project_view_draws_a_demo_provider(prepared_lean_project):
    """A real Lake notebook draws through a provider in the sandbox."""
    import os
    import subprocess
    import threading

    from glitnir.lean.project_inspection import _toolchain

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
    result = _render_one(
        source,
        expected_sha256=sha(text),
        line=5,
        provider=provider,
        kind="demo",
        name="c",
        using=(),
        cancel=threading.Event(),
    )
    assert result.get("proofs_skipped") is True, result
    assert set(result) == {"svg", "proofs_skipped"}
    assert "<title>c</title>" in result["svg"]


def test_live_view_loads_provider_modules_the_notebook_does_not_import(
    prepared_lean_project,
):
    """Provider modules come from the project's build; unbuilt ones are named.

    The command uses `title` through the `open` its preamble ends with.
    """
    import os
    import subprocess
    import threading

    from glitnir.lean.project_inspection import _toolchain

    root = prepared_lean_project
    toolchain = _toolchain()
    (root / "src/Example/Draw.lean").write_text('def drawTitle : String := "drawn"\n')
    (root / "src/Example/Unbuilt.lean").write_text("def unbuilt : Nat := 1\n")
    subprocess.run(
        [str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example.Draw"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(toolchain)},
    )
    source = root / "src/Example.lean"
    text = source.read_text() + "-- @view demo result\n"
    source.write_text(text)
    line = text.count("\n")

    def provider(imports):
        return ViewProvider(
            name="demo",
            kinds=frozenset({"demo"}),
            imports=imports,
            preamble=(
                "namespace DemoView\n"
                "def title (name : String) : String :=\n"
                '  s!"<svg xmlns=\\"http://www.w3.org/2000/svg\\">'
                '<title>{drawTitle} {name}</title></svg>"\n'
                "end DemoView\n"
                "open DemoView\n"
            ),
            marker="demoView:",
            command=lambda kind, name, using: (
                "#eval show Lean.Elab.Command.CommandElabM Unit from "
                f'Lean.logInfo ("demoView:" ++ title "{name}")'
            ),
        )

    def draw(imports):
        return _render_one(
            source,
            expected_sha256=sha(text),
            line=line,
            provider=provider(imports),
            kind="demo",
            name="result",
            using=(),
            cancel=threading.Event(),
        )

    drawn = draw(("Lean", "Example.Draw"))
    assert drawn.get("proofs_skipped") is True, drawn
    assert set(drawn) == {"svg", "proofs_skipped"}
    assert "<title>drawn result</title>" in drawn["svg"]
    unbuilt = draw(("Lean", "Example.Draw", "Example.Unbuilt"))
    assert unbuilt["error"] == (
        "This view needs `Example.Draw`, `Example.Unbuilt`, which this project has "
        "not built or has out of date. Run `lake build Example.Draw "
        f"Example.Unbuilt` in {root.resolve()}, then try again."
    ), unbuilt
    assert not (root / ".lake/build/lib/lean/Example/Unbuilt.olean").exists()


def test_live_view_preamble_is_elaborated_outside_the_notebooks_scope(
    prepared_lean_project,
):
    """The notebook's opens cannot make a preamble's names ambiguous.

    The notebook opens two namespaces that both declare `Circuit`, as a
    notebook opening `Gimle.Asgard` and `Gimle.Asgard.Dynamics` does, so the
    preamble's `Circuit` was ambiguous in the notebook's scope; as a quoted
    name in the elaborator, that left the command without one. The command
    still names `target` as the notebook does, inside its namespace.
    """
    import threading

    root = prepared_lean_project
    source = root / "src/Example.lean"
    text = (
        "import Example.Helper\n\n"
        "namespace Left\ndef Circuit : Type := Nat\nend Left\n"
        "namespace Right\ndef Circuit : Type := Nat\nend Right\n\n"
        "-- Navier–Stokes, “quoted”: byte offsets are not character offsets.\n"
        "namespace Notebook\nopen Left Right\n"
        "def target : Nat := helper + 1\n"
        "-- @view demo target\n"
    )
    source.write_text(text)
    preamble = (
        "namespace DemoView\nopen Left\n"
        "def width (_c : Circuit) : Nat := 40\n"
        "open Lean Elab Command in\n"
        'elab "#demo_view " n:ident : command => do\n'
        "  let value ← liftTermElabM do\n"
        "    let e ← Term.elabTerm n (some (mkConst ``Nat))\n"
        "    unsafe Meta.evalExpr Nat (mkConst ``Nat) e\n"
        '  logInfo m!"demoView:<svg xmlns=\\"http://www.w3.org/2000/svg\\">'
        '<title>{value + width (0 : Nat)} {``Circuit}</title></svg>"\n'
        "end DemoView\n"
    )
    provider = ViewProvider(
        name="demo",
        kinds=frozenset({"demo"}),
        imports=("Lean",),
        preamble=preamble,
        marker="demoView:",
        command=lambda kind, name, using: f"#demo_view {name}",
    )
    result = _render_one(
        source,
        expected_sha256=sha(text),
        line=text.count("\n"),
        provider=provider,
        kind="demo",
        name="target",
        using=(),
        cancel=threading.Event(),
    )
    assert result.get("proofs_skipped") is True, result
    assert set(result) == {"svg", "proofs_skipped"}
    assert "<title>42 Left.Circuit</title>" in result["svg"]


def test_a_slot_busy_past_the_time_limit_is_a_transient_error(tmp_path, monkeypatch):
    import contextlib

    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import SLOT_WAIT_TIMED_OUT, SnapshotError

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    @contextlib.contextmanager
    def busy(**options):
        raise SnapshotError(SLOT_WAIT_TIMED_OUT)
        yield

    monkeypatch.setattr(provider_lean, "snapshot_slot", busy)
    result = _render(source)
    assert result == {"error": SLOT_WAIT_TIMED_OUT, "transient": True}


@pytest.mark.parametrize(
    "message", ["Project check cancelled.", "Project check timed out."]
)
def test_cancelled_or_timed_out_drawing_is_transient(tmp_path, monkeypatch, message):
    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import SnapshotError

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    def run(*_arguments, **_options):
        raise SnapshotError(message)

    monkeypatch.setattr(provider_lean, "run_observer", run)
    assert _render(source) == {"error": message, "transient": True}


def test_lasting_errors_are_not_transient(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
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

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    def run(*_arguments, **_options):
        raise AssertionError("Lean must not run")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = _render(source, dataclasses.replace(_provider(), **changes))
    assert set(result) == {"error"}
    assert result["error"].startswith("Provider demo failed: ")


def test_a_drawing_drawn_with_proofs_skipped_says_so(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _reporting(
        monkeypatch,
        {"information": ["demoView:<svg/>"], "errors": []},
        proofs_skipped=True,
    )
    assert _render(source) == {"svg": "<svg/>", "proofs_skipped": True}
    # An invalid drawing is only an error, however it was drawn.
    _reporting(
        monkeypatch,
        {"information": ["demoView:<html/>"], "errors": []},
        proofs_skipped=True,
    )
    assert set(_render(source)) == {"error"}


def test_a_drawing_wins_over_unrelated_errors(tmp_path, monkeypatch):
    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    _reporting(
        monkeypatch,
        {"information": ["demoView:<svg/>"], "errors": [{"line": 3, "text": "x"}]},
    )
    assert _render(source) == {"svg": "<svg/>"}


def _elaborate(tmp_path, toolchain, parts, offsets=None, marker="mark:"):
    """Run the view program on `parts` (notebook, preamble, command) in the sandbox.

    The notebook is SOURCE and the one view is spliced at its end. Returns
    the report. Nothing is imported without a Lake setup. `offsets` replaces
    the views' byte offsets, one view per offset.
    """
    import threading

    from glitnir.lean.project_checking import run_observer
    from glitnir.lean.provider_lean import PROGRAM

    inputs, output = tmp_path / "inputs", tmp_path / "output"
    inputs.mkdir()
    output.mkdir()
    source = inputs / "Source.lean"
    source.write_bytes(parts[0].encode())
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
    request = {
        "source": str(source),
        "setup": str(inputs / "setup.json"),
        "marker": marker,
        "views": [
            {"at": at, "preamble": parts[1], "command": parts[2]}
            for at in (offsets or [len(parts[0].encode())])
        ],
    }
    (inputs / "request.json").write_text(json.dumps(request))
    run_observer(
        toolchain,
        ["lean", "--run", str(helper), str(inputs / "request.json"), str(report)],
        cwd=output,
        readable=[inputs, toolchain],
        writable=True,
        cancel=threading.Event(),
        deadline=time.monotonic() + 90,
    )
    return json.loads(report.read_text())


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
    # No preamble and no command: the whole source is the notebook's.
    report = _elaborate(tmp_path, project_toolchain, (text, "", ""))
    [view] = report["views"]
    assert any(
        expected in error["text"] and error["line"] in (1, 2)
        for error in view["errors"]
    ), report


# The notebook's scope: `open`s, an option, a variable and a local notation.
SCOPED_NOTEBOOK = """namespace Left
def Circuit : Type := Nat
end Left
namespace Right
def Circuit : Type := Nat
end Right
namespace Up
def value : Nat := 1
end Up
namespace Down
def value : Nat := 2
def target : Nat := 100
end Down

namespace Notebook
open Left Right Up
set_option autoImplicit false
variable (hidden : Nat)
local notation "twice " x => x + x
def target : Nat := 7

"""
SCOPED_PREAMBLE = """namespace DemoView
open Left
def width (_c : Circuit) : Nat := twice 20
def same (x : α) : α := x
def sees : Nat := hidden
end DemoView
open Nat Down

"""
SCOPED_COMMAND = """#eval IO.println s!"mark:{succ (target + DemoView.width (0 : Nat))}"
#eval value
"""


def test_live_view_preamble_scope_is_pinned(tmp_path, project_toolchain):
    """What reaches the preamble from the notebook, and the command from both.

    In the preamble, `Circuit` is not ambiguous (the notebook's `open`s are
    left out), `α` is bound (its options are) and `hidden` is unknown (its
    variables are), but its local notation `twice` still applies. In the
    command, `succ` comes from the preamble's `open Nat`, `target` is still
    the notebook's own, and `value`, which `open Up` and `open Down` both
    resolve, is ambiguous. The provider's own errors carry no line.
    """
    report = _elaborate(
        tmp_path,
        project_toolchain,
        (SCOPED_NOTEBOOK, SCOPED_PREAMBLE, SCOPED_COMMAND),
    )
    [view] = report["views"]
    assert view["information"] == ["mark:48\n"], report
    assert view["proofs_skipped"] is True
    [hidden, ambiguous] = view["errors"]
    assert hidden == {"text": "Unknown identifier `hidden`"}
    assert ambiguous["text"].startswith("Ambiguous term\n  value\n"), report
    assert "Down.value" in ambiguous["text"] and "Up.value" in ambiguous["text"]


ADJACENT_NOTEBOOK = "def c : Nat := 1\n-- @view a\n"


@pytest.mark.parametrize(
    "preamble, offsets",
    [
        # Adjacent directives: the part between them is only a comment.
        ("", [len("def c : Nat := 1\n"), len(ADJACENT_NOTEBOOK)]),
        # A preamble that is only a comment.
        ("-- only a comment\n", None),
    ],
)
def test_live_view_parts_holding_only_comments_are_no_error(
    tmp_path, project_toolchain, preamble, offsets
):
    """A part with nothing but comments before its end parses as empty.

    Lean's parser reads comments only before a command, so such a part once
    ended in an "unexpected end of input" error.
    """
    command = '#eval IO.println s!"mark:{c}"'
    report = _elaborate(
        tmp_path, project_toolchain, (ADJACENT_NOTEBOOK, preamble, command), offsets
    )
    assert report["views"] == [
        {"information": ["mark:1\n"], "errors": [], "proofs_skipped": True}
    ] * len(offsets or [None]), report


@pytest.mark.parametrize(
    "offsets, message",
    [
        ([8, 7], "offsets must be non-decreasing"),
        ([0, 10_000], "offsets must be non-decreasing"),
        ([4], "inside a character"),
    ],
)
def test_live_view_program_refuses_offsets_outside_the_source(
    tmp_path, project_toolchain, offsets, message
):
    """Offsets out of order, past the source or in a character fail closed."""
    from glitnir.lean.project_snapshot import SnapshotError

    with pytest.raises(SnapshotError, match=message):
        _elaborate(
            tmp_path,
            project_toolchain,
            ("-- “\ndef c : Nat := 1\n", "", ""),
            offsets,
        )
    assert not (tmp_path / "output/view.json").exists()


def test_live_view_skips_proofs_and_falls_back(prepared_lean_project):
    """One Lean run draws one view with proofs skipped and one without.

    The notebook is first elaborated with its proofs skipped, then again with
    them for the view that printed nothing. The second view prints only when
    `t`'s value is not `sorryAx`, the value a skipped proof gets, so only the
    full pass draws it.
    """
    import threading

    from glitnir.lean.provider_lean import run_provider_batch

    root = prepared_lean_project
    source = root / "src/Example.lean"
    text = (
        "import Example.Helper\n\n"
        "theorem t : 2 + 2 = 4 := by decide\n"
        "-- @view demo first\n"
        "example : 1 + 1 = 2 := rfl\n"
        "-- @view demo second\n"
    )
    source.write_text(text)
    svg = '<svg xmlns=\\"http://www.w3.org/2000/svg\\"/>'
    first = f'run_cmd Lean.logInfo "demoView:{svg}"'
    second = (
        "run_cmd do\n"
        "  let some info := (← Lean.getEnv).find? `t | return\n"
        "  let some value := info.value? (allowOpaque := true) | return\n"
        "  unless value.isAppOf ``sorryAx do\n"
        f'    Lean.logInfo "demoView:{svg}"'
    )
    result = run_provider_batch(
        source,
        expected_sha256=sha(text),
        provider_name="demo",
        imports=("Lean",),
        preamble="",
        marker="demoView:",
        needs="view",
        views=[(4, first), (6, second)],
        cancel=threading.Event(),
        wait=True,
    )
    drawn = '<svg xmlns="http://www.w3.org/2000/svg"/>'
    assert result == {
        "views": [
            {"payload": drawn, "proofs_skipped": True},
            {"payload": drawn, "proofs_skipped": False},
        ]
    }, result


@pytest.mark.parametrize(
    "configured, expected", [(None, "-M16384"), ("12288", "-M12288")]
)
def test_provider_lean_uses_the_configured_memory_limit(
    tmp_path, monkeypatch, configured, expected
):
    """Provider Lean runs with `GLITNIR_MEMORY_LIMIT_MB`, or 16384 MB by default."""
    source, _ = lake(
        tmp_path, monkeypatch, {"Demo.View": [[".lake/build/Demo/View.olean"]]}
    )
    calls: list = []
    _reporting(monkeypatch, {"information": ["demoView:<svg/>"], "errors": []}, calls)
    from glitnir.lean import limits

    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 65536)
    if configured is None:
        monkeypatch.delenv("GLITNIR_MEMORY_LIMIT_MB", raising=False)
    else:
        monkeypatch.setenv("GLITNIR_MEMORY_LIMIT_MB", configured)
    assert _render(source) == {"svg": "<svg/>"}
    [(_, arguments, _, _)] = calls
    assert [a for a in arguments if a.startswith("-M")] == [expected]


def test_several_views_are_drawn_in_one_run(tmp_path, monkeypatch):
    """One Lean run draws every view; each payload is checked as a drawing.

    A view whose command the provider cannot give is an error of its own and
    is left out of the run.
    """
    import threading

    from glitnir.lean import provider_lean
    from glitnir.lean.project_views import render_project_views

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})
    requests = []

    def run(runtime, arguments, **options):
        requests.append(json.loads(Path(arguments[4]).read_text()))
        drawn = {"information": ["demoView:<svg/>"], "errors": []}
        invalid = {"information": ["demoView:<html/>"], "errors": []}
        Path(arguments[-1]).write_text(
            json.dumps(
                {
                    "views": [
                        {**drawn, "proofs_skipped": True},
                        {**invalid, "proofs_skipped": False},
                    ]
                }
            )
        )

    monkeypatch.setattr(provider_lean, "run_observer", run)

    def command(kind, name, using):
        if name == "bad":
            raise ValueError("no such view")
        return f"#demo_view {name}"

    provider = ViewProvider(
        "demo", frozenset({"circuit"}), ("Demo.View",), "", "demoView:", command
    )
    result = render_project_views(
        source,
        expected_sha256=sha(NOTEBOOK),
        provider=provider,
        views=[
            (3, "circuit", "bad", ()),
            (4, "circuit", "c", ()),
            (5, "circuit", "later", ()),
        ],
        cancel=threading.Event(),
    )
    [request] = requests
    assert [view["command"] for view in request["views"]] == [
        "#demo_view c",
        "#demo_view later",
    ]
    bad, drawn, invalid = result["views"]
    assert bad == {"error": "Provider demo failed: ValueError: no such view"}
    assert drawn == {"svg": "<svg/>", "proofs_skipped": True}
    assert set(invalid) == {"error"}


def test_a_failed_run_of_several_views_is_one_error(tmp_path, monkeypatch):
    import threading

    from glitnir.lean import provider_lean
    from glitnir.lean.project_snapshot import SnapshotError
    from glitnir.lean.project_views import render_project_views

    source, _ = lake(tmp_path, monkeypatch, {"Demo.View": [["View.olean"]]})

    def run(*_arguments, **_options):
        raise SnapshotError("Project check timed out.")

    monkeypatch.setattr(provider_lean, "run_observer", run)
    result = render_project_views(
        source,
        expected_sha256=sha(NOTEBOOK),
        provider=_provider(),
        views=[(4, "circuit", "c", ()), (5, "circuit", "later", ())],
        cancel=threading.Event(),
    )
    assert result == {"error": "Project check timed out.", "transient": True}


def test_live_three_views_in_one_run(prepared_lean_project, monkeypatch):
    """Three views drawn in one Lean run equal each drawn in a run of its own.

    Each drawing names its view and whether the declaration it names is in
    the environment the view is elaborated in, so a view drawn after the
    wrong prefix would differ.
    """
    import threading

    from glitnir.lean import provider_lean
    from glitnir.lean.project_views import render_project_views

    root = prepared_lean_project
    source = root / "src/Example.lean"
    text = (
        "import Example.Helper\n\n"
        "def a : Nat := helper\n"
        "-- @view demo a\n"
        "def b : Nat := a + 1\n"
        "-- @view demo b\n"
        "theorem t : b = 2 := rfl\n"
        "-- @view demo t\n"
    )
    source.write_text(text)
    provider = ViewProvider(
        name="demo",
        kinds=frozenset({"demo"}),
        imports=("Lean",),
        preamble=(
            "open Lean Elab Command in\n"
            'elab "#demo_view " n:ident : command => do\n'
            "  let known := (← getEnv).contains n.getId\n"
            '  logInfo m!"demoView:<svg xmlns=\\"http://www.w3.org/2000/svg\\">'
            '<title>{n.getId} {known}</title></svg>"'
        ),
        marker="demoView:",
        command=lambda kind, name, using: f"#demo_view {name}",
    )
    runs = []
    observer = provider_lean.run_observer

    def counted(*arguments, **options):
        runs.append(1)
        return observer(*arguments, **options)

    monkeypatch.setattr(provider_lean, "run_observer", counted)
    views = [(4, "demo", "a", ()), (6, "demo", "b", ()), (8, "demo", "t", ())]
    together = render_project_views(
        source,
        expected_sha256=sha(text),
        provider=provider,
        views=views,
        cancel=threading.Event(),
    )
    assert len(runs) == 1, together
    alone = [
        _render_one(
            source,
            expected_sha256=sha(text),
            line=line,
            provider=provider,
            kind=kind,
            name=name,
            using=using,
            cancel=threading.Event(),
        )
        for line, kind, name, using in views
    ]
    assert len(runs) == 4
    assert together == {"views": alone}, together
    for drawing, name in zip(alone, "abt"):
        assert f"<title>{name} true</title>" in drawing["svg"], drawing
