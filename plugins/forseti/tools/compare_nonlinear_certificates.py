"""Compare what nonlinear ``unsat`` evidence each route gives (task 034).

Usage, with the ``z3`` extra installed::

    uv run python tools/compare_nonlinear_certificates.py [--lean-project P]

For every goal ``P ⊨ Q`` in task 111's corpus, plus goals chosen to sit just
beyond its square dictionary, the question is the same: ``P ∧ ¬Q`` is
unsatisfiable, and what does each route hand over that a Lean checker could
replay? The routes:

- **z3-proof**: Z3's proof object (``proof=True``), its rule heads, and the
  steps that carry no checkable content: ``th-lemma`` steps, which name no
  coefficients, and ``rewrite`` steps, which Z3 justifies only by its own
  simplifier (the sign split of ``x·y ≥ 0`` is one);
- **cvc5-cpc**: cvc5's CPC proof (``proof-format-mode cpc``), its rule names
  and its ``trust`` steps. cvc5 is not a dependency: it runs in a throwaway
  environment through ``uv run --with cvc5``, and is recorded as unavailable
  otherwise;
- **z3-qe**: exact quantifier elimination (Z3's CAD-based ``qe2`` tactic) of
  ``∀x. P → Q``, which answers ``True`` and carries no certificate;
- **psatz**: the certificate family the comparison recommends, found by task
  111's exact linear search. It widens 111 in two ways: squares of pairwise
  sums and differences of the dictionary (the diagonally dominant Gram
  matrices), and pairwise products of antecedent constraints (a degree-2
  Positivstellensatz). Each is recorded separately.

With ``--lean-project``, the Lean prototype ``PsatzPrototype.lean`` is also
checked, independently, against that project.

Every solver answer here is data about a route. None is a proof, and nothing
is written to the entailment corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations_with_replacement
from pathlib import Path
from typing import Any

import z3

from glitnir_forseti.entailment import corpus
from glitnir_forseti.entailment.evidence import Certificate, nonstrict
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.search import certify, default_dictionary
from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Context,
    Fls,
    Formula,
    Goal,
    Mul,
    Neg,
    Not,
    Or,
    Poly,
    Relation,
    Tru,
    Var,
)
from glitnir_forseti.entailment.worker import translate

REPOSITORY = Path(__file__).resolve().parents[1]
OUTPUT = REPOSITORY / "benchmarks/nonlinear-certificates"
TIMEOUT_MS = 20_000
CVC5 = "cvc5==1.4.1"


@dataclass(frozen=True)
class Case:
    """One goal and where it comes from."""

    name: str
    claim: str
    goal: Goal
    origin: str


def _c(value: int) -> Const:
    return Const(Fraction(value))


_PLANE = corpus.PLANE
_X, _Y = _PLANE.var("x"), _PLANE.var("y")
_SPACE = Context(("x", "y", "z"))
_A, _B, _C = (_SPACE.var(n) for n in ("x", "y", "z"))
_LINE = corpus.LINE
_U = _LINE.var("x")


def _sub(left: Poly, right: Poly) -> Poly:
    return Add(left, Neg(right))


BEYOND = (
    Case(
        "product-nonneg",
        "x ≥ 0 ∧ y ≥ 0 ⊨ x·y ≥ 0",
        Goal(
            _PLANE,
            And(Atom(Relation.GE, _X), Atom(Relation.GE, _Y)),
            Atom(Relation.GE, Mul(_X, _Y)),
        ),
        "beyond 111: needs a product of constraints",
    ),
    Case(
        "box-product",
        "x ≥ 1 ∧ y ≥ 1 ⊨ x·y ≥ 1",
        Goal(
            _PLANE,
            And(Atom(Relation.GE, _sub(_X, _c(1))), Atom(Relation.GE, _sub(_Y, _c(1)))),
            Atom(Relation.GE, _sub(Mul(_X, _Y), _c(1))),
        ),
        "beyond 111: needs a product of constraints",
    ),
    Case(
        "unit-product",
        "x² ≤ 1 ∧ y² ≤ 1 ⊨ x·y ≤ 1",
        Goal(
            _PLANE,
            And(
                Atom(Relation.LE, _sub(Mul(_X, _X), _c(1))),
                Atom(Relation.LE, _sub(Mul(_Y, _Y), _c(1))),
            ),
            Atom(Relation.LE, _sub(Mul(_X, _Y), _c(1))),
        ),
        "beyond 111: needs the off-diagonal square (x − y)²",
    ),
    Case(
        "triple-product",
        "x ≥ 0 ∧ y ≥ 0 ∧ z ≥ 0 ⊨ x·y·z ≥ 0",
        Goal(
            _SPACE,
            And(
                And(Atom(Relation.GE, _A), Atom(Relation.GE, _B)), Atom(Relation.GE, _C)
            ),
            Atom(Relation.GE, Mul(Mul(_A, _B), _C)),
        ),
        "beyond degree-2 products: needs a product of three constraints",
    ),
    Case(
        "odd-power",
        "x ≥ 0 ⊨ x³ ≥ 0",
        Goal(_LINE, Atom(Relation.GE, _U), Atom(Relation.GE, Mul(Mul(_U, _U), _U))),
        "inside 111: x³ = x² · x",
    ),
)


def cases() -> tuple[Case, ...]:
    """Task 111's corpus followed by the goals just beyond it."""
    own = tuple(
        Case(c.name, c.claim, c.goal, "task 111 corpus")
        for c in corpus.CASES
        if c.dictionary is None
    )
    # Task 145 moved some of these goals into the corpus; they keep their
    # "beyond" labels here.
    beyond = {c.name for c in BEYOND}
    return tuple(c for c in own if c.name not in beyond) + BEYOND


