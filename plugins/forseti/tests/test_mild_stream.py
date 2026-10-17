"""Exact search arithmetic for the physical mild-stream certificates."""

from fractions import Fraction as F

import pytest

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.streams.goal import Modes
from glitnir_forseti.streams.mild import (
    MildEvidence,
    band_bound,
    candidates,
    choose_band,
    choose_truncation,
    tail_bound,
)

START = Modes(
    (
        (1, 0, F(1, 2)),
        (-1, 0, F(1, 2)),
        (1, 1, F(1, 2)),
        (-1, -1, F(1, 2)),
        (2, 1, F(1, 2)),
        (-2, -1, F(1, 2)),
    )
)
LIMITS = EntailmentLimits()


@pytest.mark.parametrize("kind", ["mild-truncation", "mild-band"])
def test_forward_time_goal_round_trip(kind: str) -> None:
    """The public syntax and strict JSON preserve physical time and claim kind."""
    from glitnir_forseti.claims.text import parse_goal, render_goal
    from glitnir_forseti.streams.goal import decode_goal, encode_goal
    from glitnir_forseti.streams.replay import supported

    claim = (
        "truncation u window 4 time 1/5760 epsilon 21/1024"
        if kind == "mild-truncation"
        else "band u time 1/5760 : u >= -4 and u <= 4"
    )
    text = (
        f"stream vorticity viscosity 1/10 interpretation {kind} basis interaction "
        f"start modes [1, 0, 1/2], [-1, 0, 1/2] {claim}"
    )
    goal = parse_goal(text)
    assert parse_goal(render_goal(goal)) == goal
    encoded = encode_goal(goal)
    assert encoded["claim"]["time"] == [1, 5760]
    assert decode_goal(encoded, LIMITS) == goal
    assert not isinstance(supported(goal), str)


def test_exact_catalan_tail_retains_prefactor() -> None:
    """The kernel's four-term tail is sharper than the coarse geometric tail."""
    evidence = MildEvidence("mild-contraction", 3, F(3), 4, F(1, 24))
    assert tail_bound(evidence, F(1, 5760)) == F(21, 1024)
    assert band_bound(evidence, F(1, 5760)) <= F(3519, 1000)


def test_both_candidates_and_better_tail() -> None:
    """At ν=1/10 the Euler-scale bound improves the same four-term claim."""
    found = candidates(START, F(1, 10), F(1, 5760), 4, LIMITS)
    assert {item.kind for item in found} == {"mild-scale", "mild-contraction"}
    chosen = choose_truncation(START, F(1, 10), F(1, 5760), 4, F(21, 1024), LIMITS)
    assert isinstance(chosen, MildEvidence)
    assert chosen.kind == "mild-scale"
    assert tail_bound(chosen, F(1, 5760)) < F(21, 1024)


def test_positive_viscosity_can_extend_past_euler_scale() -> None:
    """At ν=1 the contraction certifies a time outside the Euler interval."""
    found = candidates(START, F(1), F(1, 576), 4, LIMITS)
    assert len(found) == 1
    assert found[0].kind == "mild-contraction"
    assert found[0].radius == F(1, 24)


def test_zero_viscosity_uses_only_scale() -> None:
    """No positive-viscosity contraction is silently used for Euler."""
    found = candidates(START, F(0), F(1, 1296), 4, LIMITS)
    assert len(found) == 1 and found[0].kind == "mild-scale"
    assert tail_bound(found[0], F(1, 1296)) == F(9, 8)


def test_neither_strict_endpoint_is_certified() -> None:
    """Equality at either geometric endpoint is not convergence."""
    assert not candidates(START, F(0), F(1, 648), 4, LIMITS)
    assert not candidates(START, F(1), F(1, 144), 4, LIMITS)


def test_unknown_is_not_refutation() -> None:
    """Too-small errors and bands produce reasons, never counterexamples."""
    assert isinstance(
        choose_truncation(START, F(1, 10), F(1, 5760), 4, F(0), LIMITS), str
    )
    assert isinstance(
        choose_band(START, F(1, 10), F(1, 5760), F(-1), F(1), LIMITS), str
    )


def test_zero_data_cancellation_has_no_time_restriction() -> None:
    """The aggregate zero table admits arbitrary horizons with exact zero error."""
    zero = Modes(((0, 0, F(1)), (0, 0, F(-1))))
    found = choose_truncation(zero, F(0), F(1000000), 4, F(0), LIMITS)
    assert isinstance(found, MildEvidence)
    assert found.weight == 0 and tail_bound(found, F(1000000)) == 0
    band = choose_band(zero, F(0), F(1000000), F(0), F(0), LIMITS)
    assert isinstance(band, MildEvidence) and band_bound(band, F(1000000)) == 0


