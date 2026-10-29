"""Render a hedging goal's claim and the proof templates that decide it.

A claim is stated with the definitions of `Gimle.Forseti.Hedging` over bands
with rational ends, ``Band.ofRat u d``, whose side conditions ``0 < u``,
``0 < d`` and ``d < 1`` `norm_num` proves:

* ``covers``: the band's own binomial hedge, started from the premium ``P``,
  ends at least at the payoff on every path from ``S₀`` whose ``N`` steps stay
  in the band, ``Covers b g N (hedge b g N) P S₀``;
* ``shortfall``: the hedge built for the band, started from its own price and
  run on any path from ``S₀`` whose steps stay in the ``against`` band, ends at
  most ``X`` below the payoff,

      ∀ S : ℕ → ℝ, S 0 = S₀ → (∀ k < N, a.InBand (S k) (S (k + 1))) →
        -X ≤ wealth (hedge b g N) (price b g N S₀) S N - g (S N)

The prelude imports the library, opens its namespace (so the claim and the
proofs cite names short) and defines ``forsetiClaim`` as the proposition a
template proves. Four templates:

* the price is at most the premium: `Covers.mono` of `covers_hedge`, with
  ``price ≤ P`` evaluated over ``ℚ`` by the kernel and carried to the reals
  by `price_cast`;
* the premium is below the price: ``∀ δ, ¬ Covers b g N δ P S₀`` from
  `not_covers_of_lt` and the same evaluation. Every spot-feedback strategy
  ``δ`` is refuted at once, so this template's ``forsetiClaim`` is that
  positive statement, and it proves ``forsetiClaim`` rather than its negation;
* the hedge run on its own band, ``X ≥ 0``: `covers_hedge` and `linarith`;
* a path of the against band on which the hedge ends more than ``X`` below
  the payoff: its returns as a list ``forsetiReturns``, `spotQ_inBand` for its
  steps, `wealth_hedge_cast` for the wealth along it, and the exact shortfall
  by the kernel.

The decider's arithmetic chooses the template and its constants. Lean is the
authority: a template whose constants are wrong fails to check, and never
proves another claim, because the claim is restated from the goal.

A refuting path is also written in words (`path_words`), for the decider's
reason and the block's docstring, and its returns are read back from the
prelude that lists them (`listed_returns`).
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from glitnir_forseti.figures import move
from glitnir_forseti.trajectory.render import RootApplication

from .goal import HedgingGoal
from .payoffs import Payoff

_HEDGING = "Gimle.Forseti.Hedging"
#: The modules a block imports and the namespaces it opens.
IMPORTS = (_HEDGING,)
OPENS = (_HEDGING,)
#: The lane's proposition, defined by the prelude.
CLAIM = "forsetiClaim"
PROVED = f": {CLAIM}"
REFUTED = f": ¬ {CLAIM}"
#: A spot-feedback strategy: the units held over step ``k`` at the spot ``S``.
STRATEGY = "ℕ → ℝ → ℝ"
_COVERS_MONO = f"{_HEDGING}.Covers.mono"
_COVERS_HEDGE = f"{_HEDGING}.covers_hedge"
_NOT_COVERS = f"{_HEDGING}.not_covers_of_lt"
_PRICE_CAST = f"{_HEDGING}.price_cast"
_WEALTH_CAST = f"{_HEDGING}.wealth_hedge_cast"
_SPOT_IN_BAND = f"{_HEDGING}.spotQ_inBand"
#: The refuting path's returns, read as a function of the step.
_RETURNS_FUN = "fun k => forsetiReturns.getD k 0"
_RETURNS = f"({_RETURNS_FUN})"
#: How the prelude defines ``forsetiClaim`` and the refuting path's returns,
#: and how a ``covers`` refutation's proposition starts: ``∀ δ``.
CLAIM_DEFINITION = "def forsetiClaim : Prop :=\n"
_RETURNS_DEFINITION = "def forsetiReturns : List ℚ := ["
NO_COVER = "∀ δ"
#: The longest repeating unit a path is written by, ``UUD`` or ``UUDD``.
_PERIOD = 4
#: A path with no such pattern is written out up to this many steps, and
#: past it by its length and its first `_SHOWN_MOVES` moves.
_WRITTEN_MOVES = 24
_SHOWN_MOVES = 12


def numeral(value: Fraction) -> str:
    """Write a rational as a Lean numeral: ``100``, ``1/50``, ``-1/50``."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def argument(value: Fraction) -> str:
    """Write a numeral as a function argument: ``100``, ``(1/50)``, ``(-1)``."""
    text = numeral(value)
    return text if value.denominator == 1 and value >= 0 else f"({text})"


def short(name: str) -> str:
    """Write a qualified name as a block cites it, under the namespace it opens."""
    return name.removeprefix(f"{_HEDGING}.")


