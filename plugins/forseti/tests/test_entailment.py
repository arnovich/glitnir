"""Tests for entailment search and replay that need no Lean (task 111).

The exact encoding, limits, rendering, producers and the replay's binding
checks run here. Z3 runs for real where it is installed, in its worker
process; Lean's own checks are in ``test_entailment_lean.py``.
"""

import json
import os
import subprocess
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

import pytest

from glitnir_forseti.entailment import corpus, render, replay, search, service
from glitnir_forseti.entailment.algebraic import (
    AlgebraicRoot,
    AlgebraicWitness,
    AtomProof,
    decode_algebraic,
    encode_algebraic,
)
from glitnir_forseti.entailment.algebraic_search import (
    build_witness,
    from_model,
    root_checks,
)
from glitnir_forseti.entailment.artifact import build_artifact, environment_identity
from glitnir_forseti.entailment.evidence import (
    Certificate,
    EvidenceError,
    PsatzCertificate,
    Row,
    SquareSum,
    Witness,
    augment,
    check_certificate,
    check_psatz,
    decode_evidence,
    encode_certificate,
    encode_psatz,
    encode_witness,
    nonstrict,
)
from glitnir_forseti.entailment.limits import EntailmentLimits, LimitError
from glitnir_forseti.entailment.polynomial import ExpansionLimit, expand
from glitnir_forseti.entailment.search import LaneOutcome, decode_dictionary
from glitnir_forseti.entailment.solver import WorkerStop, run_worker
from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Context,
    DecodeBudget,
    Fls,
    Goal,
    GoalError,
    Mul,
    Neg,
    Not,
    Or,
    Relation,
    Tru,
    Var,
    check_goal_limits,
    decode_goal,
    decode_poly,
    encode_goal,
)
from glitnir_forseti.lean.closure import ClosureObservation, digest_closure
from glitnir_forseti.lean.independent import IndependentCheck, IndependentStatus

LIMITS = EntailmentLimits()
ONE = Const(Fraction(1))
X = Var(0)

needs_z3 = pytest.mark.skipif(
    subprocess.run([sys.executable, "-c", "import z3"], capture_output=True).returncode
    != 0,
    reason="requires the z3 extra",
)


# --- syntax and encoding ---------------------------------------------------


@pytest.mark.parametrize("case", corpus.CASES, ids=lambda case: case.name)
def test_goals_round_trip_through_the_versioned_encoding(case) -> None:
    document = json.loads(json.dumps(encode_goal(case.goal)))
    assert decode_goal(document, LIMITS) == case.goal


def test_rationals_round_trip_exactly() -> None:
    value = Fraction(-(2**200) + 1, 3**50)
    goal = Goal(Context(("x",)), Atom(Relation.LE, Const(value)), Tru())
    decoded = decode_goal(json.loads(json.dumps(encode_goal(goal))), LIMITS)
    assert decoded.antecedent.polynomial.value == value  # type: ignore[union-attr]


def _goal_document(antecedent, names=("x",)) -> dict:
    return {
        "schema": "gimle.forseti.entailment-goal/v1",
        "context": list(names),
        "antecedent": antecedent,
        "consequent": ["true"],
    }


@pytest.mark.parametrize(
    "antecedent",
    [
        ["atom", "le", ["var", 1]],  # coordinate outside the context
        ["atom", "le", ["var", -1]],
        ["atom", "le", ["var", True]],
        ["atom", "le", ["const", [2, 4]]],  # not in lowest terms
        ["atom", "le", ["const", [1, -2]]],
        ["atom", "le", ["const", [1, 0]]],
        ["atom", "le", ["const", [1.5, 2]]],
        ["atom", "le", ["const", [True, 1]]],
        ["atom", "le", ["const", "1/2"]],
        ["atom", "lte", ["var", 0]],
        ["atom", "le", ["pow", ["var", 0], 2]],
        ["and", ["true"]],
        ["true", "extra"],
        "true",
        [],
    ],
)
def test_malformed_encodings_are_refused(antecedent) -> None:
    with pytest.raises(GoalError):
        decode_goal(_goal_document(antecedent), LIMITS)


@pytest.mark.parametrize(
    "names", [[], ["x", "x"], [""], ["1x"], ['x"'], ["x y"], ["é"], ["x" * 65]]
)
def test_names_that_could_not_be_rendered_safely_are_refused(names) -> None:
    with pytest.raises(GoalError):
        decode_goal(_goal_document(["true"], names), LIMITS)


def test_decoding_refuses_extra_keys_and_other_schemas() -> None:
    document = _goal_document(["true"])
    with pytest.raises(GoalError):
        decode_goal({**document, "note": 1}, LIMITS)
    with pytest.raises(GoalError):
        decode_goal({**document, "schema": "gimle.forseti.entailment-goal/v2"}, LIMITS)


def test_limits_bound_nodes_depth_variables_and_bits() -> None:
    deep: list = ["var", 0]
    for _ in range(70):
        deep = ["neg", deep]
    with pytest.raises(GoalError, match="nested"):
        decode_goal(_goal_document(["atom", "le", deep]), LIMITS)
    wide: list = ["var", 0]
    for _ in range(40):
        wide = ["add", wide, wide]
        if len(json.dumps(wide)) > 100_000:
            break
    with pytest.raises(GoalError):
        decode_goal(_goal_document(["atom", "le", wide]), LIMITS)
    with pytest.raises(GoalError, match="variables"):
        decode_goal(_goal_document(["true"], [f"x{i}" for i in range(9)]), LIMITS)
    with pytest.raises(GoalError, match="bit"):
        decode_goal(_goal_document(["atom", "le", ["const", [2**300, 1]]]), LIMITS)
    goal = Goal(Context(("x",)), Atom(Relation.LE, Const(Fraction(2**300))), Tru())
    with pytest.raises(GoalError, match="bit"):
        check_goal_limits(goal, LIMITS)


def test_limits_are_validated_and_round_trip() -> None:
    assert EntailmentLimits.from_dict(LIMITS.as_dict()) == LIMITS
    for bad in (0, -1, True, 1.5, 10**9):
        with pytest.raises(LimitError):
            EntailmentLimits(max_degree=bad)  # type: ignore[arg-type]
    with pytest.raises(LimitError):
        EntailmentLimits.from_dict({"max_degree": 3})


def test_goals_refuse_coordinates_outside_their_context() -> None:
    with pytest.raises(GoalError):
        Goal(Context(("x",)), Atom(Relation.LE, Var(1)), Tru())
    with pytest.raises(GoalError):
        Goal(Context(("x",)), Atom("le", X), Tru())  # type: ignore[arg-type]


# --- the positive fragment and polynomials ---------------------------------


def test_nonstrict_mirrors_the_lean_reading() -> None:
    p, q = Add(X, ONE), Add(X, Neg(ONE))
    assert nonstrict(Tru()) == []
    assert nonstrict(Atom(Relation.LE, p)) == [p]
    assert nonstrict(Atom(Relation.GE, p)) == [Neg(p)]
    assert nonstrict(And(Atom(Relation.GE, p), And(Tru(), Atom(Relation.LE, q)))) == [
        Neg(p),
        q,
    ]
    for outside in (
        Atom(Relation.LT, p),
        Atom(Relation.EQ, p),
        Or(Tru(), Tru()),
        Not(Tru()),
        And(Tru(), Atom(Relation.GT, p)),
    ):
        assert nonstrict(outside) is None


def test_expansion_is_exact_and_bounded_before_it_runs() -> None:
    square = Mul(Add(X, Const(Fraction(1, 2))), Add(X, Const(Fraction(1, 2))))
    assert expand(square, 1, LIMITS) == {
        (0, 0): Fraction(1),
        (0,): Fraction(1),
        (): Fraction(1, 4),
    }
    assert expand(Add(X, Neg(X)), 1, LIMITS) == {}
    high = X
    for _ in range(5):
        high = Mul(high, high)  # degree 32
    with pytest.raises(ExpansionLimit, match="degree"):
        expand(high, 1, LIMITS)
    wide = Mul(Mul(Var(0), Var(1)), Mul(Var(2), Var(3)))
    with pytest.raises(ExpansionLimit, match="monomials"):
        expand(wide, 8, EntailmentLimits(max_monomials=10))