# --- Z3 -------------------------------------------------------------------

_Z3_RULE = re.compile(r"\((?:_ )?([a-z][a-z0-9-]*)\b")
_Z3_RULES = {
    "asserted",
    "mp",
    "rewrite",
    "th-lemma",
    "unit-resolution",
    "lemma",
    "hypothesis",
    "def-axiom",
    "not-or-elim",
    "and-elim",
    "monotonicity",
    "trans",
    "symm",
    "refl",
    "quant-inst",
    "nnf-pos",
    "nnf-neg",
    "iff-true",
    "iff-false",
    "commutativity",
    "elim-unused",
    "der",
    "sk",
    "modus-ponens",
}


def _variables(goal: Goal) -> list[Any]:
    return [z3.Real(name) for name in goal.context.names]


def _negation(goal: Goal) -> tuple[list[Any], Any]:
    variables = _variables(goal)
    return variables, z3.And(
        translate(z3, variables, goal.antecedent),
        z3.Not(translate(z3, variables, goal.consequent)),
    )


_Z3_PROBE = r"""
import hashlib, json, sys, time
import z3
z3.set_param(proof=True)  # before the first context: proofs are per context
queries = json.load(sys.stdin)
out = {}
for name, text in queries.items():
    solver = z3.Solver()
    solver.set("timeout", %d)
    solver.add(z3.parse_smt2_string(text))
    started = time.monotonic()
    answer = solver.check()
    entry = {"result": str(answer), "ms": int((time.monotonic() - started) * 1000)}
    if answer == z3.unsat:
        entry["proof"] = solver.proof().sexpr()
    out[name] = entry
json.dump(out, sys.stdout)
""" % TIMEOUT_MS


