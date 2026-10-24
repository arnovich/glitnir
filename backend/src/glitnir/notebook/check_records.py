"""Saved results of explicit project checks, kept beside the notebook.

A claim's saved result is current while the environment fingerprint and the
exact source text up to the end of the claim are unchanged, and the claim's
command still ends at its last line (`CommandBoundaries.ends_at`). Any edit above a
claim, prose and blank lines included, marks it out of date, because Lean code
can read module-doc prose and line positions. Edits below a claim leave it
current, unless they continue its command (a proof step, `where`,
`termination_by`): then it is out of date, and shown through the continuation.

A checked `example` is a claim too, marked `"example": true`, with the
statement the checker checked: Lean discards an example, so Glitnir checks it
as a private theorem, and that theorem's statement is what the claim's status
is about. `"statement"` is that statement as Lean pretty-prints it in the
example's own namespace and open declarations; `"kernel_statement"` is its
kernel form, without notation, as the independent reporter prints it. Both are
display only. Its name is the checker's (it moves with the example's position),
so like every claim it is found again by its exact text; identical examples are
matched in order. Examples without a proposition claim nothing; they are saved
under `non_claims` so the preview can say so.

Each claim saves the claims it uses (`"uses"`): the theorems its type and value
name directly, each `{"module", "name", "in_notebook"}`, from this notebook or
another notebook module of the project. They are display only, as the checker
reports them, and never affect a status. Records without them (format `v1`)
recorded no uses, which is not the same as using nothing.

A check may be asked to check only the file's lines 1 through N (a check
through one claim). It saves `"withheld"` like a check that stopped at a
failing command, from line N + 1, with `"through_line": N`. Claims from there on
were not checked by it, so it keeps each one's saved result from the earlier
records it replaces, found again by its exact text and unchanged, with the
`"fingerprint"` of the environment that result was checked in: such a claim is
current only while that fingerprint and its source through its last line still
match, as for any claim, and the preview marks it not rechecked. A later check
of the whole file replaces every result.

Format `v1` records are still read: they are upgraded on loading to the current
format, and the one statement they saved for an example, the kernel form,
becomes its `"kernel_statement"`.

Saved evidence follows the same rule, keyed by its directive line: it is
current while the environment fingerprint and the exact source text up to and
including the directive are unchanged.

Saved decisions are keyed the same way, through the directive's last line. A
decision whose checked block follows its directive in the text is accepted,
whatever changed above it: the block is then part of the notebook, and the
project check judges it like any other claim.

Saved drawings (the optional `"views"` section, `{key: entry}`) are keyed like
evidence, by the view directive's line text and its index among identical
directive lines. Each entry is `{"prefix_sha256", "fingerprint", "provider",
"provider_sha256", "created", "drawing"}`: `prefix_sha256` hashes the source
through the directive's line, `fingerprint` is the environment digest taken
with the provider's modules before drawing (a drawing made in an unknown
environment is never saved), and `provider_sha256` hashes the provider's
imports, preamble, marker and the directive's command
(`glitnir.lean.project_views.view_provider_sha256`). `drawing` is the drawing
as `checked_drawing` returns it, without the `uncertain` and `proofs_skipped`
labels; an entry is well formed only if `checked_drawing` returns that
drawing unchanged. A saved drawing is current while its source, environment
and provider hash all match (`view_status`). The section is capped at
`MAX_VIEWS_BYTES`; a drawing over it is shown but not saved. Saved drawings
are a cache: when a check, evidence or a decision would make the records too
large to load again, the whole section is dropped (the views are drawn again
when asked for) before anything is refused. A drawing grants or changes no
status. Records without the section (written by 0.1.2) read as holding no
drawings.

Saved failed evidence runs (the optional `"evidence_failures"` section,
`{key: entry}`) are keyed like evidence. Each entry is `{"prefix_sha256",
"fingerprint", "provider", "created", "kind", "message"}`, with the first four
as for evidence (a failure in an unknown environment is never saved), `kind`
one of `EVIDENCE_FAILURE_KINDS` and `message` at most `MAX_MESSAGE` characters.
A failure follows the evidence currency rule (`evidence_status`). A success
removes the failure saved for its directive; a failure saved after a success
keeps both. The section is read leniently: an entry that is malformed, or of a
kind this version does not know (written by a later one), is dropped as the
file is read, a malformed section reads as empty, and a failure saved before
the evidence saved for its directive is ignored, so each is gone at the next
write. Neither makes the file unreadable. Evidence entries and the format are
unchanged by it, because Glitnir 0.1.2 to 0.1.6 refuse the whole file on any
other format or evidence entry, but ignore top-level sections they do not know:
they read these records with their claims and evidence. A check by one of them
drops the section, and a success saved by one of them leaves an older failure
in place, which is then ignored. When saving would make the records too large,
the failures are dropped after the drawings, before anything is refused.
"""

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal

from glitnir.lean.digest import fingerprint_ok
from glitnir.lean.lexing import command_boundaries
from glitnir.lean.project import read_regular
from glitnir.lean.project_checking import MAX_USES
from glitnir.notebook.decision_cards import checked_decision
from glitnir.notebook.drawings import checked_drawing
from glitnir.notebook.evidence_cards import checked_evidence

FORMAT = "glitnir.project-checks/v2"
#: The earlier format, read and upgraded to `FORMAT` on loading.
FORMAT_V1 = "glitnir.project-checks/v1"
SUFFIX = ".glitnir.json"
_MAX_RECORDS = 16 * 1024 * 1024
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
RESULTS_TOO_LARGE = (
    "the results file would exceed 16 MiB; nothing saved before was dropped"
)
# Saved results that a check carries over but neither makes nor judges.
_RESULT_SECTIONS = ("evidence", "decisions", "views", "evidence_failures")
_NON_CLAIM_KEYS: dict[str, type] = {
    "start_line": int,
    "end_line": int,
    "declaration_sha256": str,
    "prefix_sha256": str,
}
_EVIDENCE_KEYS = ("prefix_sha256", "fingerprint", "provider", "created")
_VIEW_KEYS = (*_EVIDENCE_KEYS, "provider_sha256")
_FAILURES = "evidence_failures"
_FAILURE_KEYS = (*_EVIDENCE_KEYS, "kind", "message")
#: What made a saved evidence run fail: Lean's own error for the directive, a
#: provider crash, the provider's refusal, output that is not evidence, or the
#: run's own time limit.
EVIDENCE_FAILURE_KINDS = frozenset(
    {"lean", "provider", "refused", "invalid", "timeout"}
)
#: The largest the saved drawings may be together, serialised as saved.
MAX_VIEWS_BYTES = 4 * 1024 * 1024
_VERIFICATION_KEYS = frozenset({"status", "message", "axioms", "type"})
_USE_KEYS = frozenset({"module", "name", "in_notebook"})
# When a run saved its result: UTC to the second, as runs write it ("+00:00")
# or with the equivalent "Z".
_CREATED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+00:00|Z)")
MAX_MESSAGE = 2000
# A checked statement's kernel form; a run saves a longer one shortened.
MAX_TYPE = 20_000
DecisionStatus = Literal["accepted", "stale", "unknown", "rejected", "candidate"]
UNKNOWN_FINGERPRINT = (
    "the environment fingerprint is unknown, so no result can be saved for it"
)


def _matches(saved: Any, fingerprint: str, compare_environment: bool) -> bool:
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


def _failure_entry_ok(entry: Any) -> bool:
    """Whether `entry` holds the saved fields of a failed evidence run."""
    if not isinstance(entry, dict) or set(entry) != set(_FAILURE_KEYS):
        return False
    if not all(isinstance(entry[key], str) for key in _FAILURE_KEYS):
        return False
    return (
        _created_ok(entry["created"])
        and entry["kind"] in EVIDENCE_FAILURE_KINDS
        and len(entry["message"]) <= MAX_MESSAGE
    )