# --- rendering -------------------------------------------------------------


def test_rendering_is_regenerated_from_data_and_numbers_only() -> None:
    goal = Goal(
        Context(("x", "y")),
        Atom(Relation.GE, Add(Var(1), Const(Fraction(-3, 2)))),
        Or(Atom(Relation.LT, Var(0)), Not(Atom(Relation.EQ, Var(1)))),
    )
    text = render.prelude(goal)
    assert '  ⟨["x", "y"], by decide, by decide, by decide⟩' in text
    assert "open " not in text
    assert "(.atom .ge (.add (.var ⟨1, by decide⟩) (.constant (mkRat (-3) 2))))" in text
    assert "(.or (.atom .lt (.var ⟨0, by decide⟩)) (.not (.atom .eq" in text
    assert text.startswith("import Gimle.Forseti.Syntax.Certificate\n")
    application = render.refute(
        goal, Witness((("x", Fraction(-1)), ("y", Fraction(0))))
    )
    assert application.statement == (
        ": ¬ Gimle.Forseti.Syntax.FormulaEntailment forsetiAntecedent "
        "forsetiConsequent"
    )
    assert application.script == (
        "exact Gimle.Forseti.Syntax.refutes_sound forsetiGoal "
        '[("x", (mkRat (-1) 1)), '
        '("y", (mkRat (0) 1))] (by decide +kernel)',
    )


def test_a_witness_must_name_the_expected_coordinates() -> None:
    with pytest.raises(ValueError):
        render.refute(
            corpus.UNRELATED, Witness((("y", Fraction(1)), ("x", Fraction(0))))
        )


# --- evidence --------------------------------------------------------------

DISK_CERTIFICATE = Certificate(
    (
        Row(SquareSum(((Fraction(1), Var(1)),)), (SquareSum(((Fraction(1), ONE),)),)),
        Row(SquareSum(((Fraction(1), Var(0)),)), (SquareSum(((Fraction(1), ONE),)),)),
    )
)


def test_evidence_round_trips_and_its_shape_is_checked() -> None:
    context = corpus.PLANE
    encoded = json.loads(json.dumps(encode_certificate(DISK_CERTIFICATE)))
    assert decode_evidence(encoded, context, LIMITS) == DISK_CERTIFICATE
    witness = Witness((("x", Fraction(1)), ("y", Fraction(0))))
    encoded = json.loads(json.dumps(encode_witness(witness)))
    assert decode_evidence(encoded, context, LIMITS) == witness
    antecedent = nonstrict(corpus.DISK.antecedent)
    consequent = nonstrict(corpus.DISK.consequent)
    assert antecedent is not None and consequent is not None
    check_certificate(DISK_CERTIFICATE, antecedent, consequent, LIMITS)
    dropped = Certificate((DISK_CERTIFICATE.rows[0],))
    with pytest.raises(EvidenceError, match="row"):
        check_certificate(dropped, antecedent, consequent, LIMITS)
    no_slot = Certificate(tuple(Row(row.base, ()) for row in DISK_CERTIFICATE.rows))
    with pytest.raises(EvidenceError, match="multiplier"):
        check_certificate(no_slot, antecedent, consequent, LIMITS)


@pytest.mark.parametrize(
    "evidence",
    [
        {"kind": "witness", "assignment": [["y", [0, 1]], ["x", [1, 1]]]},
        {"kind": "witness", "assignment": [["x", [1, 1]]]},
        {
            "kind": "witness",
            "assignment": [["x", [1, 1]], ["y", [0, 1]], ["z", [0, 1]]],
        },
        {"kind": "witness", "assignment": [["x", [2, 2]], ["y", [0, 1]]]},
        {"kind": "certificate", "rows": [{"base": [[[1, 1], ["var", 2]]]}]},
        {"kind": "certificate", "rows": [{"base": [], "multipliers": {}}]},
        {"kind": "certificate", "rows": "none"},
        {"kind": "oracle"},
        [],
    ],
)
def test_malformed_evidence_is_refused(evidence) -> None:
    with pytest.raises(EvidenceError):
        decode_evidence(evidence, corpus.PLANE, LIMITS)


def test_dictionaries_decode_within_limits() -> None:
    assert decode_dictionary([["const", [1, 1]], ["var", 0]], corpus.LINE, LIMITS) == (
        ONE,
        X,
    )
    for bad in ([], [["var", 1]], "x", [["const", [1, 1]]] * 17):
        with pytest.raises(GoalError):
            decode_dictionary(bad, corpus.LINE, LIMITS)


# --- the Z3 worker ---------------------------------------------------------


@needs_z3
@pytest.mark.parametrize(
    ("goal", "outcome"),
    [
        (corpus.STRICT, "none"),  # x < 0 ⊨ x ≤ 0 holds: no counterexample
        (Goal(corpus.LINE, Atom(Relation.LE, X), Atom(Relation.LT, X)), "found"),
        (Goal(corpus.LINE, Atom(Relation.EQ, X), Not(Atom(Relation.GT, X))), "none"),
        (Goal(corpus.LINE, Atom(Relation.EQ, X), Not(Atom(Relation.GE, X))), "found"),
        (corpus.ROOT_TWO, "unsupported-witness"),
        (corpus.HALF_DISK, "found"),
    ],
)
def test_refutation_translates_strictness_negation_and_equality(goal, outcome) -> None:
    found = search.refute(goal, LIMITS, time.monotonic() + 30)
    assert found.outcome == outcome
    # An irrational model is carried exactly, for the entailment service only.
    assert bool(found.roots) == (goal == corpus.ROOT_TWO)
    if outcome == "found":
        assert isinstance(found.evidence, Witness)
        # The point is checked exactly, here only as a sanity check.
        point = dict(found.evidence.assignment)
        assert tuple(point) == goal.context.names


@needs_z3
def test_the_certificate_lane_finds_the_corpus_certificates() -> None:
    for goal in (corpus.DISK, corpus.QUARTIC, corpus.BOX, corpus.BOX_THREE_HALVES):
        found = search.certify(goal, LIMITS, time.monotonic() + 30)
        assert found.outcome == "found", goal
    restricted = search.certify(
        corpus.QUARTIC, LIMITS, time.monotonic() + 30, None, (ONE,)
    )
    assert restricted.outcome == "none"
    assert "exhausted" in restricted.detail


def _assert_identity(certificate: Certificate | PsatzCertificate, goal: Goal) -> None:
    """Check −q_j = Σ w·g² + Σ_i Σ w·g²·(−p_i) exactly, as Lean will.

    A Psatz certificate's rows are checked against the augmented antecedent.
    """
    from glitnir_forseti.entailment import polynomial

    antecedent = nonstrict(goal.antecedent)
    consequent = nonstrict(goal.consequent)
    assert antecedent is not None and consequent is not None
    if isinstance(certificate, PsatzCertificate):
        check_psatz(certificate, antecedent, consequent, LIMITS)
        antecedent = augment(antecedent, certificate.pairs)
    else:
        check_certificate(certificate, antecedent, consequent, LIMITS)
    n = goal.context.dimension

    def weighted(sum_) -> dict:
        total: dict = {}
        for weight, g in sum_.terms:
            assert weight >= 0
            square = polynomial.multiply(expand(g, n, LIMITS), expand(g, n, LIMITS))
            total = polynomial.add(total, polynomial.scale(square, weight))
        return total

    for row, q in zip(certificate.rows, consequent):
        total = weighted(row.base)
        for multiplier, p in zip(row.multipliers, antecedent):
            factor = expand(Neg(p), n, LIMITS)
            total = polynomial.add(
                total, polynomial.multiply(weighted(multiplier), factor)
            )
        assert total == expand(Neg(q), n, LIMITS)


def test_the_dsos_dictionary_adds_every_pairwise_sum_and_difference() -> None:
    line = Context(("x",))
    assert search.dsos_dictionary(line) == (
        ONE,
        X,
        Add(ONE, X),
        Add(ONE, Neg(X)),
    )
    plane = Context(("x", "y"))
    assert len(search.dsos_dictionary(plane)) == 3 + 2 * 3


