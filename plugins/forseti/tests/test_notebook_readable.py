"""Readable statements of decided claims and their bridges.

What is rendered here is checked by Lean only as part of a block, in
``test_decision_provider_lean.py``; these tests pin the text a reader sees.
"""

import dataclasses
from fractions import Fraction

import pytest

from glitnir_forseti.claims.text import parse_goal
from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Fls,
    Mul,
    Neg,
    Not,
    Or,
    Relation,
    Tru,
    Var,
)
from glitnir_forseti.notebook.readable import formula, identifier, poly, readable
from glitnir_forseti.streams.goal import FieldClaim

X, Y = Var(0), Var(1)
NAMES = ("x", "y")
TRAJECTORY = (
    "trajectory three-state interpretation continuous-real obligation contract"
    " start time = 2 initial initial-x = 1, initial-y = 2, initial-z = -1"
    " observe obs-v as V always : "
)
FIELD = (
    "stream formal-heat interpretation evaluated-field basis ogf profile x^2"
    " field u over t in [0, 1], x in [-1, 1] : "
)


def _atom(relation: Relation, left, right=None) -> Atom:
    return Atom(relation, left if right is None else Add(left, Neg(right)))


@pytest.mark.parametrize(
    "value, text",
    [
        (Add(X, Neg(Y)), "x - y"),
        (Add(X, Neg(Add(Y, X))), "x - (y + x)"),
        (Add(Add(X, Y), X), "x + y + x"),
        (Add(X, Add(Y, X)), "x + (y + x)"),
        (Mul(Mul(X, Y), X), "x * y * x"),
        (Mul(X, Mul(Y, X)), "x * (y * x)"),
        (Mul(Add(X, Y), X), "(x + y) * x"),
        (Neg(X), "-x"),
        (Neg(Add(X, Y)), "-(x + y)"),
        (Mul(Neg(X), Y), "(-x) * y"),
        (Mul(Const(Fraction(-2)), X), "(-2) * x"),
        (Mul(Const(Fraction(1, 3)), X), "1/3 * x"),
        (Mul(X, Const(Fraction(1, 3))), "x * (1/3)"),
        (Const(Fraction(-5, 2)), "-5/2"),
        (Const(Fraction(-1)), "-1"),
        (Add(X, Neg(Const(Fraction(-1)))), "x - (-1)"),
    ],
)
def test_polynomials_are_written_as_real_arithmetic(value, text) -> None:
    assert poly(value, NAMES) == text


@pytest.mark.parametrize(
    "value, text",
    [
        (_atom(Relation.LE, X, Const(Fraction(6))), "x ≤ 6"),
        (_atom(Relation.GE, X), "x ≥ 0"),
        (_atom(Relation.GE, X, Const(Fraction(0))), "x ≥ 0"),
        (_atom(Relation.EQ, Mul(X, X), Const(Fraction(2))), "x * x = 2"),
        (_atom(Relation.GT, Y, Const(Fraction(-5, 2))), "y > -5/2"),
        (_atom(Relation.LT, Const(Fraction(1)), Const(Fraction(2))), "(1 : ℝ) < 2"),
        (And(And(Tru(), Fls()), Tru()), "True ∧ False ∧ True"),
        (Or(Or(Tru(), Fls()), Tru()), "True ∨ False ∨ True"),
        (And(Tru(), And(Fls(), Tru())), "True ∧ False ∧ True"),
        (Or(And(Tru(), Fls()), Tru()), "True ∧ False ∨ True"),
        (And(Or(Tru(), Fls()), Tru()), "(True ∨ False) ∧ True"),
        (Not(_atom(Relation.LT, X)), "¬(x < 0)"),
        (Not(And(Tru(), Fls())), "¬(True ∧ False)"),
        (Not(Not(Tru())), "¬¬True"),
    ],
)
def test_formulas_are_written_as_propositions(value, text) -> None:
    assert formula(value, NAMES) == text


