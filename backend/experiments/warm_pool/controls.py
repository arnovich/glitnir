"""Fixed native probes for reset boundaries, cancellation and process loss."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from experiments.warm_pool.environment import FrozenEnvironment
from experiments.warm_pool.resources import Budget
from experiments.warm_pool.runtime import Limits, Pool, wait_for

STATEFUL = (
    "theorem candidate : True := by\n"
    "  run_tac do\n"
    "    let n ← PoolBench.counter.get\n"
    '    unless n == 0 do throwError "imported tactic state leaked"\n'
    "    PoolBench.counter.set 1\n"
    "  trivial\n"
)


def probes(
    environment: FrozenEnvironment,
    budget: Budget,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """Require declaration isolation and expose imported mutable IO persistence."""
    header = environment.source.read_text()
    valid = header + "theorem candidate : True := by trivial\n"

    def done(pool: Pool, text: str, label: str) -> dict[str, Any]:
        budget.check()
        job = pool.submit(environment, text, client=label)
        while not job.future.done():
            if budget.cancel.wait(0.1):
                job.cancel.set()
        result = job.future.result()
        emit({"event": "control", "control": label, "receipt": result})
        return result

    with Pool(Limits(request_seconds=120)) as pool:
        changed = done(
            pool,
            header + "namespace Leaked\nset_option maxRecDepth 1\n",
            "change-scopes",
        )
        assert changed["state"] == "completed", changed
        reset = done(
            pool,
            header
            + (
                "run_cmd do\n"
                "  unless (← Lean.getCurrNamespace).isAnonymous do\n"
                '    throwError "namespace leaked"\n'
                "  if Lean.maxRecDepth.get (← Lean.getOptions) == 1 then\n"
                '    throwError "option leaked"\n'
            )
            + "theorem candidate : True := by trivial\n",
            "scope-option-reset",
        )
        assert reset["advisory"]["status"] == "elaborated", reset
        first = done(pool, header + STATEFUL, "mutable-first")
        second = done(pool, header + STATEFUL, "mutable-second")
        assert first["advisory"]["status"] == "elaborated", first
        assert second["advisory"]["status"] == "errors", second
        assert any(
            "tactic state leaked" in x for x in second["advisory"]["errors"]
        ), second
        assert second["generation"] == first["generation"]
    with Pool(Limits(request_seconds=120)) as pool:
        fresh = done(pool, header + STATEFUL, "mutable-fresh-process")
        assert fresh["advisory"]["status"] == "elaborated", fresh
        crashed = done(
            pool, header + "#eval (IO.Process.exit 23 : IO Unit)\n", "process-loss"
        )
        assert crashed["state"] == "interrupted", crashed
        recovered = done(pool, valid, "after-process-loss")
        assert recovered["state"] == "completed", recovered
        assert recovered["generation"] != crashed["generation"]
        assert recovered["request_id"] != crashed["request_id"]
        job = pool.submit(
            environment, header + "#eval IO.sleep 5000\n", client="cancel"
        )
        wait_for(job, budget.cancel, started=True)
        assert job.started.is_set(), "Lean never consumed the request"
        assert not job.future.done()
        job.cancel.set()
        cancelled = job.future.result(10)
        emit({"event": "control", "control": "inflight-cancel", "receipt": cancelled})
        assert cancelled["state"] == "cancelled", cancelled
        final = done(pool, valid, "after-cancel")
        assert final["state"] == "completed", final
        assert final["generation"] != cancelled["generation"]
    with Pool(Limits(request_seconds=120, max_requests=2)) as pool:
        results = [done(pool, valid, "quota-recycle") for _ in range(3)]
        assert results[0]["generation"] == results[1]["generation"]
        assert results[2]["generation"] != results[1]["generation"]
    for workers in (1, 2):
        with Pool(Limits(workers=workers, request_seconds=120)) as pool:
            jobs = [
                pool.submit(environment, valid, client=f"burst-{i % 2}")
                for i in range(4)
            ]
            for job in jobs:
                while not job.future.done():
                    if budget.cancel.wait(0.1):
                        job.cancel.set()
                result = job.future.result()
                emit(
                    {
                        "event": "burst",
                        "processes_initially_cold": True,
                        "workers": workers,
                        "receipt": result,
                    }
                )
                assert result["state"] == "completed", result

    # These are real fixture mutations, not a stubbed changed flag. Restore
    # bytes and timestamps so the remaining independently checked probes keep
    # their original input identity.
    for label, path in (
        ("saved-source", environment.source),
        ("configuration", environment.source.parent.parent / "lakefile.toml"),
        ("compiled-artifact", next(environment.source.parent.parent.rglob("*.olean"))),
    ):
        original, info = path.read_bytes(), path.stat()
        try:
            path.write_bytes(original + b" ")
            try:
                environment.assert_current()
            except ValueError:
                emit(
                    {
                        "event": "control",
                        "control": label,
                        "changed_input_refused": True,
                    }
                )
            else:
                raise AssertionError(f"Changed {label} was accepted")
        finally:
            path.write_bytes(original)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        environment.assert_current()
