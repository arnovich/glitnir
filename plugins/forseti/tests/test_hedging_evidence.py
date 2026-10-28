"""Tests for the ``hedge`` evidence directive of the hedging lane.

``hedge`` reads a payoff's hedge as an exact plan and has Lean evaluate the
payoff's binomial price at the plan's band, printing it exactly with the
parameters it was evaluated at. The provider checks that premium against
`numbers.price`, and the sampler runs the band's hedge from it on seeded random
paths in float64; a ``glitnir.evidence/v1`` card shows the profit and loss at
expiry by its quantiles. Evidence is never a proof: nothing here touches a
decision.
"""

import ast
import dataclasses
import json
import math
import random
import re
from fractions import Fraction
from pathlib import Path

import pytest
from glitnir.providers.outcomes import MAX_PAIRS, MAX_TEXT, checked_evidence

from glitnir_forseti import evidence_provider as forseti
from glitnir_forseti.hedging import numbers, sampler, service
from glitnir_forseti.hedging.examples import PRICE
from glitnir_forseti.hedging.payoffs import PAYOFFS
from glitnir_forseti.hedging.sampler import HedgePlan, returns, sample
from glitnir_forseti.notebook import evidence
from glitnir_forseti.notebook.evidence import (
    HEDGE_LAWS,
    MAX_HEDGE_STEPS,
    MAX_PATHS,
    MAX_WORK,
    NOT_PLAIN,
    EvidenceError,
    emission_command,
    hedge_plan,
    read_directive,
)

CALL = PAYOFFS["call"].lean_name
REQUIRED = "(strike := 100) (spot := 100) (down := 1/50) (up := 1/50) (steps := 20)"
#: What Lean prints for the worked example's ``hedge`` emission
#: (`tests/test_hedging_lean.py` runs it live): the parameters it evaluated
#: the price at, and the call's binomial price at ±2 % over 20 steps.
EMITTED = {
    "payoff": "call",
    "strike": ["100", "1"],
    "spot": ["100", "1"],
    "up": ["1", "50"],
    "down": ["1", "50"],
    "steps": 20,
    "premium": [str(PRICE.numerator), str(PRICE.denominator)],
}
WIDE, NARROW = Fraction(1, 50), Fraction(1, 100)
#: The ±1 % hedge's own price, from which it is run on ±2 % paths.
NARROW_PRICE = numbers.price(
    PAYOFFS["call"], Fraction(100), Fraction(100), NARROW, NARROW, 20
)


def _hedge(options: str, target: str = CALL) -> evidence.Simulation:
    return read_directive("hedge", target, options)


def plan(**changes) -> HedgePlan:
    """The worked example's default plan, with ``changes`` (numbers as text)."""
    base = hedge_plan(
        "call",
        {"strike": "100", "spot": "100", "down": "1/50", "up": "1/50", "steps": "20"},
    )
    exact = {
        key: Fraction(value) if isinstance(value, str) else value
        for key, value in changes.items()
        if key not in ("law", "payoff")
    }
    exact.update({key: changes[key] for key in ("law", "payoff") if key in changes})
    return dataclasses.replace(base, **exact)


def _provenance(card: dict) -> dict[str, str]:
    return dict(card["provenance"])


def _card(options: str = REQUIRED, emitted: dict | None = None) -> dict:
    request = forseti.parse("hedge", CALL, options)
    return forseti.compute(request, EMITTED if emitted is None else emitted)


# ----- the directive ----------------------------------------------------------------


def test_hedge_directive_parses_with_defaults() -> None:
    simulation = _hedge(REQUIRED)
    assert (simulation.error, simulation.verb, simulation.model) == ("", "hedge", CALL)
    assert simulation.hedge == HedgePlan(
        payoff="call",
        strike=Fraction(100),
        spot=Fraction(100),
        up=WIDE,
        down=WIDE,
        against_up=None,
        against_down=None,
        steps=20,
        law="uniform",
        sigma=None,
        paths=4000,
        seed=7,
    )
    assert simulation.hedge.path_band == (WIDE, WIDE)