def _before(created: str, other: str) -> bool:
    """Whether the saved time `created` is strictly earlier than `other`."""
    return datetime.fromisoformat(created) < datetime.fromisoformat(other)


def _lenient_failures(records: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return the saved failures still worth showing; malformed ones are dropped.

    A section that is not an object reads as empty. A failure saved before the
    evidence saved for its directive is out of date and dropped too.
    """
    if records is None:
        return {}
    failures = records.get(_FAILURES, {})
    if not isinstance(failures, dict):
        return {}
    evidence = records.get("evidence", {})
    kept = {}
    for key, entry in failures.items():
        if not _failure_entry_ok(entry):
            continue
        saved = evidence.get(key) if isinstance(evidence, dict) else None
        if isinstance(saved, dict) and _evidence_entry_ok(saved):
            if _before(entry["created"], saved["created"]):
                continue
        kept[key] = dict(entry)
    return kept


def _with_lenient_failures(records: dict[str, Any]) -> dict[str, Any]:
    """Return ``records`` with only the failures `_lenient_failures` keeps."""
    if _FAILURES not in records:
        return records
    failures = _lenient_failures(records)
    result = {k: v for k, v in records.items() if k != _FAILURES}
    if failures:
        result[_FAILURES] = failures
    return result


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


def _verification_ok(verification: Any, outcome: dict[str, Any]) -> bool:
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


def failure_message(message: str) -> str:
    """Return a failed run's message within `MAX_MESSAGE`, marked "…" when cut."""
    return _shortened(message, MAX_MESSAGE)


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


def _well_formed(records: dict[str, Any]) -> bool:
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


def records_path(notebook: Path) -> Path:
    """Return the sidecar path for a notebook."""
    return notebook.with_suffix(SUFFIX)


def load_records(notebook: Path) -> dict[str, Any] | None:
    """Read saved records, or None when missing, unreadable or another format."""
    try:
        loaded = json.loads(read_regular(records_path(notebook), _MAX_RECORDS))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(loaded, dict):
        return None
    records: dict[str, Any] | None = loaded
    if loaded.get("format") == FORMAT_V1:
        records = _upgraded(loaded)
    if records is None or records.get("format") != FORMAT:
        return None
    if not _well_formed(records):
        return None
    return _with_lenient_failures(records)


def _upgraded(records: dict[str, Any]) -> dict[str, Any] | None:
    """Return `v1` records in the current format, or None when malformed.

    A `v1` example's statement is the kernel form, so it is kept as such.
    """
    claims = records.get("claims")
    if not isinstance(claims, list) or not all(isinstance(c, dict) for c in claims):
        return None
    if any(
        key in claim
        for claim in claims
        for key in (
            "kernel_statement",
            "kernel_statement_shortened",
            "uses",
            "uses_truncated",
            "fingerprint",
        )
    ):
        return None
    withheld = records.get("withheld")
    if isinstance(withheld, dict) and "through_line" in withheld:
        return None
    upgraded = []
    for claim in claims:
        claim = dict(claim)
        if "statement" in claim:
            claim["kernel_statement"] = claim.pop("statement")
        if "statement_shortened" in claim:
            claim["kernel_statement_shortened"] = claim.pop("statement_shortened")
        upgraded.append(claim)
    return {**records, "format": FORMAT, "claims": upgraded}


def _serialised(records: dict[str, Any]) -> str:
    """Return the exact text ``save_records`` writes, so size checks match."""
    return json.dumps(records, indent=2, sort_keys=True) + "\n"


def _too_large(records: dict[str, Any]) -> bool:
    """Whether ``records`` would be too large to load again once saved."""
    return len(_serialised(records).encode("utf-8")) > _MAX_RECORDS


def _fitted(records: dict[str, Any]) -> dict[str, Any]:
    """Return ``records``, without saved drawings and failures if too large.

    Drawings are a cache, drawn again when asked for, so they never stop what
    a check, evidence or a decision found from being saved. Saved failures
    only say that a run failed, so they go next.
    """
    for section in ("views", _FAILURES):
        if section in records and _too_large(records):
            records = {k: v for k, v in records.items() if k != section}
    return records


def save_records(notebook: Path, records: dict[str, Any]) -> None:
    """Write records atomically next to the notebook."""
    target = records_path(notebook)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w") as stream:
            # mkstemp creates 0600; the sidecar is shared like the notebook.
            os.fchmod(stream.fileno(), 0o644)
            stream.write(_serialised(records))
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def records_from_check(
    text: str,
    result: dict[str, Any],
    fingerprint: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build records from a completed check of ``text``.

    Saved evidence, decisions, drawings and failed evidence runs in the
    ``previous`` records are carried over unchanged: a check neither makes nor
    judges them. The saved drawings, then the saved failures, are dropped
    instead when they would make the records too large.
    Raises:
        ValueError: ``fingerprint`` is not an environment digest (see
            `fingerprint_ok`), or the records, with the saved evidence and
            decisions they keep, would be too large to load again even without
            the drawings and failures. Nothing is then saved, so nothing saved
            before is lost.
    """
    if not fingerprint_ok(fingerprint):
        raise ValueError(UNKNOWN_FINGERPRINT)
    lines = _normal(text).split("\n")
    claims = []
    for theorem in result["theorems"]:
        first, last = theorem["start_line"], theorem["end_line"]
        claim = {
            "name": theorem["name"],
            "status": theorem["status"],
            "axioms": theorem["axioms"],
            "start_line": first,
            "end_line": last,
            "declaration_sha256": _sha("\n".join(lines[first - 1 : last])),
            "prefix_sha256": _sha("\n".join(lines[:last])),
            "proposition_sha256": theorem["proposition_sha256"],
        }
        if isinstance(theorem.get("uses"), list):
            claim["uses"] = [dict(use) for use in theorem["uses"][:MAX_USES]]
            if theorem.get("uses_truncated") is True or (
                len(theorem["uses"]) > MAX_USES
            ):
                claim["uses_truncated"] = True
        if theorem.get("example") is True:
            # Its name is the checker's, and moves with the example's offset;
            # the claim is found again by its text like any other.
            claim["example"] = True
            statements = (
                ("statement", theorem.get("statement")),
                ("kernel_statement", theorem.get("type")),
            )
            for key, statement in statements:
                if isinstance(statement, str):
                    claim[key] = _shortened(statement)
                    if len(statement) > MAX_TYPE:
                        claim[f"{key}_shortened"] = True
        claims.append(claim)
    non_claims = [
        {
            "start_line": item["start_line"],
            "end_line": item["end_line"],
            "declaration_sha256": _sha(
                "\n".join(lines[item["start_line"] - 1 : item["end_line"]])
            ),
            "prefix_sha256": _sha("\n".join(lines[: item["end_line"]])),
        }
        for item in result.get("non_claims", [])
    ]
    withheld = result.get("withheld")
    if isinstance(withheld, dict) and "through_line" in withheld and previous:
        # A check through a chosen line checked nothing from `from_line` on:
        # what was saved for there is kept, never made current by this check.
        claims += _not_rechecked(previous, lines, withheld["from_line"])
        if previous["environment"]["fingerprint"] == fingerprint:
            # Shown only while the whole records' fingerprint matches.
            non_claims += [
                item
                for item, first, _ in _located(previous.get("non_claims", []), lines)
                if first >= withheld["from_line"]
            ]
    records: dict[str, Any] = {
        "format": FORMAT,
        "source_sha256": result["source_sha256"],
        "environment": {
            "fingerprint": fingerprint,
            "check": result["environment_sha256"],
        },
        "withheld": result.get("withheld"),
        "claims": claims,
    }
    if isinstance(result.get("execution"), dict):
        records["environment"]["execution"] = dict(result["execution"])
    if non_claims:
        records["non_claims"] = non_claims
    kept = dict(records)
    for section in _RESULT_SECTIONS:
        saved = (
            _lenient_failures(previous)
            if section == _FAILURES
            else _saved(previous, section)
        )
        if saved:
            kept[section] = saved
    kept = _fitted(kept)
    if _too_large(kept):
        raise ValueError(RESULTS_TOO_LARGE)
    return kept


def _not_rechecked(
    previous: dict[str, Any], lines: list[str], from_line: int
) -> list[dict[str, Any]]:
    """Return the saved claims from ``from_line`` on, as they were saved.

    Each keeps its result and hashes unchanged, at its place in ``lines``, and
    the fingerprint of the environment it was checked in.
    """
    environment = previous["environment"]["fingerprint"]
    kept = []
    for claim, first, last in _located(previous["claims"], lines):
        if first >= from_line:
            kept.append(
                {
                    **claim,
                    "start_line": first,
                    "end_line": last,
                    "fingerprint": claim.get("fingerprint", environment),
                }
            )
    return kept


def _located(
    items: list[dict[str, Any]], lines: list[str]
) -> list[tuple[dict[str, Any], int, int]]:
    """Find saved items by their exact text, in order: (item, first, last line)."""
    found = []
    searched_from = 0
    for item in sorted(items, key=lambda c: c["start_line"]):
        length = item["end_line"] - item["start_line"] + 1
        for start in range(searched_from, len(lines) - length + 1):
            block = "\n".join(lines[start : start + length])
            if _sha(block) == item["declaration_sha256"]:
                searched_from = start + length
                found.append((item, start + 1, start + length))
                break
    return found


def claim_views(
    text: str,
    records: dict[str, Any] | None,
    fingerprint: str,
    *,
    compare_environment: bool = True,
) -> list[dict[str, Any]]:
    """Locate saved claims in the current text and mark each current or stale.

    A claim saved under a fingerprint that is not a digest is never current.
    Without ``compare_environment``, each claim is compared only with its
    saved source, in the environment it was recorded in: it is shown as
    recorded (`glitnir.notebook.static_export`) and ``fingerprint`` is unused.
    """
    if records is None:
        return []
    normal = _normal(text)
    lines = normal.split("\n")
    boundaries = command_boundaries(normal)
    views = []
    located = _located(records["claims"], lines)
    for position, (claim, first, last) in enumerate(located):
        # A claim the last check did not recheck keeps its own environment.
        checked_in = claim.get("fingerprint", records["environment"]["fingerprint"])
        # Saved for a command that ended at `last`; one Lean now reads on
        # past it (a proof continued below) is another command.
        ends = boundaries.ends_at(last)
        current = (
            ends
            and _matches(checked_in, fingerprint, compare_environment)
            and _sha("\n".join(lines[:last])) == claim["prefix_sha256"]
        )
        if not ends:
            # Shown through what continues it, never into the next claim.
            following = [later for _, later, _ in located[position + 1 :]]
            limit = following[0] - 1 if following else len(lines)
            last = max(last, min(boundaries.end_from(last) or last, limit))
        view = {
            "name": claim["name"],
            "start_line": first,
            "end_line": last,
            "status": claim["status"],
            "axioms": claim["axioms"],
            "current": current,
        }
        if "uses" in claim:
            view["uses"] = [dict(use) for use in claim["uses"]]
            if claim.get("uses_truncated") is True:
                view["uses_truncated"] = True
        if "fingerprint" in claim:
            view["not_rechecked"] = True
        if claim.get("example") is True:
            view["example"] = True
            for key in ("statement", "kernel_statement"):
                if key in claim:
                    view[key] = claim[key]
                if claim.get(f"{key}_shortened") is True:
                    view[f"{key}_shortened"] = True
        views.append(view)
    return views


def non_claim_views(
    text: str,
    records: dict[str, Any] | None,
    fingerprint: str,
    *,
    compare_environment: bool = True,
) -> list[dict[str, int]]:
    """Locate the saved examples that claim nothing, while still current.

    ``compare_environment`` is as in `claim_views`.
    """
    if records is None or not _matches(
        records["environment"]["fingerprint"], fingerprint, compare_environment
    ):
        return []
    normal = _normal(text)
    lines = normal.split("\n")
    boundaries = command_boundaries(normal)
    return [
        {"start_line": first, "end_line": last}
        for item, first, last in _located(records.get("non_claims", []), lines)
        if _sha("\n".join(lines[:last])) == item["prefix_sha256"]
        and boundaries.ends_at(last)
    ]


def _saved(records: dict[str, Any] | None, section: str) -> dict[str, dict[str, Any]]:
    if records is None:
        return {}
    return dict(records.get(section, {}))


def saved_evidence(records: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return the saved evidence by directive key; none when there are no records."""
    return _saved(records, "evidence")


def saved_decisions(records: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return the saved decisions by directive key; none when there are no records."""
    return _saved(records, "decisions")


def saved_views(records: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return the saved drawings by directive key; none when there are no records."""
    return _saved(records, "views")


def saved_evidence_failures(
    records: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return the saved failed evidence runs by directive key, read leniently.

    Malformed failures, failures of kinds this version does not know and
    failures saved before their directive's evidence are left out.
    """
    return _lenient_failures(records)


def _refuse_unreadable(
    records: dict[str, Any] | None, notebook: Path | None, action: str
) -> None:
    """Refuse to replace a records file that exists but could not be read."""
    if records is None and notebook is not None:
        target = records_path(notebook)
        if os.path.lexists(target):
            raise ValueError(
                f"{target.name} exists but cannot be read; fix or remove it "
                f"before {action}."
            )


def _with_entry(
    records: dict[str, Any] | None,
    section: str,
    key: str,
    entry: dict[str, Any],
    keep: Iterable[str],
    notebook: Path | None,
    advice: str,
) -> dict[str, Any]:
    """Save ``entry`` under ``key`` in ``section``, pruning keys not kept.

    Saving anything but a drawing drops the saved drawings, then the saved
    failures, when they would make the records too large; saving a drawing
    never does. Failures that `_lenient_failures` drops are dropped here too.
    """
    _refuse_unreadable(records, notebook, f"saving {section}")
    wanted = {*keep, key}
    if records is not None:
        records = _with_lenient_failures(records)
    entries = {k: v for k, v in _saved(records, section).items() if k in wanted}
    # A copy, so later changes to the caller's entry never reach the records.
    entries[key] = copy.deepcopy(entry)
    if records is None:
        records = {
            "format": FORMAT,
            "source_sha256": "",
            "environment": {"fingerprint": entry["fingerprint"], "check": ""},
            "withheld": None,
            "claims": [],
        }
    result = {**records, section: entries}
    if section != "views":
        result = _fitted(result)
    if _too_large(result):
        name = (
            records_path(notebook).name if notebook is not None else "the records file"
        )
        raise ValueError(
            f"Saved {section} would make {name} larger than 16 MiB; {advice}."
        )
    return result


def with_evidence(
    records: dict[str, Any] | None,
    key: str,
    entry: dict[str, Any],
    keep: Iterable[str],
    notebook: Path | None = None,
) -> dict[str, Any]:
    """Return ``records`` with ``entry`` saved under ``key``.

    Evidence for keys that are neither ``key`` nor in ``keep`` (the keys of the
    directives now in the file) is dropped. Without records, a file holding
    only evidence is started, unless ``notebook`` is given and its records file
    already exists (then ``records`` is None only because that file could not
    be read, and starting afresh would overwrite it).

    Raises:
        ValueError: ``entry`` is not well-formed saved evidence, an unreadable
            records file would be overwritten, or the result would be too large
            to load again even without the saved drawings.
    """
    if not _evidence_entry_ok(entry):
        raise ValueError("The evidence entry is malformed.")
    if not fingerprint_ok(entry["fingerprint"]):
        raise ValueError(f"The evidence cannot be saved: {UNKNOWN_FINGERPRINT}.")
    keep = tuple(keep)
    if records is not None and _FAILURES in records:
        # This success replaces the directive's failure; gone directives
        # lose theirs.
        wanted = set(keep) - {key}
        failures = {k: v for k, v in _lenient_failures(records).items() if k in wanted}
        records = {k: v for k, v in records.items() if k != _FAILURES}
        if failures:
            records[_FAILURES] = failures
    return _with_entry(
        records,
        "evidence",
        key,
        entry,
        keep,
        notebook,
        "remove some evidence directives or shorten their runs",
    )


def with_evidence_failure(
    records: dict[str, Any] | None,
    key: str,
    entry: dict[str, Any],
    keep: Iterable[str],
    notebook: Path | None = None,
) -> dict[str, Any]:
    """Return ``records`` with the failed run ``entry`` saved under ``key``.

    Like ``with_evidence``: failures for keys that are neither ``key`` nor in
    ``keep`` are dropped, and an unreadable records file is never replaced.
    The evidence saved for ``key``, if any, is kept: it is still valid for its
    own source and environment.

    Raises:
        ValueError: ``entry`` is not a well-formed saved failure (its message
            within `MAX_MESSAGE`, see `failure_message`), its fingerprint is
            not an environment digest, an unreadable records file would be
            overwritten, or the failure does not fit in the records even
            without the saved drawings: it is then not saved.
    """
    if not _failure_entry_ok(entry):
        raise ValueError("The evidence failure entry is malformed.")
    if not fingerprint_ok(entry["fingerprint"]):
        raise ValueError(f"The failure cannot be saved: {UNKNOWN_FINGERPRINT}.")
    result = _with_entry(
        records,
        _FAILURES,
        key,
        entry,
        keep,
        notebook,
        "the failure is shown but not saved",
    )
    if _FAILURES not in result:
        raise ValueError(
            "Saved evidence failures would make the records file too large; "
            "the failure is shown but not saved."
        )
    return result


def with_decision(
    records: dict[str, Any] | None,
    key: str,
    entry: dict[str, Any],
    keep: Iterable[str],
    notebook: Path | None = None,
) -> dict[str, Any]:
    """Return ``records`` with the decision ``entry`` saved under ``key``.

    Like ``with_evidence``: decisions for keys that are neither ``key`` nor in
    ``keep`` are dropped, and an unreadable records file is never replaced.

    Raises:
        ValueError: ``entry`` is not a well-formed saved decision (its outcome
            in the form ``checked_decision`` returns), an unreadable records
            file would be overwritten, or the result would be too large to load
            again even without the saved drawings.
    """
    if not _decision_entry_ok(entry):
        raise ValueError("The decision entry is malformed.")
    if not fingerprint_ok(entry["fingerprint"]):
        raise ValueError(f"The decision cannot be saved: {UNKNOWN_FINGERPRINT}.")
    return _with_entry(
        records,
        "decisions",
        key,
        entry,
        keep,
        notebook,
        "remove some decision directives or discard their results",
    )


def with_view(
    records: dict[str, Any] | None,
    key: str,
    entry: dict[str, Any],
    keep: Iterable[str],
    notebook: Path | None = None,
) -> dict[str, Any]:
    """Return ``records`` with the drawing ``entry`` saved under ``key``.

    Like ``with_evidence``: drawings for keys that are neither ``key`` nor in
    ``keep`` are dropped, and an unreadable records file is never replaced.

    Raises:
        ValueError: ``entry`` is not a well-formed saved drawing, its
            fingerprint is not an environment digest, an unreadable records
            file would be overwritten, or the saved drawings would exceed
            `MAX_VIEWS_BYTES` or the result be too large to load again.
    """
    if not _view_entry_ok(entry):
        raise ValueError("The drawing entry is malformed.")
    if not fingerprint_ok(entry["fingerprint"]):
        raise ValueError(f"The drawing cannot be saved: {UNKNOWN_FINGERPRINT}.")
    result = _with_entry(
        records,
        "views",
        key,
        entry,
        keep,
        notebook,
        "remove some view directives",
    )
    if len(_serialised(result["views"]).encode("utf-8")) > MAX_VIEWS_BYTES:
        name = (
            records_path(notebook).name if notebook is not None else "the records file"
        )
        raise ValueError(
            f"Saved drawings would take more than 4 MiB of {name}; "
            "this drawing is shown but not saved."
        )
    return result


def without_decision(
    records: dict[str, Any] | None, key: str, notebook: Path | None = None
) -> dict[str, Any]:
    """Return ``records`` without the decision saved under ``key``.

    Raises:
        ValueError: an unreadable records file would be overwritten, or no
            decision is saved under ``key``.
    """
    _refuse_unreadable(records, notebook, "discarding a decision")
    decisions = saved_decisions(records)
    if records is None or key not in decisions:
        raise ValueError("No decision is saved for this directive.")
    del decisions[key]
    result = {k: v for k, v in records.items() if k != "decisions"}
    if decisions:
        result["decisions"] = decisions
    return result


def evidence_prefix_sha256(text: str, line: int) -> str:
    """Hash the source up to and including the one-based ``line``."""
    return _sha("\n".join(_normal(text).split("\n")[:line]))


def decision_prefix_sha256(text: str, end_line: int) -> str:
    """Hash the source through a decision directive's last line, as for evidence."""
    return evidence_prefix_sha256(text, end_line)


def evidence_status(
    entry: dict[str, Any],
    text: str,
    line: int,
    fingerprint: str,
    *,
    compare_environment: bool = True,
) -> Literal["current", "stale"]:
    """Say whether saved evidence still describes the directive at ``line``.

    ``compare_environment`` is as in `claim_views`.
    """
    if _matches(entry["fingerprint"], fingerprint, compare_environment) and entry[
        "prefix_sha256"
    ] == evidence_prefix_sha256(text, line):
        return "current"
    return "stale"


def view_status(
    entry: dict[str, Any],
    text: str,
    line: int,
    fingerprint: str,
    provider_sha256: str | None,
    *,
    compare_environment: bool = True,
) -> Literal["current", "stale"]:
    """Say whether a saved drawing still draws the view directive at ``line``.

    It is current while the source through the directive, the environment and
    the provider's hash (``provider_sha256``, None when the provider could not
    be hashed) all match. ``compare_environment`` is as in `claim_views`.
    """
    if (
        provider_sha256 is not None
        and entry["provider_sha256"] == provider_sha256
        and evidence_status(
            entry, text, line, fingerprint, compare_environment=compare_environment
        )
        == "current"
    ):
        return "current"
    return "stale"


def block_follows(text: str, end_line: int, block: str) -> bool:
    """Whether the lines right after the one-based ``end_line`` are ``block``'s."""
    lines = _normal(text).split("\n")
    wanted = block.split("\n")
    return lines[end_line : end_line + len(wanted)] == wanted


def decision_status(
    entry: dict[str, Any],
    text: str,
    end_line: int,
    fingerprint: str,
    *,
    compare_environment: bool = True,
) -> DecisionStatus:
    """Say what a saved decision means for the directive ending at ``end_line``.

    A checked block that follows the directive is accepted before anything
    else is compared. Otherwise any change to the environment or to the text
    through the directive makes the decision stale; ``compare_environment``
    is as in `claim_views`.
    """
    outcome, verification = entry["outcome"], entry["verification"]
    if (
        verification is not None
        and verification["status"] == "checked"
        and block_follows(text, end_line, outcome["block"])
    ):
        return "accepted"
    if not _matches(entry["fingerprint"], fingerprint, compare_environment) or entry[
        "prefix_sha256"
    ] != decision_prefix_sha256(text, end_line):
        return "stale"
    if outcome["status"] == "unknown":
        return "unknown"
    if verification["status"] == "rejected":
        return "rejected"
    return "candidate"
