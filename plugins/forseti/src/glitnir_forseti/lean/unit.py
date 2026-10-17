"""Render a bounded candidate declaration for independent Lean replay."""

from glitnir.lean.lexing import LEAN_WHITESPACE, scan

MAX_PRELUDE_BYTES = 65_536
MAX_STATEMENT_BYTES = 65_536
THEOREM_NAME = "forsetiSearch"
#: What starts an explicit universe list, which Lean reads only right after the
#: name it belongs to (``L.{u}``), never after whitespace.
UNIVERSE_LIST = ".{"


def declaration_head(name: str, statement: str) -> str:
    """Return ``name statement``, with an explicit universe list glued to the name.

    A statement may begin with the declaration's universe list, ``.{u, v}``;
    Lean reads it only directly after the name, so no space goes between them.

    Args:
        name: The declared name.
        statement: Binders and type, as written after the name, optionally
            preceded by a universe list.
    """
    statement = statement.strip(LEAN_WHITESPACE)
    if statement.startswith(UNIVERSE_LIST):
        return f"{name}{statement}"
    return f"{name} {statement}"


def render_unit(prelude: str, statement: str, script: tuple[str, ...]) -> str:
    """Render the self-contained unit for one script.

    Args:
        prelude: Imports and opens, ending before the declaration.
        statement: The theorem's binders and type, as written after its name.
        script: The tactic lines, one per entry, each rendered after two
            spaces. An entry may continue onto further lines, rendered
            exactly as given, when each is blank, starts with Lean
            whitespace, or starts inside a literal the entry opened (task
            167): a notebook proof's string literal that spans lines, and its
            blank lines, are checked as the file writes them. Nothing the
            script supplies reaches column 0 where Lean reads code, as far as
            the lexer can tell, which keeps the final ``#print axioms`` line
            the unit's own; it does not keep the script
            inside the ``by`` block (Lean's commands are not
            column-sensitive, so an indented line may end it). Soundness
            rests on the independent check, which reads the theorem's
            statement and axioms from the compiled module.

    Returns:
        Lean source declaring the theorem with that script and printing its
        axioms, so the session reports them.
    """
    for label, value, maximum in (
        ("prelude", prelude, MAX_PRELUDE_BYTES),
        ("statement", statement, MAX_STATEMENT_BYTES),
    ):
        if type(value) is not str:
            raise TypeError(f"{label} must be a string")
        if len(value.encode("utf-8")) > maximum:
            raise ValueError(f"{label} exceeds its byte limit")
    if not statement.strip(LEAN_WHITESPACE):
        raise ValueError("statement must not be empty")
    if type(script) is not tuple or any(type(line) is not str for line in script):
        raise TypeError("script must be a tuple of strings")
    if not all(_one_tactic(entry) for entry in script):
        raise ValueError(
            "each script entry must be one non-empty tactic line, continued "
            "only onto blank or indented lines"
        )
    body = "\n".join(f"  {line}" for line in script)
    head = (
        f"{prelude.rstrip(LEAN_WHITESPACE)}\n"
        f"theorem {declaration_head(THEOREM_NAME, statement)} := by\n"
    )
    if not _column_zero_in_literals(head, body):
        raise ValueError(
            "a script line at column 0 must lie inside a string literal the "
            "lexer can place"
        )
    return f"{head}{body}\n\n#print axioms {THEOREM_NAME}\n"


def _column_zero_in_literals(head: str, body: str) -> bool:
    """Whether every body line at column 0 is inside a literal of the unit.

    The whole unit is scanned, since the prelude and statement decide how Lean
    reads the script: it must be settled, so it also ends outside any literal.
    The scanner assumes no notation token holds ``"`` or ``«``; a prelude that
    declares one can defeat this, as an indented line could always end the
    ``by`` block, which is why soundness rests on the independent check.
    """
    first = head.count("\n")
    column_zero = {
        first + offset
        for offset, line in enumerate(body.split("\n"))
        if line and line[0] not in LEAN_WHITESPACE
    }
    if not column_zero:
        return True
    found = scan(head + body)
    return found.settled and column_zero <= found.literal_lines


def _one_tactic(entry: str) -> bool:
    """Whether an entry is a tactic line and lines that cannot reach column 0.

    A later line may start at column 0 only inside a string, raw string,
    character literal or ``«…»`` name, as a settled `scan` of the entry places
    it; there it is the literal's text, not Lean code.
    """
    first, *rest = entry.split("\n")
    if not first.strip(LEAN_WHITESPACE):
        return False
    column_zero = {
        offset
        for offset, line in enumerate(rest, 1)
        if line and line[0] not in LEAN_WHITESPACE
    }
    if not column_zero:
        return True
    found = scan(entry)
    return found.settled and column_zero <= found.literal_lines
