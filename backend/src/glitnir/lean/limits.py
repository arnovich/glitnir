"""Resource ceilings for one Lean elaboration, apart from any Lean release.

Project checking, proof observation and provider tools share these bounds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .environment import MAX_HEARTBEATS, MAX_OUTPUT_BYTES, MAX_TIMEOUT_MS

#: Environment variable overriding the wall-clock ceiling on one elaboration.
ELABORATION_TIMEOUT_VARIABLE = "GLITNIR_ELABORATION_TIMEOUT_MS"

#: The wall-clock ceiling on one elaboration when nothing overrides it.
DEFAULT_ELABORATION_TIMEOUT_MS = 30_000


def elaboration_timeout_ms(default: int = DEFAULT_ELABORATION_TIMEOUT_MS) -> int:
    """Return the wall-clock ceiling on one elaboration.

    The deterministic budget is Lean's heartbeat limit; this ceiling only
    guards against a hung process. On a loaded machine 30s is not enough for
    work the heartbeat budget allows, so a caller such as the benchmark can
    raise it for every tool at once through `GLITNIR_ELABORATION_TIMEOUT_MS`.

    Returns:
        The configured ceiling in milliseconds, or the default.

    Raises:
        ValueError: If the variable is set but not a positive integer.
    """
    configured = os.environ.get(ELABORATION_TIMEOUT_VARIABLE)
    if not configured:
        return default
    if not configured.isdigit() or int(configured) <= 0:
        raise ValueError(
            f"{ELABORATION_TIMEOUT_VARIABLE} must be a positive integer, "
            f"not {configured!r}"
        )
    return int(configured)


#: The wall-clock ceiling on one saved-file check (and on the project inspection
#: that precedes it) when nothing overrides it.
CHECK_TIMEOUT_MS = 600_000


def check_seconds() -> float:
    """Return the deadline for one saved-file check, in seconds.

    The editor's timeout setting reaches the backend as
    `GLITNIR_ELABORATION_TIMEOUT_MS`; it is capped like proof-step observations.
    Project inspection, which resolves the module setup before a check, uses the
    same bound: a whole-Mathlib import makes `lake setup-file` take minutes.
    """
    return min(elaboration_timeout_ms(default=CHECK_TIMEOUT_MS), MAX_TIMEOUT_MS) / 1000


#: Environment variable overriding Lean's memory limit (`lean -M`), in megabytes.
MEMORY_LIMIT_VARIABLE = "GLITNIR_MEMORY_LIMIT_MB"

#: The most memory Lean gets when nothing overrides it. Decision candidate
#: checks exceed 4096 MB in Lean's interpreter, and a view or replay on top of
#: Mathlib holds about 4-5 GB of library files before any work begins.
DEFAULT_MEMORY_LIMIT_MB = 16384

#: Memory left to the rest of the machine when the default is reduced to fit.
RESERVED_MEMORY_MB = 2048

#: The smallest and largest memory limit a setting can choose.
MIN_MEMORY_LIMIT_MB = 1024
MAX_MEMORY_LIMIT_MB = 32768


def physical_memory_mb() -> int | None:
    """Return the machine's physical memory in megabytes, or None if unknown."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20
    except (ValueError, OSError, AttributeError):
        return None


def _default_memory_limit_mb() -> int:
    physical = physical_memory_mb()
    if physical is None:
        return DEFAULT_MEMORY_LIMIT_MB
    return max(
        MIN_MEMORY_LIMIT_MB,
        min(DEFAULT_MEMORY_LIMIT_MB, physical - RESERVED_MEMORY_MB),
    )


def memory_limit_mb() -> int:
    """Return the memory limit passed to every Lean run as ``-M``, in megabytes.

    The editor's `glitnir.memoryLimitMb` setting reaches the backend as
    `GLITNIR_MEMORY_LIMIT_MB`; the command-line check reads the same variable.
    A value that is not a positive decimal integer of at most six digits falls
    back to the default, and a valid value is clamped to the supported range.
    The default is 16384, or 2048 less than the machine's physical memory when
    that is smaller, but never below 1024.

    Returns:
        The configured limit, between 1024 and 32768, or the default.
    """
    configured = os.environ.get(MEMORY_LIMIT_VARIABLE, "")
    if (
        len(configured) > 6
        or not (configured.isascii() and configured.isdigit())
        or int(configured) <= 0
    ):
        return _default_memory_limit_mb()
    return min(max(int(configured), MIN_MEMORY_LIMIT_MB), MAX_MEMORY_LIMIT_MB)


def _bounded(value: object, label: str, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be a non-Boolean integer")
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    if value > maximum:
        raise ValueError(f"{label} exceeds its {maximum} limit")
    return value


@dataclass(frozen=True, slots=True)
class SessionLimits:
    """Resource ceilings applied to one elaboration."""

    timeout_ms: int = 30_000
    max_heartbeats: int = 500_000
    max_output_bytes: int = 262_144

    def __post_init__(self) -> None:
        """Reject non-integer, non-positive or over-cap limits."""
        _bounded(self.timeout_ms, "timeout_ms", MAX_TIMEOUT_MS)
        _bounded(self.max_heartbeats, "max_heartbeats", MAX_HEARTBEATS)
        _bounded(self.max_output_bytes, "max_output_bytes", MAX_OUTPUT_BYTES)
