"""Find `-- @evidence`, `-- @decide` and `-- @view` comment-line directives.

A directive is a comment line where Lean reads code, as `lexing.scan` finds
it; the same text inside a string or a block comment is not one. Where the
scan is unsure, the line is read as a directive unless the source before it
ends inside a literal or comment: evidence only proposes work and never
grants proof, so reading too much costs at most a card that fails.
"""

import hashlib
import re
from typing import Any

from glitnir.lean.lexing import command_boundaries, only_comments, scan

_EVIDENCE_DIRECTIVE = re.compile(r"--\s*@evidence\s+(\w+)\s+(\S+)(?:\s+(.*?))?\s*")
_EVIDENCE_START = re.compile(r"\s*--\s*@evidence\b")
# A target is a Lean identifier, possibly qualified, as view names are.
_EVIDENCE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")
INCOMPLETE_EVIDENCE = (
    "This evidence directive is incomplete. Write it at the start of a line as "
    "`-- @evidence <verb> <target>`."
)
BAD_TARGET = "Evidence targets must be Lean identifiers."
_DECISION_HEAD = re.compile(r"--\s*@decide\b(.*)")
_DECISION_START = re.compile(r"\s*--\s*@decide\b")
_DECISION_CONTINUATION = re.compile(r"--\s{2,}(?!@)(\S.*)")
_DECISION_BODY = re.compile(r"\s*([^\s:]+)\s*:(.*)")
# A decision's name becomes a theorem name, so it is a plain Lean identifier.
_DECISION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
INCOMPLETE_DECISION = (
    "This decision directive is incomplete. Write it at the start of a line as "
    "`-- @decide <name> : <goal>`."
)
BAD_NAME = (
    "Decision names are Lean identifiers: a letter, then letters, digits or "
    "underscores."
)
_VIEW_DIRECTIVE = re.compile(r"--\s*@view\s+(\w+)\s+(\S+)\s*(?:using\s+(.+))?\s*")
_VIEW_START = re.compile(r"--\s*@view\b")
INCOMPLETE_VIEW = (
    "This view directive is incomplete. Write it at the start of a line as "
    "`-- @view <kind> <name>`."
)
# A view name is a Lean identifier, possibly qualified.
_VIEW_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")
BAD_VIEW_NAME = "View names must be Lean identifiers."
MAX_USING = 16


def evidence_key(line_text: str, occurrence: int) -> str:
    """Name a directive's card by its text and its index among identical lines."""
    normal = line_text.rstrip()
    return hashlib.sha256(f"{normal}\n{occurrence}".encode("utf-8")).hexdigest()


def directive_lines(text: str, candidates: list[int]) -> set[int]:
    """Return the zero-based `candidates` that Lean reads as comment lines."""
    found = scan(text)
    if found.settled:
        return set(candidates) & found.comment_lines
    # The comment lines found before the scan's first doubt are sure.
    lines = text.split("\n")
    return {
        index
        for index in candidates
        if index in found.comment_lines
        or not scan("".join(line + "\n" for line in lines[:index])).open_at_end
    }


