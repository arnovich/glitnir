"""The stepped models a stepped goal may name, and the Lean names a block cites.

One model is registered: the hedged spread of
``Gimle.Forseti.Examples.HedgedSpread`` advanced by explicit Euler steps of
size ``Δ = 1/256`` under independent random shocks in its driver slot
(``Gimle.Forseti.Examples.HedgedSpreadStepped``), from the flat book
``(0, 0)``. Its deterministic part is ``A = [[1 − Δ, −Δ], [Δ, 1 − Δ]]``, and a
shock enters the exposure ``s`` only, ``B = (1, 0)``. It observes the exposure
``s``, whose band ``|s| ≤ B`` a breach leaves, and the energy ``V = s² + h²``,
whose expectation it bounds.

An entry records what the decider's exact arithmetic needs (the step, the
matrix, the shock vector, the initial state) and the qualified Lean names a
block cites, with the namespaces a block opens to write them short, and the
compiled driven model a ``sample`` directive names. Like a
trajectory entry it decides which constants a goal's words refer to and is not
trusted to be right: the arithmetic only chooses a template and its constants,
and Lean checks the block, so numbers that disagree with the Lean make a block
Lean refuses, never a proof of another claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

_STEPPED = "Gimle.Forseti.Stepped"
_HEDGED = "Gimle.Forseti.Examples.HedgedSpread"
_CONTRACT = "Gimle.Forseti.Examples.HedgedSpreadContract"
_MODULE = "Gimle.Forseti.Examples.HedgedSpreadStepped"
_DELTA = Fraction(1, 256)


@dataclass(frozen=True, slots=True)
class SteppedObservation:
    """An observed quantity of a stepped model.

    Attributes:
        lean_name: The Lean name a claim about it cites: for an exposure, the
            predicate of its band, ``B ↦ (x ↦ |x c| ≤ B)``; for an energy, the
            energy function whose expectation is bounded.
        kind: ``exposure`` (a breach claim's) or ``energy`` (an expectation's).
        component: For an exposure, its state component ``c``, the row of the
            shock weights the proxy follows; None for an energy.
    """

    lean_name: str
    kind: Literal["exposure", "energy"]
    component: int | None = None


@dataclass(frozen=True, slots=True)
class SteppedTheorems:
    """The model's Lean theorems and definitions a block cites, qualified.

    Attributes:
        breach_within: The horizon bound ``N·Δ²·v/B²`` for a two-moment law.
        exceeds_at_proxy: The bound at one mark for a sub-Gaussian law of a
            whole-number proxy, from the rational ``proxy``.
        proxy: The exposure's variance proxy at a mark, over ``ℚ``.
        proxy_rec_eq: ``proxyRec v n = proxy v n``, the form the kernel runs.
        proxy_mono: The proxy grows with the mark.
        expected_energy_eq: The exact expected energy at a mark.
        geom_sum: ``∑_{k<n} ρᵏ`` by recursion over ``ℚ``.
        geom_sum_eq: That recursion is the geometric sum.
        initial_eq: The initial state as a point.
        step_zero_fixed: A zero shock leaves the initial state in place.
        step_big_breaches: One jump ``|L| > 1/Δ`` leaves the band ``|s| ≤ 1``;
            stated for the barrier ``1`` only.
        not_breach_exact: The three-point refutation at ``1 − (1 − 2ε)ᴺ``.
        not_breach_bernoulli: The same at Bernoulli's ``2εN / (1 + 2εN)``.
    """

    breach_within: str
    exceeds_at_proxy: str
    proxy: str
    proxy_rec_eq: str
    proxy_mono: str
    expected_energy_eq: str
    geom_sum: str
    geom_sum_eq: str
    initial_eq: str
    step_zero_fixed: str
    step_big_breaches: str
    not_breach_exact: str
    not_breach_bernoulli: str


@dataclass(frozen=True, slots=True)
class SteppedEntry:
    """One stepped model.

    Attributes:
        key: The stable name a goal uses.
        imports: The Lean modules a block needs.
        opens: The namespaces a block opens, so it cites names short.
        step_name: The step map ``x d ↦ x'``.
        compiled_name: The compiled driven model the step advances, which a
            ``sample`` directive names.
        initial_name: The initial state the chain starts from.
        delta_name: The Lean constant of the step size.
        delta: The step size.
        matrix: The deterministic part ``A`` of one step, row by row.
        shock: The vector ``B`` a unit shock adds to the state.
        initial: The initial state, the only start the theorems know.
        observations: The observed quantities, by the name a goal uses.
        theorems: The Lean names a block cites.
        start: The time the chain starts at.
        horizon_cap: The most steps a claim may range over.
        unrolled_cap: The most steps a fixed-date or expected claim may range
            over. Their proofs may have the kernel unroll a recursion over
            every step (``proxyRec``, ``geomSum``), and the independent
            replay's kernel hits its recursion limit well below ``horizon_cap``.
    """

    key: str
    imports: tuple[str, ...]
    opens: tuple[str, ...]
    step_name: str
    compiled_name: str
    initial_name: str
    delta_name: str
    delta: Fraction
    matrix: tuple[tuple[Fraction, ...], ...]
    shock: tuple[Fraction, ...]
    initial: tuple[Fraction, ...]
    observations: dict[str, SteppedObservation]
    theorems: SteppedTheorems
    start: Fraction = Fraction(0)
    horizon_cap: int = 4096
    unrolled_cap: int = 1024


HEDGED_SPREAD_STEPPED = SteppedEntry(
    key="hedged-spread",
    imports=(_STEPPED, _MODULE),
    opens=("MeasureTheory", _STEPPED, _HEDGED, _CONTRACT, _MODULE),
    step_name=f"{_MODULE}.step",
    compiled_name=f"{_HEDGED}.compiled",
    initial_name=f"{_HEDGED}.compiled.initial",
    delta_name=f"{_MODULE}.Δ",
    delta=_DELTA,
    matrix=((1 - _DELTA, -_DELTA), (_DELTA, 1 - _DELTA)),
    shock=(Fraction(1), Fraction(0)),
    initial=(Fraction(0), Fraction(0)),
    observations={
        "s": SteppedObservation(f"{_MODULE}.exposureWithin", "exposure", 0),
        "V": SteppedObservation(f"{_CONTRACT}.energy", "energy"),
    },
    theorems=SteppedTheorems(
        breach_within=f"{_MODULE}.breach_within",
        exceeds_at_proxy=f"{_MODULE}.exceeds_at_proxy",
        proxy=f"{_MODULE}.proxy",
        proxy_rec_eq=f"{_MODULE}.proxyRec_eq",
        proxy_mono=f"{_MODULE}.proxy_mono",
        expected_energy_eq=f"{_MODULE}.expected_energy_eq",
        geom_sum=f"{_MODULE}.geomSum",
        geom_sum_eq=f"{_MODULE}.geomSum_eq",
        initial_eq=f"{_HEDGED}.initial_eq",
        step_zero_fixed=f"{_MODULE}.step_zero_fixed",
        step_big_breaches=f"{_MODULE}.step_big_breaches",
        not_breach_exact=f"{_STEPPED}.not_breachWithin_threePoint_exact",
        not_breach_bernoulli=f"{_STEPPED}.not_breachWithin_threePoint",
    ),
)

MODELS: dict[str, SteppedEntry] = {HEDGED_SPREAD_STEPPED.key: HEDGED_SPREAD_STEPPED}


def replay_imports() -> tuple[tuple[str, ...], ...]:
    """Every import set a stepped block uses, for the replay pin."""
    return tuple(sorted({entry.imports for entry in MODELS.values()}))
