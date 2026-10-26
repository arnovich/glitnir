"""Split a notebook's Lean file into prose, Lean and claim cells.

A notebook is one Lean module. Prose lives in ``/-! … -/`` module docs, each
claim is a ``theorem`` or ``lemma`` proved by a tactic block, and everything
else is ordinary Lean: imports, namespaces, definitions and directive comments.
This module finds those cell boundaries line by line, asking the lexer only
whether a claim's column-0 line lies inside a literal of its proof.
It is not a grammar of Lean and understands nothing else inside a cell; Lean
alone decides what the cells mean. The boundaries it finds are proposals:
no status rests on them until Lean, reading the whole file, confirms them.
"""

import re
from dataclasses import dataclass, field
from enum import Enum

from glitnir.lean.lexing import LEAN_WHITESPACE, scan


class NotebookError(ValueError):
    """Raised when a notebook file cannot be split into cells."""


def split_lines(text: str) -> list[str]:
    r"""Split notebook text into lines the way Lean counts them.

    Only ``\n`` ends a line; ``str.splitlines`` also breaks at characters
    Lean keeps inside a line, such as U+2028 in a string literal. A final
    ``\n`` ends the last line rather than starting an empty one.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


class CellKind(str, Enum):
    """The three kinds of cell a notebook file contains."""

    PROSE = "prose"
    LEAN = "lean"
    CLAIM = "claim"


@dataclass(frozen=True)
class Claim:
    """One theorem of the notebook, split the way Lean checking takes it.

    Attributes:
        name: The declaration name, as written in the file, with its explicit
            universe list if it has one (``L.{u, v}``).
        keyword: ``theorem`` or ``lemma``.
        statement: Binders and type, as written between the name and ``:=``.
        proof: The tactic block, as `proof_text` reads it from the file.
        docstring: The ``/-- … -/`` comment before the header, if any.
        modifiers: Attributes and modifiers before the keyword, such as
            ``@[simp] private``.
        as_written: Whether the proof text renders the tactic block exactly as
            the file writes it. When it does not, the claim is not checked.
    """

    name: str
    keyword: str
    statement: str
    proof: str
    docstring: str | None = None
    modifiers: str = ""
    as_written: bool = True


@dataclass(frozen=True)
class Cell:
    """A contiguous block of the notebook file.

    Attributes:
        kind: Prose, Lean, or claim.
        start_line: First line of the block, 1-based.
        end_line: Last line of the block, inclusive.
        text: The block's source exactly as it appears in the file.
        claim: The parsed claim, for claim cells only.
    """

    kind: CellKind
    start_line: int
    end_line: int
    text: str
    claim: Claim | None = None


@dataclass(frozen=True)
class Notebook:
    """A notebook file as ordered cells.

    Attributes:
        cells: The cells in file order.
        text: The text it was parsed from, which Lean reads whole to confirm
            the cells.
    """

    cells: tuple[Cell, ...]
    text: str = field(default="", repr=False)

    @property
    def claims(self) -> tuple[Claim, ...]:
        """The claims in file order."""
        return tuple(cell.claim for cell in self.cells if cell.claim is not None)

    def claim(self, name: str) -> Claim:
        """Return the claim with this name.

        Raises:
            KeyError: If the notebook has no such claim.
        """
        for claim in self.claims:
            if claim.name == name:
                return claim
        raise KeyError(name)


# Lean's whitespace within a line, never Python's ``\s`` (see LEAN_WHITESPACE).
_SPACE = r"[ \t\r]"
_ATTRIBUTE = rf"@\[(?:[^\[\]]|\[[^\[\]]*\])*\]{_SPACE}*"
_MODIFIERS = rf"((?:{_ATTRIBUTE})*(?:(?:private|protected|nonrec){_SPACE}+)*)"
#: An explicit universe list: plain identifiers, commas and spaces, nothing
#: else. It is copied into every binding, so it must mean the same wherever it
#: is spliced; a comment or literal in it could open something the file does
#: not. Any other list stays part of the name, with no
#: universes, and is bound as before.
_UNIVERSE_LIST = r"\.\{[ \t]*[^\W\d]\w*(?:[ \t]*,[ \t]*[^\W\d]\w*)*[ \t]*\}"
#: A claim's name runs to the first whitespace, or through a universe list
#: ending it, which may hold spaces (``L.{u, v}``).
_CLAIM_NAME = rf"(?:[^ \t\r]*?{_UNIVERSE_LIST}(?={_SPACE}|$)|[^ \t\r]+)"
_CLAIM_HEADER = re.compile(
    rf"^{_MODIFIERS}(theorem|lemma){_SPACE}+({_CLAIM_NAME}){_SPACE}*(.*)$"
)
_INDENTED_HEADER = re.compile(rf"^{_SPACE}+{_MODIFIERS}(theorem|lemma){_SPACE}")


def parse_notebook(text: str) -> Notebook:
    """Split a notebook's Lean source into ordered cells.

    Args:
        text: The contents of the notebook's ``.lean`` file.

    Returns:
        The cells in file order.

    Raises:
        NotebookError: If a prose block, comment or docstring is not closed, a
            claim is indented or not proved with a tactic block, or two
            claims share a name.
    """
    if type(text) is not str:
        raise TypeError("notebook text must be a string")
    lines = split_lines(text)
    cells: list[Cell] = []
    pending: list[int] = []  # line indices of the current Lean cell

    def flush() -> None:
        while pending and _blank(lines[pending[-1]]):
            pending.pop()
        while pending and _blank(lines[pending[0]]):
            pending.pop(0)
        if pending:
            cells.append(
                Cell(
                    CellKind.LEAN,
                    pending[0] + 1,
                    pending[-1] + 1,
                    "\n".join(lines[pending[0] : pending[-1] + 1]),
                )
            )
        pending.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("/-!"):
            flush()
            end = _block_end(lines, index, "prose block")
            cells.append(
                Cell(
                    CellKind.PROSE,
                    index + 1,
                    end + 1,
                    "\n".join(lines[index : end + 1]),
                )
            )
            index = end + 1
            continue
        if line.startswith("/-") and not line.startswith(("/--", "/-!")):
            end = _block_end(lines, index, "comment")
            pending.extend(range(index, end + 1))
            index = end + 1
            continue
        if _INDENTED_HEADER.match(line):
            raise NotebookError(
                f"line {index + 1}: claims must start at column 0 to be checked"
            )
        if line.startswith("/--"):
            end = _block_end(lines, index, "docstring")
            header = end + 1
            if header < len(lines) and _CLAIM_HEADER.match(lines[header]):
                flush()
                docstring = "\n".join(lines[index : end + 1])
                cell, index = _claim_cell(lines, index, header, docstring)
                cells.append(cell)
                continue
            pending.extend(range(index, end + 1))
            index = end + 1
            continue
        if _CLAIM_HEADER.match(line):
            flush()
            cell, index = _claim_cell(lines, index, index, None)
            cells.append(cell)
            continue
        pending.append(index)
        index += 1
    flush()

    notebook = Notebook(tuple(cells), text=text)
    names = [claim.name for claim in notebook.claims]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise NotebookError(f"claims share a name: {', '.join(duplicates)}")
    return notebook


def _claim_cell(
    lines: list[str], start: int, header: int, docstring: str | None
) -> tuple[Cell, int]:
    """Read one claim starting at its docstring (or header) line."""
    match = _CLAIM_HEADER.match(lines[header])
    assert match is not None
    modifiers, keyword, name, first = match.groups()
    statement = [first]
    last = header
    while not lines[last].rstrip(LEAN_WHITESPACE).endswith(":= by"):
        last += 1
        if last >= len(lines) or _unindented(lines[last]):
            raise NotebookError(
                f"line {header + 1}: claim {name} must be proved with a tactic "
                "block ending the header in ':= by'"
            )
        statement.append(lines[last])
    statement_text = (
        "\n".join(statement)
        .rstrip(LEAN_WHITESPACE)
        .removesuffix(":= by")
        .rstrip(LEAN_WHITESPACE)
    )

    body_end = _body_end(lines, last)
    body = lines[last + 1 : body_end + 1]
    if all(_blank(line) for line in body):
        raise NotebookError(f"line {header + 1}: claim {name} has no proof")
    proof, as_written = proof_text(body)
    claim = Claim(
        name,
        keyword,
        statement_text.strip(LEAN_WHITESPACE),
        proof,
        docstring,
        modifiers.strip(LEAN_WHITESPACE),
        as_written,
    )
    cell = Cell(
        CellKind.CLAIM,
        start + 1,
        body_end + 1,
        "\n".join(lines[start : body_end + 1]),
        claim,
    )
    return cell, body_end + 1


def _body_end(lines: list[str], last: int) -> int:
    """Return the index of a claim's last proof line; ``last`` ends its header.

    The proof runs to its last non-blank line before a non-blank line at column
    0. Such a line inside a string literal of the proof is Lean's text, not a
    new block, so it stays in the proof when the lexer places it
    inside a string, raw string, character literal or ``«…»`` name, and the
    scans of the whole proof and of the file up to the proof's end are both
    settled and agree on it. The file's scan is how Lean reads the line: a
    statement or earlier cell that left a literal open would make the proof's
    own scan wrong. Otherwise the proof ends at the first column-0 line, as it
    always did: a proof cut short that way is not checked (see `proof_text`).

    The scanner may still be wrong: a notebook can declare a notation token
    holding ``"``, after which Lean reads the line as code. These boundaries
    are only proposals; Lean reading the whole file confirms or rejects them.
    """
    rest = scan("\n".join(lines[last + 1 :]))
    end, through = _proof_lines(lines, last, rest.literal_lines)
    if through:
        proof = scan("\n".join(lines[last + 1 : end + 1]))
        whole = scan("\n".join(lines[: end + 1]))
        in_file = frozenset(last + 1 + offset for offset in through)
        if (
            proof.settled
            and whole.settled
            and through <= proof.literal_lines
            and in_file <= whole.literal_lines
        ):
            return end
    return _proof_lines(lines, last, frozenset())[0]


def _proof_lines(
    lines: list[str], last: int, inside: frozenset[int]
) -> tuple[int, frozenset[int]]:
    """Find a proof's last line, and the column-0 lines kept before it.

    A column-0 line is kept when its offset from the proof's first line is in
    ``inside``, the lines a scan from there places inside a literal; the
    caller confirms them with scans of the proof and the file.
    """
    body_end, kept = last, set[int]()
    for probe in range(last + 1, len(lines)):
        if _unindented(lines[probe]):
            if probe - last - 1 not in inside:
                break
            kept.add(probe - last - 1)
        if not _blank(lines[probe]):
            body_end = probe
    return body_end, frozenset(kept)


def _block_end(lines: list[str], start: int, label: str) -> int:
    """Return the index of the line closing the ``/- … -/`` block at start."""
    for index in range(start, len(lines)):
        opener = 3 if index == start else 0
        if "-/" in lines[index][opener:]:
            return index
    raise NotebookError(f"line {start + 1}: {label} is not closed")


def _blank(line: str) -> bool:
    """Whether a line holds nothing but Lean whitespace."""
    return not line.strip(LEAN_WHITESPACE)


def _unindented(line: str) -> bool:
    """Whether a line starts a new top-level block: not blank, at column 0."""
    return not _blank(line) and line[0] not in LEAN_WHITESPACE


def _dedent(lines: list[str]) -> str:
    """Join lines without their common indentation, blank lines left empty."""
    indent = min(
        len(line) - len(line.lstrip(LEAN_WHITESPACE))
        for line in lines
        if not _blank(line)
    )
    return "\n".join("" if _blank(line) else line[indent:] for line in lines)


def proof_text(body: list[str]) -> tuple[str, bool]:
    """Read a claim's tactic block from its lines in the file.

    An ordinary block is dedented and its blank lines are emptied; `render_unit`
    puts it back at two spaces, a uniform shift Lean reads the same. A block in
    which a string literal spans lines, or which the scanner cannot read
    (`scan` unsettled), is read with no shift: each tactic line loses exactly
    the two spaces `render_unit` puts back, and literal and blank lines stay as
    written, so the checked text is the file's and the proof hash follows it.
    Shifting only the tactic lines would move them against the literal's.

    Args:
        body: The lines after the claim's ``:= by``, ending at its last
            non-blank line.

    Blank lines before the first tactic lie outside any literal; the unit
    leaves them out in both readings.

    Returns:
        The proof text, and whether it renders the block exactly as written.
        It does not when a tactic line of a block read as written does not
        start with two spaces, or when the block ends inside a literal or
        comment (a line of it at column 0 ended the claim); the text is then
        the ordinary one.
    """
    if scan("\n".join(body)).open_at_end:
        # A line of a literal at column 0 ended the claim's cell early, so
        # the body is not all of the proof the file writes.
        return _dedent(body), False
    heads = _tactic_lines(body)
    if heads is None:
        return _dedent(body), True
    if any(not body[index].startswith("  ") for index in heads):
        return _dedent(body), False
    return (
        "\n".join(
            line[2:] if index in heads else line for index, line in enumerate(body)
        ),
        True,
    )


def _tactic_lines(lines: list[str]) -> frozenset[int] | None:
    """Return the tactic lines of a block read as written, or None if ordinary.

    A tactic line is a non-blank line that does not start inside a literal;
    when the scan is unsettled, every non-blank line.
    """
    found = scan("\n".join(lines))
    if found.settled and not found.literal_lines:
        return None
    literal = found.literal_lines if found.settled else frozenset()
    return frozenset(
        index
        for index, line in enumerate(lines)
        if not _blank(line) and index not in literal
    )
