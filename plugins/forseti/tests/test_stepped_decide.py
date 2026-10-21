"""The stepped decision lane: exact bounds, refusals and the blocks it proposes.

Every decision here runs without Lean, as the notebook's decision provider
runs it (``replay=False``): a decided goal is only a proposed block, which the
project's Lean checks. Each block shape is compared line by line with the
example module's proof it templates (``HedgedSpreadStepped``); the live check
of the blocks is a separate, opt-in test.
"""

import ast
import dataclasses
import json
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest

from glitnir_forseti.claims.decide import Decision, decide, goal_kind, read_goal
from glitnir_forseti.claims.text import parse_goal, render_goal
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.lean.closure import closure_identity, replay_pin
from glitnir_forseti.notebook.decisions import block
from glitnir_forseti.stepped import models, service
from glitnir_forseti.stepped.examples import EXAMPLES, Example
from glitnir_forseti.stepped.goal import SteppedGoal

HORIZON = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
    " within 256 observe s breach 1 probability <= 1/4"
)
FIXED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks subgaussian 64"
    " at every mark within 256 observe s breach 1 probability <= 1/100"
)
EXPECTED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
    " observe V expected <= 33/256 at 256"
)
#: How the decision provider asks for a Lean-free decision: no project, its own
#: cancel event (never set here), and ``replay=False``.
LEAN_FREE_KEYWORDS = {"project": None, "cancel": None}

#: The three-point law of variance 64 with jumps ±257, as every refutation
#: here writes it.
THREE_POINT_64 = (
    "have hlaw := threePoint_twoMoment (ε := 64 / (2 * 257 ^ 2)) (L := 257)"
    " (by norm_num) (by norm_num)",
    "rw [show 2 * (64 / (2 * 257 ^ 2) : ℝ) * 257 ^ 2 = 64 by norm_num] at hlaw",
)
JUMPS = (
    "step_zero_fixed (step_big_breaches 257 (by norm_num [Δ]))"
    " (step_big_breaches (-257) (by norm_num [Δ]))"
)


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


def _decide(text: str, name: str = "year") -> Decided:
    decision = decide(parse_goal(text), replay=False, **LEAN_FREE_KEYWORDS)
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


# ----- the horizon claim --------------------------------------------------------------


def test_horizon_holds() -> None:
    d = _decide(HORIZON)
    assert d.status == "PROVEN" and "breach_within" in d.block and "norm_num" in d.block
    # The whole block, as the notebook inserts it: the proof of
    # `breach_within_year` with the goal's constants.
    assert d.block == (
        "namespace Decided_year\n\n"
        "open MeasureTheory\n"
        "open Gimle.Forseti.Stepped\n"
        "open Gimle.Forseti.Examples.HedgedSpread\n"
        "open Gimle.Forseti.Examples.HedgedSpreadContract\n"
        "open Gimle.Forseti.Examples.HedgedSpreadStepped\n\n"
        "def forsetiClaim : Prop :=\n"
        "  ∀ law : Measure ℝ, TwoMomentLaw law 64 →\n"
        "    BreachWithin step compiled.initial law 256 (exposureWithin 1) (1/4)\n\n"
        "/-- For independent, identically distributed shocks drawn from any centred"
        " law with variance 64, the probability that the"
        " exposure s breaches 1 within 256 marks is at most 1/4. `step` is the"
        " model's step of size 1/256 with the shock in its driver slot, from"
        " `compiled.initial` = (0, 0); `exposureWithin 1` is the band |s| ≤ 1."
        " The stepped decider proved it. Goal (stepped): " + HORIZON + " -/\n"
        "theorem year :\n"
        "    ∀ law : Measure ℝ, TwoMomentLaw law 64 →\n"
        "      BreachWithin step compiled.initial law 256 (exposureWithin 1) (1/4)"
        " := by\n"
        "  have root : forsetiClaim := by\n"
        "    unfold forsetiClaim\n"
        "    exact fun law h => (breach_within h (by norm_num) 256).mono"
        " (by norm_num [Δ])\n"
        "  refine (fun (bridge : _ ↔ forsetiClaim) => bridge.mpr root) ?_\n"
        "  constructor\n"
        "  · intro claim\n"
        "    exact claim\n"
        "  · intro lane\n"
        "    exact lane\n\n"
        "end Decided_year"
    )
    assert block("year", d.decision).imports == (
        "Gimle.Forseti.Stepped",
        "Gimle.Forseti.Examples.HedgedSpreadStepped",
    )
    assert d.decision.stages[-1]["detail"] == "N·Δ²·v/B² = 1/4 ≤ 1/4"


