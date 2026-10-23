"""Tests for stream and field root goals that need no Lean (112, lanes 2–3).

Goal encoding, support decisions, rendering, leaf construction, the replay's
binding checks and the service's reasons run here; Lean's checks of the root
theorems are in ``test_streams_lean.py``.
"""

import json
import threading
import time
from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.entailment.evidence import (
    Certificate,
    Row,
    SquareSum,
    Witness,
    nonstrict,
)
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import InvalidEvidence, ReplayUnavailable
from glitnir_forseti.entailment.search import LaneOutcome
from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    GoalError,
    Mul,
    Neg,
    Or,
    Relation,
    Tru,
    Var,
)
from glitnir_forseti.lean.closure import closure_identity, replay_pin
from glitnir_forseti.lean.independent import IndependentCheck, IndependentStatus
from glitnir_forseti.streams import render, replay, service, tail, trig
from glitnir_forseti.streams.examples import (
    COSH,
    EULER_RADIUS,
    EULER_START,
    EXAMPLES,
    EXP,
    FRONT,
    NO_FRONT,
    OBSERVED,
    band,
    band_goal,
    burgers_goal,
    field_claim,
    front_band,
    front_truncation,
    heat_goal,
    trig_band,
    trig_truncation,
    truncation,
    vorticity_goal,
)
from glitnir_forseti.streams.goal import (
    CoefficientClaim,
    ExpSum,
    FieldClaim,
    FullStreamClaim,
    Modes,
    StreamGoal,
    TrigBandClaim,
    TrigTruncationClaim,
    TruncationClaim,
    band_of,
    decode_goal,
    encode_goal,
)
from glitnir_forseti.streams.models import BURGERS, HEAT, VORTICITY, replay_imports

LIMITS = EntailmentLimits()
TAIL_CLAIM = truncation(Fraction(1, 1000))
EXP_TAIL = heat_goal(TAIL_CLAIM, interpretation="analytic-tail", profile=EXP)
FRONT_CLAIM = front_truncation(Fraction(1, 10000))
FRONT_TAIL = burgers_goal(FRONT_CLAIM)
FRONT_RADII = (Fraction(2, 3), Fraction(1, 2))
BY_NAME = {example.name: example.goal for example in EXAMPLES}
FIELD = BY_NAME["field-band"]
COEFFICIENTS = BY_NAME["coefficients"]
ONE = Const(Fraction(1))


def _certificate(rows: int, slots: int) -> Certificate:
    return Certificate(
        tuple(
            Row(
                SquareSum(((Fraction(1), ONE),)),
                tuple(SquareSum(()) for _ in range(slots)),
            )
            for _ in range(rows)
        )
    )


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_goals_round_trip(example) -> None:
    document = json.loads(json.dumps(encode_goal(example.goal)))
    assert decode_goal(document, LIMITS) == example.goal


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d.update(extra=1), "fields"),
        (lambda d: d.update(basis="lgf"), "basis"),
        (lambda d: d.update(interpretation="numeric"), "interpretation"),
        (lambda d: d["claim"].update(kind="window"), "full-stream, coefficients"),
        (lambda d: d["claim"]["domain"].update(t=[[1, 1], [0, 1]]), "ordered"),
        (lambda d: d["claim"].update(port='u"'), "port"),
        (lambda d: d["claim"].update(post=["atom", "le", ["var", 3]]), "coordinate"),
    ],
)
def test_malformed_goals_are_refused(change, match) -> None:
    document = json.loads(json.dumps(encode_goal(FIELD)))
    change(document)
    with pytest.raises(GoalError, match=match):
        decode_goal(document, LIMITS)


def test_coefficient_goals_bound_their_window() -> None:
    document = json.loads(json.dumps(encode_goal(COEFFICIENTS)))
    document["claim"]["observed"].append(document["claim"]["observed"][0])
    with pytest.raises(GoalError, match="distinct"):
        decode_goal(document, LIMITS)
    document["claim"]["observed"] = [["u", [0, 99]]]
    document["claim"]["post"] = ["true"]
    with pytest.raises(GoalError, match="degrees"):
        decode_goal(document, LIMITS)


@pytest.mark.parametrize(
    ("goal", "reason"),
    [
        (BY_NAME["other-stream"], "full-stream identity"),
        (BY_NAME["analytic-tail"], "truncation claim"),
        (
            heat_goal(TAIL_CLAIM, interpretation="analytic-tail"),
            "exponential-sum profiles only",
        ),
        (heat_goal(TAIL_CLAIM, profile=EXP), "read as analytic-tail"),
        (
            heat_goal(
                replace(TAIL_CLAIM, port="u_xx"),
                interpretation="analytic-tail",
                profile=EXP,
            ),
            "rebuilt output",
        ),
        (BY_NAME["field-stochastic"], "stochastic"),
        (heat_goal(field_claim(band(3)), interpretation="formal-stream"), "read as"),
        (
            heat_goal(field_claim(band(3)), profile=Mul(Const(Fraction(2)), Var(0))),
            "x²",
        ),
        (
            heat_goal(CoefficientClaim(OBSERVED[:2], Atom(Relation.LE, Var(0)))),
            "observed window",
        ),
        (
            heat_goal(
                CoefficientClaim(OBSERVED, Atom(Relation.LE, Var(0))), basis="egf"
            ),
            "OGF",
        ),
        (
            heat_goal(
                FieldClaim(
                    "u", (Fraction(0), Fraction(1)), (Fraction(0), Fraction(1)), band(3)
                )
            ),
            "symmetric",
        ),
        (
            heat_goal(
                FieldClaim(
                    "u_xx",
                    (Fraction(0), Fraction(1)),
                    (Fraction(-1), Fraction(1)),
                    band(3),
                )
            ),
            "rebuilt output",
        ),
    ],
)
def test_unsupported_goals_are_named(goal, reason) -> None:
    found = replay.supported(goal)
    assert isinstance(found, str) and reason in found
    result = service.decide_stream(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED


def test_the_leaves_mirror_the_lean_leaf_shapes() -> None:
    context, antecedent, consequent = replay.leaf_constraints(COEFFICIENTS, HEAT)
    assert context.names == ("b", "u")
    assert len(nonstrict(antecedent)) == 4  # type: ignore[arg-type]
    assert len(nonstrict(consequent)) == 6  # type: ignore[arg-type]
    context, antecedent, consequent = replay.leaf_constraints(FIELD, HEAT)
    assert context.names == ("t", "x", "u")
    assert len(nonstrict(antecedent)) == 3  # type: ignore[arg-type]
    # u is replaced by the field x² + 2t in the post.
    assert "Var(index=2)" not in repr(consequent)


def test_substitution_is_exact() -> None:
    post = Atom(Relation.LE, Add(Var(2), Var(0)))
    replaced = render.substitute(post, {2: HEAT.field.expression})
    assert replaced == Atom(Relation.LE, Add(HEAT.field.expression, Var(0)))


def test_the_roots_restate_their_identities() -> None:
    claim = FIELD.claim
    assert isinstance(claim, FieldClaim)
    field = render.prove_field(FIELD, HEAT, claim, _certificate(2, 3))
    assert '.context.names = ["t", "x", "u"]' in field.prelude
    assert "Gimle.Forseti.FieldBound.sq_le_sq_of_box" in "\n".join(field.script)
    assert "(Gimle.Asgard.Streams.Heat.circuit .ogf)" in field.prelude
    observed = COEFFICIENTS.claim
    assert isinstance(observed, CoefficientClaim)
    coefficients = render.prove_coefficients(HEAT, observed, _certificate(6, 4))
    assert '"u_xx[t^0, x^0]"' in coefficients.prelude
    assert '["boundary[t^0, x^2]", "u[t^0, x^2]"]' in coefficients.prelude
    stream = render.full_stream(HEAT, HEAT.full_stream.outputs)
    assert "Realizes .ogf Gimle.Asgard.Examples.FormalHeat.heat" in stream.prelude
    assert "(MvPolynomial.C (mkRat (2) 1))" in stream.prelude
    for unit in (field, coefficients, stream):
        assert "open " not in unit.prelude


def test_the_full_stream_contract_is_stated_as_before() -> None:
    """The statement is built from the boundary the readable claim cites (158).

    Its text is part of every retained full-stream source, so it must not move.
    """
    heat = "Gimle.Asgard.Examples.FormalHeat"
    assert HEAT.full_stream.statement == (
        f"fun y => y = ![{heat}.two, {heat}.heat] ∧ "
        f"Gimle.Forseti.Stream.Boundary 0 1 {heat}.boundary y"
    )


# --- replay binding, before Lean -------------------------------------------


@pytest.fixture
def no_lean(monkeypatch):
    """Fail the test if replay reaches Lean."""
    from glitnir_forseti.trajectory import replay as trajectory_replay

    def forbidden(*args, **kwargs):
        raise AssertionError("replay reached Lean")

    monkeypatch.setattr(trajectory_replay, "verify_closure", forbidden)
    monkeypatch.setattr(trajectory_replay, "check_independently", forbidden)


def _field_artifact(goal=FIELD, evidence=None) -> bytes:
    return replay.build_artifact(
        goal, evidence or _certificate(2, 3), LIMITS, {"stage": "test"}
    )


def _edit(artifact: bytes, change) -> bytes:
    document = json.loads(artifact)
    change(document)
    return json.dumps(document).encode()


def test_a_prefix_is_not_a_whole_stream(no_lean) -> None:
    """A coefficient artifact replayed as the full-stream identity is refused."""
    artifact = replay.build_artifact(
        COEFFICIENTS, _certificate(6, 4), LIMITS, {"stage": "test"}
    )
    outcome = replay.replay_stream(artifact, BY_NAME["full-stream"], LIMITS)
    assert isinstance(outcome, InvalidEvidence)
    assert "different root goal" in outcome.reason
    relabelled = _edit(
        artifact,
        lambda d: d.update(goal=encode_goal(BY_NAME["full-stream"])),
    )
    outcome = replay.replay_stream(relabelled, BY_NAME["full-stream"], LIMITS)
    assert isinstance(outcome, InvalidEvidence)
    assert "no leaf evidence" in outcome.reason


@pytest.mark.parametrize(
    "other",
    [
        heat_goal(field_claim(band(3)), basis="egf"),
        heat_goal(
            FieldClaim(
                "u", (Fraction(0), Fraction(2)), (Fraction(-1), Fraction(1)), band(3)
            )
        ),
        heat_goal(
            FieldClaim(
                "u", (Fraction(0), Fraction(1)), (Fraction(-2), Fraction(2)), band(3)
            )
        ),
        heat_goal(field_claim(band(4))),
    ],
    ids=["basis", "time-domain", "space-domain", "bound"],
)
def test_an_old_certificate_is_bound_to_its_own_root(no_lean, other) -> None:
    outcome = replay.replay_stream(_field_artifact(), other, LIMITS)
    assert isinstance(outcome, InvalidEvidence)
    assert "different root goal" in outcome.reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: d.update(status="REFUTED"), "theorem"),
        (
            lambda d: d["environment"].update(closure_sha256="0" * 64),
            "Lean environment",
        ),
        (
            lambda d: d.update(source=d["source"].replace("decide +kernel", "sorry")),
            "retained source",
        ),
        (lambda d: d["evidence"]["certificate"]["rows"].pop(), "malformed"),
        (lambda d: d.update(evidence={"kind": "contract"}), "contract alone"),
    ],
)
def test_replay_rejects_before_lean(no_lean, change, reason) -> None:
    outcome = replay.replay_stream(_edit(_field_artifact(), change), FIELD, LIMITS)
    assert isinstance(outcome, InvalidEvidence)
    assert reason in outcome.reason


