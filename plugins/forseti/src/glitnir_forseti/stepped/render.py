"""Render a stepped goal's claim and the proof templates that decide it.

The claim is the goal stated with the library's claim forms over the model's
Lean constants, for every shock law of the class the goal names:

    ∀ law : Measure ℝ, TwoMomentLaw law 64 →
      BreachWithin step compiled.initial law 256 (exposureWithin 1) (1/4)

The prelude imports the entry's modules, opens its namespaces (so the claim and
the proofs cite names short) and defines ``forsetiClaim`` as that proposition.
A script proves ``forsetiClaim`` or ``¬ forsetiClaim``; each is a proof of the
example module ``HedgedSpreadStepped`` with the goal's constants substituted:

* a bound of at least ``1``: ``breachWithin_of_le_one`` or
  ``exceedsAt_of_le_one``, in either law class;
* the horizon bound ``N·Δ²·v/B²``: ``breach_within_year``;
* its refutation under the three-point law with jumps ``±L``:
  ``breach_within_year_tenth_refuted`` (Bernoulli's threshold) or
  ``breach_within_year_fifth_refuted`` (the exact one, the kernel evaluating
  the rational power);
* the bound at each mark: ``exceeds_at_each_mark_year``, with the kernel's
  bound ``c_up`` on the proxy and ``r = B²/(2·c_up)`` in its numeric core;
* the expected energy: ``expected_energy_year``, and its refutation under the
  three-point law of the same variance, whose expectation is exactly the bound
  ``expected_energy_eq`` states for every law of the class.

The decider's arithmetic chooses the template and its constants. Lean is the
authority: a template whose constants are wrong fails to check, and never
proves another claim, because the claim is restated from the goal.
"""

from __future__ import annotations

from fractions import Fraction

from glitnir_forseti.trajectory.render import RootApplication

from .goal import SteppedGoal
from .models import SteppedEntry

_STEPPED = "Gimle.Forseti.Stepped"
#: The lane's proposition, defined by the prelude.
CLAIM = "forsetiClaim"
PROVED = f": {CLAIM}"
REFUTED = f": ¬ {CLAIM}"
_CLASSES = {
    "two-moment": f"{_STEPPED}.TwoMomentLaw",
    "sub-gaussian": f"{_STEPPED}.SubGaussianLaw",
}
_FORMS = {
    "horizon": f"{_STEPPED}.BreachWithin",
    "fixed-date": f"{_STEPPED}.AtEveryMark",
    "expected": f"{_STEPPED}.ExpectedLe",
}
_MEASURE = "MeasureTheory.Measure"
_RUN = f"{_STEPPED}.run"
_MONO = f"{_STEPPED}.BreachWithin.mono"
_AT_MOST_ONE = {
    "horizon": f"{_STEPPED}.breachWithin_of_le_one",
    "fixed-date": f"{_STEPPED}.exceedsAt_of_le_one",
}
_THREE_POINT = f"{_STEPPED}.threePoint_twoMoment"
_EXP = f"{_STEPPED}.exp_ge_floor_cubic"
_EMPTY = "MeasureTheory.measure_empty"


def numeral(value: Fraction) -> str:
    """Write a nonnegative rational as a Lean numeral: ``64``, ``1/4``."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def argument(value: Fraction) -> str:
    """Write a numeral as a function argument: ``64``, ``(1/4)``."""
    text = numeral(value)
    return text if value.denominator == 1 else f"({text})"


def short(entry: SteppedEntry, name: str) -> str:
    """Write a qualified name as a block cites it, under the namespaces it opens."""
    for namespace in sorted(entry.opens, key=len, reverse=True):
        if name.startswith(f"{namespace}."):
            return name[len(namespace) + 1 :]
    return name


def claim(goal: SteppedGoal, entry: SteppedEntry, indent: str) -> str:
    """Write the goal's proposition, its second line indented by ``indent``."""
    law_class = short(entry, _CLASSES[goal.law])
    head = (
        f"∀ law : {short(entry, _MEASURE)} ℝ, {law_class} law "
        f"{argument(goal.variance)} →"
    )
    chain = f"{short(entry, entry.step_name)} {short(entry, entry.initial_name)} law"
    observed = short(entry, entry.observations[goal.observation].lean_name)
    if goal.barrier is None:
        tail = f"{observed} {goal.horizon}"
    else:
        tail = f"{goal.horizon} ({observed} {argument(goal.barrier)})"
    form = short(entry, _FORMS[goal.claim])
    return f"{head}\n{indent}{form} {chain} {tail} {argument(goal.bound)}"


