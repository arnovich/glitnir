"""Tests for the scanner that finds Lean comment and literal lines."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from glitnir.lean.lexing import code_tokens, only_comments, scan


def test_the_lexer_places_the_line_inside_the_literal() -> None:
    found = scan('have h : "a\nb".length = 3 := by decide\ntrivial')
    assert found.settled and found.literal_lines == {1}


@pytest.mark.parametrize(
    "source,lines",
    [
        ('a "x\ny\n\nz" b\nc', {1, 2, 3}),
        ('r#"x\n"y"#\nc', {1}),
        ("«x\ny» z\nc", {1}),
        ("f '\n'\nc", {1}),
        ('"x\\\n  y"', {1}),
        ("/- x\ny -/\nc", set()),
        ('"x" "y"\n"z"', set()),
    ],
)
def test_scan_reports_the_lines_that_start_inside_a_literal(source, lines) -> None:
    assert scan(source).literal_lines == frozenset(lines)


@pytest.mark.parametrize(
    "source,open_at_end",
    [
        ('a "x\ny', True),
        ('r#"x\n"', True),
        ("«x\ny", True),
        ("/- x\ny", True),
        ('a "x"\ny', False),
        ('a "{x', False),  # unsure before the end: not claimed open
        ("f '", True),
        ("f '\n", True),
        ("f 'x", True),
        ("f 'xy", False),  # Lean rejects it: not a literal cut short
        ("f /-", True),
    ],
)
def test_scan_reports_a_source_ending_inside_a_literal(source, open_at_end) -> None:
    assert scan(source).open_at_end is open_at_end


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        # Lean skips the character after "/-", so "/-/-" opens one comment.
        ("/-/- a -/\n-- c", {1}),
        ("/--/ a -/\n-- c", {1}),
        ("-- a\n  -- b\n--c\nx -- d\n---", {0, 1, 2, 4}),
        ("x := 2..3\n-- c", {1}),
        ("#check (h.1, 1.5, 1e3, 0b101, 0o17)\n-- c", {1}),
    ],
)
def test_scan_finds_exactly_the_comment_lines(text: str, lines: set[int]) -> None:
    found = scan(text)
    assert found.settled
    assert found.comment_lines == lines


@pytest.mark.parametrize("text", ["#check 1.x", "#check 1e", "#check 1e+", "x /-"])
def test_source_lean_rejects_unsettles_the_scan(text: str) -> None:
    assert not scan(text).settled


@settings(max_examples=500, deadline=None)
@given(
    st.text(alphabet=st.sampled_from(list("\"'r#{}\\-/!«» \nax01.e_∑")), max_size=30)
)
def test_scan_reads_any_text(text: str) -> None:
    found = scan(text)
    assert all(0 <= line <= text.count("\n") for line in found.comment_lines)


@pytest.mark.parametrize(
    "text",
    [
        # Lean reads blanks and comments after "/--" and "/-!" before the body,
        # so a "-/" in a line comment there does not close the doc comment.
        '/-- -- x -/\n"\n-/\ndef y := "\n-- hidden\n" -- "',
        ' /-! -- x -/\n"\n-/\ndef y := "\n-- hidden\n" -- "',
        "/----/\ndef x := 1\n-- c",
        "/-- /- a -/ b -/\n-- c",
        # After an identifier's "." or a name literal's backtick, "r" continues
        # a name; it does not start a raw string.
        'def y := tag X.r"\\"\n-- hidden\n\\""',
        'def y := tag2 `r"\\"\n-- hidden\n\\""',
        'def y := tag «a».r"\\"\n-- hidden\n\\""',
        # Verso doc comments are read by another parser.
        "set_option doc.verso true\n/-- a -/\ndef x := 1\n-- c",
    ],
)
def test_forms_lean_reads_differently_unsettle_the_scan(text: str) -> None:
    assert not scan(text).settled


def test_doc_comments_that_start_plainly_stay_settled() -> None:
    found = scan('/-- say "hi -/\ndef x := 1\n/-!  prose -/\n-- c')
    assert found.settled
    assert found.comment_lines == {3}


def test_code_tokens_split_symbols_and_keep_identifiers_whole() -> None:
    """Comments are not tokens; names, literals and «…» are one token each."""
    text = '/-- d -/ @[simp] x\' := Foo.bar «a b» "s" 12 -- c\n@f'
    tokens = code_tokens(text)
    assert tokens is not None
    assert [text[start:end] for start, end in tokens] == [
        "@",
        "[",
        "simp",
        "]",
        "x'",
        ":",
        "=",
        "Foo",
        ".",
        "bar",
        "«a b»",
        '"s"',
        "12",
        "@",
        "f",
    ]
    assert code_tokens('s!"{x}"') is None


@pytest.mark.parametrize(
    ("text", "result"),
    [
        ("", True),
        (" \n\t", True),
        ("-- a\n/- b /- c -/ -/\n", True),
        ("/-- doc -/\n", True),
        ("/-- one -/ /-- two -/", False),
        ("/-- -- doc -/", False),
        ("/-! module -/", False),
        ("@[simp] ", False),
        ("private ", False),
        ("/- open", False),
    ],
)
def test_only_comments_allows_one_docstring(text, result) -> None:
    """A claimed example's command may open with comments and one docstring."""
    assert only_comments(text) is result