def test_a_hedge_directive_is_read_from_a_notebook() -> None:
    """Glitnir's core finds the line and hands its parts to `read_directive`."""
    from glitnir.notebook.directives import evidence_directives

    text = f"def u := 1\n-- @evidence hedge {CALL} {REQUIRED} (paths := 10)\n"
    cell = {"index": 0, "kind": "source", "start": 1, "end": 3, "text": text}
    [found] = evidence_directives([cell], text)
    simulation = read_directive(found["verb"], found["target"], found["options"])
    assert simulation.verb == "hedge" and simulation.error == ""
    assert simulation.hedge is not None and simulation.hedge.paths == 10


def test_every_hedge_option_is_read_exactly() -> None:
    simulation = _hedge(
        "(strike := 95.5) (spot := 100) (down := 0.01) (up := 1/100) (steps := 12)"
        " (against_down := 1/50) (against_up := 0.03) (law := gaussian)"
        " (sigma := 1/100) (paths := 12) (seed := 0)",
        target=PAYOFFS["put"].lean_name,
    )
    assert simulation.error == ""
    assert simulation.hedge == HedgePlan(
        "put",
        Fraction(191, 2),
        Fraction(100),
        NARROW,
        NARROW,
        Fraction(3, 100),
        WIDE,
        12,
        "gaussian",
        NARROW,
        12,
        0,
    )
    plan = simulation.hedge
    assert plan is not None and plan.path_band == (WIDE, Fraction(3, 100))
    assert all(
        type(value) is Fraction
        for value in (plan.strike, plan.spot, plan.up, plan.down, plan.sigma)
    )


def test_the_laws_are_uniform_two_point_and_gaussian() -> None:
    assert HEDGE_LAWS == ("uniform", "twopoint", "gaussian")
    for law in ("uniform", "twopoint"):
        simulation = _hedge(f"{REQUIRED} (law := {law})")
        assert simulation.hedge is not None and simulation.hedge.law == law


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (
            f"{REQUIRED} (law := cauchy)",
            "unknown law 'cauchy'; use uniform, twopoint or gaussian",
        ),
        # A float written with an exponent is not an exact number here.
        (REQUIRED.replace("1/50) (up", "2e-2) (up"), NOT_PLAIN),
        (REQUIRED.replace("(steps := 20)", "(steps := 20.5)"), "as counts"),
        (f"{REQUIRED} (paths := 1/2)", "as counts"),
        (f"{REQUIRED} (law := gaussian) (sigma := x)", NOT_PLAIN),
        (REQUIRED.replace("(strike := 100) ", ""), "hedge needs a (strike := …)"),
        (REQUIRED.replace("(spot := 100) ", ""), "hedge needs a (spot := …)"),
        (REQUIRED.replace("(down := 1/50) ", ""), "hedge needs a (down := …)"),
        (REQUIRED.replace("(up := 1/50) ", ""), "hedge needs a (up := …)"),
        (REQUIRED.replace(" (steps := 20)", ""), "hedge needs a (steps := …)"),
        (f"{REQUIRED} (h := 1/100)", "unknown option 'h'"),
        (f"{REQUIRED} (band := 1/50)", "unknown option 'band'"),
        (
            f"{REQUIRED} (sigma := 1/100)",
            "sigma is read only with (law := gaussian)",
        ),
        (
            f"{REQUIRED} (law := gaussian)",
            "hedge with (law := gaussian) needs a (sigma := …)",
        ),
        (f"{REQUIRED} (law := gaussian) (sigma := 0)", "sigma must be positive"),
        (
            f"{REQUIRED} (against_down := 1/50)",
            "give both against_down and against_up, or neither",
        ),
        (
            f"{REQUIRED} (against_up := 1/50)",
            "give both against_down and against_up, or neither",
        ),
        (
            REQUIRED.replace("(down := 1/50)", "(down := 1)"),
            "the band's down move must be below 1",
        ),
        (
            REQUIRED.replace("(down := 1/50)", "(down := 0)"),
            "the band's down move must be positive",
        ),
        (
            REQUIRED.replace("(up := 1/50)", "(up := 0)"),
            "the band's up move must be positive",
        ),
        (
            f"{REQUIRED} (against_down := 3/2) (against_up := 1/50)",
            "the against band's down move must be below 1",
        ),
        (
            f"{REQUIRED} (against_down := 1/50) (against_up := -1/50)",
            "the against band's up move must be positive",
        ),
        (REQUIRED.replace("(spot := 100)", "(spot := 0)"), "the spot must be positive"),
        (
            REQUIRED.replace("(strike := 100)", "(strike := -1)"),
            "the strike must not be negative",
        ),
        (REQUIRED.replace("(steps := 20)", "(steps := 0)"), "steps must be 1–250"),
        (REQUIRED.replace("(steps := 20)", "(steps := 251)"), "steps must be 1–250"),
        (f"{REQUIRED} (paths := 0)", f"paths must be 1–{MAX_PATHS}"),
        (f"{REQUIRED} (paths := 100001)", f"paths must be 1–{MAX_PATHS}"),
        (f"{REQUIRED} (seed := -1)", "the seed must not be negative"),
        (
            # 4000 paths of 71 steps evaluate 4000 · 71 · 72 payoffs and draw
            # 4000 · 71 returns, 20,732,000 in all.
            REQUIRED.replace("(steps := 20)", "(steps := 71)"),
            "this run would take 20,732,000 payoff evaluations and draws, more"
            f" than the {MAX_WORK:,} allowed; use fewer paths or steps",
        ),
    ],
)
def test_each_refusal_says_why(options: str, error: str) -> None:
    simulation = _hedge(options)
    assert error in simulation.error
    assert (simulation.verb, simulation.model, simulation.hedge) == (
        "hedge",
        CALL,
        None,
    )


