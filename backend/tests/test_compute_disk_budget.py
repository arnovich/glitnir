"""Large registered images retain combined disk and per-tree traversal bounds."""

import os

import pytest

from glitnir.compute import disk_budget


def test_project_and_private_stage_have_separate_traversal_bounds(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(disk_budget, "MAX_ITEMS", 8)
    for name in ("project", "stage", "build-home", "other"):
        tree = tmp_path / name
        tree.mkdir()
        for number in range(6):
            (tree / str(number)).write_bytes(b"x")
    disk_budget.check_disk_budget(tmp_path, 24)
    with pytest.raises(ValueError, match="disk budget"):
        disk_budget.check_disk_budget(tmp_path, 23)


@pytest.mark.parametrize("tree", ["project", "stage", "build-home", "other"])
def test_each_tree_still_has_a_finite_item_budget(tmp_path, monkeypatch, tree):
    monkeypatch.setattr(disk_budget, "MAX_ITEMS", 8)
    root = tmp_path / tree
    root.mkdir()
    for number in range(9):
        (root / str(number)).mkdir()
    with pytest.raises(ValueError, match="traversal budget"):
        disk_budget.check_disk_budget(tmp_path, 100)


def test_nested_scope_names_do_not_reset_item_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_budget, "MAX_ITEMS", 8)
    tree = tmp_path / "project" / "stage" / "project"
    tree.mkdir(parents=True)
    for number in range(7):
        (tree / str(number)).touch()
    with pytest.raises(ValueError, match="traversal budget"):
        disk_budget.check_disk_budget(tmp_path, 100)


def test_symlinks_are_counted_without_following_targets(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 1000)
    entry = tmp_path / "entry"
    entry.mkdir()
    link = entry / "escape"
    link.symlink_to(outside, target_is_directory=True)
    size = link.lstat().st_size
    disk_budget.check_disk_budget(entry, size)
    with pytest.raises(ValueError, match="disk budget"):
        disk_budget.check_disk_budget(entry, size - 1)
    with pytest.raises(OSError):
        disk_budget.check_disk_budget(link, 10000)


def test_non_regular_files_are_refused(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ValueError, match="non-regular"):
        disk_budget.check_disk_budget(tmp_path, 100)


def test_scan_checks_liveness_with_bounded_work(tmp_path, monkeypatch):
    for number in range(600):
        (tmp_path / str(number)).touch()
    checks = 0
    stats = 0
    original = os.stat

    def count_stat(*args, **kwargs):
        nonlocal stats
        stats += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(os, "stat", count_stat)

    def cancel():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise InterruptedError("deadline reached")

    with pytest.raises(InterruptedError, match="deadline"):
        disk_budget.check_disk_budget(tmp_path, 100, cancel)
    assert checks == 2
    assert stats <= 256


def test_deep_directories_have_an_explicit_depth_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_budget, "MAX_DEPTH", 4)
    (tmp_path / "a/b/c/d/e").mkdir(parents=True)
    with pytest.raises(ValueError, match="depth"):
        disk_budget.check_disk_budget(tmp_path, 100)


@pytest.mark.parametrize("replacement", ["missing", "link"])
def test_directory_renames_never_follow_a_replacement_link(
    tmp_path, monkeypatch, replacement
):
    import shutil

    entry, outside = tmp_path / "entry", tmp_path / "outside"
    child = entry / "stage"
    child.mkdir(parents=True)
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 1000)
    original = os.open

    def replace(path, *args, **kwargs):
        if path == "stage" and kwargs.get("dir_fd") is not None:
            shutil.rmtree(child)
            if replacement == "link":
                child.symlink_to(outside, target_is_directory=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace)
    if replacement == "missing":
        disk_budget.check_disk_budget(entry, 100)
    else:
        with pytest.raises(OSError):
            disk_budget.check_disk_budget(entry, 100)


def test_liveness_callback_never_recursively_scans_or_skips_outer_budget(tmp_path):
    from types import SimpleNamespace

    from glitnir.compute.environments import EnvironmentCache

    cache = EnvironmentCache(tmp_path / "cache")
    (cache.root / "large").write_bytes(b"xx")
    recipe = SimpleNamespace(max_bytes=1)
    checks = 0

    def monitor():
        nonlocal checks
        checks += 1
        cache.check_budget(cache.root, recipe, monitor)

    with pytest.raises(ValueError, match="disk budget"):
        cache.check_budget(cache.root, recipe, monitor)
    assert checks == 1
    # Exceptions must release the guard for the next complete check.
    with pytest.raises(ValueError, match="disk budget"):
        cache.check_budget(cache.root, recipe)
