"""The hedging decision lane: exact prices, extreme paths and the blocks it proposes.

Every decision here runs without Lean, as the notebook's decision provider runs
it (``replay=False``): a decided goal is only a proposed block, which the
project's Lean checks. The block shapes are pinned line by line; the live check
of the blocks is a separate, opt-in test.
"""

import ast
import json
import re
import threading
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import pytest

from glitnir_forseti import figures
from glitnir_forseti.claims.decide import Decision, decide, goal_kind, read_goal
from glitnir_forseti.claims.text import parse_goal, render_goal
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.hedging import examples, numbers, service
from glitnir_forseti.hedging.examples import EXAMPLES, Example
from glitnir_forseti.hedging.goal import HedgingGoal
from glitnir_forseti.hedging.payoffs import PAYOFFS
from glitnir_forseti.lean.closure import closure_identity, replay_pin
from glitnir_forseti.notebook.decisions import block
from glitnir_forseti.notebook.evidence import read_directive

#: The worked example's two goals: the call at the money over twenty steps of
#: ±2 %, covered from 3.555; and the hedge built for ±1 % run on paths of ±2 %.
COVERS = (
    "hedging call strike 100 spot 100 band down 1/50 up 1/50 steps 20"
    " premium 3555/1000 covers"
)
SHORTFALL = (
    "hedging call strike 100 spot 100 band down 1/100 up 1/100 steps 20"
    " against down 1/50 up 1/50 shortfall <= 4"
)
#: The same hedge run on its own band, which it covers.
OWN_BAND = SHORTFALL.replace("band down 1/100 up 1/100", "band down 1/50 up 1/50")
#: The binomial price of the call at the ±2 % band with twenty steps to go.
PRICE_20 = Fraction(
    177744620736998422379605070486628497811,
    50000000000000000000000000000000000000,
)
#: The ±1 % hedge's wealth less payoff on the ±2 % zigzag of twenty returns.
ZIGZAG_SHORTFALL = Fraction(
    -215634705196340256271204096412694900382147731,
    52428800000000000000000000000000000000000000,
)
WIDE = "Band.ofRat (1/50) (1/50) (by norm_num) (by norm_num) (by norm_num)"
NARROW = "Band.ofRat (1/100) (1/100) (by norm_num) (by norm_num) (by norm_num)"
_HEDGING = "Gimle.Forseti.Hedging"
PRICE_CAST_WIDE = (
    "have hp := price_cast (fun S => call_cast 100 S) (1/50) (1/50)"
    " (by norm_num) (by norm_num) (by norm_num) 20 100"
)
#: The zigzag's returns as the refutation's prelude lists them.
ZIGZAG = ", ".join("1/50" if k % 2 == 0 else "-1/50" for k in range(20))
#: The zigzag path's spot, as the shortfall refutation writes it.
PATH = "spotQ 100 (fun k => forsetiReturns.getD k 0)"


@dataclass(frozen=True)
class Decided:
    """A decision as the notebook shows it.

    Attributes:
        status: ``PROVEN``, ``REFUTED`` or ``UNKNOWN:<reason>``.
        reason: Every stage's detail, which is where a decider says why.
        block: The proposed notebook block, for a decided goal.
        decision: The front door's own record.
    """

    status: str
    reason: str
    block: str | None
    decision: Decision


def _decide(goal: str | HedgingGoal, name: str = "hedged") -> Decided:
    parsed = parse_goal(goal) if isinstance(goal, str) else goal
    decision = decide(parsed, replay=False, project=None, cancel=None)
    status = decision.status
    if status == "UNKNOWN":
        status = f"UNKNOWN:{decision.reason}"
    reason = " | ".join(stage["detail"] for stage in decision.stages)
    source = None
    if decision.lean is not None:
        source = block(name, decision).source
    return Decided(status, reason, source, decision)


def _script(decided: Decided) -> tuple[str, ...]:
    assert decided.decision.lean is not None
    return decided.decision.lean.script


def _covers(premium: Fraction) -> HedgingGoal:
    return replace(parse_goal(COVERS), premium=premium)


# ----- covers -------------------------------------------------------------------------