@pytest.mark.parametrize(
    "text, line, ends",
    [
        ("theorem foo : a = b := h\n  |>.trans sorry\n", 1, False),
        ("theorem foo : a = b := h\ntheorem g : True := trivial\n", 1, True),
        ("theorem foo : a = b := h\n", 1, True),
        ("theorem foo : a = b := h", 1, True),
        ("def f : Nat := g\nwhere g := 1\n", 1, False),
        ("theorem t (n : Nat) : n = n := by\n  rfl\ntermination_by n\n", 2, False),
        ("theorem t (n : Nat) : n = n := by\n  rfl\ndecreasing_by simp\n", 2, False),
        ("theorem t : a := h\n|>.trans x\n", 1, False),
        ("theorem t : a := h\n<| x\n", 1, False),
        ("theorem t : P :=\nfoo\n", 1, False),
        ("theorem t : P := h\n-- note\n\n  <;> simp\n", 1, False),
        ("theorem t : P := h\n/- note -/\nwith x\n", 1, False),
        (
            "theorem t : P := h\n-- @view circuit t\n\n"
            "/-- doc -/\ntheorem u : P := h\n",
            1,
            True,
        ),
        ("theorem t : P := h\n/-! # Notes -/\n", 1, True),
        ("theorem t : P := h\n@[simp] theorem u : P := h\n", 1, True),
        ("theorem t : P := h\n#[1]\n", 1, False),
        ("theorem t : P := h\n#print axioms t\n", 1, True),
        ("theorem t : P := h\n#printaxioms t\n", 1, False),
        ("theorem t : ∀ n, P n\n| 0 => h\n| n+1 => h\n", 1, False),
        ("structure S where\n  x : Nat\nderiving Repr\n", 2, False),
        ("structure S where\n  x : Nat\nderiving instance Repr for S\n", 2, True),
        # Words that never occur inside a command start one at any column.
        ("theorem t : P := h\n  theorem u : P := h\n", 1, True),
        (
            "namespace N\n  def a : Nat := 1\n  theorem b : a = 1 := rfl\nend N\n",
            2,
            True,
        ),
        ("namespace N\n  theorem b : True := trivial\n  end N\n", 2, True),
        ("theorem t : P := h\n  private theorem u : P := h\n", 1, True),
        ("theorem t : P := h\n  Foo.end\n", 1, False),
        ("theorem t : P := h\n  «end»\n", 1, False),
        ("structure S where\n  x : Nat\nderiving\n  instance\n", 3, False),
        ("theorem t : P := h\nattribute [simp,\n  instance] t\n", 2, False),
        # Only at column 0: these have term, tactic or `where` forms.
        ("theorem t : P := by\n  simp\n  open Foo in exact h\n", 2, False),
        ("theorem t : P := by\n  simp\n  set_option x true in\n", 2, False),
        # A doc comment or attribute opens a command only before a command word.
        ("theorem foo : True := bar\nwhere\n  bar : True := trivial\n", 1, False),
        (
            (
                "theorem foo : True := bar\nwhere\nbar : True := baz\n@[simp]\n"
                "baz : True := sorry\n"
            ),
            3,
            False,
        ),
        (
            (
                "theorem foo : True := bar\nwhere\nbar : True := baz\n/-- d -/\n"
                "baz : True := sorry\n"
            ),
            3,
            False,
        ),
        (
            "theorem foo : True := trivial\n@[simp]\ntheorem bar : True := trivial\n",
            1,
            True,
        ),
        (
            (
                "theorem foo : True := trivial\n/-- d -/\n"
                "@[simp] private theorem bar : True := trivial\n"
            ),
            1,
            True,
        ),
        (
            (
                "theorem foo : True := trivial\n@[simp, foo [1]]\n"
                "theorem bar : True := trivial\n"
            ),
            1,
            True,
        ),
        # `open … in` and `set_option … in` apply to what follows `in`.
        ("theorem t : P := by\nsimp\nopen Foo in\nexact h\n", 2, False),
        ("theorem t : P := by\nsimp\nset_option x true in\nsimp\n", 2, False),
        (
            "theorem t : P := h\nset_option maxHeartbeats 400 in\ntheorem u : P := h\n",
            1,
            True,
        ),
        ("theorem t : P := h\nopen Foo in\n@[simp] theorem u : P := h\n", 1, True),
        ("theorem t : P := h\nopen Foo\ntheorem u : P := h\n", 1, True),
        ("theorem t : P := h\nset_option x true\n", 1, True),
        # A mutual block is one command.
        (
            (
                "mutual\ntheorem a : True := trivial\n"
                "theorem b : True := trivial\nend\n"
                "theorem c : True := trivial\n"
            ),
            2,
            False,
        ),
        (
            (
                "mutual\ntheorem a : True := trivial\n"
                "theorem b : True := trivial\nend\n"
                "theorem c : True := trivial\n"
            ),
            4,
            True,
        ),
        (
            "theorem z : True := trivial\nmutual\ntheorem a : True := trivial\nend\n",
            1,
            True,
        ),
        ("theorem t : P := h\nend.foo\n", 1, False),
        ("theorem t : P := h\nprivate theorem u : P := h\n", 1, True),
        ("theorem t : P := h\nnamespace N\n", 1, True),
        ("theorem t : P := h\n.trans x\n", 1, False),
        ("theorem t : P := h\n+ 1\n", 1, False),
        ("theorem t : P := h\nunknown_word\n", 1, False),
        # A cut inside a comment or literal is no boundary.
        ("/- c\ntheorem\n-/\ntheorem t : P := h\n", 1, False),
        ('def s := "a\ntheorem"\n', 1, False),
        # The lexer is sure of everything before an interpolated string only.
        ('theorem t : P := h\ndef s := s!"{x}"\n', 1, True),
        ('def s := s!"{x}"\ntheorem t : P := h\ntheorem u : P := h\n', 2, False),
        ("theorem t : P := h\n", 0, False),
        # `lemma` is an identifier without Batteries: indented, an argument.
        ("theorem foo : True → True := id\n  lemma\n", 1, False),
        ("theorem t : P := h\nlemma u : P := h\n", 1, True),
        # Indented `private`/`protected` may be a structure field.
        ("structure S where\n  x : Nat\n  private y : Nat\n", 2, False),
        ("structure S where\n  x : Nat\n  protected y : Nat\n", 2, False),
        ("theorem t : P := h\n  private noncomputable def u := 1\n", 1, True),
    ],
)
def test_a_command_ends_only_before_a_command_start(text, line, ends):
    """Only a column-0 command start, or the end, follows a command's end."""
    from glitnir.lean.lexing import command_boundaries

    assert command_boundaries(text).ends_at(line) is ends


def test_command_boundaries_find_the_command_around_a_line():
    from glitnir.lean.lexing import command_boundaries

    text = (
        "theorem a : True := trivial\n"
        "theorem b : True := by\n  trivial\n  <;> trivial\n"
        "theorem c : True := trivial\n"
    )
    found = command_boundaries(text)
    assert [found.ends_at(line) for line in range(1, 6)] == [
        True,
        False,
        False,
        True,
        True,
    ]
    assert found.last_end_before(3) == 1
    assert found.last_end_before(1) == 0
    assert found.end_from(2) == 4