def band(up: Fraction, down: Fraction) -> str:
    """Write the band ``[S (1 - down), S (1 + up)]`` as ``Band.ofRat up down …``."""
    return (
        f"Band.ofRat {argument(up)} {argument(down)}"
        " (by norm_num) (by norm_num) (by norm_num)"
    )


def _payoff(goal: HedgingGoal, payoff: Payoff) -> str:
    """Write the payoff over the reals at the goal's strike: ``call 100``."""
    return f"{short(payoff.lean_name)} {argument(goal.strike)}"


def _payoff_q(goal: HedgingGoal, payoff: Payoff) -> str:
    """Write the payoff over the rationals at the goal's strike: ``callQ 100``."""
    return f"{short(payoff.lean_q)} {argument(goal.strike)}"


def _premium(goal: HedgingGoal) -> Fraction:
    assert goal.premium is not None
    return goal.premium


def covers_claim(goal: HedgingGoal, payoff: Payoff, indent: str) -> str:
    """``Covers b g N (hedge b g N) P S₀``, its later lines indented by ``indent``."""
    hedged, g, n = band(goal.up, goal.down), _payoff(goal, payoff), goal.steps
    return (
        f"Covers ({hedged}) ({g}) {n}\n"
        f"{indent}(hedge ({hedged}) ({g}) {n})\n"
        f"{indent}{argument(_premium(goal))} {argument(goal.spot)}"
    )


def _covers_by(goal: HedgingGoal, payoff: Payoff, strategy: str) -> str:
    """``Covers b g N δ P S₀``: ``strategy`` covers from the premium."""
    return (
        f"Covers ({band(goal.up, goal.down)}) ({_payoff(goal, payoff)}) {goal.steps}"
        f" {strategy} {argument(_premium(goal))} {argument(goal.spot)}"
    )


def no_cover_claim(goal: HedgingGoal, payoff: Payoff, indent: str) -> str:
    """``∀ δ, ¬ Covers b g N δ P S₀``: no spot-feedback strategy covers from ``P``."""
    return f"∀ δ : {STRATEGY},\n{indent}¬ {_covers_by(goal, payoff, 'δ')}"


def some_cover_claim(goal: HedgingGoal, payoff: Payoff, indent: str) -> str:
    """``∃ δ, Covers b g N δ P S₀``: some spot-feedback strategy covers from ``P``.

    Its negation is what a refutation of a ``covers`` claim proves.
    """
    return f"∃ δ : {STRATEGY},\n{indent}{_covers_by(goal, payoff, 'δ')}"


def shortfall_claim(goal: HedgingGoal, payoff: Payoff, indent: str) -> str:
    """Write the ``shortfall`` claim, its later lines indented by ``indent``."""
    assert goal.against_up is not None and goal.against_down is not None
    assert goal.bound is not None
    against = band(goal.against_up, goal.against_down)
    hedged, g, n = band(goal.up, goal.down), _payoff(goal, payoff), goal.steps
    return (
        f"∀ S : ℕ → ℝ, S 0 = {numeral(goal.spot)} →\n"
        f"{indent}(∀ k < {n}, ({against}).InBand (S k) (S (k + 1))) →\n"
        f"{indent}{numeral(-goal.bound)} ≤ wealth (hedge ({hedged}) ({g}) {n})\n"
        f"{indent}  (price ({hedged}) ({g}) {n} {argument(goal.spot)}) S {n}"
        f" - {g} (S {n})"
    )


def prelude(proposition: str, definitions: tuple[str, ...] = ()) -> str:
    """Render the imports, the opens, any definitions and ``forsetiClaim``."""
    imports = "".join(f"import {module}\n" for module in IMPORTS)
    opens = "".join(f"open {namespace}\n" for namespace in OPENS)
    defined = "".join(f"{definition}\n\n" for definition in definitions)
    # Written out as `CLAIM`, not interpolated, so the provider's scan of the
    # templates' declarations sees that the name is reserved.
    definition = "def forsetiClaim : Prop :=\n"
    return f"{imports}\n{opens}\n{defined}{definition}  {proposition}\n"


def claim_body(prelude: str) -> str:
    """Return the proposition a prelude defines as ``forsetiClaim``, or ``""``."""
    _, found, body = prelude.partition(CLAIM_DEFINITION)
    return body.strip() if found else ""


def listed_returns(prelude: str) -> tuple[Fraction, ...] | None:
    """Read back the returns a refutation's prelude lists, or None if it lists none.

    They are the ``forsetiReturns`` that `shortfall_refutation` defines, one
    numeral per step.
    """
    for line in prelude.split("\n"):
        if line.startswith(_RETURNS_DEFINITION) and line.endswith("]"):
            listed = line.removeprefix(_RETURNS_DEFINITION).removesuffix("]")
            try:
                return tuple(Fraction(part) for part in listed.split(", "))
            except (ValueError, ZeroDivisionError):
                return None
    return None


