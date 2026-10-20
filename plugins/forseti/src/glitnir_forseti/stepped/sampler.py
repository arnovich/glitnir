"""Sample a registered stepped model under random shocks, in float64.

The ``sample`` evidence verb (`glitnir_forseti.notebook.evidence`) has Lean
print a stepped model's compiled field over its runtime coordinates, with the
coordinates' IDs, the index of its one driver among them, the initial state
and the step, all exact. `sample_payload` reads that print. `sample` then
steps every path at once, ``x ← x + Δ·field(d, x)``, with the shock ``d`` in
the driver's coordinate and each state in its own. It records the observed
state along the first few paths and, at every mark, the fraction of all paths
that have left the band ``|x| ≤ barrier`` by then.

One `random.Random` seeded with the plan's seed draws every shock, step by
step: at each step it draws one shock for each path, in path order. The same
plan on the same model therefore always gives the same run.

This is the only part of the stepped lane in floating point: exact values
become float64 here and nowhere upstream, and nothing returned here reaches a
claim, a goal or a decision. The breach fraction and its Wilson interval are
sampled evidence, shown on a card beside a proved bound and never as one
(semantic invariants 5 and 9).
"""

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from statistics import NormalDist
from typing import Any

from glitnir.notebook.evidence_cards import MAX_SAMPLES

from glitnir_forseti.notebook import evidence
from glitnir_forseti.notebook.evidence import (
    EvidenceError,
    ExactPolynomials,
    SamplePlan,
    check_sample_work,
    read_rational,
    to_float64,
)
from glitnir_forseti.stepped.numbers import three_point

#: The keys Lean prints for a stepped model.
_KEYS = ("states", "initial", "step", "coordinates", "driver", "field")
#: The standard normal's 97.5 % quantile, for a two-sided 95 % interval.
_Z95 = NormalDist().inv_cdf(0.975)
#: Draws ``count`` shocks from a generator.
Shocks = Callable[[random.Random, int], list[float]]
#: One right-hand side in float64: ``(coefficient, ((coordinate, power), …))``
#: per term, without the coordinates of power zero.
_Row = list[tuple[float, tuple[tuple[int, int], ...]]]


@dataclass(frozen=True)
class SamplePayload:
    """A stepped model as Lean printed it for ``sample``, read exactly.

    Attributes:
        step: The step size ``Δ``.
        initial: The initial state, in the order of ``states``.
        field: The right-hand sides, one per state in that order, as exact
            polynomials over the coordinates.
        coordinates: The IDs of the field's variables, in order.
        driver: The coordinate of the model's one driver.
        states: The states' names, their IDs without ``state-``.
        places: Each state's coordinate.
    """

    step: Fraction
    initial: tuple[Fraction, ...]
    field: ExactPolynomials
    coordinates: tuple[str, ...]
    driver: int
    states: tuple[str, ...]
    places: tuple[int, ...]


@dataclass(frozen=True)
class SampleResult:
    """A sampled run, in float64: evidence, never a bound.

    Attributes:
        marks: The marks the series keep: every mark from 0, or, when there
            are more than `MAX_SAMPLES`, every stride-th one and the last.
        paths: The observed state along the first ``show`` paths, one value
            per kept mark.
        breach_series: At each kept mark, the fraction of all paths that have
            left the band at that mark or before; every mark is checked.
        breach_fraction: The fraction at the last mark run.
        interval: The 95 % Wilson interval of ``breach_fraction`` over the
            paths.
        finite: Whether every value stayed finite. A run stops at the first
            mark where one does not, and its series end at the mark before.
    """

    marks: tuple[int, ...]
    paths: tuple[tuple[float, ...], ...]
    breach_series: tuple[float, ...]
    breach_fraction: float
    interval: tuple[float, float]
    finite: bool


