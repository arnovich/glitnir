"""Decide Forseti goals end to end through Glitnir's project runner.

Opt in like the other live Lean tests, by pointing ``FORSETI_LEAN_TEST_PROJECT``
(or ``FORSETI_LEAN_PROJECT``) at a built standalone Forseti Lean release checkout,
with ``lake`` on ``PATH``. Glitnir's checker also needs Lean 4.35.0 installed
with elan and a working sandbox, and deciding needs Z3. The notebook is written
as a new temporary module of the checkout's ``Gimle.Forseti.Tests`` library,
because the runner reads imports only from a Lake workspace, and is removed
with its results file afterwards. The provider proposes each block and
Glitnir's own check of it is what this test asserts.
"""

import hashlib
import os
import secrets
from pathlib import Path

import pytest

pytest.importorskip("z3", reason="deciding needs the z3 extra")

NOTEBOOK = """import Gimle.Forseti.Syntax.Certificate
import Gimle.Forseti.Examples.ThreeStateContract
import Gimle.Forseti.Examples.HedgedSpreadContract

/-! # Decisions through Glitnir's project runner -/

-- @decide disk : entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1

-- @decide shifted : entailment x : x >= 1 |- x >= 2

-- @decide energy_band : trajectory three-state from (1, 2, -1) at 2
--   observe V always : V >= 0 and V <= 6

-- @decide energy_below_five : trajectory three-state from (1, 2, -1) at 2
--   observe V always : V <= 5

-- @decide hedged_band : trajectory hedged-spread from (0, 0) at 0 within 1
--   observe s always : s >= -1 and s <= 1

-- @decide hedged_quarter : trajectory hedged-spread from (0, 0) at 0 within 1
--   observe V always : V >= 1/4
"""


def _line(prefix: str) -> int:
    lines = NOTEBOOK.split("\n")
    return next(n for n, line in enumerate(lines, 1) if line.startswith(prefix))


@pytest.fixture
def notebook(forseti_lake_project):
    """Create `NOTEBOOK` in the test project; remove it and its results after."""
    from glitnir.notebook.check_records import records_path

    folder = forseti_lake_project / "Gimle/Forseti/Tests"
    path = folder / f"GlitnirDecisionLive{secrets.token_hex(4)}.lean"
    # Exclusive: never overwrite a file of the checkout.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(NOTEBOOK)
        yield path
    finally:
        path.unlink(missing_ok=True)
        records_path(path).unlink(missing_ok=True)


def _decide(path: Path, line: int) -> dict:
    """Decide the directive at `line` through the preview service; return its card."""
    from glitnir.notebook.source_preview import SourcePreviewService

    sha = hashlib.sha256(NOTEBOOK.encode()).hexdigest()
    service = SourcePreviewService(path)
    try:
        service.run_decision(sha, line)
        service._decision_worker.join(timeout=2400)
        assert not service._decision_worker.is_alive(), "the run did not finish"
        return next(c for c in service.snapshot()["decisions"] if c["line"] == line)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("prefix", "status", "theorem"),
    [
        ("-- @decide disk", "proven", "disk"),
        ("-- @decide shifted", "refuted", "shifted_refuted"),
        ("-- @decide energy_band", "proven", "energy_band"),
        ("-- @decide energy_below_five", "refuted", "energy_below_five_refuted"),
        # A driven model: the readable claim carries the driver premises.
        ("-- @decide hedged_band", "proven", "hedged_band"),
        ("-- @decide hedged_quarter", "refuted", "hedged_quarter_refuted"),
    ],
)
def test_live_decisions_are_checked_by_the_project(
    notebook, prefix: str, status: str, theorem: str
) -> None:
    card = _decide(notebook, _line(prefix))
    assert card["provider"] == "forseti", card
    assert card["run_error"] is None, card["run_error"]
    assert card["outcome"]["status"] == status, card
    assert card["outcome"]["theorem"] == theorem
    assert card["verification"]["status"] == "checked", card["verification"]
    assert card["status"] == "candidate"
    # Nothing is inserted until the editor accepts the block.
    assert notebook.read_text(encoding="utf-8") == NOTEBOOK