def test_a_witness_names_the_admitted_input(no_lean) -> None:
    goal = BY_NAME["field-below-two"]
    witness = Witness((("t", Fraction(1)), ("x", Fraction(1)), ("u", Fraction(3))))
    artifact = replay.build_artifact(goal, witness, LIMITS, {"stage": "test"})
    evidence = json.loads(artifact)["evidence"]
    assert "the heat solution" in evidence["input"]
    changed = _edit(artifact, lambda d: d["evidence"].update(input="![0, 0, 0]"))
    outcome = replay.replay_stream(changed, goal, LIMITS)
    assert isinstance(outcome, InvalidEvidence)
    assert "another admitted input" in outcome.reason


def test_replay_maps_lean_outcomes(monkeypatch) -> None:
    from glitnir_forseti.trajectory import replay as trajectory_replay

    confirmed = IndependentCheck(IndependentStatus.CONFIRMED)
    monkeypatch.setattr(trajectory_replay, "verify_closure", lambda *a, **k: confirmed)
    for check, expected in (
        (
            IndependentCheck(IndependentStatus.CONFIRMED, ("propext",)),
            replay.ValidatedStream,
        ),
        (IndependentCheck(IndependentStatus.CONFIRMED, ("sorryAx",)), InvalidEvidence),
        (IndependentCheck(IndependentStatus.REJECTED, reason="no"), InvalidEvidence),
        (IndependentCheck(IndependentStatus.UNKNOWN, reason="late"), ReplayUnavailable),
    ):
        monkeypatch.setattr(
            trajectory_replay, "check_independently", lambda *a, c=check, **k: c
        )
        assert type(replay.replay_stream(_field_artifact(), FIELD, LIMITS)) is expected


# --- the service -----------------------------------------------------------


def _lanes(monkeypatch, certificate: LaneOutcome, witness: LaneOutcome) -> None:
    monkeypatch.setattr(service, "certify", lambda *a, **k: certificate)
    monkeypatch.setattr(service, "refute", lambda *a, **k: witness)


@pytest.mark.parametrize(
    ("certificate", "witness", "reason"),
    [
        ("none", "none", "exhausted"),
        ("unsupported-fragment", "none", "unsupported"),
        ("timeout", "none", "timeout"),
        ("z3-unavailable", "z3-unavailable", "z3-unavailable"),
    ],
)
def test_unknown_reasons(monkeypatch, certificate, witness, reason) -> None:
    _lanes(monkeypatch, LaneOutcome(certificate), LaneOutcome(witness))
    result = service.decide_stream(COEFFICIENTS)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is not None and result.reason.value == reason


def test_a_field_witness_tries_the_corners_first(monkeypatch) -> None:
    """The bound 2 fails at the corner (1, 1), where the field u is 3."""
    _lanes(monkeypatch, LaneOutcome("none"), LaneOutcome("none"))
    seen = []

    def fake_replay(artifact, goal, *args, **kwargs):
        seen.append(json.loads(artifact)["evidence"]["values"])
        return InvalidEvidence("Lean rejected the root proof")

    monkeypatch.setattr(service, "replay_stream", fake_replay)
    result = service.decide_stream(BY_NAME["field-below-two"])
    assert seen == [[["t", [1, 1]], ["x", [1, 1]], ["u", [3, 1]]]]
    assert result.reason is service.UnknownReason.EXHAUSTED


def test_the_full_stream_identity_needs_no_search(monkeypatch) -> None:
    _lanes(monkeypatch, LaneOutcome("found"), LaneOutcome("found"))
    seen = []

    def fake_replay(artifact, goal, *args, **kwargs):
        seen.append(json.loads(artifact)["evidence"])
        return replay.ValidatedStream("PROVEN", goal, replay.ContractOnly(), (), ())

    monkeypatch.setattr(service, "replay_stream", fake_replay)
    result = service.decide_stream(BY_NAME["full-stream"])
    assert seen == [{"kind": "contract"}]
    assert result.status is service.Status.PROVEN


def test_every_stream_import_set_is_pinned() -> None:
    pinned = [tuple(entry["imports"]) for entry in replay_pin()["closures"]]
    for imports in replay_imports():
        assert imports in pinned
        assert len(closure_identity(imports)) == 64


