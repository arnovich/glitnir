"""Tests for the out-of-process axiom check (task 129).

The comparison of module reports runs without Lean. The live tests replay the
attacks that defeated reading ``#print axioms`` from the elaborating process,
through `check_unit`, and skip without a configured Lean project.
"""

import json
import subprocess

import pytest
from unit_checking import PROJECT_VARIABLE, check_unit

from glitnir_forseti.lean.independent import (
    IndependentStatus,
    check_independently,
    compare_reports,
    proposition_fingerprint,
)
from glitnir_forseti.lean.session import SessionLimits

TIMEOUT_MS = 600_000


def _constant(name, kind="definition", meaning="two", levels=()):
    """A constant as the report describes it: its canonical entry is opaque."""
    return {
        "name": name,
        "kind": kind,
        "levels": list(levels),
        "meaning": f"[{json.dumps(kind)},{json.dumps(meaning)}]",
    }


#: Canonical propositions as the report writes them; their content is opaque
#: to Python, which only compares and fingerprints them.
STATEMENTS = {"forsetiSearch": '[[],"two = 2"]', "earlier": '[[],"1 = 1"]'}

REFERENCE = {
    "module": "ForsetiIndependent",
    "imports": ["Init"],
    "constants": [
        _constant("two"),
        _constant("earlier", "axiom", "1 = 1"),
        _constant("forsetiSearch", "axiom", "two = 2"),
    ],
    "declaration": "forsetiSearch",
    "axioms": ["forsetiSearch"],
    "statements": STATEMENTS,
}


def _candidate(**changes):
    candidate = {
        "module": "ForsetiIndependent",
        "imports": ["Init"],
        "constants": [
            _constant("two"),
            _constant("earlier", "axiom", "1 = 1"),
            _constant("forsetiSearch", "theorem", "two = 2"),
            _constant("forsetiSearch._proof_1", "theorem", "True"),
        ],
        "declaration": "forsetiSearch",
        "axioms": ["propext", "earlier"],
        "statements": STATEMENTS,
    }
    candidate.update(changes)
    return candidate


def test_an_unchanged_environment_is_confirmed_with_its_axioms() -> None:
    check = compare_reports(REFERENCE, _candidate())
    assert check.status is IndependentStatus.CONFIRMED
    assert check.axioms == ("propext", "earlier")


def test_a_confirmed_check_fingerprints_the_reference_propositions() -> None:
    check = compare_reports(REFERENCE, _candidate())
    assert check.proposition == proposition_fingerprint(STATEMENTS["forsetiSearch"])
    assert dict(check.bound) == {
        "earlier": proposition_fingerprint(STATEMENTS["earlier"])
    }
    assert len(check.proposition) == 64
    assert check.proposition != proposition_fingerprint(STATEMENTS["earlier"])


@pytest.mark.parametrize(
    ("statements", "reason"),
    [
        (
            {**STATEMENTS, "forsetiSearch": '[[],"two = 3"]'},
            "not the reference statement",
        ),
        ({"earlier": STATEMENTS["earlier"]}, "not the reference statement"),
        ({**STATEMENTS, "earlier": '[[],"1 = 2"]'}, "changed earlier"),
    ],
    ids=["theorem", "theorem-missing", "used-axiom"],
)
def test_a_candidate_proposition_that_differs_is_rejected(statements, reason) -> None:
    check = compare_reports(REFERENCE, _candidate(statements=statements))
    assert check.status is IndependentStatus.REJECTED
    assert reason in check.reason


def test_a_proposition_only_the_candidate_reports_is_not_bound() -> None:
    """An axiom the proof added is not the prelude's, so nothing binds it."""
    statements = {**STATEMENTS, "added": '[[],"False"]'}
    check = compare_reports(REFERENCE, _candidate(statements=statements))
    assert check.status is IndependentStatus.CONFIRMED
    assert "added" not in check.bound


@pytest.mark.parametrize(
    "reference",
    [
        {**REFERENCE, "statements": None},
        {**REFERENCE, "statements": {"forsetiSearch": 1}},
        {**REFERENCE, "statements": {"earlier": STATEMENTS["earlier"]}},
        {**REFERENCE, "declaration": "other"},
    ],
    ids=["absent", "not-text", "no-theorem", "other-declaration"],
)
def test_a_malformed_proposition_report_is_unknown(reference) -> None:
    check = compare_reports(reference, _candidate())
    assert check.status is IndependentStatus.UNKNOWN
    assert check.proposition == "" and not check.bound


