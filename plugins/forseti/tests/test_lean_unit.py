"""The independent replay renderer survives retirement of whole-unit search."""

import pytest

from glitnir_forseti.lean.unit import THEOREM_NAME, render_unit


def test_replay_unit_binds_claim_and_prints_axioms():
    source = render_unit("import Init", "(n : Nat) : n = n", ("rfl",))
    assert source == (
        f"import Init\ntheorem {THEOREM_NAME} (n : Nat) : n = n := by\n"
        f"  rfl\n\n#print axioms {THEOREM_NAME}\n"
    )


@pytest.mark.parametrize(
    "prelude,statement,script,error",
    [
        ("import Init", " ", (), ValueError),
        ("import Init", ": True", ("skip\nsorry",), ValueError),
        ("import Init", ": True", ("",), ValueError),
        ("import Init", ": True", ["trivial"], TypeError),
        (None, ": True", (), TypeError),
        ("x" * 65537, ": True", (), ValueError),
        ("import Init", "x" * 65537, (), ValueError),
    ],
)
def test_invalid_source_cannot_reach_replay(prelude, statement, script, error):
    with pytest.raises(error):
        render_unit(prelude, statement, script)


def test_an_explicit_universe_list_is_glued_to_the_name():
    source = render_unit("import Init", ".{u} (α : Sort u) : α = α", ("rfl",))
    assert f"theorem {THEOREM_NAME}.{{u}} (α : Sort u) : α = α := by\n" in source


def test_an_entry_continues_onto_blank_or_indented_lines_as_written() -> None:
    unit = render_unit("import Init", ": True", ('have h : "a\n\n   \n b" = x', "t"))
    assert ':= by\n  have h : "a\n\n   \n b" = x\n  t\n\n#print' in unit


@pytest.mark.parametrize(
    "script",
    [
        ('have h : "a\nb" = x', "trivial"),
        ('have h : "a\n#print axioms x" = y',),
        ("have h : '\n' = c",),
        ('have h : r#"a\nb"# = x',),
        ('have h : "a\n\n  \nb\n  c" = x',),
    ],
)
def test_a_script_may_reach_column_zero_inside_a_literal(script) -> None:
    unit = render_unit("import Init", ": True", script)
    assert "\n".join(f"  {entry}" for entry in script) in unit


@pytest.mark.parametrize(
    "script",
    [
        ("skip\nsorry",),
        ('have h := "a"\nsorry',),
        ("trivial\ntheorem x : False := sorry",),
        ('have h : "a" = y\n#print axioms x',),
        ("\n  trivial",),
        ("  \n  trivial",),
        # Unsettled: a string holding "{" may be interpolated.
        ('have h : s!"{1}\nb" = x',),
        # A comment is not a literal.
        ("skip /-\nx -/",),
        # Left open: the line after it would be the unit's.
        ('have h : "a\nb',),
    ],
)
def test_a_script_line_at_column_zero_outside_a_literal_is_refused(script) -> None:
    with pytest.raises(ValueError):
        render_unit("import Init", ": True", script)


@pytest.mark.parametrize(
    "prelude,script",
    [
        # Each entry alone looks fine; together the second leaves code at 0.
        (
            "import Mathlib",
            ('have := "', 'skip\n  "\naxiom evil : False\ndef q := "', "trivial"),
        ),
        # The prelude leaves a string open.
        ('import Mathlib\ndef s := "', ('have h : "a\nb" = x',)),
    ],
)
def test_render_unit_reads_the_script_in_its_unit(prelude, script) -> None:
    with pytest.raises(ValueError):
        render_unit(prelude, ": True", script)