def test_a_name_that_is_a_lean_word_is_quoted() -> None:
    assert identifier("fun") == "«fun»"
    assert identifier("V") == "V"
    goal = parse_goal("entailment at, x : at >= 0 |- at + x*x >= 0")
    stated = readable(goal)
    assert stated is not None
    assert stated.statement == ":\n    ∀ «at» x : ℝ, «at» ≥ 0 → «at» + x * x ≥ 0"


def test_a_trajectory_claim_states_the_contract_it_is() -> None:
    stated = readable(parse_goal(TRAJECTORY + "V >= 0 and V <= 6"))
    assert stated is not None
    assert stated.statement == (
        ":\n"
        "    have circuit := Gimle.Forseti.Examples.ThreeStateContract.observed\n"
        '    have time : Gimle.Asgard.Dynamics.TimeDomain := ⟨"time", 2⟩\n'
        "    ∀ u : Gimle.Asgard.Dynamics.Signal (0 + 3),\n"
        "      Gimle.Asgard.Dynamics.signalRight u 2 = ![1, 2, -1] →\n"
        "      (∃ y : Gimle.Asgard.Dynamics.Signal 4, circuit.Rel time u y) ∧\n"
        "      (∀ y y' : Gimle.Asgard.Dynamics.Signal 4, circuit.Rel time u y →"
        " circuit.Rel time u y' →\n"
        "        ∀ t ≥ 2, y t = y' t) ∧\n"
        "      (∀ y : Gimle.Asgard.Dynamics.Signal 4, circuit.Rel time u y →\n"
        "        ∀ t ≥ 2, have V := y t 3; V ≥ 0 ∧ V ≤ 6)"
    )
    assert "port obs-v" in stated.description
    assert "(initial-x, initial-y, initial-z) start at (1, 2, -1)" in (
        stated.description
    )
    # The bridge names every constant's cast once, and is an equivalence with
    # the contract, used backwards for a proof and forwards for a refutation.
    assert any("(((mkRat (6) 1) : ℚ) : ℝ) = 6" in line for line in stated.setup)
    assert stated.lane == "forsetiContract"
    bridge = "refine (fun (bridge : _ ↔ forsetiContract) => "
    assert (
        bridge + "bridge.mpr (show forsetiIdentities ∧ forsetiContract from root).2) ?_"
        in stated.from_root
    )
    assert bridge + "refuted.2 (bridge.mp claim)) ?_" in stated.to_root
    assert stated.from_root[-len(stated.from_lane) :] == tuple(
        "  " + line for line in stated.from_lane
    )


def test_a_driven_trajectory_claim_states_its_driver_precondition() -> None:
    """A driven claim takes the goal's precondition as premises.

    The input carries the driver wires first; the premises are asgard's
    admission and the band on the bounded driver.
    """
    stated = readable(
        parse_goal(
            "trajectory hedged-spread from (0, 0) at 0 within 1/2 observe V always"
            " : V <= 1"
        )
    )
    assert stated is not None
    assert stated.statement == (
        ":\n"
        "    have circuit := Gimle.Forseti.Examples.HedgedSpreadContract.observed\n"
        '    have time : Gimle.Asgard.Dynamics.TimeDomain := ⟨"time", 0⟩\n'
        "    ∀ u : Gimle.Asgard.Dynamics.Signal (1 + 2),\n"
        "      Gimle.Forseti.Examples.HedgedSpread.evolution.Admitted "
        "Gimle.Forseti.Examples.HedgedSpread.drivers\n"
        "        (Gimle.Asgard.Dynamics.signalLeft u) →\n"
        "      (∀ t ≥ 0, |Gimle.Asgard.Dynamics.signalLeft u t 0| ≤ (1/2)) →\n"
        "      Gimle.Asgard.Dynamics.signalRight u 0 = ![0, 0] →\n"
        "      (∃ y : Gimle.Asgard.Dynamics.Signal 2, circuit.Rel time u y) ∧\n"
        "      (∀ y y' : Gimle.Asgard.Dynamics.Signal 2, circuit.Rel time u y →"
        " circuit.Rel time u y' →\n"
        "        ∀ t ≥ 0, y t = y' t) ∧\n"
        "      (∀ y : Gimle.Asgard.Dynamics.Signal 2, circuit.Rel time u y →\n"
        "        ∀ t ≥ 0, have V := y t 1; V ≤ 1)"
    )
    assert "driver wires (driver-d) are admitted, with |driver-d| ≤ 1/2" in (
        stated.description
    )
    # The band's bound is cast like every other constant, and the lane's one
    # conjunction is taken apart into the statement's premises.
    assert any("(((mkRat (1) 2) : ℚ) : ℝ) = (1/2)" in line for line in stated.setup)
    assert "forsetiDrivers" in stated.to_lane[1]
    assert "· exact fun u ⟨admit, band, start⟩ => (claim u admit band start).1" in (
        stated.to_lane
    )
    assert stated.from_lane[-1] == (
        "exact fun u admit band start => ⟨realizable u ⟨admit, band, start⟩, "
        "unique u ⟨admit, band, start⟩, holds u ⟨admit, band, start⟩⟩"
    )