def sample_payload(emitted: Any) -> SamplePayload:
    """Read what Lean printed for a stepped model.

    Raises:
        EvidenceError: If a key is missing or malformed, the states, initial
            values and field rows differ in number, the model has not exactly
            one driver coordinate, the coordinates are not that driver and the
            states, or the field cannot be expanded.
    """
    if not isinstance(emitted, dict):
        raise EvidenceError("Lean printed a malformed field")
    missing = [key for key in _KEYS if key not in emitted]
    if missing:
        raise EvidenceError(f"the emitted model has no {missing[0]!r}")
    ids, values, trees = emitted["states"], emitted["initial"], emitted["field"]
    coordinates, driver = emitted["coordinates"], emitted["driver"]
    for name, given in (("states", ids), ("coordinates", coordinates)):
        if type(given) is not list or not all(type(i) is str for i in given):
            raise EvidenceError(f"the {name} are a list of IDs")
    if type(values) is not list or type(trees) is not list:
        raise EvidenceError("the initial state and the field are lists")
    if not ids or not len(ids) == len(values) == len(trees):
        raise EvidenceError(
            f"the emitted model's states ({len(ids)}), initial values "
            f"({len(values)}) and field rows ({len(trees)}) differ in number"
        )
    if driver is None:
        raise EvidenceError(
            "sample shocks a model with exactly one driver coordinate; this "
            "model has none or several"
        )
    if type(driver) is not int or not 0 <= driver < len(coordinates):
        raise EvidenceError("the driver is a coordinate index")
    if coordinates[driver] in ids:
        raise EvidenceError("the driver's coordinate is a state")
    place = {coordinate: index for index, coordinate in enumerate(coordinates)}
    if len(coordinates) != len(ids) + 1 or set(place) != {coordinates[driver], *ids}:
        raise EvidenceError("the coordinates are not the driver and the states")
    names = tuple(i.removeprefix("state-") for i in ids)
    if len(set(names)) != len(names):
        raise EvidenceError("the state names are not distinct")
    step = read_rational(emitted["step"])
    if step <= 0:
        raise EvidenceError("the step must be positive")
    return SamplePayload(
        step=step,
        initial=tuple(read_rational(value) for value in values),
        field=evidence.expression_polynomials(trees, len(coordinates)),
        coordinates=tuple(coordinates),
        driver=driver,
        states=names,
        places=tuple(place[i] for i in ids),
    )


def shocks(law: str, variance: Fraction, barrier: Fraction, step: Fraction) -> Shocks:
    """Return a function that draws shocks of ``law`` from a generator.

    ``gaussian`` is normal with mean 0 and the variance; ``twopoint`` is
    ``±√variance`` with probability 1/2 each; ``threepoint`` is ``+L`` and
    ``−L`` with probability ``ε`` each and 0 otherwise, with ``(L, ε)`` from
    `three_point` at the step and barrier, so that one jump leaves the band.
    The exact values become float64 here.

    Raises:
        EvidenceError: If the law is unknown, a value cannot be run in
            float64, or the three-point law does not exist for the variance.
    """
    if law == "gaussian":
        sigma = math.sqrt(to_float64(variance))

        def gaussian(rng: random.Random, count: int) -> list[float]:
            gauss = rng.gauss
            return [gauss(0.0, sigma) for _ in range(count)]

        return gaussian
    if law == "twopoint":
        root = math.sqrt(to_float64(variance))

        def twopoint(rng: random.Random, count: int) -> list[float]:
            uniform = rng.random
            return [root if uniform() < 0.5 else -root for _ in range(count)]

        return twopoint
    if law == "threepoint":
        try:
            reach, eps = three_point(step, variance, barrier)
        except ValueError as error:
            raise EvidenceError(str(error)) from None
        jump, mass = to_float64(Fraction(reach)), to_float64(eps)

        def threepoint(rng: random.Random, count: int) -> list[float]:
            uniform = rng.random
            return [_three(uniform(), jump, mass) for _ in range(count)]

        return threepoint
    raise EvidenceError(f"unknown law {law!r}")


def _three(u: float, jump: float, mass: float) -> float:
    """Map a uniform draw to ``+jump``, ``−jump`` or 0, ``mass`` each jump."""
    if u < mass:
        return jump
    return -jump if u < 2 * mass else 0.0


def draw(
    law: str, variance: Fraction, barrier: Fraction, step: Fraction, rng: random.Random
) -> float:
    """Draw one shock of ``law`` (see `shocks`, which `sample` uses)."""
    return shocks(law, variance, barrier, step)(rng, 1)[0]


