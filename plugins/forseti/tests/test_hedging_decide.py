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

from glitnir_forseti.claims.decide import Decision, decide, goal_kind, read_goal
from glitnir_forseti.claims.text import parse_goal, render_goal
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.hedging import numbers, service
from glitnir_forseti.hedging.goal import HedgingGoal
from glitnir_forseti.notebook.decisions import block

#: The design's two goals: the call at the money over twenty steps of ±2 %,
#: covered from 3.555; and the hedge built for ±1 % run on paths of ±2 %.
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


def test_the_design_premium_is_covered() -> None:
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
        "/-- Re-hedged at each of 20 steps at the binomial ratio from a premium of"
        " 711/200, the call struck at 100 on a spot of 100 is covered at expiry on"
        " every path whose step returns stay within −1/50 and +1/50. The hedging"
        " decider proved it. Goal (hedging): hedging call strike 100 spot 100"
        " band down 1/50 up 1/50 steps 20 premium 711/200 covers -/\n"
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
    assert (
        "The hedging decider refuted it: no spot-feedback strategy, holding over"
        " each step a number of units set by the step and the spot, covers it"
        " from a premium of 7/2."
    ) in d.block
    assert "(not_exists.mpr refuted) (bridge.mp claim)" in d.block
    # The band's own hedge from 7/2 falls short on every extreme path; the
    # search names the one its tie rule picks.
    assert d.decision.stages[-1]["detail"] == (
        "the premium 7/2 is below the binomial price ≈ 3.5549, so no"
        " spot-feedback strategy covers from it; from it the band's own hedge"
        " ends ≈ 0.0549 below the payoff on its worst extreme path"
        " UUUUUUUUUUUUUUUUUUUU (U a rise of 1/50, D a fall of 1/50)"
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


def test_a_long_horizon_skips_the_path_search() -> None:
    """Past the shortfall cap the extreme-path search would cost minutes."""
    d = _decide(COVERS.replace("steps 20", "steps 250").replace("3555/1000", "12"))
    assert d.status == "REFUTED"
    assert "the extreme-path search runs up to 60 steps" in d.reason
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
        "on the extreme path UDUDUDUDUDUDUDUDUDUD (U a rise of 1/50, D a fall of"
        " 1/50) the hedge ends ≈ 4.1129 below the payoff, more than 4"
    )


def test_five_is_a_gap_not_a_proof() -> None:
    """No theorem bounds the mis-banded hedge's shortfall over every path."""
    d = _decide(SHORTFALL.replace("<= 4", "<= 5"))
    assert d.status == "UNKNOWN:gap" and d.block is None
    assert "≈ 4.1129" in d.reason and "within the claimed 5" in d.reason
    assert "no theorem" in d.reason


# ----- refusals -----------------------------------------------------------------------


def test_a_whole_step_down_is_unsupported() -> None:
    d = _decide(COVERS.replace("down 1/50", "down 1"))
    assert d.status == "UNKNOWN:unsupported"
    assert "down move 1 would take the spot to zero or below" in d.reason
    assert d.block is None and d.decision.lean is None


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


#: One goal per template.
TEMPLATE_GOALS = (
    COVERS,
    COVERS.replace("3555/1000", "7/2"),
    OWN_BAND.replace("<= 4", "<= 0"),
    SHORTFALL,
)


def test_the_examples_propose_every_block_template() -> None:
    """Each template is live-checked only if some example emits it."""
    blocks = " ".join(_decide(text).block or "" for text in TEMPLATE_GOALS)
    templates = {
        "covered from the price": "exact Covers.mono hr (covers_hedge",
        "not from less": "exact not_covers_of_lt",
        "own band": "S h0 hband",
        "a path that breaks it": "def forsetiReturns : List ℚ :=",
    }
    assert [name for name, needle in templates.items() if needle not in blocks] == []


def test_no_block_holds_a_float_literal() -> None:
    for text in TEMPLATE_GOALS:
        source = _decide(text).block
        assert source is not None
        assert re.search(r"\d\.\d", source) is None, text


def test_the_decider_uses_no_floating_point() -> None:
    """Decisions and their reasons are exact: no float literal or conversion.

    Only the wall clock is a float, and it decides nothing but a timeout.
    """
    package = Path(service.__file__).parent
    for name in ("goal.py", "payoffs.py", "numbers.py", "service.py", "render.py"):
        tree = ast.parse((package / name).read_text())
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.Constant) and type(node.value) is float
            ), name
            assert not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("float", "round")
            ), name


def test_the_text_is_unchanged_by_deciding() -> None:
    goal = parse_goal(COVERS)
    decide(goal, replay=False)
    assert render_goal(goal) == COVERS.replace("3555/1000", "711/200")