def test_an_undriven_claim_keeps_the_initial_wires_as_its_only_premise() -> None:
    stated = readable(parse_goal(TRAJECTORY + "V >= 0 and V <= 6"))
    assert stated is not None
    assert "Admitted" not in stated.statement
    assert "forsetiDrivers" not in stated.to_lane[1]
    assert "· exact fun u start => (claim u start).1" in stated.to_lane


def test_an_entailment_claim_quantifies_its_names_over_the_reals() -> None:
    stated = readable(parse_goal("entailment x, y : x >= 0 and y >= x |- y <= 1/3"))
    assert stated is not None
    assert stated.statement == ":\n    ∀ x y : ℝ, x ≥ 0 ∧ y ≥ x → y ≤ 1/3"
    assert stated.description == "x, y are real numbers."
    assert stated.lane == (
        "Gimle.Forseti.Syntax.FormulaEntailment forsetiAntecedent forsetiConsequent"
    )
    assert (stated.root, stated.refuted) == ("root", "refuted")
    assert stated.to_lane[0] == "revert claim"
    assert stated.from_lane[0] == "revert lane"


def test_a_long_entailment_is_broken_over_lines() -> None:
    goal = parse_goal(
        "entailment alpha, beta : alpha*alpha + beta*beta <= 1 and alpha >= 0"
        " |- alpha <= 1 and beta <= 1 and beta >= -1"
    )
    stated = readable(goal)
    assert stated is not None
    assert stated.statement == (
        ":\n    ∀ alpha beta : ℝ,\n"
        "      alpha * alpha + beta * beta ≤ 1 ∧ alpha ≥ 0 →\n"
        "      alpha ≤ 1 ∧ beta ≤ 1 ∧ beta ≥ -1"
    )


def test_a_field_claim_states_the_realized_field_on_the_box() -> None:
    stated = readable(parse_goal(FIELD + "u >= 0 and u <= 3"))
    assert stated is not None
    assert stated.statement == (
        ":\n"
        "    ∀ input, Gimle.Forseti.Examples.HeatFieldBound.Solves .ogf"
        " (_root_.Polynomial.X ^ 2) input →\n"
        "      (Gimle.Asgard.Streams.Heat.circuit .ogf).Defined input ∧\n"
        "      ∃ q, Gimle.Asgard.Streams.Realizes .ogf"
        " ((Gimle.Asgard.Streams.Heat.circuit .ogf).value input 1) q ∧\n"
        "        ∀ t x : ℝ, t ≥ 0 ∧ t ≤ 1 ∧ x ≥ -1 ∧ x ≤ 1 →\n"
        "          have u := Gimle.Asgard.Streams.field q ![t, x]; u ≥ 0 ∧ u ≤ 3"
    )


COEFFICIENTS = (
    "stream formal-heat interpretation formal-stream basis ogf profile x^2"
    " coefficients u(1, 0), u(0, 2), u_xx(0, 0) : "
)
FULL_STREAM = (
    "stream formal-heat interpretation formal-stream basis ogf profile x^2"
    " full-stream u_xx = 2, u = x^2 + 2*t"
)
TRUNCATION = (
    "stream formal-heat interpretation analytic-tail basis egf profile"
    " exponentials [1, 1], [-1/2, 2] truncation u window 8, 16"
    " radius 1/4, 1/2 epsilon 1/1000"
)
FRONT = (
    "stream burgers viscosity 1/2 interpretation analytic-tail basis ogf potential"
    " exponentials [1, 0], [1, -1] truncation u window 16, 16"
    " radius 1/6, 1/8 epsilon 1/10000"
)
TO_INDEX = "Gimle.Asgard.Streams.Lowering.Degrees.toIndex"