def _with(*constants):
    """The candidate's constants with ``constants`` replacing their namesakes."""
    replaced = {c["name"]: c for c in constants}
    return [replaced.get(c["name"], c) for c in _candidate()["constants"]]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"imports": ["Init", "Mathlib"]}, "imports"),
        (
            {"constants": [c for c in _candidate()["constants"] if c["name"] != "two"]},
            "dropped two",
        ),
        ({"constants": _with(_constant("two", meaning="3"))}, "changed two"),
        (
            {"constants": _with(_constant("two", "opaque"))},
            "changed two",
        ),
        (
            {"constants": _with(_constant("two", levels=["u"]))},
            "changed two",
        ),
        (
            {"constants": _with(_constant("earlier", "axiom", "1 = 2"))},
            "changed earlier",
        ),
        (
            {
                "constants": _with(
                    _constant("forsetiSearch", "theorem", "two = 2", ["u"])
                )
            },
            "changed forsetiSearch",
        ),
        (
            {"constants": _with(_constant("forsetiSearch", "axiom", "two = 2"))},
            "changed forsetiSearch",
        ),
        ({"declaration": "other"}, "not the reference statement"),
        ({"declaration": None}, "not the reference statement"),
    ],
)
def test_any_change_to_the_reference_is_rejected(changes, reason) -> None:
    check = compare_reports(REFERENCE, _candidate(**changes))
    assert check.status is IndependentStatus.REJECTED
    assert reason in check.reason


@pytest.mark.parametrize("axioms", [None, "propext", [1]])
def test_a_malformed_axiom_list_is_unknown(axioms) -> None:
    check = compare_reports(REFERENCE, _candidate(axioms=axioms))
    assert check.status is IndependentStatus.UNKNOWN


@pytest.mark.parametrize(
    "report",
    ["reference", "candidate"],
)
@pytest.mark.parametrize("meaning", [None, 1, ["two"]])
def test_a_constant_without_a_canonical_entry_is_unknown(report, meaning) -> None:
    """Printed text is never compared, so a report without the entry decides nothing."""
    broken = [
        {**c, "meaning": meaning} if c["name"] == "two" else c
        for c in _candidate()["constants"]
    ]
    if report == "reference":
        check = compare_reports({**REFERENCE, "constants": broken}, _candidate())
    else:
        check = compare_reports(REFERENCE, _candidate(constants=broken))
    assert check.status is IndependentStatus.UNKNOWN
    assert "malformed" in check.reason


def test_without_a_lean_project_the_check_is_unknown(monkeypatch) -> None:
    monkeypatch.delenv("FORSETI_LEAN_PROJECT", raising=False)
    monkeypatch.delenv("FORSETI_LEAN_TEST_PROJECT", raising=False)
    check = check_independently("", ": True", ("trivial",), SessionLimits())
    assert check.status is IndependentStatus.UNKNOWN


# ----- live attacks --------------------------------------------------------------

SWAP_ENVIRONMENT = (
    "run_tac do",
    "  let env ← Lean.importModules #[{ module := `Mathlib }] {}",
    "  let decl := Lean.Declaration.defnDecl { name := `two, levelParams := [], "
    "type := Lean.mkConst `Nat, value := Lean.mkNatLit 3, hints := .abbrev, "
    "safety := .safe }",
    "  let main ← Lean.getEnv",
    "  Lean.setEnv (env.setMainModule main.mainModule)",
    "  Lean.addDecl decl",
    "exact rfl",
)


@pytest.fixture
def lean_project(lean_test_project, monkeypatch):
    """Point `check_unit` at the configured release."""
    monkeypatch.setenv(PROJECT_VARIABLE, str(lean_test_project))
    return lean_test_project


def test_a_proof_that_swaps_the_environment_is_not_accepted(lean_project) -> None:
    result = check_unit(
        ": two = 3",
        SWAP_ENVIRONMENT,
        prelude="import Mathlib\n\ndef two : Nat := 2",
        timeout_ms=TIMEOUT_MS,
    )
    assert result["accepted"] is False
    assert result["independent"]["status"] == "rejected"
    assert "changed two" in result["independent"]["reason"]


def test_a_hidden_axiom_behind_a_redefined_report_is_not_accepted(
    lean_project,
) -> None:
    prelude = (
        "import Mathlib\n\n"
        "macro_rules | `(#print axioms $_) => "
        "`(#eval IO.println \"'x' does not depend on any axioms\")\n\n"
        "axiom cheat : False"
    )
    result = check_unit(
        ": (2 : ℕ) = 3", ("exact cheat.elim",), prelude=prelude, timeout_ms=TIMEOUT_MS
    )
    assert result["accepted"] is False


def test_an_honest_proof_is_still_accepted(lean_project) -> None:
    result = check_unit("(p : Prop) (h : p) : p", ("exact h",), timeout_ms=TIMEOUT_MS)
    assert result["accepted"] is True, result
    assert result["independent"]["status"] == "confirmed"


# ----- let in a statement (task 147) ---------------------------------------------

#: Lean turns a theorem's non-dependent ``let`` into ``have`` but keeps an
#: axiom's as written; the report prints both without that kernel-ignored flag.
LET_STATEMENT = ": let a := 1; a ≤ 2"