@needs_z3
def test_the_certificate_lane_widens_to_dsos_after_the_default() -> None:
    """x² ≤ 1 ∧ y² ≤ 1 ⊨ x·y ≤ 1 needs a square of x ± y."""
    goal = corpus.UNIT_PRODUCT
    default = search.default_dictionary(goal.context)
    plain = search.certify(goal, LIMITS, time.monotonic() + 30, None, default)
    assert plain.outcome == "none"
    found = search.certify(goal, LIMITS, time.monotonic() + 30)
    assert found.outcome == "found" and "dsos" in found.detail
    assert isinstance(found.evidence, Certificate)
    _assert_identity(found.evidence, goal)
    squares = [
        g
        for row in found.evidence.rows
        for sum_ in (row.base, *row.multipliers)
        for _, g in sum_.terms
    ]
    assert any(g not in default for g in squares)


@needs_z3
def test_a_goal_the_default_proves_keeps_its_default_certificate() -> None:
    found = search.certify(corpus.DISK, LIMITS, time.monotonic() + 30)
    assert found.outcome == "found" and "dsos" not in found.detail
    default = search.default_dictionary(corpus.DISK.context)
    assert isinstance(found.evidence, Certificate)
    assert all(
        g in default
        for row in found.evidence.rows
        for sum_ in (row.base, *row.multipliers)
        for _, g in sum_.terms
    )


@needs_z3
def test_dsos_beyond_the_dictionary_limit_is_skipped_unsearched(monkeypatch) -> None:
    calls = []
    real = search.run_worker
    monkeypatch.setattr(
        search, "run_worker", lambda *a, **k: calls.append(1) or real(*a, **k)
    )
    small = EntailmentLimits(max_dictionary=8)
    found = search.certify(corpus.UNIT_PRODUCT, small, time.monotonic() + 30)
    assert found.outcome == "none"
    assert found.detail.endswith("its 9-square dsos widening exceeds the limits")
    assert len(calls) == 1  # the default dictionary's search only


@needs_z3
def test_dsos_beyond_the_weight_limit_leaves_the_lane_exhausted() -> None:
    # 9 weights for the default dictionary, 27 for the widening.
    small = EntailmentLimits(max_weights=20)
    found = search.certify(corpus.UNIT_PRODUCT, small, time.monotonic() + 30)
    assert found.outcome == "none"
    assert "widening ended resource-limit" in found.detail


@needs_z3
def test_a_widened_search_still_exhausts_beyond_its_reach() -> None:
    found = search.certify(corpus.TRIPLE_PRODUCT, LIMITS, time.monotonic() + 60)
    assert found.outcome == "none"
    assert found.detail.endswith("so is its 16-square dsos widening")


def test_the_widening_gets_a_share_of_the_time_and_is_optional(monkeypatch) -> None:
    """A widening that cannot finish leaves the default's exhaustion standing."""
    deadlines = []
    answers = iter(
        [LaneOutcome("none", "default exhausted"), LaneOutcome("timeout", "late")]
    )

    def fake(context, antecedent, consequent, limits, deadline, cancel, squares):
        deadlines.append(deadline)
        return next(answers)

    monkeypatch.setattr(search, "_search", fake)
    start = time.monotonic()
    found = search.certify(corpus.UNIT_PRODUCT, LIMITS, start + 100)
    assert found.outcome == "none"
    assert found.detail.startswith("default exhausted; ")
    assert "widening ended timeout: late" in found.detail
    assert deadlines[0] == start + 100
    assert deadlines[1] <= start + 100 * search.WIDENING_SHARE + 1


def test_a_cancelled_widening_is_cancelled(monkeypatch) -> None:
    answers = iter([LaneOutcome("none", "exhausted"), LaneOutcome("cancelled", "")])
    monkeypatch.setattr(search, "_search", lambda *a: next(answers))
    found = search.certify(corpus.UNIT_PRODUCT, LIMITS, time.monotonic() + 10)
    assert found.outcome == "cancelled"


@needs_z3
def test_the_three_halves_certificate_uses_the_changed_rational() -> None:
    found = search.certify(corpus.BOX_THREE_HALVES, LIMITS, time.monotonic() + 30)
    assert isinstance(found.evidence, Certificate)
    (row,) = found.evidence.rows
    assert row.base.terms == ((Fraction(1, 2), ONE),)


def test_the_certificate_lane_refuses_what_it_cannot_express() -> None:
    assert search.certify(corpus.STRICT, LIMITS, time.monotonic() + 5).outcome == (
        "unsupported-fragment"
    )
    tight = EntailmentLimits(max_weights=2)
    assert search.certify(corpus.DISK, tight, time.monotonic() + 5).outcome == (
        "resource-limit"
    )
    low = EntailmentLimits(max_degree=3)
    assert search.certify(corpus.QUARTIC, low, time.monotonic() + 5).outcome == (
        "resource-limit"
    )


def _fake_python(tmp_path: Path, body: str) -> str:
    script = tmp_path / "python"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.parametrize(
    ("body", "cause", "seconds"),
    [
        # Only the timeout case needs a short deadline; the others finish at
        # once, and a long one keeps a loaded machine from turning them into
        # timeouts.
        ("sleep 30", "timeout", 1),
        ("head -c 5000000 /dev/zero", "output", 60),
        ("echo not json", "failed", 60),
        ('echo \'{"no": "result"}\'', "failed", 60),
    ],
)
def test_the_worker_is_bounded_and_reaped(
    tmp_path, monkeypatch, live_children, body, cause, seconds
) -> None:
    from glitnir_forseti.entailment import solver

    monkeypatch.setattr(solver.sys, "executable", _fake_python(tmp_path, body))
    started = time.monotonic()
    answer = run_worker({"op": "refute"}, time.monotonic() + seconds)
    assert isinstance(answer, WorkerStop) and answer.cause == cause
    assert time.monotonic() - started < seconds + 10
    assert live_children() == []


def test_the_worker_is_cancelled_and_reaped(
    tmp_path, monkeypatch, live_children
) -> None:
    from glitnir_forseti.entailment import solver

    monkeypatch.setattr(solver.sys, "executable", _fake_python(tmp_path, "sleep 30"))
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    answer = run_worker({"op": "refute"}, time.monotonic() + 30, cancel)
    assert answer == WorkerStop("cancelled")
    assert live_children() == []


def test_missing_z3_is_an_answer(tmp_path, monkeypatch) -> None:
    shadow = tmp_path / "shadow" / "z3"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise ImportError('z3 removed for a test')\n")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join([str(shadow.parent), os.environ.get("PYTHONPATH", "")]),
    )
    found = search.refute(corpus.UNRELATED, LIMITS, time.monotonic() + 30)
    assert found.outcome == "z3-unavailable"
    assert "z3 removed for a test" in found.detail


# --- replay binding, before Lean -------------------------------------------


def _artifact(goal=corpus.DISK, evidence=DISK_CERTIFICATE) -> bytes:
    application = render.application(goal, evidence)
    return build_artifact(goal, evidence, application, LIMITS, {"lane": "test"})


def _edit(artifact: bytes, change) -> bytes:
    document = json.loads(artifact)
    change(document)
    return json.dumps(document).encode()


@pytest.fixture
def no_lean(monkeypatch):
    """Fail the test if replay reaches Lean."""

    def forbidden(*args, **kwargs):
        raise AssertionError("replay reached Lean")

    monkeypatch.setattr(replay, "verify_closure", forbidden)
    monkeypatch.setattr(replay, "check_independently", forbidden)


def _swap_labels(document) -> None:
    document["goal"]["context"] = ["y", "x"]


def _stale_bound(document) -> None:
    document["goal"] = encode_goal(
        Goal(
            corpus.PLANE,
            corpus.DISK.antecedent,
            And(
                Atom(Relation.LE, Add(Mul(Var(0), Var(0)), Neg(Const(Fraction(2))))),
                corpus.DISK.consequent.right,  # type: ignore[union-attr]
            ),
        )
    )


def _drop_slot(document) -> None:
    document["evidence"]["rows"][0]["multipliers"] = []


def _witness_for_proof(document) -> None:
    document["evidence"] = encode_witness(
        Witness((("x", Fraction(1)), ("y", Fraction(0))))
    )


