"""Cross-check the stepped sampler against Asgard's Euler–Maruyama.

The sampler steps the hedged book ``ds = (−s − h) + d``, ``dh = s − h`` with
``Δ = 1/256`` under Gaussian shocks ``d`` of variance 64, so ``Δ·d`` is
``N(0, 1/1024)``: that is the Euler–Maruyama scheme of the SDE
``ds = (−s − h) dt + σ dW``, ``dh = (s − h) dt`` with ``σ = 1/2`` and
``dt = Δ``. Asgard runs that SDE on its own: two ``sde()`` equations combined
by `build_coupled_sde_circuit` and stepped by its coupled circuit-driven
executor, the route its YAML runner takes, under an Itô `StochasticCalculus`.

The two draw from different generators, so only the ensembles are compared:
the mean of ``s² + h²`` at the marks 64, 128 and 256 (``t`` = 1/4, 1/2, 1),
never a path. The hedged book's payload is the one `test_stepped_evidence`
reads. The comparison is marked ``simulation``: it skips where `gimle.asgard`
is not installed.
"""

import math
import statistics
from fractions import Fraction

import pytest
from test_stepped_evidence import HEDGED_PAYLOAD

from glitnir_forseti.notebook.evidence import sample_plan
from glitnir_forseti.stepped.sampler import ensemble

PATHS = 2000
MARKS = (64, 128, 256)
SIGMA = Fraction(1, 2)
VARIANCE = Fraction(64)


def _plugin(seed: int) -> dict[int, list[float]]:
    """Each path's ``s² + h²`` at each mark, from the plugin's sampler."""
    run = sample_plan(
        {
            "law": "gaussian",
            "variance": str(VARIANCE),
            "observe": "s",
            "paths": str(PATHS),
            "steps": str(MARKS[-1]),
            "seed": str(seed),
        }
    )
    return {
        mark: [a * a + b * b for a, b in zip(s, h, strict=True)]
        for mark, (s, h) in ensemble(HEDGED_PAYLOAD, run)
        if mark in MARKS
    }


def _asgard(seed: int) -> dict[int, list[float]]:
    """Each path's ``s² + h²`` at each mark, from Asgard's Euler–Maruyama.

    ``h`` takes no noise; its zero diffusion is a parameter, as Asgard's
    Brownian-motion example does, so that it broadcasts across the paths.
    """
    import jax.numpy as jnp
    import numpy as np
    from gimle.asgard.compile.coupled_sde import build_coupled_sde_circuit
    from gimle.asgard.runtime.stochastic.calculus import StochasticCalculus
    from gimle.asgard.runtime.stochastic_circuit.executor import (
        StochasticCircuitExecutor,
    )

    circuit, names, labels = build_coupled_sde_circuit(
        [
            {"name": "s", "equation": "Y = sde(-s - $h, $sigma, s)"},
            {"name": "h", "equation": "Y = sde($s - h, $zero, h)"},
        ]
    )
    assert names == ["s", "h"]
    executor = StochasticCircuitExecutor(
        circuit,
        var_values={"sigma": jnp.array([float(SIGMA)]), "zero": jnp.array([0.0])},
    )
    dt = float(HEDGED_PAYLOAD.step)
    calculus = StochasticCalculus(
        n_paths=PATHS,
        dt=dt,
        seed=seed,
        interpretation="ito",
        method="euler_maruyama",
    )
    result = executor.execute_coupled_circuit_driven_result(
        calculus=calculus,
        t_start=0.0,
        t_end=MARKS[-1] * dt,
        x0_vector=jnp.array([float(x) for x in HEDGED_PAYLOAD.initial]),
        dim_labels=labels,
        names=names,
    )
    paths = np.asarray(result.paths, dtype=np.float64)
    assert paths.shape == (2, PATHS, MARKS[-1] + 1)
    energies = {}
    for mark in MARKS:
        assert float(result.times[mark]) == mark * dt
        s, h = paths[0, :, mark], paths[1, :, mark]
        energies[mark] = (s * s + h * h).tolist()
    return energies


def _mean_and_error(energy: list[float]) -> tuple[float, float]:
    """The mean of the per-path energies and its standard error."""
    return statistics.fmean(energy), statistics.stdev(energy) / math.sqrt(len(energy))


def test_both_steppers_run_the_same_model() -> None:
    """Asgard's ``σ² dt`` is the sampler's ``Δ²·v``, from the same flat start."""
    step = HEDGED_PAYLOAD.step
    assert SIGMA * SIGMA * step == step * step * VARIANCE == Fraction(1, 1024)
    assert HEDGED_PAYLOAD.initial == (0, 0)


# Rests on pinned seeds and JAX's PRNG streams: seed 2, mark 128 is at 2.78 of 3 se.
@pytest.mark.simulation
@pytest.mark.parametrize("seed", [1, 2])
def test_the_sampler_agrees_with_asgard_on_the_mean_energy(seed: int) -> None:
    plugin, asgard = _plugin(seed), _asgard(seed)
    for mark in MARKS:
        ours, our_error = _mean_and_error(plugin[mark])
        theirs, their_error = _mean_and_error(asgard[mark])
        tolerance = 3 * math.hypot(our_error, their_error)
        assert abs(ours - theirs) <= tolerance, (mark, ours, theirs, tolerance)
