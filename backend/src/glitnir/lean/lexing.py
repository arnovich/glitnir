"""Find the comment lines of Lean source, the way Lean's tokenizer reads them.

A claim's context hash leaves out comment lines: lines whose first non-blank
characters are ``--``. That is only right where Lean is reading code. Inside a
string, raw string, character literal, ``«…»`` name, block comment or doc
comment the same line is Lean's text, and leaving it out would let an edit that
changes a definition keep a claim checked.

It also reports the lines that start inside a string literal, raw string,
character literal or ``«…»`` name, whose leading blanks are Lean's text, so a
claim's proof holding one is checked as written rather than re-indented
and such a line at column 0 neither ends the claim's cell nor is
refused by ``render_unit``.

`scan` walks the source once, mirroring ``Lean.Parser.Basic`` (``whitespace``,
``finishCommentBlock``, ``strLitFnAux``, ``charLitFnAux``, ``rawStrLitFnAux``,
``numberFnAux``, ``identFnAux``) only as far as telling code from literal and
comment text. It is not a grammar of Lean and decides nothing else.

It fails closed. A check that holds means Lean elaborated the source without
error, so the scanner only has to agree with Lean on error-free source. Where it
cannot be sure that it does, the scan is *unsettled* and the caller leaves every
line in. That covers a string holding ``{`` (it may be interpolated, as with
``s!``, and only the parser knows), an escape Lean rejects, a ``'`` after a
symbol that may belong to a notation token such as ``∑'``, a ``--`` or ``/-``
glued to the token before it, a comment right after a doc comment's opener, an
``r"`` after ``.`` or a backtick (where ``r`` may continue a name), Verso doc
comments, and source that ends inside a literal or comment.
It assumes that no notation token contains ``"`` or ``«``; Mathlib has none.
A notebook can declare one, so a notebook's claims whose Lean would differ if
a literal's column-0 line were code are left unchecked.
"""

import bisect
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache


@dataclass(frozen=True)
class Scan:
    """What `scan` found in one piece of Lean source.

    Attributes:
        comment_lines: Zero-based indices of the lines that are comment lines.
        settled: Whether the scanner is sure it read the source as Lean does
            and the source ends outside any literal or comment. When it is not,
            no line may be left out, here or in any source Lean reads after it.
        literal_lines: Zero-based indices of the lines that start inside a
            string, raw string, character literal or ``«…»`` name, so that
            their leading blanks are Lean's text. Complete only when settled.
        open_at_end: Whether the source ends inside a string, raw string,
            ``«…»`` name or comment, as far as the scan read before any other
            doubt. A claim's proof that does was cut short by a column-0 line
            the notebook could not place inside a literal.
    """

    comment_lines: frozenset[int]
    settled: bool
    literal_lines: frozenset[int] = frozenset()
    open_at_end: bool = False


class _Unsettled(Exception):
    """The scanner cannot be sure it reads the source as Lean does."""


class _OpenAtEnd(_Unsettled):
    """The source ends inside a literal or comment."""


class _Before(Enum):
    """What precedes the scanner's position, which decides how it reads on."""

    BLANK = "blank"  # start, whitespace or a comment: a token starts here
    TOKEN = "token"  # a literal, number or «name»: a token starts here
    IDENT = "ident"  # an identifier's characters: ' and digits continue it
    SYMBOL = "symbol"  # a symbol that may continue a notation token


#: What Lean reads as whitespace between tokens. Python's ``str.strip`` and
#: regex ``\s`` also take U+2028, U+2029, U+0085, ``\v``, ``\f`` and
#: ``\x1c``-``\x1f``, which Lean rejects outside a literal and keeps inside
#: one; stripping them would hide text Lean reads.
LEAN_WHITESPACE = " \t\r\n"
_BLANK = LEAN_WHITESPACE
_SIMPLE_ESCAPES = "\\\"'rnt"
_HEX = "0123456789abcdefABCDEF"


def scan(text: str) -> Scan:
    """Find the comment lines of a piece of Lean source.

    Args:
        text: Lean source, with lines separated by newlines.

    Returns:
        The comment lines, and whether the scan is settled.
    """
    return _scan(text)


