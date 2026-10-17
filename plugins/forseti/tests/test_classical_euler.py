"""Classical Euler readings bind PDE guarantees to literal circuit goals."""

from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.claims.text import parse_goal, render_goal
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.notebook.readable import readable
from glitnir_forseti.streams import render, replay, service, trig
from glitnir_forseti.streams.examples import trig_band, trig_truncation, vorticity_goal
from glitnir_forseti.streams.goal import Modes, decode_goal, encode_goal
from glitnir_forseti.streams.models import VORTICITY


@pytest.mark.parametrize("band", [False, True])
def test_classical_reading_is_explicit_and_preserves_the_bound(band: bool) -> None:
    """Old and new readings share data but only the explicit new one claims PDEs."""
    ordinary = vorticity_goal(
        trig_band(Fraction(-4), Fraction(4))
        if band
        else trig_truncation(Fraction(1, 100))
    )
    goal = replace(
        ordinary, interpretation="euler-band" if band else "euler-truncation"
    )
    assert replay.supported(goal) == VORTICITY
    assert decode_goal(encode_goal(goal), EntailmentLimits()) == goal
    assert parse_goal(render_goal(goal)) == goal
    stated, old = readable(goal), readable(ordinary)
    assert stated is not None and old is not None
    assert "Gimle.Forseti.Euler.Certified" in stated.statement
    assert "Gimle.Forseti.Euler.Certified" not in old.statement
    assert (
        "classical" in stated.description
        and "does not assert" not in stated.description
    )
    assert "input 0 0" in stated.statement
    assert replay.imports_for(goal, VORTICITY) != replay.imports_for(
        ordinary, VORTICITY
    )


@pytest.mark.parametrize(
    ("terms", "reason"),
    [(((0, 0, Fraction(1)),), "mean-zero"), (((1, 0, Fraction(1)),), "even")],
)
def test_physical_premises_are_checked_without_narrowing_old_lanes(
    terms, reason
) -> None:
    """Unsuitable initial data cannot acquire a classical guarantee."""
    ordinary = replace(
        vorticity_goal(trig_truncation(Fraction(1))), profile=Modes(terms)
    )
    classical = replace(ordinary, interpretation="euler-truncation")
    assert reason in replay.supported(classical)
    if terms[0][:2] != (0, 0):
        assert replay.supported(ordinary) == VORTICITY


@pytest.mark.parametrize("mode", [(0, 0), (4, -2)])
def test_zero_data_support_large_radius_but_still_check_the_error(mode) -> None:
    """A zero solution does not make a negative error bound true."""
    ordinary = vorticity_goal(trig_truncation(Fraction(0)))
    goal = replace(
        ordinary,
        interpretation="euler-truncation",
        profile=Modes(((mode[0], mode[1], Fraction(0)),)),
        claim=replace(ordinary.claim, radius=Fraction(1000000)),
    )
    assert replay.supported(goal) == VORTICITY
    outcome, evidence = service._trig_truncation(goal, EntailmentLimits())
    assert outcome.outcome == "found" and isinstance(evidence, trig.RadiusEvidence)
    source = render.prove_trig_truncation(goal, evidence)
    assert "Gimle.Forseti.Euler.Certified" in source.prelude
    outcome, evidence = service._trig_truncation(
        replace(goal, claim=replace(goal.claim, epsilon=Fraction(-1))),
        EntailmentLimits(),
    )
    assert outcome.outcome == "none" and evidence is None


@pytest.mark.parametrize(
    "radius,found",
    [(Fraction(0), True), (Fraction(1, 649), True), (Fraction(1, 648), False)],
)
def test_classical_radius_is_strictly_inside_the_geometric_radius(
    radius, found
) -> None:
    """Closed requested endpoints must remain inside the open analytic radius."""
    goal = replace(
        vorticity_goal(trig_truncation(Fraction(10000))),
        interpretation="euler-truncation",
    )
    goal = replace(goal, claim=replace(goal.claim, radius=radius))
    outcome, evidence = service._trig_truncation(goal, EntailmentLimits())
    assert (outcome.outcome == "found") == found
    assert (evidence is not None) == found


