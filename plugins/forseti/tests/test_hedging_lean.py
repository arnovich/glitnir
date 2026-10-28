r"""Live Lean check of every block template the hedging lane proposes.

The hedging decider replays nothing: a decided goal is only a proposed block,
and the project's Lean is what makes it a proof. This test takes each example
through the notebook's decision provider, as the editor does, writes a scratch
notebook that imports what the block needs, and has Glitnir check the saved
file followed by the block (``check_candidate``, the one proof authority for a
decision). It runs against any built Forseti Lean checkout holding the hedging
library, and fails instead of skipping under the gate::

    FORSETI_REQUIRE_LEAN_PROJECT=1 \
    FORSETI_LEAN_PROJECT=/path/to/lean-checkout \
        uv run python -m pytest tests/test_hedging_lean.py

Glitnir's check replays the block's whole import closure in the kernel itself,
once per session, and the block over it; the plugin's replay pin plays no part
in it.

Beside the examples, which fix the worked example's call (strike and spot 100,
twenty steps), variants reach what those constants do not: the lane's caps,
where a ``shortfall`` refutation has the kernel evaluate the hedge along a
path of 60 steps and a ``covers`` block evaluates a price over 250, and the
straddle, whose names no example cites. A check is held to the budget the
stepped live test defines (`test_stepped_lean.CHECK_BUDGET_MS`, five minutes
unless ``FORSETI_CHECK_BUDGET_MS`` names another number of milliseconds).

Each scratch notebook is a new module of the checkout's ``Gimle.Forseti.Tests``
library, written by the ``scratch_notebook`` fixture, which creates it
exclusively and removes only what it created.
"""

import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from glitnir.lean.project_checking import check_candidate
from glitnir.providers.outcomes import checked_decision
from test_stepped_lean import STANDARD_AXIOMS, WARM_BLOCK, Checked, _check_budget_ms

from glitnir_forseti.claims.text import render_goal
from glitnir_forseti.decision_provider import decide, parse
from glitnir_forseti.hedging.examples import EXAMPLES, Example

#: The worked example's goals, as the variants below change them.
COVERS = (
    "hedging call strike 100 spot 100 band down 1/50 up 1/50 steps 20"
    " premium 711/200 covers"
)
SHORTFALL = (
    "hedging call strike 100 spot 100 band down 1/100 up 1/100 steps 20"
    " against down 1/50 up 1/50 shortfall <= 4"
)


@pytest.fixture
def check_budget_ms() -> int:
    """Return the check budget, failing a test at once if the variable is bad."""
    return _check_budget_ms()


def _check(
    write: Callable[[str, str], Path], imports: str, block: str, theorem: str
) -> Checked:
    """Check a block as the editor does, in a scratch notebook of the project.

    The notebook holds the imports, then a directive line; the block is checked
    right after it. ``write`` is the ``scratch_notebook`` fixture's, so no file
    of the checkout is overwritten and only the scratch file is removed.
    """
    header = "".join(f"{line}\n" for line in imports.split("\n"))
    text = f"{header}\n-- @decide {theorem} : (scratch)\n"
    path = write("GlitnirHedgingLive", text)
    stage_ms: dict[str, int] = {}
    started = time.monotonic()
    verdict = check_candidate(
        path,
        expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
        insert_after_line=text.count("\n"),
        block=block,
        theorem=theorem,
        cancel=threading.Event(),
        stage_ms=stage_ms,
    )
    elapsed = round((time.monotonic() - started) * 1000)
    return Checked(
        verdict["status"] == "checked",
        verdict["message"],
        tuple(verdict["axioms"]),
        elapsed,
        stage_ms,
    )


@pytest.fixture(scope="module")
def replayed() -> set[str]:
    """The import sets whose one-off kernel replay the session cache holds."""
    return set()


@pytest.fixture
def check_block(scratch_notebook, replayed: set[str], record_check_stages):
    """Return a function that checks a decision outcome's block, timed.

    The outcome's own last stage lists the imports the block needs, so a block
    whose imports are wrong fails here. The first check of an import set
    replays its whole closure in the kernel, which takes minutes and is cached
    for the session (`conftest.private_replay_cache`); a trivial block is
    checked first, untimed, so that a block's time is its own, and the block's
    check must reuse that replay rather than replay the imports again. Each
    check's stage times are written to the log (`record_check_stages`).
    """

    def check(outcome: dict) -> Checked:
        assert outcome["stages"][-1] == [
            "imports",
            "needed",
            "import Gimle.Forseti.Hedging",
        ], outcome["stages"]
        imports = outcome["stages"][-1][2]
        if imports not in replayed:
            warm = _check(scratch_notebook, imports, WARM_BLOCK, "warm_up")
            record_check_stages("warm_up", warm.stage_ms)
            assert warm.ok, warm.message
            replayed.add(imports)
            print(f"replay of {imports!r}: {warm.check_ms} ms")
        checked = _check(
            scratch_notebook, imports, outcome["block"], outcome["theorem"]
        )
        record_check_stages(outcome["theorem"], checked.stage_ms)
        # The warm-up's import replay was recorded, so this check reuses it:
        # a refused record would replay the closure (minutes) on every check.
        assert "replay_imports" not in checked.stage_ms, checked.stage_ms
        if checked.ok:
            # A checked block was replayed over the recorded imports.
            assert "replay" in checked.stage_ms, checked.stage_ms
        return checked

    return check