def test_the_work_counts_payoffs_and_draws_and_is_inclusive() -> None:
    """Each path evaluates ``N(N + 1)`` payoffs and draws ``N`` returns.

    A Gaussian return may take up to 100 draws, so it counts 100.
    """
    within = _hedge(REQUIRED.replace("(steps := 20)", "(steps := 69)"))
    assert within.error == "" and 4000 * 69 * (70 + 1) <= MAX_WORK
    over = _hedge(REQUIRED.replace("(steps := 20)", "(steps := 70)"))
    assert "take 20,160,000 payoff evaluations and draws" in over.error
    gaussian = f"{REQUIRED} (law := gaussian) (sigma := 1/100)"
    assert _hedge(gaussian).error == ""
    long_gaussian = gaussian.replace("(steps := 20)", "(steps := 40)")
    assert "take 22,560,000 payoff evaluations and draws" in _hedge(long_gaussian).error
    assert MAX_HEDGE_STEPS == service.COVERS_CAP == 250


def test_a_hedge_names_a_registered_payoff() -> None:
    for target in ("Gimle.Forseti.Hedging.callQ", "call", "Mine.call"):
        simulation = _hedge(REQUIRED, target=target)
        assert f"{target!r} is not a registered payoff" in simulation.error
        assert all(payoff.lean_name in simulation.error for payoff in PAYOFFS.values())
    assert "is not a Lean name" in _hedge(REQUIRED, target=f"{CALL}) (x").error


def test_a_malformed_hedge_option_gets_the_hedge_hint() -> None:
    simulation = _hedge(f"{REQUIRED} law uniform")
    assert simulation.error == (
        "options are written (strike := 100) (spot := 100) (down := 1/50)"
        " (up := 1/50) (steps := 20)"
    )


def test_an_unknown_verb_lists_all_four() -> None:
    simulation = read_directive("fly", CALL, REQUIRED)
    assert "use simulate, iterate, sample or hedge" in simulation.error