def test_a_let_statement_with_a_correct_proof_is_accepted(lean_project) -> None:
    result = check_unit(
        LET_STATEMENT,
        ("intro a", "decide"),
        prelude="import Lean",
        timeout_ms=TIMEOUT_MS,
    )
    assert result["accepted"] is True, result
    assert result["independent"]["status"] == "confirmed"


def test_a_let_statement_with_a_wrong_proof_is_rejected(lean_project) -> None:
    result = check_unit(
        ": let a := 3; a ≤ 2",
        ("intro a", "decide"),
        prelude="import Lean",
        timeout_ms=TIMEOUT_MS,
    )
    assert result["accepted"] is False
    assert result["errors"], result


def _swapped_candidate(monkeypatch, statement: str, script: tuple[str, ...]):
    """Build the candidate from ``statement`` while the reference keeps its own."""
    from glitnir_forseti.lean import independent

    real = independent.render_unit
    monkeypatch.setattr(
        independent,
        "render_unit",
        lambda prelude, _statement, _script: real(prelude, statement, script),
    )
    return check_independently(
        "import Lean", LET_STATEMENT, ("decide",), SessionLimits(timeout_ms=TIMEOUT_MS)
    )


@pytest.mark.parametrize(
    "statement, script",
    [
        (": let a := 0; a ≤ 2", ("intro a", "decide")),
        (": let a := 1; a ≤ 3", ("intro a", "decide")),
        (": let a := 1; a < 2", ("intro a", "decide")),
        (": let a : Int := 1; a ≤ 2", ("intro a", "decide")),
        (": (1 : Nat) ≤ 2", ("decide",)),
        (": let a := 1; let c := 0; a ≤ 2", ("intro a c", "decide")),
    ],
    ids=["value", "bound", "relation", "type", "zeta", "extra-let"],
)
def test_a_let_statement_changed_beyond_elaboration_is_refused(
    lean_project, monkeypatch, statement, script
) -> None:
    check = _swapped_candidate(monkeypatch, statement, script)
    assert check.status is IndependentStatus.REJECTED, check
    assert "not the reference statement" in check.reason


@pytest.mark.parametrize(
    "statement, script",
    [
        (": have a := 1; a ≤ 2", ("intro a", "decide")),
        (": let b := 1; b ≤ 2", ("intro b", "decide")),
    ],
    ids=["have", "binder-name"],
)
def test_a_let_differing_only_in_what_the_kernel_ignores_is_the_same_statement(
    lean_project, monkeypatch, statement, script
) -> None:
    """``nondep`` and binder names: the kernel reads the same term (task 154)."""
    check = _swapped_candidate(monkeypatch, statement, script)
    assert check.status is IndependentStatus.CONFIRMED, check


# ----- propositions, not statement text (task 150) -------------------------------

#: A term that elaborates to ``True`` exactly when the environment holds
#: ``marker``, like the probe that made a checked claim vouch for ``False``
#: where it was assumed (task 150's r1).
PROBE = """import Lean

open Lean Elab Term in
elab "probe" : term => do
  return if (← getEnv).contains `marker then mkConst ``True else mkConst ``False
"""


def _bound(prelude: str, name: str) -> str:
    check = check_independently(
        prelude,
        ": True",
        ("trivial",),
        SessionLimits(timeout_ms=TIMEOUT_MS),
        bind=(name,),
    )
    assert check.status is IndependentStatus.CONFIRMED, check
    return check.bound[name]


def test_the_same_text_in_another_environment_is_another_proposition(
    lean_project,
) -> None:
    proved = check_independently(
        PROBE + "\naxiom marker : True",
        ": probe",
        ("trivial",),
        SessionLimits(timeout_ms=TIMEOUT_MS),
    )
    assert proved.status is IndependentStatus.CONFIRMED, proved
    assumed = _bound(PROBE + "\naxiom forsetiClaim_0 : probe", "forsetiClaim_0")
    assert assumed != proved.proposition
    same = _bound(
        PROBE + "\naxiom marker : True\naxiom forsetiClaim_0 : probe",
        "forsetiClaim_0",
    )
    assert same == proved.proposition


#: Universe parameters under their names, and what the positions keep apart.
_UNIVERSE_CASES = {
    "u": "axiom forsetiClaim_0.{u} (α : Sort u) (a : α) : a = a",
    "renamed": "axiom forsetiClaim_0.{w} (α : Sort w) (a : α) : a = a",
    "two": "axiom forsetiClaim_0.{u, v} (α : Sort u) (β : Sort v) : True",
    "two-renamed": "axiom forsetiClaim_0.{a, b} (α : Sort a) (β : Sort b) : True",
    "two-swapped": "axiom forsetiClaim_0.{v, u} (α : Sort u) (β : Sort v) : True",
    "succ": "axiom forsetiClaim_0.{u} (α : Sort (u+1)) (a : α) : a = a",
    "reached": "def D.{u} (α : Sort u) : Sort u := α\n"
    "axiom forsetiClaim_0.{v} (α : Sort v) : D α = α",
    "reached-renamed": "def D.{w} (α : Sort w) : Sort w := α\n"
    "axiom forsetiClaim_0.{x} (α : Sort x) : D α = α",
    "reached-swapped": "def D.{w, y} (α : Sort w) (β : Sort y) : Sort w := α\n"
    "axiom forsetiClaim_0.{x} (α : Sort x) : D.{x, x} α α = α",
    "reached-two": "def D.{y, w} (α : Sort w) (β : Sort y) : Sort w := α\n"
    "axiom forsetiClaim_0.{x} (α : Sort x) : D.{x, x} α α = α",
}