def _decided(name: str, text: str) -> dict:
    """Decide a goal as the provider does, from its text."""
    return decide(parse(name, "hedging", text), threading.Event())


def _status(outcome: dict) -> str:
    """Write a provider outcome as an example's expected status."""
    if outcome["status"] == "unknown":
        return f"UNKNOWN:{outcome['reason']}"
    return outcome["status"].upper()


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_emitted_block_checks(
    check_block, check_budget_ms: int, example: Example
) -> None:
    outcome = _decided(example.name, render_goal(example.goal))
    assert _status(outcome) == example.expected, outcome["stages"]
    if example.expected.startswith("UNKNOWN"):
        # An undecided goal proposes nothing for Lean to check.
        assert "block" not in outcome
        return
    # Glitnir accepts the outcome's shape before it checks the block.
    assert checked_decision(outcome)["block"] == outcome["block"]
    result = check_block(outcome)
    print(f"{example.name}: {result.check_ms} ms, axioms {sorted(result.axioms)}")
    assert result.ok, result.message
    assert set(result.axioms) <= STANDARD_AXIOMS
    assert result.check_ms < check_budget_ms


#: The 60-step zigzag, ``+2 %, −2 %, …``, as the refutation's prelude lists it.
_ZIGZAG_60 = ", ".join("1/50" if k % 2 == 0 else "-1/50" for k in range(60))
#: Goals whose blocks reach what no example does, with the status the decider
#: gives each and a piece of text its block must hold to show it did.
VARIANTS = {
    # The ±1 % hedge loses ≈ 8.7031 on the ±2 % zigzag of 60 steps: the kernel
    # evaluates its wealth along all of them, at the shortfall cap.
    "shortfall_cap": (
        SHORTFALL.replace("steps 20", "steps 60").replace("<= 4", "<= 8"),
        "REFUTED",
        f"def forsetiReturns : List ℚ := [{_ZIGZAG_60}]",
    ),
    # The price over 250 steps (≈ 12.5705) is a sum of 251 terms the kernel
    # evaluates, at the covers cap, once at most a premium and once above one.
    "covers_cap": (
        COVERS.replace("steps 20", "steps 250").replace("711/200", "13"),
        "PROVEN",
        "priceQ (callQ 100) (1/50) (1/50) 250 100 ≤ 13 := by decide +kernel",
    ),
    "covers_cap_refuted": (
        COVERS.replace("steps 20", "steps 250").replace("711/200", "12"),
        "REFUTED",
        "(12 : ℚ) < priceQ (callQ 100) (1/50) (1/50) 250 100 := by decide +kernel",
    ),
    # The straddle's price is twice the call's at the money (≈ 7.1098): the
    # catalogue's straddle example, citing the straddle's own names.
    "straddle_covered": (
        COVERS.replace("call", "straddle").replace("711/200", "711/100"),
        "PROVEN",
        "(straddle_convexOn 100)",
    ),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_a_template_variant_checks(
    check_block, check_budget_ms: int, name: str
) -> None:
    """Each variant is decided by the real decider and checked like an example."""
    text, status, needle = VARIANTS[name]
    outcome = _decided(name, text)
    assert _status(outcome) == status, outcome["stages"]
    assert needle in outcome["block"]
    assert checked_decision(outcome)["block"] == outcome["block"]
    result = check_block(outcome)
    print(f"{name}: {result.check_ms} ms, axioms {sorted(result.axioms)}")
    assert result.ok, result.message
    assert set(result.axioms) <= STANDARD_AXIOMS
    assert result.check_ms < check_budget_ms


def test_a_lowered_premium_is_rejected(check_block) -> None:
    """A premium lowered by hand below the price fails in the kernel.

    The block writes the premium ``711/200`` in ``forsetiClaim``, in its
    theorem and in the step ``priceQ … ≤ 711/200 := by decide +kernel``.
    Lowering it to ``7/2`` everywhere leaves a block that parses and states
    the lowered claim, so only the kernel's evaluation of the price can refuse
    it, at that step's line of the block. Lean then reports that ``decide``
    failed, the price being past what the elaborator reduces to explain why. A
    parse error would read ``unexpected …`` instead, and could pass a weaker
    check.
    """
    [example] = [e for e in EXAMPLES if e.name == "covered_from_price"]
    outcome = _decided(example.name, render_goal(example.goal))
    assert outcome["block"].count("711/200") >= 3
    lowered = outcome["block"].replace("711/200", "7/2")
    step = next(
        number
        for number, line in enumerate(lowered.split("\n"), 1)
        if "≤ 7/2 := by decide +kernel" in line
    )
    result = check_block({**outcome, "block": lowered})
    print(f"lowered premium: {result.message}")
    assert not result.ok
    assert result.message.startswith(f"Block line {step}: "), result.message
    assert "Tactic `decide` failed" in result.message
    assert "unexpected" not in result.message