@lru_cache(maxsize=4096)
def _scan(text: str) -> Scan:
    comments: set[int] = set()
    literals: set[int] = set()
    try:
        if "doc.verso" in text:
            raise _Unsettled  # Verso doc comments are read by another parser
        _walk(text, comments, literals)
    except _OpenAtEnd:
        return Scan(frozenset(comments), False, frozenset(literals), True)
    except _Unsettled:
        return Scan(frozenset(comments), False, frozenset(literals))
    return Scan(frozenset(comments), True, frozenset(literals))


def code_tokens(text: str) -> list[tuple[int, int]] | None:
    """Return the character spans of the source's code tokens, in order.

    Whitespace and comments (doc comments included) are not tokens. An
    identifier is one token up to a dot (``Foo.example`` is three), a literal
    or ``«…»`` name is one token, and every other symbol character is its own
    token. None when the scan is not settled.
    """
    tokens: list[tuple[int, int]] = []
    try:
        if "doc.verso" in text:
            raise _Unsettled
        _walk(text, set(), set(), tokens)
    except _Unsettled:
        return None
    return tokens


def only_comments(text: str, docstrings: int = 1) -> bool:
    """Whether ``text`` is nothing but whitespace and comments.

    At most ``docstrings`` of the comments may be doc comments (``/-- … -/``);
    module docs (``/-! … -/``) are commands and never count as comments.
    """
    index = 0
    try:
        while index < len(text):
            if text[index] in _BLANK:
                index += 1
            elif text.startswith("/--", index):
                docstrings -= 1
                if docstrings < 0 or text.startswith(
                    ("--", "/-"), _skip_blanks(text, index + 3)
                ):
                    return False
                index = _comment_end(text, index + 3)
            elif text.startswith("/-!", index):
                return False
            elif text.startswith("/-", index):
                index = _comment_end(text, index + 3)
            elif text.startswith("--", index):
                end = text.find("\n", index)
                index = len(text) if end < 0 else end
            else:
                return False
    except _Unsettled:
        return False
    return True


def _walk(
    text: str,
    comments: set[int],
    literals: set[int],
    tokens: list[tuple[int, int]] | None = None,
    elements: list[tuple[int, int, str]] | None = None,
    progress: list[int] | None = None,
) -> None:
    """Record the comment and literal lines of the source, or raise `_Unsettled`.

    With ``tokens``, also record the span of each code token. With
    ``elements``, record every code token, comment and doc comment as
    ``(start, end, kind)``, kind ``"code"``, ``"comment"`` or ``"doc"``; with
    ``progress``, keep in it where the element being read starts, so that a
    caller knows how far the source was read as Lean reads it.
    """
    index, before = 0, _Before.BLANK
    while index < len(text):
        if progress is not None:
            progress[0] = index
        char = text[index]
        start = token = index
        code, previous = True, before
        if char in _BLANK:
            index, before, code = index + 1, _Before.BLANK, False
        elif text.startswith(("--", "/-"), index):
            code = False
            if before is not _Before.BLANK:
                raise _Unsettled  # possibly inside a notation token
            if char == "-":
                start = text.rfind("\n", 0, index) + 1
                if not text[start:index].strip(" \t"):
                    comments.add(text.count("\n", 0, index))
                end = text.find("\n", index)
                index = len(text) if end < 0 else end
            else:
                if text[index + 2 : index + 3] in ("-", "!"):
                    # "/--" and "/-!" are tokens, and Lean reads blanks and
                    # comments after them before the body starts.
                    body = _skip_blanks(text, index + 3)
                    if text.startswith(("--", "/-"), body):
                        raise _Unsettled
                # Lean skips the character after "/-" before counting nesting,
                # whether the comment is a block comment or a doc comment.
                index = _comment_end(text, index + 3)
        elif char == '"':
            index, before = _string_end(text, index + 1), _Before.TOKEN
        elif char == "'":
            index, before = _apostrophe(text, index, before)
        elif char == "r" and before is not _Before.IDENT and _raw_start(text, index):
            if text[index - 1 : index] in (".", "`"):
                raise _Unsettled  # "r" may continue a name, as in X.r or `r
            index, before = _raw_end(text, index + 1), _Before.TOKEN
        elif char == "«":
            end = text.find("»", index + 1)
            if end < 0:
                raise _OpenAtEnd
            index, before = end + 1, _Before.TOKEN
        elif before is _Before.IDENT and _is_id_rest(char):
            index += 1
        elif "0" <= char <= "9":
            index, before = _number_end(text, index), _Before.TOKEN
        elif _is_id_first(char):
            index, before = index + 1, _Before.IDENT
            while index < len(text) and _is_id_rest(text[index]):
                index += 1
        else:
            index, before = index + 1, _Before.SYMBOL
        if code and tokens is not None:
            continued = previous is _Before.IDENT and before is _Before.IDENT
            if continued and tokens and tokens[-1][1] == token:
                tokens[-1] = (tokens[-1][0], index)  # a continued identifier
            else:
                tokens.append((token, index))
        if elements is not None and char not in _BLANK:
            if code:
                continued = previous is _Before.IDENT and before is _Before.IDENT
                if continued and elements and elements[-1][1] == token:
                    elements[-1] = (elements[-1][0], index, "code")
                else:
                    elements.append((token, index, "code"))
            else:
                doc = char == "/" and text[token + 2 : token + 3] in ("-", "!")
                elements.append((token, index, "doc" if doc else "comment"))
        if before is _Before.TOKEN and "\n" in text[start:index]:
            # A literal (numbers never span lines): the lines after each of
            # its line ends start inside it.
            first = text.count("\n", 0, start) + 1
            literals.update(range(first, first + text.count("\n", start, index)))


