"""Shared fixtures for the project-mode Lean checking tests."""

import contextlib
import os
import shutil
from pathlib import Path

import pytest

from glitnir.preview import snapshot as preview_snapshot
from glitnir.service import preview as source_preview

#: `Example.Helper` as `prepared_lean_project` builds it.
EXAMPLE_HELPER = "def helper : Nat := 1\n"


@pytest.fixture
def stub_warm_runtime():
    """A controllable elaborator for scheduler identity and recovery behavior."""
    import threading

    from experiments.warm_pool.runtime import Interrupted

    gate, started = threading.Event(), threading.Event()
    calls = []

    class Environment:
        key = "a" * 64
        changed = False

        def assert_current(self):
            if self.changed:
                raise Interrupted("environment changed")

    class Session:
        def __init__(self, environment, limits, generation):
            self.environment, self.generation = environment, generation
            self.closed, self.requests = False, 0

        def check(self, request):
            calls.append((request.identity, request.source, self.generation))
            self.requests += 1
            if request.source == "blocked":
                started.set()
                assert gate.wait(3)
            if request.source == "crash":
                raise Interrupted("process exited before reply")
            return {"status": "elaborated", "errors": [], "warnings": []}

        def close(self):
            self.closed = True

    try:
        yield Environment(), Session, calls, gate, started
    finally:
        gate.set()


@pytest.fixture
def warm_environment(tmp_path, project_toolchain):
    """A captured public fixture with its own bounded, disposable artifact store."""
    from experiments.warm_pool.environment import create_fixture, freeze
    from experiments.warm_pool.resources import Budget
    from glitnir.lean import artifact_store

    disk_bytes = int(os.environ.get("GLITNIR_WARM_TEST_DISK_BYTES", 3 * 1024**3))
    assert 3 * 1024**3 <= disk_bytes <= 16 * 1024**3
    try:
        with Budget(tmp_path, disk_bytes=disk_bytes) as budget:
            source = create_fixture(
                tmp_path / "warm-base", project_toolchain, budget=budget
            )
            with artifact_store.lease(tmp_path / "store", prune=False) as store:
                yield freeze(source, tmp_path / "warm-snapshot", store, budget)
    finally:
        shutil.rmtree(tmp_path / "warm-snapshot", ignore_errors=True)
        shutil.rmtree(tmp_path / "store", ignore_errors=True)