def _other_environment(document) -> None:
    document["environment"]["release_sha256"] = "0" * 64


def _other_theorem(document) -> None:
    document["theorem"]["statement"] = "negation"


def _tampered_source(document) -> None:
    document["source"] = document["source"].replace("(by decide +kernel)", "(by sorry)")


def _injected_source(document) -> None:
    document["source"] += '\n#eval IO.Process.run {cmd := "true"}\n'


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_swap_labels, "different goal"),
        (_stale_bound, "different goal"),
        (_drop_slot, "wrong shape"),
        (_witness_for_proof, "does not match"),
        (_other_environment, "different Lean environment"),
        (_other_theorem, "different theorem"),
        (_tampered_source, "retained source"),
        (_injected_source, "retained source"),
        (lambda d: d.update(extra=1), "v1 fields"),
        (lambda d: d.update(schema="other"), "schema"),
        (lambda d: d.update(status="VALID"), "does not match"),
    ],
)
def test_replay_rejects_a_mismatched_artifact_before_lean(
    no_lean, change, reason
) -> None:
    outcome = replay.replay_artifact(_edit(_artifact(), change), corpus.DISK, LIMITS)
    assert isinstance(outcome, replay.InvalidEvidence)
    assert reason in outcome.reason


def test_replay_takes_the_goal_from_the_caller(no_lean) -> None:
    outcome = replay.replay_artifact(_artifact(), corpus.DISK_REORDERED, LIMITS)
    assert isinstance(outcome, replay.InvalidEvidence)
    assert "different goal" in outcome.reason


@pytest.mark.parametrize(
    "artifact", [b"", b"{", b"[" * 100_000, b"1" * 5_000 + b"x", b"\xff"]
)
def test_replay_refuses_unreadable_bytes(no_lean, artifact) -> None:
    assert isinstance(
        replay.replay_artifact(artifact, corpus.DISK, LIMITS), replay.InvalidEvidence
    )


def test_replay_refuses_an_oversized_artifact(no_lean) -> None:
    small = EntailmentLimits(max_artifact_bytes=100)
    outcome = replay.replay_artifact(_artifact(), corpus.DISK, small)
    assert isinstance(outcome, replay.InvalidEvidence)
    assert "byte limit" in outcome.reason


def _lean(monkeypatch, check, closure=IndependentCheck(IndependentStatus.CONFIRMED)):
    monkeypatch.setattr(replay, "verify_closure", lambda *a, **k: closure)
    monkeypatch.setattr(replay, "check_independently", lambda *a, **k: check)


@pytest.mark.parametrize(
    ("check", "closure", "expected"),
    [
        (
            IndependentCheck(IndependentStatus.CONFIRMED, ("propext", "sorryAx")),
            IndependentCheck(IndependentStatus.CONFIRMED),
            "depends on sorryAx",
        ),
        (
            IndependentCheck(IndependentStatus.REJECTED, reason="did not compile: x"),
            IndependentCheck(IndependentStatus.CONFIRMED),
            "Lean rejected",
        ),
        (
            IndependentCheck(IndependentStatus.UNKNOWN, reason="late", stop="timeout"),
            IndependentCheck(IndependentStatus.CONFIRMED),
            "unavailable",
        ),
        (
            IndependentCheck(IndependentStatus.CONFIRMED),
            IndependentCheck(IndependentStatus.UNKNOWN, reason="mathlib differs"),
            "unavailable",
        ),
    ],
)
def test_replay_maps_lean_outcomes(monkeypatch, check, closure, expected) -> None:
    _lean(monkeypatch, check, closure)
    outcome = replay.replay_artifact(_artifact(), corpus.DISK, LIMITS)
    if expected == "unavailable":
        assert isinstance(outcome, replay.ReplayUnavailable)
    else:
        assert isinstance(outcome, replay.InvalidEvidence)
        assert expected in outcome.reason


def test_replay_validates_with_allowlisted_axioms(monkeypatch) -> None:
    _lean(
        monkeypatch,
        IndependentCheck(IndependentStatus.CONFIRMED, ("propext", "Quot.sound")),
    )
    outcome = replay.replay_artifact(_artifact(), corpus.DISK, LIMITS)
    assert isinstance(outcome, replay.ValidatedEntailment)
    assert outcome.status == "PROVEN" and outcome.goal == corpus.DISK