def prelude(goal: SteppedGoal, entry: SteppedEntry) -> str:
    """Render the imports, the opens and ``forsetiClaim``."""
    imports = "".join(f"import {module}\n" for module in entry.imports)
    opens = "".join(f"open {namespace}\n" for namespace in entry.opens)
    # Written out as `CLAIM`, not interpolated, so the provider's scan of the
    # templates' declarations sees that the name is reserved.
    definition = "def forsetiClaim : Prop :=\n"
    return f"{imports}\n{opens}\n{definition}  {claim(goal, entry, '    ')}\n"


def _application(
    goal: SteppedGoal,
    entry: SteppedEntry,
    statement: str,
    lines: tuple[str, ...],
    dependencies: tuple[str, ...],
) -> RootApplication:
    return RootApplication(
        prelude(goal, entry), statement, (f"unfold {CLAIM}", *lines), dependencies
    )


def at_most_one(goal: SteppedGoal, entry: SteppedEntry) -> RootApplication:
    """Prove a probability claim whose bound is at least one, in either class."""
    lemma = _AT_MOST_ONE[goal.claim]
    binders = "law h" if goal.claim == "horizon" else "law h n _"
    return _application(
        goal,
        entry,
        PROVED,
        (
            f"exact fun {binders} => haveI := h.prob; "
            f"{short(entry, lemma)} (by norm_num)",
        ),
        (lemma,),
    )


def horizon_hold(goal: SteppedGoal, entry: SteppedEntry) -> RootApplication:
    """Prove a horizon claim from ``N·Δ²·v/B² ≤ q``, as ``breach_within_year``."""
    theorem = entry.theorems.breach_within
    return _application(
        goal,
        entry,
        PROVED,
        (
            f"exact fun law h => ({short(entry, theorem)} h (by norm_num) "
            f"{goal.horizon}).mono (by norm_num [{short(entry, entry.delta_name)}])",
        ),
        (theorem, _MONO),
    )


def _witness(entry: SteppedEntry, variance: Fraction, reach: int) -> tuple[str, ...]:
    """Name ``hlaw``: the three-point law with jumps ``±reach`` is in the class."""
    eps = f"{argument(variance)} / (2 * {reach} ^ 2)"
    return (
        f"have hlaw := {short(entry, _THREE_POINT)} (ε := {eps}) (L := {reach})"
        " (by norm_num) (by norm_num)",
        f"rw [show 2 * ({eps} : ℝ) * {reach} ^ 2 = {argument(variance)} by norm_num]"
        " at hlaw",
    )