def test_irrational_heat_radius_is_rounded_up_exactly() -> None:
    """Rational search always verifies the squared heat condition."""
    found = candidates(START, F(1), F(1, 1000), 4, LIMITS)
    contraction = next(item for item in found if item.kind == "mild-contraction")
    assert contraction.radius is not None
    assert contraction.radius**2 >= F(1, 1000)
    assert 4 * contraction.radius * contraction.weight < 1


def test_exact_carrier_matches_kernel_checked_first_terms() -> None:
    """Independent table coefficients match Asgard's exact degree 0–3 examples."""
    from glitnir_forseti.streams.mild_carrier import exact_terms

    terms = exact_terms(START, F(1, 10), 3, LIMITS)
    assert [(len(term), sum(map(len, term.values()))) for term in terms] == [
        (6, 6),
        (12, 24),
        (28, 112),
        (52, 382),
    ]
    assert terms[0][(1, 0)] == {(1, 0): F(1, 2)}
    assert terms[1][(0, 1)] == {(1, 0): F(5, 8), (3, 0): F(-5, 8)}
    assert terms[2][(2, 2)] == {
        (8, 0): F(-35, 13),
        (14, 0): F(5, 26),
        (8, 1): F(-5, 13),
        (6, 0): F(5, 2),
    }
    assert terms[3][(1, 0)][(1, 0)] == F(-5247629, 15375360)


@pytest.mark.parametrize("viscosity", [F(0), F(1, 10)])
def test_duhamel_resonance_and_exact_differential_identity(viscosity: F) -> None:
    """Duhamel solves its ODE and starts at zero, including resonance and ν=0."""
    from glitnir_forseti.streams.mild_carrier import derivative, duhamel

    forcing = {(3, 2): F(2, 7), (8, 0): F(-3, 5)}
    result = duhamel(forcing, 3, viscosity, LIMITS)
    recovered = derivative(result, viscosity)
    for key, coefficient in result.items():
        recovered[key] = recovered.get(key, F(0)) + 3 * viscosity * coefficient
    assert {key: value for key, value in recovered.items() if value} == forcing
    assert sum(value for (_, power), value in result.items() if power == 0) == 0


def test_carrier_is_bounded_and_cooperatively_cancellable() -> None:
    """Report-only arithmetic cannot run unbounded while a decision is cancelled."""
    import threading

    from glitnir_forseti.streams.mild_carrier import exact_terms
    from glitnir_forseti.streams.trig import SearchStopped

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(SearchStopped, match="cancelled"):
        exact_terms(START, F(1, 10), 3, LIMITS, cancel=cancelled)
    with pytest.raises(ValueError, match="degree"):
        exact_terms(START, F(1, 10), 10, LIMITS)


def _goal(*, band: bool = False):
    """Build the public three-mode forward-time goal used by the replay regressions."""
    from glitnir_forseti.claims.text import parse_goal

    claim = (
        "band u time 1/5760 : u >= -3519/1000 and u <= 3519/1000"
        if band
        else "truncation u window 4 time 1/5760 epsilon 21/1024"
    )
    return parse_goal(
        "stream vorticity viscosity 1/10 interpretation"
        f" mild-{'band' if band else 'truncation'} basis interaction"
        " start modes [1, 0, 1/2],"
        f" [-1, 0, 1/2], [1, 1, 1/2], [-1, -1, 1/2], [2, 1, 1/2], [-2, -1, 1/2] {claim}"
    )


@pytest.mark.parametrize("basis", ["ogf", "egf"])
def test_mild_rejects_taylor_basis_relabelling(basis: str) -> None:
    """Mild interaction degree has its own explicit public basis."""
    from dataclasses import replace

    from glitnir_forseti.streams import replay

    goal = _goal()
    assert not isinstance(replay.supported(goal), str)
    assert "interaction basis" in replay.supported(replace(goal, basis=basis))


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "heat"),
        ("viscosity", F(-1)),
        ("interpretation", "trig-truncation"),
        ("profile", Modes(((0, 0, F(1)),))),
        ("profile", Modes(((1, 0, F(1)),))),
    ],
)
def test_mild_admission_is_physical(field, value) -> None:
    """Unrelated models and nonphysical starts never enter this proof lane."""
    from dataclasses import replace

    from glitnir_forseti.streams.replay import supported

    assert isinstance(supported(replace(_goal(), **{field: value})), str)


