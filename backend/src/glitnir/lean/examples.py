"""Checking `example` commands as named theorems."""

from typing import Any

from glitnir.lean.failures import CommandFailure
from glitnir.lean.lexing import code_tokens, only_comments
from glitnir.lean.project_snapshot import SnapshotError

#: The prefix of the theorem names examples are checked under; see `example_tag`.
EXAMPLE_TAG = "_glitnir_example_"
_KEYWORD = b"example"
#: Added to an unknown-identifier failure inside an example.
EXAMPLE_NOTE = (
    "Glitnir checks this example as a theorem, which includes only the section "
    "variables its statement mentions"
)
#: What may follow a checked example's keyword: whitespace or a signature.
_AFTER_KEYWORD = (b" ", b"\t", b"\n", b"(", b"{", b"[", b":", "⦃".encode())
#: Declaration modifiers; an example after one is not checked.
_MODIFIERS = frozenset(
    (
        "private",
        "protected",
        "public",
        "meta",
        "noncomputable",
        "unsafe",
        "partial",
        "nonrec",
    )
)


def example_tag(text: str) -> str:
    """Return the prefix of the names the examples of ``text`` are checked under.

    The example whose `example` keyword starts at byte ``n`` is checked as
    ``private theorem <tag>n``. The tag occurs nowhere in ``text``, so the
    source cannot declare or mention any of these names itself.
    """
    tag = EXAMPLE_TAG
    while tag in text:
        tag = "_" + tag
    return tag


def _byte_offsets(text: str, offsets: list[int]) -> list[int]:
    """Convert increasing character offsets into ``text`` to UTF-8 byte offsets."""
    result = []
    position = size = 0
    for offset in offsets:
        size += len(text[position:offset].encode())
        position = offset
        result.append(size)
    return result


def _span(item: Any, size: int) -> tuple[int, int]:
    """Return a reported example's keyword span, checked against its command."""
    span = item.get("keyword") if isinstance(item, dict) else None
    if (
        not isinstance(span, list)
        or len(span) != 2
        or not all(type(value) is int for value in span)
        or type(item.get("start")) is not int
        or type(item.get("end")) is not int
    ):
        raise SnapshotError("The source declaration report is malformed.")
    first, last = span
    if (
        last - first != len(_KEYWORD)
        or not 0 <= item["start"] <= first < last <= item["end"] <= size
    ):
        raise SnapshotError("The source declaration report is malformed.")
    return first, last


def _token(text: str, tokens: list[tuple[int, int]], index: int) -> str:
    start, end = tokens[index]
    return text[start:end]


def _attribute_or_modifier(
    text: str, tokens: list[tuple[int, int]], index: int
) -> bool:
    """Whether code token ``index`` follows an attribute or a modifier.

    That is, the token before it is a modifier keyword, or the `]` closing an
    `@[…]` attribute list. Doc comments are not tokens, so they never count.
    """
    if index == 0:
        return False
    before = _token(text, tokens, index - 1)
    if before in _MODIFIERS:
        return text[tokens[index - 1][0] - 1 : tokens[index - 1][0]] != "."
    if before != "]":
        return False
    depth = 0
    for position in range(index - 1, -1, -1):
        piece = _token(text, tokens, position)
        depth += {"]": 1, "[": -1}.get(piece, 0)
        if depth == 0:
            return (
                position > 0
                and _token(text, tokens, position - 1) == "@"
                and tokens[position - 1][1] == tokens[position][0]
            )
    return False


def _boundary_before(data: bytes, offset: int, items: list[dict[str, Any]]) -> int:
    """Return the end of the last reported command before byte ``offset``.

    It ends a whole command, so the source before it can be checked alone; 0
    when there is none, which makes the check unavailable.
    """
    ends: list[int] = [item["end"] for item in items if item["end"] <= offset]
    if not ends:
        return 0
    end = max(ends)
    return end + 1 if data[end : end + 1] == b"\n" else end


