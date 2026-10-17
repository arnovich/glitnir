"""List `-- @evidence <verb> <target> <options>` directives as preview cards.

A directive is a comment line where Lean reads code, as `lexing.scan` finds
it; the same text inside a string or a block comment is not one. Where the
scan is unsure, the line is read as a directive unless the source before it
ends inside a literal or comment: evidence only proposes work and never
grants proof, so reading too much costs at most a card that fails.
"""

import hashlib
import json
import math
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
EVIDENCE_FORMAT = "glitnir.evidence/v1"
MAX_SAMPLES = 4000
MAX_SERIES = 16
MAX_TEXT = 200
MAX_PAIRS = 32
MAX_BYTES = 2_000_000
_BODY_KEYS = {"format", "series", "plane", "provenance", "finite"}
_SERIES_KEYS = {"times", "values", "names"}


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
            }
        )
    return cards


def _number(value: Any, what: str, *, nullable: bool = False) -> float | None:
    """Return `value` as a finite float; booleans are not numbers here."""
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number.")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{what} is out of range.") from None
    if not math.isfinite(number):
        raise ValueError(f"{what} must be finite.")
    return number


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ValueError(f"{what} must be text of at most {MAX_TEXT} characters.")
    return value


def _list(value: Any, what: str, limit: int) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list.")
    if len(value) > limit:
        raise ValueError(f"{what} may hold at most {limit} entries.")
    return value


def _checked_series(series: Any) -> dict[str, Any]:
    if not isinstance(series, dict) or set(series) != _SERIES_KEYS:
        raise ValueError("series must hold exactly times, values and names.")
    times = [
        _number(time, "A time") for time in _list(series["times"], "times", MAX_SAMPLES)
    ]
    names = [
        _text(name, "A series name")
        for name in _list(series["names"], "names", MAX_SERIES)
    ]
    rows = _list(series["values"], "values", MAX_SAMPLES)
    if len(rows) != len(times):
        raise ValueError("values needs one row for each time.")
    values = []
    for row in rows:
        row = _list(row, "A row of values", MAX_SERIES)
        if len(row) != len(names):
            raise ValueError("Each row of values needs one value for each name.")
        values.append([_number(item, "A value", nullable=True) for item in row])
    return {"times": times, "values": values, "names": names}


def _checked_plane(plane: Any, width: int) -> list[int]:
    if not isinstance(plane, list) or len(plane) != 2:
        raise ValueError("plane must be two series indices.")
    for index in plane:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("plane must be two series indices.")
        if not 0 <= index < width:
            raise ValueError("plane refers to a series that does not exist.")
    return list(plane)


def _checked_provenance(provenance: Any) -> list[list[str]]:
    pairs = []
    for pair in _list(provenance, "provenance", MAX_PAIRS):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("provenance must hold [label, value] pairs.")
        pairs.append([_text(pair[0], "A label"), _text(pair[1], "A value")])
    return pairs


def checked_evidence(body: Any) -> dict[str, Any]:
    """Return a normalised copy of provider output, or raise ValueError.

    Provider output is untrusted: the shape, bounds and finiteness checked here
    are all the preview and the saved file ever see of it.
    """
    if not isinstance(body, dict):
        raise ValueError("Evidence must be an object.")
    unknown = set(body) - _BODY_KEYS
    if unknown:
        raise ValueError(f"Evidence has unknown keys: {', '.join(sorted(unknown))}.")
    if body.get("format") != EVIDENCE_FORMAT:
        raise ValueError(f"Evidence format must be {EVIDENCE_FORMAT}.")
    for key in ("series", "provenance", "finite"):
        if key not in body:
            raise ValueError(f"Evidence needs {key}.")
    if not isinstance(body["finite"], bool):
        raise ValueError("finite must be true or false.")
    series = _checked_series(body["series"])
    checked: dict[str, Any] = {"format": EVIDENCE_FORMAT, "series": series}
    if "plane" in body:
        checked["plane"] = _checked_plane(body["plane"], len(series["names"]))
    checked["provenance"] = _checked_provenance(body["provenance"])
    checked["finite"] = body["finite"]
    size = len(json.dumps(checked, separators=(",", ":")).encode("utf-8"))
    if size > MAX_BYTES:
        raise ValueError(f"Evidence may hold at most {MAX_BYTES} bytes.")
    return checked
