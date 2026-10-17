"""The nonlinear-certificate comparison of task 034.

What these pin is the comparison's two widenings of task 111's search. Pairwise
constraint products prove ``x ≥ 0 ∧ y ≥ 0 ⊨ x·y ≥ 0``, and the diagonally
dominant squares prove ``x² ≤ 1 ∧ y² ≤ 1 ⊨ x·y ≤ 1``. Each is beyond 111's
own dictionary, and a product of three constraints is beyond both. The Lean
prototype also checks independently.
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("z3")

from glitnir_forseti.entailment.evidence import nonstrict  # noqa: E402
from glitnir_forseti.entailment.limits import EntailmentLimits  # noqa: E402
from glitnir_forseti.entailment.syntax import Atom, Mul, Neg, Relation  # noqa: E402

_TOOL = Path(__file__).resolve().parents[1] / "tools/compare_nonlinear_certificates.py"
_SPEC = importlib.util.spec_from_file_location("compare_nonlinear_certificates", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
compare = importlib.util.module_from_spec(_SPEC)
# The tool's dataclasses resolve their module through ``sys.modules``.
sys.modules[_SPEC.name] = compare
_SPEC.loader.exec_module(compare)

LIMITS = EntailmentLimits()
BY_NAME = {case.name: case for case in compare.BEYOND}


def test_products_augment_the_antecedent_with_each_pair() -> None:
    goal = BY_NAME["product-nonneg"].goal
    augmented, pairs = compare.with_products(goal)
    assert pairs == [(0, 0), (0, 1), (1, 1)]
    constraints = nonstrict(goal.antecedent)
    derived = nonstrict(augmented.antecedent)
    assert derived[: len(constraints)] == constraints
    assert derived[len(constraints) + 1] == Neg(Mul(constraints[0], constraints[1]))


def test_the_dsos_dictionary_has_pairwise_sums_and_differences() -> None:
    goal = BY_NAME["unit-product"].goal
    dictionary = compare.dsos_dictionary(goal)
    assert len(dictionary) == 3 + 2 * 3


@pytest.mark.parametrize(
    ("name", "found"),
    [
        ("product-nonneg", {"products", "products+dsos"}),
        ("box-product", {"products", "products+dsos"}),
        ("unit-product", {"dsos", "products+dsos"}),
        ("triple-product", set()),
        ("odd-power", {"sos", "dsos", "products", "products+dsos"}),
    ],
)
def test_each_widening_proves_what_it_should(name: str, found: set[str]) -> None:
    variants = compare.psatz(BY_NAME[name].goal, LIMITS)
    assert {label for label, v in variants.items() if v["outcome"] == "found"} == found


def test_the_smt_query_is_the_negated_entailment() -> None:
    import z3

    goal = BY_NAME["box-product"].goal
    solver = z3.Solver()
    solver.add(z3.parse_smt2_string(compare.smt_query(goal)))
    assert solver.check() == z3.unsat
    relaxed = type(goal)(
        goal.context,
        goal.antecedent,
        Atom(Relation.GE, Mul(goal.context.var("x"), goal.context.var("y"))),
    )
    solver = z3.Solver()
    solver.add(z3.parse_smt2_string(compare.smt_query(relaxed)))
    assert solver.check() == z3.unsat


def test_z3_proofs_rest_on_unchecked_steps() -> None:
    goal = BY_NAME["product-nonneg"].goal
    proof = compare.z3_proofs({"p": compare.smt_query(goal)})["p"]
    assert proof["result"] == "unsat" and proof["unchecked_steps"] >= 1


def test_the_prototype_checks_independently(lean_test_project) -> None:
    from glitnir_forseti.lean.independent import IndependentStatus, check_independently
    from glitnir_forseti.lean.session import SessionLimits

    source = (compare.OUTPUT / "PsatzPrototype.lean").read_text()
    check = check_independently(
        source,
        ": Gimle.Forseti.Prototype.Psatz.productNonneg.Entailment",
        ("exact Gimle.Forseti.Prototype.Psatz.productNonneg_proved",),
        SessionLimits(timeout_ms=600_000),
        lean_test_project,
        deadline=time.monotonic() + 600,
    )
    assert check.status is IndependentStatus.CONFIRMED, check.reason
    assert set(check.axioms) <= {"propext", "Classical.choice", "Quot.sound"}
