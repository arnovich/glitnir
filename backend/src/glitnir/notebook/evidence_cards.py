"""List `-- @evidence <verb> <target> <options>` directives as preview cards.

A directive is a comment line where Lean reads code, as `lexing.scan` finds
it; the same text inside a string or a block comment is not one. Where the
scan is unsure, the line is read as a directive unless the source before it
ends inside a literal or comment: evidence only proposes work and never
grants proof, so reading too much costs at most a card that fails.
"""

import hashlib
import re
from typing import Any

from glitnir.lean.lexing import scan
from glitnir.providers import (
    DecisionProvider,
    EvidenceProvider,
    EvidenceRequest,
    Providers,
)

_DIRECTIVE = re.compile(r"--\s*@evidence\s+(\w+)\s+(\S+)(?:\s+(.*?))?\s*")
_START = re.compile(r"\s*--\s*@evidence\b")
# A target is a Lean identifier, possibly qualified, as view names are.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*")
INCOMPLETE = (
    "This evidence directive is incomplete. Write it at the start of a line as "
    "`-- @evidence <verb> <target>`."
)
BAD_TARGET = "Evidence targets must be Lean identifiers."


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
        index for index, line in enumerate(lines) if _START.match(line) is not None
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
            match = _DIRECTIVE.fullmatch(source)
            if match is None:  # listed so the reader sees the mistake
                words = _START.sub("", source, count=1).split() + ["", ""]
                verb, target, options, error = words[0], words[1], "", INCOMPLETE
            else:
                verb, target, options = match.group(1), match.group(2), match.group(3)
                error = "" if _NAME.fullmatch(target) else BAD_TARGET
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


def provider_failure(
    provider: EvidenceProvider | DecisionProvider, error: BaseException
) -> str:
    """Word an exception raised by provider code; KeyboardInterrupt propagates."""
    if isinstance(error, KeyboardInterrupt):
        raise error
    return f"Provider {provider.name} failed: {type(error).__name__}: {error}"


def provider_unavailable(
    provider: EvidenceProvider | DecisionProvider,
) -> tuple[str | None, str]:
    """Return what the provider lacks, or the error asking it raised."""
    try:
        missing = provider.unavailable()
    except BaseException as error:  # provider code must never break the preview
        return None, provider_failure(provider, error)
    if missing is not None and not isinstance(missing, str):
        return None, f"Provider {provider.name} reported an unreadable availability."
    return missing, ""


def evidence_request(
    provider: EvidenceProvider, directive: dict[str, Any]
) -> tuple[EvidenceRequest | None, str]:
    """Return the provider's request for a directive, or why it rejects it."""
    try:
        request = provider.parse(
            directive["verb"], directive["target"], directive["options"]
        )
    except ValueError as error:
        return None, str(error) or "The provider rejected this directive."
    except BaseException as error:  # provider code must never break the preview
        return None, provider_failure(provider, error)
    if not isinstance(request, EvidenceRequest):
        return None, f"Provider {provider.name} returned an unreadable request."
    return request, ""


def _parse_error(provider: EvidenceProvider, directive: dict[str, Any]) -> str:
    """Return why the provider rejects the directive, or "" if it accepts it."""
    return evidence_request(provider, directive)[1]


def evidence_cards(
    directives: list[dict[str, Any]], providers: Providers
) -> list[dict[str, Any]]:
    """Give each directive its provider and errors; nothing runs or computes."""
    notes = "".join(
        f" (Provider {name} failed to load: {reason})"
        for name, reason in providers.errors.items()
    )
    cards: list[dict[str, Any]] = []
    for directive in directives:
        provider = providers.evidence.get(directive["verb"])
        error, unavailable = directive["error"], None
        if not error and provider is None:
            error = f"No installed provider runs `@evidence {directive['verb']}`."
            error += notes
        if not error and provider is not None:
            unavailable, error = provider_unavailable(provider)
            error = error or _parse_error(provider, directive)
        cards.append(
            {
                "cell": directive["cell"],
                "line": directive["line"],
                "verb": directive["verb"],
                "target": directive["target"],
                "key": directive["key"],
                "provider": provider.name if provider is not None else None,
                "error": error,
                "unavailable": unavailable,
                "status": "never",
                "run_error": None,
                "snapshot": None,
                "failure": None,
            }
        )
    return cards