def test_tight_limit_refuted() -> None:
    d = _decide(HORIZON.replace("<= 1/4", "<= 1/10"))
    assert d.status == "REFUTED" and "threePoint" in d.block and "257" in d.block
    # Below Bernoulli's 0.1988: the cheaper block of `breach_within_year_tenth_refuted`.
    assert _script(d) == (
        "unfold forsetiClaim",
        "intro h",
        *THREE_POINT_64,
        "exact not_breachWithin_threePoint (by norm_num) (by norm_num) "
        + JUMPS
        + " (by norm_num) (h _ hlaw)",
    )
    assert "theorem year_refuted : ¬ (∀ law : Measure ℝ," in d.block
    assert "have refuted : ¬ forsetiClaim := by" in d.block


def test_a_bound_between_the_thresholds_is_refuted_exactly() -> None:
    """``1/5`` is above Bernoulli's 0.1988 but below the exact 0.2198."""
    d = _decide(HORIZON.replace("<= 1/4", "<= 1/5"))
    assert d.status == "REFUTED"
    # The proof of `breach_within_year_fifth_refuted`, the kernel evaluating the power.
    assert _script(d) == (
        "unfold forsetiClaim",
        "intro h",
        "have hq : (1/5 : ℚ) < 1 - (1 - 2 * (64 / (2 * 257 ^ 2) : ℚ)) ^ 256"
        " := by decide +kernel",
        "have hr := Rat.cast_lt (K := ℝ) |>.mpr hq",
        "push_cast at hr",
        *THREE_POINT_64,
        "exact not_breachWithin_threePoint_exact (by norm_num) (by norm_num) "
        + JUMPS
        + " hr (h _ hlaw)",
    )


def test_horizon_gap_is_unknown() -> None:
    d = _decide(HORIZON.replace("<= 1/4", "<= 22/100"))
    assert d.status.startswith("UNKNOWN") and "1/4" in d.reason and "0.1988" in d.reason
    assert d.status == "UNKNOWN:gap" and d.block is None
    # The exact threshold is the one that decides; 22/100 is above it.
    assert "0.2198" in d.reason and "11/50" in d.reason and "16384/82433" in d.reason


def test_subgaussian_horizon_unknown() -> None:
    d = _decide(HORIZON.replace("variance", "subgaussian").replace("1/4", "1/50"))
    assert d.status == "UNKNOWN:unsupported"
    assert "no horizon theorem for the sub-Gaussian class" in d.reason


def test_trivial_and_zero_bounds() -> None:
    trivial = _decide(HORIZON.replace("<= 1/4", "<= 1"))
    assert trivial.status == "PROVEN"
    assert _script(trivial) == (
        "unfold forsetiClaim",
        "exact fun law h => haveI := h.prob; breachWithin_of_le_one (by norm_num)",
    )
    zero = _decide(HORIZON.replace("<= 1/4", "<= 0"))
    assert zero.status == "REFUTED"
    assert "not_breachWithin_threePoint (by norm_num)" in zero.block


def test_a_bound_of_one_holds_in_either_class() -> None:
    """Any bound of at least one holds for every probability law, whatever its class."""
    sub = _decide(HORIZON.replace("variance 64", "subgaussian 5/2").replace("1/4", "1"))
    assert sub.status == "PROVEN"
    assert "SubGaussianLaw law (5/2) →" in sub.block
    marks = _decide(FIXED.replace("subgaussian", "variance").replace("1/100", "3/2"))
    assert marks.status == "PROVEN"
    assert _script(marks) == (
        "unfold forsetiClaim",
        "exact fun law h n _ => haveI := h.prob; exceedsAt_of_le_one (by norm_num)",
    )
    assert "AtEveryMark step compiled.initial law 256 (exposureWithin 1) (3/2)" in (
        marks.block
    )