@pytest.mark.parametrize("viscosity", [Fraction(-1), Fraction(1, 10)])
def test_classical_euler_rejects_nonzero_viscosity(viscosity) -> None:
    """Changing viscosity cannot retain an Euler certificate."""
    goal = replace(
        vorticity_goal(trig_truncation(Fraction(1))),
        interpretation="euler-truncation",
        viscosity=viscosity,
    )
    assert isinstance(replay.supported(goal), str)


def test_zero_band_still_needs_to_contain_zero() -> None:
    """Zero data remove the radius restriction, never the requested postcondition."""
    goal = replace(
        vorticity_goal(trig_band(Fraction(1), Fraction(2))),
        interpretation="euler-band",
        profile=Modes(((0, 0, Fraction(0)),)),
    )
    outcome, evidence = service._trig_band(goal, EntailmentLimits())
    assert outcome.outcome == "none" and evidence is None


@pytest.mark.parametrize("change", ["basis", "reading", "port"])
def test_classical_reading_keeps_the_typed_lane_restrictions(change: str) -> None:
    """A stronger postcondition does not widen the model's supported interface."""
    goal = replace(
        vorticity_goal(trig_truncation(Fraction(1))),
        interpretation="euler-truncation",
    )
    if change == "basis":
        goal = replace(goal, basis="egf")
    elif change == "reading":
        goal = replace(goal, interpretation="euler-band")
    else:
        goal = replace(goal, claim=replace(goal.claim, port="other"))
    assert isinstance(replay.supported(goal), str)


def test_explicit_zero_modes_do_not_require_nonexistent_opposites() -> None:
    """Symmetry concerns coefficients, including modes absent from the list."""
    goal = vorticity_goal(trig_truncation(Fraction(1)))
    goal = replace(
        goal,
        interpretation="euler-truncation",
        profile=Modes((*goal.profile.terms, (7, 4, Fraction(0)))),
    )
    assert replay.supported(goal) == VORTICITY


def test_multimode_root_has_literal_data_and_no_producer_window() -> None:
    """Different search windows prove the same literal circuit judgment."""
    start = Modes(
        (
            (1, 0, Fraction(1, 2)),
            (-1, 0, Fraction(1, 2)),
            (0, 2, Fraction(1, 4)),
            (0, -2, Fraction(1, 4)),
        )
    )
    goal = replace(
        vorticity_goal(trig_band(Fraction(-2), Fraction(2))),
        interpretation="euler-band",
        profile=start,
    )
    goal = replace(goal, claim=replace(goal.claim, radius=Fraction(1, 2160)))
    assert replay.supported(goal) == VORTICITY
    assert trig.geometric(start) == (Fraction(9, 2), Fraction(216))
    first = trig.BandEvidence(2, Fraction(3, 2), 1, trig.picard_norms(start, 1))
    second = replace(first, window=2, norms=trig.picard_norms(start, 2))
    a, b = render.prove_trig_band(goal, first), render.prove_trig_band(goal, second)
    # The producer's auxiliary bound differs, but the entire root definition does not.
    assert (
        a.prelude.split("def forsetiBound")[0] == b.prelude.split("def forsetiBound")[0]
    )
    assert "NS.vorticityCircuit .ogf" in a.prelude
    assert "input 0 0 = forsetiStart 0" in a.prelude
    assert render.modes_table(start) in a.prelude


@pytest.mark.parametrize("band", [False, True])
def test_cli_reports_the_complete_classical_contract(band: bool) -> None:
    """The command-line scope must match the stronger explicit Euler reading."""
    from glitnir_forseti.streams.__main__ import _solution_class

    goal = vorticity_goal(
        (
            trig_band(Fraction(-4), Fraction(4))
            if band
            else trig_truncation(Fraction(1, 100))
        ),
        interpretation="euler-band" if band else "euler-truncation",
    )
    report = _solution_class(goal)
    assert "total circuit contract" in report
    assert "classical vorticity equation" in report
    assert "checks evenness and zero mean" in report
    assert "only the bound" not in report
    assert "does not check" not in report