def test_universe_parameter_names_are_not_part_of_a_proposition(
    lean_project,
) -> None:
    """They are bound per declaration and instantiated by position (task 176)."""
    bound = {
        key: _bound(f"import Lean\n{text}", "forsetiClaim_0")
        for key, text in _UNIVERSE_CASES.items()
    }
    assert bound["u"] == bound["renamed"]
    assert bound["two"] == bound["two-renamed"]
    assert bound["reached"] == bound["reached-renamed"]
    # The order of the parameters, and where each is used, still count.
    assert bound["two"] != bound["two-swapped"]
    assert bound["u"] != bound["succ"]
    assert bound["reached-two"] != bound["reached-swapped"]


def test_a_definition_the_statement_names_is_part_of_its_proposition(
    lean_project,
) -> None:
    """``flag`` is the same text on both sides, but not the same definition."""
    statement = "\ndef flag : Prop := probe\naxiom forsetiClaim_0 : flag"
    unmarked = _bound(PROBE + statement, "forsetiClaim_0")
    marked = _bound(PROBE + "\naxiom marker : True" + statement, "forsetiClaim_0")
    assert unmarked != marked


def test_a_match_auxiliary_agrees_under_another_name(lean_project) -> None:
    """``first.match_1`` and ``second.match_1`` are compared by content."""
    statement = ": ∀ n : Nat, (match n with | 0 => True | k + 1 => 0 < k + 1)"
    other = ": ∀ n : Nat, (match n with | 0 => True | k + 1 => k < k + 1)"
    first = _bound(f"axiom first {statement}", "first")
    assert first == _bound(f"axiom second {statement}", "second")
    assert first != _bound(f"axiom second {other}", "second")


# ----- match and section variables (task 154) -----------------------------------

#: A ``match`` makes an auxiliary ``Foo.forsetiSearch.match_1``, and both it and
#: the statement carry binder names whose hygiene differs between two modules.
MATCH_PRELUDE = "import Lean\n\nnamespace Foo"
MATCH = ": ∀ n : Nat, (match n with | 0 => 0 | k + 1 => k) ≤ n"
SPLIT = ("intro n", "split <;> omega")
#: An anonymous section instance is a hygienic binder of the statement.
VARIABLES = "import Lean\n\nsection\nvariable {α : Type} [Inhabited α]"
DEFAULT = ": (default : α) = default"


def _swapped(
    monkeypatch,
    prelude: str,
    statement: str,
    script: tuple[str, ...],
    *,
    candidate_prelude: str | None = None,
    candidate_statement: str | None = None,
):
    """Check a candidate built from other text than the reference's."""
    from glitnir_forseti.lean import independent

    real = independent.render_unit
    monkeypatch.setattr(
        independent,
        "render_unit",
        lambda _prelude, _statement, _script: real(
            candidate_prelude or prelude, candidate_statement or statement, script
        ),
    )
    return check_independently(
        prelude, statement, ("sorry",), SessionLimits(timeout_ms=TIMEOUT_MS)
    )


@pytest.mark.parametrize(
    ("prelude", "statement", "script"),
    [
        (MATCH_PRELUDE, MATCH, SPLIT),
        (
            MATCH_PRELUDE + "\ndef forsetiSearch.match_1 : Nat := 0",
            MATCH,
            SPLIT,
        ),
        (VARIABLES, DEFAULT, ("rfl",)),
    ],
    ids=["match", "match-name-taken", "variables"],
)
def test_match_and_section_variables_are_accepted(
    lean_project, prelude, statement, script
) -> None:
    result = check_unit(statement, script, prelude=prelude, timeout_ms=TIMEOUT_MS)
    assert result["accepted"] is True, result
    assert result["independent"]["status"] == "confirmed"