def test_replay_imports_no_solver() -> None:
    code = (
        "import sys, glitnir_forseti.entailment, glitnir_forseti.entailment.replay, "
        "glitnir_forseti.entailment.__main__\n"
        "bad = [m for m in sys.modules if m == 'z3' or m.startswith('z3.') or "
        "m.split('.')[-1] in ('solver', 'worker', 'search', 'service', "
        "'algebraic_search')]\n"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_environment_identity_names_the_pins() -> None:
    identity = environment_identity()
    assert identity["imports"] == list(render.IMPORTS)
    assert identity["toolchain"].startswith("leanprover/lean4:")
    assert {d["name"] for d in identity["dependencies"]} >= {"asgard", "mathlib"}


# --- the closure digest ----------------------------------------------------


def _library(root: Path, modules: dict[str, bytes]) -> Path:
    library = root / ".lake/build/lib/lean"
    for name, data in modules.items():
        stem = name.replace(".", "/")
        (library / stem).parent.mkdir(parents=True, exist_ok=True)
        (library / f"{stem}.olean").write_bytes(data)
        (root / f"{stem}.lean").parent.mkdir(parents=True, exist_ok=True)
        (root / f"{stem}.lean").write_text(f"-- {name}\n")
    return library


def test_the_closure_digest_covers_every_loaded_file(tmp_path) -> None:
    package = tmp_path / "package"
    toolchain = tmp_path / "toolchain"
    library = _library(package, {"Gimle.A": b"a", "Gimle.B": b"b"})
    (toolchain / "lib/lean/Init").mkdir(parents=True)
    (toolchain / "lib/lean/Init/Core.olean").write_bytes(b"core")
    (toolchain / "bin").mkdir()
    (toolchain / "bin/leanchecker").write_bytes(b"kernel")
    listing = "\n".join(
        [
            f"Init.Core\t{toolchain}/lib/lean/Init/Core.olean",
            f"Gimle.A\t{library}/Gimle/A.olean",
            f"Gimle.B\t{library}/Gimle/B.olean",
        ]
    )
    roots = {"package": package}

    def observe():
        return digest_closure("Gimle.B", listing, roots, toolchain)

    first = observe()
    assert isinstance(first, ClosureObservation)
    assert first.modules == 3
    assert [p[:2] for p in first.packages] == [("package", 2), ("toolchain", 1)]
    for changed in (
        library / "Gimle/A.olean",
        package / "Gimle/B.lean",
        toolchain / "lib/lean/Init/Core.olean",
        toolchain / "bin/leanchecker",
    ):
        original = changed.read_bytes()
        changed.write_bytes(original + b" ")
        assert observe() != first, changed
        changed.write_bytes(original)
    (library / "Gimle/A.olean.private").write_bytes(b"extra")
    assert observe() != first
    changed_extra = observe()
    assert isinstance(changed_extra, ClosureObservation)
    assert changed_extra.packages[1] == first.packages[1]
    # Lean 4.33 and later also load a module's IR signature.
    (library / "Gimle/A.ir.sig").write_bytes(b"signature")
    assert observe() != changed_extra


def test_the_closure_digest_refuses_files_it_cannot_attribute(tmp_path) -> None:
    package = tmp_path / "package"
    library = _library(package, {"Gimle.A": b"a"})
    outside = tmp_path / "elsewhere.olean"
    outside.write_bytes(b"x")
    roots = {"package": package}
    for listing, reason in (
        (f"Gimle.A\t{outside}", "outside"),
        (f"Gimle.B\t{library}/Gimle/A.olean", "another module"),
        ("no tab here", "malformed"),
        ("", "empty"),
    ):
        observed = digest_closure("Gimle.A", listing, roots, tmp_path / "tc")
        assert isinstance(observed, IndependentCheck)
        assert reason in observed.reason
    # A shadow copy in another package could be loaded instead of the one
    # hashed, so a module found twice is refused.
    shadow = tmp_path / "shadow"
    _library(shadow, {"Gimle.A": b"forged"})
    observed = digest_closure(
        "Gimle.A",
        f"Gimle.A\t{library}/Gimle/A.olean",
        {**roots, "shadow": shadow},
        tmp_path / "tc",
    )
    assert isinstance(observed, IndependentCheck)
    assert "more than one package" in observed.reason


def test_closure_hashing_stops_at_the_deadline_and_on_cancel(tmp_path) -> None:
    package = tmp_path / "package"
    library = _library(package, {"Gimle.A": b"a"})
    listing = f"Gimle.A\t{library}/Gimle/A.olean"
    roots = {"package": package}
    late = digest_closure(
        "Gimle.A", listing, roots, tmp_path / "tc", deadline=time.monotonic() - 1
    )
    assert isinstance(late, IndependentCheck) and late.stop == "timeout"
    cancel = threading.Event()
    cancel.set()
    stopped = digest_closure("Gimle.A", listing, roots, tmp_path / "tc", cancel=cancel)
    assert isinstance(stopped, IndependentCheck) and stopped.stop == "cancelled"


# --- the service -----------------------------------------------------------


def _lanes(monkeypatch, refutation: LaneOutcome, certificate: LaneOutcome) -> None:
    monkeypatch.setattr(service, "refute", lambda *a, **k: refutation)
    monkeypatch.setattr(service, "certify_with_products", lambda *a, **k: certificate)


@pytest.mark.parametrize(
    ("refutation", "certificate", "reason"),
    [
        ("unsupported-witness", "unsupported-fragment", "unsupported-witness"),
        ("none", "none", "dictionary-exhausted"),
        ("none", "unsupported-fragment", "unsupported-fragment"),
        ("z3-unavailable", "z3-unavailable", "z3-unavailable"),
        ("z3-unknown", "none", "z3-unknown"),
        ("none", "resource-limit", "resource-limit"),
        ("timeout", "none", "timeout"),
        ("cancelled", "none", "cancelled"),
    ],
)
def test_unknown_reasons_follow_the_lanes(
    monkeypatch, refutation, certificate, reason
) -> None:
    _lanes(monkeypatch, LaneOutcome(refutation), LaneOutcome(certificate))
    result = service.decide_entailment(corpus.DISK)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is not None and result.reason.value == reason
    assert result.artifact is None


def test_found_evidence_is_decided_only_by_replay(monkeypatch) -> None:
    witness = Witness((("x", Fraction(1)), ("y", Fraction(0))))
    _lanes(monkeypatch, LaneOutcome("found", evidence=witness), LaneOutcome("none"))
    seen = []

    def fake_replay(artifact, goal, *args, **kwargs):
        seen.append((json.loads(artifact)["status"], goal))
        return replay.InvalidEvidence("Lean rejected the evidence")

    monkeypatch.setattr(service, "replay_artifact", fake_replay)
    result = service.decide_entailment(corpus.DISK)
    assert seen == [("REFUTED", corpus.DISK)]
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.INVALID_EVIDENCE


def test_a_validated_replay_decides_and_keeps_the_artifact(monkeypatch) -> None:
    _lanes(
        monkeypatch,
        LaneOutcome("none"),
        LaneOutcome("found", evidence=DISK_CERTIFICATE),
    )
    monkeypatch.setattr(
        service,
        "replay_artifact",
        lambda artifact, goal, *a, **k: replay.ValidatedEntailment(
            "PROVEN", goal, DISK_CERTIFICATE, ("propext",)
        ),
    )
    result = service.decide_entailment(corpus.DISK)
    assert result.status is service.Status.PROVEN
    assert result.artifact is not None
    assert json.loads(result.artifact)["goal"] == encode_goal(corpus.DISK)
    assert [lane.lane for lane in result.lanes] == ["refutation", "certificate"]


def test_lean_unavailable_after_a_found_counterexample(monkeypatch) -> None:
    monkeypatch.delenv("FORSETI_LEAN_PROJECT", raising=False)
    witness = Witness((("x", Fraction(1)), ("y", Fraction(0))))
    _lanes(monkeypatch, LaneOutcome("found", evidence=witness), LaneOutcome("none"))
    result = service.decide_entailment(corpus.HALF_DISK)
    assert result.reason is service.UnknownReason.LEAN_UNAVAILABLE
    assert result.lanes[0].check == "unavailable"


def test_a_goal_over_the_limits_is_unknown(monkeypatch) -> None:
    _lanes(monkeypatch, LaneOutcome("none"), LaneOutcome("none"))
    result = service.decide_entailment(corpus.DISK, EntailmentLimits(max_nodes=3))
    assert result.reason is service.UnknownReason.RESOURCE_LIMIT
    assert result.limits["max_nodes"] == 3


def test_a_cancelled_request_does_not_search(monkeypatch) -> None:
    _lanes(monkeypatch, LaneOutcome("found"), LaneOutcome("found"))
    cancel = threading.Event()
    cancel.set()
    result = service.decide_entailment(corpus.DISK, cancel=cancel)
    assert result.reason is service.UnknownReason.CANCELLED


def test_the_decoder_budget_is_shared_across_a_certificate() -> None:
    budget = DecodeBudget(EntailmentLimits(max_nodes=2), 1)
    decode_poly(["var", 0], budget)
    decode_poly(["var", 0], budget)
    with pytest.raises(GoalError):
        decode_poly(["var", 0], budget)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ('{"result": "unknown", "detail": "incomplete quantifiers"}', "z3-unknown"),
        ('{"result": "unknown", "detail": "timeout"}', "timeout"),
        ('{"result": "error", "detail": "Z3Exception: bad"}', "z3-unknown"),
        ('{"result": "sat", "point": [[1, 0]]}', "z3-unknown"),
        ('{"result": "sat", "point": [[1, 1]]}', "z3-unknown"),
        ('{"result": "maybe"}', "z3-unknown"),
    ],
)
def test_backend_answers_are_bounded_and_named(
    tmp_path, monkeypatch, answer, outcome
) -> None:
    from glitnir_forseti.entailment import solver

    fake = _fake_python(tmp_path, f"cat > /dev/null; echo '{answer}'")
    monkeypatch.setattr(solver.sys, "executable", fake)
    found = search.refute(corpus.UNRELATED, LIMITS, time.monotonic() + 10)
    assert found.outcome == outcome
    assert found.evidence is None


def test_weights_from_the_backend_are_bounded(tmp_path, monkeypatch) -> None:
    from glitnir_forseti.entailment import solver

    huge = 2**300
    answer = json.dumps({"result": "sat", "weights": [[huge, 1]] * 6})
    fake = _fake_python(tmp_path, f"cat > /dev/null; echo '{answer}'")
    monkeypatch.setattr(solver.sys, "executable", fake)
    found = search.certify(corpus.BOX, LIMITS, time.monotonic() + 10)
    assert found.outcome == "z3-unknown"
    assert "unusable weights" in found.detail


