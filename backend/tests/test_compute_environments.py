"""Registered builds stay pinned, bounded and separate across jobs."""

from dataclasses import replace

import pytest

from glitnir.compute.environments import EnvironmentCache, load_registry
from glitnir.lean.project_inspection import TOOLCHAIN


def test_registry_requires_exact_supported_recipes(tmp_path):
    path = tmp_path / "registry.toml"
    recipe = f"""[[environments]]
project = "lab"
environment = "a"
repository = "https://example.test/project.git"
commit = "{'a' * 40}"
toolchain = "{TOOLCHAIN}"
targets = ["Example"]
max_bytes = 10000000
"""
    path.write_text(recipe)
    assert load_registry(path)[0].environment == "a"
    for bad in (
        recipe.replace("a" * 40, "main"),
        recipe.replace(TOOLCHAIN, "nightly"),
        recipe.replace('["Example"]', '["--help"]'),
        recipe.replace('["Example"]', '"Example"'),
        recipe + recipe,
    ):
        path.write_text(bad)
        with pytest.raises(ValueError):
            load_registry(path)


def test_pinned_a_b_a_builds_once_and_detects_corruption(
    registered_repositories, tmp_path
):
    recipes, run, builds = registered_repositories
    cache = EnvironmentCache(tmp_path / "cache", max_entries=2, max_bytes=40000000)
    for recipe in (recipes[0], recipes[1], recipes[0]):
        with cache.prepare(recipe, run, lambda: None) as entry:
            expected = 1 if recipe.environment == "a" else 2
            assert (entry / "project/Helper.lean").read_text() == (
                f"def value : Nat := {expected}\n"
            )
    assert len(builds) == 2
    with cache.prepare(recipes[0], run, lambda: None) as entry:
        source = entry / "project/Helper.lean"
        source.chmod(0o600)
        source.write_text("def value : Nat := 99\n")
    with pytest.raises(ValueError, match="integrity"):
        with cache.prepare(recipes[0], run, lambda: None):
            pass
    assert len(builds) == 2


def test_failure_and_cancellation_never_publish(registered_repositories, tmp_path):
    recipes, run, _ = registered_repositories
    cache = EnvironmentCache(tmp_path / "cache", max_entries=2, max_bytes=40000000)

    def fail(command, cwd):
        if "build" in command:
            raise ValueError("build failed")
        return run(command, cwd)

    with pytest.raises(ValueError, match="build failed"):
        with cache.prepare(recipes[0], fail, lambda: None):
            pass
    assert cache.ready(recipes) == []
    assert not list(cache.root.glob(".building-*"))

    def cancelled():
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        with cache.prepare(recipes[0], run, cancelled):
            pass
    assert cache.ready(recipes) == []


def test_lru_and_candidate_stage_share_budget(registered_repositories, tmp_path):
    recipes, run, builds = registered_repositories
    cache = EnvironmentCache(tmp_path / "cache", max_entries=1, max_bytes=20000000)
    for recipe in (recipes[0], recipes[1], recipes[0]):
        with cache.prepare(recipe, run, lambda: None):
            pass
    assert len(builds) == 3
    with pytest.raises(ValueError, match="budget"):
        with cache.prepare(recipes[0], run, lambda: None) as entry:
            (entry / "stage").mkdir()
            (entry / "stage/oversize").write_bytes(b"x" * recipes[0].max_bytes)
            cache.check_budget(entry, recipes[0])
    assert cache.ready(recipes) == []


def test_toolchain_mismatch_prevents_build(registered_repositories, tmp_path):
    recipes, run, builds = registered_repositories
    with pytest.raises(ValueError, match="toolchain"):
        replace(recipes[0], toolchain="wrong")
    assert not builds