def test_a_coefficient_claim_binds_the_coefficients_it_reads() -> None:
    stated = readable(parse_goal(COEFFICIENTS + "c0 >= 2 and c2 >= 1"))
    assert stated is not None
    # c1 is observed but unread, so it is not bound: Lean would warn.
    assert stated.statement == (
        ":\n"
        "    have circuit := Gimle.Asgard.Examples.FormalHeat.circuit\n"
        "    ∀ input, Gimle.Forseti.Examples.FormalHeatContract.admitted input →\n"
        "      circuit.Defined input ∧\n"
        "        have y := circuit.value input\n"
        f"        have c0 : ℝ := y 1 ({TO_INDEX} ![1, 0])\n"
        f"        have c2 : ℝ := y 0 ({TO_INDEX} ![0, 0])\n"
        "        c0 ≥ 2 ∧ c2 ≥ 1"
    )
    assert "c0 is u[t^1, x^0]" in stated.description
    assert "c1 is u[t^0, x^2]" in stated.description
    assert stated.lane == "forsetiContract"
    # Each bound coefficient is tied to the observation's own point by `rfl`.
    heat = "Gimle.Forseti.Examples.StreamObservation.heatOutput"
    assert (
        f"have forsetiC0 : ∀ y p, {heat}.point y ⟨0, p⟩ = "
        f"((y 1 ({TO_INDEX} ![1, 0]) : ℚ) : ℝ) := fun _ _ => rfl" in stated.setup
    )
    assert stated.to_lane[0] == "revert claim"
    assert stated.from_lane[0] == "revert lane"


def test_a_coefficient_claim_reading_nothing_binds_nothing() -> None:
    stated = readable(parse_goal(COEFFICIENTS + "true"))
    assert stated is not None
    assert stated.statement.endswith(
        "        have y := circuit.value input\n        True"
    )


def test_a_full_stream_claim_states_the_polynomials_each_output_realizes() -> None:
    stated = readable(parse_goal(FULL_STREAM))
    assert stated is not None
    assert stated.statement == (
        ":\n"
        "    have circuit := Gimle.Asgard.Examples.FormalHeat.circuit\n"
        "    have t : Gimle.Asgard.Streams.Poly 2 := MvPolynomial.X 0\n"
        "    have x : Gimle.Asgard.Streams.Poly 2 := MvPolynomial.X 1\n"
        "    ∀ input, Gimle.Forseti.Examples.FormalHeatContract.admitted input →\n"
        "      circuit.Defined input ∧\n"
        "        have y := circuit.value input\n"
        "        Gimle.Asgard.Streams.Realizes .ogf (y 0) 2 ∧\n"
        "        Gimle.Asgard.Streams.Realizes .ogf (y 1) (x * x + 2 * t) ∧\n"
        "        Gimle.Forseti.Stream.Boundary 0 1"
        " Gimle.Asgard.Examples.FormalHeat.boundary y"
    )
    assert "y 0 is u_xx and y 1 is u" in stated.description
    # The bridge uses the identities the root proves beside the contract.
    identities = "have identities : forsetiIdentities := "
    assert (
        identities + "(show forsetiIdentities ∧ forsetiContract from root).1"
        in stated.from_root
    )
    assert identities + "refuted.1" in stated.to_root


@pytest.mark.parametrize(
    "value, text",
    [
        (Const(Fraction(2)), "2"),
        (Const(Fraction(-3)), "-3"),
        (Const(Fraction(1, 3)), "MvPolynomial.C (1/3)"),
        (Mul(Const(Fraction(-1, 3)), X), "MvPolynomial.C (-1/3) * x"),
        (Mul(X, Const(Fraction(-2))), "x * (-2)"),
    ],
)
def test_stream_polynomials_write_fractions_as_constants(value, text) -> None:
    assert poly(value, NAMES, stream=True) == text