def evidence_directives(cells: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Find evidence directives in the source cells of `text`, in file order."""
    lines = text.split("\n")
    candidates = [
        index
        for index, line in enumerate(lines)
        if _EVIDENCE_START.match(line) is not None
    ]
    if not candidates:
        return []
    readable = directive_lines(text, candidates)
    found: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for cell in cells:
        if cell["kind"] != "source":
            continue
        for line in range(cell["start"], cell["end"] + 1):
            if line - 1 not in readable:
                continue
            source = lines[line - 1]
            match = _EVIDENCE_DIRECTIVE.fullmatch(source)
            if match is None:  # listed so the reader sees the mistake
                words = _EVIDENCE_START.sub("", source, count=1).split() + ["", ""]
                verb, target, options, error = (
                    words[0],
                    words[1],
                    "",
                    INCOMPLETE_EVIDENCE,
                )
            else:
                verb, target, options = match.group(1), match.group(2), match.group(3)
                error = "" if _EVIDENCE_NAME.fullmatch(target) else BAD_TARGET
            normal = source.rstrip()
            occurrence = seen.get(normal, 0)
            seen[normal] = occurrence + 1
            found.append(
                {
                    "cell": cell["index"],
                    "line": line,
                    "verb": verb,
                    "target": target,
                    "options": options or "",
                    "key": evidence_key(source, occurrence),
                    "error": error,
                }
            )
    return found


def decision_key(directive_text: str, occurrence: int) -> str:
    """Name a directive's card by its text and its index among identical ones.

    `directive_text` is the head line and its continuation lines joined with
    newlines; each line's trailing blanks are ignored.
    """
    normal = "\n".join(line.rstrip() for line in directive_text.split("\n"))
    return hashlib.sha256(f"{normal}\n{occurrence}".encode("utf-8")).hexdigest()


def _read_decision(head: str, continuations: list[str]) -> tuple[str, str, str, str]:
    """Return the name, kind, goal and error of one directive's lines."""
    match = _DECISION_HEAD.fullmatch(head)
    rest = (
        match.group(1) if match is not None else _DECISION_START.sub("", head, count=1)
    )
    body = " ".join([rest, *continuations])
    parts = _DECISION_BODY.fullmatch(body)
    if parts is None:
        words = body.split() + [""]
        return words[0], "", "", INCOMPLETE_DECISION
    name, goal = parts.group(1), " ".join(parts.group(2).split())
    kind = goal.split()[0] if goal else ""
    if match is None or not goal:
        return name, kind, goal, INCOMPLETE_DECISION
    return name, kind, goal, "" if _DECISION_NAME.fullmatch(name) else BAD_NAME


def decision_directives(cells: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Find decision directives in the source cells of `text`, in file order."""
    lines = text.split("\n")
    if not any(_DECISION_START.match(line) for line in lines):
        return []
    # A continuation can only follow a head or another continuation, so other
    # comment lines never need the (possibly slow) readability check.
    candidates: list[int] = []
    for index, line in enumerate(lines):
        if _DECISION_START.match(line) is not None:
            candidates.append(index)
        elif candidates and candidates[-1] == index - 1:
            if _DECISION_CONTINUATION.fullmatch(line):
                candidates.append(index)
    readable = directive_lines(text, candidates)
    found: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    named: set[str] = set()
    for cell in cells:
        if cell["kind"] != "source":
            continue
        line = cell["start"]
        while line <= cell["end"]:
            head = lines[line - 1]
            if line - 1 not in readable or _DECISION_START.match(head) is None:
                line += 1
                continue
            end = line
            continuations: list[str] = []
            while end < cell["end"] and end in readable:
                more = _DECISION_CONTINUATION.fullmatch(lines[end])
                if more is None:
                    break
                continuations.append(more.group(1))
                end += 1
            name, kind, goal, error = _read_decision(head, continuations)
            if not error and name in named:
                error = f"Another `@decide` directive above already names `{name}`."
            if not error:
                named.add(name)
            normal = "\n".join(source.rstrip() for source in lines[line - 1 : end])
            occurrence = seen.get(normal, 0)
            seen[normal] = occurrence + 1
            found.append(
                {
                    "cell": cell["index"],
                    "line": line,
                    "end_line": end,
                    "name": name,
                    "kind": kind,
                    "goal": goal,
                    "key": decision_key(normal, occurrence),
                    "error": error,
                }
            )
            line = end + 1
    return found


def view_directives(cells: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Find `-- @view <kind> <name> [using a, b]` lines in source cells.

    As for evidence and decisions, only lines Lean reads as comment lines of
    `source` count; the same text in a string or block comment does not.
    """
    candidates = [
        index
        for index, line in enumerate(source.split("\n"))
        if _VIEW_START.match(line.strip()) is not None
    ]
    if not candidates:
        return []
    readable = directive_lines(source, candidates)
    found: list[dict[str, Any]] = []
    for cell in cells:
        if cell["kind"] != "source":
            continue
        for offset, text in enumerate(cell["text"].split("\n")):
            if cell["start"] + offset - 1 not in readable:
                continue
            match = _VIEW_DIRECTIVE.fullmatch(text)
            if match is None:
                start = _VIEW_START.match(text.strip())
                if start is not None:  # listed so the reader sees the mistake
                    words = start.string[start.end() :].split()
                    found.append(
                        {
                            "cell": cell["index"],
                            "line": cell["start"] + offset,
                            "kind": words[0] if words else "",
                            "name": words[1] if len(words) > 1 else "",
                            "using": [],
                            "error": INCOMPLETE_VIEW,
                        }
                    )
                continue
            kind, name, tail = match.groups()
            tail = (tail or "").strip()
            if tail.startswith("[") and tail.endswith("]"):
                tail = tail[1:-1]
            using = [item.strip() for item in tail.split(",") if item.strip()]
            error = ""
            if not all(_VIEW_NAME.fullmatch(item) for item in [name, *using]):
                using, error = [], BAD_VIEW_NAME
            elif len(using) > MAX_USING:
                using, error = [], f"Too many `using` names (at most {MAX_USING})."
            found.append(
                {
                    "cell": cell["index"],
                    "line": cell["start"] + offset,
                    "kind": kind,
                    "name": name,
                    "using": using,
                    "error": error,
                }
            )
    return found


def view_keys(text: str, views: list[dict[str, Any]]) -> dict[int, str]:
    """Map each listed view directive's line to its saved-drawing key.

    As for evidence, the key is the directive's line text and its index among
    identical listed directive lines (`evidence_key`).
    """
    lines = text.split("\n")
    seen: dict[str, int] = {}
    keys: dict[int, str] = {}
    for view in views:
        source = lines[view["line"] - 1]
        normal = source.rstrip()
        occurrence = seen.get(normal, 0)
        seen[normal] = occurrence + 1
        keys[view["line"]] = evidence_key(source, occurrence)
    return keys


def starts_command(text: str, line: int) -> bool:
    """Whether the directive at one-based `line` of `text` lies between commands.

    The trusted lexer must find that the command before it ends above it
    (`CommandBoundaries.ends_at(line - 1)`), or nothing but comments may
    precede it. A run elaborates the notebook cut at each of its directives,
    so only such a directive is drawn in a run with others.
    """
    if command_boundaries(text).ends_at(line - 1):
        return True
    before = "\n".join(text.split("\n", line - 1)[: line - 1])
    return only_comments(before, docstrings=0)
