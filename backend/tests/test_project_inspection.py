"""Explicit project inspection is confined, bounded and never a proof check."""

import hashlib
import json
import threading

import pytest

from glitnir.lean import project_inspection as inspection


def test_missing_or_unsupported_setup_never_launches(tmp_path, monkeypatch):
    """Loose files, absent manifests and unsupported versions stay readable."""
    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := True.intro\n")
    monkeypatch.setattr(inspection, "_run", lambda *a, **k: pytest.fail("launched"))
    assert inspection.inspect_project(source)["status"] == "unavailable"
    (tmp_path / "lakefile.toml").write_text('name = "test"')
    (tmp_path / "lean-toolchain").write_text(inspection.TOOLCHAIN)
    assert "manifest" in inspection.inspect_project(source)["message"]
    (tmp_path / "lake-manifest.json").write_text("{}")
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.99.0")
    assert "4.32.1" in inspection.inspect_project(source)["message"]


class _Clock:
    """A monotonic clock the test advances; sleeping in the poll loop is skipped."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


@pytest.mark.parametrize(
    "configured, seconds", [(None, 600), ("90000", 90), ("3600000", 1800)]
)
def test_inspection_deadline_follows_check_time_limit(
    tmp_path, monkeypatch, configured, seconds
):
    """The deadline is the configured check limit (default 600 s), not a constant."""
    import subprocess

    if configured is None:
        monkeypatch.delenv("GLITNIR_ELABORATION_TIMEOUT_MS", raising=False)
    else:
        monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", configured)
    clock = _Clock()

    class Process:
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout):
            # Each poll advances the clock by one second of simulated work.
            clock.now += 1
            raise subprocess.TimeoutExpired("lake", timeout)

    monkeypatch.setattr(inspection.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(inspection, "confine", lambda command, **kwargs: command)
    monkeypatch.setattr(inspection.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(inspection, "kill_and_reap", lambda process: None)
    with pytest.raises(ValueError, match="timed out"):
        inspection._run(["--version"], tmp_path, tmp_path, threading.Event())
    assert clock.now == seconds


def test_explicit_inspection_timeout_overrides_check_limit(tmp_path, monkeypatch):
    """A caller answering a quick request can pass a shorter bound to every run."""
    source = tmp_path / "A.lean"
    source.write_text("example : True := True.intro\n")
    (tmp_path / "lakefile.toml").write_text('name = "test"')
    (tmp_path / "lean-toolchain").write_text(inspection.TOOLCHAIN)
    (tmp_path / "lake-manifest.json").write_text("{}")
    limits = []

    def run(arguments, *args, timeout_seconds=None, **kwargs):
        limits.append(timeout_seconds)
        return "Lake version 0"

    monkeypatch.setattr(inspection, "_toolchain", lambda: tmp_path)
    monkeypatch.setattr(inspection, "_run", run)
    inspection.inspect_project(source, timeout_seconds=7)
    assert len(limits) == 1 and 6 < limits[0] <= 7


def test_the_inspection_is_bounded_as_a_whole(tmp_path, monkeypatch):
    """Two Lake runs share one limit, so an inspection never takes twice it."""
    clock = _Clock()
    limits = []

    def run(arguments, *rest, timeout_seconds=None, program=None, **kwargs):
        limits.append(timeout_seconds)
        clock.now += 40
        if program is None:
            return "Lake version 5.0.0-src+x (Lean version 4.32.1)"
        raise ValueError("stop here")

    monkeypatch.setattr(inspection.time, "monotonic", clock.monotonic)
    source = _inspectable(tmp_path, monkeypatch, run)
    inspection.inspect_project(source, timeout_seconds=100)
    assert limits == [100, 60]
    limits.clear()
    clock.now = 0
    result = inspection.inspect_project(source, timeout_seconds=30)
    assert limits == [30]
    assert result == {
        "status": "unavailable",
        "message": "Project inspection timed out.",
    }


def _inspectable(tmp_path, monkeypatch, run):
    source = tmp_path / "A.lean"
    source.write_text("example : True := True.intro\n")
    (tmp_path / "lakefile.toml").write_text('name = "test"')
    (tmp_path / "lean-toolchain").write_text(inspection.TOOLCHAIN)
    (tmp_path / "lake-manifest.json").write_text("{}")
    monkeypatch.setattr(inspection, "_toolchain", lambda: tmp_path)
    monkeypatch.setattr(inspection, "_run", run)
    return source


def test_extra_imports_are_named_to_the_setup_program(tmp_path, monkeypatch):
    arguments = []

    def run(args, *rest, program=None, **kwargs):
        arguments.append(args)
        if program is None:
            return "Lake version 5.0.0-src+x (Lean version 4.32.1)"
        raise ValueError("stop here")

    source = _inspectable(tmp_path, monkeypatch, run)
    inspection.inspect_project(source, imports=("Gimle.Asgard.Visualization.Model",))
    assert arguments[-1] == [
        "setup-file",
        str(source.absolute()),
        "Gimle.Asgard.Visualization.Model",
    ]
    arguments.clear()
    inspection.inspect_project(source)
    assert arguments[-1] == ["setup-file", str(source.absolute())]


@pytest.mark.parametrize("module", ["-x", "A..B", "A B", "A\n#eval 1", ""])
def test_invalid_extra_imports_never_reach_lake(tmp_path, monkeypatch, module):
    source = _inspectable(tmp_path, monkeypatch, lambda *a, **k: pytest.fail("ran"))
    result = inspection.inspect_project(source, imports=(module,))
    assert result["status"] == "unavailable"
    assert "Invalid module name" in result["message"]


def test_an_out_of_date_import_is_marked_as_needing_a_build(tmp_path, monkeypatch):
    """Lake's `--no-build` exit status says a build is needed; nothing is built."""

    def run(args, *rest, program=None, **kwargs):
        if program is None:
            return "Lake version 5.0.0-src+x (Lean version 4.32.1)"
        raise inspection.NeedsBuild("Lake could not inspect this prepared project.")

    source = _inspectable(tmp_path, monkeypatch, run)
    result = inspection.inspect_project(source, imports=("Demo.View",))
    assert result == {
        "status": "unavailable",
        "message": "Lake could not inspect this prepared project.",
        "needs_build": True,
    }