def _live_gate_set() -> bool:
    """Whether this run requires the live Lean tests rather than skipping them."""
    return (
        os.environ.get("GLITNIR_REQUIRE_PROJECT_INSPECTION") == "1"
        and os.environ.get("GLITNIR_TEST_PROOF_TRACE") == "1"
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Mark every `test_live_` test `live`; CI selects jobs with `-m`."""
    for item in items:
        if getattr(item, "originalname", item.name).startswith("test_live_"):
            item.add_marker(pytest.mark.live)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Under the live gate a live test that skips fails instead.

    The live CI job runs only `live` tests, so a skip there would pass it with
    nothing checked.
    """
    report = yield
    live = item.get_closest_marker("live") is not None
    if report.skipped and live and _live_gate_set():
        reason = report.longrepr
        if isinstance(reason, tuple):  # (path, line, reason)
            reason = reason[-1]
        report.outcome = "failed"
        report.longrepr = f"A live test skipped under the live gate: {reason}"
    return report


#: The `user_properties` key a live test's project checks are recorded under.
CHECK_STAGES = "check_stages"


class _StageReporter:
    """Write each recorded check's stage times after its test's outcome.

    The same reporter is in `plugins/forseti/tests/conftest.py`; the two test trees are
    separate packages, and register it under different names.

    The live jobs capture test output, so a print would show only for a
    failure; this line is in every CI log and says where a check's time goes.
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
    """Report the stage times live tests record (`record_check_stages`)."""
    config.pluginmanager.register(_StageReporter(config), "glitnir-check-stages")


@pytest.fixture(autouse=True)
def record_check_stages(request, monkeypatch):
    """Record the stage times of every project check a live test runs.

    `check_project` and `check_candidate` are wrapped, unchanged otherwise, so
    the live job's log shows each check's stages (`_StageReporter`).
    """
    if request.node.get_closest_marker("live") is None:
        return
    from glitnir.lean import project_checking

    def record(label, stage_ms):
        request.node.user_properties.append((CHECK_STAGES, (label, dict(stage_ms))))

    check_project = project_checking.check_project
    check_candidate = project_checking.check_candidate

    def project(source, **options):
        result = check_project(source, **options)
        if isinstance(result.get("stage_ms"), dict):
            record(f"{request.node.name} {Path(source).name}", result["stage_ms"])
        return result

    def candidate(source, **options):
        stage_ms = options.setdefault("stage_ms", {})
        try:
            return check_candidate(source, **options)
        finally:
            record(f"{request.node.name} {Path(source).name} candidate", stage_ms)

    monkeypatch.setattr(project_checking, "check_project", project)
    monkeypatch.setattr(project_checking, "check_candidate", candidate)


@pytest.fixture(autouse=True)
def no_startup_sweep(monkeypatch):
    """Keep startup sweeps out of the developer's real temp directory."""
    monkeypatch.setattr("glitnir.service.cli.sweep_stale_scratch", lambda: None)
    monkeypatch.setattr("glitnir.service.server.sweep_stale_scratch", lambda: None)


@pytest.fixture(autouse=True)
def no_caffeinate(monkeypatch):
    """Start no caffeinate, which tests patching `subprocess.Popen` would catch."""
    monkeypatch.setattr("glitnir.lean.awake._enabled", lambda: False)


@pytest.fixture
def awake_depth(monkeypatch):
    """Replace a module's `awake` with one that counts the blocks holding it.

    `awake_depth(module)` returns a one-item list holding the current depth,
    so a fake stage can assert that the run keeps the computer awake.
    """

    def install(module):
        depth = [0]

        @contextlib.contextmanager
        def counted():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

        monkeypatch.setattr(module, "awake", counted)
        return depth

    return install


@pytest.fixture
def draw_each_alone(monkeypatch):
    """Fake the preview's view runs with a fake of one view's drawing.

    `draw_each_alone(render)` replaces `source_preview.render_project_views`
    with a run that draws each of its views with `render`, called with
    the notebook, then `line`, `kind`, `name`, `using` and the run's other
    options.
    """

    def install(render):
        def run(path, *, views, **options):
            return {
                "views": [
                    render(
                        path, line=line, kind=kind, name=name, using=using, **options
                    )
                    for line, kind, name, using in views
                ]
            }

        monkeypatch.setattr("glitnir.service.preview.render_project_views", run)

    return install


@pytest.fixture(scope="session")
def session_replay_cache(tmp_path_factory):
    """One private replay cache directory shared by this session's live tests."""
    return tmp_path_factory.mktemp("replay-cache") / "glitnir" / "replay"


@pytest.fixture(autouse=True)
def private_replay_cache(request, tmp_path_factory, session_replay_cache, monkeypatch):
    """Keep records of replayed imports out of the developer's real cache.

    Live tests (anything using `project_toolchain`) share one session cache, so
    the minutes-long kernel replay of an import closure runs once and later
    tests reuse its record; the record is keyed by every imported file, so a
    test that changes its imports simply misses. Every other test gets its own
    empty cache, so stub-driven tests never see another test's entries. A live
    test that needs a cold cache requests `fresh_replay_cache`.
    """
    if "project_toolchain" in request.fixturenames:
        directory = session_replay_cache
    else:
        directory = tmp_path_factory.mktemp("replay-cache") / "glitnir" / "replay"
    monkeypatch.setattr("glitnir.lean.replay_cache.cache_directory", lambda: directory)


@pytest.fixture(scope="session")
def session_artifact_store(tmp_path_factory):
    """One private artifact store shared by this session's live tests."""
    return tmp_path_factory.mktemp("artifact-store") / "glitnir" / "artifacts"


@pytest.fixture(autouse=True)
def private_artifact_store(
    request, tmp_path_factory, session_artifact_store, monkeypatch
):
    """Keep stored snapshot inputs out of the developer's real cache.

    Live tests share one session store, as they share the replay cache, so a
    dependency closure is stored once; every other test gets its own.
    """
    if "project_toolchain" in request.fixturenames:
        directory = session_artifact_store
    else:
        directory = tmp_path_factory.mktemp("artifact-store") / "glitnir" / "artifacts"
    monkeypatch.setattr(
        "glitnir.lean.artifact_store.store_directory", lambda: directory
    )
    return directory


@pytest.fixture
def fresh_replay_cache(private_replay_cache, tmp_path_factory, monkeypatch):
    """Give one test its own empty replay cache, whatever ran before it.

    Required by tests that assert a cold (`full`, `imports-only`) first replay,
    or that record, prune or tamper with entries. Depending on
    `private_replay_cache` makes this patch apply after the default one.
    """
    directory = tmp_path_factory.mktemp("fresh-replay-cache") / "glitnir" / "replay"
    monkeypatch.setattr("glitnir.lean.replay_cache.cache_directory", lambda: directory)
    return directory


@pytest.fixture
def project_toolchain(request):
    """Run live Lean tests only when opted in; then require Lean and a sandbox.

    Each live test copies the project's dependencies (gigabytes with Mathlib)
    into a snapshot, so installing Lean alone must not start them. A test using
    it must be named `test_live_`: CI runs those, and only those, with Lean.
    """
    from glitnir.lean.project_inspection import _toolchain
    from glitnir.lean.sandbox import sandbox_available

    if not getattr(request.node, "originalname", "").startswith("test_live_"):
        pytest.fail("A test needing live Lean must be named test_live_...")
    if os.environ.get("GLITNIR_REQUIRE_PROJECT_INSPECTION") != "1":
        pytest.skip("live Lean test: set GLITNIR_REQUIRE_PROJECT_INSPECTION=1 to run")
    try:
        toolchain = _toolchain()
    except ValueError:
        from glitnir.lean.project_inspection import TOOLCHAIN

        pytest.fail(f"project inspection gate requires installed {TOOLCHAIN}")
    assert sandbox_available(), "live project inspection requires a sandbox"
    return toolchain


def build_example_project(root, toolchain, helper=EXAMPLE_HELPER):
    """Write and build the minimal `Example` project at `root`.

    `Example` imports `Example.Helper`, whose source is `helper`.
    """
    import subprocess

    from glitnir.lean.project_inspection import TOOLCHAIN

    (root / "src/Example").mkdir(parents=True)
    (root / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (root / "lakefile.toml").write_text(
        'name = "example"\n[[lean_lib]]\nname = "Example"\nsrcDir = "src"\n'
        "[lean_lib.leanOptions]\nautoImplicit = false\n"
    )
    (root / "src/Example/Helper.lean").write_text(helper)
    (root / "src/Example.lean").write_text(
        "import Example.Helper\ntheorem result : helper = 1 := rfl\n"
    )
    subprocess.run(
        [str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(toolchain)},
    )
    return root


@pytest.fixture
def prepared_lean_project(tmp_path, project_toolchain):
    """Build a test-owned minimal project with the installed adapter toolchain."""
    return build_example_project(tmp_path / "project", project_toolchain)


@pytest.fixture
def lemma_bundle():
    """The versioned golden wire fixture also tested by Taskvalet."""
    import json

    return json.loads(
        (Path(__file__).parent / "fixtures/lean-lemma-v1.json").read_text()
    )


@pytest.fixture
def stub_handoff(tmp_path, lemma_bundle, monkeypatch):
    """Exercise real durable staging with stubbed build and metadata inspection."""
    import json

    import glitnir.compute.handoff as module
    from glitnir.compute.protocol import context_digest, digest
    from glitnir.lean.project_inspection import TOOLCHAIN

    base = tmp_path / "prepared"
    base.mkdir()
    (base / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (base / "lakefile.toml").write_text(
        'name = "example"\n[[lean_lib]]\nname = "Example"\n'
    )
    (base / ".lake").mkdir()
    value = lemma_bundle
    value["job"]["request"].update(
        checker_sha256=module.checker_digest(),
        context_sha256=context_digest(base / "Candidate.lean"),
    )
    value["sha256"] = digest(
        json.dumps(
            {k: v for k, v in value.items() if k != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    builds = []

    def build(project, *args):
        builds.append(project)
        (project / ".lake/artifact").write_bytes(b"fake unit-test artifact")

    def inspect(source, **kwargs):
        return {
            "status": "resolved",
            "setup": {"name": "Next", "importArts": {"Candidate": [["fake.olean"]]}},
        }

    monkeypatch.setattr(module, "require_build_limits", lambda memory_mb: None)
    monkeypatch.setattr(module, "build_module", build)
    monkeypatch.setattr(module, "inspect_project", inspect)
    return base, value, "import Candidate\ntheorem reused : True := candidate\n", builds


@pytest.fixture
def handoff_project(tmp_path, project_toolchain, lemma_bundle):
    """A public source-root Lake project and a dedicated exact-source module."""
    import json
    import subprocess

    from glitnir.compute.protocol import checker_digest, context_digest, digest
    from glitnir.lean.project_inspection import TOOLCHAIN

    base = tmp_path / "prepared"
    base.mkdir()
    (base / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (base / "lakefile.toml").write_text(
        'name = "example"\n[[lean_lib]]\nname = "Example"\n'
    )
    (base / "Example.lean").write_text("theorem base : True := by trivial\n")
    subprocess.run(
        [str(project_toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=base,
        check=True,
        capture_output=True,
        timeout=180,
    )
    value = lemma_bundle
    request = value["job"]["request"]
    request.update(
        path="Example/Checked.lean",
        checker_sha256=checker_digest(),
        context_sha256=context_digest(base / "Example/Checked.lean"),
    )
    value["job"]["result"]["module"] = "Example.Checked"
    payload = {k: v for k, v in value.items() if k != "sha256"}
    value["sha256"] = digest(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    return base, value, "import Example.Checked\ntheorem reused : True := candidate\n"


@pytest.fixture(scope="session")
def replay_records(tmp_path_factory):
    """Record each base closure's full replay once per session, on first use.

    Returns `record(helper, toolchain)`: the directory of a replay cache holding
    one record, of the closure `Example` imports when `Example.Helper` is
    `helper`, made by a cold full check of a project of its own. Tests never use
    it directly; `recorded_replay_cache` gives each one a private copy.
    """
    import hashlib
    import json

    from glitnir.lean import replay_cache
    from glitnir.lean.project_checking import check_project

    recorded = {}

    def record(helper, toolchain):
        if helper not in recorded:
            root = build_example_project(
                tmp_path_factory.mktemp("recorded-project") / "project",
                toolchain,
                helper,
            )
            directory = (
                tmp_path_factory.mktemp("recorded-replay") / "glitnir" / "replay"
            )
            # Build messages (a linter warning, say) are kept in the `.olean`
            # with the source's absolute path, so no other project's build of
            # the same source would match this record.
            olean = root / ".lake/build/lib/lean/Example/Helper.olean"
            assert (
                str(root).encode() not in olean.read_bytes()
            ), "Example.Helper must build without messages to be recorded once"
            source = root / "src/Example.lean"
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(replay_cache, "cache_directory", lambda: directory)
                result = check_project(source, expected_sha256=digest)
            assert result["status"] == "checked_snapshot", result
            assert result["replay"] == "full", result
            (entry,) = directory.glob("*.json")
            assert json.loads(entry.read_text())["extendable"] is True
            recorded[helper] = directory
        return recorded[helper]

    return record


@pytest.fixture
def recorded_replay_cache(fresh_replay_cache, project_toolchain, replay_records):
    """Start one test's replay cache from a recorded base closure.

    Returns `seed(helper=EXAMPLE_HELPER)`, which copies the session's record of
    that base (see `replay_records`) into this test's own cache and returns it.
    The test then checks over a recorded, `extendable` base without paying its
    full replay, and whatever it records or tampers with stays its own. Tests
    asserting the cold path itself use `fresh_replay_cache` instead.
    """
    import shutil

    def seed(helper=EXAMPLE_HELPER):
        fresh_replay_cache.parent.mkdir(mode=0o700)
        shutil.copytree(replay_records(helper, project_toolchain), fresh_replay_cache)
        return fresh_replay_cache

    return seed


@pytest.fixture
def project_snapshot_inputs(tmp_path):
    """Small inert files for input-capture tests; they are never executed."""
    root = tmp_path / "project"
    toolchain = tmp_path / "toolchain"
    root.mkdir()
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "lib/lean").mkdir(parents=True)
    (toolchain / "bin/lean").write_bytes(b"inert toolchain")
    (toolchain / "lib/lean/Lean.olean").write_bytes(b"trusted library")
    source = root / "A.lean"
    source.write_text("theorem t : True := True.intro\n")
    artifact = root / "Dependency.olean"
    artifact.write_bytes(b"dependency version one")
    setup = {
        "name": "A",
        "isModule": False,
        "importArts": {"Dependency": [[str(artifact)]]},
        "plugins": [],
        "dynlibs": [],
        "options": {},
    }
    return root, toolchain, source, artifact, setup


@pytest.fixture
def prepared_git_project(tmp_path, project_toolchain):
    """Build only test-owned Git dependencies, including a nested transitive one."""
    import json
    import subprocess

    from glitnir.lean.project_inspection import TOOLCHAIN

    toolchain = project_toolchain
    environment = {
        **os.environ,
        "LEAN_SYSROOT": str(toolchain),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }

    def run(arguments, root):
        return subprocess.run(
            arguments,
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            timeout=180,
        ).stdout.decode()

    def repository(name, files):
        directory = tmp_path / (name + "-origin")
        directory.mkdir()
        for filename, contents in files.items():
            target = directory / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
        run(["git", "init", "-b", "main"], directory)
        run(["git", "add", *files.keys()], directory)
        run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "Prepare local dependency fixture",
            ],
            directory,
        )
        return directory, run(["git", "rev-parse", "HEAD"], directory).strip()

    leaf, leaf_rev = repository(
        "leaf",
        {
            "lakefile.toml": 'name = "leaf"\n[[lean_lib]]\nname = "Leaf"\n',
            "Leaf.lean": "def leafValue : Nat := 7\n",
        },
    )
    dep, dep_rev = repository(
        "dep",
        {
            "nested/lakefile.toml": (
                'name = "dep"\n[[require]]\nname = "leaf"\n'
                f'git = {json.dumps(str(leaf))}\nrev = "{leaf_rev}"\n'
                '[[lean_lib]]\nname = "Dep"\n'
            ),
            "nested/Dep.lean": "import Leaf\ndef depValue : Nat := leafValue\n",
        },
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (root / "lakefile.toml").write_text(
        'name = "example"\npackagesDir = "vendor"\n[[require]]\nname = "dep"\n'
        f'git = {json.dumps(str(dep))}\nrev = "{dep_rev}"\nsubDir = "nested"\n'
        '[[lean_lib]]\nname = "Example"\n'
    )
    (root / "Example.lean").write_text(
        "import Dep\ntheorem result : depValue = 7 := rfl\n"
    )
    run([str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"], root)
    return root


@pytest.fixture
def compute_project(tmp_path):
    """A small prepared-project source tree for transport and isolation tests."""
    from glitnir.lean.project_inspection import TOOLCHAIN

    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "example"\n[[lean_lib]]\nname = "Example"\n'
    )
    (root / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (root / "lake-manifest.json").write_text('{"packages": []}\n')
    (root / "Example.lean").write_text("theorem sample : True := by trivial\n")
    (root / "Helper.lean").write_text("theorem helper : True := by trivial\n")
    return root


@pytest.fixture(autouse=True)
def artifact_store_test_space(request, monkeypatch):
    """Keep store unit tests independent of the host's changing free disk space.

    Disk-pressure cases override this sampler explicitly. Live checks continue
    to use the real filesystem reserve.
    """
    if request.node.path.name == "test_artifact_store.py":
        monkeypatch.setattr(
            "glitnir.lean.artifact_store._free_bytes",
            lambda _descriptor: (100 * 1024**3, 1024**4),
        )


@pytest.fixture
def registered_repositories(tmp_path, monkeypatch):
    """Two real pinned Git trees; only the Lake build is substituted."""
    import subprocess

    from glitnir.compute.environments import Recipe
    from glitnir.lean.project_inspection import TOOLCHAIN

    recipes = []
    builds = []
    for name, value in [("a", 1), ("b", 2)]:
        root = tmp_path / name
        root.mkdir()
        (root / "lakefile.toml").write_text(
            'name = "example"\n[[lean_lib]]\nname = "Helper"\n'
            '[[lean_lib]]\nname = "Example"\n'
        )
        (root / "lean-toolchain").write_text(TOOLCHAIN + "\n")
        (root / "lake-manifest.json").write_text(
            '{"version": "1.1.0", "packagesDir": ".lake/packages", '
            '"packages": [], "name": "example", "lakeDir": ".lake"}\n'
        )
        (root / "Helper.lean").write_text(f"def value : Nat := {value}\n")
        (root / "Example.lean").write_text(
            f"import Helper\ntheorem result : value = {value} := rfl\n"
        )
        for args in (
            ["init", "-q"],
            [
                "add",
                "lakefile.toml",
                "lean-toolchain",
                "lake-manifest.json",
                "Helper.lean",
                "Example.lean",
            ],
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        recipes.append(
            Recipe("lab", name, str(root), commit, TOOLCHAIN, ("Example",), 10000000)
        )
    monkeypatch.setattr(
        "glitnir.compute.environments._toolchain", lambda: tmp_path / "toolchain"
    )

    def run(command, cwd):
        if command[0].endswith("/lake") and "--version" in command:
            from glitnir.lean.project_inspection import supported_version

            return f"Lake (Lean version {supported_version()})"
        if command[0].endswith("/lake"):
            builds.append(cwd)
            (cwd / ".lake").mkdir(exist_ok=True)
            (cwd / ".lake/artifact").write_text("built")
            return ""
        return subprocess.check_output(
            command, cwd=cwd, text=True, stderr=subprocess.PIPE
        )

    return recipes, run, builds


@pytest.fixture
def dynamic_worker_case(registered_repositories, tmp_path, monkeypatch):
    """Exercise the actual worker/supervisor with tiny Git trees and a fake Lake."""
    import sys
    import time
    from pathlib import Path

    from glitnir.compute.client import Client, Settings
    from glitnir.compute.environments import EnvironmentCache
    from glitnir.compute.protocol import make_request
    from glitnir.compute.worker import Worker
    from glitnir.lean.project_inspection import supported_version

    recipes, _, _ = registered_repositories
    toolchain = tmp_path / "toolchain"
    (toolchain / "bin").mkdir(parents=True)
    lake = toolchain / "bin/lake"
    lake.write_text(
        f"#!{sys.executable}\nimport sys,time\n"
        'if "--version" in sys.argv: '
        f'print("Lake (Lean version {supported_version()})")\n'
    )
    lake.chmod(0o700)
    cache = EnvironmentCache(tmp_path / "cache", max_bytes=40000000)
    client = Client(Settings("https://tasks.example.test", "lab", "a", "secret"))
    worker = Worker(client, None, tmp_path / "journal", recipes=recipes, cache=cache)
    request = make_request(Path(recipes[0].repository) / "Example.lean", "a", 60, 4096)
    # Check-stage refusal is fast and still exercises the real isolated checker child.
    request["context_sha256"] = "f" * 64
    calls = []

    def send(method, path, body=None):
        calls.append((path, body))
        if path.endswith("/claim"):
            return {
                "job": {
                    "id": request["request_id"],
                    "request": request,
                    "deadline": time.time() + 60,
                },
                "lease": "a" * 64,
            }
        return {"state": "running"}

    monkeypatch.setattr(client, "request", send)
    return worker, lake, calls, request


@pytest.fixture
def registered_git_dependency(tmp_path, project_toolchain):
    """A real locked dependency and root, built locally before freezing its recipe."""
    import json
    import subprocess

    from glitnir.compute.environments import Recipe
    from glitnir.lean.project_inspection import TOOLCHAIN

    def run(args, cwd):
        return subprocess.check_output(
            args, cwd=cwd, text=True, stderr=subprocess.STDOUT, timeout=180
        )

    dependency = tmp_path / "dependency"
    dependency.mkdir()
    (dependency / "lakefile.toml").write_text(
        'name = "dependency"\n[[lean_lib]]\nname = "Dep"\n'
    )
    (dependency / "Dep.lean").write_text("def depValue : Nat := 7\n")

    def commit(root, files):
        run(["git", "init", "-q"], root)
        run(["git", "add", *files], root)
        run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "fixture",
            ],
            root,
        )
        return run(["git", "rev-parse", "HEAD"], root).strip()

    dependency_rev = commit(dependency, ["lakefile.toml", "Dep.lean"])
    root = tmp_path / "root"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "example"\n[[require]]\nname = "dependency"\n'
        + f'git = {json.dumps(str(dependency))}\nrev = "{dependency_rev}"\n'
        + '[[lean_lib]]\nname = "Example"\n'
    )
    (root / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (root / "Example.lean").write_text(
        "import Dep\ntheorem result : depValue = 7 := rfl\n"
    )
    run(
        [str(project_toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        root,
    )
    revision = commit(
        root, ["lakefile.toml", "lean-toolchain", "lake-manifest.json", "Example.lean"]
    )
    return (
        Recipe(
            "lab", "dependency", str(root), revision, TOOLCHAIN, ("Example",), 50000000
        ),
        run,
    )


@pytest.fixture
def patch_fingerprint(monkeypatch):
    """Return a function replacing `environment_fingerprint` where it is used.

    The service's runs call it through `glitnir.service.preview`; the snapshot's
    `Fingerprints` calls it through `glitnir.preview.snapshot`. The fake gets the
    positional arguments only, not the `remote` keyword.
    """

    def patch(fake):
        def call(*args, **_remote):
            return fake(*args)

        monkeypatch.setattr(source_preview, "environment_fingerprint", call)
        monkeypatch.setattr(preview_snapshot, "environment_fingerprint", call)

    return patch