#: Words that never occur inside a command: where one is a keyword token, at
#: any column, a command starts (indented commands inside a namespace are
#: common style).
ANYWHERE_STARTS = frozenset(
    {
        "theorem",
        "def",
        "example",
        "structure",
        "inductive",
        "class",
        "instance",
        "abbrev",
        "axiom",
        "opaque",
        "namespace",
        "section",
        "end",
        "variable",
        "include",
        "universe",
        "mutual",
        "import",
        "noncomputable",
    }
)
#: Modifiers that also mark structure fields (`  private y : Nat`): elsewhere
#: than column 0 they start a command only before a word of ANYWHERE_STARTS.
FIELD_MODIFIERS = frozenset({"private", "protected"})
#: Words that start a command only at column 0: elsewhere they may be a tactic,
#: a term or part of another command (`open … in`, `set_option … in`).
#: `lemma` is a keyword only where Batteries or Mathlib define it; in a core
#: project it is an identifier, so an indented one may be an argument.
COLUMN0_STARTS = frozenset(
    {
        "lemma",
        "private",
        "protected",
        "open",
        "export",
        "set_option",
        "omit",
        "attribute",
        "public",
        "meta",
        "partial",
        "unsafe",
        "nonrec",
    }
)
#: Every word that starts a notebook command; a line starting with anything
#: else may continue the command before it.
COMMAND_STARTS = ANYWHERE_STARTS | COLUMN0_STARTS
_PRINT = re.compile(r"#print(?=[ \t\r\n])")
_DERIVING_INSTANCE = re.compile(r"deriving[ \t\r\n]+instance(?![^ \t\r\n(])")
#: Tokens after which an ANYWHERE_STARTS word is not a command
#: (`deriving instance`, `@[instance]`, `attribute [instance, simp]`).
_NOT_AFTER = frozenset({"deriving", "[", ",", "@", "."})