def z3_proofs(queries: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Run Z3 with proofs on each ``P ∧ ¬Q`` in a fresh process, and classify.

    Z3 enables proofs per context, when the context is made, so the run needs
    a process whose first context is made with ``proof=True``.
    """
    run = subprocess.run(
        [sys.executable, "-c", _Z3_PROBE],
        input=json.dumps(queries),
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    results: dict[str, dict[str, Any]] = {}
    for name, entry in json.loads(run.stdout).items():
        text = entry.pop("proof", None)
        if text is None:
            results[name] = entry
            continue
        heads: dict[str, int] = {}
        for head in _Z3_RULE.findall(text):
            if head in _Z3_RULES:
                heads[head] = heads.get(head, 0) + 1
        results[name] = {
            **entry,
            "bytes": len(text),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "rules": dict(sorted(heads.items())),
            # Neither kind replays without Z3's own arithmetic or rewriter.
            "unchecked_steps": heads.get("th-lemma", 0) + heads.get("rewrite", 0),
        }
    return results


def z3_qe(goal: Goal) -> dict[str, Any]:
    """Eliminate ``∀x. P → Q`` exactly with Z3's ``qe2``."""
    variables = _variables(goal)
    claim = z3.ForAll(
        variables,
        z3.Implies(
            translate(z3, variables, goal.antecedent),
            translate(z3, variables, goal.consequent),
        ),
    )
    started = time.monotonic()
    try:
        tactic = z3.TryFor(z3.Tactic("qe2"), TIMEOUT_MS)
        result = tactic(claim)
        formula = str(result.as_expr())
    except z3.Z3Exception as error:
        return {"result": "error", "detail": str(error)[:200]}
    elapsed = int((time.monotonic() - started) * 1000)
    return {"result": formula[:200], "ms": elapsed, "certificate": None}


# --- cvc5, out of process ---------------------------------------------------


def _smt_poly(value: Poly, names: tuple[str, ...]) -> str:
    if type(value) is Var:
        return names[value.index]
    if type(value) is Const:
        q = value.value
        text = f"(/ {abs(q.numerator)} {q.denominator})"
        return f"(- {text})" if q < 0 else text
    if type(value) is Add:
        return f"(+ {_smt_poly(value.left, names)} {_smt_poly(value.right, names)})"
    if type(value) is Mul:
        return f"(* {_smt_poly(value.left, names)} {_smt_poly(value.right, names)})"
    if type(value) is Neg:
        return f"(- {_smt_poly(value.argument, names)})"
    raise TypeError(f"not a polynomial: {value!r}")


_SMT_RELATION = {
    Relation.EQ: "=",
    Relation.LE: "<=",
    Relation.LT: "<",
    Relation.GE: ">=",
    Relation.GT: ">",
}


def _smt_formula(value: Formula, names: tuple[str, ...]) -> str:
    if type(value) is Atom:
        relation = _SMT_RELATION[value.relation]
        return f"({relation} {_smt_poly(value.polynomial, names)} 0)"
    if type(value) is Tru:
        return "true"
    if type(value) is Fls:
        return "false"
    if type(value) is Not:
        return f"(not {_smt_formula(value.argument, names)})"
    if type(value) is And or type(value) is Or:
        word = "and" if type(value) is And else "or"
        left = _smt_formula(value.left, names)
        return f"({word} {left} {_smt_formula(value.right, names)})"
    raise TypeError(f"not a formula: {value!r}")


def smt_query(goal: Goal) -> str:
    """``P ∧ ¬Q`` as an SMT-LIB script body, over ``QF_NRA``."""
    names = goal.context.names
    declarations = " ".join(f"(declare-const {n} Real)" for n in names)
    return (
        f"{declarations} (assert {_smt_formula(goal.antecedent, names)})"
        f" (assert (not {_smt_formula(goal.consequent, names)}))"
    )


_CVC5_PROBE = r"""
import json, re, sys, time, cvc5
queries = json.load(sys.stdin)
version = getattr(cvc5, "__version__", "?")
out = {"version": version, "cases": {}}
for name, text in queries.items():
    tm = cvc5.TermManager(); s = cvc5.Solver(tm)
    s.setOption("produce-proofs", "true"); s.setOption("proof-format-mode", "cpc")
    s.setOption("dag-thresh", "0"); s.setOption("tlimit-per", "%d")
    s.setLogic("QF_NRA")
    sm = cvc5.SymbolManager(tm)
    parser = cvc5.InputParser(s, sm)
    try:
        parser.setStringInput(cvc5.InputLanguage.SMT_LIB_2_6, text, name)
        while True:
            cmd = parser.nextCommand()
            if cmd.isNull(): break
            cmd.invoke(s, sm)
        started = time.monotonic()
        r = s.checkSat()
        ms = int((time.monotonic() - started) * 1000)
    except Exception as error:
        out["cases"][name] = {"result": "error", "detail": str(error)[:200]}; continue
    entry = {"result": str(r), "ms": ms}
    if r.isUnsat():
        proof = s.proofToString(s.getProof()[0])
        proof = proof.decode() if isinstance(proof, bytes) else proof
        rules = {}
        for rule in re.findall(r":rule ([\w\-]+)", proof):
            rules[rule] = rules.get(rule, 0) + 1
        entry.update(bytes=len(proof), rules=dict(sorted(rules.items())),
                     trust=rules.get("trust", 0))
    out["cases"][name] = entry
json.dump(out, sys.stdout)
""" % TIMEOUT_MS


def cvc5_proofs(queries: dict[str, str]) -> dict[str, Any]:
    """Run cvc5 in a throwaway environment; unavailable is recorded, not fatal."""
    try:
        run = subprocess.run(
            ["uv", "run", "--no-project", "--with", CVC5, "python", "-c", _CVC5_PROBE],
            input=json.dumps(queries),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "detail": str(error)}
    if run.returncode != 0:
        return {"available": False, "detail": run.stderr[-500:]}
    result = json.loads(run.stdout)
    result["available"] = True
    return result


# --- the recommended family ---------------------------------------------------


def dsos_dictionary(goal: Goal) -> tuple[Poly, ...]:
    """111's dictionary plus every pairwise sum and difference of its entries."""
    base = default_dictionary(goal.context)
    pairs = [
        f(a, b)
        for i, a in enumerate(base)
        for b in base[i + 1 :]
        for f in (lambda a, b: Add(a, b), lambda a, b: Add(a, Neg(b)))
    ]
    return tuple(base) + tuple(pairs)


def with_products(goal: Goal) -> tuple[Goal, list[tuple[int, int]]] | None:
    """Augment the antecedent with ``−(pᵢ·pⱼ) ≤ 0`` for every pair ``i ≤ j``."""
    constraints = nonstrict(goal.antecedent)
    if constraints is None:
        return None
    pairs = list(combinations_with_replacement(range(len(constraints)), 2))
    antecedent: Formula = goal.antecedent
    for i, j in pairs:
        antecedent = And(
            antecedent,
            Atom(Relation.LE, Neg(Mul(constraints[i], constraints[j]))),
        )
    return Goal(goal.context, antecedent, goal.consequent), pairs


def _size(certificate: Certificate) -> int:
    return sum(
        len(row.base.terms) + sum(len(m.terms) for m in row.multipliers)
        for row in certificate.rows
    )


def psatz(goal: Goal, limits: EntailmentLimits) -> dict[str, Any]:
    """Search 111's family and its two widenings, each exactly."""
    found: dict[str, Any] = {}
    augmented = with_products(goal)
    # Each family names its dictionary: the lane widens an unnamed one itself.
    base = default_dictionary(goal.context)
    variants: list[tuple[str, Goal, tuple[Poly, ...]]] = [
        ("sos", goal, base),
        ("dsos", goal, dsos_dictionary(goal)),
    ]
    if augmented is not None:
        variants += [
            ("products", augmented[0], base),
            ("products+dsos", augmented[0], dsos_dictionary(goal)),
        ]
    for label, posed, dictionary in variants:
        started = time.monotonic()
        outcome = certify(
            posed, limits, time.monotonic() + TIMEOUT_MS / 1000, None, dictionary
        )
        entry: dict[str, Any] = {
            "outcome": outcome.outcome,
            "ms": int((time.monotonic() - started) * 1000),
        }
        if isinstance(outcome.evidence, Certificate):
            entry["terms"] = _size(outcome.evidence)
        found[label] = entry
    return found


def check_prototype(project: Path) -> dict[str, Any]:
    """Check the Lean prototype independently: every example compiles."""
    from glitnir_forseti.lean.independent import check_independently
    from glitnir_forseti.lean.session import SessionLimits

    source = (OUTPUT / "PsatzPrototype.lean").read_text()
    results = {}
    for theorem in ("disk", "productNonneg", "boxProduct", "unitProduct"):
        started = time.monotonic()
        check = check_independently(
            source,
            f": Gimle.Forseti.Prototype.Psatz.{theorem}.Entailment",
            (f"exact Gimle.Forseti.Prototype.Psatz.{theorem}_proved",),
            SessionLimits(timeout_ms=600_000),
            project,
            deadline=time.monotonic() + 600,
        )
        results[theorem] = {
            "status": check.status.value,
            "axioms": list(check.axioms),
            "ms": int((time.monotonic() - started) * 1000),
        }
    return results


def robustness() -> dict[str, Any]:
    """Malformed input and an exhausted budget, for each route."""
    report: dict[str, Any] = {}
    try:
        z3.parse_smt2_string("(declare-const x Real) (assert (<= x")
        report["z3_malformed"] = "accepted"
    except z3.Z3Exception as error:
        report["z3_malformed"] = f"rejected: {str(error)[:120]}"
    goal = BEYOND[2].goal
    _, query = _negation(goal)
    solver = z3.Solver()
    solver.set("timeout", 1)
    solver.add(query)
    report["z3_timeout_1ms"] = str(solver.check())
    malformed = cvc5_proofs({"malformed": "(declare-const x Real) (assert (<= x"})
    report["cvc5_malformed"] = malformed.get("cases", {}).get(
        "malformed", {"result": "unavailable"}
    )
    report["psatz_timeout"] = certify(
        goal, EntailmentLimits(), time.monotonic(), None, dsos_dictionary(goal)
    ).outcome
    return report


def main() -> int:
    """Run every route on every case and write ``results.json``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--lean-project", type=Path)
    arguments = parser.parse_args()
    limits = EntailmentLimits()
    chosen = cases()
    queries = {c.name: smt_query(c.goal) for c in chosen}
    cvc5 = cvc5_proofs(queries)
    # Two fresh processes: is the proof text the same run to run?
    z3_first, z3_second = z3_proofs(queries), z3_proofs(queries)
    rows = []
    for case in chosen:
        first, second = z3_first[case.name], z3_second[case.name]
        first["deterministic"] = first.get("sha256") == second.get("sha256")
        row = {
            "name": case.name,
            "claim": case.claim,
            "origin": case.origin,
            "z3_proof": first,
            "cvc5_cpc": cvc5.get("cases", {}).get(case.name, {"result": "unavailable"}),
            "z3_qe": z3_qe(case.goal),
            "psatz": psatz(case.goal, limits),
        }
        rows.append(row)
        found = [k for k, v in row["psatz"].items() if v["outcome"] == "found"]
        print(f"{case.name}: z3 {first['result']}, psatz {found or 'none'}")
    document: dict[str, Any] = {
        "schema": "gimle.forseti.nonlinear-certificate-comparison/v1",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "z3": z3.get_version_string(),
            "cvc5": cvc5.get("version") if cvc5.get("available") else None,
            "cvc5_detail": None if cvc5.get("available") else cvc5.get("detail"),
            "timeout_ms": TIMEOUT_MS,
        },
        "cases": rows,
        "robustness": robustness(),
    }
    if arguments.lean_project is not None:
        document["prototype"] = check_prototype(arguments.lean_project)
        print("prototype:", {k: v["status"] for k, v in document["prototype"].items()})
    (OUTPUT / "results.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