@pytest.mark.parametrize(
    ("prelude", "statement", "script"),
    [
        (VARIABLES + " [DecidableEq α]", DEFAULT, ("rfl",)),
        (
            "import Lean\n\nsection\nvariable {α : Type} (h : Nonempty α)\n"
            "include h",
            ": ∃ x : α, x = x",
            ("exact ⟨Classical.choice h, rfl⟩",),
        ),
        (
            "import Lean\n\nclass Fact (p : Prop) : Prop where\n  out : p\n\n"
            "section\nvariable [h : Fact False]",
            ": (1 : Nat) = 2",
            ("exact absurd h.out id",),
        ),
    ],
    ids=["unused-instance", "include", "vacuous-instance"],
)
def test_a_theorem_taking_variables_its_axiom_drops_is_refused(
    lean_project, prelude, statement, script
) -> None:
    """A theorem takes these section variables and the later axiom does not.

    The two readings are different propositions (the last proves ``1 = 2``
    from ``Fact False``), so no reading is confirmed (task 154).
    """
    result = check_unit(statement, script, prelude=prelude, timeout_ms=TIMEOUT_MS)
    assert result["accepted"] is False
    assert result["independent"]["status"] == "rejected"
    assert "not the reference statement" in result["independent"]["reason"]


@pytest.mark.parametrize(
    ("statement", "script"),
    [
        (": ∀ n : Nat, (match n with | 0 => 0 | 1 => 0 | k + 2 => k) ≤ n", SPLIT),
        (": ∀ n : Nat, (match n with | 0 => 0 | _ => n - 1) ≤ n", SPLIT),
        (": ∀ n : Nat, (match n with | 0 => 0 | k + 1 => k - 1) ≤ n", SPLIT),
        (": ∀ n : Nat, (match n with | 0 => 0 | k + 1 => k) < n + 1", SPLIT),
    ],
    ids=["arms", "pattern", "value", "body"],
)
def test_a_different_match_is_refused(lean_project, monkeypatch, statement, script):
    check = _swapped(
        monkeypatch, MATCH_PRELUDE, MATCH, script, candidate_statement=statement
    )
    assert check.status is IndependentStatus.REJECTED, check
    # A changed auxiliary is found first, a changed statement over the same
    # auxiliary by its proposition; never by failing to compile.
    assert check.reason in (
        "the candidate changed Foo.forsetiSearch.match_1",
        "the candidate's theorem is not the reference statement",
    )


def test_a_forged_match_auxiliary_is_refused(lean_project, monkeypatch) -> None:
    """The prelude's ``forsetiSearch.match_1`` is compared by content."""
    prelude = MATCH_PRELUDE + "\ndef forsetiSearch.match_1 : Nat := 0"
    check = _swapped(
        monkeypatch,
        prelude,
        MATCH,
        SPLIT,
        candidate_prelude=MATCH_PRELUDE + "\ndef forsetiSearch.match_1 : Nat := 1",
    )
    assert check.status is IndependentStatus.REJECTED, check
    assert "changed Foo.forsetiSearch.match_1" in check.reason


@pytest.mark.parametrize(
    ("prelude", "statement", "script"),
    [
        (
            "import Lean\n\nsection\nvariable {α : Type 1} [Inhabited α]",
            DEFAULT,
            ("rfl",),
        ),
        (VARIABLES + " [DecidableEq α]", DEFAULT, ("rfl",)),
        (VARIABLES + " [i : Inhabited α]", DEFAULT, ("rfl",)),
        (
            "import Lean\n\nsection\nvariable {α : Type} [Nonempty α]",
            ": (Classical.choice inferInstance : α) = Classical.choice inferInstance",
            ("rfl",),
        ),
        (
            VARIABLES + " {β : Type} [Inhabited β]",
            ": (default : α) = default ∧ (default : β) = default",
            ("exact ⟨rfl, rfl⟩",),
        ),
    ],
    ids=["type", "extra-instance", "other-instance", "other-class", "extra-variable"],
)
def test_different_section_variables_are_refused(
    lean_project, monkeypatch, prelude, statement, script
) -> None:
    check = _swapped(
        monkeypatch,
        VARIABLES,
        DEFAULT,
        script,
        candidate_prelude=prelude,
        candidate_statement=statement,
    )
    assert check.status is IndependentStatus.REJECTED, check
    assert "not the reference statement" in check.reason


# ----- each step, against a scripted toolchain ---------------------------------


class _Toolchain:
    """Answer the check's Lean processes from a script, without Lean."""

    def __init__(self, **overrides):
        self.overrides = overrides
        self.calls: list[list[str]] = []
        self.sources: dict[str, str] = {}

    def __call__(self, arguments, *, lean_path=None, cwd=None, **_):
        from glitnir_forseti.lean import independent

        self.calls.append(list(arguments))
        program = arguments[0]
        if program == "lean" and "-o" in arguments:
            step = "reference" if cwd.name == "reference" else "candidate"
            self.sources[step] = (cwd / f"{independent.MODULE}.lean").read_text()
            result = self.overrides.get(step, independent._Completed(0, "", ""))
            if result is not None and result.returncode == 0:
                (cwd / f"{independent.MODULE}.olean").write_bytes(b"olean")
            return result
        if program == "leanchecker":
            return self.overrides.get("replay", independent._Completed(0, "", ""))
        step = "report_" + lean_path.name.removesuffix("-module")
        default = REFERENCE if lean_path.name.startswith("reference") else _candidate()
        return self.overrides.get(
            step, independent._Completed(0, json.dumps(default), "")
        )


