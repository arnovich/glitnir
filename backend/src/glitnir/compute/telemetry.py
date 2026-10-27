"""Optional bounded operational metadata; never a proof verdict or fingerprint."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

FORMAT = "taskvalet.compute-telemetry/v1"
MAX_MS = 86_400_000
NUMBERS = {"elapsed_ms", "preparation_ms", "checking_ms", "staging_ms"}
CACHES = {
    "environment_cache": {"unknown", "prepared", "hit", "miss"},
    "imports_replay": {"unknown", "full", "cached", "incremental"},
}
STAMPS = {
    "submitted",
    "leased",
    "preparing_observed",
    "checking_observed",
    "terminal",
    "terminal_observed",
}


def supports(job: dict[str, Any]) -> bool:
    """Negotiate optional worker reports from the lease's versioned envelope."""
    value = job.get("telemetry")
    return isinstance(value, dict) and value.get("format") == FORMAT


def _milliseconds(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_MS


def observations(value: Any) -> dict[str, Any] | None:
    """Validate numeric/cache observations without accepting verdicts, paths or logs."""
    if not isinstance(value, dict) or set(value) - NUMBERS - CACHES.keys() - {
        "native_stage_ms"
    }:
        return None
    for key, item in value.items():
        if key in NUMBERS and not _milliseconds(item):
            return None
        if key in CACHES and (not isinstance(item, str) or item not in CACHES[key]):
            return None
        if key == "native_stage_ms" and (
            not isinstance(item, dict)
            or len(item) > 32
            or any(
                not isinstance(k, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,47}", k) is None
                or not _milliseconds(v)
                for k, v in item.items()
            )
        ):
            return None
    return dict(value)


def native_observations(result: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only the checker's existing timing and replay-mode observations."""
    value: dict[str, Any] = {}
    stages = {"native_stage_ms": result.get("stage_ms")}
    if observations(stages) is not None:
        value.update(stages)
    replay = result.get("replay")
    if replay == "full":
        value["imports_replay"] = "full"
    elif replay == "imports cached":
        value["imports_replay"] = "cached"
    elif isinstance(replay, str) and re.fullmatch(
        r"imports cached \(\+\d+ modules?\)", replay
    ):
        value["imports_replay"] = "incremental"
    return value


def _timestamp(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _parse(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not supports(job):
        return None
    value = job["telemetry"]
    stamps = value.get("timestamps")
    if not isinstance(stamps, dict) or not STAMPS <= stamps.keys():
        return None
    if not _timestamp(stamps.get("submitted")) or any(
        item is not None and not _timestamp(item)
        for key, item in stamps.items()
        if key in STAMPS
    ):
        return None
    if "worker_observed_at" not in value or (
        value["worker_observed_at"] is not None
        and not _timestamp(value["worker_observed_at"])
    ):
        return None
    raw = value.get("worker_observations")
    worker = observations(raw) if raw is not None else {}
    if worker is None:
        return None
    return stamps, worker


def _span(start: Any, end: Any) -> int | None:
    if start is None or end is None:
        return None
    elapsed = (float(end) - float(start)) * 1000
    return round(elapsed) if math.isfinite(elapsed) and 0 <= elapsed <= MAX_MS else None


def describe(job: dict[str, Any]) -> str:
    """Describe execution costs while retaining legacy status without metrics."""
    state, phase = job.get("state"), job.get("phase")
    label = (
        phase
        if state == "running" and phase in {"preparing", "checking"}
        else str(state)
    )
    parsed = _parse(job)
    if parsed is None:
        return label
    stamps, worker = parsed
    parts = []
    queue = _span(stamps["submitted"], stamps["leased"])
    if queue is not None:
        parts.append(f"queue {queue / 1000:.1f}s")
    for key, title in (
        ("preparation_ms", "reported worker preparation"),
        ("checking_ms", "reported worker checking"),
    ):
        if key in worker:
            parts.append(f"{title} {worker[key] / 1000:.1f}s")
    replay = worker.get("native_stage_ms", {}).get("replay")
    if replay is not None:
        parts.append(f"native replay {replay / 1000:.1f}s")
    cache = worker.get("environment_cache")
    if cache in {"hit", "miss"}:
        parts.append(f"environment cache {cache}")
    mode = worker.get("imports_replay")
    if mode and mode != "unknown":
        parts.append(f"imports {mode}")
    return label + (" (" + "; ".join(parts) + ")" if parts else "")