def test_a_fractional_output_is_a_constant_polynomial_argument() -> None:
    stated = readable(
        parse_goal(
            "stream formal-heat interpretation formal-stream basis ogf profile x^2"
            " full-stream u_xx = 1/3, u = -x"
        )
    )
    assert stated is not None
    assert "Realizes .ogf (y 0) (MvPolynomial.C (1/3)) ∧" in stated.statement
    assert "Realizes .ogf (y 1) (-x) ∧" in stated.statement
    assert "have t :" not in stated.statement
    assert (
        "have forsetiPoly0 : (MvPolynomial.C (mkRat (1) 3) :"
        " Gimle.Asgard.Streams.Poly 2) = MvPolynomial.C (1/3) := "
        "by norm_num [Rat.mkRat_eq_div, map_ofNat]" in stated.setup
    )


def test_a_truncation_needs_an_exponential_sum() -> None:
    goal = parse_goal(TRUNCATION)
    polynomial = dataclasses.replace(goal, profile=Mul(X, X))
    with pytest.raises(ValueError, match="exponential-sum"):
        readable(polynomial)


def test_a_truncation_claim_states_the_bound_on_the_box() -> None:
    stated = readable(parse_goal(TRUNCATION))
    assert stated is not None
    streams = "Gimle.Asgard.Streams"
    assert stated.statement == (
        ":\n"
        f"    have circuit := {streams}.Heat.circuit .egf\n"
        f"    have boundary := {streams}.AnalyticHeat.boundary .egf\n"
        f"      ({streams}.AnalyticHeat.expSum [(1, 1), (-1/2, 2)])\n"
        f"    (∃ u unused u_xx : {streams}.Stream 2,\n"
        "      circuit.Rel ![u, boundary, unused] ![u_xx, u]) ∧\n"
        f"    ∀ u unused u_xx : {streams}.Stream 2,\n"
        "      circuit.Rel ![u, boundary, unused] ![u_xx, u] →\n"
        "      ∀ t x : ℝ, |t| ≤ 1/4 ∧ |x| ≤ 1/2 →\n"
        f"        HasSum ({streams}.seriesTerm .egf u ![t, x])\n"
        f"          ({streams}.analyticField .egf u ![t, x]) ∧\n"
        f"        |{streams}.analyticField .egf u ![t, x] -\n"
        f"          {streams}.windowField .egf ![8, 16] u ![t, x]| ≤ 1/1000"
    )
    assert (stated.lane, stated.root, stated.refuted) == (
        "forsetiRoot",
        "root",
        "refuted",
    )
    assert any("forsetiInside" in line for line in stated.setup)


def test_a_front_truncation_claim_names_the_slice_and_the_viscosity() -> None:
    stated = readable(parse_goal(FRONT))
    assert stated is not None
    streams = "Gimle.Asgard.Streams"
    assert stated.statement == (
        ":\n"
        f"    have circuit := {streams}.Burgers.circuit .ogf (1/2)\n"
        f"    have slice := {streams}.ColeHopf.zeroSlice\n"
        f"      ({streams}.ColeHopf.front (1/2) [(1, 0), (1, -1)])\n"
        f"    (∃ u unused u_t : {streams}.Stream 2,\n"
        "      circuit.Rel ![u, slice, unused] ![u_t, u]) ∧\n"
        f"    ∀ u unused u_t : {streams}.Stream 2,\n"
        "      circuit.Rel ![u, slice, unused] ![u_t, u] →\n"
        "      ∀ t x : ℝ, |t| ≤ 1/6 ∧ |x| ≤ 1/8 →\n"
        f"        HasSum ({streams}.seriesTerm .ogf u ![t, x])\n"
        f"          ({streams}.analyticField .ogf u ![t, x]) ∧\n"
        f"        |{streams}.analyticField .ogf u ![t, x] -\n"
        f"          {streams}.windowField .ogf ![16, 16] u ![t, x]| ≤ 1/10000"
    )
    assert "Cole–Hopf front" in stated.description
    assert any(line.startswith("have forsetiFront :") for line in stated.setup)
    assert any(line.startswith("have forsetiCircuit :") for line in stated.setup)
    assert "rw [forsetiFront, forsetiCircuit]" in stated.to_lane
    # An integer viscosity is a plain numeral; a negative one is parenthesised.
    one = readable(parse_goal(FRONT.replace("viscosity 1/2", "viscosity 1")))
    assert one is not None and "Burgers.circuit .ogf 1\n" in one.statement
    assert (
        "circuit .ogf (mkRat (1) 1) = Gimle.Asgard.Streams.Burgers.circuit .ogf 1"
        in (" ".join(one.setup))
    )
    negative = readable(parse_goal(FRONT.replace("viscosity 1/2", "viscosity -1")))
    assert negative is not None and "Burgers.circuit .ogf (-1)\n" in negative.statement