def test_variance_sixteen_holds_tighter() -> None:
    d = _decide(
        HORIZON.replace("variance 64", "variance 16").replace("<= 1/4", "<= 1/16")
    )
    assert d.status == "PROVEN"
    assert "TwoMomentLaw law 16 →" in d.block


def test_another_barrier_is_a_gap_not_a_refutation() -> None:
    """`step_big_breaches` is stated for the barrier 1, so 2 has no refutation."""
    d = _decide(HORIZON.replace("breach 1", "breach 2").replace("1/4", "1/20"))
    assert d.status == "UNKNOWN:gap"
    assert "1/16" in d.reason and "barrier 1" in d.reason
    held = _decide(HORIZON.replace("breach 1", "breach 3/2").replace("1/4", "1/9"))
    assert held.status == "PROVEN" and "(exposureWithin (3/2)) (1/9)" in held.block


def test_a_variance_without_a_three_point_law_is_a_gap() -> None:
    d = _decide(HORIZON.replace("variance 64", "variance 70000").replace("1/4", "1/2"))
    assert d.status == "UNKNOWN:gap" and "three-point" in d.reason


# ----- the fixed-date claim -----------------------------------------------------------


def test_fixed_date_holds() -> None:
    d = _decide(FIXED)
    assert (
        d.status == "PROVEN"
        and "decide +kernel" in d.block
        and "exp_ge_floor_cubic 5" in d.block
    )
    # The proof of `exceeds_at_each_mark_year`, its numeric core inlined, with
    # the kernel's proxy bound `c_up` and `r = B²/(2·c_up)` substituted.
    assert _script(d) == (
        "unfold forsetiClaim",
        "intro law h",
        "have numeric : ∀ c : ℝ, 0 < c → c ≤ 18251/200000 →"
        " 2 * Real.exp (-1 ^ 2 / (2 * c)) ≤ 1/100 := by",
        "  intro c h0 h1",
        "  have hr : (100000/18251 : ℝ) ≤ 1 ^ 2 / (2 * c) := by",
        "    rw [le_div_iff₀ (by positivity)]",
        "    linarith",
        "  have hexp : Real.exp (-1 ^ 2 / (2 * c)) ≤ Real.exp (-(100000/18251)) := by",
        "    rw [Real.exp_le_exp, neg_div]",
        "    exact neg_le_neg hr",
        "  have hE := exp_ge_floor_cubic 5 (r := 100000/18251) (by norm_num)",
        "  norm_num at hE",
        "  have hq : 2 * Real.exp (-(100000/18251)) ≤ 1/100 := by",
        "    rw [Real.exp_neg, ← div_eq_mul_inv, div_le_iff₀ (Real.exp_pos _)]",
        "    linarith",
        "  linarith",
        "intro n hn",
        "rcases Nat.eq_zero_or_pos n with rfl | hn0",
        "· have hempty : {ω : ℕ → ℝ | ¬ exposureWithin 1"
        " (run step compiled.initial ω 0)} = ∅ := by",
        "    ext ω",
        "    simp only [run_zero]",
        "    norm_num [exposureWithin, initial_eq]",
        "  refine ⟨h.prob, by norm_num, ?_⟩",
        "  rw [hempty, measure_empty]",
        "  exact zero_le",
        "· refine exceeds_at_proxy h (by norm_num) n ?_",
        "  have hc : proxy 64 256 ≤ 18251/200000 := by",
        "    rw [← proxyRec_eq]",
        "    decide +kernel",
        "  have hle : (proxy 64 n : ℝ) ≤ 18251/200000 := by",
        "    have := Rat.cast_le (K := ℝ) |>.mpr"
        " ((proxy_mono 64 (by norm_num) hn).trans hc)",
        "    push_cast at this",
        "    exact this",
        "  have hpos : (0 : ℝ) < proxy 64 n := by",
        "    have h1 : (0 : ℚ) < proxy 64 1 := by",
        "      rw [← proxyRec_eq]",
        "      decide +kernel",
        "    exact_mod_cast h1.trans_le (proxy_mono 64 (by norm_num) hn0)",
        "  exact numeric _ hpos hle",
    )
    assert (
        "∀ law : Measure ℝ, SubGaussianLaw law 64 →\n"
        "      AtEveryMark step compiled.initial law 256 (exposureWithin 1) (1/100)"
    ) in d.block
    assert "at each mark up to 256, taken separately," in d.block


