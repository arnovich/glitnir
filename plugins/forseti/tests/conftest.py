"""Shared fixtures for the Forseti release, session and provider tests.

The live lane tests (``test_stepped_lean.py``, ``test_hedging_lean.py``) check
each proposed block the way the editor does, through `check_block`; the
constants and the budget they are held to are imported from here.
"""

import hashlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

#: The fixtures that give a test a built Forseti Lean checkout.
_LEAN_FIXTURES = frozenset({"lean_test_project", "forseti_lake_project"})
#: The axioms Lean itself provides; a block may rest on nothing else.
STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
#: The longest a block may take to check, in milliseconds, unless the
#: environment says otherwise (`read_check_budget_ms`).
CHECK_BUDGET_MS = 300_000
#: A block that needs nothing, to have the checker replay a notebook's imports.
WARM_BLOCK = "theorem warm_up : True := trivial"


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

    The same reporter is in `backend/tests/conftest.py`; the two test trees are
    separate packages, and register it under different names.

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
        for key, value in report.user_properties:
            if key == CHECK_STAGES:
                label, stage_ms = value
                writer.write_line(f"check stages {label}: {stage_summary(stage_ms)}")


def pytest_configure(config):
    """Report the stage times tests record with `record_check_stages`."""
    config.pluginmanager.register(
        _StageReporter(config), "glitnir-forseti-check-stages"
    )


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


def read_check_budget_ms() -> int:
    """Return the budget a live check is held to.

    An unset or empty ``FORSETI_CHECK_BUDGET_MS`` means `CHECK_BUDGET_MS`.

    Raises:
        pytest.fail.Exception: If the variable is not a positive whole number.
    """
    value = os.environ.get("FORSETI_CHECK_BUDGET_MS", "").strip()
    if not value:
        return CHECK_BUDGET_MS
    try:
        budget = int(value)
    except ValueError:
        budget = 0
    if budget <= 0:
        pytest.fail(
            "FORSETI_CHECK_BUDGET_MS must be a positive whole number of"
            f" milliseconds, got {value!r}",
            pytrace=False,
        )
    return budget


@pytest.fixture
def check_budget_ms() -> int:
    """Return the check budget, failing a test at once if the variable is bad."""
    return read_check_budget_ms()


@dataclass(frozen=True)
class Checked:
    """What the project's Lean said about a proposed block.

    Attributes:
        ok: Whether Glitnir reports the block's theorem as checked.
        message: Glitnir's message, which names the failing line otherwise.
        axioms: The axioms the theorem depends on.
        check_ms: How long the check took, in milliseconds.
        stage_ms: How long each of the check's stages took, in milliseconds.
    """

    ok: bool
    message: str
    axioms: tuple[str, ...]
    check_ms: int
    stage_ms: dict[str, int]


def check_in_scratch(
    write: Callable[[str, str], Path], imports: str, block: str, theorem: str
) -> Checked:
    """Check a block as the editor does, in a scratch notebook of the project.

    The notebook holds the imports, then a directive line; the block is checked
    right after it. ``write`` is the ``scratch_notebook`` fixture's, so no file
    of the checkout is overwritten and only the scratch file is removed.
    """
    from glitnir.lean.project_checking import check_candidate

    header = "".join(f"{line}\n" for line in imports.split("\n"))
    text = f"{header}\n-- @decide {theorem} : (scratch)\n"
    path = write("GlitnirBlockLive", text)
    stage_ms: dict[str, int] = {}
    started = time.monotonic()
    verdict = check_candidate(
        path,
        expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
        insert_after_line=text.count("\n"),
        block=block,
        theorem=theorem,
        cancel=threading.Event(),
        stage_ms=stage_ms,
    )
    elapsed = round((time.monotonic() - started) * 1000)
    return Checked(
        verdict["status"] == "checked",
        verdict["message"],
        tuple(verdict["axioms"]),
        elapsed,
        stage_ms,
    )


@pytest.fixture(scope="module")
def replayed() -> set[str]:
    """The import sets whose one-off kernel replay the session cache holds."""
    return set()


@pytest.fixture
def check_block(scratch_notebook, replayed: set[str], record_check_stages):
    """Return a function that checks a decision outcome's block, timed.

    The outcome's own last stage lists the imports the block needs, so a block
    whose imports are wrong fails here. The first check of an import set
    replays its whole closure in the kernel, which takes minutes and is cached
    for the session (`private_replay_cache`); a trivial block is checked
    first, untimed, so that a block's time is its own, and the block's check
    must reuse that replay rather than replay the imports again. Each check's
    stage times are written to the log (`record_check_stages`).
    """

    def check(outcome: dict) -> Checked:
        assert outcome["stages"][-1][:2] == ["imports", "needed"], outcome["stages"]
        imports = outcome["stages"][-1][2]
        if imports not in replayed:
            warm = check_in_scratch(scratch_notebook, imports, WARM_BLOCK, "warm_up")
            record_check_stages("warm_up", warm.stage_ms)
            assert warm.ok, warm.message
            replayed.add(imports)
            print(f"replay of {imports!r}: {warm.check_ms} ms")
        checked = check_in_scratch(
            scratch_notebook, imports, outcome["block"], outcome["theorem"]
        )
        record_check_stages(outcome["theorem"], checked.stage_ms)
        # The warm-up's import replay was recorded, so this check reuses it:
        # a refused record would replay the closure (minutes) on every check.
        assert "replay_imports" not in checked.stage_ms, checked.stage_ms
        if checked.ok:
            # A checked block was replayed over the recorded imports.
            assert "replay" in checked.stage_ms, checked.stage_ms
        return checked

    return check


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