def test_a_claim_about_a_lane_the_model_lacks_is_refused() -> None:
    for text in (
        "stream burgers interpretation evaluated-field basis ogf profile x^2"
        " field u over t in [0, 1], x in [-1, 1] : u >= 0",
        "stream burgers interpretation formal-stream basis ogf profile x^2"
        " coefficients u(1, 0) : c0 >= 2",
        "stream burgers interpretation formal-stream basis ogf profile x^2"
        " full-stream u = x^2",
    ):
        with pytest.raises(ValueError, match="has no .* lane"):
            readable(parse_goal(text))


@pytest.mark.parametrize(
    "word",
    [
        "using",
        "for",
        "try",
        "catch",
        "finally",
        "unless",
        "break",
        "continue",
        "def",
        "theorem",
        "lemma",
        "example",
        "abbrev",
        "class",
        "structure",
        "inductive",
        "variable",
        "attribute",
        "export",
        "macro",
        "set_option",
        "nomatch",
        "nofun",
        "termination_by",
        "decreasing_by",
        "at",
        "from",
    ],
)
def test_lean_reserved_words_are_written_in_guillemets(word: str) -> None:
    """Each was confirmed to break a Lean binder when written bare (140 review)."""
    from glitnir_forseti.notebook.readable import identifier

    assert identifier(word) == f"«{word}»"
    assert identifier("x") == "x"


def test_a_field_band_claim_states_the_band_on_the_box() -> None:
    from glitnir_forseti.streams.examples import band_goal, front_band

    goal = band_goal(front_band(Fraction(2, 5), Fraction(3, 5)))
    stated = readable(goal)
    assert stated is not None
    streams = "Gimle.Asgard.Streams"
    assert stated.statement == (
        ":\n"
        f"    have circuit := {streams}.Burgers.circuit .ogf (1/2)\n"
        f"    have slice := {streams}.ColeHopf.zeroSlice\n"
        f"      ({streams}.ColeHopf.front (1/2) [(1, 0), (1, -1)])\n"
        f"    ∀ u unused u_t : {streams}.Stream 2,\n"
        "      circuit.Rel ![u, slice, unused] ![u_t, u] →\n"
        "      ∀ t x : ℝ, |t| ≤ 1/6 ∧ |x| ≤ 1/8 →\n"
        f"        HasSum ({streams}.seriesTerm .ogf u ![t, x])\n"
        f"          ({streams}.analyticField .ogf u ![t, x]) ∧\n"
        f"        2/5 ≤ {streams}.analyticField .ogf u ![t, x] ∧\n"
        f"        {streams}.analyticField .ogf u ![t, x] ≤ 3/5"
    )
    assert (
        "analytic field" in stated.description
        and "Cole–Hopf front" in stated.description
    )
    assert stated.lane == "forsetiRoot"
    assert any("forsetiCast1" in line for line in stated.setup)
    upper = readable(band_goal(front_band(None, Fraction(3, 5))))
    assert upper is not None and upper.statement.endswith(
        "analyticField .ogf u ![t, x] ≤ 3/5"
    )
    assert not any("forsetiCast1" in line for line in upper.setup)
    with pytest.raises(ValueError, match="band"):
        readable(
            band_goal(
                FieldClaim(
                    "u",
                    (Fraction(-1, 6), Fraction(1, 6)),
                    (Fraction(-1, 8), Fraction(1, 8)),
                    Atom(Relation.LE, Add(Var(2), Neg(Var(0)))),
                )
            )
        )
    with pytest.raises(ValueError, match="viscosity"):
        readable(band_goal(front_band(None, Fraction(3, 5)), viscosity=None))
    with pytest.raises(ValueError, match="exponential-sum"):
        readable(
            band_goal(front_band(None, Fraction(3, 5)), profile=Mul(Var(0), Var(0)))
        )


