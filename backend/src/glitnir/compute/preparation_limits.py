"""Require aggregate service limits before running untrusted Lake preparation."""

import sys
from pathlib import Path, PurePosixPath

from glitnir.lean.project import read_regular


def cgroup_directory() -> Path:
    """Resolve the current unified cgroup, without accepting traversal or v1 limits."""
    if sys.platform != "linux":
        raise ValueError(
            "Handoff preparation requires Linux with bubblewrap and bounded cgroup v2"
        )
    lines = read_regular(Path("/proc/self/cgroup"), 65536).decode().splitlines()
    unified = [line.removeprefix("0::") for line in lines if line.startswith("0::")]
    if (
        len(unified) != 1
        or not unified[0].startswith("/")
        or ".." in PurePosixPath(unified[0]).parts
    ):
        raise ValueError("Cannot resolve the current cgroup v2 resource boundary")
    return Path("/sys/fs/cgroup") / unified[0].lstrip("/")


def require_build_limits(memory_mb: int) -> None:
    """Require a supervisor bounding all children, tmpfs, swap and process count.

    The operator creates this scope; Glitnir never changes service limits or
    writes cgroup controls. The entire preparation, including metadata inspection,
    remains within the same inherited resource boundary.
    """
    try:
        root = cgroup_directory()
        memory = int(read_regular(root / "memory.max", 128))
        swap = int(read_regular(root / "memory.swap.max", 128))
        processes = int(read_regular(root / "pids.max", 128))
        if (
            not 0 < memory <= memory_mb * 1024**2
            or swap != 0
            or not 1 <= processes <= 128
        ):
            raise ValueError("Limits exceed the preparation budget")
    except (OSError, ValueError) as error:
        raise ValueError(
            "Handoff preparation requires cgroup v2 MemoryMax <= memory_mb, "
            "MemorySwapMax=0 and TasksMax <=128; use a bounded systemd scope"
        ) from error