def path_words(path: Sequence[bool], up: Fraction, down: Fraction) -> str:
    """Write an extreme path in words, by its pattern when it has a short one.

    ``True`` marks a rise of ``up``, ``False`` a fall of ``down``, each a
    simple return written as a percentage with its exact value beside
    (`figures.move`). The path of only rises or only falls, the zigzag (up and
    down by turns) and a path repeating a unit of at most `_PERIOD` moves are
    written by that pattern; any other by its moves, ``UDDU…`` with U a rise
    and D a fall, and past `_WRITTEN_MOVES` steps by its length and first
    moves.
    """
    steps = len(path)
    rise, fall = move(up), move(-down)
    letters = "".join("U" if moved_up else "D" for moved_up in path)
    if all(path):
        return f"the path of {_count(steps, 'rise')} of {rise}"
    if not any(path):
        return f"the path of {_count(steps, 'fall')} of {fall}"
    legend = f"U a rise of {rise}, D a fall of {fall}"
    period = _period(letters)
    if period == 2:
        first, second = (rise, fall) if path[0] else (fall, rise)
        turns = "up and down" if path[0] else "down and up"
        return f"the zigzag ({turns} by turns) of {steps} steps of {first} and {second}"
    if period is not None:
        unit = letters[:period]
        return f"the path repeating {unit} over {steps} steps ({legend})"
    if steps <= _WRITTEN_MOVES:
        return f"the path {letters} ({legend})"
    return f"a path of {steps} steps starting {letters[:_SHOWN_MOVES]}… ({legend})"


def _period(letters: str) -> int | None:
    """Return the shortest unit, of at most `_PERIOD` moves, that the path repeats.

    It must repeat at least twice; the path may end inside a repetition.
    """
    for period in range(1, _PERIOD + 1):
        if len(letters) < 2 * period:
            return None
        if all(letters[i] == letters[i % period] for i in range(len(letters))):
            return period
    return None


def _count(steps: int, word: str) -> str:
    """Write ``1 rise`` or ``20 rises``."""
    return f"{steps} {word}" if steps == 1 else f"{steps} {word}s"


def _application(
    proposition: str,
    statement: str,
    lines: tuple[str, ...],
    dependencies: tuple[str, ...],
    definitions: tuple[str, ...] = (),
) -> RootApplication:
    return RootApplication(
        prelude(proposition, definitions),
        statement,
        (f"unfold {CLAIM}", *lines),
        dependencies,
    )


def _price_cast(goal: HedgingGoal, payoff: Payoff) -> str:
    """Name ``hp``: the real price on the band is the rational one."""
    strike = argument(goal.strike)
    return (
        f"have hp := {short(_PRICE_CAST)} (fun S => {short(payoff.cast_lemma)}"
        f" {strike} S) {argument(goal.up)} {argument(goal.down)}"
        f" (by norm_num) (by norm_num) (by norm_num) {goal.steps}"
        f" {argument(goal.spot)}"
    )


def _price_q(goal: HedgingGoal, payoff: Payoff) -> str:
    """Write the binomial price over ``ℚ``, which the kernel evaluates."""
    return (
        f"priceQ ({_payoff_q(goal, payoff)}) {argument(goal.up)}"
        f" {argument(goal.down)} {goal.steps} {argument(goal.spot)}"
    )


def covers_hold(goal: HedgingGoal, payoff: Payoff) -> RootApplication:
    """Prove a ``covers`` claim from ``price ≤ P``: the price covers, so ``P`` does."""
    hedged = band(goal.up, goal.down)
    strike = argument(goal.strike)
    return _application(
        covers_claim(goal, payoff, "    "),
        PROVED,
        (
            f"have hq : {_price_q(goal, payoff)} ≤ {numeral(_premium(goal))}"
            " := by decide +kernel",
            "have hr := Rat.cast_le (K := ℝ) |>.mpr hq",
            _price_cast(goal, payoff),
            "push_cast at hp hr",
            "rw [← hp] at hr",
            f"exact {short(_COVERS_MONO)} hr ({short(_COVERS_HEDGE)} ({hedged})"
            f" ({short(payoff.convexity)} {strike}) {goal.steps} (by norm_num))",
        ),
        (
            _COVERS_HEDGE,
            _COVERS_MONO,
            _PRICE_CAST,
            payoff.cast_lemma,
            payoff.convexity,
        ),
    )