@pytest.mark.parametrize(
    "field,value",
    [
        ("size", True),
        ("size", 1),
        ("weight", [1, 1]),
        ("window", 0),
        ("window", 3),
        ("window", True),
        ("extra", 0),
        ("kind", "euler-radius"),
        ("radius", [1, 24]),
    ],
)
def test_strict_mild_evidence_decoder(field, value) -> None:
    """Evidence cannot smuggle another window, constants, kind or surplus field."""
    from glitnir_forseti.entailment.evidence import EvidenceError
    from glitnir_forseti.streams import replay
    from glitnir_forseti.streams.models import VORTICITY

    goal = _goal()
    evidence = MildEvidence("mild-scale", 3, F(3), 4)
    document = replay._encode_evidence(evidence, goal, VORTICITY)
    assert replay._decode_evidence(document, goal, VORTICITY, LIMITS) == evidence
    document[field] = value
    with pytest.raises(EvidenceError):
        replay._decode_evidence(document, goal, VORTICITY, LIMITS)


@pytest.mark.parametrize("band", [False, True])
def test_decider_keeps_carrier_evidence_out_of_certificate(band: bool) -> None:
    """The diagnostic table is visible, while root data contain only bound premises."""
    from glitnir_forseti.notebook.readable import readable
    from glitnir_forseti.streams import mild_render, replay, service
    from glitnir_forseti.streams.models import VORTICITY

    goal = _goal(band=band)
    outcome, found = service._mild(goal, LIMITS)
    assert outcome.outcome == "found" and isinstance(found, MildEvidence)
    assert "Carrier report evidence only" in outcome.detail
    document = replay._encode_evidence(found, goal, VORTICITY)
    assert set(document) <= {"kind", "size", "weight", "window", "radius"}
    app = mild_render.prove(goal, found)
    assert "Gimle.Forseti.MildVorticity.Certified" in app.prelude
    shown = readable(goal)
    assert shown and "dissipation" in shown.description
    assert "interaction" in shown.description
    assert "input 0 0" in shown.statement


def test_zero_window_is_explicitly_unsupported() -> None:
    """A zero window remains a readable goal, but this lane proves N≥1 only."""
    from dataclasses import replace

    from glitnir_forseti.streams import service

    goal = _goal()
    goal = replace(goal, claim=replace(goal.claim, window=0))
    result = service.decide_stream(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED


def test_both_certificate_routes_target_the_identical_literal_root() -> None:
    """Choosing a majorant may change the proof but never the user's theorem."""
    from glitnir_forseti.streams import mild_render

    scale = mild_render.prove(_goal(), MildEvidence("mild-scale", 3, F(3), 4))
    contraction = mild_render.prove(
        _goal(), MildEvidence("mild-contraction", 3, F(3), 4, F(1, 24))
    )
    assert scale.prelude == contraction.prelude
    assert scale.statement == contraction.statement
    assert scale.script != contraction.script


def test_one_expensive_certificate_does_not_discard_a_feasible_other_route() -> None:
    """An expensive heat-radius power must not discard a feasible scale proof."""
    found = choose_truncation(START, F(1), F(1, 1000), 12, F(10), LIMITS)
    assert isinstance(found, MildEvidence) and found.kind == "mild-scale"
    assert tail_bound(found, F(1, 1000)) < 10


def test_radius_search_limit_keeps_scale_candidate() -> None:
    """The optional bisection can exceed a small budget without losing scale."""
    from dataclasses import replace

    limits = replace(LIMITS, max_coefficient_bits=32)
    found = candidates(START, F(1), F(1, 1000), 1, limits)
    assert [item.kind for item in found] == ["mild-scale"]
    assert isinstance(
        choose_band(START, F(1), F(1, 1000), F(-30), F(30), limits), MildEvidence
    )


def test_all_certificate_bounds_over_budget_stop_search() -> None:
    """Exhausting both routes remains a structured resource limit."""
    from glitnir_forseti.streams.trig import SearchStopped

    with pytest.raises(SearchStopped) as caught:
        choose_truncation(START, F(1), F(1, 1000), 64, F(10), LIMITS)
    assert caught.value.outcome == "resource-limit"


@pytest.mark.parametrize("outcome", ["cancelled", "timeout"])
def test_interruption_after_scale_candidate_is_not_swallowed(
    monkeypatch, outcome
) -> None:
    """A partial result must not turn explicit interruption into success."""
    from glitnir_forseti.streams import mild
    from glitnir_forseti.streams.trig import SearchStopped

    def stop(*args):
        raise SearchStopped(outcome, "interrupted during contraction search")

    monkeypatch.setattr(mild, "_heat_radius", stop)
    with pytest.raises(SearchStopped) as caught:
        choose_truncation(START, F(1), F(1, 1000), 1, F(10), LIMITS)
    assert caught.value.outcome == outcome