@pytest.fixture
def toolchain(monkeypatch, tmp_path):
    from glitnir_forseti.lean import independent

    project = tmp_path / "project"
    project.mkdir()
    shared = tmp_path / "libs" / ".lake/build/lib/lean/Gimle/Asgard.olean"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"olean")
    monkeypatch.setattr(independent, "select_project", lambda project_: project)
    monkeypatch.setattr(independent, "sandbox_available", lambda: True)
    monkeypatch.setattr(independent.shutil, "which", lambda name: "/bin/elan")
    monkeypatch.setattr(
        independent, "package_roots", lambda *args: {"asgard": tmp_path / "libs"}
    )
    monkeypatch.setattr(independent, "lean_module_setup", lambda *args: "{}")

    def install(**overrides):
        fake = _Toolchain(**overrides)
        monkeypatch.setattr(independent, "_run", fake)
        return fake

    return install


def _completed(code=0, out="", err=""):
    from glitnir_forseti.lean.independent import _Completed

    return _Completed(code, out, err)


def test_scripted_steps_confirm_an_unchanged_candidate(toolchain) -> None:
    fake = toolchain()
    check = check_independently("def two := 2", ": two = 2", ("rfl",), SessionLimits())
    assert check.status is IndependentStatus.CONFIRMED
    assert [call[0] for call in fake.calls] == [
        "lean",
        "lean",
        "leanchecker",
        "lean",
        "lean",
    ]


def test_the_reference_states_the_candidates_statement_as_an_axiom(toolchain) -> None:
    """Later claims assume a claim as an axiom, so the reference is one."""
    fake = toolchain()
    check_independently("def two := 2\n", "\n: two = 2 ", ("rfl",), SessionLimits())
    assert fake.sources["reference"] == (
        "def two := 2\naxiom forsetiSearch : two = 2\n"
    )
    assert fake.sources["candidate"].startswith(
        "def two := 2\ntheorem forsetiSearch : two = 2 := by\n  rfl\n"
    )


def test_the_reference_keeps_an_explicit_universe_list(toolchain) -> None:
    fake = toolchain()
    check_independently("", ".{u} (α : Sort u) : α = α", ("rfl",), SessionLimits())
    reference = fake.sources["reference"]
    assert reference == "\naxiom forsetiSearch.{u} (α : Sort u) : α = α\n"


@pytest.mark.parametrize(
    ("overrides", "status", "reason"),
    [
        ({"reference": None}, IndependentStatus.UNKNOWN, "reference: compilation"),
        (
            {"reference": _completed(1, '{"severity":"error","data":"bad"}')},
            IndependentStatus.UNKNOWN,
            "reference: did not compile: bad",
        ),
        ({"candidate": None}, IndependentStatus.UNKNOWN, "candidate: compilation"),
        (
            {"candidate": _completed(1, "not json\n")},
            IndependentStatus.REJECTED,
            "did not compile: Lean exited with an error",
        ),
        (
            {"candidate": _completed(153, "")},
            IndependentStatus.UNKNOWN,
            "candidate: compilation was stopped by a limit",
        ),
        (
            {"reference": _completed(137, "")},
            IndependentStatus.UNKNOWN,
            "reference: compilation was stopped by a limit",
        ),
        (
            {"candidate": _completed(129, "")},
            IndependentStatus.REJECTED,
            "did not compile: Lean exited with an error",
        ),
        (
            {"candidate": _completed(1, '{"severity":"error","data":""}')},
            IndependentStatus.REJECTED,
            "did not compile: error",
        ),
        (
            {
                "candidate": _completed(
                    1,
                    '{"severity":"error","data":"(deterministic) timeout at '
                    "`whnf`, maximum number of heartbeats (200000) has been "
                    'reached"}',
                )
            },
            IndependentStatus.UNKNOWN,
            "ran out of heartbeats",
        ),
        ({"replay": None}, IndependentStatus.UNKNOWN, "kernel replay did not"),
        (
            {"replay": _completed(1, "", "uncaught exception: bad\n")},
            IndependentStatus.REJECTED,
            "kernel replay rejected the candidate: uncaught exception: bad",
        ),
        ({"report_candidate": None}, IndependentStatus.UNKNOWN, "did not complete"),
        (
            {"report_candidate": _completed(4, "", "expected one constant\n")},
            IndependentStatus.REJECTED,
            "expected one constant",
        ),
        (
            {"report_candidate": _completed(0, "not json")},
            IndependentStatus.UNKNOWN,
            "not JSON",
        ),
        (
            {"report_reference": _completed(0, "[]")},
            IndependentStatus.UNKNOWN,
            "malformed",
        ),
    ],
)
def test_each_step_that_fails_decides_the_outcome(
    toolchain, overrides, status, reason
) -> None:
    toolchain(**overrides)
    check = check_independently("def two := 2", ": two = 2", ("rfl",), SessionLimits())
    assert check.status is status
    assert reason in check.reason