def test_parse_carries_the_plan_as_exact_json() -> None:
    options = (
        f"{REQUIRED} (against_down := 0.02) (against_up := 1/50)"
        " (law := gaussian) (sigma := 1/100) (paths := 100)"
    )
    request = forseti.parse("hedge", CALL, options)
    assert request.options == {
        "payoff": "call",
        "strike": "100",
        "spot": "100",
        "up": "1/50",
        "down": "1/50",
        "against_up": "1/50",
        "against_down": "1/50",
        "steps": 20,
        "law": "gaussian",
        "sigma": "1/100",
        "paths": 100,
        "seed": 7,
    }
    assert json.loads(json.dumps(request.options)) == request.options
    rebuilt = forseti._simulation(request)
    assert (rebuilt.verb, rebuilt.model) == ("hedge", CALL)
    assert rebuilt.hedge == _hedge(options).hedge
    plain = forseti.parse("hedge", CALL, REQUIRED).options
    assert (plain["against_up"], plain["against_down"], plain["sigma"]) == (
        None,
        None,
        None,
    )


def test_parse_refuses_a_hedge_that_cannot_run() -> None:
    with pytest.raises(ValueError, match=r"^Unknown law 'cauchy'; use uniform"):
        forseti.parse("hedge", CALL, f"{REQUIRED} (law := cauchy)")


# ----- the emission -----------------------------------------------------------------


def test_the_emission_evaluates_the_librarys_price() -> None:
    command = emission_command("hedge", CALL, plan())
    assert command.startswith("#eval")
    for line in (
        "  let strike : ℚ := 100",
        "  let spot : ℚ := 100",
        "  let up : ℚ := 1/50",
        "  let down : ℚ := 1/50",
        "  let steps : ℕ := 20",
        "  let premium : ℚ :=",
        "    Gimle.Forseti.Hedging.priceQ"
        " (Gimle.Forseti.Hedging.callQ strike) up down steps spot",
    ):
        assert line in command.split("\n"), line
    assert 'IO.println ("forsetiEvidence:" ++' in command
    for key in ("payoff", "strike", "spot", "up", "down", "steps", "premium"):
        assert f'("{key}",' in command
    assert '("payoff", Lean.toJson "call")' in command
    assert "toString q.num, toString q.den" in command


def test_the_emission_writes_numbers_not_option_text() -> None:
    """Only the payoff's Lean names and exact numbers reach Lean."""
    written = _hedge(
        "(strike := 100.0) (spot := 0100) (down := 0.020) (up := 2/100) (steps := 20)"
        " (law := twopoint) (paths := 9) (seed := 3)",
        target=PAYOFFS["straddle"].lean_name,
    )
    assert written.hedge is not None
    command = emission_command("hedge", written.model, written.hedge)
    assert "Gimle.Forseti.Hedging.straddleQ strike" in command
    assert "let up : ℚ := 1/50" in command and "let strike : ℚ := 100\n" in command
    for text in ("100.0", "0100", "0.020", "2/100", "twopoint", "paths", "seed"):
        assert text not in command, text
    # The plan's law, paths and seed are the sampler's: the emission is the
    # same for every run of one hedge, so Lean's print can be reused.
    other = dataclasses.replace(written.hedge, law="uniform", paths=4000, seed=7)
    assert emission_command("hedge", written.model, other) == command


def test_the_emission_needs_its_plan_and_the_payoffs_name() -> None:
    with pytest.raises(EvidenceError, match="a hedge emission needs its plan"):
        emission_command("hedge", CALL)
    with pytest.raises(EvidenceError, match="is not a registered payoff"):
        emission_command("hedge", "Gimle.Forseti.Hedging.callQ", plan())
    with pytest.raises(EvidenceError, match="names the put, not the call"):
        emission_command("hedge", PAYOFFS["put"].lean_name, plan())


