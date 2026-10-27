"""The preview snapshot: what the renderer shows for one saved notebook.

`build_snapshot` is a function of the saved text, the saved records beside it,
a fingerprint of the environment and the service's `PreviewState`; it reads
files but starts no thread or process and writes nothing. The service copies
its state into a `PreviewState` and calls it; a static export or a linked
notebook calls it with an empty one.
"""

import hashlib
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, NotRequired, TypedDict

from glitnir.compute.client import remote_identity
from glitnir.lean.environment import MAX_SOURCE_BYTES
from glitnir.lean.fingerprint import (
    Fingerprint,
    FingerprintRecord,
    environment_fingerprint,
)
from glitnir.lean.lexing import code_tokens, scan
from glitnir.lean.prepared_packages import Resolution
from glitnir.lean.project import ProjectDescription, describe_project, read_regular
from glitnir.lean.project_inspection import LeanSupport, lean_support
from glitnir.lean.project_snapshot import SLOT_BUSY, SLOT_WAIT_TIMED_OUT
from glitnir.lean.results import NonClaim, StageMs, Theorem, Withheld
from glitnir.notebook.check_records import (
    ClaimView,
    NonClaimView,
    claim_views,
    load_records,
    non_claim_views,
)
from glitnir.notebook.directives import (
    ViewDirective,
    decision_directives,
    evidence_directives,
    view_directives,
)
from glitnir.notebook.document import (
    CellKind,
    NotebookError,
    parse_notebook,
    split_lines,
)
from glitnir.notebook.records_schema import Records
from glitnir.preview import assets
from glitnir.preview.assets import AssetStatus
from glitnir.preview.cards import (
    DecisionCard,
    EvidenceCard,
    decision_cards,
    environment_digest,
    evidence_cards,
    unavailable_words,
    view_cards,
    with_runs,
    with_saved_decisions,
    with_saved_evidence,
)
from glitnir.preview.linked import LEAN_NAME, sibling_references
from glitnir.providers import Providers

NOTICE = (
    "Saved-source preview. Check this file explicitly "
    "to observe its compiled propositions."
)


class Cell(TypedDict):
    """One cell of the saved text: prose with its markdown, or source; numbered."""

    kind: Literal["prose", "source"]
    start: int
    end: int
    text: str
    markdown: NotRequired[str]
    id: NotRequired[str]
    index: NotRequired[int]
    claim: NotRequired[str]


class CheckState(TypedDict):
    """What the service knows of its project check: running, unavailable, or the result.

    A finished check carries the result's own keys besides these, and
    `through_line` says how far the check covered the file.
    """

    status: Literal["running", "unavailable", "checked_snapshot"]
    source_sha256: str
    through_line: int | None
    message: str
    theorems: list[Theorem]
    environment_sha256: NotRequired[str]
    toolchain: NotRequired[str]
    module: NotRequired[str]
    non_claims: NotRequired[list[NonClaim]]
    snapshot_bytes: NotRequired[int]
    resolution: NotRequired[Resolution | None]
    replay: NotRequired[str]
    withheld: NotRequired[Withheld | None]
    stage_ms: NotRequired[StageMs]
    execution: NotRequired[dict[str, Any]]


class ResolutionSummary(TypedDict):
    """What the preview shows of a package resolution: its kind and packages."""

    kind: str | None
    packages: list[Any]


class InspectionSummary(TypedDict):
    """What the preview shows of an inspection; never the setup itself."""

    status: str
    source_sha256: str
    message: str
    resolution: NotRequired[ResolutionSummary]


class WithheldView(TypedDict):
    """Where the saved check stopped, flagged stale when saved for other text."""

    from_line: int
    message: str
    through_line: NotRequired[int]
    stale: NotRequired[Literal[True]]


class LinkedInfo(TypedDict):
    """How a linked notebook's snapshot was reached, and whether it was compared."""

    path: str
    file: str
    compared: bool


class NeutralProject(TypedDict):
    """A recorded snapshot's project: kind, root and toolchain, without local paths."""

    kind: Literal["lake", "loose"]
    root: str | None
    toolchain: str | None


class PreviewInfo(TypedDict):
    """The snapshot's header: mode, notice, the check and inspection, the project."""

    mode: str
    notice: str
    check: CheckState | None
    inspection: InspectionSummary | None
    project: ProjectDescription | NeutralProject
    lean: LeanSupport
    linked: NotRequired[LinkedInfo]
    recorded: NotRequired[Literal[True]]