def test_the_worked_example_premium_is_covered() -> None:
    d = _decide(COVERS, "held")
    assert d.status == "PROVEN"
    assert {
        f"{_HEDGING}.covers_hedge",
        f"{_HEDGING}.Covers.mono",
        f"{_HEDGING}.price_cast",
    } <= set(d.decision.dependencies)
    # The whole block, as the notebook inserts it.
    assert d.block == (
        "namespace Decided_held\n\n"
        "open Gimle.Forseti.Hedging\n\n"
        "def forsetiClaim : Prop :=\n"
        f"  Covers ({WIDE}) (call 100) 20\n"
        f"    (hedge ({WIDE}) (call 100) 20)\n"
        "    (711/200) 100\n\n"
        "/-- Sold for a premium of 3.555 (711/200) and hedged at the band's"
        " binomial hedge ratio, rebalanced at each of 20 steps with zero interest"
        " and no costs, the seller's account ends at least at the payoff of the"
        " call struck at 100 on every path from a spot of 100 whose simple"
        " returns stay within −2 % and +2 % (−1/50 and +1/50) a step. The"
        " hedging decider proved it. The binomial price at the band is"
        f" {PRICE_20.numerator}/{PRICE_20.denominator} (≈ 3.5549); any premium"
        " of at least the price covers the payoff, 3.5549 (the price rounded up"
        " to four places) among them. Goal (hedging): hedging call strike 100"
        " spot 100 band down 1/50 up 1/50 steps 20 premium 711/200 covers -/\n"
        "theorem held :\n"
        f"    Covers ({WIDE}) (call 100) 20\n"
        f"      (hedge ({WIDE}) (call 100) 20)\n"
        "      (711/200) 100 := by\n"
        "  have root : forsetiClaim := by\n"
        "    unfold forsetiClaim\n"
        "    have hq : priceQ (callQ 100) (1/50) (1/50) 20 100 ≤ 711/200"
        " := by decide +kernel\n"
        "    have hr := Rat.cast_le (K := ℝ) |>.mpr hq\n"
        f"    {PRICE_CAST_WIDE}\n"
        "    push_cast at hp hr\n"
        "    rw [← hp] at hr\n"
        f"    exact Covers.mono hr (covers_hedge ({WIDE}) (call_convexOn 100) 20"
        " (by norm_num))\n"
        "  refine (fun (bridge : _ ↔ forsetiClaim) => bridge.mpr root) ?_\n"
        "  constructor\n"
        "  · intro claim\n"
        "    exact claim\n"
        "  · intro lane\n"
        "    exact lane\n\n"
        "end Decided_held"
    )
    assert block("held", d.decision).imports == ("Gimle.Forseti.Hedging",)
    assert d.decision.stages[-1]["stage"] == "price"
    assert d.decision.stages[-1]["detail"] == (
        "the binomial price ≈ 3.5549 is at most the premium 711/200"
    )


def test_seven_halves_is_refuted_for_every_spot_feedback_strategy() -> None:
    d = _decide(COVERS.replace("3555/1000", "7/2"), "low")
    assert d.status == "REFUTED"
    assert f"{_HEDGING}.not_covers_of_lt" in d.decision.dependencies
    assert _script(d) == (
        "unfold forsetiClaim",
        "have hq : (7/2 : ℚ) < priceQ (callQ 100) (1/50) (1/50) 20 100"
        " := by decide +kernel",
        "have hr := Rat.cast_lt (K := ℝ) |>.mpr hq",
        PRICE_CAST_WIDE,
        "push_cast at hp hr",
        "rw [← hp] at hr",
        f"exact not_covers_of_lt ({WIDE}) (call 100) 20 (by norm_num) hr",
    )
    # The refutation proves a positive statement: no strategy covers.
    assert d.decision.lean is not None
    assert d.decision.lean.statement == ": forsetiClaim"
    assert d.block is not None
    assert (
        "def forsetiClaim : Prop :=\n"
        "  ∀ δ : ℕ → ℝ → ℝ,\n"
        f"    ¬ Covers ({WIDE}) (call 100) 20 δ (7/2) 100\n"
    ) in d.block
    assert (
        "theorem low_refuted : ¬ (∃ δ : ℕ → ℝ → ℝ,\n"
        f"      Covers ({WIDE}) (call 100) 20 δ (7/2) 100) := by\n"
        "  have refuted : forsetiClaim := by\n"
    ) in d.block
    # The claim stated is what is refuted: that some spot-feedback strategy,
    # defined where it is named, covers from 7/2.
    assert (
        "/-- Some spot-feedback strategy, holding over each step a number of"
        " units set by the step and the spot, keeps the seller's account at least"
        " at the payoff of the call struck at 100 on every path from a spot of"
        " 100 whose simple returns stay within −2 % and +2 % (−1/50 and +1/50) a"
        " step, from a premium of 3.5 (7/2), rebalanced at each of 20 steps with"
        " zero interest and no costs. The hedging decider refuted it. The premium"
        " is below the binomial price at the band,"
        f" {PRICE_20.numerator}/{PRICE_20.denominator} (≈ 3.5549), the least"
        " premium from which any spot-feedback strategy covers the payoff."
    ) in d.block
    assert "(not_exists.mpr refuted) (bridge.mp claim)" in d.block
    # From its price the band's own hedge ends at the payoff on every extreme
    # path, so from 7/2 it ends the difference short on each.
    assert d.decision.stages[-1]["detail"] == (
        "the premium 7/2 is below the binomial price ≈ 3.5549, so no"
        " spot-feedback strategy covers from it; from it the band's own hedge"
        " ends the difference, ≈ 0.0549, below the payoff on every extreme path"
        " of the band, by the replication identity, which the block does not"
        " state; the all-up path of 20 rises of 1/50 is one of them"
    )