def wilson(count: int, total: int) -> tuple[float, float]:
    """Return the 95 % Wilson score interval of ``count`` hits in ``total`` draws.

    It is sampling confidence about a sampled fraction, never a statement
    about the probability a claim is about.
    """
    fraction = count / total
    square = _Z95 * _Z95
    scale = 1 + square / total
    centre = (fraction + square / (2 * total)) / scale
    spread = fraction * (1 - fraction) / total + square / (4 * total * total)
    half = _Z95 * math.sqrt(spread) / scale
    low = 0.0 if count == 0 else max(0.0, centre - half)
    high = 1.0 if count == total else min(1.0, centre + half)
    return low, high


def sample(payload: SamplePayload, plan: SamplePlan) -> SampleResult:
    """Run the plan's shock paths of the model in float64.

    Every path starts at the initial state and takes ``plan.steps`` steps; a
    path has breached by a mark once ``|x_observe| > barrier`` at that mark or
    an earlier one, the initial mark included. The series keep every mark
    unless there are more than `MAX_SAMPLES`; then they keep every stride-th
    mark, the stride `evidence.integrate` uses, and the last.

    Raises:
        EvidenceError: If ``plan.observe`` is not a state of the model, the
            run would take more than `MAX_WORK` field-term evaluations, a
            value cannot be run in float64, or the law cannot be drawn
            (see `shocks`).
    """
    if plan.observe not in payload.states:
        raise EvidenceError(
            f"observe names {plan.observe!r}, not a state of the model; its "
            f"states are {', '.join(payload.states)}"
        )
    terms = sum(len(output) for output in payload.field.outputs)
    check_sample_work(plan.paths, plan.steps, terms)
    draws = shocks(plan.law, plan.variance, plan.barrier, payload.step)
    field: list[_Row] = [
        [
            (to_float64(coefficient), tuple((i, p) for i, p in enumerate(powers) if p))
            for powers, coefficient in output
        ]
        for output in payload.field.outputs
    ]
    step, barrier = to_float64(payload.step), to_float64(plan.barrier)
    count = plan.paths
    columns = [[0.0] * count for _ in payload.coordinates]
    for place, value in zip(payload.places, payload.initial):
        columns[place] = [to_float64(value)] * count
    observed = payload.places[payload.states.index(plan.observe)]
    rng = random.Random(plan.seed)
    stride = max(1, -(-plan.steps // (MAX_SAMPLES - 1)))
    inside = [i for i, x in enumerate(columns[observed]) if abs(x) <= barrier]
    marks: list[int] = []
    paths: list[list[float]] = [[] for _ in range(min(plan.show, count))]
    breach: list[float] = []

    def record(mark: int) -> None:
        marks.append(mark)
        for path, x in zip(paths, columns[observed]):
            path.append(x)
        breach.append((count - len(inside)) / count)

    record(0)
    finite = True
    for mark in range(1, plan.steps + 1):
        columns[payload.driver] = draws(rng, count)
        try:
            rates = [_rates(output, columns, count) for output in field]
        except OverflowError:
            finite = False
        else:
            moved = [
                [x + step * rate for x, rate in zip(columns[place], row)]
                for place, row in zip(payload.places, rates)
            ]
            finite = all(_finite(column) for column in moved)
        if not finite:
            if marks[-1] != mark - 1:
                record(mark - 1)
            break
        for place, column in zip(payload.places, moved):
            columns[place] = column
        position = columns[observed]
        inside = [i for i in inside if abs(position[i]) <= barrier]
        if mark % stride == 0 or mark == plan.steps:
            record(mark)
    return SampleResult(
        marks=tuple(marks),
        paths=tuple(tuple(path) for path in paths),
        breach_series=tuple(breach),
        breach_fraction=breach[-1],
        interval=wilson(count - len(inside), count),
        finite=finite,
    )


def _rates(output: _Row, columns: list[list[float]], count: int) -> list[float]:
    """Evaluate one right-hand side on every path at once."""
    total = [0.0] * count
    for coefficient, factors in output:
        product: list[float] | None = None
        for coordinate, power in factors:
            column = columns[coordinate]
            part = column if power == 1 else [x**power for x in column]
            product = (
                part if product is None else [a * b for a, b in zip(product, part)]
            )
        if product is None:
            total = [t + coefficient for t in total]
        else:
            total = [t + coefficient * x for t, x in zip(total, product)]
    return total


def _finite(column: list[float]) -> bool:
    """Whether every value is finite; the sum is a quick check of the common case."""
    return math.isfinite(sum(column)) or all(map(math.isfinite, column))