def test_the_replay_command_reports_invalid_evidence(tmp_path, capsys) -> None:
    from glitnir_forseti.entailment.__main__ import main

    goal = tmp_path / "goal.json"
    goal.write_text(json.dumps(encode_goal(corpus.BOX)))
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(_artifact())
    assert main(["replay", str(artifact), "--goal", str(goal)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "outcome": "invalid",
        "reason": "the artifact is about a different goal",
    }
    goal.write_text('{"schema": "other"}')
    assert main(["replay", str(artifact), "--goal", str(goal)]) == 2


@needs_z3
def test_the_worker_answers_in_process() -> None:
    """The worker's translation, run where coverage sees it."""
    from glitnir_forseti.entailment import worker

    def refute(goal):
        return worker.answer(
            {
                "op": "refute",
                "goal": encode_goal(goal),
                "limits": LIMITS.as_dict(),
                "timeout_ms": 10_000,
            }
        )

    assert refute(corpus.UNRELATED)["result"] == "sat"
    assert refute(corpus.STRICT) == {"result": "unsat"}
    root_two = refute(corpus.ROOT_TWO)
    assert root_two["result"] == "algebraic"
    ((value,),) = [root_two["model"]]
    assert value["polynomial"] == [-2, 0, 1]
    lower, upper = (Fraction(*value[side]) for side in ("lower", "upper"))
    assert lower < upper and lower * lower < 2 < upper * upper
    mixed = refute(corpus.MIXED)
    assert mixed["model"][1] == [1, 1]
    every = Goal(
        corpus.LINE,
        Or(Tru(), Not(Atom(Relation.GT, X))),
        And(Atom(Relation.GE, X), Or(Fls(), Atom(Relation.EQ, Mul(X, Neg(ONE))))),
    )
    assert refute(every)["result"] == "sat"
    columns = [[[[], [1, 1]]], [[[0], [1, 1]]]]
    feasible = worker.answer(
        {
            "op": "weights",
            "columns": columns,
            "target": [[[], [3, 2]], [[0], [1, 2]]],
            "timeout_ms": 10_000,
        }
    )
    assert feasible == {"result": "sat", "weights": [[3, 2], [1, 2]]}
    negative = worker.answer(
        {
            "op": "weights",
            "columns": columns,
            "target": [[[0], [-1, 1]]],
            "timeout_ms": 10_000,
        }
    )
    assert negative == {"result": "unsat"}
    assert worker.answer({"op": "prove"})["result"] == "error"
    assert worker.answer(None)["result"] == "error"
    assert worker.answer({"op": "refute"})["result"] == "error"


def test_a_goal_with_shared_subterms_is_refused_quickly() -> None:
    """A DAG whose tree is exponential is refused, not walked."""
    shared = X
    for _ in range(60):
        shared = Add(shared, shared)
    started = time.monotonic()
    with pytest.raises(GoalError, match="nodes"):
        Goal(corpus.LINE, Atom(Relation.LE, shared), Tru())
    assert time.monotonic() - started < 5


def test_replay_refuses_an_unhashable_status(no_lean) -> None:
    outcome = replay.replay_artifact(
        _edit(_artifact(), lambda d: d.update(status=[])), corpus.DISK, LIMITS
    )
    assert isinstance(outcome, replay.InvalidEvidence)


def test_the_worker_that_cannot_start_is_a_failure(monkeypatch) -> None:
    from glitnir_forseti.entailment import solver

    def refuse(*args, **kwargs):
        raise OSError("no processes left")

    monkeypatch.setattr(solver.subprocess, "Popen", refuse)
    answer = run_worker({"op": "refute"}, time.monotonic() + 5)
    assert isinstance(answer, WorkerStop) and answer.cause == "failed"
    found = search.refute(corpus.UNRELATED, LIMITS, time.monotonic() + 5)
    assert found.outcome == "z3-unknown"


def test_an_oversized_answer_is_a_resource_limit(tmp_path, monkeypatch) -> None:
    from glitnir_forseti.entailment import solver

    fake = _fake_python(tmp_path, "cat > /dev/null; head -c 5000000 /dev/zero")
    monkeypatch.setattr(solver.sys, "executable", fake)
    found = search.refute(corpus.UNRELATED, LIMITS, time.monotonic() + 10)
    assert found.outcome == "resource-limit"


def test_a_lane_timeout_does_not_end_the_request(monkeypatch) -> None:
    """A refutation that uses up its share leaves the certificate lane to run."""
    calls = []

    def slow_refute(goal, limits, deadline, cancel):
        calls.append(("refutation", deadline))
        return LaneOutcome("timeout")

    def certify(goal, limits, deadline, cancel, dictionary):
        calls.append(("certificate", deadline))
        return LaneOutcome("none")

    monkeypatch.setattr(service, "refute", slow_refute)
    monkeypatch.setattr(service, "certify_with_products", certify)
    started = time.monotonic()
    result = service.decide_entailment(corpus.DISK, EntailmentLimits(wall_time_ms=9000))
    assert [lane for lane, _ in calls] == ["refutation", "certificate"]
    assert calls[0][1] - started < 3.5  # a third of the budget
    assert result.reason is service.UnknownReason.TIMEOUT


def test_the_cli_reports_bad_input_without_a_traceback(tmp_path, capsys) -> None:
    from glitnir_forseti.entailment.__main__ import main

    goal = tmp_path / "goal.json"
    goal.write_text("[" * 100_000)
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b"{}")
    assert main(["replay", str(artifact), "--goal", str(goal)]) == 2
    assert main(["replay", str(tmp_path / "missing"), "--goal", str(goal)]) == 2
    assert main(["decide", str(goal), "--wall-time-ms", "0"]) == 2
    assert "error" in capsys.readouterr().err


def test_the_worker_starts_on_this_platform(live_children) -> None:
    """The real worker, with its real child setup, starts and answers here.

    macOS refused the address-space cap the setup once applied, so the worker
    never started there (task 134); this fails on any platform where it cannot.
    """
    answer = run_worker({"op": "none"}, time.monotonic() + 60)
    assert not isinstance(answer, WorkerStop), answer
    assert answer["result"] in ("error", "unavailable")
    assert live_children() == []


# --- real-algebraic counterexamples (task 033) ------------------------------

SQRT_TWO = AlgebraicRoot(
    (Fraction(-2), Fraction(0), Fraction(1)), Fraction(7, 5), Fraction(3, 2)
)
ROOT_TWO_WITNESS = AlgebraicWitness(
    (SQRT_TWO,), (AtomProof("zero", (ONE,)),), (AtomProof("enclosure"),)
)


def _soon() -> float:
    return time.monotonic() + 30


def _root(coefficients, lower, upper) -> AlgebraicRoot:
    return AlgebraicRoot(
        tuple(Fraction(c) for c in coefficients), Fraction(lower), Fraction(upper)
    )


@pytest.mark.parametrize(
    ("root", "isolates"),
    [
        (SQRT_TWO, True),
        (_root((-2, 0, 1), Fraction(-3, 2), Fraction(-7, 5)), True),
        (_root((-1, 1), 1, 1), True),  # a rational coordinate: x − 1 on [1, 1]
        (_root((-2, 0, 1), Fraction(3, 2), 2), False),  # no sign change
        (_root((0, -2, 0, 1), -2, 2), False),  # three roots
        (_root((4, 0, -4, 0, 1), Fraction(7, 5), Fraction(3, 2)), False),  # square
        (_root((-2, 0, 1), Fraction(3, 2), Fraction(7, 5)), False),  # reversed
    ],
)
def test_the_producer_mirrors_root_isolation(root, isolates) -> None:
    """The producer's mirror of ``Root.check``; Lean re-checks it regardless."""
    assert root_checks(root) is isolates


def test_an_algebraic_witness_round_trips() -> None:
    witness = build_witness(corpus.PRODUCT, _product_roots(), LIMITS, _soon())
    assert isinstance(witness, AlgebraicWitness)
    encoded = json.loads(json.dumps(encode_algebraic(witness)))
    assert decode_algebraic(encoded, corpus.PLANE, LIMITS) == witness


def _product_roots() -> tuple[AlgebraicRoot, ...]:
    return (SQRT_TWO, _root((-3, 0, 1), Fraction(17, 10), Fraction(9, 5)))


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(kind="witness"),
        lambda d: d.update(extra=1),
        lambda d: d["coordinates"].append(d["coordinates"][0]),
        lambda d: d["coordinates"][0].update(coefficients=[[1, 1]]),  # degree 0
        lambda d: d["coordinates"][0].update(coefficients=[[1, 1], [0, 1]]),
        lambda d: d["coordinates"][0].update(coefficients=[[1, 1]] * 10),
        lambda d: d["coordinates"][0].update(interval=[[1, 1]]),
        lambda d: d["coordinates"][0].update(interval=[[2**300, 1], [1, 1]]),
        lambda d: d.update(antecedent=[["zero", []]]),
        lambda d: d.update(antecedent=[["nonzero"]]),
        lambda d: d.update(antecedent=[["enclosure"]] * (LIMITS.max_weights + 1)),
    ],
)
def test_a_malformed_algebraic_witness_is_refused(change) -> None:
    document = encode_algebraic(ROOT_TWO_WITNESS)
    change(document)
    with pytest.raises(EvidenceError):
        decode_algebraic(document, corpus.LINE, LIMITS)


