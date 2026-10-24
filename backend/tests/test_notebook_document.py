"""Tests for splitting a notebook's Lean source into cells and claims.

These exercise only the core parser: cell boundaries, claim headers, universe
lists and proofs that hold multi-line literals. Nothing here runs Lean.
"""

import pytest

from glitnir.lean.lexing import scan
from glitnir.notebook.document import (
    CellKind,
    NotebookError,
    parse_notebook,
    split_lines,
)


def test_claims_without_docstrings_and_lemma_keyword_are_found() -> None:
    notebook = parse_notebook(
        "import Mathlib\n\n/-- not a claim -/\ndef one : ℕ := 1\n\n"
        "lemma one_eq : one = 1 := by\n  rfl\n\n  \ntheorem t : True := by\n  trivial\n"
    )
    assert [c.name for c in notebook.claims] == ["one_eq", "t"]
    assert notebook.claim("one_eq").keyword == "lemma"
    assert notebook.claim("one_eq").docstring is None
    assert (
        notebook.cells[0].text
        == "import Mathlib\n\n/-- not a claim -/\ndef one : ℕ := 1"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("theorem t : True := trivial\n", "tactic block"),
        ("theorem t :\n  True\n", "tactic block"),
        ("/-! prose never closed\n", "not closed"),
        ("/-- doc never closed\n", "not closed"),
        ("theorem t : True := by\n\ndef x := 1\n", "has no proof"),
        (
            "theorem t : True := by\n  trivial\ntheorem t : True := by\n  trivial\n",
            "share a name",
        ),
    ],
)
def test_malformed_notebooks_are_rejected(text: str, message: str) -> None:
    with pytest.raises(NotebookError, match=message):
        parse_notebook(text)


def test_block_comments_never_become_claims() -> None:
    notebook = parse_notebook(
        "/-\ntheorem old : 1 = 2 := by\n  simp\n-/\n\n"
        "theorem t : True := by\n  trivial\n"
    )
    assert [c.name for c in notebook.claims] == ["t"]
    assert notebook.cells[0].kind is CellKind.LEAN


def test_statements_may_contain_default_arguments() -> None:
    notebook = parse_notebook("theorem d (n : ℕ := 0) :\n    n = n := by\n  rfl\n")
    assert notebook.claim("d").statement == "(n : ℕ := 0) :\n    n = n"


UNIVERSES = (
    "theorem L.{v, u} (α : Sort u) (β : Sort v) (a : α) : a = a := by\n  rfl\n\n"
    "theorem M : (3 : Nat) = 3 := by\n  exact L Nat Nat 3\n"
)


def test_a_claim_name_keeps_its_universe_list() -> None:
    notebook = parse_notebook(UNIVERSES)
    first, second = notebook.claims
    assert first.name == "L.{v, u}"
    assert first.universes == ".{v, u}"
    assert first.statement == "(α : Sort u) (β : Sort v) (a : α) : a = a"
    assert second.universes == ""


@pytest.mark.parametrize(
    "header",
    [
        "theorem x.{u}.y : True := by",  # not a universe list: the name as before
        "theorem x : @id.{1} Nat 0 = 0 := by",  # a universe list in the statement
    ],
)
def test_only_a_list_ending_the_name_is_its_universes(header) -> None:
    claim = parse_notebook(f"{header}\n  rfl\n").claims[0]
    assert claim.universes == ""
    assert claim.name == header.split()[1]


@pytest.mark.parametrize(
    "universes", [".{u /- } -/ }", ".{u -- }", '.{u, "v"}', ".{«u}»}", ".{}"]
)
def test_a_universe_list_is_copied_only_when_it_holds_bare_names(universes) -> None:
    """A list holding an open comment or literal is not copied into bindings."""
    claim = parse_notebook(
        f"theorem L{universes} (α : Sort u) (a : α) : a = a := by\n  rfl\n"
    ).claims[0]
    assert claim.universes == ""


def _notebook(body: str) -> str:
    return (
        f"import Mathlib\n\ntheorem c : True := by\n{body}\n\n"
        "def after := 1\n\ntheorem later : after = 1 := by\n  rfl\n"
    )


def _cell_of(notebook, name: str):
    """The cell holding the named claim."""
    return next(
        cell
        for cell in notebook.cells
        if cell.claim is not None and cell.claim.name == name
    )


def _line_based_end(text: str, cell_start: int) -> int:
    """The 1-based last line of a claim cut at its first column-0 line."""
    return next(
        index
        for index, line in enumerate(split_lines(text))
        if index > cell_start and line and line[0] not in " \t\r"
    )


# The scan is unsure before it reaches the column-0 line (a string holding "{"
# may be interpolated), so nothing says the line is inside a literal. The claim
# ends there, as before, and is checked as the text before it: a text that
# ends inside a literal, which Lean rejects (see the live test).
_CUT_UNSURE = {
    "interpolated": '  have h : s!"{1}\nb" = "1\nb" := by decide\n  trivial',
    "brace": '  have h : "a {\nb".length = 5 := by decide\n  trivial',
}


@pytest.mark.parametrize("body", _CUT_UNSURE.values(), ids=_CUT_UNSURE)
def test_where_the_scan_is_unsure_first_the_claim_ends_as_before(body: str) -> None:
    text = _notebook(body)
    notebook = parse_notebook(text)
    cell = _cell_of(notebook, "c")
    assert cell.end_line == _line_based_end(text, cell.start_line)
    assert notebook.claim("c").proof == body.split("\n")[0][2:]


