"""What a well-formed `<Notebook>.glitnir.json` is.

Nothing here reads or writes a file.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Final, Literal, NotRequired, TypedDict

from glitnir.lean.digest import fingerprint_ok
from glitnir.lean.project_checking import MAX_USES
from glitnir.lean.results import Axiom, Use, Verdict, Withheld
from glitnir.providers.outcomes import (
    Decision,
    Drawing,
    Evidence,
    checked_decision,
    checked_drawing,
    checked_evidence,
)

FORMAT = "glitnir.project-checks/v2"
#: The earlier format, read and upgraded to `FORMAT` on loading.
FORMAT_V1 = "glitnir.project-checks/v1"
_WITHHELD_KEYS = frozenset({"from_line", "message"})
_CLAIM_KEYS: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "status": str,
    "axioms": list,
    "start_line": int,
    "end_line": int,
    "declaration_sha256": str,
    "prefix_sha256": str,
    "proposition_sha256": str,
}
_NON_CLAIM_KEYS: dict[str, type] = {
    "start_line": int,
    "end_line": int,
    "declaration_sha256": str,
    "prefix_sha256": str,
}
_EVIDENCE_KEYS = ("prefix_sha256", "fingerprint", "provider", "created")
_VIEW_KEYS = (*_EVIDENCE_KEYS, "provider_sha256")
_FAILURES: Final = "evidence_failures"
_DECISION_FAILURES: Final = "decision_failures"
_FAILURE_SECTIONS: Final = (_FAILURES, _DECISION_FAILURES)
_FAILURE_KEYS = (*_EVIDENCE_KEYS, "kind", "message")
#: What made a saved evidence run fail: Lean's own error for the directive, a
#: provider crash, the provider's refusal, output that is not evidence, or the
#: run's own time limit.
EVIDENCE_FAILURE_KINDS = frozenset(
    {"lean", "provider", "refused", "invalid", "timeout"}
)
#: What made a saved decision run fail: a provider crash, the provider's
#: refusal, an outcome that is not a decision of the directive, or the
#: candidate check's own time limit. A block the kernel check rejects is a
#: saved decision, never a failure.
DECISION_FAILURE_KINDS = frozenset({"provider", "refused", "invalid", "timeout"})
_VERIFICATION_KEYS = frozenset({"status", "message", "axioms", "type"})
_USE_KEYS = frozenset({"module", "name", "in_notebook"})
# When a run saved its result: UTC to the second, as runs write it ("+00:00")
# or with the equivalent "Z".
_CREATED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+00:00|Z)")
MAX_MESSAGE = 2000
# A checked statement's kernel form; a run saves a longer one shortened.
MAX_TYPE = 20_000

#: The result sections a records file may hold besides its claims.
Section = Literal[
    "evidence", "decisions", "views", "evidence_failures", "decision_failures"
]
#: The two sections of saved failed runs.
FailureSection = Literal["evidence_failures", "decision_failures"]


class Environment(TypedDict):
    """The environment the records' check ran in, and the check's identity.

    A check run remotely adds `execution`, the compute layer's description.
    """

    fingerprint: str
    check: str
    execution: NotRequired[dict[str, Any]]


class ClaimRecord(TypedDict):
    """One saved claim, as `check_records.py`'s docstring describes it."""

    name: str
    status: str
    axioms: list[Axiom]
    start_line: int
    end_line: int
    declaration_sha256: str
    prefix_sha256: str
    proposition_sha256: str
    uses: NotRequired[list[Use]]
    uses_truncated: NotRequired[Literal[True]]
    example: NotRequired[Literal[True]]
    statement: NotRequired[str]
    statement_shortened: NotRequired[Literal[True]]
    kernel_statement: NotRequired[str]
    kernel_statement_shortened: NotRequired[Literal[True]]
    fingerprint: NotRequired[str]


class NonClaimRecord(TypedDict):
    """A saved example that claims nothing, found again by its text."""

    start_line: int
    end_line: int
    declaration_sha256: str
    prefix_sha256: str


class EvidenceEntry(TypedDict):
    """Saved evidence for one directive: its origin and the checked snapshot."""

    prefix_sha256: str
    fingerprint: str
    provider: str
    created: str
    snapshot: Evidence


class DecisionEntry(TypedDict):
    """A saved decision: the provider's outcome and the kernel's verdict."""

    prefix_sha256: str
    fingerprint: str
    provider: str
    created: str
    outcome: Decision
    verification: Verdict | None


class ViewEntry(TypedDict):
    """A saved drawing for one view directive, keyed by its provider's hash too."""

    prefix_sha256: str
    fingerprint: str
    provider: str
    provider_sha256: str
    created: str
    drawing: Drawing


class FailureEntry(TypedDict):
    """A saved failed evidence or decision run: the kind of failure and its message."""

    prefix_sha256: str
    fingerprint: str
    provider: str
    created: str
    kind: str
    message: str