class DirectiveProviders(TypedDict):
    """The evidence verbs and decision kinds installed providers offer."""

    evidence: list[str]
    decide: list[str]


class DirectiveUnavailable(TypedDict):
    """Of those, the words that cannot run yet, with what is missing."""

    evidence: dict[str, str]
    decide: dict[str, str]


class Snapshot(TypedDict):
    """What the renderer shows for one saved notebook (`GET /api/notebook`)."""

    file: str
    sha256: str
    preview: PreviewInfo
    cells: list[Cell]
    claims: list[Any]
    project_claims: list[ClaimView]
    project_non_claims: list[NonClaimView]
    project_withheld: WithheldView | None
    checking: list[Any]
    check_errors: dict[str, str]
    reading: bool
    views: list[ViewDirective]
    evidence: list[EvidenceCard]
    decisions: list[DecisionCard]
    directive_providers: DirectiveProviders
    directive_unavailable: DirectiveUnavailable
    links: list[Any]
    references: list[dict[str, str]]
    revisions: list[Any]
    assets: list[AssetStatus]


@dataclass(frozen=True)
class PreviewState:
    """What a running preview adds to its saved notebook: all empty by default."""

    check: CheckState | None = None
    inspection: InspectionSummary | None = None
    running: str | None = None
    run_errors: Mapping[str, str] = field(default_factory=dict)
    #: Keys whose last run error was also saved as the card's failure.
    saved_failures: frozenset[str] = frozenset()


def read_source(path: Path) -> str:
    """Read the saved notebook, with CRLF read as LF."""
    text = read_regular(path, MAX_SOURCE_BYTES).decode("utf-8")
    return text.replace("\r\n", "\n")


def _source_cell(start: int, end: int, lines: list[str]) -> Cell:
    return {
        "kind": "source",
        "start": start,
        "end": end,
        "text": "".join(lines[start - 1 : end]),
    }


def _split_at_claims(
    cells: list[Cell], claims: Sequence[ClaimView], lines: list[str]
) -> list[Cell]:
    """Give each claim's lines their own source cell, keeping all text."""
    result: list[Cell] = []
    for cell in cells:
        if cell["kind"] != "source":
            result.append(cell)
            continue
        cursor = cell["start"]
        for claim in claims:
            first, last = claim["start_line"], claim["end_line"]
            if first < cursor or last > cell["end"]:
                continue
            if first > cursor:
                result.append(_source_cell(cursor, first - 1, lines))
            result.append({**_source_cell(first, last, lines), "claim": claim["name"]})
            cursor = last + 1
        if cursor <= cell["end"]:
            result.append(_source_cell(cursor, cell["end"], lines))
    return result


def cut_short(message: str) -> bool:
    """Whether a check stopped before Lean said anything about the notebook."""
    return (
        message.rstrip().endswith("cancelled.")
        or message.startswith(SLOT_BUSY)
        or message.startswith(SLOT_WAIT_TIMED_OUT)
    )


def _failed_fresh_check(observation: CheckState | None, sha256: str) -> bool:
    """Whether the latest check of this exact text was unavailable, not cut short."""
    if observation is None or observation.get("status") != "unavailable":
        return False
    if observation.get("source_sha256") != sha256:
        return False
    return not cut_short(str(observation.get("message") or ""))


def _withheld(records: Records | None, sha256: str) -> WithheldView | None:
    """Return where the saved check stopped, flagged stale for other text."""
    withheld = records.get("withheld") if records else None
    if not isinstance(withheld, dict):
        return None
    if records is not None and records.get("source_sha256") != sha256:
        return {**withheld, "stale": True}
    return {**withheld}


def _module_link(directory: Path, module: str) -> str | None:
    """Return a module's `.lean` file relative to ``directory``, if it is there.

    Only a regular file reached through no symbolic link counts; the link is
    checked again when it is read (`read_linked_notebook`).
    """
    parts = module.split(".")
    if not all(LEAN_NAME.fullmatch(part) for part in parts):
        return None
    relative = [*parts[:-1], parts[-1] + ".lean"]
    current = directory
    try:
        for index, part in enumerate(relative):
            current = current / part
            mode = os.lstat(current).st_mode
            last = index == len(relative) - 1
            if not (stat.S_ISREG(mode) if last else stat.S_ISDIR(mode)):
                return None
    except OSError:
        return None
    return "/".join(relative)