def test_fixed_date_gap_is_unknown() -> None:
    d = _decide(FIXED.replace("1/100", "1/1000"))
    assert d.status == "UNKNOWN:gap"
    assert "0.0084" in d.reason and "1/1000" in d.reason


def test_a_far_barrier_caps_the_exponent() -> None:
    """``r`` grows with the barrier squared; the block uses at most `EXP_CAP`."""
    d = _decide(FIXED.replace("breach 1", "breach 64"))
    assert d.status == "PROVEN"
    assert f"exp_ge_floor_cubic {service.EXP_CAP} (r := {service.EXP_CAP})" in d.block


def test_a_fractional_proxy_is_refused() -> None:
    d = _decide(FIXED.replace("subgaussian 64", "subgaussian 129/2"))
    assert d.status == "UNKNOWN:unsupported" and "whole number" in d.reason


# ----- how reasons show figures -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (Fraction(1, 4), "1/4"),
        (Fraction(3), "3"),
        (Fraction(16384, 82433), "16384/82433 (0.1988)"),
        (Fraction(1, 10**10), "1/10000000000 (1.0e-10)"),
        (Fraction(-1, 256), "-1/256 (-0.0039)"),
        (Fraction(10**50 + 1, 10**50), "≈ 1.0000"),
        (Fraction(1, 7) ** 60, "≈ 2.0e-51"),
        (Fraction(7) ** 60 + Fraction(1, 3), "≈ 5.1e50"),
        # Past what `str` writes for an integer: told long by its bit length.
        (Fraction(1, 3**20000), "≈ 3.8e-9543"),
    ],
)
def test_a_figure_is_exact_when_short_and_a_decimal_otherwise(value, shown) -> None:
    assert service._figure(value) == shown


def test_a_tiny_bound_never_reads_as_a_larger_one() -> None:
    """``2/E(r) ≈ < 0.0001 ≤ 1/10000000000`` read as 0.0001 ≤ 10⁻¹⁰."""
    d = _decide(
        FIXED.replace("breach 1", "breach 64").replace("1/100", "1/10000000000")
    )
    assert d.status == "PROVEN"
    detail = d.decision.stages[-1]["detail"]
    assert "≈ <" not in detail and "< 0.0001" not in detail
    assert detail == (
        "2/E(r) ≈ 1.3e-111 ≤ 1/10000000000 (1.0e-10) at r = 256, from the proxy"
        " bound c_up = 18251/200000 (0.0913) on the proxy at mark 256, which is"
        " ≈ 0.0913"
    )


def test_a_tiny_three_point_mass_reads_as_a_number() -> None:
    d = _decide(
        HORIZON.replace("variance 64", f"variance 1/{10**30}").replace("1/4", "0")
    )
    assert d.status == "REFUTED"
    assert d.decision.stages[-1]["detail"] == (
        "the three-point law with jumps ±257 and ε ="
        " 1/132098000000000000000000000000000000 (7.6e-36) breaches with"
        " probability at least 1 − (1 − 2ε)^256 ≈ 3.9e-33, above 0"
    )


def test_a_long_figure_is_shown_by_its_decimal() -> None:
    """A crafted variance's horizon bound is 146 characters as a fraction."""
    variance = Fraction(10**70 + 1, 8 * 10**70 + 7)
    d = _decide(HORIZON.replace("variance 64", f"variance {variance}"))
    assert d.status == "PROVEN"
    assert d.decision.stages[-1]["detail"] == "N·Δ²·v/B² ≈ 0.0005 ≤ 1/4"


def test_the_gap_reasons_read_in_order() -> None:
    horizon = _decide(HORIZON.replace("<= 1/4", "<= 22/100"))
    assert horizon.decision.stages[-1]["detail"] == (
        "the horizon bound N·Δ²·v/B² = 1/4 is above the claimed 11/50, and not"
        " below the three-point threshold 1 − (1 − 2ε)^256 ≈ 0.2198 at ε ="
        " 32/66049 (0.0005), nor Bernoulli's 2εN/(1 + 2εN) = 16384/82433 (0.1988)"
    )
    fixed = _decide(FIXED.replace("1/100", "1/1000"))
    assert fixed.decision.stages[-1]["detail"] == (
        "the fixed-date bound 2/E(r) ≈ 0.0084 is above the claimed 1/1000 at r ="
        " 100000/18251 (5.4792), from the proxy bound c_up = 18251/200000 (0.0913)"
        " on the proxy at mark 256, which is ≈ 0.0913; no refutation of a claim at"
        " each mark is in this version"
    )
    expected = _decide(EXPECTED)
    assert expected.decision.stages[-1]["detail"] == (
        "Δ²·v·∑_(k<256) ρ^k ≈ 0.1085 ≤ 33/256 (0.1289)"
    )


