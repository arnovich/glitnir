"""The ODE lane's example goals over the Lean-compiled models.

The first model is Asgard's three-state system compiled by ``asgard-lean`` from
its source equations, with its declared initial state ``(1, 2, −1)`` at
``t = 2``; ``V = x² + y² + z²`` is its observed energy. The oscillators, the
driven forced decay and the driven hedged spread follow. Each goal is written
here as data, and no evidence is: the service finds it or reports why not.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Formula,
    Neg,
    Relation,
    Var,
)

from .goal import TrajectoryGoal

_V = Var(0)
DECLARED_INITIAL = (
    ("initial-x", Fraction(1)),
    ("initial-y", Fraction(2)),
    ("initial-z", Fraction(-1)),
)


def _at_most(bound: Fraction) -> Formula:
    return Atom(Relation.LE, Add(_V, Neg(Const(bound))))


def _at_least(bound: Fraction) -> Formula:
    return Atom(Relation.GE, Add(_V, Neg(Const(bound))))


def energy_goal(
    post: Formula,
    *,
    start: Fraction = Fraction(2),
    initial: tuple[tuple[str, Fraction], ...] = DECLARED_INITIAL,
    observation: str = "obs-v",
    name: str = "V",
    obligation: str = "contract",
    quantifier: str = "always",
    interpretation: str = "continuous-real",
    model: str = "three-state",
) -> TrajectoryGoal:
    """Pose a goal about a model's observed energy (three-state by default)."""
    return TrajectoryGoal(
        model=model,
        interpretation=interpretation,
        obligation=obligation,
        axis="time",
        start=start,
        initial=initial,
        observation=observation,
        name=name,
        quantifier=quantifier,
        post=post,
    )


@dataclass(frozen=True, slots=True)
class Example:
    """One example goal and what it must end as."""

    name: str
    claim: str
    goal: TrajectoryGoal
    expected: str


BAND = And(_at_least(Fraction(0)), _at_most(Fraction(6)))

#: The damped oscillator's declared initial state, x(0) = 1 and v(0) = 0,
#: sorted by port ID as goals require.
OSCILLATOR_INITIAL = (("initial-v", Fraction(0)), ("initial-x", Fraction(1)))


def oscillator_goal(
    post: Formula,
    *,
    initial: tuple[tuple[str, Fraction], ...] = OSCILLATOR_INITIAL,
    start: Fraction = Fraction(0),
    model: str = "damped-oscillator",
) -> TrajectoryGoal:
    """Pose a goal about an oscillator's energy (the damped one by default)."""
    return energy_goal(
        post,
        start=start,
        initial=initial,
        observation="obs-e",
        name="E",
        model=model,
    )


#: The Lorenz system's declared start, (1, 1, 1) at t = 0.
LORENZ_INITIAL = (
    ("initial-x", Fraction(1)),
    ("initial-y", Fraction(1)),
    ("initial-z", Fraction(1)),
)


def lorenz_goal(post: Formula) -> TrajectoryGoal:
    """Pose a goal about the Lorenz system's ``V = x² + y² + (z − 38)²``."""
    return energy_goal(post, start=Fraction(0), initial=LORENZ_INITIAL, model="lorenz")


#: The T3 truncation's declared start, (1, …, 1) at t = 0, sorted by port ID.
NS_T3_INITIAL = (
    ("initial-a10", Fraction(1)),
    ("initial-a11", Fraction(1)),
    ("initial-a21", Fraction(1)),
)


def ns_t3_goal(post: Formula, *, enstrophy: bool = False) -> TrajectoryGoal:
    """Pose a goal about the T3 truncation's energy ``E`` or enstrophy ``Z``."""
    return energy_goal(
        post,
        start=Fraction(0),
        initial=NS_T3_INITIAL,
        observation="obs-z" if enstrophy else "obs-e",
        name="Z" if enstrophy else "E",
        model="ns-t3",
    )