@pytest.mark.parametrize(
    ("goal", "roots"),
    [
        (corpus.ROOT_TWO, (SQRT_TWO,)),
        (corpus.ROOT_TWO_POSITIVE, (_root((-2, 0, 1), -2, -1),)),
        (corpus.BOUNDARY, (_root((-2, 0, 1), 1, 2),)),
        (corpus.MIXED, (_root((-2, 0, 1), 0, 2), _root((-1, 1), 1, 1))),
    ],
)
def test_the_producer_refines_until_every_sign_is_known(goal, roots) -> None:
    witness = build_witness(goal, roots, LIMITS, _soon())
    assert isinstance(witness, AlgebraicWitness)
    assert all(root_checks(root) for root in witness.coordinates)
    kinds = [atom.kind for atom in witness.antecedent + witness.consequent]
    assert "zero" in kinds


def test_the_producer_reports_what_it_cannot_establish() -> None:
    """``y = x`` is not a per-coordinate identity: no witness, and a reason."""
    reason = build_witness(corpus.TIED, (SQRT_TWO, SQRT_TWO), LIMITS, _soon())
    assert isinstance(reason, str) and "sign" in reason


def test_a_model_reads_rationals_and_roots_exactly() -> None:
    roots = from_model(
        [{"polynomial": [-2, 0, 1], "lower": [7, 5], "upper": [3, 2]}, [1, 3]],
        2,
        LIMITS,
    )
    assert roots == (
        SQRT_TWO,
        _root((Fraction(-1, 3), 1), Fraction(1, 3), Fraction(1, 3)),
    )
    for bad in (
        [[1, 1]],
        [{"polynomial": [1], "lower": [0, 1], "upper": [1, 1]}, [1, 1]],
        # A zero leading coefficient would divide by zero in the producer.
        [{"polynomial": [-2, 0, 1, 0], "lower": [1, 1], "upper": [2, 1]}, [1, 1]],
    ):
        with pytest.raises(EvidenceError):
            from_model(bad, 2, LIMITS)


def test_an_algebraic_artifact_checks_in_its_own_closure(monkeypatch) -> None:
    seen = []

    def closure(imports, *args, **kwargs):
        seen.append(imports)
        return IndependentCheck(IndependentStatus.CONFIRMED)

    monkeypatch.setattr(replay, "verify_closure", closure)
    monkeypatch.setattr(
        replay,
        "check_independently",
        lambda *a, **k: IndependentCheck(IndependentStatus.CONFIRMED, ("propext",)),
    )
    artifact = _artifact(corpus.ROOT_TWO, ROOT_TWO_WITNESS)
    document = json.loads(artifact)
    assert document["environment"] == environment_identity(render.ALGEBRAIC_IMPORTS)
    assert "import Gimle.Forseti.Syntax.AlgebraicWitness" in document["source"]
    outcome = replay.replay_artifact(artifact, corpus.ROOT_TWO, LIMITS)
    assert isinstance(outcome, replay.ValidatedEntailment)
    assert outcome.status == "REFUTED" and seen == [render.ALGEBRAIC_IMPORTS]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: d.update(status="PROVEN"), "does not match"),
        (
            lambda d: d["environment"].update(imports=list(render.IMPORTS)),
            "environment",
        ),
        (
            lambda d: d["evidence"]["coordinates"][0].update(interval=[[3, 2], [2, 1]]),
            "retained source",
        ),
    ],
)
def test_an_algebraic_artifact_is_refused_before_lean(no_lean, change, reason) -> None:
    artifact = _edit(_artifact(corpus.ROOT_TWO, ROOT_TWO_WITNESS), change)
    outcome = replay.replay_artifact(artifact, corpus.ROOT_TWO, LIMITS)
    assert isinstance(outcome, replay.InvalidEvidence)
    assert reason in outcome.reason


def test_the_service_checks_an_algebraic_model(monkeypatch) -> None:
    replayed = []

    def fake_replay(artifact, goal, *args, **kwargs):
        replayed.append(json.loads(artifact)["evidence"]["kind"])
        return replay.ValidatedEntailment("REFUTED", goal, ROOT_TWO_WITNESS, ())

    _lanes(
        monkeypatch,
        LaneOutcome("unsupported-witness", "root-obj", roots=(SQRT_TWO,)),
        LaneOutcome("none"),
    )
    monkeypatch.setattr(service, "replay_artifact", fake_replay)
    result = service.decide_entailment(corpus.ROOT_TWO)
    assert result.status is service.Status.REFUTED
    assert replayed == ["algebraic-witness"]


def test_an_unusable_algebraic_model_stays_unknown(monkeypatch) -> None:
    _lanes(
        monkeypatch,
        LaneOutcome("unsupported-witness", "root-obj", roots=(SQRT_TWO, SQRT_TWO)),
        LaneOutcome("unsupported-fragment"),
    )
    result = service.decide_entailment(corpus.TIED)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED_WITNESS
    assert "sign" in result.lanes[0].detail


def test_an_oversized_atom_is_a_reason_not_a_crash() -> None:
    """``(x² − 2)·x⁷`` vanishes at √2, but expanding it exceeds the degree."""
    big = Mul(Add(Mul(X, X), Neg(Const(Fraction(2)))), X)
    for _ in range(6):
        big = Mul(big, X)
    goal = Goal(
        corpus.LINE,
        And(corpus.ROOT_TWO.antecedent, Atom(Relation.EQ, big)),
        corpus.ROOT_TWO.consequent,
    )
    reason = build_witness(goal, (SQRT_TWO,), LIMITS, _soon())
    assert isinstance(reason, str) and "expansion limit" in reason


def test_refinement_honours_the_deadline_and_cancellation() -> None:
    late = build_witness(corpus.TIED, (SQRT_TWO, SQRT_TWO), LIMITS, time.monotonic())
    assert isinstance(late, str) and "deadline" in late
    cancel = threading.Event()
    cancel.set()
    stopped = build_witness(corpus.TIED, (SQRT_TWO, SQRT_TWO), LIMITS, _soon(), cancel)
    assert isinstance(stopped, str) and "cancelled" in stopped


def test_a_rational_witness_cannot_claim_the_algebraic_environment(no_lean) -> None:
    witness = Witness((("x", Fraction(1)), ("y", Fraction(0))))
    artifact = _edit(
        _artifact(corpus.HALF_DISK, witness),
        lambda d: d.update(environment=environment_identity(render.ALGEBRAIC_IMPORTS)),
    )
    outcome = replay.replay_artifact(artifact, corpus.HALF_DISK, LIMITS)
    assert isinstance(outcome, replay.InvalidEvidence)
    assert "environment" in outcome.reason


# --- constraint products (forseti-lean 027) ---------------------------------

_ROW = Row(
    SquareSum(()), (SquareSum(()), SquareSum(()), SquareSum(((Fraction(1), ONE),)))
)
_PRODUCT = PsatzCertificate(((0, 1),), (_ROW,))


def test_a_psatz_certificate_round_trips_and_checks() -> None:
    goal = corpus.PRODUCT_NONNEG
    encoded = json.loads(json.dumps(encode_psatz(_PRODUCT)))
    assert encoded["kind"] == "psatz-certificate" and encoded["pairs"] == [[0, 1]]
    assert decode_evidence(encoded, goal.context, LIMITS) == _PRODUCT
    _assert_identity(_PRODUCT, goal)


@pytest.mark.parametrize(
    "pairs",
    [(), ((0, 2),), ((1, 0),), ((-1, 0),), ((0, 1), (0, 1)), ((1, 1), (0, 1))],
    ids=["none", "out-of-range", "descending", "negative", "repeated", "unsorted"],
)
def test_malformed_pairs_are_refused_before_lean(pairs) -> None:
    antecedent = nonstrict(corpus.PRODUCT_NONNEG.antecedent)
    consequent = nonstrict(corpus.PRODUCT_NONNEG.consequent)
    assert antecedent is not None and consequent is not None
    multipliers = (SquareSum(()),) * (len(antecedent) + len(pairs))
    certificate = PsatzCertificate(pairs, (Row(SquareSum(()), multipliers),))
    with pytest.raises(EvidenceError):
        check_psatz(certificate, antecedent, consequent, LIMITS)