def test_source_symlinks_are_not_materialized(tmp_path):
    from glitnir.compute.environments import _materialize_links

    root = tmp_path / "project"
    root.mkdir()
    (root / "data").write_text("theorem x : True := by trivial")
    (root / "Example.lean").symlink_to("data")
    with pytest.raises(ValueError, match="link"):
        _materialize_links(root)
    (root / "Example.lean").unlink()
    (root / "artifact").symlink_to("data")
    _materialize_links(root)
    assert not (root / "artifact").is_symlink()
    assert (root / "artifact").read_bytes() == (root / "data").read_bytes()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_budget_scan_tolerates_disappearing_download_files(tmp_path, monkeypatch, kind):
    """Download files and directories can disappear after enumeration."""
    import os
    import shutil
    from types import SimpleNamespace

    cache = EnvironmentCache(tmp_path / "cache")
    entry = cache.root / ".building-test"
    entry.mkdir()
    temporary = entry / "download.part"
    if kind == "file":
        temporary.write_bytes(b"partial")
    else:
        temporary.mkdir()
        (temporary / "large").write_bytes(b"partial")
    (entry / "persistent").write_bytes(b"x")
    original = os.stat

    def stat(path, *args, **kwargs):
        if path == temporary.name and kwargs.get("dir_fd") is not None:
            if kind == "file":
                temporary.unlink()
            else:
                shutil.rmtree(temporary)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    cache.check_budget(entry, SimpleNamespace(max_bytes=1))


def test_inventory_still_refuses_a_disappearing_prepared_file(tmp_path, monkeypatch):
    """Immutable reuse/inventory checks must retain their fail-closed behavior."""
    from pathlib import Path

    from glitnir.compute.environments import _inventory

    source = tmp_path / "Example.lean"
    source.write_text("def original := 1\n")
    original = Path.lstat

    def stat(path):
        if path == source:
            source.unlink()
        return original(path)

    monkeypatch.setattr(Path, "lstat", stat)
    with pytest.raises(FileNotFoundError):
        _inventory(tmp_path, lambda: None)