def test_the_command_reports_errors_without_a_traceback(tmp_path, capsys) -> None:
    from glitnir_forseti.streams.__main__ import main

    goal = tmp_path / "goal.json"
    goal.write_text(json.dumps(encode_goal(heat_goal(field_claim(band(4))))))
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(_field_artifact())
    assert main(["replay", str(artifact), "--goal", str(goal)]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "invalid"
    goal.write_text("{")
    assert main(["replay", str(artifact), "--goal", str(goal)]) == 2


def test_full_stream_claims_carry_every_output() -> None:
    with pytest.raises(GoalError):
        heat_goal(FullStreamClaim(()))


# --- truncation claims (135) ----------------------------------------------


def test_truncation_goals_round_trip_and_refuse_malformed_parts() -> None:
    document = json.loads(json.dumps(encode_goal(EXP_TAIL)))
    assert document["schema"] == "gimle.forseti.stream-goal/v2"
    assert document["profile"] == {"exponentials": [[[1, 1], [1, 1]]]}
    assert decode_goal(document, LIMITS) == EXP_TAIL
    for change, match in (
        (lambda d: d["claim"].update(window=[8]), "window"),
        (lambda d: d["claim"].update(window=[8, 999]), "window"),
        (lambda d: d["claim"]["radius"].update(t=[-1, 4]), "nonnegative"),
        (lambda d: d["claim"].update(extra=1), "fields"),
        (lambda d: d.update(profile={"exponentials": []}), "non-empty"),
        (lambda d: d.update(profile={"exponentials": [[[1, 1]]]}), r"\[c, a\]"),
        (
            lambda d: d.update(profile={"exponentials": [[[1, 1], [1, 1]]] * 17}),
            "bounded",
        ),
    ):
        changed = json.loads(json.dumps(document))
        change(changed)
        with pytest.raises(GoalError, match=match):
            decode_goal(changed, LIMITS)


def test_the_tail_mirror_matches_the_lean_example() -> None:
    """asgard-lean's ExpHeat: e^x, box (1/4, 1/2), window (8, 16) gives 1/12288."""
    assert tail.tail_bound(EXP, TAIL_CLAIM, Fraction(1)) == Fraction(1, 12288)
    assert tail.weight(COSH) == 1
    assert tail.fits(EXP, Fraction(1)) and not tail.fits(EXP, Fraction(2))


@pytest.mark.parametrize(
    ("profile", "claim", "expected"),
    [
        (EXP, TAIL_CLAIM, Fraction(1)),
        (ExpSum(((Fraction(3), Fraction(-2)),)), TAIL_CLAIM, "not strictly inside"),
        (EXP, truncation(Fraction(1, 20000)), "below the certified bound 1/12288"),
        # A constant profile fits every ρ: it is doubled from 3/2 until the
        # bound 5·(Σ qᵢ^Nᵢ)·∏(1 − qᵢ)⁻¹ is at most ε.
        (ExpSum(((Fraction(5), Fraction(0)),)), TAIL_CLAIM, Fraction(3, 2)),
    ],
)
def test_the_producer_chooses_the_largest_fitting_radius(profile, claim, expected):
    chosen = tail.choose(profile, claim, LIMITS)
    if isinstance(expected, Fraction):
        assert chosen == tail.TailEvidence(expected)
        assert tail.tail_bound(profile, claim, expected) <= claim.epsilon
    else:
        assert isinstance(chosen, str) and expected in chosen


def test_a_truncation_artifact_carries_only_its_radius(no_lean) -> None:
    artifact = replay.build_artifact(
        EXP_TAIL, tail.TailEvidence(Fraction(1)), LIMITS, {"stage": "test"}
    )
    document = json.loads(artifact)
    assert document["schema"] == "gimle.forseti.stream-artifact/v2"
    assert document["evidence"] == {"kind": "tail-radius", "rho": [1, 1]}
    assert document["environment"]["imports"] == ["Gimle.Asgard.Streams.AnalyticHeat"]
    assert "expSum_truncation" in document["source"]
    for change, reason in (
        (lambda d: d["evidence"].update(rho=[0, 1]), "positive"),
        (lambda d: d["evidence"].update(extra=1), "only its radius"),
        (lambda d: d.update(evidence={"kind": "contract"}), "contract alone"),
        (lambda d: d["evidence"].update(rho=[2, 1]), "retained source"),
        (lambda d: d.update(status="REFUTED"), "different theorem"),
        (lambda d: d["environment"].update(imports=["Gimle.Forseti"]), "environment"),
    ):
        outcome = replay.replay_stream(_edit(artifact, change), EXP_TAIL, LIMITS)
        assert isinstance(outcome, InvalidEvidence), outcome
        assert reason in outcome.reason
    for other in (
        heat_goal(
            truncation(Fraction(1, 999)), interpretation="analytic-tail", profile=EXP
        ),
        heat_goal(TAIL_CLAIM, interpretation="analytic-tail", profile=COSH),
        heat_goal(
            replace(TAIL_CLAIM, window=(8, 15)),
            interpretation="analytic-tail",
            profile=EXP,
        ),
    ):
        outcome = replay.replay_stream(artifact, other, LIMITS)
        assert (
            isinstance(outcome, InvalidEvidence)
            and "different root goal" in outcome.reason
        )


def test_a_radius_is_refused_for_other_claims(no_lean) -> None:
    artifact = _edit(
        _field_artifact(),
        lambda d: d.update(evidence={"kind": "tail-radius", "rho": [1, 1]}),
    )
    outcome = replay.replay_stream(artifact, FIELD, LIMITS)
    assert isinstance(outcome, InvalidEvidence) and "radius" in outcome.reason


def test_a_truncation_claim_below_the_bound_is_never_refuted(monkeypatch) -> None:
    monkeypatch.setattr(
        service, "replay_stream", lambda *a, **k: pytest.fail("nothing to check")
    )
    goal = heat_goal(
        truncation(Fraction(1, 20000)), interpretation="analytic-tail", profile=EXP
    )
    result = service.decide_stream(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.EXHAUSTED
    assert "1/12288" in result.stages[-1].detail


def test_a_constant_profile_stops_doubling_at_the_bit_limit() -> None:
    """Radii near 2^255 would need a ρ over the coefficient bit limit."""
    huge = Fraction(2**255 - 1)
    claim = TruncationClaim("u", (64, 64), (huge, huge), Fraction(1, 2**200))
    chosen = tail.choose(ExpSum(((Fraction(1), Fraction(0)),)), claim, LIMITS)
    # No ρ over the limit is proposed; the reason names the last bound reached.
    assert isinstance(chosen, str) and "certified bound ≈ 2^" in chosen


# --- the Burgers front (187) ------------------------------------------------


def test_a_viscous_goal_round_trips_and_a_heat_document_reads_as_before() -> None:
    document = json.loads(json.dumps(encode_goal(FRONT_TAIL)))
    assert document["viscosity"] == [1, 2]
    assert decode_goal(document, LIMITS) == FRONT_TAIL
    assert "viscosity" not in encode_goal(EXP_TAIL)
    for change, match in (
        (lambda d: d.update(viscosity="1/2"), "integer pair"),
        (lambda d: d.update(viscosity=[1, 0]), "denominator"),
        (lambda d: d.update(extra=1), "fields"),
    ):
        changed = json.loads(json.dumps(document))
        change(changed)
        with pytest.raises(GoalError, match=match):
            decode_goal(changed, LIMITS)
    with pytest.raises(GoalError, match="viscosity is a fraction"):
        burgers_goal(FRONT_CLAIM, viscosity=0.5)  # type: ignore[arg-type]


def test_the_front_mirror_matches_the_lean_example() -> None:
    """asgard-lean's BurgersFront: ν = 1/2, 1 + e^(−x), r = (2/3, 1/2), 1/16384."""
    nu = Fraction(1, 2)
    assert tail.constant_term(FRONT) == 2 and tail.rest_weight(FRONT) == 1
    assert tail.heat_radii(nu, Fraction(1)) == (Fraction(2), Fraction(1))
    assert tail.front_bound(nu, FRONT) == Fraction(1, 2)
    assert tail.small_enough(nu, Fraction(1), FRONT, FRONT_RADII)
    assert not tail.small_enough(nu, Fraction(1), FRONT, (Fraction(1), Fraction(1, 2)))
    assert tail.front_tail_bound(nu, FRONT, FRONT_CLAIM, FRONT_RADII) == Fraction(
        1, 16384
    )
    chosen = tail.choose_front(nu, FRONT, FRONT_CLAIM, LIMITS)
    assert chosen == tail.FrontEvidence(Fraction(1), FRONT_RADII)


@pytest.mark.parametrize(
    ("claim", "profile", "expected"),
    [
        # A tighter ε than any radii on the grid certify.
        (front_truncation(Fraction(1, 1000000)), FRONT, "below the certified bound"),
        # The box reaches half the heat radii: no r fits.
        (
            front_truncation(Fraction(1, 10000), (Fraction(1), Fraction(1, 8))),
            FRONT,
            "not strictly inside half of any radii",
        ),
        # Every grid point holds the box, but the inverse is majorized at none:
        # the rest weight 99/100 against the constant term 1/100.
        (
            FRONT_CLAIM,
            ExpSum(((Fraction(1), Fraction(0)), (Fraction(-99, 100), Fraction(-1)))),
            "majorized at no radii on the grid",
        ),
        # A constant potential has a zero front: the bound is 0 at the first
        # radii that hold the box, at the starting ρ = max(1, 2 r_x, 2 r_t + 1).
        (
            front_truncation(Fraction(1, 10000)),
            ExpSum(((Fraction(2), Fraction(0)),)),
            (Fraction(4, 3), Fraction(0)),
        ),
        # The box |x| ≤ 1 needs ρ doubled from 2 to 4 before r_x/2 can exceed it.
        (
            front_truncation(Fraction(1, 10000), (Fraction(1, 6), Fraction(1))),
            ExpSum(((Fraction(2), Fraction(0)),)),
            (Fraction(4), Fraction(0)),
        ),
    ],
)
def test_the_front_producer_chooses_the_least_bound(claim, profile, expected) -> None:
    chosen = tail.choose_front(Fraction(1, 2), profile, claim, LIMITS)
    if isinstance(expected, str):
        assert isinstance(chosen, str) and expected in chosen, chosen
    else:
        rho, bound = expected
        assert isinstance(chosen, tail.FrontEvidence) and chosen.rho == rho, chosen
        assert all(b < r / 2 for b, r in zip(claim.radius, chosen.radii))
        assert tail.small_enough(Fraction(1, 2), chosen.rho, profile, chosen.radii)
        assert (
            tail.front_tail_bound(Fraction(1, 2), profile, claim, chosen.radii) == bound
        )
        assert bound <= claim.epsilon


def test_the_front_mirror_pins_the_lean_tests_too() -> None:
    """asgard-lean's Tests/ColeHopf: window 20 × 20 gives 1/262144; ρ = 2 misfits."""
    nu = Fraction(1, 2)
    wider = replace(FRONT_CLAIM, window=(20, 20))
    assert tail.front_tail_bound(nu, FRONT, wider, FRONT_RADII) == Fraction(1, 262144)
    assert not tail.fits(FRONT, Fraction(2))
    assert list(tail._candidate_rho(FRONT, FRONT_CLAIM, LIMITS)) == [Fraction(1)]
    # The potential without a constant term is refused before any division.
    assert "constant term" in tail.choose_front(nu, NO_FRONT, FRONT_CLAIM, LIMITS)


def test_the_front_producer_keeps_its_radii_within_the_bit_limit() -> None:
    """Radii over the coefficient bit limit could not be carried by an artifact."""
    tiny = EntailmentLimits(max_coefficient_bits=8)
    chosen = tail.choose_front(Fraction(1, 2), FRONT, FRONT_CLAIM, tiny)
    assert chosen == tail.FrontEvidence(Fraction(1), FRONT_RADII)
    rate = ExpSum(((Fraction(1), Fraction(0)), (Fraction(1), Fraction(-1, 1000))))
    chosen = tail.choose_front(Fraction(1, 2), rate, FRONT_CLAIM, tiny)
    assert isinstance(chosen, str) and "within the coefficient bit limit" in chosen


@pytest.mark.parametrize(
    ("goal", "reason"),
    [
        (burgers_goal(FRONT_CLAIM, viscosity=Fraction(0)), "positive viscosity"),
        (burgers_goal(FRONT_CLAIM, viscosity=Fraction(-1)), "positive viscosity"),
        (burgers_goal(FRONT_CLAIM, viscosity=None), "names its viscosity"),
        (burgers_goal(FRONT_CLAIM, profile=NO_FRONT), "constant term"),
        (burgers_goal(FRONT_CLAIM, basis="egf"), "OGF circuit"),
        (burgers_goal(replace(FRONT_CLAIM, port="u_t")), "rebuilt output u"),
        (
            burgers_goal(
                field_claim(band(3)),
                interpretation="evaluated-field",
                profile=Mul(Var(0), Var(0)),
            ),
            "names a viscosity",
        ),
        (
            burgers_goal(
                field_claim(band(3)),
                viscosity=None,
                interpretation="evaluated-field",
                profile=Mul(Var(0), Var(0)),
            ),
            "truncation claims only",
        ),
        (replace(EXP_TAIL, viscosity=Fraction(1)), "take no viscosity"),
        (
            replace(heat_goal(field_claim(band(3))), viscosity=Fraction(1)),
            "names a viscosity",
        ),
    ],
)
def test_unsupported_front_goals_are_named(goal, reason) -> None:
    found = replay.supported(goal)
    assert isinstance(found, str) and reason in found, found
    result = service.decide_stream(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED


def test_a_front_artifact_carries_its_radii(no_lean) -> None:
    evidence = tail.FrontEvidence(Fraction(1), FRONT_RADII)
    artifact = replay.build_artifact(FRONT_TAIL, evidence, LIMITS, {"stage": "test"})
    document = json.loads(artifact)
    assert document["evidence"] == {
        "kind": "front-radii",
        "rho": [1, 1],
        "r": {"t": [2, 3], "x": [1, 2]},
    }
    assert document["environment"]["imports"] == ["Gimle.Asgard.Streams.ColeHopf"]
    assert document["goal"]["viscosity"] == [1, 2]
    source = document["source"]
    assert "ColeHopf.front_truncation" in source
    assert "ColeHopf.front (mkRat (1) 2) [((mkRat (1) 1), (mkRat (0) 1))" in source
    assert "def forsetiRadii : Fin 2 → ℚ := ![(mkRat (2) 3), (mkRat (1) 2)]" in source
    for change, reason in (
        (lambda d: d["evidence"].update(rho=[0, 1]), "positive"),
        (lambda d: d["evidence"]["r"].update(t=[-2, 3]), "positive"),
        (lambda d: d["evidence"].update(extra=1), "only its radius ρ and radii r"),
        (lambda d: d["evidence"]["r"].update(y=[1, 1]), "per axis"),
        (
            lambda d: d.update(evidence={"kind": "tail-radius", "rho": [1, 1]}),
            "only its radius ρ and radii r",
        ),
        (lambda d: d["evidence"]["r"].update(t=[1, 3]), "retained source"),
        (lambda d: d["evidence"].update(r=[[2, 3], [1, 2]]), "per axis"),
        (lambda d: d["evidence"]["r"].update(t="2/3"), "integer pair"),
        (lambda d: d["evidence"]["r"].update(t=[4, 6]), "lowest terms"),
        (lambda d: d["evidence"]["r"].update(t=[2**500, 3]), "bit size"),
        (lambda d: d["evidence"].pop("kind"), "only its radius ρ and radii r"),
        (lambda d: d["goal"].pop("viscosity"), "different root goal"),
        (lambda d: d["goal"].update(viscosity=[-1, 2]), "different root goal"),
        (
            lambda d: d["environment"].update(
                imports=["Gimle.Asgard.Streams.AnalyticHeat"]
            ),
            "different Lean environment",
        ),
        (lambda d: d.update(status="REFUTED"), "different theorem"),
    ):
        outcome = replay.replay_stream(_edit(artifact, change), FRONT_TAIL, LIMITS)
        assert isinstance(outcome, InvalidEvidence), outcome
        assert reason in outcome.reason, (reason, outcome.reason)
    for other in (
        burgers_goal(FRONT_CLAIM, viscosity=Fraction(1, 3)),
        burgers_goal(front_truncation(Fraction(1, 9999))),
        burgers_goal(FRONT_CLAIM, profile=COSH),
    ):
        outcome = replay.replay_stream(artifact, other, LIMITS)
        assert (
            isinstance(outcome, InvalidEvidence)
            and "different root goal" in outcome.reason
        )
    heat = _edit(
        replay.build_artifact(
            EXP_TAIL, tail.TailEvidence(Fraction(1)), LIMITS, {"stage": "test"}
        ),
        lambda d: d.update(
            evidence={
                "kind": "front-radii",
                "rho": [1, 1],
                "r": {"t": [1, 1], "x": [1, 1]},
            }
        ),
    )
    outcome = replay.replay_stream(heat, EXP_TAIL, LIMITS)
    assert (
        isinstance(outcome, InvalidEvidence) and "only its radius ρ" in outcome.reason
    )


def test_a_front_claim_below_the_bound_is_never_refuted(monkeypatch) -> None:
    monkeypatch.setattr(
        service, "replay_stream", lambda *a, **k: pytest.fail("nothing to check")
    )
    result = service.decide_stream(burgers_goal(front_truncation(Fraction(1, 1000000))))
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.EXHAUSTED
    assert "1/16384" in result.stages[-1].detail


def test_the_burgers_model_has_only_the_truncation_lane() -> None:
    assert BURGERS.tail is not None and BURGERS.tail.viscous
    assert (BURGERS.full_stream, BURGERS.coefficients, BURGERS.field) == (None,) * 3
    assert ("Gimle.Asgard.Streams.ColeHopf",) in replay_imports()
    with pytest.raises(ValueError, match="viscosity"):
        render.prove_truncation(FRONT_TAIL, Fraction(1))
    with pytest.raises(ValueError, match="viscosity"):
        render.prove_front(EXP_TAIL, Fraction(1), FRONT_RADII)
    with pytest.raises(ValueError, match="exponential-sum truncation claim"):
        render.prove_front(heat_goal(field_claim(band(3))), Fraction(1), FRONT_RADII)
    front = tail.FrontEvidence(Fraction(1), FRONT_RADII)
    with pytest.raises(ValueError, match="needs a viscosity"):
        replay.build_artifact(EXP_TAIL, front, LIMITS, {})
    with pytest.raises(ValueError, match="takes no viscosity"):
        replay.build_artifact(FRONT_TAIL, tail.TailEvidence(Fraction(1)), LIMITS, {})


def test_a_viscosity_over_the_bit_limit_is_a_resource_limit() -> None:
    huge = burgers_goal(FRONT_CLAIM, viscosity=Fraction(2**400 + 1, 3))
    assert replay.supported(huge) is BURGERS
    result = service.decide_stream(huge)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.RESOURCE_LIMIT


def test_the_front_stage_reports_its_radii_and_bound() -> None:
    outcome, evidence = service._tail(FRONT_TAIL, BURGERS, LIMITS)
    assert outcome.outcome == "found"
    assert evidence == tail.FrontEvidence(Fraction(1), FRONT_RADII)
    assert outcome.detail == (
        "ρ = 1, inverse radii (r_t, r_x) = (2/3, 1/2), certified bound 1/16384 "
        "≤ ε = 1/10000"
    )
    outcome, evidence = service._tail(EXP_TAIL, HEAT, LIMITS)
    assert outcome.outcome == "found" and evidence == tail.TailEvidence(Fraction(1))
    assert outcome.detail == "ρ = 1, certified bound 1/12288 ≤ ε = 1/1000"


def test_the_report_names_the_front_solution_class() -> None:
    from glitnir_forseti.streams.__main__ import _profile, _solution_class

    front = _solution_class(FRONT_TAIL)
    assert "Burgers circuit at viscosity 1/2" in front
    assert "Gimle.Asgard.Streams.ColeHopf" in front and "closed-form front" in front
    heat = _solution_class(EXP_TAIL)
    assert "heat circuit" in heat and "Burgers" not in heat
    assert "Burgers" not in _solution_class(FIELD)
    assert _profile(FRONT_TAIL) == "1·e^(0x) + 1·e^(-1x)"


def test_exponential_sum_terms_are_a_tuple() -> None:
    with pytest.raises(GoalError, match="bounded list"):
        heat_goal(
            TAIL_CLAIM,
            interpretation="analytic-tail",
            profile=ExpSum([(Fraction(1), Fraction(1))]),  # type: ignore[arg-type]
        )


# --- field bands on the front ---------------------------------------

NU = Fraction(1, 2)
BAND = band_goal(front_band(Fraction(2, 5), Fraction(3, 5)))
ABOVE = band_goal(front_band(Fraction(2, 5), Fraction(27, 50)))
BELOW = band_goal(front_band(Fraction(11, 20), None))
UPPER = band_goal(front_band(None, Fraction(3, 5)))
NARROW = band_goal(front_band(Fraction(56, 125), Fraction(69, 125)))


def test_band_of_reads_bands_in_u_alone() -> None:
    u, t = Var(2), Var(0)
    assert band_of(BAND.claim.post) == (Fraction(2, 5), Fraction(3, 5))
    assert band_of(UPPER.claim.post) == (None, Fraction(3, 5))
    assert band_of(BELOW.claim.post) == (Fraction(11, 20), None)
    # c − u ≤ 0 is u ≥ c; two lower bounds keep the larger.
    assert band_of(
        And(
            Atom(Relation.LE, Add(Const(Fraction(1, 2)), Neg(u))),
            Atom(Relation.GE, Add(u, Neg(Const(Fraction(2, 5))))),
        )
    ) == (Fraction(1, 2), None)
    for post in (
        Atom(Relation.LE, Add(u, Neg(t))),  # u ≤ t
        Atom(Relation.LE, Add(Mul(Const(Fraction(2)), u), Neg(Const(Fraction(1))))),
        Atom(Relation.LT, Add(u, Neg(Const(Fraction(1))))),  # strict
        Atom(Relation.LE, Mul(u, u)),
        Atom(Relation.GE, Add(t, Neg(Const(Fraction(1))))),  # no u
        Or(Atom(Relation.GE, u), Atom(Relation.LE, u)),
        Tru(),
        # An empty band, and a degree past the expansion limit.
        front_band(Fraction(3, 5), Fraction(2, 5)).post,
        Atom(Relation.LE, _power(u, 9)),
    ):
        assert band_of(post) is None, post
    nine = band_goal(replace(BAND.claim, post=Atom(Relation.LE, _power(u, 9))))
    assert "is a band" in str(replay.supported(nine))
    result = service.decide_stream(nine)
    assert result.reason is service.UnknownReason.UNSUPPORTED


def _power(base, exponent: int):
    poly = base
    for _ in range(exponent - 1):
        poly = Mul(poly, base)
    return poly


def test_the_window_mirror_matches_the_lean_example() -> None:
    """asgard-lean's BurgersFront: the checked inverseTable and the band it gives."""
    table = tail.inverse_table(NU, FRONT, tail.BAND_WINDOW)
    assert len(table) == 16 and all(len(row) == 16 for row in table)
    assert table[0][:6] == (
        Fraction(1, 2),
        Fraction(1, 4),
        Fraction(0),
        Fraction(-1, 48),
        Fraction(0),
        Fraction(1, 480),
    )
    assert table[1][:3] == (Fraction(-1, 8), Fraction(0), Fraction(1, 32))
    assert table[14][1] == Fraction(-929569, 45706531936665600)
    assert table[15][0] == Fraction(929569, 1371195958099968000)
    coefficients = tail.front_window(NU, FRONT, table, tail.BAND_WINDOW)
    assert coefficients[0][0] == Fraction(1, 2)  # the front at the origin
    spread = tail.window_spread(coefficients, (Fraction(1, 6), Fraction(1, 8)))
    assert spread < Fraction(6, 100)  # asgard-lean Tests/ColeHopf
    low, high = render.window_bounds(
        BAND,
        BAND.claim,
        tail.WindowEvidence(
            Fraction(1), FRONT_RADII, (16, 16), Fraction(1, 16384), table
        ),
    )
    assert Fraction(2, 5) < low < Fraction(449, 1000)
    assert Fraction(552, 1000) < high < Fraction(3, 5)
    # The window agrees with the closed form 1/(1 + e^(x − t/2)) at the corner.
    import math

    corner = tail.window_value(coefficients, (Fraction(1, 6), Fraction(-1, 8)))
    assert abs(float(corner) - 1 / (1 + math.exp(-5 / 24))) < 1 / 16384
    assert tail.heat_coeff(NU, FRONT, 0, 0) == 2
    assert tail.shifted(FRONT) == ExpSum(
        ((Fraction(0), Fraction(0)), (Fraction(-1), Fraction(-1)))
    )


def test_the_band_producer_proves_refutes_or_says_why() -> None:
    chosen = tail.choose_band(NU, FRONT, BAND.claim, LIMITS)
    assert isinstance(chosen, tail.WindowEvidence) and not chosen.refutes
    assert (chosen.rho, chosen.radii, chosen.window, chosen.epsilon) == (
        Fraction(1),
        FRONT_RADII,
        (16, 16),
        Fraction(1, 16384),
    )
    assert chosen.table == tail.inverse_table(NU, FRONT, (16, 16))
    above = tail.choose_band(NU, FRONT, ABOVE.claim, LIMITS)
    assert isinstance(above, tail.WindowEvidence) and above.refutes
    assert (above.point, above.side) == ((Fraction(1, 6), Fraction(-1, 8)), "above")
    below = tail.choose_band(NU, FRONT, BELOW.claim, LIMITS)
    assert isinstance(below, tail.WindowEvidence)
    assert (below.point, below.side) == ((Fraction(1, 6), Fraction(1, 8)), "below")
    narrow = tail.choose_band(NU, FRONT, NARROW.claim, LIMITS)
    assert isinstance(narrow, str)
    assert "[≈ 0.4477, ≈ 0.5523]" in narrow and "no proof and no refutation" in narrow
    assert "[56/125, 69/125]" in narrow
    assert "constant term" in tail.choose_band(NU, NO_FRONT, BAND.claim, LIMITS)
    tiny = EntailmentLimits(max_coefficient_bits=8)
    assert "certified error is over" in tail.choose_band(NU, FRONT, BAND.claim, tiny)
    small = EntailmentLimits(max_coefficient_bits=16)  # ε = 1/16384 fits, the table not
    assert "inverse's window is over" in tail.choose_band(NU, FRONT, BAND.claim, small)
    constant = tail.choose_band(
        NU,
        ExpSum(((Fraction(1), Fraction(0)),)),
        front_band(Fraction(-1), Fraction(1)),
        LIMITS,
    )
    assert isinstance(constant, tail.WindowEvidence) and constant.epsilon == 0
    with pytest.raises(ValueError, match="window's shape"):
        replace(constant, table=constant.table[1:])
    with pytest.raises(ValueError, match="point and a side"):
        replace(constant, side="above")
    with pytest.raises(ValueError, match="above, below or empty"):
        replace(constant, point=(Fraction(0), Fraction(0)), side="sideways")
    shape = FieldClaim(
        "u",
        BAND.claim.time,
        BAND.claim.space,
        Atom(Relation.LE, Add(Var(2), Neg(Var(0)))),
    )
    assert "not a band" in tail.choose_band(NU, FRONT, shape, LIMITS)
    wide = replace(BAND.claim, time=(Fraction(-1), Fraction(1)))
    assert "not strictly inside" in tail.choose_band(NU, FRONT, wide, LIMITS)


def test_approximately_shows_four_decimals() -> None:
    assert tail.approximately(Fraction(1, 3)) == "≈ 0.3333"
    assert tail.approximately(Fraction(-1, 2)) == "≈ -0.5000"
    assert tail.approximately(Fraction(2)) == "≈ 2.0000"
    assert tail.approximately(Fraction(99999, 100000)) == "≈ 1.0000"
    assert tail.approximately(Fraction(1, 20000)) == "≈ 0.0001"
    assert tail.approximately(Fraction(-1, 20000)) == "≈ -0.0001"


@pytest.mark.parametrize(
    ("goal", "reason"),
    [
        (
            heat_goal(
                FieldClaim(
                    "u",
                    (Fraction(-1, 4), Fraction(1, 4)),
                    (Fraction(-1, 2), Fraction(1, 2)),
                    Atom(Relation.GE, Var(2)),
                ),
                interpretation="analytic-field",
                profile=EXP,
            ),
            "no window root",
        ),
        (
            band_goal(
                FieldClaim(
                    "u",
                    BAND.claim.time,
                    BAND.claim.space,
                    Atom(Relation.LE, Add(Var(2), Neg(Var(0)))),
                )
            ),
            "is a band",
        ),
        (
            band_goal(replace(BAND.claim, time=(Fraction(0), Fraction(1, 6)))),
            "both intervals are symmetric",
        ),
        (
            band_goal(replace(BAND.claim, space=(Fraction(-1, 8), Fraction(1, 4)))),
            "both intervals are symmetric",
        ),
        (
            band_goal(BAND.claim, interpretation="analytic-tail"),
            "read as analytic-field",
        ),
        (band_goal(front_truncation(Fraction(1, 10000))), "is a field claim"),
        (
            band_goal(BAND.claim, interpretation="evaluated-field"),
            "read as analytic-field",
        ),
        (band_goal(BAND.claim, viscosity=None), "names its viscosity"),
        (band_goal(BAND.claim, viscosity=Fraction(0)), "positive viscosity"),
        (band_goal(BAND.claim, basis="egf"), "OGF circuit"),
        (band_goal(replace(BAND.claim, port="v")), "analytic-field claims are about"),
        (
            band_goal(BAND.claim, profile=Mul(Var(0), Var(0))),
            "exponential-sum profiles only",
        ),
        (band_goal(BAND.claim, profile=NO_FRONT), "constant term"),
        (band_goal(BAND.claim, interpretation="stochastic-ito"), "stochastic"),
    ],
)
def test_unsupported_band_goals_are_named(goal, reason) -> None:
    found = replay.supported(goal)
    assert isinstance(found, str) and reason in found, found
    result = service.decide_stream(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED


def _window(goal):
    found = tail.choose_band(NU, FRONT, goal.claim, LIMITS)
    assert isinstance(found, tail.WindowEvidence)
    return found


def test_a_band_artifact_carries_its_checked_window(no_lean) -> None:
    artifact = replay.build_artifact(BAND, _window(BAND), LIMITS, {"stage": "test"})
    document = json.loads(artifact)
    evidence = document["evidence"]
    assert set(evidence) == {"kind", "rho", "r", "window", "epsilon", "table"}
    assert evidence["kind"] == "window" and evidence["window"] == [16, 16]
    assert evidence["rho"] == [1, 1] and evidence["r"] == {"t": [2, 3], "x": [1, 2]}
    assert evidence["epsilon"] == [1, 16384]
    assert len(evidence["table"]) == 16 and all(len(r) == 16 for r in evidence["table"])
    assert evidence["table"][0][:2] == [[1, 2], [1, 4]]
    assert document["status"] == "PROVEN"
    assert document["environment"]["imports"] == ["Gimle.Asgard.Streams.ColeHopf"]
    source = document["source"]
    assert (
        "ColeHopf.front_band_of_window (lo := ((mkRat (2) 5) : ℚ)) "
        "(hi := ((mkRat (3) 5) : ℚ))"
    ) in source
    assert "(ε := (mkRat (1) 16384))" in source and "(Q := forsetiQ)" in source
    assert (
        "def forsetiTable : List (List ℚ) := [\n    [(mkRat (1) 2), (mkRat (1) 4),"
        in source
    )
    assert (
        "(((mkRat (2) 5) : ℚ) : ℝ) ≤ Gimle.Asgard.Streams.analyticField .ogf a x ∧"
        in source
    )
    assert (
        "forsetiPoint" not in source and "theorem forsetiSearch : forsetiRoot" in source
    )
    assert "TailCertificate.hasSum" in json.dumps(document["dependencies"])
    for change, reason in (
        (lambda d: d["evidence"].update(rho=[0, 1]), "positive"),
        (lambda d: d["evidence"]["r"].update(x=[-1, 2]), "positive"),
        (lambda d: d["evidence"].update(epsilon=[-1, 16384]), "ε is not negative"),
        (lambda d: d["evidence"].update(epsilon=[0, 1]), "retained source"),
        (lambda d: d["evidence"].update(window=[16, 17]), "the fixed [16, 16]"),
        (lambda d: d["evidence"].update(window=[0, 16]), "the fixed [16, 16]"),
        (lambda d: d["evidence"].update(window=[8, 16]), "the fixed [16, 16]"),
        (lambda d: d["evidence"].update(window=[True, 16]), "the fixed [16, 16]"),
        (lambda d: d["evidence"]["table"].pop(), "one row per time degree"),
        (lambda d: d["evidence"]["table"][3].pop(), "one entry per space degree"),
        (lambda d: d["evidence"]["table"][3].__setitem__(3, "1/2"), "integer pair"),
        (lambda d: d["evidence"]["table"][3].__setitem__(3, [2**500, 1]), "bit size"),
        (lambda d: d["evidence"]["table"][3].__setitem__(3, [1, 7]), "retained source"),
        (lambda d: d["evidence"].update(extra=1), "carries its radius ρ"),
        (lambda d: d["evidence"].pop("table"), "carries its radius ρ"),
        (
            lambda d: d["evidence"].update(point={"t": [0, 1], "x": [0, 1]}),
            "carries its radius ρ",
        ),
        (lambda d: d["evidence"].update(kind="front-radii"), "carries its radius ρ"),
        (
            lambda d: d["evidence"].update(
                point={"t": [0, 1], "x": [0, 1]}, side="above"
            ),
            "does not match the kind of evidence",
        ),
        (lambda d: d.update(status="REFUTED"), "different theorem"),
        (
            lambda d: d["environment"].update(
                imports=["Gimle.Asgard.Streams.AnalyticHeat"]
            ),
            "different Lean environment",
        ),
    ):
        outcome = replay.replay_stream(_edit(artifact, change), BAND, LIMITS)
        assert isinstance(outcome, InvalidEvidence), (reason, outcome)
        assert reason in outcome.reason, (reason, outcome.reason)
    for other in (UPPER, NARROW, band_goal(BAND.claim, viscosity=Fraction(1, 3))):
        outcome = replay.replay_stream(artifact, other, LIMITS)
        assert (
            isinstance(outcome, InvalidEvidence)
            and "different root goal" in outcome.reason
        )
    tail_artifact = _edit(
        replay.build_artifact(
            FRONT_TAIL, tail.FrontEvidence(Fraction(1), FRONT_RADII), LIMITS, {}
        ),
        lambda d: d.update(evidence=evidence),
    )
    outcome = replay.replay_stream(tail_artifact, FRONT_TAIL, LIMITS)
    assert (
        isinstance(outcome, InvalidEvidence)
        and "only its radius ρ and radii r" in outcome.reason
    )


def test_a_band_refutation_carries_its_point_and_side(no_lean) -> None:
    above = _window(ABOVE)
    artifact = replay.build_artifact(ABOVE, above, LIMITS, {"stage": "test"})
    document = json.loads(artifact)
    assert document["status"] == "REFUTED"
    assert document["evidence"]["point"] == {"t": [1, 6], "x": [-1, 8]}
    assert document["evidence"]["side"] == "above"
    source = document["source"]
    assert "def forsetiPoint : Fin 2 → ℚ := ![(mkRat (1) 6), (mkRat (-1) 8)]" in source
    assert "ColeHopf.front_above_of_window (hi := ((mkRat (27) 50) : ℚ))" in source
    assert "fun a unused v rel x hx => (h a unused v rel x hx).2.2" in source
    assert "HasSum (Gimle.Asgard.Streams.seriesTerm .ogf a x)" in source
    assert "theorem forsetiSearch : ¬ forsetiRoot" in source
    for change, reason in (
        (lambda d: d["evidence"].update(side="sideways"), "above or below"),
        (lambda d: d["evidence"].update(side="below"), "retained source"),
        (lambda d: d["evidence"]["point"].update(x=[-1, 4]), "in the claim's box"),
        (lambda d: d["evidence"]["point"].update(t=[1, 1]), "in the claim's box"),
        (lambda d: d["evidence"]["point"].update(y=[0, 1]), "per axis"),
        (lambda d: d["evidence"].pop("side"), "carries its radius ρ"),
        (lambda d: d["evidence"].pop("point"), "carries its radius ρ"),
        (
            lambda d: (d["evidence"].pop("point"), d["evidence"].pop("side")),
            "does not match the kind of evidence",
        ),
        (lambda d: d.update(status="PROVEN"), "different theorem"),
    ):
        outcome = replay.replay_stream(_edit(artifact, change), ABOVE, LIMITS)
        assert isinstance(outcome, InvalidEvidence), (reason, outcome)
        assert reason in outcome.reason, (reason, outcome.reason)
    below = _window(BELOW)
    source = json.loads(replay.build_artifact(BELOW, below, LIMITS, {}))["source"]
    assert "ColeHopf.front_below_of_window (lo := ((mkRat (11) 20) : ℚ))" in source
    assert "frontValue_eq" not in source and "linarith" not in source
    assert "fun a unused v rel x hx => (h a unused v rel x hx).2" in source
    outcome = replay.replay_stream(
        _edit(
            replay.build_artifact(BELOW, below, LIMITS, {}),
            lambda d: d["evidence"].update(side="above"),
        ),
        BELOW,
        LIMITS,
    )
    assert (
        isinstance(outcome, InvalidEvidence)
        and "one the claim bounds" in outcome.reason
    )
    upper = json.loads(replay.build_artifact(UPPER, _window(UPPER), LIMITS, {}))[
        "source"
    ]
    assert "a unused v rel x hx).2" in upper and "(lo := ((mkRat (" in upper
    assert (
        "Gimle.Asgard.Streams.analyticField .ogf a x ≤ (((mkRat (3) 5) : ℚ) : ℝ)\n"
        in upper
    )
    two_sided = band_goal(front_band(Fraction(11, 20), Fraction(3, 5)))
    source = json.loads(
        replay.build_artifact(two_sided, _window(two_sided), LIMITS, {})
    )["source"]
    assert "fun a unused v rel x hx => (h a unused v rel x hx).2.1" in source


def test_band_templates_refuse_what_does_not_fit() -> None:
    proof, above, below = _window(BAND), _window(ABOVE), _window(BELOW)
    with pytest.raises(ValueError, match="band claim"):
        render.prove_band(FRONT_TAIL, proof)
    with pytest.raises(ValueError, match="band claim"):
        render.refute_band(heat_goal(field_claim(band(3))), above)
    with pytest.raises(ValueError, match="no refutation point"):
        render.prove_band(BAND, above)
    with pytest.raises(ValueError, match="point and a side"):
        render.refute_band(ABOVE, proof)
    with pytest.raises(ValueError, match="upper bound"):
        render.refute_band(band_goal(front_band(Fraction(2, 5), None)), above)
    with pytest.raises(ValueError, match="lower bound"):
        render.refute_band(UPPER, below)
    with pytest.raises(ValueError, match="does not fit"):
        replay.application(FRONT_TAIL, BURGERS, proof)
    with pytest.raises(ValueError, match="does not fit"):
        replay.build_artifact(
            BAND, tail.FrontEvidence(Fraction(1), FRONT_RADII), LIMITS, {}
        )
    with pytest.raises(ValueError, match="does not fit"):
        replay.build_artifact(FRONT_TAIL, proof, LIMITS, {})
    assert replay.refutes(above) and not replay.refutes(proof)
    assert not replay.refutes(tail.FrontEvidence(Fraction(1), FRONT_RADII))


def test_the_window_stage_reports_its_bound_or_point(monkeypatch) -> None:
    outcome, evidence = service._band(BAND, LIMITS)
    assert outcome.outcome == "found" and evidence == _window(BAND)
    assert outcome.detail == (
        "ρ = 1, inverse radii (r_t, r_x) = (2/3, 1/2), window 16 × 16, certified "
        "error 1/16384; the window bounds u within [≈ 0.4477, ≈ 0.5523] on the box"
    )
    outcome, _ = service._band(ABOVE, LIMITS)
    assert outcome.detail.endswith(
        "; the window at (1/6, -1/8) is above the band by more than the certified error"
    )
    monkeypatch.setattr(
        service, "replay_stream", lambda *a, **k: pytest.fail("nothing to check")
    )
    result = service.decide_stream(NARROW)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.EXHAUSTED
    assert (
        result.stages[-1].stage == "window" and "≈ 0.5523" in result.stages[-1].detail
    )


# --- the Euler radius lane (217) ------------------------------------------------

EULER_TAIL = vorticity_goal(trig_truncation(Fraction(1, 100)))
EULER_BAND = vorticity_goal(trig_band(Fraction(-31, 10), Fraction(31, 10)))
_U_FIELD = Var(2)


def test_the_picard_mirror_gives_the_pages_norms() -> None:
    """The ℓ¹ norms asgard-lean's EulerBand bounds by 8/5 and 223/260, exactly."""
    assert trig.picard_norms(EULER_START, 3) == (
        Fraction(3),
        Fraction(8, 5),
        Fraction(223, 260),
    )
    assert trig.geometric(EULER_START) == (Fraction(9), Fraction(648))
    assert trig.largest_size(EULER_START) == 3 and trig.weight(EULER_START) == 3
    assert trig.tail_bound(Fraction(9), Fraction(648), EULER_RADIUS, 3) == Fraction(
        1, 100
    )


def test_the_zero_mode_transports_nothing() -> None:
    """``coupling 0 q`` is ``0 / 0 = 0`` in Lean; the mirror must not divide."""
    start = Modes(
        ((0, 0, Fraction(1)), (1, 0, Fraction(1, 2)), (-1, 0, Fraction(1, 2)))
    )
    norms = trig.picard_norms(start, 3)
    assert norms[0] == 2 and all(value >= 0 for value in norms)


def test_euler_goals_round_trip_through_json() -> None:
    for goal in (EULER_TAIL, EULER_BAND, vorticity_goal(trig_band(None, Fraction(4)))):
        encoded = encode_goal(goal)
        assert encoded["profile"] == {
            "modes": [
                [k1, k2, [c.numerator, c.denominator]]
                for k1, k2, c in EULER_START.terms
            ]
        }
        assert decode_goal(json.loads(json.dumps(encoded)), LIMITS) == goal
    assert encode_goal(EULER_TAIL)["claim"]["kind"] == "trig-truncation"
    assert encode_goal(EULER_BAND)["claim"]["kind"] == "trig-band"


def _start(modes) -> StreamGoal:
    return vorticity_goal(trig_truncation(Fraction(1, 100)), start=modes)


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda: _start(Modes(())), "bounded list of modes"),
        (
            lambda: _start(Modes(tuple((k, 0, Fraction(1)) for k in range(65)))),
            "bounded list of modes",
        ),
        (lambda: _start(Modes(((65, 0, Fraction(1)),))), "bounded list of modes"),
        (lambda: _start(Modes(((1, 0, 1),))), "bounded list of modes"),
        (lambda: _start(Modes(((1, 0, Fraction(1)), (1, 0, Fraction(1))))), "distinct"),
        (
            lambda: vorticity_goal(trig_truncation(Fraction(1), window=-1)),
            "bounded natural number",
        ),
        (
            lambda: vorticity_goal(trig_truncation(Fraction(1), radius=Fraction(-1))),
            "nonnegative",
        ),
        (
            lambda: vorticity_goal(TrigTruncationClaim("u", 3, EULER_RADIUS, 1)),
            "ε is a fraction",
        ),
        (
            lambda: vorticity_goal(trig_band(None, Fraction(3), radius=Fraction(-1))),
            "nonnegative",
        ),
        (
            lambda: vorticity_goal(trig_truncation(Fraction(1)), viscosity=None),
            "names its viscosity",
        ),
    ],
)
def test_malformed_euler_goals_are_refused(make, match) -> None:
    with pytest.raises(GoalError, match=match):
        make()


@pytest.mark.parametrize(
    ("field", "bad", "match"),
    [
        ("profile", {"modes": []}, "bounded, non-empty"),
        ("profile", {"modes": "x"}, "bounded, non-empty"),
        ("profile", {"modes": [[1, 0]]}, "a mode is"),
        ("profile", {"modes": [[1.0, 0, [1, 2]]]}, "a mode is"),
        ("profile", {"modes": [[True, 0, [1, 2]]]}, "a mode is"),
        (
            "claim",
            {
                "kind": "trig-truncation",
                "port": "u",
                "window": "3",
                "radius": [1, 6480],
                "epsilon": [1, 100],
            },
            "window",
        ),
        (
            "claim",
            {"kind": "trig-truncation", "port": "u", "window": 3, "radius": [1, 6480]},
            "trig-truncation",
        ),
        ("claim", {"kind": "trig-band", "port": "u", "radius": [1, 6480]}, "trig-band"),
    ],
)
def test_malformed_euler_json_is_refused(field, bad, match) -> None:
    document = encode_goal(EULER_TAIL)
    document[field] = bad
    with pytest.raises(GoalError, match=match):
        decode_goal(document, LIMITS)


def test_euler_goals_are_held_to_the_coefficient_bit_limit() -> None:
    from glitnir_forseti.streams.goal import check_goal_limits

    huge = _start(Modes(((1, 0, Fraction(2**300)), (-1, 0, Fraction(1)))))
    with pytest.raises(GoalError, match="bit"):
        check_goal_limits(huge, LIMITS)
    tiny = EntailmentLimits(max_coefficient_bits=64)
    over = trig.choose_truncation(
        EULER_START, trig_truncation(Fraction(1, 100), window=64), tiny
    )
    assert isinstance(over, str) and "over the coefficient bit limit" in over
    none = trig.choose_band(
        EULER_START,
        trig_band(None, Fraction(4), radius=Fraction(1, 6481)),
        EntailmentLimits(max_coefficient_bits=8),
    )
    assert isinstance(none, str) and "no window within the coefficient bit" in none
    with pytest.raises(ValueError, match="one norm per degree"):
        trig.BandEvidence(3, Fraction(3), 2, (Fraction(3),))
    with pytest.raises(ValueError, match="no radius is certified"):
        trig.tail_bound(Fraction(9), Fraction(648), Fraction(1, 648), 3)


def test_the_euler_lane_takes_only_what_the_theorems_state() -> None:
    assert replay.supported(EULER_TAIL) is VORTICITY
    assert replay.supported(EULER_BAND) is VORTICITY
    assert replay.imports_for(EULER_TAIL, VORTICITY) == (
        "Gimle.Forseti.Fourier",
        "Gimle.Asgard.Streams.EulerRadius",
        "Gimle.Asgard.Streams.VorticityTable",
    )


@pytest.mark.parametrize(
    ("goal", "reason"),
    [
        (replace(EULER_TAIL, viscosity=Fraction(1, 10)), "positive viscosity"),
        (
            replace(EULER_TAIL, claim=trig_truncation(Fraction(1), window=0)),
            "at least one degree",
        ),
        (replace(EULER_TAIL, viscosity=Fraction(-1)), "not negative"),
        (replace(EULER_TAIL, basis="egf"), "OGF stream"),
        (
            replace(EULER_TAIL, claim=replace(EULER_TAIL.claim, port="v")),
            "about the vorticity u",
        ),
        (replace(EULER_TAIL, interpretation="trig-band"), "read as trig-truncation"),
        (replace(EULER_BAND, interpretation="trig-truncation"), "read as trig-band"),
        (
            replace(EULER_TAIL, interpretation="analytic-tail"),
            "read as trig-truncation",
        ),
        (replace(EULER_TAIL, profile=EXP), "declares its start as modes"),
        (
            replace(EULER_TAIL, claim=front_truncation(Fraction(1, 100))),
            "one-axis truncation",
        ),
        (
            vorticity_goal(
                TrigBandClaim(
                    "u", EULER_RADIUS, Atom(Relation.LE, Add(_U_FIELD, Neg(Var(0))))
                )
            ),
            "is a band",
        ),
        (
            vorticity_goal(
                trig_truncation(Fraction(1, 100)), start=Modes(((0, 0, Fraction(1)),))
            ),
            "zero mode",
        ),
        (
            replace(
                heat_goal(field_claim(band(3))),
                profile=EULER_START,
                viscosity=Fraction(0),
            ),
            "no trigonometric lane",
        ),
        (
            replace(EULER_TAIL, model="burgers", viscosity=Fraction(1, 2)),
            "no trigonometric lane",
        ),
    ],
)
def test_unsupported_euler_goals_are_named(goal, reason) -> None:
    found = replay.supported(goal)
    assert isinstance(found, str) and reason in found, found
    result = service.decide_stream(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED


def test_the_producer_chooses_nothing_it_cannot_certify() -> None:
    found = trig.choose_truncation(EULER_START, EULER_TAIL.claim, LIMITS)
    assert found == trig.RadiusEvidence(3, Fraction(3))
    tight = trig.choose_truncation(
        EULER_START, trig_truncation(Fraction(1, 1000)), LIMITS
    )
    assert isinstance(tight, str) and "below the certified bound 1/100" in tight
    at_radius = trig.choose_truncation(
        EULER_START, trig_truncation(Fraction(1, 100), radius=Fraction(1, 648)), LIMITS
    )
    assert isinstance(at_radius, str) and "not below the certified radius" in at_radius
    wide = trig.choose_band(EULER_START, EULER_BAND.claim, LIMITS)
    assert wide == trig.BandEvidence(
        3, Fraction(3), 3, (Fraction(3), Fraction(8, 5), Fraction(223, 260))
    )
    upper = trig.choose_band(EULER_START, trig_band(None, Fraction(4)), LIMITS)
    assert isinstance(upper, trig.BandEvidence) and upper.window == 1
    narrow = trig.choose_band(EULER_START, trig_band(None, Fraction(3)), LIMITS)
    assert isinstance(narrow, str) and "evaluates the field at no point" in narrow
    assert isinstance(
        trig.choose_band(EULER_START, trig_band(Fraction(-3), None), LIMITS), str
    )
    at_radius = trig.choose_band(
        EULER_START, trig_band(None, Fraction(4), radius=Fraction(1, 648)), LIMITS
    )
    assert isinstance(at_radius, str) and "not below the certified radius" in at_radius
    zero = Modes(((0, 0, Fraction(1)),))
    assert "zero mode" in str(
        trig.choose_band(zero, trig_band(None, Fraction(4)), LIMITS)
    )
    assert "zero mode" in str(trig.choose_truncation(zero, EULER_TAIL.claim, LIMITS))
    tight = service.decide_stream(vorticity_goal(trig_truncation(Fraction(1, 1000))))
    assert tight.status is service.Status.UNKNOWN
    assert tight.reason is service.UnknownReason.EXHAUSTED


def test_the_euler_lanes_report_their_constants(monkeypatch) -> None:
    """The producers name the constants Lean will decide, and the stage names.

    With no Lean project configured, the radius lane stops at the replay; a
    configured project (as on CI's plugin-live job) would prove the claim.
    """
    monkeypatch.delenv("FORSETI_LEAN_PROJECT", raising=False)
    outcome, found = service._trig_truncation(EULER_TAIL, LIMITS)
    assert found == trig.RadiusEvidence(3, Fraction(3))
    assert "ρ = 72LK = 648" in outcome.detail
    assert "certified radius 1/ρ = 1/648" in outcome.detail
    outcome, found = service._trig_band(EULER_BAND, LIMITS)
    assert isinstance(found, trig.BandEvidence) and found.window == 3
    assert "window 3, norms 3, 8/5, 223/260" in outcome.detail
    result = service.decide_stream(EULER_TAIL)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is not None and result.reason.value == "lean-unavailable"
    assert [stage.stage for stage in result.stages] == ["binding", "radius"]
    result = service.decide_stream(EULER_BAND)
    assert [stage.stage for stage in result.stages] == ["binding", "spread"]
    exhausted = service.decide_stream(vorticity_goal(trig_band(None, Fraction(3))))
    assert exhausted.status is service.Status.UNKNOWN
    assert exhausted.reason is service.UnknownReason.EXHAUSTED
    assert "≈ 3.0012" in exhausted.stages[-1].detail


def test_zero_weight_euler_truncation_still_requires_replay(monkeypatch) -> None:
    """A zero-growth bound is valid; its diagnostic must not divide by zero."""
    goal = vorticity_goal(
        trig_truncation(Fraction(1)), start=Modes(((1, 0, Fraction(0)),))
    )
    checked = []

    def unavailable(artifact, original, *args, **kwargs):
        checked.append(original)
        return ReplayUnavailable("test checker unavailable")

    monkeypatch.setattr(service, "replay_stream", unavailable)
    result = service.decide_stream(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.LEAN_UNAVAILABLE
    assert checked == [goal]
    outcome, evidence = service._trig_truncation(goal, LIMITS)
    assert evidence == trig.RadiusEvidence(1, Fraction(0))
    assert "zero growth" in outcome.detail


def test_an_euler_band_stops_at_the_first_sufficient_window(monkeypatch) -> None:
    """A degree-one band must not calculate any transport coefficients."""

    def forbidden(*args, **kwargs):
        raise AssertionError("the first window was already sufficient")

    monkeypatch.setattr(trig, "_transport", forbidden)
    evidence = trig.choose_band(EULER_START, trig_band(None, Fraction(4)), LIMITS)
    assert evidence == trig.BandEvidence(3, Fraction(3), 1, (Fraction(3),))


@pytest.mark.parametrize("stop", ["timeout", "cancelled"])
def test_euler_band_interrupts_during_convolution(monkeypatch, stop) -> None:
    """Interrupt the producer after it enters a convolution, before replay."""
    cancel = threading.Event()
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    cross = trig._cross
    calls = []

    def interrupt(p, q):
        calls.append((p, q))
        if stop == "cancelled":
            cancel.set()
        else:
            now[0] = 1.0
        return cross(p, q)

    def forbidden(*args, **kwargs):
        raise AssertionError("an interrupted producer must not reach replay")

    monkeypatch.setattr(trig, "_cross", interrupt)
    monkeypatch.setattr(service, "replay_stream", forbidden)
    result = service.decide_stream(
        EULER_BAND, replace(LIMITS, wall_time_ms=10), cancel=cancel
    )
    assert calls
    assert result.status is service.Status.UNKNOWN
    assert result.reason.value == stop
    assert result.stages[-1].outcome == stop


def test_euler_band_bounds_intermediate_coefficients(monkeypatch) -> None:
    """Over-limit rational arithmetic stops inside the producer."""

    def large(p, q):
        return 2**300

    def forbidden(*args, **kwargs):
        raise AssertionError("over-limit coefficients must not reach replay")

    monkeypatch.setattr(trig, "_cross", large)
    monkeypatch.setattr(service, "replay_stream", forbidden)
    result = service.decide_stream(EULER_BAND)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.RESOURCE_LIMIT


def test_euler_band_bounds_mode_tables(monkeypatch) -> None:
    """The table size is bounded while convolution introduces new modes."""
    calls = []
    cross = trig._cross

    def record(p, q):
        calls.append((p, q))
        return cross(p, q)

    def forbidden(*args, **kwargs):
        raise AssertionError("over-limit tables must not reach replay")

    monkeypatch.setattr(service, "replay_stream", forbidden)
    monkeypatch.setattr(trig, "_cross", record)
    result = service.decide_stream(EULER_BAND, replace(LIMITS, max_monomials=6))
    assert calls
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.RESOURCE_LIMIT


def test_an_euler_artifact_carries_the_starts_constants(no_lean) -> None:
    evidence = trig.RadiusEvidence(3, Fraction(3))
    artifact = replay.build_artifact(EULER_TAIL, evidence, LIMITS, {"stage": "test"})
    document = json.loads(artifact)
    assert document["evidence"] == {"kind": "euler-radius", "size": 3, "weight": [3, 1]}
    assert document["environment"]["imports"] == [
        "Gimle.Forseti.Fourier",
        "Gimle.Asgard.Streams.EulerRadius",
        "Gimle.Asgard.Streams.VorticityTable",
    ]
    source = document["source"]
    assert "def forsetiPost (output : Gimle.Asgard.Streams.Fourier.Point 1)" in source
    assert "Gimle.Forseti.Fourier.Contract" in source
    assert "NS.vorticityCircuit .ogf (mkRat (0) 1)" in source
    assert "fun input => input 0 0 = forsetiStart 0" in source
    assert "TruncationBound (output 0)" in source
    assert "Gimle.Forseti.Fourier.solution_contract" in source
    assert "noncomputable def forsetiStart" in source
    assert "NS.euler_l1_geometric forsetiStart (K := 3)" in source
    assert (
        "tailBound (mkRat (9) 1) (mkRat (648) 1) (mkRat (1) 6480) 3 ≤ (mkRat (1) 100)"
        in source
    )
    band_evidence = trig.BandEvidence(
        3, Fraction(3), 3, (Fraction(3), Fraction(8, 5), Fraction(223, 260))
    )
    document = json.loads(
        replay.build_artifact(EULER_BAND, band_evidence, LIMITS, {"stage": "test"})
    )
    assert document["evidence"] == {
        "kind": "euler-band",
        "size": 3,
        "weight": [3, 1],
        "window": 3,
        "norms": [[3, 1], [8, 5], [223, 260]],
    }
    source = document["source"]
    assert "analyticField (output 0) t x" in source
    assert "Gimle.Forseti.Fourier.Contract" in source
    assert "abs_analyticField_le (x := x) geo" in source and "ht 3" in source
    assert (
        source.count("stream_eq_table (mkRat (0) 1) forsetiTable forsetiStart rfl") == 3
    )
    assert "def forsetiBound : ℚ := (mkRat (" in source


@pytest.mark.parametrize(
    ("goal", "evidence", "match"),
    [
        (
            EULER_TAIL,
            {"kind": "euler-band", "size": 3, "weight": [3, 1]},
            "size and weight",
        ),
        (
            EULER_TAIL,
            {"kind": "euler-radius", "size": 2, "weight": [3, 1]},
            "largest mode size",
        ),
        (
            EULER_TAIL,
            {"kind": "euler-radius", "size": 3, "weight": [2, 1]},
            "absolute sum",
        ),
        (
            EULER_TAIL,
            {"kind": "euler-radius", "size": 3, "weight": [3, 1], "x": 1},
            "size and weight",
        ),
        (
            EULER_BAND,
            {
                "kind": "euler-band",
                "size": 3,
                "weight": [3, 1],
                "window": 5,
                "norms": [[3, 1]] * 5,
            },
            "between 1 and 4",
        ),
        (
            EULER_BAND,
            {
                "kind": "euler-band",
                "size": 3,
                "weight": [3, 1],
                "window": 2,
                "norms": [[3, 1]],
            },
            "one norm per degree",
        ),
        (
            EULER_BAND,
            {
                "kind": "euler-band",
                "size": 3,
                "weight": [3, 1],
                "window": 1,
                "norms": [[-3, 1]],
            },
            "not negative",
        ),
        (
            EULER_BAND,
            {"kind": "euler-radius", "size": 3, "weight": [3, 1]},
            "window and norms",
        ),
        (
            EULER_TAIL,
            {"kind": "euler-radius", "size": "3", "weight": [3, 1]},
            "largest mode size",
        ),
        (
            EULER_BAND,
            {
                "kind": "euler-band",
                "size": 3,
                "weight": [3, 1],
                "window": "1",
                "norms": [[3, 1]],
            },
            "between 1 and 4",
        ),
        (
            EULER_BAND,
            {
                "kind": "euler-band",
                "size": 3,
                "weight": [3, 1],
                "window": 1,
                "norms": "x",
            },
            "one norm per degree",
        ),
    ],
)
def test_euler_evidence_is_decoded_strictly(goal, evidence, match) -> None:
    with pytest.raises(replay.EvidenceError, match=match):
        replay._decode_evidence(evidence, goal, VORTICITY, LIMITS)


def test_euler_evidence_that_fits_decodes_to_its_value() -> None:
    assert replay._decode_evidence(
        {"kind": "euler-radius", "size": 3, "weight": [3, 1]},
        EULER_TAIL,
        VORTICITY,
        LIMITS,
    ) == trig.RadiusEvidence(3, Fraction(3))
    decoded = replay._decode_evidence(
        {
            "kind": "euler-band",
            "size": 3,
            "weight": [3, 1],
            "window": 1,
            "norms": [[3, 1]],
        },
        EULER_BAND,
        VORTICITY,
        LIMITS,
    )
    assert decoded == trig.BandEvidence(3, Fraction(3), 1, (Fraction(3),))


def test_the_euler_templates_refuse_what_the_lane_does_not_take() -> None:
    with pytest.raises(ValueError, match="ν = 0"):
        render.prove_trig_truncation(
            replace(EULER_TAIL, viscosity=Fraction(1, 10)),
            trig.RadiusEvidence(3, Fraction(3)),
        )
    with pytest.raises(ValueError, match="ν = 0"):
        render.prove_trig_band(
            EULER_TAIL, trig.BandEvidence(3, Fraction(3), 1, (Fraction(3),))
        )
    with pytest.raises(ValueError, match="does not fit"):
        replay.application(
            EULER_TAIL, VORTICITY, trig.BandEvidence(3, Fraction(3), 1, (Fraction(3),))
        )
    lower = vorticity_goal(trig_band(Fraction(-4), None))
    source = json.loads(
        replay.build_artifact(
            lower, trig.BandEvidence(3, Fraction(3), 1, (Fraction(3),)), LIMITS, {}
        )
    )["source"]
    assert "linarith [hb.1]" in source and "hb.2" not in source
    with pytest.raises(ValueError, match="OGF stream"):
        render.prove_trig_truncation(
            replace(EULER_TAIL, basis="egf"), trig.RadiusEvidence(3, Fraction(3))
        )


def test_a_hostile_euler_artifact_at_the_certified_radius_is_invalid(no_lean) -> None:
    """At ``ρ r = 1`` no bound exists; replay refuses before Lean, no crash."""
    at_radius = vorticity_goal(trig_band(None, Fraction(4), radius=Fraction(1, 648)))
    fine = replay.build_artifact(
        vorticity_goal(trig_band(None, Fraction(4))),
        trig.BandEvidence(3, Fraction(3), 1, (Fraction(3),)),
        LIMITS,
        {},
    )
    document = json.loads(fine)
    document["goal"] = encode_goal(at_radius)
    outcome = replay.replay_stream(json.dumps(document).encode(), at_radius, LIMITS)
    assert isinstance(outcome, InvalidEvidence), outcome


def test_the_euler_command_report_names_the_modes_and_the_series() -> None:
    from glitnir_forseti.streams.__main__ import _profile, _solution_class

    assert _profile(EULER_TAIL).startswith(
        "1/2·e^(i(x)) + 1/2·e^(i(-x)) + 1/2·e^(i(x + y))"
    )
    assert "the claim here is only the bound" in _solution_class(EULER_BAND)