@pytest.mark.parametrize("code, kind", [(3, "NeedsBuild"), (1, "ValueError")])
def test_only_the_no_build_exit_status_needs_a_build(tmp_path, monkeypatch, code, kind):
    class Process:
        returncode = code

        def poll(self):
            return code

    monkeypatch.setattr(inspection, "confine", lambda command, **kwargs: command)
    monkeypatch.setattr(inspection.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(inspection, "kill_and_reap", lambda process: None)
    with pytest.raises(ValueError) as raised:
        inspection._run(
            ["setup-file"], tmp_path, tmp_path, threading.Event(), program=tmp_path
        )
    assert type(raised.value).__name__ == kind


def test_sandbox_opt_out_is_never_honored(tmp_path, monkeypatch):
    """Even the legacy opt-out cannot start project code without confinement."""
    from glitnir.lean import sandbox

    monkeypatch.setenv("GLITNIR_UNSANDBOXED", "1")
    monkeypatch.setattr(sandbox, "_bubblewrap", lambda: None)
    monkeypatch.setattr(sandbox, "_seatbelt", lambda: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.confine(["lake"], readable=[], cwd=tmp_path, required=True)


def test_live_standard_and_custom_module_setup(prepared_lean_project):
    """Lake supplies custom source roots, local imports and configured options."""
    source = prepared_lean_project / "src/Example.lean"
    before = {
        p: p.read_bytes() for p in prepared_lean_project.rglob("*") if p.is_file()
    }
    result = inspection.inspect_project(source)
    assert result["status"] == "resolved", result
    assert result["setup"]["name"] == "Example"
    assert "Example.Helper" in result["setup"]["importArts"]
    assert result["setup"]["options"]["autoImplicit"] is False
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "Check saved file" in result["message"]
    assert {
        p: p.read_bytes() for p in prepared_lean_project.rglob("*") if p.is_file()
    } == before
    source.write_text("import Std\nexample : True := True.intro\n")
    assert inspection.inspect_project(source)["status"] == "resolved"


def test_live_missing_and_stale_imports_do_not_build(prepared_lean_project):
    """Inspection explains preparation failures without regenerating artifacts."""
    source = prepared_lean_project / "src/Example.lean"
    helper = prepared_lean_project / "src/Example/Helper.lean"
    artifact = prepared_lean_project / ".lake/build/lib/lean/Example/Helper.olean"
    original = artifact.read_bytes()
    helper.write_text("def helper : Nat := 2\n")
    result = inspection.inspect_project(source)
    assert result["status"] == "unavailable", result
    assert artifact.read_bytes() == original
    artifact.unlink()
    assert inspection.inspect_project(source)["status"] == "unavailable"
    assert not artifact.exists()


@pytest.mark.parametrize("value", [{}, {"name": 1}, {"name": "A", "isModule": "false"}])
def test_invalid_setup_is_rejected(value, tmp_path):
    """Malformed output is not a resolved environment."""
    with pytest.raises(ValueError):
        inspection.validate_setup(value, tmp_path, tmp_path)


def test_artifact_paths_must_be_regular_and_confined(tmp_path):
    """Lake output cannot authorize reads outside the declared roots."""
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    value = dict(
        name="A",
        isModule=False,
        importArts={"B": [str(outside)]},
        options={},
        plugins=[],
        dynlibs=[],
    )
    with pytest.raises(ValueError, match="outside"):
        inspection.validate_setup(value, root, root)
    value["importArts"] = {"B": [str(root)]}
    with pytest.raises(ValueError, match="regular"):
        inspection.validate_setup(value, root, root)


def test_live_cancelled_inspection_does_not_run(prepared_lean_project):
    """Closing a preview cancels its explicit inspection."""
    cancel = threading.Event()
    cancel.set()
    result = inspection.inspect_project(
        prepared_lean_project / "src/Example.lean", cancel=cancel
    )
    assert result["status"] == "unavailable"
    assert "cancel" in result["message"].lower()


def test_live_saved_source_binding_and_configuration_race(
    prepared_lean_project, monkeypatch
):
    """An inspection cannot report the setup for a different saved source/config."""
    source = prepared_lean_project / "src/Example.lean"
    assert (
        "changed"
        in inspection.inspect_project(source, expected_sha256="0" * 64)["message"]
    )
    original = inspection._run

    def mutate(arguments, *args, **kwargs):
        result = original(arguments, *args, **kwargs)
        if "setup-file" in arguments:
            (prepared_lean_project / "lakefile.toml").write_text('name = "changed"')
        return result

    monkeypatch.setattr(inspection, "_run", mutate)
    assert "changed" in inspection.inspect_project(source)["message"]


def test_live_process_limits(prepared_lean_project, monkeypatch):
    """The explicit runner caps output and kills work exceeding its deadline."""
    toolchain = inspection._toolchain()
    monkeypatch.setattr(inspection, "MAX_OUTPUT_BYTES", 20)
    with pytest.raises(ValueError, match="output limit"):
        inspection._run(
            ["--version"], prepared_lean_project, toolchain, threading.Event()
        )
    with pytest.raises(ValueError, match="timed out"):
        inspection._run(
            ["--version"],
            prepared_lean_project,
            toolchain,
            threading.Event(),
            timeout_seconds=0,
        )


def test_live_executable_configuration_cannot_write(prepared_lean_project):
    """Lake configuration initialization stays inside the read-only sandbox."""
    import subprocess

    root = prepared_lean_project
    canary = root / "canary"
    (root / "lakefile.toml").unlink()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage probe where\n\n"
        "initialize writeCanary : Unit ← IO.FS.writeFile "
        f'{json.dumps(str(canary))} "changed"\n'
        'lean_lib Example where\n  srcDir := "src"\n'
    )
    # Prepare only our test-authored configuration outside the inspection sandbox.
    subprocess.run(
        [str(inspection._toolchain() / "bin/lake"), "--keep-toolchain", "resolve-deps"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
    )
    canary.write_text("original")
    result = inspection.inspect_project(root / "src/Example.lean")
    assert result["status"] == "unavailable", result
    assert canary.read_text() == "original"


def test_negative_lean_option_is_not_a_nat(tmp_path):
    """The supported Lean setup schema uses nonnegative numeric options."""
    value = dict(
        name="A",
        isModule=False,
        importArts={},
        options={"maxRecDepth": -1},
        plugins=[],
        dynlibs=[],
    )
    with pytest.raises(ValueError, match="options"):
        inspection.validate_setup(value, tmp_path, tmp_path)


def test_live_cancel_reaps_running_configuration(prepared_lean_project, monkeypatch):
    """Cancellation interrupts a running Lake initializer and reaps its process."""
    import os
    import subprocess
    import time

    root = prepared_lean_project
    (root / "lakefile.toml").unlink()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage probe where\n\n"
        "initialize waitForCancellation : Unit ← do\n"
        '  if (← IO.getEnv "GLITNIR_FIXTURE_PREPARE") != some "1" then\n'
        "    IO.sleep 10000\n"
        'lean_lib Example where\n  srcDir := "src"\n'
    )
    setup = subprocess.run(
        [str(inspection._toolchain() / "bin/lake"), "--keep-toolchain", "resolve-deps"],
        cwd=root,
        env={**os.environ, "GLITNIR_FIXTURE_PREPARE": "1"},
        capture_output=True,
        timeout=60,
    )
    assert setup.returncode == 0, setup.stderr.decode()
    processes = []
    launched = threading.Event()
    popen = subprocess.Popen

    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        if "setup-file" in args[0]:
            launched.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", record)
    cancel = threading.Event()
    answers = []
    worker = threading.Thread(
        target=lambda: answers.append(
            inspection.inspect_project(root / "src/Example.lean", cancel=cancel)
        )
    )
    worker.start()
    try:
        assert launched.wait(10)
        time.sleep(0.3)
        assert worker.is_alive(), answers
        cancel.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert "cancel" in answers[0]["message"].lower()
        assert all(process.poll() is not None for process in processes)
    finally:
        cancel.set()
        worker.join(timeout=10)