def _with_use_links(claims: list[ClaimView], notebook: Path) -> None:
    """Link each use of another notebook's claim to that notebook, if readable.

    The link is relative to ``notebook``, as `/api/linked` reads it; a use
    without one is shown as text.
    """
    links: dict[str, str | None] = {}
    for claim in claims:
        for use in claim.get("uses", []):
            if use["in_notebook"]:
                continue
            if use["module"] not in links:
                links[use["module"]] = _module_link(notebook.parent, use["module"])
            link = links[use["module"]]
            if link is not None:
                use["path"] = link


def uncompared(snapshot: Snapshot) -> Snapshot:
    """Mark saved results that were not compared with any environment.

    Their status is the saved one; whether it is still current is unknown,
    so claims have no currency and saved evidence, failures and decisions read
    as uncompared rather than out of date.
    """
    for claim in snapshot["project_claims"]:
        claim["current"] = None
    for card in [*snapshot["evidence"], *snapshot["decisions"]]:
        failure = card.get("failure")
        if failure is not None:
            failure["status"] = "uncompared"
        if (
            card.get("snapshot") is not None
            or card.get("outcome") is not None
            or failure is not None
        ):
            if card["status"] != "running":
                card["status"] = "uncompared"
    return snapshot


def _lines(text: str) -> list[str]:
    """Split `text` at LF only, keeping each line's ending."""
    parts = text.split("\n")
    lines = [line + "\n" for line in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _confirmed_prose(lines: list[str], start: int, end: int) -> bool:
    """Whether Lean reads ``lines[start:end]`` as one comment, as the parser does.

    The source before the block must end outside any literal or comment, and
    the block must hold no code: nothing before it opens a string the block
    sits in, and the block's comment closes at its end. An unsettled scan
    confirms nothing.
    """
    if not scan("".join(lines[:start])).settled:
        return False
    return code_tokens("".join(lines[start:end])) == []


def cells(text: str) -> list[Cell]:
    """Split saved text into prose and source cells, keeping all of it."""
    lines = _lines(text)
    cells: list[Cell] = []
    try:
        notebook = parse_notebook(text)
        cursor = 0
        for cell in notebook.cells:
            start, end = cell.start_line - 1, cell.end_line
            # The line parser does not read strings. Keep the entire file raw
            # unless Lean's own tokenizer reads each prose block as one.
            if cell.kind is CellKind.PROSE and not _confirmed_prose(lines, start, end):
                raise NotebookError("unconfirmed lexical boundary")
            if start > cursor:
                cells.append(
                    {
                        "kind": "source",
                        "start": cursor + 1,
                        "end": start,
                        "text": "".join(lines[cursor:start]),
                    }
                )
            prose = cell.kind is CellKind.PROSE
            cells.append(
                {
                    "kind": "prose" if prose else "source",
                    "start": start + 1,
                    "end": end,
                    "text": "".join(lines[start:end]),
                    "markdown": (
                        cell.text.strip().removeprefix("/-!").removesuffix("-/").strip()
                        if prose
                        else ""
                    ),
                }
            )
            cursor = end
        if cursor < len(lines):
            cells.append(
                {
                    "kind": "source",
                    "start": cursor + 1,
                    "end": len(lines),
                    "text": "".join(lines[cursor:]),
                }
            )
    except NotebookError:
        cells = []
    if not cells and text:
        cells = [
            {
                "id": "cell-0",
                "index": 0,
                "kind": "source",
                "start": 1,
                "end": len(split_lines(text)),
                "text": text,
            }
        ]
    return cells


def prose(cells: list[Cell]) -> list[str]:
    """Return the markdown of each prose cell."""
    return [cell["markdown"] for cell in cells if cell["kind"] == "prose"]


def indexed_cells(text: str) -> list[Cell]:
    """Split `text` into cells numbered as the snapshot numbers them."""
    entries = cells(text)
    for index, entry in enumerate(entries):
        entry["id"] = f"cell-{index}"
        entry["index"] = index
    return entries


def expected_theorem(name: str, status: str) -> str:
    """Name the theorem a proven or refuted decision of `name` must declare."""
    return name if status == "proven" else f"{name}_refuted"


class Fingerprints:
    """The environment fingerprints of one saved text, each computed at most once.

    Called with the modules loaded besides the notebook's (a provider's),
    none by default, it returns the digest; `environment` returns the whole
    fingerprint. A failure is remembered too, so every later call raises it
    again without recomputing.
    """

    def __init__(self, path: Path, text: str) -> None:
        """Keep the notebook and its saved text; nothing is computed yet."""
        self._path, self._text = path, text
        self._found: dict[tuple[str, ...], FingerprintRecord] = {}
        self._failed: dict[tuple[str, ...], Exception] = {}

    def environment(self, imports: tuple[str, ...] = ()) -> FingerprintRecord:
        """Return the fingerprint taken with `imports`."""
        if imports in self._failed:
            raise self._failed[imports]
        if imports not in self._found:
            try:
                if imports:
                    # The dependencies' git states are the notebook's own.
                    packages = self.environment().get("packages")
                    found = environment_fingerprint(
                        self._path,
                        self._text,
                        imports,
                        packages,
                        remote=remote_identity(self._path),
                    )
                else:
                    found = environment_fingerprint(
                        self._path, self._text, remote=remote_identity(self._path)
                    )
                self._found[imports] = found
            except Exception as error:
                self._failed[imports] = error
                raise
        return self._found[imports]

    def __call__(self, imports: tuple[str, ...] = ()) -> str:
        """Return the digest of the fingerprint taken with `imports`."""
        return str(self.environment(imports)["sha256"])


def build_snapshot(
    path: Path,
    text: str,
    fingerprint: Fingerprint,
    state: PreviewState,
    providers: Callable[[], Providers],
    *,
    compare_environment: bool = True,
) -> Snapshot:
    """Build the snapshot of `text`; `fingerprint` is called only if needed.

    Without `compare_environment` (only `static_export.SavedNotebook`),
    `fingerprint` is never called: saved results are compared with their
    saved source only, as recorded. `providers` is called only when a
    directive needs them.
    """
    lines = _lines(text)
    entries = cells(text)
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    observation, inspection = state.check, state.inspection
    running, run_errors = state.running, dict(state.run_errors)
    saved_failures = state.saved_failures
    records = load_records(path)
    project_claims: list[ClaimView] = []
    project_non_claims: list[NonClaimView] = []
    if records is not None:
        try:
            # A failed fingerprint is '', which matches nothing: the saved
            # results are then shown, all out of date.
            environment = environment_digest(fingerprint) if compare_environment else ""
            project_claims = claim_views(
                text,
                records,
                environment,
                compare_environment=compare_environment,
            )
            project_non_claims = non_claim_views(
                text,
                records,
                environment,
                compare_environment=compare_environment,
            )
        except Exception:  # noqa: BLE001 - saved results must not break preview
            project_claims, project_non_claims = [], []
    _with_use_links(project_claims, path)
    if _failed_fresh_check(observation, sha256):
        # A fresh check of this exact file could not run, so no saved
        # result for it can be what a fresh check would report.
        for claim in project_claims:
            claim["current"] = False
        project_non_claims = []
    if project_claims:
        entries = _split_at_claims(entries, project_claims, lines)
    for index, entry in enumerate(entries):
        entry["id"] = f"cell-{index}"
        entry["index"] = index
    views = view_directives(entries, text)
    evidence = evidence_directives(entries, text)
    decisions = decision_directives(entries, text)
    # Providers load only when a directive needs them.
    loaded = providers() if views or evidence or decisions else None
    project = describe_project(path)
    return {
        "file": path.name,
        "sha256": sha256,
        "preview": {
            "mode": "project",
            "notice": NOTICE,
            "check": observation,
            "inspection": inspection,
            "project": project,
            "lean": lean_support(project),
        },
        "cells": entries,
        "claims": [],
        "project_claims": project_claims,
        "project_non_claims": project_non_claims,
        "project_withheld": _withheld(records, sha256),
        "checking": [],
        "check_errors": {},
        "reading": False,
        "views": [] if loaded is None else view_cards(views, loaded),
        "evidence": (
            []
            if loaded is None
            else with_runs(
                with_saved_evidence(
                    evidence_cards(evidence, loaded),
                    records,
                    text,
                    fingerprint,
                    loaded,
                    compare_environment,
                ),
                running,
                run_errors,
                saved_failures,
            )
        ),
        "decisions": (
            []
            if loaded is None
            else with_runs(
                with_saved_decisions(
                    decision_cards(decisions, loaded),
                    records,
                    text,
                    fingerprint,
                    compare_environment,
                ),
                running,
                run_errors,
                saved_failures,
            )
        ),
        "directive_providers": {
            "evidence": [] if loaded is None else sorted(loaded.evidence),
            "decide": [] if loaded is None else sorted(loaded.decisions),
        },
        # Of those, the words that cannot run yet, with what is missing.
        "directive_unavailable": {
            "evidence": unavailable_words(loaded, "evidence"),
            "decide": unavailable_words(loaded, "decide"),
        },
        "links": [],
        "references": sibling_references(path),
        "revisions": [],
        "assets": assets.statuses(path, assets.references(prose(entries))),
    }
