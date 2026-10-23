"""Shared fixtures for the Forseti release, session and provider tests."""

import os
from pathlib import Path

import pytest

#: The fixtures that give a test a built Forseti Lean checkout.
_LEAN_FIXTURES = frozenset({"lean_test_project", "forseti_lake_project"})


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Under the Lean gate a Lean test that skips fails instead.

    The gate (``FORSETI_REQUIRE_LEAN_PROJECT=1``) is how CI runs the Lean
    tests, so a skip there would pass with nothing checked, as the backend's
    live job refuses too. A skip a test declares (a ``skipif`` mark, such as
    another platform's sandbox probes) and one for the optional ``simulation``
    input stay skips.
    """
    report = yield
    if (
        report.skipped
        and os.environ.get("FORSETI_REQUIRE_LEAN_PROJECT") == "1"
        and _LEAN_FIXTURES & set(getattr(item, "fixturenames", ()))
        and item.get_closest_marker("simulation") is None
        and not any(mark.args and mark.args[0] for mark in item.iter_markers("skipif"))
    ):
        reason = report.longrepr
        if isinstance(reason, tuple):  # (path, line, reason)
            reason = reason[-1]
        report.outcome = "failed"
        report.longrepr = f"A Lean test skipped under the Lean gate: {reason}"
    return report


#: The `user_properties` key a test's project checks are recorded under.
CHECK_STAGES = "check_stages"


class _StageReporter:
    """Write each recorded check's stage times after its test's outcome.

    CI captures test output, so a print would show only for a failure; this
    line is in every log and says where a check's time goes.
    """

    def __init__(self, config) -> None:
        self._config = config

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        if report.when != "call":
            return
        from glitnir.lean.project_checking import stage_summary

        writer = self._config.pluginmanager.get_plugin("terminalreporter")
        if writer is None:
            return
        for key, (label, stage_ms) in report.user_properties:
            if key == CHECK_STAGES:
                writer.write_line(f"check stages {label}: {stage_summary(stage_ms)}")


def pytest_configure(config):
    """Report the stage times tests record with `record_check_stages`."""
    config.pluginmanager.register(_StageReporter(config), "glitnir-check-stages")


@pytest.fixture
def record_check_stages(request):
    """Return ``record(label, stage_ms)``, which logs one check's stage times."""

    def record(label: str, stage_ms: dict[str, int]) -> None:
        request.node.user_properties.append((CHECK_STAGES, (label, dict(stage_ms))))

    return record


@pytest.fixture(scope="session")
def session_replay_cache(tmp_path_factory):
    """One private replay cache directory shared by this session's live tests."""
    return tmp_path_factory.mktemp("replay-cache") / "glitnir" / "replay"


@pytest.fixture(autouse=True)
def private_replay_cache(request, tmp_path_factory, session_replay_cache, monkeypatch):
    """Keep records of replayed imports out of the developer's real cache.

    Live tests against a Forseti checkout share one session cache, so the
    minutes-long kernel replay of Mathlib and Forseti runs once per session;
    every other test gets its own empty cache.
    """
    if "forseti_lake_project" in request.fixturenames:
        directory = session_replay_cache
    else:
        directory = tmp_path_factory.mktemp("replay-cache") / "glitnir" / "replay"
    monkeypatch.setattr("glitnir.lean.replay_cache.cache_directory", lambda: directory)


@pytest.fixture
def lean_release_project(tmp_path, monkeypatch):
    """Model pinned installation files without requiring a Lean checkout."""
    import hashlib
    import json

    from glitnir_forseti import lean_release as release

    project = tmp_path / "lean-project"
    project.mkdir()
    (project / "lean-toolchain").write_text(release.TOOLCHAIN + "\n")
    (project / "lake-manifest.json").write_bytes(release.LOCKFILE_BYTES)
    roots = release.package_roots(project, json.loads(release.LOCKFILE_BYTES))
    pins = []
    for package, relative, _ in release.SOURCE_PINS:
        content = f"fixture {package}/{relative}\n".encode()
        path = roots[package] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        pins.append((package, relative, hashlib.sha256(content).hexdigest()))
    monkeypatch.setattr(release, "SOURCE_PINS", tuple(pins))
    return project


@pytest.fixture(scope="session")
def lean_test_project():
    """Opt into live Lean tests using a configured standalone release checkout."""
    import os
    import shutil

    configured = os.environ.get("FORSETI_LEAN_TEST_PROJECT") or os.environ.get(
        "FORSETI_LEAN_PROJECT"
    )
    # This skip is why a stale pin can go unnoticed: with neither variable set,
    # every live Lean test silently passes. A gate that must exercise the pin
    # sets FORSETI_REQUIRE_LEAN_PROJECT=1 and gets a loud failure instead.
    if os.environ.get("FORSETI_REQUIRE_LEAN_PROJECT") == "1":
        assert (
            configured
        ), "Lean gate requires FORSETI_LEAN_PROJECT or FORSETI_LEAN_TEST_PROJECT"
        assert shutil.which("lake") is not None, "Lean gate requires lake on PATH"
    if not configured or shutil.which("lake") is None:
        pytest.skip("requires a configured standalone Lean test project and lake")
    from glitnir_forseti.lean_release import verify_project

    project = Path(configured).resolve()
    verify_project(project)
    assert (
        project / ".lake/build/lib/lean/Gimle/Forseti.olean"
    ).is_file(), "build the standalone release first"
    return project


@pytest.fixture(scope="session")
def forseti_lake_project():
    """Opt into live tests that need any built Forseti Lean checkout.

    Glitnir's project mode checks against whatever the project builds, so these
    tests do not bind the audited release pin the way `lean_test_project` does.
    """
    import os
    import shutil

    configured = os.environ.get("FORSETI_LEAN_TEST_PROJECT") or os.environ.get(
        "FORSETI_LEAN_PROJECT"
    )
    if os.environ.get("FORSETI_REQUIRE_LEAN_PROJECT") == "1":
        assert (
            configured
        ), "Lean gate requires FORSETI_LEAN_PROJECT or FORSETI_LEAN_TEST_PROJECT"
        assert shutil.which("lake") is not None, "Lean gate requires lake on PATH"
    if not configured or shutil.which("lake") is None:
        pytest.skip("requires a configured Forseti Lean checkout and lake")
    project = Path(configured).resolve()
    assert (
        project / ".lake/build/lib/lean/Gimle/Forseti.olean"
    ).is_file(), "build the Forseti Lean checkout first"
    return project


@pytest.fixture
def scratch_notebook(forseti_lake_project):
    """Return ``write(prefix, text)``, which adds a notebook to the checkout.

    Glitnir's runner reads imports only from a Lake workspace, so the notebook
    is a new module ``<prefix><random hex>`` of the checkout's
    ``Gimle.Forseti.Tests`` library, created exclusively so that no file of the
    checkout is overwritten. Every notebook written, and its results file, is
    removed afterwards.
    """
    import os
    import secrets

    from glitnir.notebook.check_records import records_path

    written: list[Path] = []

    def write(prefix: str, text: str) -> Path:
        folder = forseti_lake_project / "Gimle/Forseti/Tests"
        path = folder / f"{prefix}{secrets.token_hex(4)}.lean"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        written.append(path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        return path

    try:
        yield write
    finally:
        for path in written:
            path.unlink(missing_ok=True)
            records_path(path).unlink(missing_ok=True)


@pytest.fixture
def live_children():
    """List this process's live children from /proc, to assert none survive."""
    import os

    def children() -> list[int]:
        if not Path("/proc").is_dir():
            return []
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[1]) == os.getpid() and fields[0] != "Z":
                found.append(int(entry.name))
        return found

    return children


@pytest.fixture(autouse=True)
def integration_dependencies(request):
    """Require optional inputs only for tests explicitly declaring them."""
    if request.node.get_closest_marker("simulation"):
        pytest.importorskip("gimle.asgard", reason="optional simulation integration")


@pytest.fixture
def fake_asgard():
    """Install the test-only Asgard stand-in (`tests/fake_asgard`) for one test.

    The stand-in's modules replace any `gimle`, `gimle.asgard` and
    `gimle.asgard.rational_interchange` in `sys.modules`; the originals, or
    their absence, are restored afterwards.
    """
    import importlib.util
    import sys

    names = ("gimle", "gimle.asgard", "gimle.asgard.rational_interchange")
    saved = {name: sys.modules[name] for name in names if name in sys.modules}
    root = Path(__file__).parent / "fake_asgard"
    parent = None
    try:
        for name in names:
            path = root.joinpath(*name.split("."))
            if path.is_dir():
                spec = importlib.util.spec_from_file_location(
                    name, path / "__init__.py", submodule_search_locations=[str(path)]
                )
            else:
                spec = importlib.util.spec_from_file_location(
                    name, path.with_suffix(".py")
                )
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            if parent is not None:
                setattr(parent, name.rpartition(".")[2], module)
            parent = module
        yield sys.modules["gimle.asgard"]
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