def covers_refutation(goal: HedgingGoal, payoff: Payoff) -> RootApplication:
    """Refute a ``covers`` claim from ``P < price``.

    The proposition is ``∀ δ, ¬ Covers …``: every spot-feedback strategy, not
    just the band's hedge, falls short from ``P``, which `not_covers_of_lt`
    proves at once. So the statement is ``forsetiClaim`` itself.
    """
    return _application(
        no_cover_claim(goal, payoff, "    "),
        PROVED,
        (
            f"have hq : ({numeral(_premium(goal))} : ℚ) < {_price_q(goal, payoff)}"
            " := by decide +kernel",
            "have hr := Rat.cast_lt (K := ℝ) |>.mpr hq",
            _price_cast(goal, payoff),
            "push_cast at hp hr",
            "rw [← hp] at hr",
            f"exact {short(_NOT_COVERS)} ({band(goal.up, goal.down)})"
            f" ({_payoff(goal, payoff)}) {goal.steps} (by norm_num) hr",
        ),
        (_NOT_COVERS, _PRICE_CAST, payoff.cast_lemma),
    )


def shortfall_hold(goal: HedgingGoal, payoff: Payoff) -> RootApplication:
    """Prove a ``shortfall`` claim with ``X ≥ 0`` for a hedge on its own band.

    `covers_hedge` gives ``g (S N) ≤ wealth …`` on every path of the band, so
    the wealth less the payoff is at least ``0 ≥ -X``.
    """
    return _application(
        shortfall_claim(goal, payoff, "    "),
        PROVED,
        (
            "intro S h0 hband",
            f"have hc := {short(_COVERS_HEDGE)} ({band(goal.up, goal.down)})"
            f" ({short(payoff.convexity)} {argument(goal.strike)}) {goal.steps}"
            f" (by norm_num : (0 : ℝ) < {numeral(goal.spot)}) S h0 hband",
            "linarith",
        ),
        (_COVERS_HEDGE, payoff.convexity),
    )


def shortfall_refutation(
    goal: HedgingGoal,
    payoff: Payoff,
    returns: Sequence[Fraction],
    shortfall: Fraction,
) -> RootApplication:
    """Refute a ``shortfall`` claim on the path of ``returns``.

    The path is ``spotQ S₀`` of the returns, a list the prelude defines; each
    return is inside the against band, so `spotQ_inBand` puts the path in it.
    `wealth_hedge_cast` and the payoff's cast lemma turn the claim's wealth
    less payoff on that path into the rational ``shortfall``, which the kernel
    computes exactly, and which is below ``-X``.
    """
    assert goal.against_up is not None and goal.against_down is not None
    assert len(returns) == goal.steps
    n, spot = goal.steps, argument(goal.spot)
    strike, cast = argument(goal.strike), short(payoff.cast_lemma)
    path = f"spotQ {spot} {_RETURNS}"
    listed = ", ".join(numeral(value) for value in returns)
    up, down = argument(goal.against_up), argument(goal.against_down)
    lines = (
        "intro h",
        f"have hr : ∀ k < {n}, -({numeral(goal.against_down)} : ℚ)"
        " ≤ forsetiReturns.getD k 0"
        f" ∧ forsetiReturns.getD k 0 ≤ {numeral(goal.against_up)}"
        " := by decide +kernel",
        f"have h0 : (({path} 0 : ℚ) : ℝ) = {numeral(goal.spot)}"
        " := by norm_num [spotQ]",
        f"have key := h (fun k => (({path} k : ℚ) : ℝ)) h0"
        f" ({short(_SPOT_IN_BAND)} (S₀ := {spot}) (N := {n})"
        f" (r := {_RETURNS_FUN}) {up} {down}"
        " (by norm_num) (by norm_num) (by norm_num) (by norm_num) hr)",
        f"have hw := {short(_WEALTH_CAST)} (fun S => {cast} {strike} S)"
        f" {argument(goal.up)} {argument(goal.down)}"
        f" (by norm_num) (by norm_num) (by norm_num) {n} {spot} {_RETURNS} {n}",
        f"have hc := {cast} {strike} ({path} {n})",
        f"have hk : wealthQ ({_payoff_q(goal, payoff)}) {argument(goal.up)}"
        f" {argument(goal.down)} {n} {spot} {_RETURNS} {n}"
        f" - {_payoff_q(goal, payoff)} ({path} {n}) = {numeral(shortfall)}"
        " := by decide +kernel",
        "have hs := congrArg (Rat.cast : ℚ → ℝ) hk",
        "push_cast at hw hc hs",
        "linarith",
    )
    return _application(
        shortfall_claim(goal, payoff, "    "),
        REFUTED,
        lines,
        (_WEALTH_CAST, _SPOT_IN_BAND, payoff.cast_lemma),
        (f"def forsetiReturns : List ℚ := [{listed}]",),
    )
