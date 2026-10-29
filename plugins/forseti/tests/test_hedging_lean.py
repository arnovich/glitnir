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
straddle, whose names no example cites. A check is the shared
`conftest.check_block` fixture, held to the budget
`block_checking.CHECK_BUDGET_MS` (five minutes unless
``FORSETI_CHECK_BUDGET_MS`` names another number of milliseconds), as the
stepped lane's live test is.

Each scratch notebook is a new module of the checkout's ``Gimle.Forseti.Tests``
library, written by the ``scratch_notebook`` fixture, which creates it
exclusively and removes only what it created.

The ``hedge`` evidence verb's emission is run here too, through the runner
Glitnir runs evidence with, in a notebook that imports the hedging library as
a plain import (a notebook is not a Lean ``module``): Lean evaluates the
library's own price and prints it, and the provider checks it and turns it into
the card Glitnir would save.
"""

import hashlib
import json
import threading

import pytest
from block_checking import STANDARD_AXIOMS
from glitnir.providers.outcomes import checked_decision, checked_evidence

from glitnir_forseti import evidence_provider
from glitnir_forseti.claims.text import render_goal
from glitnir_forseti.decision_provider import decide, parse
from glitnir_forseti.hedging.examples import EXAMPLES, PRICE, Example
from glitnir_forseti.hedging.payoffs import PAYOFFS

#: A bad budget fails every test here at once, not only those that read it.
pytestmark = pytest.mark.usefixtures("check_budget_ms")
#: The worked example's goals, as the variants below change them.
COVERS = (
    "hedging call strike 100 spot 100 band down 1/50 up 1/50 steps 20"
    " premium 711/200 covers"
)
SHORTFALL = (
    "hedging call strike 100 spot 100 band down 1/100 up 1/100 steps 20"
    " against down 1/50 up 1/50 shortfall <= 4"
)
#: The worked example's call as a ``hedge`` directive names it, and its options.
CALL = PAYOFFS["call"].lean_name
HEDGE_OPTIONS = (
    "(strike := 100) (spot := 100) (down := 1/50) (up := 1/50) (steps := 20)"
)
HEDGE_DIRECTIVE = f"-- @evidence hedge {CALL} {HEDGE_OPTIONS}"
HEDGE_NOTEBOOK = f"""import Gimle.Forseti.Hedging

/-! # The at-the-money call, hedged on random paths -/

{HEDGE_DIRECTIVE}
"""


@pytest.fixture
def check_block(check_block):
    """Check a block as `conftest.check_block` does, once it needs only the lane.

    Every hedging block imports the hedging library and nothing else.
    """

    def check(outcome: dict):
        assert outcome["stages"][-1] == [
            "imports",
            "needed",
            "import Gimle.Forseti.Hedging",
        ], outcome["stages"]
        return check_block(outcome)

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


def test_live_hedge_emission_prints_the_exact_premium(scratch_notebook) -> None:
    """Lean prints the call's binomial price at ±2 % over 20 steps, exactly.

    The directive goes through the provider's own ``parse``, ``command`` and
    ``compute``, and its Lean through the runner Glitnir runs evidence with.
    The notebook imports the hedging library, a Lean ``module``, with a plain
    ``import``, and ``#eval`` runs the library's ``priceQ`` from its scope. The
    printed JSON is read here first; the card is then checked as Glitnir checks
    it, and carries the sampled profit and loss, never a proved number.
    """
    from glitnir.lean.provider_lean import run_provider_lean

    path = scratch_notebook("GlitnirHedgeLive", HEDGE_NOTEBOOK)
    [provider] = [
        p for p in evidence_provider.evidence_providers() if "hedge" in p.verbs
    ]
    request = provider.parse("hedge", CALL, HEDGE_OPTIONS)
    result = run_provider_lean(
        path,
        expected_sha256=hashlib.sha256(HEDGE_NOTEBOOK.encode()).hexdigest(),
        line=HEDGE_NOTEBOOK.split("\n").index(HEDGE_DIRECTIVE) + 1,
        provider_name=provider.name,
        imports=provider.imports,
        preamble=provider.preamble,
        command=provider.command(request),
        marker=provider.marker,
        needs="evidence",
        cancel=threading.Event(),
    )
    assert "payload" in result, result
    emitted = json.loads(result["payload"])
    print(f"emitted: {json.dumps(emitted)}")
    assert emitted == {
        "payoff": "call",
        "strike": ["100", "1"],
        "spot": ["100", "1"],
        "up": ["1", "50"],
        "down": ["1", "50"],
        "steps": 20,
        "premium": [
            "177744620736998422379605070486628497811",
            "50000000000000000000000000000000000000",
        ],
    }
    assert emitted["premium"] == [str(PRICE.numerator), str(PRICE.denominator)]
    card = provider.compute(request, emitted)
    assert checked_evidence(card) == card and card["finite"] is True
    provenance = dict(card["provenance"])
    print(f"provenance: {provenance}")
    assert provenance[evidence_provider.HEDGE_PREMIUM] == "≈ 3.5549"
    assert provenance["paths"] == "4000"
    assert provenance["paths below zero"].startswith("0 of 4000")
    assert float(provenance["minimum"]) >= -1e-9
    assert not any(
        "<=" in value or "≤" in value or "bound" in value or "bound" in label
        for label, value in provenance.items()
    )
