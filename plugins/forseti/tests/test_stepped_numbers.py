"""Exact numbers of the stepped stochastic lane match the proved constants."""

import time
from fractions import Fraction

import pytest

from glitnir_forseti.stepped.numbers import (
    PROXY_GRID,
    Interrupted,
    bernoulli_lower,
    exp_lower,
    expected_energy,
    fixed_date_bound,
    horizon_bound,
    proxy,
    rho,
    three_point,
)

D = Fraction(1, 256)


def test_horizon_bound_is_a_quarter() -> None:
    """Zero energy, 256 steps at variance 64 give exactly one quarter."""
    assert horizon_bound(Fraction(0), 256, D, Fraction(64), Fraction(1)) == Fraction(
        1, 4
    )


def test_three_point_parameters() -> None:
    """The law parameters and the Bernoulli lower bound (0.1988) are exact."""
    L, eps = three_point(D, Fraction(64), Fraction(1))
    assert L == 257 and eps == Fraction(64, 2 * 257 * 257)
    lower = bernoulli_lower(eps, 256)
    assert lower == Fraction(16384, 82433)
    assert Fraction(19875, 100000) < lower < Fraction(19876, 100000)


def test_three_point_rejects_fat_variance() -> None:
    """A variance that pushes epsilon past one half has no three-point law."""
    with pytest.raises(ValueError):
        three_point(D, Fraction(70000), Fraction(1))


def test_proxy_256_matches_lean() -> None:
    """The recursion reproduces the interval the Lean kernel proved."""
    A = ((1 - D, -D), (D, 1 - D))
    c = proxy(A, (Fraction(1), Fraction(0)), 0, D, Fraction(64), 256)
    assert Fraction(912, 10000) <= c <= Fraction(913, 10000)
    assert proxy(A, (Fraction(1), Fraction(0)), 0, D, Fraction(64), 1) == D * D * 64


def test_exp_lower_matches_lean_constants() -> None:
    """The cubic floor bound clears both thresholds proved in Lean."""
    assert exp_lower(Fraction(547, 100)) >= 200
    assert exp_lower(Fraction(255, 64)) >= 50


def test_fixed_date_bound_under_one_percent() -> None:
    """The year-end barrier crossing is bounded by under one percent."""
    A = ((1 - D, -D), (D, 1 - D))
    c = proxy(A, (Fraction(1), Fraction(0)), 0, D, Fraction(64), 256)
    c_up, r, bound = fixed_date_bound(c, Fraction(1))
    assert c_up == Fraction(18251, 200000) and c <= c_up < c + PROXY_GRID
    assert r == 1 / (2 * c_up) and Fraction(547, 100) <= r
    assert bound == 2 / exp_lower(r) and bound <= Fraction(1, 100)


def test_fixed_date_bound_lowers_r_to_its_cap() -> None:
    """A far barrier's ``r`` is lowered to the cap, which still bounds ``exp``."""
    A = ((1 - D, -D), (D, 1 - D))
    c = proxy(A, (Fraction(1), Fraction(0)), 0, D, Fraction(64), 256)
    c_up, r, bound = fixed_date_bound(c, Fraction(64), cap=256)
    assert c_up == Fraction(18251, 200000) and 64 * 64 / (2 * c_up) > 257
    assert r == 256 and bound == 2 / exp_lower(Fraction(256))
    # At the cap itself nothing is lowered.
    assert fixed_date_bound(c, Fraction(1), cap=5)[1] == 1 / (2 * c_up)


def test_the_long_recursions_stop_when_asked() -> None:
    """``proxy`` and ``expected_energy`` poll ``stop`` at every step."""
    A = ((1 - D, -D), (D, 1 - D))
    asked = []

    def stop() -> bool:
        asked.append(None)
        return len(asked) > 3

    started = time.monotonic()
    with pytest.raises(Interrupted):
        proxy(A, (Fraction(1), Fraction(0)), 0, D, Fraction(64), 4096, stop=stop)
    assert len(asked) == 4
    with pytest.raises(Interrupted):
        expected_energy(Fraction(0), rho(D), D, Fraction(64), 4096, stop=lambda: True)
    assert time.monotonic() - started < 0.5
    # Not asked to stop, they run to the end.
    assert (
        proxy(A, (Fraction(1), Fraction(0)), 0, D, Fraction(64), 2, stop=lambda: False)
        > 0
    )


def test_expected_energy_matches_table() -> None:
    """The exact energy after 256 steps and its stationary value are tabulated."""
    e = expected_energy(Fraction(0), rho(D), D, Fraction(64), 256)
    assert Fraction(1085, 10000) < e < Fraction(1086, 10000)
    assert Fraction(1, 1024) / (1 - rho(D)) == Fraction(32, 255)