def test_the_provider_command_rebuilds_the_plan() -> None:
    request = forseti.parse("hedge", CALL, REQUIRED)
    assert forseti.command(request) == emission_command("hedge", CALL, plan())


# ----- the payload ------------------------------------------------------------------


def test_the_printed_premium_is_checked_and_read_exactly() -> None:
    premium = sampler.checked_premium(EMITTED, plan())
    assert premium == PRICE and type(premium) is Fraction


def test_a_premium_that_is_not_the_binomial_price_is_refused() -> None:
    wrong = {**EMITTED, "premium": ["7", "2"]}
    with pytest.raises(ValueError) as refused:
        sampler.checked_premium(wrong, plan())
    message = str(refused.value)
    assert "7/2" in message and "3.5549" in message, message
    assert message.startswith("Lean printed the premium 7/2"), message


def test_a_premium_off_beyond_four_places_names_the_difference() -> None:
    off = PRICE + Fraction(1, 10**30)
    wrong = {**EMITTED, "premium": [str(off.numerator), str(off.denominator)]}
    with pytest.raises(EvidenceError, match=r"they differ by 1/10{30} \(1\.0e-30\)$"):
        sampler.checked_premium(wrong, plan())


def test_compute_refuses_a_premium_mismatch() -> None:
    request = forseti.parse("hedge", CALL, f"{REQUIRED} (paths := 10)")
    with pytest.raises(ValueError, match="is not the binomial price"):
        forseti.compute(request, {**EMITTED, "premium": ["7", "2"]})


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"payoff": "put"}, "Lean printed the hedge of the put, not the call"),
        ({"strike": ["99", "1"]}, "Lean printed the strike as 99, not 100"),
        ({"spot": ["101", "1"]}, "Lean printed the spot as 101, not 100"),
        ({"up": ["1", "100"]}, "Lean printed the up move as 1/100, not 1/50"),
        ({"down": ["1", "25"]}, "Lean printed the down move as 1/25, not 1/50"),
        ({"steps": 21}, "Lean printed the steps as 21, not 20"),
        ({"steps": True}, "the steps are a count"),
        ({"steps": "20"}, "the steps are a count"),
        ({"premium": ["2", "4"]}, "must be reduced"),
        ({"premium": [7, 2]}, "a value is a canonical decimal pair"),
        ({"premium": ["1" * 4001, "1"]}, "more than 4000 digits"),
        ({"premium": ["1", "1" * 4001]}, "more than 4000 digits"),
    ],
)
def test_a_print_that_does_not_match_the_plan_is_refused(changes, error) -> None:
    with pytest.raises(EvidenceError, match=re.escape(error)):
        sampler.checked_premium({**EMITTED, **changes}, plan())


@pytest.mark.parametrize("key", sorted(EMITTED))
def test_a_print_missing_a_key_is_refused(key: str) -> None:
    emitted = {k: v for k, v in EMITTED.items() if k != key}
    with pytest.raises(EvidenceError, match=f"the printed hedge has no '{key}'"):
        sampler.checked_premium(emitted, plan())


def test_compute_refuses_what_lean_did_not_print_as_a_hedge() -> None:
    request = forseti.parse("hedge", CALL, REQUIRED)
    with pytest.raises(EvidenceError, match="a hedge that is not an object") as raised:
        forseti.compute(request, [1, 2])
    # The simulate wording, "a malformed field", does not describe a hedge.
    assert "field" not in str(raised.value)
    with pytest.raises(EvidenceError, match="unknown key 'field'"):
        forseti.compute(request, {**EMITTED, "field": []})