def test_exactly_the_price_is_covered_and_a_millionth_less_is_not() -> None:
    """The boundary is the whole point of "from nothing less"."""
    exact = _decide(_covers(PRICE_20))
    assert exact.status == "PROVEN"
    assert exact.block is not None
    assert f"≤ {PRICE_20.numerator}/{PRICE_20.denominator} := by" in exact.block
    below = _decide(_covers(PRICE_20 - Fraction(1, 10**6)))
    assert below.status == "REFUTED"
    assert below.block is not None and "not_covers_of_lt" in below.block


@pytest.fixture
def no_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the extreme-path search raise, as a request out of time would."""

    def interrupted(*args: object, **kwargs: object) -> None:
        raise numbers.Interrupted

    monkeypatch.setattr(numbers, "worst_extreme_path", interrupted)


@pytest.mark.usefixtures("no_search")
@pytest.mark.parametrize("steps", [20, 250])
def test_a_covers_refutation_names_its_path_without_a_search(steps: int) -> None:
    """The shortfall from the premium is the price less it, on every extreme path.

    So no search runs, and nothing can interrupt an established refutation into
    a timeout, at any number of steps up to the cap.
    """
    text = COVERS.replace("steps 20", f"steps {steps}").replace("3555/1000", "7/2")
    d = _decide(text)
    assert d.status == "REFUTED"
    assert d.decision.stages[-1]["detail"].endswith(
        " below the payoff on every extreme path of the band, by the replication"
        " identity, which the block does not state; the all-up path of"
        f" {steps} rises of 1/50 is one of them"
    )


def test_a_long_horizon_is_decided_either_way() -> None:
    d = _decide(COVERS.replace("steps 20", "steps 250").replace("3555/1000", "12"))
    assert d.status == "REFUTED"
    assert "the all-up path of 250 rises of 1/50" in d.reason
    held = _decide(COVERS.replace("steps 20", "steps 250").replace("3555/1000", "13"))
    assert held.status == "PROVEN"


def test_every_payoff_cites_its_own_names() -> None:
    put = _decide(COVERS.replace("call", "put"))
    assert put.status == "PROVEN"
    assert "priceQ (putQ 100)" in put.block and "put_convexOn 100" in put.block
    assert "(fun S => put_cast 100 S)" in put.block
    assert f"{_HEDGING}.put_cast" in put.decision.dependencies
    straddle = _decide(SHORTFALL.replace("call", "straddle").replace("<= 4", "<= 0"))
    assert straddle.status == "REFUTED"
    assert "wealthQ (straddleQ 100)" in straddle.block
    assert "straddle_cast 100" in straddle.block


def test_a_fractional_strike_and_spot_are_written_as_numerals() -> None:
    d = _decide(
        COVERS.replace("strike 100", "strike 201/2").replace("spot 100", "spot 99")
    )
    assert d.status in ("PROVEN", "REFUTED")
    assert "(call (201/2)) 20" in d.block and "(callQ (201/2))" in d.block
    assert "(fun S => call_cast (201/2) S)" in d.block


# ----- shortfall ----------------------------------------------------------------------


def test_a_hedge_on_its_own_band_has_no_shortfall() -> None:
    d = _decide(OWN_BAND.replace("<= 4", "<= 0"), "same")
    assert d.status == "PROVEN"
    assert _script(d) == (
        "unfold forsetiClaim",
        "intro S h0 hband",
        f"have hc := covers_hedge ({WIDE}) (call_convexOn 100) 20"
        " (by norm_num : (0 : ℝ) < 100) S h0 hband",
        "linarith",
    )
    assert f"{_HEDGING}.covers_hedge" in d.decision.dependencies
    assert d.block is not None
    assert (
        "theorem same :\n"
        "    ∀ S : ℕ → ℝ, S 0 = 100 →\n"
        f"      (∀ k < 20, ({WIDE}).InBand (S k) (S (k + 1))) →\n"
        f"      0 ≤ wealth (hedge ({WIDE}) (call 100) 20)\n"
        f"        (price ({WIDE}) (call 100) 20 100) S 20 - call 100 (S 20) := by\n"
    ) in d.block
    assert d.decision.stages[-1]["stage"] == "own-band"


def test_a_negative_bound_on_its_own_band_is_refuted_by_a_path() -> None:
    """Ending at least a half above the payoff everywhere is more than it does."""
    d = _decide(OWN_BAND.replace("<= 4", "<= -1/2"))
    assert d.status == "REFUTED"
    assert d.block is not None
    assert "1/2 ≤ wealth" in d.block
    assert "20) = 0 := by decide +kernel" in d.block


def test_the_narrow_hedge_loses_more_than_four_on_the_zigzag() -> None:
    d = _decide(SHORTFALL, "narrow")
    assert d.status == "REFUTED"
    assert d.block is not None
    found = re.search(r"def forsetiReturns : List ℚ := \[(.*)\]", d.block)
    assert found is not None and found.group(1) == ZIGZAG
    assert len(found.group(1).split(", ")) == 20
    shortfall = f"{ZIGZAG_SHORTFALL.numerator}/{ZIGZAG_SHORTFALL.denominator}"
    assert _script(d) == (
        "unfold forsetiClaim",
        "intro h",
        "have hr : ∀ k < 20, -(1/50 : ℚ) ≤ forsetiReturns.getD k 0"
        " ∧ forsetiReturns.getD k 0 ≤ 1/50 := by decide +kernel",
        f"have h0 : (({PATH} 0 : ℚ) : ℝ) = 100 := by norm_num [spotQ]",
        f"have key := h (fun k => (({PATH} k : ℚ) : ℝ)) h0"
        " (spotQ_inBand (S₀ := 100) (N := 20)"
        " (r := fun k => forsetiReturns.getD k 0) (1/50) (1/50)"
        " (by norm_num) (by norm_num) (by norm_num) (by norm_num) hr)",
        "have hw := wealth_hedge_cast (fun S => call_cast 100 S) (1/100) (1/100)"
        " (by norm_num) (by norm_num) (by norm_num) 20 100"
        " (fun k => forsetiReturns.getD k 0) 20",
        f"have hc := call_cast 100 ({PATH} 20)",
        "have hk : wealthQ (callQ 100) (1/100) (1/100) 20 100"
        " (fun k => forsetiReturns.getD k 0) 20"
        f" - callQ 100 ({PATH} 20) = {shortfall} := by decide +kernel",
        "have hs := congrArg (Rat.cast : ℚ → ℝ) hk",
        "push_cast at hw hc hs",
        "linarith",
    )
    assert {
        f"{_HEDGING}.wealth_hedge_cast",
        f"{_HEDGING}.spotQ_inBand",
    } <= set(d.decision.dependencies)
    assert (
        "theorem narrow_refuted : ¬ (∀ S : ℕ → ℝ, S 0 = 100 →\n"
        f"      (∀ k < 20, ({WIDE}).InBand (S k) (S (k + 1))) →\n"
        f"      -4 ≤ wealth (hedge ({NARROW}) (call 100) 20)\n"
        f"        (price ({NARROW}) (call 100) 20 100) S 20 - call 100 (S 20))"
        " := by\n"
        "  have refuted : ¬ forsetiClaim := by\n"
    ) in d.block
    assert d.decision.stages[-1]["detail"] == (
        "on the zigzag (up and down by turns) of 20 steps of +2 % (+1/50) and −2 %"
        " (−1/50), the hedge ends ≈ 4.1129 below the payoff, more than 4"
    )
    # The docstring names the path and its exact shortfall, recomputed.
    assert (
        "The hedging decider refuted it. On the zigzag (up and down by turns) of"
        " 20 steps of +2 % (+1/50) and −2 % (−1/50), the seller's account less"
        f" the payoff ends at −{-ZIGZAG_SHORTFALL.numerator}/"
        f"{ZIGZAG_SHORTFALL.denominator} (≈ −4.1129), below −4."
    ) in d.block


def test_five_is_a_gap_not_a_proof() -> None:
    """No theorem bounds the mis-banded hedge's shortfall over every path.

    The reason says what can be done: which bounds the path refutes, that no
    bound for the wider band is proved, and the directive that samples it.
    """
    d = _decide(SHORTFALL.replace("<= 4", "<= 5"))
    assert d.status == "UNKNOWN:gap" and d.block is None
    directive = (
        "-- @evidence hedge Gimle.Forseti.Hedging.call (strike := 100)"
        " (spot := 100) (down := 1/100) (up := 1/100) (steps := 20)"
        " (against_down := 1/50) (against_up := 1/50)"
    )
    assert d.decision.stages[-1]["detail"] == (
        "the worst extreme path, the zigzag (up and down by turns) of 20 steps of"
        " +2 % (+1/50) and −2 % (−1/50), ends ≈ 4.1129 below the payoff, within"
        " the claimed 5: bounds below ≈ 4.1129 are refuted on this path; no bound"
        f" for a wider band is proved in this version; `{directive}` samples the"
        " band's other paths"
    )
    # The provider shows a stage's detail up to 500 characters.
    assert len(d.decision.stages[-1]["detail"]) <= 500
    # The directive it suggests is one the evidence provider runs.
    target, _, options = directive.removeprefix("-- @evidence hedge ").partition(" ")
    sampled = read_directive("hedge", target, options)
    assert sampled.error == "" and sampled.hedge is not None
    assert sampled.hedge.path_band == (Fraction(1, 50), Fraction(1, 50))


@pytest.mark.parametrize(
    ("path", "words"),
    [
        ((True,) * 3, "the path of 3 rises of +2 % (+1/50)"),
        ((False,), "the path of 1 fall of −1 % (−1/100)"),
        (
            (False, True) * 3,
            "the zigzag (down and up by turns) of 6 steps of −1 % (−1/100) and"
            " +2 % (+1/50)",
        ),
        (
            (True, True, False) * 3,
            "the path repeating UUD over 9 steps (U a rise of +2 % (+1/50), D a"
            " fall of −1 % (−1/100))",
        ),
        (
            (True, False, False),
            "the path UDD (U a rise of +2 % (+1/50), D a fall of −1 % (−1/100))",
        ),
        (
            (True, False, False, True, True) * 6,
            "a path of 30 steps starting UDDUUUDDUUUD… (U a rise of +2 % (+1/50),"
            " D a fall of −1 % (−1/100))",
        ),
    ],
)
def test_a_path_is_written_by_its_pattern(path: tuple[bool, ...], words: str) -> None:
    from glitnir_forseti.hedging.render import path_words

    assert path_words(path, Fraction(1, 50), Fraction(1, 100)) == words


def test_a_block_reads_back_its_claim_and_its_path() -> None:
    """The prelude's ``forsetiClaim`` and listed returns read back exactly."""
    from glitnir_forseti.hedging import render

    assert render.claim_body(render.prelude("∀ δ : ℕ → ℝ → ℝ, True")) == (
        "∀ δ : ℕ → ℝ → ℝ, True"
    )
    assert render.claim_body("") == ""
    d = _decide(SHORTFALL)
    assert d.decision.lean is not None
    zigzag = tuple(Fraction(1, 50) * (-1) ** k for k in range(20))
    assert render.listed_returns(d.decision.lean.prelude) == zigzag
    assert render.listed_returns(render.prelude("True")) is None