_OPEN_BEFORE = {
    # The statement's string runs into the proof: Lean reads the column-0
    # line as code, so it must not vanish from later claims' Lean.
    "statement": (
        'import Mathlib\n\ntheorem c : "x := by\n  have h : "\n'
        'def hidden := 1\n  "\n\ntheorem later : True := by\n  trivial\n'
    ),
    # An earlier cell may be interpolated: the scanner cannot say.
    "earlier-cell": (
        'import Mathlib\n\ndef s := s!"{1}"\n\ntheorem c : True := by\n'
        '  have h : "a\nb".length = 3 := by decide\n  trivial\n'
    ),
}


@pytest.mark.parametrize("text", _OPEN_BEFORE.values(), ids=_OPEN_BEFORE)
def test_the_file_before_the_proof_must_agree(text: str) -> None:
    notebook = parse_notebook(text)
    cell = _cell_of(notebook, "c")
    assert cell.end_line == _line_based_end(text, cell.start_line)


def test_many_column_zero_lines_parse_in_linear_time() -> None:
    import time

    lines = "\n".join(f"x{i}" for i in range(8000))
    body = f'  have h : "a\n{lines}".length > 0 := by decide\n  trivial'
    started = time.monotonic()
    assert parse_notebook(_notebook(body)).claim("c").as_written
    assert time.monotonic() - started < 5


_BLANKS = " \t\r"


def _proof_notebook(body: str, name: str = "c") -> str:
    return (
        f"import Mathlib\n\ntheorem {name} : True := by\n{body}\n"
        "theorem later : True := by\n  trivial\n"
    )


def _old_proof(body: str) -> str:
    """The proof text before this task: dedented, blank lines emptied."""
    lines = split_lines(body)
    indent = min(
        len(line) - len(line.lstrip(_BLANKS)) for line in lines if line.strip(_BLANKS)
    )
    return "\n".join("" if not line.strip(_BLANKS) else line[indent:] for line in lines)


# Each proof holds a string literal that spans lines. The checked body must be
# the file's body byte for byte.
_LITERAL_PROOFS = {
    "blank-line": '  have h : "a\n\n  b".length = 5 := by decide\n  trivial',
    "blank-line-with-spaces": (
        '  have h : "a\n     \n b".length = 8 := by decide\n  trivial'
    ),
    "deeper-indentation": (
        '  have h : "a\n        b".length = 11 := by decide\n  trivial'
    ),
    "shallower-than-the-tactics": (
        '    have h : "a\n b".length = 4 := by decide\n    trivial'
    ),
    "block-at-four": '    have h : "a\n\n  b".length = 5 := by decide\n    trivial',
    "nested-tactics": (
        "  constructor\n"
        '  · have h : "x\n\n      y" = "x\n\n      y" := rfl\n'
        "    trivial\n"
        "  · trivial"
    ),
    "raw-string": '  have h : r#"a\n\n  "b"#.length = 7 := by decide\n  trivial',
    "string-gap": '  have h : "a\\\n      b" = "ab" := by decide\n  trivial',
    "two-literals": (
        '  have h : "a\n\n b" = "a\n\n b" := rfl\n'
        '  have k : "c\n   d".length = 6 := by decide\n'
        "  trivial"
    ),
    "leading-blank": '\n   \n  have h : "a\n\n  b".length = 5 := by decide\n  trivial',
    "unsettled-interpolation": (
        '  have h : s!"{1}\n\n    x" = "1\n\n    x" := by decide\n  trivial'
    ),
}


def test_the_literal_lines_stay_with_their_tactic() -> None:
    text = _proof_notebook(_LITERAL_PROOFS["blank-line"])
    claim = parse_notebook(text).claim("c")
    assert claim.proof == 'have h : "a\n\n  b".length = 5 := by decide\ntrivial'


def test_the_later_claims_are_unaffected() -> None:
    text = _proof_notebook(_LITERAL_PROOFS["blank-line"])
    assert parse_notebook(text).claim("later").proof == "trivial"


# Ordinary proofs: no literal spans a line and the scan is settled. They keep
# their proof text, script and hash, however they are indented and spaced.
_ORDINARY_PROOFS = {
    "two-spaces": "  unfold f\n  simp",
    "four-spaces": "    unfold f\n    simp",
    "nested": "  constructor\n  · exact a\n  · exact b",
    "blank-lines": "  unfold f\n\n  simp\n   \n  rfl",
    "leading-blank": "\n  simp",
    "one-line-strings": '  have h : "a" = "a" := rfl\n  simp [h]',
    "block-comment": "  /- a\n\n     b -/\n  simp",
    "one-space": " simp\n exact h",
}


@pytest.mark.parametrize("body", _ORDINARY_PROOFS.values(), ids=_ORDINARY_PROOFS)
def test_an_ordinary_proof_is_read_as_before(body: str) -> None:
    claim = parse_notebook(_proof_notebook(body)).claim("c")
    assert claim.as_written
    assert claim.proof == _old_proof(body)


def test_an_unsettled_proof_at_one_space_is_not_checked() -> None:
    notebook = parse_notebook(
        _proof_notebook(' have h : s!"{1}" = "1" := by decide\n trivial')
    )
    assert not scan(notebook.claim("c").proof).settled
    assert not notebook.claim("c").as_written


def test_split_lines_breaks_at_newlines_only() -> None:
    assert split_lines("") == []
    assert split_lines("a") == ["a"]
    assert split_lines("a\n") == ["a"]
    assert split_lines("a\n\nb c\r\n") == ["a", "", "b c\r"]