# ----- the sampler ------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"law": "uniform"},
        {"law": "twopoint"},
        # A Gaussian well inside the band is rarely redrawn ...
        {"law": "gaussian", "sigma": "1/200"},
        # ... and one far wider than it nearly always reaches the cap of 100
        # draws and takes the nearer end.
        {"law": "gaussian", "sigma": "100"},
        {"law": "uniform", "against_up": "3/100", "against_down": "1/25"},
        {
            "law": "gaussian",
            "sigma": "1/50",
            "against_up": "1/25",
            "against_down": "1/25",
        },
        {"law": "twopoint", "up": "1/10", "down": "1/3"},
    ],
)
def test_every_drawn_return_lies_in_the_band(changes) -> None:
    """The theorems speak of paths in the band and no other."""
    run = plan(**changes)
    down, up = run.path_band
    low, high = -float(down), float(up)
    rng = random.Random(3)
    drawn = [r for _ in range(500) for r in returns(run, rng)]
    assert len(drawn) == 500 * run.steps
    assert all(low <= r <= high for r in drawn)
    if run.law == "twopoint":
        assert set(drawn) == {low, high}
        assert 0.45 < drawn.count(high) / len(drawn) < 0.55
    if run.law == "gaussian" and run.sigma == 100:
        # A draw lands in the band with probability ≈ 0.00016, so ≈ 98 % of
        # returns are an end of the band, the nearer one.
        assert sum(r in (low, high) for r in drawn) / len(drawn) > 0.95
        assert 0.45 < drawn.count(high) / len(drawn) < 0.55


def test_the_gaussian_is_centred_and_truncated_to_the_band() -> None:
    run = plan(law="gaussian", sigma="1/100")
    rng = random.Random(1)
    drawn = [r for _ in range(2000) for r in returns(run, rng)]
    mean = sum(drawn) / len(drawn)
    spread = math.sqrt(sum((r - mean) ** 2 for r in drawn) / len(drawn))
    # The normal of σ = 1/100 truncated to ±2σ has a standard deviation of
    # about 0.88σ.
    assert abs(mean) < 3e-4 and 0.0085 < spread < 0.0091
    assert max(drawn) < 0.02 and min(drawn) > -0.02


def test_the_uniform_law_fills_the_band() -> None:
    rng = random.Random(1)
    drawn = [r for _ in range(2000) for r in returns(plan(), rng)]
    assert abs(sum(drawn) / len(drawn)) < 3e-4
    assert max(drawn) > 0.0199 and min(drawn) < -0.0199


def test_the_bands_own_hedge_replicates_on_its_extreme_paths() -> None:
    """From the price the hedge ends at the payoff on every path of band ends."""
    result = sample(plan(law="twopoint", paths=500), PRICE)
    assert len(result.pnl) == 500 and result.finite is True
    assert all(abs(value) <= 1e-9 for value in result.pnl)
    assert result.below_zero == 0


def test_the_bands_own_hedge_never_ends_below_the_payoff() -> None:
    result = sample(plan(), PRICE)
    assert len(result.pnl) == 4000 and result.pnl == sorted(result.pnl)
    assert result.pnl[0] >= -1e-9 and result.below_zero == 0
    # Moves inside the band leave the hedger ahead: the price of robustness.
    assert 1 < result.pnl[2000] < 2


def test_a_hedge_run_on_a_wider_band_can_end_below_the_payoff() -> None:
    """The ±1 % hedge from its own price, run on ±2 % paths (seed 7)."""
    run = plan(up="1/100", down="1/100", against_up="1/50", against_down="1/50")
    result = sample(run, NARROW_PRICE)
    assert result.pnl[0] < 0 and result.below_zero > 0
    assert result.below_zero == sum(value < -1e-7 for value in result.pnl)
    # Sampling does not find the zigzag's loss of ≈ 4.1129.
    assert result.pnl[0] > -4.1


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"payoff": "put", "law": "twopoint"},
        {"payoff": "straddle", "strike": "103"},
        {"up": "1/100", "down": "1/100", "against_up": "1/50", "against_down": "1/50"},
        {"up": "1/20", "down": "1/30", "steps": 7},
    ],
)
def test_a_path_runs_as_the_exact_hedge_does(changes) -> None:
    """One path's float64 outcome is the exact wealth less payoff on its returns.

    The run's first path draws its returns from a generator seeded with the
    plan's seed; `numbers.wealth` runs the same hedge on them exactly.
    """
    run = dataclasses.replace(plan(**changes), paths=1)
    payoff = PAYOFFS[run.payoff]
    price = numbers.price(payoff, run.strike, run.spot, run.up, run.down, run.steps)
    path = [Fraction(r) for r in returns(run, random.Random(run.seed))]
    final, spot = numbers.wealth(
        payoff, run.strike, run.spot, run.up, run.down, run.steps, path
    )
    exact = final - payoff.value(run.strike, spot)
    [sampled] = sample(run, price).pnl
    assert sampled == pytest.approx(float(exact), abs=1e-9)