def horizon_refutation(
    goal: SteppedGoal, entry: SteppedEntry, reach: int, exact: bool
) -> RootApplication:
    """Refute a horizon claim under the three-point law with jumps ``±reach``.

    ``exact`` uses the threshold ``1 − (1 − 2ε)ᴺ``, which the kernel evaluates
    (``breach_within_year_fifth_refuted``); otherwise the claim's bound is
    below Bernoulli's ``2εN / (1 + 2εN)``, which ``norm_num`` checks
    (``breach_within_year_tenth_refuted``). The jumps leave the band ``|s| ≤ 1``
    only, as ``step_big_breaches`` states it.
    """
    theorems = entry.theorems
    delta = short(entry, entry.delta_name)
    big = short(entry, theorems.step_big_breaches)
    jumps = (
        f"{short(entry, theorems.step_zero_fixed)} "
        f"({big} {reach} (by norm_num [{delta}])) "
        f"({big} (-{reach}) (by norm_num [{delta}]))"
    )
    witness = _witness(entry, goal.variance, reach)
    lines: tuple[str, ...]
    if exact:
        eps = f"{argument(goal.variance)} / (2 * {reach} ^ 2)"
        refutation = theorems.not_breach_exact
        lines = (
            f"have hq : ({numeral(goal.bound)} : ℚ) < 1 - (1 - 2 * ({eps} : ℚ)) ^ "
            f"{goal.horizon} := by decide +kernel",
            "have hr := Rat.cast_lt (K := ℝ) |>.mpr hq",
            "push_cast at hr",
            *witness,
            f"exact {short(entry, refutation)} (by norm_num) (by norm_num) {jumps}"
            " hr (h _ hlaw)",
        )
    else:
        refutation = theorems.not_breach_bernoulli
        lines = (
            *witness,
            f"exact {short(entry, refutation)} (by norm_num) (by norm_num) {jumps}"
            " (by norm_num) (h _ hlaw)",
        )
    return _application(
        goal,
        entry,
        REFUTED,
        ("intro h", *lines),
        (
            refutation,
            _THREE_POINT,
            theorems.step_zero_fixed,
            theorems.step_big_breaches,
        ),
    )


def fixed_date_hold(
    goal: SteppedGoal, entry: SteppedEntry, c_up: Fraction, r: Fraction
) -> RootApplication:
    """Prove a claim at every mark, as ``exceeds_at_each_mark_year``.

    ``c_up`` bounds the proxy at the last mark, which bounds it at every mark
    (``proxy_mono``), and ``0 ≤ r ≤ B²/(2·c_up)``. The numeric core shows
    ``2 exp(−B²/(2c)) ≤ q`` for every proxy ``0 < c ≤ c_up`` from
    ``exp(r) ≥ E(r)``, the lower bound of ``exp_ge_floor_cubic`` at ``⌊r⌋``.
    """
    assert goal.barrier is not None
    theorems = entry.theorems
    barrier, bound = argument(goal.barrier), numeral(goal.bound)
    level, cap = numeral(r), numeral(c_up)
    proxy, variance = short(entry, theorems.proxy), argument(goal.variance)
    recursion = short(entry, theorems.proxy_rec_eq)
    mono = short(entry, theorems.proxy_mono)
    safe = short(entry, entry.observations[goal.observation].lean_name)
    path = (
        f"{short(entry, _RUN)} {short(entry, entry.step_name)} "
        f"{short(entry, entry.initial_name)} ω 0"
    )
    lines = (
        "intro law h",
        f"have numeric : ∀ c : ℝ, 0 < c → c ≤ {cap} →"
        f" 2 * Real.exp (-{barrier} ^ 2 / (2 * c)) ≤ {bound} := by",
        "  intro c h0 h1",
        f"  have hr : ({level} : ℝ) ≤ {barrier} ^ 2 / (2 * c) := by",
        "    rw [le_div_iff₀ (by positivity)]",
        "    linarith",
        f"  have hexp : Real.exp (-{barrier} ^ 2 / (2 * c)) ≤ Real.exp (-({level}))"
        " := by",
        "    rw [Real.exp_le_exp, neg_div]",
        "    exact neg_le_neg hr",
        f"  have hE := {short(entry, _EXP)} {r.numerator // r.denominator}"
        f" (r := {level}) (by norm_num)",
        "  norm_num at hE",
        f"  have hq : 2 * Real.exp (-({level})) ≤ {bound} := by",
        "    rw [Real.exp_neg, ← div_eq_mul_inv, div_le_iff₀ (Real.exp_pos _)]",
        "    linarith",
        "  linarith",
        "intro n hn",
        "rcases Nat.eq_zero_or_pos n with rfl | hn0",
        f"· have hempty : {{ω : ℕ → ℝ | ¬ {safe} {barrier} ({path})}} = ∅ := by",
        "    ext ω",
        f"    norm_num [{safe}, {short(entry, theorems.initial_eq)}]",
        "  refine ⟨h.prob, by norm_num, ?_⟩",
        f"  rw [hempty, {short(entry, _EMPTY)}]",
        "  exact zero_le",
        f"· refine {short(entry, theorems.exceeds_at_proxy)} h (by norm_num) n ?_",
        f"  have hc : {proxy} {variance} {goal.horizon} ≤ {cap} := by",
        f"    rw [← {recursion}]",
        "    decide +kernel",
        f"  have hle : ({proxy} {variance} n : ℝ) ≤ {cap} := by",
        "    have := Rat.cast_le (K := ℝ) |>.mpr"
        f" (({mono} {variance} (by norm_num) hn).trans hc)",
        "    push_cast at this",
        "    exact this",
        f"  have hpos : (0 : ℝ) < {proxy} {variance} n := by",
        f"    have h1 : (0 : ℚ) < {proxy} {variance} 1 := by",
        f"      rw [← {recursion}]",
        "      decide +kernel",
        f"    exact_mod_cast h1.trans_le ({mono} {variance} (by norm_num) hn0)",
        "  exact numeric _ hpos hle",
    )
    return _application(
        goal,
        entry,
        PROVED,
        lines,
        (
            theorems.exceeds_at_proxy,
            theorems.proxy_rec_eq,
            theorems.proxy_mono,
            _EXP,
            theorems.initial_eq,
        ),
    )