class CommandBoundaries:
    """Where a command of Lean source may end, as far as the lexer can tell.

    A line ends a command when the cut after it lies outside every literal,
    comment and `mutual` … `end` block, and the source after it, past blank
    lines and comments, ends or starts a command:

    - a word of `ANYWHERE_STARTS` as a keyword token, at any column, also
      after `private`/`protected` (alone, those may begin a structure field);
    - at column 0, a word of `COLUMN0_STARTS` (`open` and `set_option` only
      when not the `… in` combinator of a tactic or term), ``#print``,
      ``deriving instance`` or a module doc;
    - at column 0, a doc comment or ``@[…]`` followed by a command word, so
      not one opening a `where` declaration.

    Anything else (``where``, ``termination_by``, ``|``, ``|>.``, an
    operator, an unknown word, or a source the lexer cannot read that far)
    is no boundary: a caller then withholds more, never less.
    """

    def __init__(self, text: str) -> None:
        """Read ``text`` once; nothing past where the lexer is unsure counts."""
        self._text = text
        elements: list[tuple[int, int, str]] = []
        progress = [0]
        try:
            if "doc.verso" in text:
                raise _Unsettled
            _walk(text, set(), set(), None, elements, progress)
            self._settled = len(text)
        except _Unsettled:
            # Elements recorded before the one that raised were read fully.
            self._settled = progress[0]
        self._elements = [e for e in elements if e[1] <= self._settled]
        self._starts = [e[0] for e in self._elements]
        self._lines = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        self._mutual = self._mutual_blocks()

    def _word(self, index: int) -> str | None:
        """Return the code token at ``index`` as a whole word, or None."""
        start, end, kind = self._elements[index]
        if kind != "code":
            return None
        if self._text[end : end + 1] == "." or self._text[start - 1 : start] == ".":
            return None  # part of a dotted name
        return self._text[start:end]

    def _column0(self, index: int) -> bool:
        start = self._elements[index][0]
        return start == 0 or self._text[start - 1] == "\n"

    def _next(self, index: int) -> int:
        """Return the first element after ``index`` that is not a plain comment."""
        index += 1
        while index < len(self._elements) and self._elements[index][2] == "comment":
            index += 1
        return index

    def _previous_code(self, index: int) -> str | None:
        while index > 0:
            index -= 1
            start, end, kind = self._elements[index]
            if kind == "code":
                return self._text[start:end]
        return None

    def _mutual_blocks(self) -> list[tuple[int, int]]:
        """Spans from a column-0 `mutual` to its column-0 `end`: one command."""
        blocks = []
        index = 0
        while index < len(self._elements):
            if self._word(index) == "mutual" and self._column0(index):
                close = index + 1
                while close < len(self._elements) and not (
                    self._word(close) == "end" and self._column0(close)
                ):
                    close += 1
                end = (
                    self._elements[close][0]
                    if close < len(self._elements)
                    else len(self._text) + 1
                )
                blocks.append((self._elements[index][0], end))
                index = close
            index += 1
        return blocks

    def _after_attribute(self, index: int) -> int:
        """Return the element after the ``@[…]`` whose ``@`` is at ``index``."""
        depth = 0
        index += 1
        while index < len(self._elements):
            start, end, kind = self._elements[index]
            if kind == "code":
                token = self._text[start:end]
                depth += token == "["
                depth -= token == "]"
                if depth == 0:
                    return self._next(index)
            index += 1
        return index

    def _declaration_follows(self, index: int, depth: int = 0) -> bool:
        """Whether a command word follows a doc comment or attribute."""
        if index >= len(self._elements) or depth > 64:
            return False
        start, _, kind = self._elements[index]
        if kind == "doc":
            return not self._text.startswith(
                "/-!", start
            ) and self._declaration_follows(self._next(index), depth + 1)
        if self._text.startswith("@[", start):
            return self._declaration_follows(self._after_attribute(index), depth + 1)
        return self._word(index) in COMMAND_STARTS

    def _not_combinator(self, index: int) -> bool:
        """Whether `open`/`set_option` at ``index`` is a command of its own.

        `open X in t` and `set_option o v in t` apply to what follows `in`:
        a command when that is a command, else a tactic or term continuing
        the command before.
        """
        index += 1
        while index < len(self._elements):
            word = self._word(index)
            if word == "in":
                return self._declaration_follows(self._next(index))
            if word in ANYWHERE_STARTS or (
                self._column0(index) and self._elements[index][2] != "comment"
            ):
                return True
            index += 1
        return self._settled == len(self._text)

    def _command_at(self, index: int) -> bool:
        """Whether the element at ``index``, first after a cut, starts a command."""
        if index == len(self._elements):
            return self._settled == len(self._text)
        start, _, kind = self._elements[index]
        column0 = self._column0(index)
        text = self._text
        if kind == "doc":
            if text.startswith("/-!", start):
                return column0
            return column0 and self._declaration_follows(self._next(index))
        if text.startswith("@[", start):
            return column0 and self._declaration_follows(self._after_attribute(index))
        word = self._word(index)
        if word in ANYWHERE_STARTS:
            return self._previous_code(index) not in _NOT_AFTER
        if word in FIELD_MODIFIERS and not column0:
            following = self._next(index)
            while following < len(self._elements) and (
                self._word(following) in FIELD_MODIFIERS
            ):
                following = self._next(following)
            return (
                following < len(self._elements)
                and self._word(following) in ANYWHERE_STARTS
                and self._previous_code(index) not in _NOT_AFTER
            )
        if not column0:
            return False
        if _PRINT.match(text, start):
            return True
        if word == "deriving":
            return _DERIVING_INSTANCE.match(text, start) is not None
        if word in ("open", "set_option"):
            return self._not_combinator(index)
        return word in COLUMN0_STARTS

    def _cut(self, line: int) -> int:
        """Return the offset just after one-based ``line`` and its line end."""
        if line >= len(self._lines):
            return len(self._text)
        return self._lines[line]

    def ends_at(self, line: int) -> bool:
        """Whether a command of the source can end with one-based ``line``."""
        if line < 1:
            return False
        cut = self._cut(line)
        if cut > self._settled:
            return False
        if any(start < cut <= end for start, end in self._mutual):
            return False
        index = bisect.bisect_left(self._starts, cut)
        if index > 0 and self._elements[index - 1][1] > cut:
            return False  # the cut lies inside a literal or comment
        while index < len(self._elements) and self._elements[index][2] == "comment":
            index += 1
        return self._command_at(index)

    def last_end_before(self, line: int) -> int:
        """Return the last line before ``line`` that ends a command, or 0."""
        for candidate in range(min(line, len(self._lines)) - 1, 0, -1):
            if self.ends_at(candidate):
                return candidate
        return 0

    def end_from(self, line: int) -> int | None:
        """Return the first line from ``line`` on that ends a command, if any."""
        for candidate in range(max(line, 1), len(self._lines) + 1):
            if self.ends_at(candidate):
                return candidate
        return None


