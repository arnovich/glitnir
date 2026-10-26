"""Untrusted preparation requires aggregate kernel resource boundaries."""

import pytest

from glitnir.compute.preparation_limits import require_build_limits


@pytest.mark.parametrize(
    "memory,swap,pids",
    [
        (str(4096 * 1024**2), "0", "128"),
        ("max", "0", "128"),
        (str(8192 * 1024**2), "0", "128"),
        ("1024", "max", "128"),
        ("1024", "0", "max"),
        ("1024", "0", "129"),
    ],
)
def test_requires_bounded_memory_swap_and_process_tree(
    tmp_path, monkeypatch, memory, swap, pids
):
    monkeypatch.setattr(
        "glitnir.compute.preparation_limits.cgroup_directory", lambda: tmp_path
    )
    for name, value in (
        ("memory.max", memory),
        ("memory.swap.max", swap),
        ("pids.max", pids),
    ):
        (tmp_path / name).write_text(value)
    if memory == str(4096 * 1024**2) and swap == "0" and pids == "128":
        require_build_limits(4096)
    else:
        with pytest.raises(ValueError, match="cgroup"):
            require_build_limits(4096)