def test_the_reference_describes_what_the_candidate_uses_and_what_is_bound(
    toolchain,
) -> None:
    fake = toolchain()
    check = check_independently(
        "def two := 2", ": two = 2", ("rfl",), SessionLimits(), bind=("target",)
    )
    assert check.status is IndependentStatus.CONFIRMED
    candidate, reference = fake.calls[-2:]
    assert candidate[-2:] == ["ForsetiIndependent", "forsetiSearch"]
    assert candidate[-3].endswith("AxiomReport.lean")
    assert reference[-4:] == [
        "forsetiSearch",
        "forsetiSearch",
        "earlier",
        "target",
    ]


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [(True, "module"), (False, "output")],
)
def test_only_compile_steps_may_write_a_full_module_file(
    monkeypatch, tmp_path, cwd, expected
) -> None:
    """A module file is far larger than any diagnostics, so it has its own cap."""
    from glitnir_forseti.lean import independent

    captured = {}

    def popen(command, **options):
        captured.update(options)
        raise OSError("stop here")

    monkeypatch.setattr(independent, "confine", lambda command, **_: list(command))
    monkeypatch.setattr(
        independent, "lean_process_environment", lambda *_: {"LEAN_PATH": ""}
    )
    monkeypatch.setattr(independent.subprocess, "Popen", popen)
    monkeypatch.setattr(independent, "lean_sysroot", lambda elan: "/test/lean")
    limits = SessionLimits()
    with pytest.raises(OSError, match="stop here"):
        independent._run(
            ["lean"],
            limits=limits,
            project=tmp_path,
            elan="/bin/elan",
            shared=tmp_path,
            report=tmp_path / "AxiomReport.lean",
            cwd=tmp_path if cwd else None,
        )
    cap = captured["preexec_fn"].args[1]
    assert cap == (
        independent.MODULE_FILE_BYTES
        if expected == "module"
        else limits.max_output_bytes
    )
    assert independent.MODULE_FILE_BYTES > 64 * limits.max_output_bytes
    # leanchecker would otherwise start ``lean`` to find its toolchain.
    assert captured["env"]["LEAN_SYSROOT"] == "/test/lean"


def _run_once(independent, tmp_path):
    return independent._run(
        ["leanchecker", "M"],
        limits=SessionLimits(),
        project=tmp_path,
        elan="/bin/elan",
        shared=tmp_path,
        report=tmp_path / "AxiomReport.lean",
    )


def test_a_step_without_its_toolchain_prefix_does_not_complete(
    monkeypatch, tmp_path
) -> None:
    from glitnir_forseti.lean import independent

    def missing(elan):
        raise subprocess.TimeoutExpired(elan, 10)

    monkeypatch.setattr(independent, "lean_sysroot", missing)
    monkeypatch.setattr(
        independent, "lean_process_environment", lambda *_: {"LEAN_PATH": ""}
    )
    monkeypatch.setattr(
        independent.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("no process may start"),
    )
    assert _run_once(independent, tmp_path) == independent._Stopped("toolchain")


def test_a_failed_sandbox_supervisor_decides_nothing(monkeypatch, tmp_path) -> None:
    """The supervisor's own failure is not Lean's exit status, so not a verdict."""
    from glitnir_forseti.lean import independent

    class Process:
        returncode = None

        def wait(self, timeout):
            self.returncode = independent.SUPERVISOR_FAILED
            return self.returncode

    monkeypatch.setattr(independent, "lean_sysroot", lambda elan: "/test/lean")
    monkeypatch.setattr(independent, "confine", lambda command, **_: list(command))
    monkeypatch.setattr(
        independent, "lean_process_environment", lambda *_: {"LEAN_PATH": ""}
    )
    monkeypatch.setattr(independent.subprocess, "Popen", lambda *a, **k: Process())
    assert _run_once(independent, tmp_path) == independent._Stopped("sandbox")


def test_the_shared_namespace_is_linked_first_on_the_search_path(
    monkeypatch, tmp_path
) -> None:
    from glitnir_forseti.lean import independent

    first = tmp_path / "a" / ".lake/build/lib/lean/Gimle/Forseti.olean"
    second = tmp_path / "b" / ".lake/build/lib/lean/Gimle/Asgard/Core.olean"
    duplicate = tmp_path / "b" / ".lake/build/lib/lean/Gimle/Forseti.olean"
    for path in (first, second, duplicate):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.parent.parent.parent.name.encode())
    monkeypatch.setattr(
        independent,
        "package_roots",
        lambda *args: {"forseti": tmp_path / "a", "asgard": tmp_path / "b"},
    )
    merged = independent._merge_shared_namespaces(tmp_path, tmp_path / "merged")
    assert (merged / "Gimle/Forseti.olean").resolve() == first.resolve()
    assert (merged / "Gimle/Asgard/Core.olean").resolve() == second.resolve()