#: The K5 truncation's declared start, (1, …, 1) at t = 0, sorted by port ID.
NS_K5_INITIAL = (
    ("initial-a01", Fraction(1)),
    ("initial-a10", Fraction(1)),
    ("initial-a11", Fraction(1)),
    ("initial-a12", Fraction(1)),
    ("initial-a21", Fraction(1)),
)


def ns_k5_goal(post: Formula, *, enstrophy: bool = False) -> TrajectoryGoal:
    """Pose a goal about the K5 truncation's energy ``E`` or enstrophy ``Z``."""
    return energy_goal(
        post,
        start=Fraction(0),
        initial=NS_K5_INITIAL,
        observation="obs-z" if enstrophy else "obs-e",
        name="Z" if enstrophy else "E",
        model="ns-k5",
    )


#: The B2 truncation's declared start, (1, …, 1) at t = 0, sorted by port ID.
NS_B2_INITIAL = (
    ("initial-a01", Fraction(1)),
    ("initial-a02", Fraction(1)),
    ("initial-a10", Fraction(1)),
    ("initial-a11", Fraction(1)),
    ("initial-a12", Fraction(1)),
    ("initial-a1m1", Fraction(1)),
    ("initial-a1m2", Fraction(1)),
    ("initial-a20", Fraction(1)),
    ("initial-a21", Fraction(1)),
    ("initial-a22", Fraction(1)),
    ("initial-a2m1", Fraction(1)),
    ("initial-a2m2", Fraction(1)),
)


def ns_b2_goal(post: Formula, *, enstrophy: bool = False) -> TrajectoryGoal:
    """Pose a goal about the B2 truncation's energy ``E`` or enstrophy ``Z``."""
    return energy_goal(
        post,
        start=Fraction(0),
        initial=NS_B2_INITIAL,
        observation="obs-z" if enstrophy else "obs-e",
        name="Z" if enstrophy else "E",
        model="ns-b2",
    )


def ns_t3s_goal(post: Formula, *, observe: str = "obs-r") -> TrajectoryGoal:
    """Pose a goal about the small-forcing member's ``R`` (or ``obs-e``, ``obs-z``)."""
    names = {"obs-e": "E", "obs-z": "Z", "obs-r": "R"}
    return energy_goal(
        post,
        start=Fraction(0),
        initial=NS_T3_INITIAL,
        observation=observe,
        name=names[observe],
        model="ns-t3s",
    )


def forced_goal(
    post: Formula,
    *,
    drivers: tuple[tuple[str, Fraction], ...] = (("driver-u", Fraction(1)),),
) -> TrajectoryGoal:
    """Pose a goal about the forced decay's ``x``, under a driver bound."""
    return TrajectoryGoal(
        model="forced-decay",
        interpretation="continuous-real",
        obligation="contract",
        axis="time",
        start=Fraction(0),
        initial=(("initial-x", Fraction(0)),),
        observation="obs-x",
        name="x",
        quantifier="always",
        post=post,
        drivers=drivers,
    )


def hedged_goal(
    post: Formula,
    *,
    observe: str = "obs-s",
    drivers: tuple[tuple[str, Fraction], ...] = (("driver-d", Fraction(1)),),
) -> TrajectoryGoal:
    """Pose a goal about the hedged spread's ``s`` under a driver bound.

    ``observe="obs-v"`` poses it about its quadratic ``V`` instead.
    """
    names = {"obs-s": "s", "obs-v": "V"}
    return TrajectoryGoal(
        model="hedged-spread",
        interpretation="continuous-real",
        obligation="contract",
        axis="time",
        start=Fraction(0),
        initial=(("initial-h", Fraction(0)), ("initial-s", Fraction(0))),
        observation=observe,
        name=names[observe],
        quantifier="always",
        post=post,
        drivers=drivers,
    )