def test_the_run_is_seeded() -> None:
    a, b = sample(plan(paths=50), PRICE), sample(plan(paths=50), PRICE)
    assert a == b
    assert sample(plan(paths=50, seed=8), PRICE).pnl != a.pnl
    rng_a, rng_b = random.Random(5), random.Random(5)
    assert returns(plan(), rng_a) == returns(plan(), rng_b)


def test_the_premium_is_where_the_hedge_starts() -> None:
    """A premium one higher ends every path one higher."""
    low = sample(plan(paths=20), PRICE)
    high = sample(plan(paths=20), PRICE + 1)
    for a, b in zip(low.pnl, high.pnl, strict=True):
        assert b - a == pytest.approx(1, abs=1e-9)


def test_a_path_past_float64_is_refused_before_the_run(monkeypatch) -> None:
    run = plan(up="1000", steps=250, paths=1)
    monkeypatch.setattr(sampler, "returns", lambda *_: pytest.fail("it ran"))
    with pytest.raises(EvidenceError, match="past what float64 holds"):
        sample(run, PRICE)
    tiny = plan(down="999/1000", steps=250, paths=1)
    with pytest.raises(EvidenceError, match="past what float64 holds"):
        sample(tiny, PRICE)
    rounded = plan(down=f"{10**40 - 1}/{10**40}", steps=1, paths=1)
    with pytest.raises(EvidenceError, match="rounds to 1 in float64"):
        sample(rounded, PRICE)


def test_the_float_payoffs_are_the_registrys() -> None:
    assert set(sampler.FLOAT_PAYOFFS) == set(PAYOFFS)
    for key, payoff in PAYOFFS.items():
        twin = sampler.FLOAT_PAYOFFS[key]
        for spot in (0.0, 87.25, 100.0, 112.5):
            exact = payoff.value(Fraction(100), Fraction(spot))
            assert twin(100.0, spot) == float(exact), (key, spot)


def test_the_quantiles_interpolate_between_the_sorted_values() -> None:
    values = [float(v) for v in range(41)]
    assert sampler.quantiles(values) == values
    assert sampler.quantiles([1.0, 3.0]) == [1.0 + i / 20 for i in range(41)]
    assert sampler.quantiles([2.0]) == [2.0] * 41
    assert sampler.levels() == [i / 40 for i in range(41)]


# ----- the card ---------------------------------------------------------------------


def test_the_card_shows_the_quantiles_of_the_profit_and_loss() -> None:
    card = _card(f"{REQUIRED} (paths := 400)")
    assert checked_evidence(card) == card and "plane" not in card
    series = card["series"]
    assert series["names"] == ["profit and loss at expiry"]
    assert series["times"] == [i / 40 for i in range(41)]
    assert len(series["values"]) == 41 and all(
        len(row) == 1 for row in series["values"]
    )
    result = sample(plan(paths=400), PRICE)
    assert [row[0] for row in series["values"]] == sampler.quantiles(result.pnl)
    assert card["finite"] is True


