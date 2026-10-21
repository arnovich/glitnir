"""Shared fixtures for the project-mode Lean checking tests."""

import os

import pytest

#: `Example.Helper` as `prepared_lean_project` builds it.
EXAMPLE_HELPER = "def helper : Nat := 1\n"


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


@pytest.fixture(autouse=True)
def no_startup_sweep(monkeypatch):
    """Keep startup sweeps out of the developer's real temp directory."""
    monkeypatch.setattr("glitnir.notebook.__main__.sweep_stale_scratch", lambda: None)
    monkeypatch.setattr("glitnir.notebook.server.sweep_stale_scratch", lambda: None)


@pytest.fixture(autouse=True)
def no_caffeinate(monkeypatch):
    """Start no caffeinate, which tests patching `subprocess.Popen` would catch."""
    monkeypatch.setattr("glitnir.lean.awake._enabled", lambda: False)


@pytest.fixture
def draw_each_alone(monkeypatch):
    """Fake the preview's view runs with a fake of one view's drawing.

    `draw_each_alone(render)` replaces `source_preview.render_project_views`
    with a run that draws each of its views with `render`, called as
    `render_project_view` is: the notebook, then `line`, `kind`, `name`,
    `using` and the run's other options.
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

        monkeypatch.setattr("glitnir.notebook.source_preview.render_project_views", run)

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
