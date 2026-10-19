"""Exact rational arithmetic behind the stepped stochastic lane.

Every function takes and returns `fractions.Fraction` (or `int` for counts);
nothing here touches floating point, so a figure is the very number the
corresponding theorem states. The module computes:

* the horizon bound ``(V₀ + N·Δ²·v) / B²`` on the chance of crossing a barrier
  ``B`` within ``N`` steps of size ``Δ`` under shock variance ``v``;
* the parameters of the three-point law that attains that variance, and the
  Bernoulli lower bound ``2εN / (1 + 2εN)`` on its crossing chance;
* the variance proxy ``Δ²·v·Σ_{k<N} ((A^k B)_c)²`` of a linear recursion;
* the rational lower bound ``E(r)`` on ``exp(r)`` and the fixed-date bound
  built from it;
* the exact expected energy ``ρ^N·V₀ + Δ²·v·Σ_{k<N} ρ^k`` and its factor
  ``ρ = 1 − 2Δ + 2Δ²``.
"""

from fractions import Fraction

#: A rational lower bound on Euler's number, ``e ≥ 2.7182818283``.
E_LOWER = Fraction(27182818283, 10**10)
#: The proxy is rounded up to a multiple of this before it is inverted.
PROXY_GRID = Fraction(1, 10**6)


def _floor(value: Fraction) -> int:
    """Return the greatest integer not above ``value``."""
    return value // 1


def _ceil(value: Fraction) -> int:
    """Return the least integer not below ``value``."""
    return -((-value) // 1)


def _require_positive(name: str, value: Fraction) -> None:
    """Raise `ValueError` unless ``value`` is strictly positive."""
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


def _require_steps(steps: int) -> None:
    """Raise `ValueError` unless ``steps`` is a non-negative integer."""
    if steps < 0:
        raise ValueError(f"steps must not be negative, got {steps}")


def horizon_bound(
    initial_energy: Fraction,
    steps: int,
    step: Fraction,
    variance: Fraction,
    barrier: Fraction,
) -> Fraction:
    """Return ``(V₀ + N·Δ²·v) / B²``, the horizon bound on crossing ``B``."""
    _require_steps(steps)
    _require_positive("barrier", barrier)
    return (initial_energy + steps * step * step * variance) / (barrier * barrier)


def three_point(
    step: Fraction, variance: Fraction, barrier: Fraction
) -> tuple[int, Fraction]:
    """Return ``(L, ε)`` of the three-point law with ``L = ⌊B/Δ⌋ + 1``, ``ε = v/(2L²)``.

    The law puts mass ``ε`` on each of ``±L`` and the rest on zero, so it needs
    ``ε ≤ 1/2``; a variance too large for that raises `ValueError`.
    """
    _require_positive("step", step)
    _require_positive("barrier", barrier)
    if variance < 0:
        raise ValueError(f"variance must not be negative, got {variance}")
    reach = _floor(barrier / step) + 1
    eps = variance / (2 * reach * reach)
    if eps > Fraction(1, 2):
        raise ValueError(
            f"no three-point law: epsilon {eps} exceeds 1/2 for variance "
            f"{variance} at reach {reach}"
        )
    return reach, eps


def bernoulli_lower(eps: Fraction, steps: int) -> Fraction:
    """Return ``2εN / (1 + 2εN)``, the Bernoulli lower bound on crossing."""
    _require_steps(steps)
    if eps < 0:
        raise ValueError(f"epsilon must not be negative, got {eps}")
    mean = 2 * eps * steps
    return mean / (1 + mean)


def proxy(
    matrix: tuple[tuple[Fraction, ...], ...],
    shock: tuple[Fraction, ...],
    component: int,
    step: Fraction,
    variance: Fraction,
    steps: int,
) -> Fraction:
    """Return ``Δ²·v·Σ_{k<N} ((A^k B)_component)²`` by the ``(state, acc)`` recursion.

    The state starts at the shock vector ``B``. Each of the ``N`` rounds first
    adds ``state[component]²`` to the accumulator and then replaces the state
    by ``A·state``, so after ``N`` rounds the accumulator covers ``k < N``.
    """
    _require_steps(steps)
    size = len(shock)
    if len(matrix) != size or any(len(row) != size for row in matrix):
        raise ValueError(
            f"matrix must be {size}x{size} to act on a shock of size {size}"
        )
    if not 0 <= component < size:
        raise ValueError(f"component {component} is outside 0..{size - 1}")
    state = shock
    acc = Fraction(0)
    for _ in range(steps):
        acc += state[component] * state[component]
        state = tuple(
            sum((a * s for a, s in zip(row, state)), Fraction(0)) for row in matrix
        )
    return step * step * variance * acc


def exp_lower(r: Fraction) -> Fraction:
    """Return ``E(r) = e₋^⌊r⌋ · (1 + f + f²/2 + f³/6)`` with ``f = r − ⌊r⌋``.

    ``e₋ = 27182818283/10¹⁰`` is below Euler's number and the cubic is below
    ``exp(f)``, so ``E(r) ≤ exp(r)`` for every ``r ≥ 0``.
    """
    if r < 0:
        raise ValueError(f"r must not be negative, got {r}")
    whole = _floor(r)
    f = r - whole
    return E_LOWER**whole * (1 + f + f * f / 2 + f * f * f / 6)


def fixed_date_bound(
    proxy_value: Fraction, barrier: Fraction
) -> tuple[Fraction, Fraction]:
    """Return ``(r, bound)`` with ``r = B²/(2·c_up)`` and ``bound = 2/E(r)``.

    ``c_up`` is the proxy rounded up to a multiple of ``1/10⁶``, so ``r`` errs
    low and the bound errs high.
    """
    _require_positive("proxy", proxy_value)
    _require_positive("barrier", barrier)
    c_up = _ceil(proxy_value / PROXY_GRID) * PROXY_GRID
    r = barrier * barrier / (2 * c_up)
    return r, 2 / exp_lower(r)


def expected_energy(
    initial_energy: Fraction,
    rho: Fraction,
    step: Fraction,
    variance: Fraction,
    steps: int,
) -> Fraction:
    """Return ``ρ^N·V₀ + Δ²·v·Σ_{k<N} ρ^k`` exactly."""
    _require_steps(steps)
    power = Fraction(1)
    total = Fraction(0)
    for _ in range(steps):
        total += power
        power *= rho
    return power * initial_energy + step * step * variance * total


def rho(step: Fraction) -> Fraction:
    """Return the contraction factor ``1 − 2Δ + 2Δ²``."""
    return 1 - 2 * step + 2 * step * step