@lru_cache(maxsize=4)
def command_boundaries(text: str) -> CommandBoundaries:
    """Return the command boundaries of ``text``, read once per text.

    Few are kept: a text may be 16 MiB.
    """
    return CommandBoundaries(text)


def _comment_end(text: str, index: int) -> int:
    """Return the end of a ``/- … -/`` comment whose body starts at index."""
    if index > len(text):
        raise _OpenAtEnd  # the source ends right after "/-"
    depth = 1
    while index < len(text):
        if text.startswith("-/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        elif text.startswith("/-", index):
            depth += 1
            index += 2
        else:
            index += 1
    raise _OpenAtEnd


def _string_end(text: str, index: int) -> int:
    """Return the end of a string literal whose body starts at index."""
    while index < len(text):
        char = text[index]
        if char == '"':
            return index + 1
        if char == "{":
            raise _Unsettled  # an interpolated string reads code here
        if char == "\\":
            index = _escape_end(text, index + 1, in_string=True)
        else:
            index += 1
    raise _OpenAtEnd


def _escape_end(text: str, index: int, in_string: bool) -> int:
    """Return the end of an escape; index is just after its backslash."""
    code = text[index : index + 1]
    if code and code in _SIMPLE_ESCAPES:
        return index + 1
    if code in ("x", "u"):
        digits = 2 if code == "x" else 4
        found = text[index + 1 : index + 1 + digits]
        if len(found) == digits and all(char in _HEX for char in found):
            return index + 1 + digits
    if in_string and code == "\n":
        return index + 1  # a string gap: the whitespace after it is harmless
    raise _Unsettled


def _apostrophe(text: str, index: int, before: _Before) -> tuple[int, _Before]:
    """Read a ``'``: part of a name, the ``''`` token, or a character literal."""
    if before is _Before.IDENT:
        return index + 1, _Before.IDENT
    if text.startswith("''", index):
        return index + 2, _Before.SYMBOL
    end = _char_end(text, index + 1)
    if before is _Before.SYMBOL:
        if end is not None:
            raise _Unsettled  # a character literal, or a token such as ∑'
        return index + 1, _Before.SYMBOL
    if end is None:
        if index + 2 >= len(text) and text[index + 1 : index + 2] != "\\":
            raise _OpenAtEnd  # the source ends inside the character literal
        raise _Unsettled  # Lean reads a character literal here and rejects it
    return end, _Before.TOKEN


def _char_end(text: str, index: int) -> int | None:
    """Return the end of a character literal whose body starts at index."""
    if index >= len(text):
        return None
    if text[index] == "\\":
        try:
            index = _escape_end(text, index + 1, in_string=False)
        except _Unsettled:
            return None
    else:
        index += 1
    return index + 1 if text[index : index + 1] == "'" else None


def _raw_start(text: str, index: int) -> bool:
    """Whether a raw string literal ``r#…#"`` starts at index."""
    index += 1
    while text[index : index + 1] == "#":
        index += 1
    return text[index : index + 1] == '"'


def _raw_end(text: str, index: int) -> int:
    """Return the end of a raw string literal whose ``#``s start at index."""
    hashes = 0
    while text[index + hashes] == "#":
        hashes += 1
    closing = '"' + "#" * hashes
    end = text.find(closing, index + hashes + 1)
    if end < 0:
        raise _OpenAtEnd
    return end + len(closing)


def _number_end(text: str, index: int) -> int:
    """Return the end of the numeric literal starting at index."""
    base = text[index + 1 : index + 2]
    if text[index] == "0" and base in ("b", "B", "o", "O", "x", "X"):
        digits = {"b": "01_", "o": "01234567_", "x": _HEX + "_"}[base.lower()]
        return _digits_end(text, index + 2, digits)
    index = _digits_end(text, index, "0123456789_")
    if text.startswith("..", index):
        return index
    if text[index : index + 1] == ".":
        index += 1
        if text[index : index + 1].isdigit():
            index = _digits_end(text, index, "0123456789_")
        elif _is_id_first(text[index : index + 1]) or text[index : index + 1] == "«":
            raise _Unsettled  # Lean rejects an identifier after a bare dot
    if text[index : index + 1] in ("e", "E"):
        exponent = index + 1
        if text[exponent : exponent + 1] in ("+", "-"):
            exponent += 1
        if not "0" <= text[exponent : exponent + 1] <= "9":
            raise _Unsettled  # Lean rejects an exponent without digits
        index = _digits_end(text, exponent, "0123456789_")
    return index


def _skip_blanks(text: str, index: int) -> int:
    while index < len(text) and text[index] in _BLANK:
        index += 1
    return index


def _digits_end(text: str, index: int, digits: str) -> int:
    while index < len(text) and text[index] in digits:
        index += 1
    return index


def _is_id_first(char: str) -> bool:
    """Lean's ``isIdFirst``: an ASCII letter, ``_`` or a letter-like symbol."""
    return bool(char) and (
        ("a" <= char <= "z")
        or ("A" <= char <= "Z")
        or char == "_"
        or _letter_like(char)
    )


def _is_id_rest(char: str) -> bool:
    """Lean's ``isIdRest``: what may continue an identifier."""
    return _is_id_first(char) or "0" <= char <= "9" or char in "'!?" or _subscript(char)


def _letter_like(char: str) -> bool:
    """Lean's ``isLetterLike``."""
    value = ord(char)
    return (
        (0x3B1 <= value <= 0x3C9 and value != 0x3BB)
        or (0x391 <= value <= 0x3A9 and value not in (0x3A0, 0x3A3))
        or 0x3CA <= value <= 0x3FB
        or 0x1F00 <= value <= 0x1FFE
        or 0x2100 <= value <= 0x214F
        or 0x1D49C <= value <= 0x1D59F
        or (0x00C0 <= value <= 0x00FF and value not in (0x00D7, 0x00F7))
        or 0x0100 <= value <= 0x017F
    )


def _subscript(char: str) -> bool:
    """Lean's ``isSubScriptAlnum``."""
    value = ord(char)
    return (
        0x2080 <= value <= 0x2089
        or 0x2090 <= value <= 0x209C
        or 0x1D62 <= value <= 0x1D6A
        or value == 0x2C7C
    )