def test_budget_scan_does_not_hide_permission_errors(tmp_path, monkeypatch):
    """Only vanished entries are expected while trusted build tools are writing."""
    import os
    from types import SimpleNamespace

    cache = EnvironmentCache(tmp_path / "cache")
    source = cache.root / "protected"
    source.write_bytes(b"x")
    original = os.stat

    def stat(path, *args, **kwargs):
        if path == source.name and kwargs.get("dir_fd") is not None:
            raise PermissionError("Unreadable build file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(PermissionError):
        cache.check_budget(cache.root, SimpleNamespace(max_bytes=100))


def test_live_switching_preserves_each_projects_propositions(
    registered_repositories, project_toolchain, monkeypatch, tmp_path
):
    """Real cold A/B and warm A run native sandbox inspection and kernel replay."""
    import subprocess
    import threading
    from pathlib import Path

    from glitnir.compute.protocol import make_request
    from glitnir.compute.worker import execute

    recipes, _, _ = registered_repositories
    monkeypatch.setattr(
        "glitnir.compute.environments._toolchain", lambda: project_toolchain
    )
    builds = []

    def run(command, cwd):
        if "build" in command:
            builds.append(cwd)
        return subprocess.check_output(
            command, cwd=cwd, text=True, stderr=subprocess.STDOUT, timeout=180
        )

    cache = EnvironmentCache(tmp_path / "cache", max_entries=2, max_bytes=100000000)
    # Real artifacts need more space than the source-only fixtures.
    recipes = [replace(r, max_bytes=50000000) for r in recipes]
    requests = [
        make_request(Path(r.repository) / "Example.lean", r.environment, 600, 4096)
        for r in recipes
    ]
    for index in (0, 1, 0):
        recipe = recipes[index]
        with cache.prepare(recipe, run, lambda: None) as entry:
            result = execute(
                entry / "project", requests[index], entry / "stage", threading.Event()
            )
            assert result["status"] == "checked_snapshot", result
            assert [(t["name"], t["status"]) for t in result["theorems"]] == [
                ("result", "checked")
            ], result
            assert result.get("withheld") is None, result
            # Same module names from B cannot satisfy A's exact context.
            wrong = {**requests[1 - index], "environment": recipe.environment}
            rejected = execute(
                entry / "project", wrong, entry / "stage", threading.Event()
            )
            assert rejected["status"] == "unavailable", rejected
            assert "context differs" in rejected["message"], rejected
    assert len(builds) == 2


def test_capacity_reserves_future_stage_growth(registered_repositories, tmp_path):
    recipes, run, _ = registered_repositories
    cache = EnvironmentCache(tmp_path / "cache", max_entries=2, max_bytes=15000000)
    with cache.prepare(recipes[0], run, lambda: None):
        pass
    with cache.prepare(recipes[1], run, lambda: None):
        pass
    # Each small build reserves ten MB for its eventual candidate stage.
    assert cache.ready(recipes) == ["b"]


def test_smaller_limits_are_applied_to_warm_hits(registered_repositories, tmp_path):
    recipes, run, _ = registered_repositories
    cache = EnvironmentCache(tmp_path / "cache", max_entries=2, max_bytes=40000000)
    for recipe in recipes:
        with cache.prepare(recipe, run, lambda: None):
            pass
    smaller = EnvironmentCache(cache.root, max_entries=1, max_bytes=10000000)
    with smaller.prepare(recipes[0], run, lambda: None):
        pass
    assert smaller.ready(recipes) == ["a"]


def test_live_registered_git_dependency_checks_and_refuses_admission(
    registered_git_dependency, tmp_path
):
    import threading
    from pathlib import Path

    from glitnir.compute.protocol import make_request
    from glitnir.compute.worker import execute

    recipe, run = registered_git_dependency
    source = Path(recipe.repository) / "Example.lean"
    cache = EnvironmentCache(tmp_path / "cache", max_bytes=100000000)
    with cache.prepare(recipe, run, lambda: None) as entry:
        for text, expected in [
            ("import Dep\ntheorem result : depValue = 7 := rfl\n", "checked"),
            ("import Dep\ntheorem result : depValue = 8 := by sorry\n", "admitted"),
        ]:
            source.write_text(text)
            request = make_request(source, recipe.environment, 600, 4096)
            result = execute(
                entry / "project", request, entry / "stage", threading.Event()
            )
            assert result["status"] == "checked_snapshot", result
            assert [(t["name"], t["status"]) for t in result["theorems"]] == [
                ("result", expected)
            ], result
            assert result.get("withheld") is None, result


def test_opt_in_mathlib_cache_precedes_build(registered_repositories, tmp_path):
    recipes, run, _ = registered_repositories
    recipe = replace(recipes[0], mathlib_cache=True)
    commands = []

    def record(command, cwd):
        commands.append(command)
        if command[-3:] == ["exe", "cache", "get"]:
            return ""
        return run(command, cwd)

    cache = EnvironmentCache(tmp_path / "cache", max_bytes=20000000)
    with cache.prepare(recipe, record, lambda: None):
        pass
    cached = next(
        i for i, cmd in enumerate(commands) if cmd[-3:] == ["exe", "cache", "get"]
    )
    built = next(i for i, cmd in enumerate(commands) if "build" in cmd)
    assert cached < built
    assert recipe.key(cache.checker) != recipes[0].key(cache.checker)
    with pytest.raises(ValueError, match="boolean"):
        replace(recipe, mathlib_cache="yes")


def test_failed_mathlib_download_does_not_fall_back_to_build(
    registered_repositories, tmp_path
):
    recipes, run, builds = registered_repositories
    recipe = replace(recipes[0], mathlib_cache=True)

    def unavailable(command, cwd):
        if command[-3:] == ["exe", "cache", "get"]:
            raise ValueError("download failed")
        return run(command, cwd)

    cache = EnvironmentCache(tmp_path / "cache", max_bytes=20000000)
    with pytest.raises(ValueError, match="download failed"):
        with cache.prepare(recipe, unavailable, lambda: None):
            pass
    assert not builds
    assert cache.ready([recipe]) == []


@pytest.mark.parametrize("marker", [None, '{"path":'])
def test_restart_discards_incomplete_stage_but_reuses_build(
    registered_repositories, tmp_path, marker
):
    recipes, run, builds = registered_repositories
    cache = EnvironmentCache(tmp_path / "cache", max_bytes=20000000)
    with cache.prepare(recipes[0], run, lambda: None) as entry:
        stage = entry / "stage"
        stage.mkdir()
        (stage / "partial-copy").write_text("interrupted")
        if marker is not None:
            (stage / "ready").touch()
            (stage / "candidate.json").write_text(marker)
    with cache.prepare(recipes[0], run, lambda: None) as entry:
        assert not (entry / "stage").exists()
    assert len(builds) == 1