def test_an_exhibited_shortfall_too_large_to_write_is_a_resource_limit() -> None:
    """A band of long numbers compounds past what a block can state exactly.

    Over 40 steps the ±(about 1/2) paths leave the hedge for ±(about 3/32)
    short by a rational of thousands of digits, past Python's limit on
    writing an integer: the decider says so instead of failing.
    """
    up = Fraction(3 * 2**250 + 1, 2**255)
    down = Fraction(3 * 2**250 - 1, 2**255)
    goal = HedgingGoal(
        payoff="call",
        strike=Fraction(100),
        spot=Fraction(100),
        up=up,
        down=down,
        steps=40,
        claim="shortfall",
        against_up=Fraction(1, 2) + Fraction(1, 2**255),
        against_down=Fraction(1, 2) - Fraction(1, 2**255),
        bound=Fraction(0),
    )
    d = _decide(goal)
    assert d.status == "UNKNOWN:resource-limit" and d.block is None
    detail = d.decision.stages[-1]["detail"]
    assert d.decision.stages[-1]["outcome"] == "resource-limit"
    assert "would refute the claim" in detail
    assert "too large to state exactly in a block" in detail
    assert f"more than {service.MAX_SHORTFALL_BITS} bits" in detail


# ----- how sentences show numbers -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (Fraction(100), "100"),
        (Fraction(711, 200), "3.555 (711/200)"),
        (Fraction(-1, 2), "−0.5 (−1/2)"),
        (Fraction(1, 3), "1/3 (≈ 0.3333)"),
        (Fraction(-1, 3), "−1/3 (≈ −0.3333)"),
        (PRICE_20, f"{PRICE_20.numerator}/{PRICE_20.denominator} (≈ 3.5549)"),
        # Too long to write in a sentence: its decimal alone.
        (Fraction(1, 3**400), "≈ 1.4e-191"),
    ],
)
def test_a_sentence_writes_a_decimal_first_and_the_exact_value_beside(
    value: Fraction, shown: str
) -> None:
    assert figures.stated(value) == shown