@pytest.mark.parametrize(
    "pairs",
    ["x", [[0]], [[0, "1"]], [[0, True]], [[0, 1.0]]],
    ids=["text", "short", "string", "bool", "float"],
)
def test_malformed_pair_encodings_are_refused(pairs) -> None:
    encoded = {**encode_psatz(_PRODUCT), "pairs": pairs}
    with pytest.raises(EvidenceError):
        decode_evidence(encoded, corpus.PRODUCT_NONNEG.context, LIMITS)


def test_a_pair_no_row_uses_is_refused() -> None:
    """Naming an unused product would give one identity two encodings."""
    antecedent = nonstrict(corpus.PRODUCT_NONNEG.antecedent)
    consequent = nonstrict(corpus.PRODUCT_NONNEG.consequent)
    assert antecedent is not None and consequent is not None
    padded = PsatzCertificate(
        ((0, 0), (0, 1)),
        (
            Row(
                SquareSum(()),
                _ROW.multipliers[:2] + (SquareSum(()),) + _ROW.multipliers[2:],
            ),
        ),
    )
    with pytest.raises(EvidenceError, match="uses"):
        check_psatz(padded, antecedent, consequent, LIMITS)


def test_rows_must_cover_the_augmented_antecedent() -> None:
    antecedent = nonstrict(corpus.PRODUCT_NONNEG.antecedent)
    consequent = nonstrict(corpus.PRODUCT_NONNEG.consequent)
    assert antecedent is not None and consequent is not None
    short = PsatzCertificate(((0, 1),), (Row(SquareSum(()), (SquareSum(()),) * 2),))
    with pytest.raises(EvidenceError, match="multiplier"):
        check_psatz(short, antecedent, consequent, LIMITS)


def test_a_psatz_certificate_renders_its_checker_and_closure() -> None:
    application = render.application(corpus.PRODUCT_NONNEG, _PRODUCT)
    assert application.prelude.startswith(f"import {render.PSATZ_IMPORT}\n")
    (line,) = application.script
    assert line.startswith(
        "exact Gimle.Forseti.Syntax.PsatzCertificate.sound forsetiGoal ⟨[(0, 1)], "
    )
    assert render.imports_for(_PRODUCT) == render.PSATZ_IMPORTS
    assert render.imports_for(Certificate((_ROW,))) == render.IMPORTS


def test_a_psatz_artifact_binds_and_regenerates() -> None:
    goal = corpus.PRODUCT_NONNEG
    artifact = build_artifact(
        goal, _PRODUCT, render.application(goal, _PRODUCT), LIMITS, {"lane": "test"}
    )
    decided = replay.regenerate_application(artifact, goal, LIMITS)
    assert isinstance(decided, tuple)
    status, evidence, _ = decided
    assert status == "PROVEN" and evidence == _PRODUCT
    document = json.loads(artifact)
    assert document["environment"] == environment_identity(render.PSATZ_IMPORTS)
    hostile = {**document, "evidence": {**document["evidence"], "pairs": [[0, 5]]}}
    refused = replay.regenerate_application(json.dumps(hostile).encode(), goal, LIMITS)
    assert isinstance(refused, replay.InvalidEvidence)
    negated = {**document, "status": "REFUTED"}
    refused = replay.regenerate_application(json.dumps(negated).encode(), goal, LIMITS)
    assert isinstance(refused, replay.InvalidEvidence)


@needs_z3
@pytest.mark.parametrize("goal", [corpus.PRODUCT_NONNEG, corpus.BOX_PRODUCT])
def test_constraint_products_prove_what_no_dictionary_does(goal) -> None:
    plain = search.certify(goal, LIMITS, time.monotonic() + 30)
    assert plain.outcome == "none"
    found = search.certify_with_products(goal, LIMITS, time.monotonic() + 60)
    assert found.outcome == "found" and "constraint products" in found.detail
    assert isinstance(found.evidence, PsatzCertificate)
    assert found.evidence.pairs == ((0, 1),)  # the unused squares are pruned
    _assert_identity(found.evidence, goal)


@needs_z3
def test_products_are_not_tried_for_a_supplied_dictionary_or_a_proven_goal() -> None:
    restricted = search.certify_with_products(
        corpus.QUARTIC, LIMITS, time.monotonic() + 30, None, (ONE,)
    )
    assert restricted.outcome == "none" and "products" not in restricted.detail
    proven = search.certify_with_products(corpus.DISK, LIMITS, time.monotonic() + 30)
    assert isinstance(proven.evidence, Certificate)


@needs_z3
def test_three_constraint_products_stay_beyond_reach() -> None:
    found = search.certify_with_products(
        corpus.TRIPLE_PRODUCT, LIMITS, time.monotonic() + 60
    )
    assert found.outcome == "none"
    assert "constraint products ended" in found.detail


def test_products_get_a_share_of_the_time_and_are_optional(monkeypatch) -> None:
    deadlines = []
    monkeypatch.setattr(
        search, "certify", lambda *a, **k: LaneOutcome("none", "exhausted")
    )

    def products(goal, limits, deadline, cancel):
        deadlines.append(deadline)
        return LaneOutcome("timeout", "late")

    monkeypatch.setattr(search, "certify_products", products)
    start = time.monotonic()
    found = search.certify_with_products(corpus.PRODUCT_NONNEG, LIMITS, start + 100)
    assert found.outcome == "none" and "constraint products ended timeout" in (
        found.detail
    )
    assert deadlines[0] <= start + 100 * search.WIDENING_SHARE + 1


@needs_z3
@pytest.mark.parametrize("count", [44, 60])
def test_many_antecedents_end_exhausted_not_in_an_error(count) -> None:
    """The pairs grow quadratically; they are bounded before anything is built."""
    x, y = Var(0), Var(1)
    atoms = [Atom(Relation.GE, (x, y)[i % 2]) for i in range(count)]
    while len(atoms) > 1:
        atoms = [And(a, b) for a, b in zip(atoms[::2], atoms[1::2])] + (
            atoms[-1:] if len(atoms) % 2 else []
        )
    goal = Goal(corpus.PLANE, atoms[0], Atom(Relation.GE, Mul(x, y)))
    check_goal_limits(goal, LIMITS)
    found = search.certify_with_products(goal, LIMITS, time.monotonic() + 60)
    assert found.outcome in ("found", "none"), found
    products = search.certify_products(goal, LIMITS, time.monotonic() + 60)
    assert products.outcome == "resource-limit"


@needs_z3
def test_a_goal_without_constraints_has_no_products_to_try(monkeypatch) -> None:
    monkeypatch.setattr(
        search, "certify_products", lambda *a: pytest.fail("nothing to multiply")
    )
    goal = Goal(corpus.PLANE, Tru(), Atom(Relation.GE, Mul(X, Var(1))))
    found = search.certify_with_products(goal, LIMITS, time.monotonic() + 30)
    assert found.outcome == "none" and "products" not in found.detail


def test_a_certificate_that_uses_no_product_is_an_ordinary_one() -> None:
    empty = SquareSum(())
    used = SquareSum(((Fraction(1), ONE),))
    rows = (Row(used, (used, empty, empty)),)
    pruned = search._prune(rows, ((0, 0),), 2)
    assert pruned == Certificate((Row(used, (used, empty)),))
    kept = search._prune((Row(used, (empty, empty, used)),), ((0, 1),), 2)
    assert kept == PsatzCertificate(((0, 1),), (Row(used, (empty, empty, used)),))


def test_solver_child_does_not_inherit_remote_credential(monkeypatch) -> None:
    """Local solver search has no need for the remote computation credential."""
    from glitnir_forseti.entailment import solver

    monkeypatch.setenv("GLITNIR_TASKVALET_TOKEN", "private-compute-token")
    observed = []

    def refuse(*args, **kwargs):
        observed.append("GLITNIR_TASKVALET_TOKEN" in kwargs.get("env", os.environ))
        raise OSError("no child needed")

    monkeypatch.setattr(solver.subprocess, "Popen", refuse)
    run_worker({"op": "refute"}, time.monotonic() + 5)
    assert observed == [False]