# ----- the expectation claim ----------------------------------------------------------


def test_expected_holds_and_refutes() -> None:
    assert _decide(EXPECTED).status == "PROVEN"
    assert _decide(EXPECTED.replace("33/256", "1/10")).status == "REFUTED"


def test_expected_blocks() -> None:
    held = _decide(EXPECTED)
    # The proof of `expected_energy_year`.
    assert _script(held) == (
        "unfold forsetiClaim",
        "intro law h",
        "have hq : Δ ^ 2 * 64 * geomSum 256 ≤ 33/256 := by decide +kernel",
        "have hr := Rat.cast_le (K := ℝ) |>.mpr hq",
        "push_cast at hr",
        "refine ⟨h.prob, by norm_num, ?_⟩",
        "rw [expected_energy_eq h, ← geomSum_eq]",
        "exact ENNReal.ofReal_le_ofReal hr",
    )
    assert (
        "∀ law : Measure ℝ, TwoMomentLaw law 64 →\n"
        "      ExpectedLe step compiled.initial law energy 256 (33/256)"
    ) in held.block
    refuted = _decide(EXPECTED.replace("33/256", "1/10"))
    # The three-point witness of variance 64, the exact expectation against the bound.
    assert _script(refuted) == (
        "unfold forsetiClaim",
        "intro h",
        *THREE_POINT_64,
        "obtain ⟨-, hc, hle⟩ := h _ hlaw",
        "rw [expected_energy_eq hlaw, ← geomSum_eq,"
        " ENNReal.ofReal_le_ofReal_iff hc] at hle",
        "have hq : (1/10 : ℚ) < Δ ^ 2 * 64 * geomSum 256 := by decide +kernel",
        "have hr := Rat.cast_lt (K := ℝ) |>.mpr hq",
        "push_cast at hr",
        "linarith",
    )
    assert "the expected energy V at mark 256 is at most 1/10" in refuted.block


def test_a_large_variance_takes_a_wider_witness() -> None:
    """ε = v/(2L²) must stay at most 1/2, so the jump grows with the variance."""
    d = _decide(
        EXPECTED.replace("variance 64", "variance 70000").replace("33/256", "1")
    )
    assert d.status == "REFUTED"
    assert "(L := 265)" in d.block and "70000 / (2 * 265 ^ 2)" in d.block


# ----- refusals -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,needle",
    [
        (HORIZON.replace("step 1/256", "step 1/100"), "1/256"),
        (HORIZON.replace("within 256", "within 5000"), "4096"),
        (HORIZON.replace("observe s", "observe h"), "observe"),
        (EXPECTED.replace("observe V", "observe s"), "expected"),
        (HORIZON.replace("hedged-spread", "nowhere"), "no stepped model 'nowhere'"),
        (HORIZON.replace("step 1/256", "step -1/256"), "positive"),
        (HORIZON.replace("from (0, 0)", "from (1, 0)"), "(0, 0)"),
        (HORIZON.replace("from (0, 0)", "from (0, 0, 0)"), "(0, 0)"),
        (HORIZON.replace("at 0 step", "at 1 step"), "start"),
        (HORIZON.replace("within 256", "within 0"), "at least one"),
        (HORIZON.replace("breach 1", "breach 0"), "barrier"),
        (HORIZON.replace("observe s", "observe V"), "breach"),
        (FIXED.replace("subgaussian", "variance"), "Chebyshev"),
        (EXPECTED.replace("variance", "subgaussian"), "two-moment"),
        (FIXED.replace("subgaussian 64", "subgaussian 0"), "positive proxy"),
    ],
)
def test_refusals(text: str, needle: str) -> None:
    d = _decide(text)
    assert d.status == "UNKNOWN:unsupported" and needle in d.reason
    assert d.block is None and d.decision.lean is None