@pytest.mark.parametrize(
    ("value", "shown", "moved"),
    [
        (Fraction(1, 50), "+2 %", "+2 % (+1/50)"),
        (Fraction(-1, 80), "−1.25 %", "−1.25 % (−1/80)"),
        (Fraction(1, 300), "≈ +0.3333 %", "≈ +0.3333 % (+1/300)"),
        # A return of a long numeral is shown by its percentage alone.
        (Fraction(1, 3**100), "≈ +1.9e-46 %", "≈ +1.9e-46 %"),
    ],
)
def test_a_return_is_a_signed_percentage(
    value: Fraction, shown: str, moved: str
) -> None:
    assert figures.percent(value) == shown
    assert figures.move(value) == moved


def test_a_premium_that_covers_is_the_price_rounded_up() -> None:
    assert figures.rounded_up(PRICE_20) == "3.5549"
    assert figures.rounded_up(Fraction(35548, 10000)) == "3.5548"
    assert figures.rounded_up(Fraction(355481, 100000)) == "3.5549"
    assert figures.rounded_up(Fraction(10**9)) is None


# ----- refusals -----------------------------------------------------------------------


def test_a_whole_step_down_is_unsupported() -> None:
    d = _decide(COVERS.replace("down 1/50", "down 1"))
    assert d.status == "UNKNOWN:unsupported"
    assert "down move 1 would take the spot to zero or below" in d.reason
    assert d.block is None and d.decision.lean is None