def _expectation(goal: SteppedGoal, entry: SteppedEntry) -> str:
    """``Δ ^ 2 * v * geomSum N``, the exact expected energy over ``ℚ``."""
    return (
        f"{short(entry, entry.delta_name)} ^ 2 * {argument(goal.variance)} * "
        f"{short(entry, entry.theorems.geom_sum)} {goal.horizon}"
    )


def expected_hold(goal: SteppedGoal, entry: SteppedEntry) -> RootApplication:
    """Prove an expectation claim, as ``expected_energy_year``."""
    theorems = entry.theorems
    return _application(
        goal,
        entry,
        PROVED,
        (
            "intro law h",
            f"have hq : {_expectation(goal, entry)} ≤ {numeral(goal.bound)}"
            " := by decide +kernel",
            "have hr := Rat.cast_le (K := ℝ) |>.mpr hq",
            "push_cast at hr",
            "refine ⟨h.prob, by norm_num, ?_⟩",
            f"rw [{short(entry, theorems.expected_energy_eq)} h,"
            f" ← {short(entry, theorems.geom_sum_eq)}]",
            "exact ENNReal.ofReal_le_ofReal hr",
        ),
        (theorems.expected_energy_eq, theorems.geom_sum_eq),
    )


def expected_refutation(
    goal: SteppedGoal, entry: SteppedEntry, reach: int
) -> RootApplication:
    """Refute an expectation claim under the three-point law with jumps ``±reach``.

    The witness is in the two-moment class, so the assumed claim bounds its
    expected energy, which ``expected_energy_eq`` gives exactly; the kernel
    shows the bound is below it. The claim form's ``0 ≤ c`` reads the bound
    back from ``ENNReal``.
    """
    theorems = entry.theorems
    return _application(
        goal,
        entry,
        REFUTED,
        (
            "intro h",
            *_witness(entry, goal.variance, reach),
            "obtain ⟨-, hc, hle⟩ := h _ hlaw",
            f"rw [{short(entry, theorems.expected_energy_eq)} hlaw,"
            f" ← {short(entry, theorems.geom_sum_eq)},"
            " ENNReal.ofReal_le_ofReal_iff hc] at hle",
            f"have hq : ({numeral(goal.bound)} : ℚ) < {_expectation(goal, entry)}"
            " := by decide +kernel",
            "have hr := Rat.cast_lt (K := ℝ) |>.mpr hq",
            "push_cast at hr",
            "linarith",
        ),
        (theorems.expected_energy_eq, theorems.geom_sum_eq, _THREE_POINT),
    )