#: The shapes whose proofs unroll the horizon, with their goals at 256 marks and
#: the text that sets the number of marks, by the cap they are tried against.
UNROLLED = (
    ("fixed-date", FIXED, "within 256", "within {n}", "a fixed-date claim"),
    ("expected", EXPECTED, "at 256", "at {n}", "an expected claim"),
)


@pytest.mark.parametrize(
    "text,old,new,shape", [u[1:] for u in UNROLLED], ids=[u[0] for u in UNROLLED]
)
def test_an_unrolled_claim_is_decided_up_to_its_cap_only(
    text: str, old: str, new: str, shape: str
) -> None:
    """At the cap the claim is decided; one mark past it, it is unsupported."""
    cap = models.HEDGED_SPREAD_STEPPED.unrolled_cap
    assert cap == 1024
    at_cap = _decide(text.replace(old, new.format(n=cap)))
    assert at_cap.status == "PROVEN" and at_cap.block is not None
    past = _decide(text.replace(old, new.format(n=cap + 1)))
    assert past.status == "UNKNOWN:unsupported"
    assert past.block is None and past.decision.lean is None
    assert shape in past.reason
    assert f"up to {cap} steps, not {cap + 1}" in past.reason
    # A breach within a horizon goes on up to the lane's cap, which the reason
    # names, and which `within N` alone does not tell from a fixed-date claim.
    assert "`within N` without `at every mark`" in past.reason
    assert f"goes up to {models.HEDGED_SPREAD_STEPPED.horizon_cap}" in past.reason


def test_a_trivial_fixed_date_claim_stops_at_the_same_cap() -> None:
    """A bound of 1 holds for every law, yet the cap is by claim shape."""
    cap = models.HEDGED_SPREAD_STEPPED.unrolled_cap
    trivial = FIXED.replace("<= 1/100", "<= 1")
    at_cap = _decide(trivial.replace("within 256", f"within {cap}"))
    assert at_cap.status == "PROVEN"
    assert at_cap.block is not None and "exceedsAt_of_le_one" in at_cap.block
    past = _decide(trivial.replace("within 256", f"within {cap + 1}"))
    assert past.status == "UNKNOWN:unsupported" and past.block is None


def test_a_refuted_expected_claim_stops_at_the_same_cap() -> None:
    cap = models.HEDGED_SPREAD_STEPPED.unrolled_cap
    refuted = EXPECTED.replace("33/256", "1/10")
    assert _decide(refuted.replace("at 256", f"at {cap}")).status == "REFUTED"
    past = _decide(refuted.replace("at 256", f"at {cap + 1}"))
    assert past.status == "UNKNOWN:unsupported" and past.block is None


def test_a_horizon_claim_is_decided_up_to_the_horizon_cap() -> None:
    """The unrolled cap does not bind a claim whose proof does not unroll."""
    entry = models.HEDGED_SPREAD_STEPPED
    assert entry.horizon_cap > entry.unrolled_cap
    # N·Δ²·v/B² = 4096 · 4 / 256² = 1/4, exactly at the cap.
    text = HORIZON.replace("within 256", f"within {entry.horizon_cap}")
    held = _decide(text.replace("variance 64", "variance 4"))
    assert held.status == "PROVEN"
    past = _decide(text.replace("within 4096", "within 4097"))
    assert past.status == "UNKNOWN:unsupported" and "4096" in past.reason


def test_a_refusal_comes_before_a_trivial_bound() -> None:
    """A goal off the model's step is refused even when its bound is 1."""
    d = _decide(HORIZON.replace("step 1/256", "step 1/100").replace("1/4", "1"))
    assert d.status == "UNKNOWN:unsupported"


def test_an_oversized_number_is_a_resource_limit() -> None:
    d = _decide(HORIZON.replace("<= 1/4", f"<= 1/{2**300}"))
    assert d.status == "UNKNOWN:resource-limit"


def test_a_cancelled_decision_is_unknown() -> None:
    cancel = threading.Event()
    cancel.set()
    decision = decide(parse_goal(HORIZON), cancel=cancel, replay=False)
    assert (decision.status, decision.reason) == ("UNKNOWN", "cancelled")