EXAMPLES = (
    Example(
        "energy-band",
        "well-posed from (1,2,−1) at t=2, and 0 ≤ V ≤ 6 for all t ≥ 2",
        energy_goal(BAND),
        "PROVEN",
    ),
    Example(
        "energy-below-seven",
        "well-posed, and V ≤ 7 for all t ≥ 2",
        energy_goal(_at_most(Fraction(7))),
        "PROVEN",
    ),
    Example(
        "energy-below-five",
        "V ≤ 5 for all t ≥ 2: refuted at t = 2, where V = 6",
        energy_goal(_at_most(Fraction(5))),
        "REFUTED",
    ),
    Example(
        "energy-at-least-one",
        "V ≥ 1 for all t ≥ 2: the band allows V = 0, but no admissible "
        "trajectory is shown to reach it",
        energy_goal(_at_least(Fraction(1))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "energy-positive",
        "V > 0 for all t ≥ 2: strict, outside the certificate fragment",
        energy_goal(Atom(Relation.GT, _V)),
        "UNKNOWN:unsupported",
    ),
    Example(
        "stochastic",
        "the band under an Itô reading of the same circuit",
        energy_goal(BAND, interpretation="stochastic-ito"),
        "UNKNOWN:unsupported",
    ),
    Example(
        "oscillator-band",
        "damped oscillator from (x, v) = (1, 0) at t = 0: well-posed, and "
        "0 ≤ E ≤ 2 for all t ≥ 0, E = 2x² + v²",
        oscillator_goal(And(_at_least(Fraction(0)), _at_most(Fraction(2)))),
        "PROVEN",
    ),
    Example(
        "oscillator-below-three",
        "damped oscillator: E ≤ 3 for all t ≥ 0",
        oscillator_goal(_at_most(Fraction(3))),
        "PROVEN",
    ),
    Example(
        "oscillator-below-one",
        "damped oscillator: E ≤ 1 for all t ≥ 0, refuted at t = 0, where E = 2",
        oscillator_goal(_at_most(Fraction(1))),
        "REFUTED",
    ),
    Example(
        "oscillator-other-start",
        "damped oscillator from (x, v) = (2, 0): no contract is known",
        oscillator_goal(
            _at_most(Fraction(8)),
            initial=(("initial-v", Fraction(0)), ("initial-x", Fraction(2))),
        ),
        "UNKNOWN:unsupported",
    ),
    Example(
        "harmonic-band",
        "undamped oscillator 4x'' + x = 0 from (1, 0) at t = 0: well-posed, and "
        "0 ≤ E ≤ 1 for all t ≥ 0, E = x² + 4v² (conserved)",
        oscillator_goal(
            And(_at_least(Fraction(0)), _at_most(Fraction(1))),
            model="harmonic-oscillator",
        ),
        "PROVEN",
    ),
    Example(
        "harmonic-below-two",
        "undamped oscillator: E ≤ 2 for all t ≥ 0",
        oscillator_goal(_at_most(Fraction(2)), model="harmonic-oscillator"),
        "PROVEN",
    ),
    Example(
        "harmonic-below-half",
        "undamped oscillator: E ≤ 1/2 for all t ≥ 0, refuted at t = 0, where E = 1",
        oscillator_goal(_at_most(Fraction(1, 2)), model="harmonic-oscillator"),
        "REFUTED",
    ),
    Example(
        "forced-band",
        "forced decay x' + x = u' + u from x = 0 at t = 0, for admitted drivers "
        "with |u| ≤ 1: well-posed, and −2 ≤ x ≤ 2 for all t ≥ 0",
        forced_goal(And(_at_least(Fraction(-2)), _at_most(Fraction(2)))),
        "PROVEN",
    ),
    Example(
        "forced-small-driver",
        "forced decay, for admitted drivers with |u| ≤ 1/2: x ≤ 3 for all t ≥ 0",
        forced_goal(_at_most(Fraction(3)), drivers=(("driver-u", Fraction(1, 2)),)),
        "PROVEN",
    ),
    Example(
        "forced-negative",
        "forced decay: x ≤ −1 for all t ≥ 0, refuted at t = 0 under the zero "
        "driver, where x = 0",
        forced_goal(_at_most(Fraction(-1))),
        "REFUTED",
    ),
    Example(
        "forced-below-one",
        "forced decay: x ≤ 1 for all t ≥ 0; false (u = cos passes it), but no "
        "bounded template shows it",
        forced_goal(_at_most(Fraction(1))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "forced-no-precondition",
        "forced decay: −2 ≤ x ≤ 2 with no driver precondition, which would be "
        "`True`: refused",
        forced_goal(And(_at_least(Fraction(-2)), _at_most(Fraction(2))), drivers=()),
        "UNKNOWN:unsupported",
    ),
    Example(
        "forced-large-driver",
        "forced decay, for |u| ≤ 3: x ≤ 2, beyond the known contract (and false: "
        "u = 3 gives x(2) > 2)",
        forced_goal(_at_most(Fraction(2)), drivers=(("driver-u", Fraction(3)),)),
        "UNKNOWN:unsupported",
    ),
    Example(
        "hedged-band",
        "hedged spread s' = -s - h + d, h' = s - h from (0, 0) at t = 0, for "
        "admitted drivers with |d| ≤ 1: well-posed, and −1 ≤ s ≤ 1 for all t ≥ 0",
        hedged_goal(And(_at_least(Fraction(-1)), _at_most(Fraction(1)))),
        "PROVEN",
    ),
    Example(
        "hedged-energy-band",
        "hedged spread, for |d| ≤ 1: 0 ≤ V ≤ 1 for all t ≥ 0, V = s² + h²",
        hedged_goal(
            And(_at_least(Fraction(0)), _at_most(Fraction(1))), observe="obs-v"
        ),
        "PROVEN",
    ),
    Example(
        "hedged-small-driver",
        "hedged spread, for admitted drivers with |d| ≤ 1/2: s ≤ 1 for all t ≥ 0",
        hedged_goal(_at_most(Fraction(1)), drivers=(("driver-d", Fraction(1, 2)),)),
        "PROVEN",
    ),
    Example(
        "hedged-energy-at-least-quarter",
        "hedged spread: V ≥ 1/4 for all t ≥ 0, refuted at t = 0 under the zero "
        "driver, where V = 0",
        hedged_goal(_at_least(Fraction(1, 4)), observe="obs-v"),
        "REFUTED",
    ),
    Example(
        "hedged-below-half",
        "hedged spread: s ≤ 1/2 for all t ≥ 0; false (d = 1 takes s to "
        "1/2 + e^(−π/2)/2 at t = π/2), but no bounded template shows it and the "
        "start does not refute it",
        hedged_goal(_at_most(Fraction(1, 2))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "hedged-no-precondition",
        "hedged spread: −1 ≤ s ≤ 1 with no driver precondition, which would be "
        "`True`: refused",
        hedged_goal(And(_at_least(Fraction(-1)), _at_most(Fraction(1))), drivers=()),
        "UNKNOWN:unsupported",
    ),
    Example(
        "hedged-large-driver",
        "hedged spread, for |d| ≤ 3: s ≤ 1, beyond the known contract (and false: "
        "d = 3 gives s(π/2) > 1)",
        hedged_goal(_at_most(Fraction(1)), drivers=(("driver-d", Fraction(3)),)),
        "UNKNOWN:unsupported",
    ),
    Example(
        "lorenz-band",
        "Lorenz σ = 10, ρ = 28, β = 8/3 from (1, 1, 1) at t = 0: well-posed, and "
        "0 ≤ V ≤ 1600 for all t ≥ 0, V = x² + y² + (z − 38)²",
        lorenz_goal(And(_at_least(Fraction(0)), _at_most(Fraction(1600)))),
        "PROVEN",
    ),
    Example(
        "lorenz-below-thirteen-hundred",
        "Lorenz: V ≤ 1300 for all t ≥ 0, refuted at t = 0, where V = 1371",
        lorenz_goal(_at_most(Fraction(1300))),
        "REFUTED",
    ),
    Example(
        "lorenz-below-fourteen-hundred",
        "Lorenz: V ≤ 1400 for all t ≥ 0; true along the trajectory as far as a "
        "simulation sees, but the contract gives only 1600 and t = 0 does not "
        "refute it",
        lorenz_goal(_at_most(Fraction(1400))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "ns-t3-band",
        "T3: 3-mode 2D Navier–Stokes truncation, ν = 1/10, f = 1, from "
        "(1, …, 1): well-posed, and 0 ≤ E ≤ 13 for all t ≥ 0",
        ns_t3_goal(And(_at_least(Fraction(0)), _at_most(Fraction(13)))),
        "PROVEN",
    ),
    Example(
        "ns-t3-below-one",
        "T3: E ≤ 1 for all t ≥ 0, refuted at t = 0, where E = 17/10",
        ns_t3_goal(_at_most(Fraction(1))),
        "REFUTED",
    ),
    Example(
        "ns-t3-below-10",
        "T3: E ≤ 10 for all t ≥ 0, from forseti-lean 042's degree-4 tube around "
        "the member's simulated focus, whose contract gives 9",
        ns_t3_goal(_at_most(Fraction(10))),
        "PROVEN",
    ),
    Example(
        "ns-t3-below-8",
        "T3: E ≤ 8 for all t ≥ 0; a simulation from the start peaks near "
        "7.0, but the contract gives only 9 and t = 0 does not refute it",
        ns_t3_goal(_at_most(Fraction(8))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "ns-t3-below-12",
        "T3: E ≤ 12 for all t ≥ 0, below the laminar level 25/2 that no "
        "origin-centred form reaches (forseti-lean 040's ellipsoid; the band "
        "is now 042's 9)",
        ns_t3_goal(_at_most(Fraction(12))),
        "PROVEN",
    ),
    Example(
        "ns-t3-enstrophy-ball",
        "T3: the enstrophy Z = Σ a_k² stays in 0 ≤ Z ≤ 26 for all t ≥ 0, from "
        "the enstrophy contract (level f²/(4ν²) = 25, uniform in the member)",
        ns_t3_goal(And(_at_least(Fraction(0)), _at_most(Fraction(26))), enstrophy=True),
        "PROVEN",
    ),
    Example(
        "ns-t3-enstrophy-below-20",
        "T3: Z ≤ 20 for all t ≥ 0; the contract gives only 26 and the start, "
        "where Z = 3, does not refute it",
        ns_t3_goal(_at_most(Fraction(20)), enstrophy=True),
        "UNKNOWN:exhausted",
    ),
    Example(
        "ns-t3s-rest-ball",
        "T3S: T3's modes at ν = 1/2, f = 1, below the small-forcing threshold: "
        "the unforced energy R = Σ_{k≠(1,1)} a_k²/λ_k never exceeds its start "
        "value 6/5 (the laminar line attracts; R decays at rate 1/5)",
        ns_t3s_goal(And(_at_least(Fraction(0)), _at_most(Fraction(6, 5)))),
        "PROVEN",
    ),
    Example(
        "ns-t3s-rest-below-one",
        "T3S: R ≤ 1 for all t ≥ 0, refuted at t = 0, where R = 6/5 (it decays "
        "below 1 soon after, but the claim is about every time)",
        ns_t3s_goal(_at_most(Fraction(1))),
        "REFUTED",
    ),
    Example(
        "ns-t3s-energy-ball",
        "T3S: 0 ≤ E ≤ 17/10 for all t ≥ 0, the energy ball at the start level",
        ns_t3s_goal(
            And(_at_least(Fraction(0)), _at_most(Fraction(17, 10))), observe="obs-e"
        ),
        "PROVEN",
    ),
    Example(
        "ns-t3s-enstrophy-ball",
        "T3S: 0 ≤ Z ≤ 4 for all t ≥ 0, the ball on which the decay is proved",
        ns_t3s_goal(
            And(_at_least(Fraction(0)), _at_most(Fraction(4))), observe="obs-z"
        ),
        "PROVEN",
    ),
    Example(
        "ns-k5-band",
        "K5: 5-mode 2D Navier–Stokes truncation, ν = 1/10, f = 1, from "
        "(1, …, 1): well-posed, and 0 ≤ E ≤ 13 for all t ≥ 0",
        ns_k5_goal(And(_at_least(Fraction(0)), _at_most(Fraction(13)))),
        "PROVEN",
    ),
    Example(
        "ns-k5-below-one",
        "K5: E ≤ 1 for all t ≥ 0, refuted at t = 0, where E = 29/10",
        ns_k5_goal(_at_most(Fraction(1))),
        "REFUTED",
    ),
    Example(
        "ns-k5-below-6",
        "K5: E ≤ 6 for all t ≥ 0, below the laminar level 25/2: forseti-lean "
        "041's degree-4 tube around the member's simulated attractor, trapped under a "
        "decrease local to the set",
        ns_k5_goal(_at_most(Fraction(6))),
        "PROVEN",
    ),
    Example(
        "ns-k5-below-5",
        "K5: E ≤ 5 for all t ≥ 0; a simulation from the start peaks near "
        "4.9, but the contract gives only 6 and t = 0 does not refute it",
        ns_k5_goal(_at_most(Fraction(5))),
        "UNKNOWN:exhausted",
    ),
    Example(
        "ns-b2-band",
        "B2: 12-mode 2D Navier–Stokes truncation, ν = 1/50, f = 1, from "
        "(1, …, 1): well-posed, and 0 ≤ E ≤ 313 for all t ≥ 0",
        ns_b2_goal(And(_at_least(Fraction(0)), _at_most(Fraction(313)))),
        "PROVEN",
    ),
    Example(
        "ns-b2-below-one",
        "B2: E ≤ 1 for all t ≥ 0, refuted at t = 0, where E = 91/20",
        ns_b2_goal(_at_most(Fraction(1))),
        "REFUTED",
    ),
    Example(
        "ns-k5-enstrophy-ball",
        "K5: 0 ≤ Z ≤ 26 for all t ≥ 0, from the enstrophy contract (the same "
        "level as T3's: it does not depend on the modes)",
        ns_k5_goal(And(_at_least(Fraction(0)), _at_most(Fraction(26))), enstrophy=True),
        "PROVEN",
    ),
    Example(
        "ns-k5-enstrophy-below-20",
        "K5: Z ≤ 20 for all t ≥ 0; the contract gives only 26 and the start, "
        "where Z = 5, does not refute it",
        ns_k5_goal(_at_most(Fraction(20)), enstrophy=True),
        "UNKNOWN:exhausted",
    ),
    Example(
        "ns-b2-enstrophy-ball",
        "B2: 0 ≤ Z ≤ 626 for all t ≥ 0, from the enstrophy contract (level "
        "f²/(4ν²) = 625), so every amplitude stays within √626",
        ns_b2_goal(
            And(_at_least(Fraction(0)), _at_most(Fraction(626))), enstrophy=True
        ),
        "PROVEN",
    ),
    Example(
        "ns-b2-below-150",
        "B2: E ≤ 150 for all t ≥ 0; a simulation from the start peaks near "
        "98, but the contract gives only 313 and t = 0 does not refute it",
        ns_b2_goal(_at_most(Fraction(150))),
        "UNKNOWN:exhausted",
    ),
)