def example_edits(
    text: str,
    entries: list[dict[str, Any]],
    unclaimed: list[dict[str, Any]],
    tag: str,
) -> tuple[list[tuple[int, bytes]], list[dict[str, Any]]]:
    """Return the edits for the claimed examples, and the examples claiming nothing.

    The reader, which runs the source's own code, is untrusted: it only
    classifies. Every `example` keyword Lean reads in ``text`` is found here by
    the trusted lexer, and the reader must account for exactly those, each
    once, as a claim (an entry with ``keyword``) or in ``unclaimed``. A claim's
    keyword must follow nothing but whitespace, comments and at most one
    docstring from its command's start, be followed by whitespace or its
    signature, and its theorem must carry the name the rewrite gives it.
    Otherwise the report is malformed. Examples claiming nothing are returned
    only when the lexer read the file exactly.

    Raises:
        CommandFailure: at the first claimed example when the lexer cannot read
            the file exactly; at an example after an attribute or a modifier,
            or one that uses `decl_name%`, which a renamed theorem reads
            differently; or before an `example` keyword that is not an example
            command.
        SnapshotError: the report does not match the source.
    """
    data = text.encode()
    claimed = [entry for entry in entries if "keyword" in entry]
    spans = [_span(entry, len(data)) for entry in claimed]
    others = [_span(entry, len(data)) for entry in unclaimed]
    tokens = code_tokens(text)
    if tokens is None:
        if claimed:
            raise CommandFailure(
                "Glitnir cannot read this file exactly enough to check its "
                "examples (for example, a string containing `{`); state them as "
                "named theorems instead.",
                offset=min(entry["start"] for entry in claimed),
            )
        return [], []
    indices = [
        index
        for index, (start, _) in enumerate(tokens)
        if _token(text, tokens, index) == _KEYWORD.decode()
        and text[start - 1 : start] != "."
    ]
    offsets = _byte_offsets(text, [tokens[index][0] for index in indices])
    token_at = dict(zip(offsets, indices, strict=True))
    reported = sorted(first for first, _ in spans + others)
    if len(set(reported)) != len(reported) or not set(reported) <= set(offsets):
        raise SnapshotError(
            "The reader's examples do not match the source's `example` keywords."
        )
    # Every reason to stop is collected, and the earliest one wins: stopping
    # later would leave an earlier one in the source checked before it.
    failures: list[CommandFailure] = []
    missing = sorted(set(offsets) - set(reported))
    if missing:
        line = data.count(b"\n", 0, missing[0]) + 1
        failures.append(
            CommandFailure(
                f"Line {line} has the `example` keyword outside an example "
                "command; Glitnir checks no example from there.",
                offset=_boundary_before(data, missing[0], entries + unclaimed),
            )
        )
    edits: list[tuple[int, bytes]] = []
    previous = 0
    for entry, (first, last) in zip(claimed, spans, strict=True):
        if _attribute_or_modifier(text, tokens, token_at[first]):
            failures.append(
                CommandFailure(
                    "Examples with attributes or modifiers are not checked; "
                    "name it as a theorem.",
                    offset=entry["start"],
                )
            )
        name = f"{tag}{first}"
        try:
            lead = data[entry["start"] : first].decode()
        except UnicodeDecodeError as error:
            raise SnapshotError(
                "The source declaration report is malformed."
            ) from error
        if (
            entry["start"] < previous
            or not only_comments(lead)
            or not data[last:].startswith(_AFTER_KEYWORD)
            or entry["name"].rsplit(".", 1)[-1] != name
        ):
            raise SnapshotError("The source declaration report is malformed.")
        if b"decl_name%" in data[entry["start"] : entry["end"]]:
            failures.append(
                CommandFailure(
                    "An example that uses `decl_name%` is not checked: Glitnir "
                    "checks an example as a theorem with another name.",
                    offset=entry["start"],
                )
            )
        edits.append((first, f"private theorem {name}".encode()))
        previous = entry["end"]
    if failures:
        raise min(failures, key=lambda failure: failure.offset)
    return edits, unclaimed


def rewrite_examples(data: bytes, edits: list[tuple[int, bytes]]) -> bytes:
    """Return ``data`` with the `example` keyword at each edit replaced."""
    parts = []
    cursor = 0
    for offset, replacement in edits:
        parts += [data[cursor:offset], replacement]
        cursor = offset + len(_KEYWORD)
    parts.append(data[cursor:])
    return b"".join(parts)


def check_rewrite(
    original: bytes, compiled: bytes, edits: list[tuple[int, bytes]]
) -> None:
    """Require ``compiled`` to differ from ``original`` only at the edits.

    Every other byte must be equal and in order, each edit must replace one
    `example` keyword, and no replacement may add a line: every line keeps its
    number, and only columns after a keyword on its own line move.
    """
    left = right = 0
    for offset, replacement in edits:
        length = offset - left
        if (
            length < 0
            or b"\n" in replacement
            or original[offset : offset + len(_KEYWORD)] != _KEYWORD
            or compiled[right : right + length] != original[left:offset]
            or compiled[right + length : right + length + len(replacement)]
            != replacement
        ):
            raise SnapshotError("The compiled source differs from the saved source.")
        left = offset + len(_KEYWORD)
        right += length + len(replacement)
    if compiled[right:] != original[left:]:
        raise SnapshotError("The compiled source differs from the saved source.")


def original_offset(offset: int, edits: list[tuple[int, bytes]]) -> int:
    """Map a byte offset in the compiled source back to the saved source.

    An offset inside a replacement maps to the start of its `example` keyword.
    """
    shift = 0
    for first, replacement in edits:
        start = first + shift
        if offset < start:
            break
        if offset < start + len(replacement):
            return first
        shift += len(replacement) - len(_KEYWORD)
    return offset - shift