def test_a_band_written_in_percent_is_told_its_units() -> None:
    """``down 2 up 2`` meant ±2 %: the refusal says how to write that."""
    d = _decide(COVERS.replace("down 1/50 up 1/50", "down 2 up 2"))
    assert d.status == "UNKNOWN:unsupported"
    assert d.reason.endswith(
        "moves are simple returns as fractions of the spot: ±2 % a step is"
        " `down 1/50 up 1/50` or `down 0.02 up 0.02`"
    )


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("up", Fraction(0), "the band's up move must be positive, got 0"),
        ("up", Fraction(-1, 50), "the band's up move must be positive"),
        ("down", Fraction(0), "the band's down move must be positive, got 0"),
        ("down", Fraction(3, 2), "would take the spot to zero or below"),
        ("spot", Fraction(0), "the spot must be positive, got 0"),
        ("steps", 0, "at least one step, got 0"),
    ],
)
def test_what_the_theorems_exclude_is_unsupported_never_refuted(
    field: str, value: object, needle: str
) -> None:
    """A goal past its own checks still never crashes the decider."""
    goal = parse_goal(COVERS)
    object.__setattr__(goal, field, value)
    found = service.supported(goal)
    assert isinstance(found, str) and needle in found
    d = _decide(goal)
    assert d.status == "UNKNOWN:unsupported" and needle in d.reason


def test_an_unknown_payoff_is_unsupported() -> None:
    d = _decide(replace(parse_goal(COVERS), payoff="bond"))
    assert d.status == "UNKNOWN:unsupported"
    assert "no hedging payoff 'bond'; the payoffs are call, put, straddle" in (d.reason)


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("against down 1/50", "against down 1/200", "does not contain"),
        ("up 1/50 shortfall", "up 1/200 shortfall", "does not contain"),
        ("against down 1/50", "against down 1", "against band's down move 1"),
    ],
)
def test_an_against_band_that_is_not_wider_is_unsupported(
    old: str, new: str, needle: str
) -> None:
    d = _decide(SHORTFALL.replace(old, new))
    assert d.status == "UNKNOWN:unsupported" and needle in d.reason