def test_the_card_names_its_provenance() -> None:
    card = _card(f"{REQUIRED} (paths := 400)")
    assert [label for label, _ in card["provenance"]] == [
        "verb",
        "payoff",
        "strike",
        "spot",
        "band",
        "steps",
        "law",
        "paths",
        "seed",
        "premium",
        "minimum",
        "median",
        "maximum",
        "paths below zero",
        "arithmetic",
        "provider",
        "created",
    ]
    provenance = _provenance(card)
    assert {k: provenance[k] for k in list(provenance)[:10]} == {
        "verb": "hedge",
        "payoff": "call",
        "strike": "100",
        "spot": "100",
        "band": "down 1/50 up 1/50",
        "steps": "20",
        "law": "uniform",
        "paths": "400",
        "seed": "7",
        "premium": "≈ 3.5549",
    }
    values = [row[0] for row in card["series"]["values"]]
    assert float(provenance["minimum"]) == pytest.approx(values[0], rel=1e-5)
    assert float(provenance["median"]) == pytest.approx(values[20], rel=1e-5)
    assert float(provenance["maximum"]) == pytest.approx(values[-1], rel=1e-5)
    assert provenance["paths below zero"] == "0 of 400, by more than 1e-07"
    assert provenance["arithmetic"] == "float64"
    assert provenance["provider"].startswith("glitnir-forseti ")
    assert all(len(value) <= MAX_TEXT for value in provenance.values())
    assert len(card["provenance"]) <= MAX_PAIRS


def test_the_card_names_the_against_band_and_the_gaussian_spread() -> None:
    narrow = REQUIRED.replace("1/50", "1/100")
    emitted = {
        **EMITTED,
        "up": ["1", "100"],
        "down": ["1", "100"],
        "premium": [str(NARROW_PRICE.numerator), str(NARROW_PRICE.denominator)],
    }
    card = _card(
        f"{narrow} (against_down := 1/50) (against_up := 1/50)"
        " (law := gaussian) (sigma := 1/100) (paths := 200)",
        emitted,
    )
    assert checked_evidence(card) == card
    labels = [label for label, _ in card["provenance"]]
    assert labels[4:9] == ["band", "against", "steps", "law", "sigma"]
    provenance = _provenance(card)
    assert provenance["band"] == "down 1/100 up 1/100"
    assert provenance["against"] == "down 1/50 up 1/50"
    assert provenance["sigma"] == "1/100"
    assert provenance["premium"] == "≈ 1.7702"


def test_the_card_carries_no_proved_number() -> None:
    card = _card(f"{REQUIRED} (paths := 50)")
    for label, value in _provenance(card).items():
        text = f"{label} {value}".lower()
        for word in ("<=", "≤", "bound", "proved", "proven", "theorem", "floor"):
            assert word not in text, (label, value)


def test_a_run_that_leaves_float64_is_refused_on_the_card() -> None:
    huge = REQUIRED.replace("(up := 1/50)", "(up := 1000)").replace(
        "(steps := 20)", "(steps := 250)"
    )
    price = numbers.price(
        PAYOFFS["call"], Fraction(100), Fraction(100), Fraction(1000), WIDE, 250
    )
    printed = {
        **EMITTED,
        "up": ["1000", "1"],
        "steps": 250,
        "premium": [str(price.numerator), str(price.denominator)],
    }
    with pytest.raises(EvidenceError, match="past what float64 holds"):
        _card(f"{huge} (paths := 1)", printed)


def test_no_decision_reads_the_sampler() -> None:
    """Evidence is never proof: nothing in the hedging decider imports the sampler."""
    package = Path(sampler.__file__).parent
    lane = [
        package.parent / "decision_provider.py",
        *(path for path in sorted(package.glob("*.py")) if path.name != "sampler.py"),
        *sorted((package.parent / "claims").rglob("*.py")),
    ]
    assert package / "service.py" in lane and package / "render.py" in lane
    for path in lane:
        imported = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not {
            name
            for name in imported
            if name.endswith("sampler")
            or name.endswith("evidence_provider")
            or name.endswith("evidence")
        }, path
