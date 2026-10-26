"""Offline fixed-workload measurement; never an agent tool or production API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiments.warm_pool.controls import STATEFUL, probes
from experiments.warm_pool.environment import (
    PROGRAM,
    FrozenEnvironment,
    create_fixture,
    freeze,
)
from experiments.warm_pool.resources import Budget
from experiments.warm_pool.runtime import Limits, Pool, wait_for
from glitnir.compute.protocol import checker_digest
from glitnir.lean import artifact_store, replay_cache
from glitnir.lean.project_checking import check_candidate
from glitnir.lean.project_inspection import TOOLCHAIN, _toolchain

WORKLOADS = (
    ("valid", "theorem candidate : True := by trivial\n", "checked"),
    ("repeat-valid", "theorem candidate : True := by trivial\n", "checked"),
)


def validate(
    environment: FrozenEnvironment,
    source: str,
    cancel: threading.Event,
    *,
    checker: Callable[..., dict[str, Any]] = check_candidate,
) -> dict[str, Any]:
    """Run the independent native checker on exactly the submitted candidate."""
    environment.assert_current()
    header = environment.source.read_text()
    if not source.startswith(header) or header.count("\n") != 1:
        raise ValueError("Candidate differs from the frozen single-import header")
    stages: dict[str, int] = {}
    started = time.monotonic()
    verdict = checker(
        environment.source,
        expected_sha256=hashlib.sha256(header.encode()).hexdigest(),
        insert_after_line=1,
        block=source[len(header) :],
        theorem="candidate",
        cancel=cancel,
        stage_ms=stages,
    )
    environment.assert_current()
    return {
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "verdict": verdict,
        "stage_ms": stages,
        "elapsed_ms": (time.monotonic() - started) * 1000,
    }


def measure(root: Path, emit: Callable[[dict[str, Any]], None]) -> None:
    """Measure serial reuse separately from a two-client, bounded burst probe.

    All advisory responses precede serial native validation, so the warm pool
    is closed before native helper processes start. Actual time-to-final is
    measured across this barrier; it is not an interactive production latency.
    """
    original_base, original_replay = (
        replay_cache.cache_base,
        replay_cache.cache_directory,
    )
    original_temp = tempfile.tempdir
    original_limits = {
        key: os.environ.get(key)
        for key in ("GLITNIR_MEMORY_LIMIT_MB", "GLITNIR_ELABORATION_TIMEOUT_MS")
    }
    cache = root / "cache"
    scratch = root / "scratch"
    scratch.mkdir()
    tempfile.tempdir = str(scratch)
    replay_cache.cache_base = lambda: cache
    os.environ.update(
        GLITNIR_MEMORY_LIMIT_MB="4096", GLITNIR_ELABORATION_TIMEOUT_MS="120000"
    )
    try:
        with Budget(root) as budget:
            with artifact_store.lease(
                cache / "glitnir/artifacts", prune=False
            ) as store:
                emit(
                    {
                        "event": "manifest",
                        "format": "glitnir.warm-benchmark/v1",
                        "started_utc": datetime.now(timezone.utc).isoformat(),
                        "experiment_sha256": hashlib.sha256(
                            b"".join(
                                path.name.encode() + b"\0" + path.read_bytes()
                                for path in sorted(PROGRAM.parent.iterdir())
                                if path.suffix in {".py", ".lean"}
                            )
                        ).hexdigest(),
                        "toolchain": TOOLCHAIN,
                        "checker_sha256": checker_digest(),
                        "worker_sha256": hashlib.sha256(
                            PROGRAM.read_bytes()
                        ).hexdigest(),
                        "platform": platform.system(),
                        "machine": platform.machine(),
                        "logical_cpus": os.cpu_count(),
                        "limits": {
                            "disk_bytes": budget.disk_bytes,
                            "seconds": 1800,
                            "memory_mb": 4096,
                            "workers": 2,
                        },
                        "arrival": (
                            "serial advisory, then serial native validation; "
                            "separate burst probe"
                        ),
                        "cold": (
                            "new fixture build and snapshot, "
                            "OS page cache not flushed"
                        ),
                    }
                )
                seed_started = time.monotonic()
                seed_source = create_fixture(
                    root / "seed-project", _toolchain(), profile="Basic", budget=budget
                )
                freeze(seed_source, root / "seed-snapshot", store, budget)
                emit(
                    {
                        "event": "store-seed",
                        "elapsed_ms": (time.monotonic() - seed_started) * 1000,
                        "shared_by_all_scenarios": True,
                    }
                )
                shutil.rmtree(root / "seed-project")
                shutil.rmtree(root / "seed-snapshot")
                controls = root / "controls"
                source = create_fixture(
                    controls / "project", _toolchain(), profile="State", budget=budget
                )
                control_environment = freeze(
                    source, controls / "snapshot", store, budget
                )
                probes(control_environment, budget, emit)
                replay_cache.cache_directory = lambda: cache / "glitnir/replay-controls"
                for index in range(2):
                    final = validate(
                        control_environment,
                        source.read_text() + STATEFUL,
                        budget.cancel,
                    )
                    emit({"event": "control-final", "index": index, "final": final})
                    assert final["verdict"]["status"] == "checked", final
                for name, block, advisory_status in (
                    (
                        "admitted",
                        "theorem candidate : True := by sorry\n",
                        "elaborated",
                    ),
                    ("invalid", "theorem candidate : False := by trivial\n", "errors"),
                ):
                    text = source.read_text() + block
                    with Pool(Limits(request_seconds=120)) as pool:
                        job = pool.submit(control_environment, text, client=name)
                        wait_for(job, budget.cancel)
                        result = job.future.result()
                    emit(
                        {
                            "event": "negative-advisory",
                            "control": name,
                            "receipt": result,
                        }
                    )
                    assert result["advisory"]["status"] == advisory_status, result
                    final = validate(control_environment, text, budget.cancel)
                    emit({"event": "negative-final", "control": name, "final": final})
                    assert final["verdict"]["status"] == "rejected", final
                shutil.rmtree(controls)
                for profile, modes in (
                    ("Basic", ("cold", "prepared", "warm")),
                    ("Standard", ("warm", "prepared", "cold")),
                ):
                    for mode in modes:
                        scenario = root / f"{profile}-{mode}"
                        scenario.mkdir()
                        started = time.monotonic()
                        replay = cache / "glitnir" / f"replay-{profile}-{mode}"
                        replay_cache.cache_directory = lambda: replay
                        preparations = []
                        candidates = []
                        rows = []
                        limits = Limits(
                            workers=1,
                            max_requests=8 if mode == "warm" else 1,
                            request_seconds=120,
                        )
                        with Pool(limits) as pool:
                            environment: FrozenEnvironment | None = None
                            for index, (name, block, expected) in enumerate(WORKLOADS):
                                budget.check()
                                preparation = 0.0
                                if environment is None or mode == "cold":
                                    before = time.monotonic()
                                    source = create_fixture(
                                        scenario / f"project-{index}",
                                        _toolchain(),
                                        profile=profile,
                                        budget=budget,
                                    )
                                    environment = freeze(
                                        source,
                                        scenario / f"snapshot-{index}",
                                        store,
                                        budget,
                                    )
                                    preparation = (time.monotonic() - before) * 1000
                                source_text = environment.source.read_text() + block
                                job = pool.submit(
                                    environment,
                                    source_text,
                                    client=f"client-{index % 2}",
                                )
                                # Cancel this ID on a breach; never relaunch it.
                                while not job.future.done():
                                    if budget.cancel.wait(0.1):
                                        job.cancel.set()
                                response = job.future.result()
                                if response["state"] != "completed":
                                    raise ValueError(
                                        f"Advisory did not complete: {response}"
                                    )
                                if response["advisory"]["status"] != "elaborated":
                                    raise ValueError(
                                        "A valid timing candidate did not elaborate"
                                    )
                                row = {
                                    "event": "candidate",
                                    "profile": profile,
                                    "mode": mode,
                                    "workload": name,
                                    "index": index,
                                    "preparation_ms": preparation,
                                    "advisory": response,
                                }
                                rows.append(row)
                                preparations.append(preparation)
                                candidates.append(
                                    (environment, source_text, expected, job.submitted)
                                )
                                emit({**row, "event": "advisory"})
                        for row, (environment, text, expected, submitted) in zip(
                            rows, candidates
                        ):
                            final = validate(environment, text, budget.cancel)
                            if final["verdict"]["status"] != expected:
                                raise ValueError(
                                    f"Native control disagreed: {row['workload']}"
                                    f": {final}"
                                )
                            emit(
                                {
                                    **row,
                                    "final": final,
                                    "time_to_final_from_submit_ms": (
                                        time.monotonic() - submitted
                                    )
                                    * 1000,
                                }
                            )
                        emit(
                            {
                                "event": "scenario",
                                "profile": profile,
                                "mode": mode,
                                "wall_ms": (time.monotonic() - started) * 1000,
                                "preparation_total_ms": sum(preparations),
                                "peak_owned_bytes_sampled": budget.peak_bytes,
                            }
                        )
                        shutil.rmtree(scenario)
                        shutil.rmtree(replay, ignore_errors=True)
                emit(
                    {"event": "finished", "peak_owned_bytes_sampled": budget.peak_bytes}
                )
    finally:
        replay_cache.cache_base, replay_cache.cache_directory = (
            original_base,
            original_replay,
        )
        tempfile.tempdir = original_temp
        for key, value in original_limits.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    """Create new owned scratch and an exclusive path-free JSONL evidence file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as output:

        def emit(row: dict[str, Any]) -> None:
            output.write(json.dumps(row, sort_keys=True) + "\n")
            output.flush()
            print(
                row["event"],
                row.get("profile", ""),
                row.get("mode", ""),
                row.get("workload", ""),
                flush=True,
            )

        with tempfile.TemporaryDirectory(prefix="glitnir-warm-benchmark-") as directory:
            measure(Path(directory), emit)


if __name__ == "__main__":
    main()