def test_an_euler_claim_states_the_start_as_a_table_of_modes() -> None:
    from glitnir_forseti.streams.examples import (
        trig_band,
        trig_truncation,
        vorticity_goal,
    )

    stated = readable(vorticity_goal(trig_truncation(Fraction(1, 100))))
    assert stated is not None
    streams = "Gimle.Asgard.Streams"
    assert stated.statement == (
        ":\n"
        f"    Gimle.Forseti.Fourier.Contract ({streams}.NS.vorticityCircuit .ogf 0)\n"
        f"      (fun input => input 0 0 = {streams}.Torus.Table.toTrig "
        "[((1, 0), (1/2)),"
        " ((-1, 0), (1/2)), ((1, 1), (1/2)), ((-1, -1), (1/2)), ((2, 1), (1/2)),"
        " ((-2, -1), (1/2))])\n"
        "      (fun output =>\n"
        "        have ω := output 0;\n"
        "        (∀ (t : ℝ) (x : ℝ × ℝ), |t| ≤ (1/6480) →\n"
        f"          HasSum ({streams}.TrigStream.seriesTerm ω t x)\n"
        f"            ({streams}.TrigStream.analyticField ω t x)) ∧\n"
        f"        {streams}.TrigStream.TruncationBound ω (1/6480) 3 (1/100))"
    )
    assert (
        "a unique formal output exists and every output ω satisfies"
        in stated.description
    )
    assert stated.lane == "forsetiRoot"
    assert stated.setup[0].startswith(
        f"have forsetiInitial : forsetiStart 0 = {streams}.Torus.Table.toTrig"
    )
    assert any("forsetiCast1" in line for line in stated.setup)
    band = readable(vorticity_goal(trig_band(None, Fraction(3))))
    assert band is not None and band.statement.endswith(
        "∀ (t : ℝ) (x : ℝ × ℝ), |t| ≤ (1/6480) →\n"
        f"          {streams}.TrigStream.analyticField ω t x ≤ 3)"
    )
    assert not any("forsetiCast2" in line for line in band.setup)
    squared = dataclasses.replace(
        vorticity_goal(trig_truncation(Fraction(1, 100))), profile=Mul(Var(0), Var(0))
    )
    with pytest.raises(ValueError, match="start of modes"):
        readable(squared)


STEPPED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks {law} 64"
    " within 256 observe s breach 1 probability <= 1/4"
)


@pytest.mark.parametrize(
    ("law", "shocks"),
    [
        ("variance", "any centred law with variance 64"),
        ("subgaussian", "any sub-Gaussian law with proxy 64"),
    ],
)
def test_a_stepped_claim_describes_its_shocks_as_independent(law, shocks) -> None:
    """The statement fixes one law for every step: the shocks are i.i.d."""
    stated = readable(parse_goal(STEPPED.format(law=law)))
    assert stated is not None
    assert stated.description.startswith(
        f"For independent, identically distributed shocks drawn from {shocks}, the"
        " probability that the exposure s breaches 1 within 256 marks is at most"
        " 1/4. "
    )
    assert "identically distributed" in stated.description
    assert " from any " in stated.description
    assert stated.statement.startswith(":\n    ∀ law : Measure ℝ, ")
    assert stated.lane == "forsetiClaim"
    # The statement is the lane's proposition, which the bridge restates as is.
    assert stated.to_lane == ("exact claim",) and stated.from_lane == ("exact lane",)