@pytest.fixture
def no_arithmetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every exact computation raise, as a ``stop`` that would refuse it."""

    def refused(*args: object, **kwargs: object) -> None:
        raise AssertionError("arithmetic ran before the cap was checked")

    for name in ("price", "delta", "wealth", "worst_extreme_path"):
        monkeypatch.setattr(numbers, name, refused)


@pytest.mark.usefixtures("no_arithmetic")
@pytest.mark.parametrize(
    ("text", "cap", "steps"),
    [(COVERS, 250, 251), (SHORTFALL, 60, 61)],
    ids=["covers", "shortfall"],
)
def test_steps_past_the_cap_are_a_resource_limit_before_any_arithmetic(
    text: str, cap: int, steps: int
) -> None:
    assert (service.COVERS_CAP, service.SHORTFALL_CAP) == (250, 60)
    d = _decide(text.replace("steps 20", f"steps {steps}"))
    assert d.status == "UNKNOWN:resource-limit"
    assert f"up to {cap} steps, not {steps}" in d.reason
    assert d.block is None


def test_a_shortfall_at_its_cap_is_decided() -> None:
    d = _decide(SHORTFALL.replace("steps 20", "steps 60").replace("<= 4", "<= 8"))
    assert d.status == "REFUTED"
    assert d.block is not None
    found = re.search(r"def forsetiReturns : List ℚ := \[(.*)\]", d.block)
    assert found is not None and len(found.group(1).split(", ")) == 60


def test_an_oversized_number_is_a_resource_limit() -> None:
    d = _decide(COVERS.replace("3555/1000", f"1/{2**300}"))
    assert d.status == "UNKNOWN:resource-limit"


def test_a_cancelled_decision_is_unknown() -> None:
    cancel = threading.Event()
    cancel.set()
    decision = decide(parse_goal(COVERS), cancel=cancel, replay=False)
    assert (decision.status, decision.reason) == ("UNKNOWN", "cancelled")


def test_the_wall_time_stops_the_path_search() -> None:
    text = SHORTFALL.replace("steps 20", "steps 60")
    limits = EntailmentLimits(wall_time_ms=100)
    decision = decide(parse_goal(text), limits, replay=False)
    assert (decision.status, decision.reason) == ("UNKNOWN", "timeout")
    assert decision.stages[-1]["detail"] == (
        "searching for the worst extreme path over 60 steps ran out of time"
        " (100 ms); use fewer steps or a larger wall time"
    )


def test_with_a_replay_nothing_is_claimed() -> None:
    """The lane has no pinned replay: a replayed request never claims a proof."""
    decision = decide(parse_goal(COVERS))
    assert (decision.status, decision.reason) == ("UNKNOWN", "lean-unavailable")
    assert decision.artifact is None and decision.lean is None
    assert "no pinned replay" in decision.stages[-1]["detail"]


# ----- the kind and the registry ------------------------------------------------------


def test_the_kind_is_registered_in_both_encodings() -> None:
    goal = parse_goal(SHORTFALL)
    assert goal_kind(goal) == "hedging"
    assert read_goal(json.dumps(goal.to_json())) == goal
    record = decide(goal, replay=False).as_dict()
    assert record["kind"] == "hedging" and record["goal"] == SHORTFALL
    assert record["lean"]["statement"] == ": ¬ forsetiClaim"


def test_the_lane_keeps_no_artifacts() -> None:
    assert service.regenerate_application(b"{}", None, None) == (
        "the hedging lane keeps no artifacts"
    )


def test_a_goal_of_the_wrong_type_is_an_error() -> None:
    with pytest.raises(TypeError):
        service.decide_hedging(object())  # type: ignore[arg-type]


# ----- the examples -------------------------------------------------------------------

#: The goal-only JSON fixtures, one per example, as the other lanes keep theirs.
FIXTURES = Path(__file__).parents[1] / "benchmarks" / "hedging" / "goals"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_examples_round_trip(example: Example) -> None:
    document = json.loads((FIXTURES / f"{example.name}.json").read_text())
    goal = HedgingGoal.from_json(document)
    assert goal == example.goal and goal.to_json() == document
    text = render_goal(goal)
    assert parse_goal(text) == goal and render_goal(parse_goal(text)) == text
    d = _decide(text, example.name)
    assert d.status == example.expected, d.reason
    # A decided example proposes a block for the project's Lean to check, and
    # an undecided one proposes none.
    assert (d.block is None) == example.expected.startswith("UNKNOWN")


def test_every_fixture_is_an_example() -> None:
    names = [example.name for example in EXAMPLES]
    assert len(set(names)) == len(names)
    assert {path.stem for path in FIXTURES.glob("*.json")} == set(names)


def test_the_examples_are_the_lanes_outcomes() -> None:
    assert {example.name: example.expected for example in EXAMPLES} == {
        "covered_from_price": "PROVEN",
        "covered_exactly": "PROVEN",
        "not_from_seven_halves": "REFUTED",
        "narrow_hedge_loses_four": "REFUTED",
        "narrow_hedge_five_unknown": "UNKNOWN:gap",
        "same_band_no_shortfall": "PROVEN",
        "put_covered": "PROVEN",
        "too_many_steps": "UNKNOWN:resource-limit",
        "band_too_wide": "UNKNOWN:unsupported",
        "against_narrower": "UNKNOWN:unsupported",
    }


def test_the_examples_use_the_worked_example_texts() -> None:
    texts = {example.name: render_goal(example.goal) for example in EXAMPLES}
    assert texts["covered_from_price"] == COVERS.replace("3555/1000", "711/200")
    assert texts["not_from_seven_halves"] == COVERS.replace("3555/1000", "7/2")
    assert texts["narrow_hedge_loses_four"] == SHORTFALL
    assert texts["narrow_hedge_five_unknown"] == SHORTFALL.replace("<= 4", "<= 5")
    assert texts["same_band_no_shortfall"] == OWN_BAND.replace("<= 4", "<= 0")
    assert texts["put_covered"] == texts["covered_from_price"].replace("call", "put")
    assert texts["too_many_steps"] == texts["covered_from_price"].replace(
        "steps 20", "steps 251"
    )
    assert texts["band_too_wide"] == texts["covered_from_price"].replace(
        "down 1/50", "down 1"
    )
    assert texts["against_narrower"] == (
        "hedging call strike 100 spot 100 band down 1/50 up 1/50 steps 20"
        " against down 1/100 up 1/100 shortfall <= 4"
    )


def test_covered_exactly_is_at_the_exact_price() -> None:
    [exactly] = [e for e in EXAMPLES if e.name == "covered_exactly"]
    goal = exactly.goal
    payoff = PAYOFFS[goal.payoff]
    fair = numbers.price(payoff, goal.strike, goal.spot, goal.up, goal.down, 20)
    assert goal.premium == fair == PRICE_20


def test_the_examples_name_the_lanes_import_set() -> None:
    """The one import set a block needs, which the replay pin holds."""
    assert examples.replay_imports() == (("Gimle.Forseti.Hedging",),)
    for example in EXAMPLES:
        d = _decide(example.goal, example.name)
        if d.block is not None:
            assert (block(example.name, d.decision).imports,) == (
                examples.replay_imports()
            )


def test_every_hedging_import_set_is_pinned() -> None:
    """The replay pin holds the hedging lane's import set.

    The live gate verifies every import set the pin holds against the project's
    build, so a set missing from the pin is never checked for the hedging
    module.
    """
    pinned = [tuple(entry["imports"]) for entry in replay_pin()["closures"]]
    for imports in examples.replay_imports():
        assert imports in pinned
        assert len(closure_identity(imports)) == 64


def test_the_examples_propose_every_block_template() -> None:
    """Each template is live-checked only if some example emits it."""
    blocks = " ".join(_decide(e.goal, e.name).block or "" for e in EXAMPLES)
    templates = {
        "covered from the price": "exact Covers.mono hr (covers_hedge",
        "not from less": "exact not_covers_of_lt",
        "own band": "S h0 hband",
        "a path that breaks it": "def forsetiReturns : List ℚ :=",
    }
    assert [name for name, needle in templates.items() if needle not in blocks] == []


def test_no_block_holds_a_float_literal() -> None:
    """The Lean is exact; only the docstring writes decimals, exactly computed."""
    for example in EXAMPLES:
        source = _decide(example.goal, example.name).block
        if example.expected.startswith("UNKNOWN"):
            assert source is None
            continue
        assert source is not None
        lean = re.sub(r"/--.*?-/", "", source, flags=re.S)
        assert lean != source
        assert re.search(r"\d\.\d", lean) is None, example.name


def test_the_decider_uses_no_floating_point() -> None:
    """Decisions and their reasons are exact: no float literal or conversion.

    Only the wall clock is a float, and it decides nothing but a timeout.
    """
    package = Path(service.__file__).parent
    names = ("goal", "payoffs", "numbers", "service", "render", "examples")
    # The rule a reason writes its figures by is shared with the stepped lane.
    paths = [package / f"{name}.py" for name in names] + [Path(figures.__file__)]
    for path in paths:
        name = path.name
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.Constant) and type(node.value) is float
            ), name
            assert not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("float", "round")
            ), name


def test_the_lane_imports_no_private_name() -> None:
    """The lane shares other modules' helpers only through their public names."""
    package = Path(service.__file__).parent
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                private = [a.name for a in node.names if a.name.startswith("_")]
                assert private == [], path.name


def test_the_text_is_unchanged_by_deciding() -> None:
    goal = parse_goal(COVERS)
    decide(goal, replay=False)
    assert render_goal(goal) == COVERS.replace("3555/1000", "711/200")
