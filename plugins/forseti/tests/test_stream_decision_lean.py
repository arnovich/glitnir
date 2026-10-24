"""Decide, accept and project-check ``stream`` decisions through Glitnir.

The editor flow end to end, for the heat field of the solution
``u = x^2 + 2t``: one bound that holds (proven) and one that fails at a corner of
the box (refuted). Each decision is run, its checked candidate block is inserted
after the directive and the notebook saved (a copy of ``insertion()`` in
``extension/src/accept-decision.ts``), and the whole notebook is then checked by
the project runner, the proof authority. It needs a built standalone Forseti Lean
release checkout like the other live Lean tests and runs under
``FORSETI_REQUIRE_LEAN_PROJECT``; the first check replays Mathlib and Forseti
once, and a later one reuses that replay.
"""

import hashlib

import pytest

pytest.importorskip("z3", reason="deciding needs the z3 extra")

#: Allowed for one decision or project check, in seconds (a cold replay is slow).
TIMEOUT_S = 3600

HEAD = "stream formal-heat interpretation evaluated-field basis ogf profile x^2"

NOTEBOOK = f"""import Gimle.Forseti.Syntax.Certificate
import Gimle.Forseti.Examples.HeatFieldBound
import Gimle.Asgard.Examples.PolynomialHeat

/-!
# Stream decisions through Glitnir's project runner

The heat reconstruction with profile $x^2$ has the solution $u = x^2 + 2t$.
On the strip $[0,1] \\times [-1,1]$ it lies between $0$ and $3$.
-/

-- @decide heat_band : {HEAD}
--   field u over t in [0, 1], x in [-1, 1] : u >= 0 and u <= 3

/-! The bound $u \\le 2$ fails at the corner $(t, x) = (1, 1)$. -/

-- @decide heat_below_two : {HEAD}
--   field u over t in [0, 1], x in [-1, 1] : u <= 2
"""

DECISIONS = [
    ("heat_band", "proven", "Decided_heat_band.heat_band"),
    ("heat_below_two", "refuted", "Decided_heat_below_two.heat_below_two_refuted"),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _accept(path, candidate: dict) -> None:
    """Insert the block after the directive and save, as the editor's Accept does."""
    text = path.read_text(encoding="utf-8")
    assert _sha(text) == candidate["sha256"]
    lines = text.split("\n")
    after = candidate["insert_after_line"]
    lines[after:after] = [candidate["block"]]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_live_stream_decisions_are_accepted_and_project_checked(
    scratch_notebook,
) -> None:
    from glitnir.service.preview import SourcePreviewService

    path = scratch_notebook("GlitnirStreamDecisionLive", NOTEBOOK)
    service = SourcePreviewService(path)
    try:
        for key, status, _ in DECISIONS:
            text = path.read_text(encoding="utf-8")
            line = next(
                n
                for n, row in enumerate(text.split("\n"), 1)
                if row.startswith(f"-- @decide {key} ")
            )
            service.run_decision(_sha(text), line)
            service._decision_worker.join(timeout=TIMEOUT_S)
            assert not service._decision_worker.is_alive(), "the run did not finish"
            card = next(c for c in service.snapshot()["decisions"] if c["line"] == line)
            assert card["run_error"] is None, card["run_error"]
            assert card["outcome"]["status"] == status, card
            assert card["verification"]["status"] == "checked", card["verification"]
            _accept(path, service.decision_candidate(card["key"]))

        text = path.read_text(encoding="utf-8")
        service.check(_sha(text))
        service._worker.join(timeout=TIMEOUT_S)
        assert not service._worker.is_alive(), "the project check did not finish"
        snapshot = service.snapshot()
        check = snapshot["preview"]["check"]
        assert check["status"] == "checked_snapshot", check
        checked = {t["name"]: t for t in check["theorems"]}
        for _, _, theorem in DECISIONS:
            name = next(n for n in checked if n.endswith(theorem))
            assert checked[name]["status"] == "checked", checked[name]
        assert [c["status"] for c in snapshot["decisions"]] == ["accepted"] * 2
    finally:
        service.close()
