"""List `-- @decide <name> : <goal>` directives as preview cards.

A directive is a comment line where Lean reads code, as `lexing.scan` finds
it, and the comment lines right after it that start with `--` and at least two
spaces (not `@`) continue its goal. The same text inside a string or a block
comment is not one; where the scan is unsure, lines are read as in
`evidence_cards`. A decision only proposes a Lean block that the project
checker then checks, so reading too much costs at most a card that fails.

A provider's outcome is untrusted; `providers.outcomes.checked_decision` is
all of it the preview and the saved records ever see.
"""

import hashlib
import re
from typing import Any

from glitnir.notebook.evidence_cards import (
    directive_lines,
    provider_failure,
    provider_unavailable,
)
from glitnir.providers import DecisionProvider, DecisionRequest, Providers

_HEAD = re.compile(r"--\s*@decide\b(.*)")
_START = re.compile(r"\s*--\s*@decide\b")
_CONTINUATION = re.compile(r"--\s{2,}(?!@)(\S.*)")
_BODY = re.compile(r"\s*([^\s:]+)\s*:(.*)")
# A decision's name becomes a theorem name, so it is a plain Lean identifier.
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
INCOMPLETE = (
    "This decision directive is incomplete. Write it at the start of a line as "
    "`-- @decide <name> : <goal>`."
)
BAD_NAME = (
    "Decision names are Lean identifiers: a letter, then letters, digits or "
    "underscores."
)


def decision_key(directive_text: str, occurrence: int) -> str:
    """Name a directive's card by its text and its index among identical ones.

    `directive_text` is the head line and its continuation lines joined with
    newlines; each line's trailing blanks are ignored.
    """
    normal = "\n".join(line.rstrip() for line in directive_text.split("\n"))
    return hashlib.sha256(f"{normal}\n{occurrence}".encode("utf-8")).hexdigest()


def _read(head: str, continuations: list[str]) -> tuple[str, str, str, str]:
    """Return the name, kind, goal and error of one directive's lines."""
    match = _HEAD.fullmatch(head)
    rest = match.group(1) if match is not None else _START.sub("", head, count=1)
    body = " ".join([rest, *continuations])
    parts = _BODY.fullmatch(body)
    if parts is None:
        words = body.split() + [""]
        return words[0], "", "", INCOMPLETE
    name, goal = parts.group(1), " ".join(parts.group(2).split())
    kind = goal.split()[0] if goal else ""
    if match is None or not goal:
        return name, kind, goal, INCOMPLETE
    return name, kind, goal, "" if _NAME.fullmatch(name) else BAD_NAME


def decision_directives(cells: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Find decision directives in the source cells of `text`, in file order."""
    lines = text.split("\n")
    if not any(_START.match(line) for line in lines):
        return []
    # A continuation can only follow a head or another continuation, so other
    # comment lines never need the (possibly slow) readability check.
    candidates: list[int] = []
    for index, line in enumerate(lines):
        if _START.match(line) is not None:
            candidates.append(index)
        elif candidates and candidates[-1] == index - 1:
            if _CONTINUATION.fullmatch(line):
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
            if line - 1 not in readable or _START.match(head) is None:
                line += 1
                continue
            end = line
            continuations: list[str] = []
            while end < cell["end"] and end in readable:
                more = _CONTINUATION.fullmatch(lines[end])
                if more is None:
                    break
                continuations.append(more.group(1))
                end += 1
            name, kind, goal, error = _read(head, continuations)
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


def decision_request(
    provider: DecisionProvider, directive: dict[str, Any]
) -> tuple[DecisionRequest | None, str]:
    """Return the provider's request for a directive, or why it rejects it."""
    try:
        request = provider.parse(
            directive["name"], directive["kind"], directive["goal"]
        )
    except ValueError as error:
        return None, str(error) or "The provider rejected this directive."
    except BaseException as error:  # provider code must never break the preview
        return None, provider_failure(provider, error)
    if not isinstance(request, DecisionRequest):
        return None, f"Provider {provider.name} returned an unreadable request."
    return request, ""


def decision_cards(
    directives: list[dict[str, Any]], providers: Providers
) -> list[dict[str, Any]]:
    """Give each directive its provider and errors; nothing runs or decides."""
    notes = "".join(
        f" (Provider {name} failed to load: {reason})"
        for name, reason in providers.errors.items()
    )
    cards: list[dict[str, Any]] = []
    for directive in directives:
        provider = providers.decisions.get(directive["kind"])
        error, unavailable = directive["error"], None
        if not error and provider is None:
            error = f"No installed provider decides `{directive['kind']}` goals."
            error += notes
        if not error and provider is not None:
            unavailable, error = provider_unavailable(provider)
            error = error or decision_request(provider, directive)[1]
        cards.append(
            {
                **directive,
                "provider": provider.name if provider is not None else None,
                "error": error,
                "unavailable": unavailable,
                # Imports are checked against the prepared setup when a
                # decision runs, not read from the text here.
                "missing_imports": [],
                "status": "never",
                "run_error": None,
                "outcome": None,
                "verification": None,
            }
        )
    return cards