class Records(TypedDict):
    """A `<Notebook>.glitnir.json` file, as `well_formed` accepts it."""

    format: str
    source_sha256: str
    environment: Environment
    withheld: Withheld | None
    claims: list[ClaimRecord]
    non_claims: NotRequired[list[NonClaimRecord]]
    evidence: NotRequired[dict[str, EvidenceEntry]]
    decisions: NotRequired[dict[str, DecisionEntry]]
    views: NotRequired[dict[str, ViewEntry]]
    evidence_failures: NotRequired[dict[str, FailureEntry]]
    decision_failures: NotRequired[dict[str, FailureEntry]]


def matches(saved: Any, fingerprint: str, compare_environment: bool) -> bool:
    """Whether a result saved under `saved` may be current under `fingerprint`.

    A saved fingerprint that is not a digest is never current. Without
    `compare_environment` the result is compared as recorded: only its source
    is compared, with the environment it was saved in.
    """
    if not fingerprint_ok(saved):
        return False
    if not compare_environment:
        return True
    return fingerprint_ok(fingerprint) and saved == fingerprint


def _normal(text: str) -> str:
    return text.replace("\r\n", "\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _created_ok(created: str) -> bool:
    """Whether `created` is an ISO-8601 UTC time to the second."""
    if _CREATED.fullmatch(created) is None:
        return False
    try:
        datetime.fromisoformat(created)
    except ValueError:
        return False
    return True


def _evidence_entry_ok(entry: Any) -> bool:
    """Whether `entry` holds the saved evidence fields and a valid snapshot."""
    if not isinstance(entry, dict) or set(entry) != {*_EVIDENCE_KEYS, "snapshot"}:
        return False
    if not all(isinstance(entry[key], str) for key in _EVIDENCE_KEYS):
        return False
    if not _created_ok(entry["created"]):
        return False
    try:
        checked_evidence(entry["snapshot"])
    except ValueError:
        return False
    return True


def _failure_entry_ok(entry: Any, section: str = _FAILURES) -> bool:
    """Whether `entry` holds the saved fields of a failed run in `section`."""
    if not isinstance(entry, dict) or set(entry) != set(_FAILURE_KEYS):
        return False
    if not all(isinstance(entry[key], str) for key in _FAILURE_KEYS):
        return False
    kinds = EVIDENCE_FAILURE_KINDS if section == _FAILURES else DECISION_FAILURE_KINDS
    return (
        _created_ok(entry["created"])
        and entry["kind"] in kinds
        and len(entry["message"]) <= MAX_MESSAGE
    )


def _before(created: str, other: str) -> bool:
    """Whether the saved time `created` is strictly earlier than `other`."""
    return datetime.fromisoformat(created) < datetime.fromisoformat(other)


def lenient_failures(
    records: dict[str, Any] | None, section: str = _FAILURES
) -> dict[str, dict[str, Any]]:
    """Return the saved failures still worth showing; malformed ones are dropped.

    `section` is `evidence_failures` or `decision_failures`. A section that is
    not an object reads as empty. A failure saved before the evidence or
    decision saved for its directive is out of date and dropped too.
    """
    if records is None:
        return {}
    failures = records.get(section, {})
    if not isinstance(failures, dict):
        return {}
    succeeded, success_ok = (
        ("evidence", _evidence_entry_ok)
        if section == _FAILURES
        else ("decisions", _decision_entry_ok)
    )
    successes = records.get(succeeded, {})
    kept = {}
    for key, entry in failures.items():
        if not _failure_entry_ok(entry, section):
            continue
        saved = successes.get(key) if isinstance(successes, dict) else None
        if isinstance(saved, dict) and success_ok(saved):
            if _before(entry["created"], saved["created"]):
                continue
        kept[key] = dict(entry)
    return kept


def with_lenient_failures(records: dict[str, Any]) -> dict[str, Any]:
    """Return ``records`` with only the failures `lenient_failures` keeps."""
    for section in _FAILURE_SECTIONS:
        if section in records:
            failures = lenient_failures(records, section)
            records = {k: v for k, v in records.items() if k != section}
            if failures:
                records[section] = failures
    return records


def _view_entry_ok(entry: Any) -> bool:
    """Whether `entry` holds the saved drawing fields and a drawing as checked.

    The drawing must be what `checked_drawing` returns for it, so a drawing
    with an error, a label or any other field is not a saved drawing.
    """
    if not isinstance(entry, dict) or set(entry) != {*_VIEW_KEYS, "drawing"}:
        return False
    if not all(isinstance(entry[key], str) for key in _VIEW_KEYS):
        return False
    if not _created_ok(entry["created"]):
        return False
    drawing = entry["drawing"]
    if not isinstance(drawing, dict) or "error" in drawing:
        return False
    try:
        return checked_drawing(json.dumps(drawing)) == drawing
    except (TypeError, ValueError, RecursionError):
        return False


def _verification_ok(verification: Any, outcome: Decision) -> bool:
    """Whether `verification` is a candidate check's result, or None if unknown."""
    if outcome["status"] == "unknown":
        return verification is None
    if not isinstance(verification, dict) or set(verification) != _VERIFICATION_KEYS:
        return False
    message, axioms, kind = (
        verification["message"],
        verification["axioms"],
        verification["type"],
    )
    return (
        verification["status"] in ("checked", "rejected")
        and isinstance(message, str)
        and len(message) <= MAX_MESSAGE
        and isinstance(axioms, list)
        and all(isinstance(axiom, str) for axiom in axioms)
        and (kind is None or (isinstance(kind, str) and len(kind) <= MAX_TYPE))
    )


def _decision_entry_ok(entry: Any) -> bool:
    """Whether `entry` holds the saved decision fields in their normal form."""
    if not isinstance(entry, dict) or set(entry) != {
        *_EVIDENCE_KEYS,
        "outcome",
        "verification",
    }:
        return False
    if not all(isinstance(entry[key], str) for key in _EVIDENCE_KEYS):
        return False
    if not _created_ok(entry["created"]):
        return False
    try:
        outcome = checked_decision(entry["outcome"])
    except ValueError:
        return False
    return outcome == entry["outcome"] and _verification_ok(
        entry["verification"], outcome
    )


def _statement_ok(statement: Any) -> bool:
    """Whether ``statement`` is a saved checked statement."""
    return isinstance(statement, str) and len(statement) <= MAX_TYPE


def _uses_ok(uses: Any) -> bool:
    """Whether ``uses`` is a saved list of the claims a claim uses."""
    return (
        isinstance(uses, list)
        and len(uses) <= MAX_USES
        and all(
            isinstance(use, dict)
            and set(use) == _USE_KEYS
            and isinstance(use["module"], str)
            and isinstance(use["name"], str)
            and type(use["in_notebook"]) is bool
            for use in uses
        )
    )


def _shortened(statement: str, limit: int = MAX_TYPE) -> str:
    """Return ``statement`` within the saved length, marked when cut."""
    if len(statement) <= limit:
        return statement
    return statement[: limit - 1] + "…"


def _withheld_ok(withheld: Any) -> bool:
    """Whether ``withheld`` says where a saved check stopped, or is None."""
    if withheld is None:
        return True
    if not isinstance(withheld, dict) or not (
        _WITHHELD_KEYS <= set(withheld) <= {*_WITHHELD_KEYS, "through_line"}
    ):
        return False
    from_line = withheld["from_line"]
    if type(from_line) is not int or from_line < 1:
        return False
    if not isinstance(withheld["message"], str):
        return False
    if "through_line" not in withheld:
        return True
    # A check through line N stops there, or earlier at a failing command.
    through = withheld["through_line"]
    return type(through) is int and 1 <= through and from_line <= through + 1


def well_formed(records: dict[str, Any]) -> bool:
    """Check the shape that ``claim_views`` relies on; anything else is unknown."""
    environment = records.get("environment")
    if not isinstance(environment, dict) or not isinstance(
        environment.get("fingerprint"), str
    ):
        return False
    if not _withheld_ok(records.get("withheld")):
        return False
    claims = records.get("claims")
    if not isinstance(claims, list):
        return False
    for claim in claims:
        if not isinstance(claim, dict):
            return False
        for key, kind in _CLAIM_KEYS.items():
            if key not in claim or not isinstance(claim[key], kind):
                return False
        if "example" in claim and claim["example"] is not True:
            return False
        for key in ("statement", "kernel_statement"):
            if key in claim and not _statement_ok(claim[key]):
                return False
            if claim.get(f"{key}_shortened", True) is not True:
                return False
        if "uses" in claim and not _uses_ok(claim["uses"]):
            return False
        if claim.get("uses_truncated", True) is not True or (
            "uses_truncated" in claim and "uses" not in claim
        ):
            return False
        if "fingerprint" in claim and not isinstance(claim["fingerprint"], str):
            return False
    non_claims = records.get("non_claims", [])
    if not isinstance(non_claims, list) or not all(
        isinstance(item, dict)
        and set(item) == set(_NON_CLAIM_KEYS)
        and all(isinstance(item[key], kind) for key, kind in _NON_CLAIM_KEYS.items())
        for item in non_claims
    ):
        return False
    for section, entry_ok in (
        ("evidence", _evidence_entry_ok),
        ("decisions", _decision_entry_ok),
        ("views", _view_entry_ok),
    ):
        entries = records.get(section, {})
        if not isinstance(entries, dict):
            return False
        if not all(entry_ok(entry) for entry in entries.values()):
            return False
    return True