def test_the_report_program_is_the_pinned_one() -> None:
    import hashlib

    from glitnir_forseti.lean import independent

    digest = hashlib.sha256(independent.REPORT_PROGRAM.read_bytes()).hexdigest()
    assert digest == independent.REPORT_SHA256


def test_a_modified_report_program_is_refused(toolchain, monkeypatch, tmp_path) -> None:
    from glitnir_forseti.lean import independent

    forged = tmp_path / "AxiomReport.lean"
    forged.write_text("def main : IO Unit := pure ()\n")
    monkeypatch.setattr(independent, "REPORT_PROGRAM", forged)
    fake = toolchain()
    check = check_independently("def two := 2", ": two = 2", ("rfl",), SessionLimits())
    assert check.status is IndependentStatus.UNKNOWN
    assert "modified" in check.reason
    assert fake.calls == []


def test_without_a_sandbox_the_check_refuses_to_run(toolchain, monkeypatch) -> None:
    from glitnir_forseti.lean import independent

    monkeypatch.setattr(independent, "sandbox_available", lambda: False)
    monkeypatch.setenv("GLITNIR_UNSANDBOXED", "1")
    fake = toolchain()
    check = check_independently("def two := 2", ": two = 2", ("rfl",), SessionLimits())
    assert check.status is IndependentStatus.UNKNOWN
    assert "sandbox" in check.reason
    assert fake.calls == []


def _io_attack(target: str) -> tuple[str, ...]:
    """Overwrite a file during the candidate build; exit early elsewhere."""
    return (
        "run_tac do",
        "  let here ← IO.currentDir",
        '  if here.fileName == some "candidate" then',
        f'    IO.FS.writeFile "{target}" "def main : IO Unit := pure ()"',
        "  else",
        "    IO.Process.exit 0",
        "sorry",
    )


def test_a_proof_cannot_rewrite_the_report_program_to_prove_false(
    lean_project,
) -> None:
    from glitnir_forseti.lean import independent

    before = independent.REPORT_PROGRAM.read_bytes()
    result = check_unit(
        ": False",
        _io_attack(str(independent.REPORT_PROGRAM)),
        prelude="import Mathlib",
        timeout_ms=TIMEOUT_MS,
    )
    assert result["accepted"] is False
    assert independent.REPORT_PROGRAM.read_bytes() == before


def test_a_proof_that_fakes_a_resource_limit_is_at_most_unknown(
    lean_project,
) -> None:
    """Exiting as if killed by the file-size limit cannot be told apart from it.

    The proof then turns its own rejection into unknown, never into acceptance
    (invariant 4 already treats every limit as unknown).
    """
    script = (
        "run_tac do",
        '  if (← IO.currentDir).fileName == some "candidate" then',
        "    IO.Process.exit 153",
        "trivial",
    )
    result = check_unit(": True", script, prelude="import Lean", timeout_ms=TIMEOUT_MS)
    assert result["errors"] == [], result
    assert result["accepted"] is False
    assert result["independent"]["status"] == "unknown"


def test_a_proof_cannot_write_outside_its_scratch_directory(
    lean_project, tmp_path
) -> None:
    target = tmp_path / "written-by-a-proof.txt"
    script = (
        "run_tac do",
        f'  IO.FS.writeFile "{target}" "x"',
        "trivial",
    )
    result = check_unit(
        ": True", script, prelude="import Mathlib", timeout_ms=TIMEOUT_MS
    )
    assert result["accepted"] is False
    assert not target.exists()


def test_only_the_candidates_module_file_reaches_the_later_steps(tmp_path) -> None:
    from glitnir_forseti.lean import independent

    build = tmp_path / "candidate"
    build.mkdir()
    (build / f"{independent.MODULE}.olean").write_bytes(b"module")
    (build / "Mathlib.olean").write_bytes(b"forged")
    (build / "Init").mkdir()
    (build / "Init" / "Core.olean").write_bytes(b"forged")
    isolated = independent._isolate(build, tmp_path / "candidate-module")
    assert sorted(p.name for p in isolated.iterdir()) == [f"{independent.MODULE}.olean"]

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / f"{independent.MODULE}.olean").symlink_to(build / "Mathlib.olean")
    assert list(independent._isolate(linked, tmp_path / "other").iterdir()) == []


def test_a_proposition_fingerprint_is_bound_to_the_release_pin(monkeypatch) -> None:
    """Give another fingerprint under another release pin.

    Imported constants are only named, so the pin is part of what a
    fingerprint means.
    """
    from glitnir_forseti.lean import independent

    same = independent.proposition_fingerprint("[x]")
    assert same == independent.proposition_fingerprint("[x]")
    monkeypatch.setattr(independent, "RELEASE_SHA256", "0" * 64)
    assert independent.proposition_fingerprint("[x]") != same