#: Goals at the horizon cap, whose exact recursions take seconds: the proxy of
#: the fixed-date claim about 7 s, the expected energy about 3 s. Their own cap
#: is lower, so the registered model is lifted to the horizon cap for these.
AT_THE_CAP = (
    FIXED.replace("within 256", "within 4096"),
    EXPECTED.replace("at 256", "at 4096"),
)


@pytest.fixture
def unrolled_to_the_horizon_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let fixed-date and expected goals range as far as horizon goals."""
    entry = models.HEDGED_SPREAD_STEPPED
    lifted = dataclasses.replace(entry, unrolled_cap=entry.horizon_cap)
    monkeypatch.setitem(models.MODELS, entry.key, lifted)


@pytest.mark.usefixtures("unrolled_to_the_horizon_cap")
@pytest.mark.parametrize("text", AT_THE_CAP, ids=["fixed-date", "expected"])
def test_a_cancel_stops_a_long_recursion(text: str) -> None:
    """A cancel set while the recursion runs ends the request at once."""
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        decision = decide(parse_goal(text), cancel=cancel, replay=False)
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
    assert (decision.status, decision.reason) == ("UNKNOWN", "cancelled")
    assert elapsed < 1.5, elapsed


@pytest.mark.usefixtures("unrolled_to_the_horizon_cap")
@pytest.mark.parametrize("text", AT_THE_CAP, ids=["fixed-date", "expected"])
def test_the_wall_time_stops_a_long_recursion(text: str) -> None:
    started = time.monotonic()
    decision = decide(
        parse_goal(text), EntailmentLimits(wall_time_ms=200), replay=False
    )
    elapsed = time.monotonic() - started
    assert (decision.status, decision.reason) == ("UNKNOWN", "timeout")
    assert elapsed < 1.5, elapsed


def test_with_a_replay_nothing_is_claimed() -> None:
    """The lane has no pinned replay: a replayed request never claims a proof."""
    decision = decide(parse_goal(HORIZON))
    assert (decision.status, decision.reason) == ("UNKNOWN", "lean-unavailable")
    assert decision.artifact is None and decision.lean is None


# ----- the kind and the registry ------------------------------------------------------


def test_the_kind_is_registered_in_both_encodings() -> None:
    goal = parse_goal(EXPECTED)
    assert goal_kind(goal) == "stepped"
    assert read_goal(json.dumps(goal.to_json())) == goal
    record = decide(goal, replay=False).as_dict()
    assert record["kind"] == "stepped" and record["goal"] == EXPECTED
    assert record["lean"]["statement"] == ": forsetiClaim"


def test_the_registry_matches_the_example_module() -> None:
    entry = models.MODELS["hedged-spread"]
    assert entry is models.HEDGED_SPREAD_STEPPED
    delta = Fraction(1, 256)
    assert entry.delta == delta and entry.horizon_cap == 4096
    assert entry.unrolled_cap == 1024
    assert entry.matrix == ((1 - delta, -delta), (delta, 1 - delta))
    assert entry.shock == (Fraction(1), Fraction(0))
    assert entry.initial == (Fraction(0), Fraction(0)) and entry.start == 0
    assert entry.observations["s"] == models.SteppedObservation(
        "Gimle.Forseti.Examples.HedgedSpreadStepped.exposureWithin", "exposure", 0
    )
    # The energy is named once, by its observation, and has no component.
    assert entry.observations["V"] == models.SteppedObservation(
        "Gimle.Forseti.Examples.HedgedSpreadContract.energy", "energy"
    )
    assert entry.compiled_name == "Gimle.Forseti.Examples.HedgedSpread.compiled"
    assert entry.initial_name == f"{entry.compiled_name}.initial"
    assert models.replay_imports() == (
        ("Gimle.Forseti.Stepped", "Gimle.Forseti.Examples.HedgedSpreadStepped"),
    )
    # Every Lean name is qualified, under one of the namespaces a block opens.
    names = [entry.step_name, entry.compiled_name, entry.initial_name]
    names += [entry.delta_name, *(o.lean_name for o in entry.observations.values())]
    names += [
        getattr(entry.theorems, f.name) for f in dataclasses.fields(entry.theorems)
    ]
    assert all(any(n.startswith(f"{o}.") for o in entry.opens) for n in names)


def test_a_decided_goal_cites_qualified_lemmas() -> None:
    decision = _decide(FIXED).decision
    assert decision.dependencies == (
        "Gimle.Forseti.Examples.HedgedSpreadStepped.exceeds_at_proxy",
        "Gimle.Forseti.Examples.HedgedSpreadStepped.proxyRec_eq",
        "Gimle.Forseti.Examples.HedgedSpreadStepped.proxy_mono",
        "Gimle.Forseti.Stepped.exp_ge_floor_cubic",
        "Gimle.Forseti.Stepped.run_zero",
        "Gimle.Forseti.Examples.HedgedSpread.initial_eq",
    )


# ----- the examples -------------------------------------------------------------------

#: The goal-only JSON fixtures, one per example, as the other lanes keep theirs.
FIXTURES = Path(__file__).parents[1] / "benchmarks" / "stepped" / "goals"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_examples_round_trip(example: Example) -> None:
    document = json.loads((FIXTURES / f"{example.name}.json").read_text())
    goal = SteppedGoal.from_json(document)
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


def test_the_examples_include_the_notebook_claims() -> None:
    expected = {example.name: example.expected for example in EXAMPLES}
    assert {
        "breach_within_year": "PROVEN",
        "mark_on_any_date": "PROVEN",
        "expected_gauge": "PROVEN",
        "tight_limit": "REFUTED",
        "breach_within_year_subgaussian": "UNKNOWN:unsupported",
        "trivial_bound": "PROVEN",
        "zero_bound": "REFUTED",
        "fifth_refuted": "REFUTED",
    }.items() <= expected.items()


def test_the_examples_use_the_canonical_goal_texts() -> None:
    texts = {example.name: render_goal(example.goal) for example in EXAMPLES}
    assert texts["breach_within_year"] == HORIZON
    assert texts["mark_on_any_date"] == FIXED
    assert texts["expected_gauge"] == EXPECTED
    assert texts["tight_limit"] == HORIZON.replace("<= 1/4", "<= 1/10")
    assert texts["fifth_refuted"] == HORIZON.replace("<= 1/4", "<= 1/5")
    assert texts["breach_within_year_subgaussian"] == (
        "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks subgaussian 64"
        " within 256 observe s breach 1 probability <= 1/50"
    )


def test_every_stepped_import_set_is_pinned() -> None:
    """The replay pin holds the stepped lane's import set.

    The live gate verifies every import set the pin holds against the project's
    build, so a set missing from the pin is never checked for the stepped
    modules.
    """
    pinned = [tuple(entry["imports"]) for entry in replay_pin()["closures"]]
    for imports in models.replay_imports():
        assert imports in pinned
        assert len(closure_identity(imports)) == 64


def test_the_examples_propose_every_block_template() -> None:
    """Each template is live-checked only if some example emits it."""
    blocks = " ".join(
        _decide(render_goal(e.goal), e.name).block or "" for e in EXAMPLES
    )
    templates = {
        "bound of one, horizon": "breachWithin_of_le_one",
        "bound of one, marks": "exceedsAt_of_le_one",
        "horizon hold": "(breach_within h",
        "horizon refutation, Bernoulli": "not_breachWithin_threePoint (by norm_num)",
        "horizon refutation, exact": "not_breachWithin_threePoint_exact",
        "fixed date": "exceeds_at_proxy",
        "expectation hold": "exact ENNReal.ofReal_le_ofReal hr",
        "expectation refutation": "ENNReal.ofReal_le_ofReal_iff hc",
    }
    assert [name for name, needle in templates.items() if needle not in blocks] == []


def test_the_decider_uses_no_floating_point() -> None:
    """Decisions and their reasons are exact: no float literal or conversion.

    Only the wall clock is a float, and it decides nothing but a timeout.
    """
    package = Path(service.__file__).parent
    for name in ("models.py", "service.py", "render.py", "examples.py"):
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


def test_a_goal_of_the_wrong_type_is_an_error() -> None:
    with pytest.raises(TypeError):
        service.decide_stepped(object())  # type: ignore[arg-type]
    assert isinstance(parse_goal(HORIZON), SteppedGoal)
