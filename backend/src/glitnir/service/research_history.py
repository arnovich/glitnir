"""Read a bounded Taskvalet investigation beside one exactly bound claim.

The adapter is observational. It accepts no Taskvalet write methods and never
derives proof authority from a campaign state, proposal or peer opinion. A
checked result is shown only when a Glitnir project check binds the exact
declaration, type, source, environment and configured axiom policy.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from glitnir.lean.project import read_regular
from glitnir.notebook.check_records import ClaimView, load_records
from glitnir.notebook.document import NotebookError
from glitnir.notebook.records_schema import ClaimRecord, Records

FORMAT = "glitnir.taskvalet-research-links/v1"
SUFFIX = ".research.json"
URL_ENV = "GLITNIR_TASKVALET_URL"
TOKEN_ENV = "GLITNIR_TASKVALET_TOKEN"
MAX_RESPONSE_BYTES = 2_000_000
MAX_ROUNDS = 24
MAX_PROPOSALS = 16
MAX_PARTICIPANTS = 32
MAX_EVIDENCE = 20
MAX_CHECKS = 32
MAX_ARTIFACTS = 32
MAX_TEXT = 50_000
MAX_LINKS = 32
MAX_LINK_BYTES = 1_000_000
MAX_REQUESTS = 48
MAX_REQUEST_SECONDS = 30.0
MAX_PROJECTED_BYTES = 8_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z")


@dataclass(frozen=True)
class ResearchLink:
    """One sidecar link from a local claim to immutable research context."""

    claim: str
    project: str
    problem: str
    problem_version: int
    declaration: str
    type: str
    context_sha256: str
    source_sha256: str
    environment: Mapping[str, str]
    axiom_policy: tuple[str, ...]
    local_checks: tuple[Mapping[str, str], ...] = ()
    independent_checks: tuple[Mapping[str, Any], ...] = ()
    stale_reasons: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        """Return the non-secret binding exposed in the notebook snapshot."""
        return asdict(self)


class ResearchHistoryError(ValueError):
    """A configured history cannot be read or trusted as shaped."""


def _reject_nonfinite(value: str) -> None:
    """Reject JavaScript-incompatible JSON constants from remote data."""
    raise ValueError(f"non-finite JSON constant {value}")


@dataclass
class _ReadBudget:
    """One investigation's bounded outbound request budget."""

    deadline: float
    requests: int = 0

    def remaining(self) -> float:
        return self.deadline - monotonic()

    def exhausted(self) -> bool:
        return self.requests >= MAX_REQUESTS or self.remaining() <= 0


def links_path(notebook: Path) -> Path:
    """Return the optional Taskvalet research sidecar beside ``notebook``."""
    return notebook.with_suffix(SUFFIX)


def research_links(
    notebook: Path,
    text: str,
    claims: list[ClaimView],
) -> dict[str, dict[str, Any]]:
    """Read strict links and bind them to this saved source and its checks.

    The sidecar is configuration only. It never changes the project-check
    records and malformed configuration is reported without hiding claims.
    """
    try:
        raw_document = json.loads(read_regular(links_path(notebook), MAX_LINK_BYTES))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise NotebookError(f"research links are unreadable: {error}") from error
    if (
        not isinstance(raw_document, dict)
        or set(raw_document) != {"format", "links"}
        or raw_document.get("format") != FORMAT
    ):
        raise NotebookError("malformed or unsupported research links sidecar")
    raw = raw_document["links"]
    if not isinstance(raw, list) or len(raw) > MAX_LINKS:
        raise NotebookError(f"research links must be a list of at most {MAX_LINKS}")
    source_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    views = {claim["name"]: claim for claim in claims}
    records = load_records(notebook)
    saved = _saved_claims(records)
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        link = _link(item)
        if link.claim in result:
            raise NotebookError(
                f"research claim {link.claim!r} is linked more than once"
            )
        claim = views.get(link.claim)
        if claim is None:
            raise NotebookError(f"research link names no claim {link.claim!r}")
        stale = []
        record = saved.get(link.claim)
        if record is None or record.get("type_sha256") != _sha256(link.type):
            stale.append("The claim type changed")
        if record is None or record["prefix_sha256"] != link.context_sha256:
            stale.append("The claim context changed")
        if link.source_sha256 != source_sha256:
            stale.append("The notebook source changed")
        configured = [
            {"claim": link.claim, "declaration": link.declaration, "type": link.type},
            *link.local_checks,
        ]
        mapped_local_checks = []
        independent = []
        for checked in configured:
            local = views.get(checked["claim"])
            local_record = saved.get(checked["claim"])
            if local is None or local_record is None:
                raise NotebookError(
                    f"research local check names no claim {checked['claim']!r}"
                )
            if _sha256(checked["type"]) != local_record.get("type_sha256"):
                if checked["claim"] != link.claim:
                    stale.append(f"The mapped claim type changed: {checked['claim']}")
                continue
            context_sha256 = local_record["prefix_sha256"]
            if checked["claim"] != link.claim:
                mapped_local_checks.append(
                    {
                        **checked,
                        "context_sha256": context_sha256,
                        "source_sha256": source_sha256,
                    }
                )
            external_axioms = {axiom["name"] for axiom in local_record["axioms"]}
            if (
                local["status"] == "checked"
                and local["current"] is True
                and external_axioms <= set(link.axiom_policy)
                and records is not None
                and link.environment.get("fingerprint")
                == records["environment"]["fingerprint"]
                and link.environment.get("check") == records["environment"]["check"]
            ):
                independent.append(
                    {
                        **checked,
                        "context_sha256": context_sha256,
                        "source_sha256": source_sha256,
                        "record": {
                            "status": local_record["status"],
                            "type_sha256": local_record["type_sha256"],
                            "prefix_sha256": local_record["prefix_sha256"],
                        },
                        "environment": {
                            "fingerprint": records["environment"]["fingerprint"],
                            "check": records["environment"]["check"],
                        },
                        "external_axioms": sorted(external_axioms),
                    }
                )
        link = ResearchLink(
            **{
                **asdict(link),
                "local_checks": tuple(mapped_local_checks),
                "independent_checks": tuple(independent),
                "stale_reasons": tuple(dict.fromkeys(stale)),
            }
        )
        result[link.claim] = link.public()
    return result


def _saved_claims(records: Records | None) -> dict[str, ClaimRecord]:
    """Return the newest saved record of each named claim."""
    if records is None:
        return {}
    return {record["name"]: record for record in records["claims"]}


def _link(value: Any) -> ResearchLink:
    fields = {
        "claim",
        "project",
        "problem",
        "problem_version",
        "declaration",
        "type",
        "context_sha256",
        "source_sha256",
        "environment",
        "axiom_policy",
    }
    allowed_fields = fields | {"local_checks"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(fields),
        frozenset(allowed_fields),
    }:
        raise NotebookError("malformed or unsupported research link")
    text = ("claim", "project", "problem", "declaration", "type")
    if any(
        not isinstance(value[key], str) or not value[key] or len(value[key]) > MAX_TEXT
        for key in text
    ):
        raise NotebookError("research link text fields must be non-empty strings")
    if "." not in value["declaration"] or not _NAME.fullmatch(value["declaration"]):
        raise NotebookError("research declaration must be fully qualified")
    if value["declaration"] != value["claim"]:
        raise NotebookError("research declaration must name the linked claim")
    if type(value["problem_version"]) is not int or value["problem_version"] < 1:
        raise NotebookError("research problem_version must be a positive integer")
    for key in ("context_sha256", "source_sha256"):
        if not isinstance(value[key], str) or not _SHA256.fullmatch(value[key]):
            raise NotebookError(f"research {key} must be a sha256 digest")
    environment = value["environment"]
    if (
        not isinstance(environment, dict)
        or not environment
        or len(environment) > 16
        or any(
            not isinstance(key, str)
            or not isinstance(item, str)
            or not key
            or not item
            or len(key) > 100
            or len(item) > 1000
            for key, item in environment.items()
        )
    ):
        raise NotebookError("research environment must be a small string mapping")
    if (
        not _SHA256.fullmatch(environment.get("fingerprint", ""))
        or not _SHA256.fullmatch(environment.get("check", ""))
        or not (set(environment) - {"fingerprint", "check"})
    ):
        raise NotebookError(
            "research environment must pin the local fingerprint and check, "
            "plus the worker environment"
        )
    policy = value["axiom_policy"]
    if (
        not isinstance(policy, list)
        or len(policy) > 64
        or any(
            not isinstance(name, str) or not _NAME.fullmatch(name) for name in policy
        )
        or len(set(policy)) != len(policy)
    ):
        raise NotebookError("research axiom_policy must contain unique Lean names")
    local_checks = value.get("local_checks", [])
    if (
        not isinstance(local_checks, list)
        or len(local_checks) > 31
        or any(
            not isinstance(item, dict)
            or set(item) != {"claim", "declaration", "type"}
            or any(
                not isinstance(item[key], str)
                or not item[key]
                or len(item[key]) > MAX_TEXT
                for key in ("claim", "declaration", "type")
            )
            or not _NAME.fullmatch(item["declaration"])
            or item["declaration"] != item["claim"]
            for item in local_checks
        )
    ):
        raise NotebookError("research local_checks must map qualified local claims")
    try:
        if str(UUID(value["problem"])) != value["problem"]:
            raise ValueError
    except ValueError as error:
        raise NotebookError("research problem must be a canonical UUID") from error
    return ResearchLink(
        claim=value["claim"],
        project=value["project"],
        problem=value["problem"],
        problem_version=value["problem_version"],
        declaration=value["declaration"],
        type=value["type"],
        context_sha256=value["context_sha256"],
        source_sha256=value["source_sha256"],
        environment=dict(environment),
        axiom_policy=tuple(policy),
        local_checks=tuple(dict(item) for item in local_checks),
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise HTTPError(
            req.full_url, code, "Taskvalet redirects are refused", headers, fp
        )


class TaskvaletReader:
    """Read the three owner-authorized Taskvalet investigation projections."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        get: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        """Configure an environment-backed or injected read transport."""
        self.base_url = (
            base_url if base_url is not None else os.environ.get(URL_ENV, "")
        ).rstrip("/")
        self.token = token if token is not None else os.environ.get(TOKEN_ENV, "")
        self._get = get
        self._configuration_error = ""
        if self.base_url:
            try:
                _endpoint(self.base_url)
            except ResearchHistoryError as error:
                self._configuration_error = str(error)
                self.base_url = ""

    def investigation(self, link: Mapping[str, Any]) -> dict[str, Any]:
        """Load and normalize one linked investigation without service writes."""
        binding = ResearchLink(
            claim=link["claim"],
            project=link["project"],
            problem=link["problem"],
            problem_version=link["problem_version"],
            declaration=link["declaration"],
            type=link["type"],
            context_sha256=link["context_sha256"],
            source_sha256=link["source_sha256"],
            environment=dict(link["environment"]),
            axiom_policy=tuple(link["axiom_policy"]),
            local_checks=tuple(link.get("local_checks", ())),
            independent_checks=tuple(link.get("independent_checks", ())),
            stale_reasons=tuple(link.get("stale_reasons", ())),
        )
        if self._configuration_error:
            return _unavailable(binding, self._configuration_error)
        if not self.base_url:
            return _unavailable(binding, f"Set {URL_ENV} to the Taskvalet service.")
        if not self.token and self._get is None:
            return _unavailable(binding, f"Set {TOKEN_ENV} to a read credential.")
        project = quote(binding.project, safe="")
        problem = quote(binding.problem, safe="")
        partial: list[str] = []
        budget = _ReadBudget(monotonic() + MAX_REQUEST_SECONDS)
        try:
            research = self._request(
                f"/api/v1/projects/{project}/research/{problem}", budget
            )
            query = urlencode({"problem": binding.problem})
            rounds_reply = self._request(
                f"/api/v1/projects/{project}/rounds?{query}", budget
            )
        except ResearchHistoryError as error:
            return _unavailable(binding, str(error), state="disconnected")
        rounds = rounds_reply.get("rounds")
        if not isinstance(rounds, list):
            return _unavailable(
                binding, "Taskvalet returned no readable round history."
            )
        readable_rounds = [
            item
            for item in rounds
            if isinstance(item, dict)
            and _dict(item.get("plan")).get("problem_version")
            == binding.problem_version
        ]
        proposal_rounds = sorted(
            readable_rounds,
            key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))),
        )[-MAX_ROUNDS:]
        ordered_round_ids = [
            str(item["id"])
            for item in proposal_rounds
            if isinstance(item.get("id"), str)
        ]
        round_ids = set(ordered_round_ids)
        proposals: dict[str, dict[str, Any]] = {}
        summaries: dict[str, dict[str, Any]] = {}
        listed_complete = False
        listed_available = True
        try:
            listed_value = self._request(
                f"/api/v1/projects/{project}/round-proposals", budget
            ).get("proposals", [])
            if not isinstance(listed_value, list):
                raise ResearchHistoryError(
                    "Taskvalet returned no readable proposal index"
                )
            listed_complete = len(listed_value) < 100
            for item in _list(listed_value, 100):
                if isinstance(item, dict) and item.get("source_round") in round_ids:
                    summaries.setdefault(str(item["source_round"]), item)
        except ResearchHistoryError:
            listed_available = False
            partial.append("Allocation proposal index is unavailable")
        if listed_available and not listed_complete:
            for source_round in ordered_round_ids:
                if source_round in summaries:
                    continue
                query = urlencode({"source_round": source_round})
                try:
                    listed_value = self._request(
                        f"/api/v1/projects/{project}/round-proposals?{query}", budget
                    ).get("proposals", [])
                    candidate = next(
                        (
                            item
                            for item in _list(listed_value, 100)
                            if isinstance(item, dict)
                            and item.get("source_round") == source_round
                        ),
                        None,
                    )
                    if candidate is not None:
                        summaries[source_round] = candidate
                except ResearchHistoryError:
                    partial.append(
                        f"Allocation proposal index for round {source_round} "
                        "is unavailable"
                    )
                    if budget.exhausted():
                        partial.append("Taskvalet read budget was exhausted")
                        break
        relevant = sorted(
            summaries.values(),
            key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))),
            reverse=True,
        )
        omitted = max(0, len(relevant) - MAX_PROPOSALS)
        if omitted:
            partial.append(
                f"{omitted} round allocation proposal"
                f"{'s are' if omitted != 1 else ' is'} outside this bounded view"
            )
        for item in relevant[:MAX_PROPOSALS]:
            identity = quote(str(item.get("id", "")), safe="")
            try:
                proposal = self._request(
                    f"/api/v1/projects/{project}/round-proposals/{identity}", budget
                )
                proposals[str(item["source_round"])] = proposal
            except ResearchHistoryError:
                partial.append(f"Proposal {item.get('id', '?')} is unavailable")
                if budget.exhausted():
                    partial.append("Taskvalet read budget was exhausted")
                    break
        return _project(binding, research, readable_rounds, proposals, partial)

    def _request(self, path: str, budget: _ReadBudget) -> dict[str, Any]:
        remaining = budget.remaining()
        if budget.requests >= MAX_REQUESTS or remaining <= 0:
            raise ResearchHistoryError("Taskvalet read budget was exhausted")
        budget.requests += 1
        if self._get is not None:
            value = self._get(path)
            if not isinstance(value, dict):
                raise ResearchHistoryError("Taskvalet returned a non-object response")
            return value
        request = Request(
            self.base_url + path,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with build_opener(_NoRedirect).open(
                request, timeout=max(0.05, min(5.0, remaining))
            ) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ResearchHistoryError(
                        "Taskvalet response exceeds the 2 MB limit"
                    )
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise ResearchHistoryError(f"Taskvalet is unavailable: {error}") from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise ResearchHistoryError("Taskvalet response exceeds the 2 MB limit")
        try:
            value = json.loads(body, parse_constant=_reject_nonfinite)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ResearchHistoryError("Taskvalet returned malformed JSON") from error
        if not isinstance(value, dict):
            raise ResearchHistoryError("Taskvalet returned a non-object response")
        return value


def _endpoint(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
        not in (
            "",
            "/",
        )
    ):
        raise ResearchHistoryError(
            "Taskvalet URL must be an origin without credentials"
        )
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname:
        try:
            if ipaddress.ip_address(parsed.hostname).is_loopback:
                return
        except ValueError:
            if parsed.hostname.lower() == "localhost":
                return
    raise ResearchHistoryError("Taskvalet URL must use HTTPS or loopback HTTP")


def _unavailable(
    binding: ResearchLink, reason: str, state: str = "unavailable"
) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "proof_authority": False,
        "binding": binding.public(),
        "rounds": [],
        "partial": [],
        "truncated_rounds": 0,
    }


def _dict(value: Any) -> dict[str, Any]:
    """Narrow an untrusted JSON value to an object."""
    return value if isinstance(value, dict) else {}


def _sha256(value: str) -> str:
    """Return the exact identity digest for an unshortened checked type."""
    return hashlib.sha256(value.encode()).hexdigest()


def _list(value: Any, limit: int) -> list[Any]:
    """Narrow and bound an untrusted JSON collection."""
    return value[:limit] if isinstance(value, list) else []


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    """Narrow and bound untrusted display text."""
    return value[:limit] if isinstance(value, str) else ""


def _optional_text(value: Any, limit: int) -> str | None:
    """Return bounded display text while preserving an absent remote field."""
    return value[:limit] if isinstance(value, str) else None


def _portable(value: Any, depth: int = 0) -> Any:
    """Project untrusted JSON to a finite, bounded, standards-compliant value."""
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _text(value)
    if depth >= 8:
        return None
    if isinstance(value, list):
        return [_portable(item, depth + 1) for item in value[:64]]
    if isinstance(value, dict):
        return {
            _text(key, 200): _portable(item, depth + 1)
            for key, item in list(value.items())[:64]
            if isinstance(key, str)
        }
    return None


def _finite_percent(value: Any) -> int | float | None:
    """Return a finite percentage only when it is numeric and in range."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value if 0 <= value <= 100 else None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate JSON keys in portable evidence."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence key")
        result[key] = value
    return result


def _project(
    binding: ResearchLink,
    research: dict[str, Any],
    rounds: list[Any],
    proposals: Mapping[str, dict[str, Any]],
    partial: list[str],
) -> dict[str, Any]:
    current_problem = research.get("problem")
    history = [
        item for item in _list(research.get("history"), 100) if isinstance(item, dict)
    ]
    problem = next(
        (
            item
            for item in history
            if item.get("id") == binding.problem
            and item.get("version") == binding.problem_version
        ),
        (
            current_problem
            if isinstance(current_problem, dict)
            and current_problem.get("id") == binding.problem
            and current_problem.get("version") == binding.problem_version
            else None
        ),
    )
    if not isinstance(problem, dict) or not isinstance(problem.get("data"), dict):
        return _unavailable(binding, "The linked Taskvalet problem is missing.")
    problem_data: dict[str, Any] = problem["data"]
    stale = list(binding.stale_reasons)
    if problem.get("id") != binding.problem:
        stale.append("Taskvalet returned another problem")
    if (
        isinstance(current_problem, dict)
        and current_problem.get("version") != binding.problem_version
    ):
        stale.append("The Taskvalet problem has a newer version")
    assignments: dict[str, dict[str, Any]] = {
        item["id"]: item
        for item in _list(research.get("assignments"), 100)
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("problem_version") == binding.problem_version
    }
    readable = [
        item
        for item in rounds
        if isinstance(item, dict)
        and _dict(item.get("plan")).get("problem_version") == binding.problem_version
    ]
    readable.sort(
        key=lambda item: (str(item.get("created_at", "")), str(item.get("id", "")))
    )
    truncated = max(0, len(readable) - MAX_ROUNDS)
    readable = readable[-MAX_ROUNDS:]
    latest_versions: dict[str, int] = {}
    superseded_ids: set[str] = set()
    findings = research.get("findings")
    verifications = research.get("verifications")
    for record in [
        *(findings if isinstance(findings, list) else []),
        *(verifications if isinstance(verifications, list) else []),
    ]:
        if (
            isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and type(record.get("version")) is int
        ):
            latest_versions[record["id"]] = max(
                latest_versions.get(record["id"], 0), record["version"]
            )
            superseded = _dict(record.get("data")).get("supersedes")
            if isinstance(superseded, str):
                superseded_ids.add(superseded)
    projected = [
        _round(
            binding,
            round_,
            proposals.get(str(round_.get("id"))),
            assignments,
            problem_data,
            latest_versions,
            superseded_ids,
        )
        for round_ in readable
    ]
    result: dict[str, Any] = {
        "state": "ready",
        "reason": "; ".join(stale),
        "proof_authority": False,
        "binding": binding.public(),
        "problem": {
            "id": problem.get("id"),
            "version": problem.get("version"),
            "title": _text(problem_data.get("title")),
            "statement": _text(problem_data.get("statement")),
            "assumptions": _text(problem_data.get("assumptions")),
        },
        "rounds": projected,
        "partial": partial,
        "truncated_rounds": truncated,
    }
    dropped_for_size = 0
    while (
        projected
        and len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        > MAX_PROJECTED_BYTES
    ):
        projected.pop(0)
        dropped_for_size += 1
        result["truncated_rounds"] += 1
    if dropped_for_size:
        partial.append(
            f"{dropped_for_size} older round"
            f"{'s are' if dropped_for_size != 1 else ' is'} outside the 8 MB view"
        )
    if (
        len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        > MAX_PROJECTED_BYTES
    ):
        return _unavailable(
            binding,
            "The bounded Taskvalet history could not fit in the 8 MB view.",
        )
    result["state"] = (
        "stale"
        if stale
        else "partial" if partial or result["truncated_rounds"] else "ready"
    )
    return result


def _round(
    binding: ResearchLink,
    value: dict[str, Any],
    proposal_record: dict[str, Any] | None,
    assignments: Mapping[str, dict[str, Any]],
    problem: Mapping[str, Any],
    latest_versions: Mapping[str, int],
    superseded_ids: set[str],
) -> dict[str, Any]:
    plan = _dict(value.get("plan"))
    handoff = _dict(value.get("handoff"))
    campaign = _dict(value.get("campaign"))
    proposal = _dict(proposal_record.get("proposal") if proposal_record else None)
    participants = []
    for participant in _list(campaign.get("participants"), MAX_PARTICIPANTS):
        if not isinstance(participant, dict):
            continue
        assignment_id = participant.get("assignment")
        assignment = (
            assignments.get(assignment_id, {}) if isinstance(assignment_id, str) else {}
        )
        assignment_data = _dict(assignment.get("data"))
        participants.append(
            {
                key: _portable(participant.get(key))
                for key in (
                    "role",
                    "task",
                    "worker",
                    "assignment",
                    "status",
                    "findings",
                )
            }
            | {"assignment_title": _text(assignment_data.get("title", ""), 2_000)}
        )
    evidence = [
        _evidence(binding, item, problem, latest_versions, superseded_ids)
        for item in _list(handoff.get("context_inputs"), MAX_EVIDENCE)
        if isinstance(item, dict)
    ]
    return {
        "id": _text(value.get("id"), 200),
        "created_at": _text(value.get("created_at"), 100),
        "status": _text(value.get("status"), 100),
        "approach": _text(plan.get("approach")),
        "instructions": _text(plan.get("instructions")),
        "parent_round": _text(plan.get("parent_round"), 200),
        "participants": participants,
        "evidence": evidence,
        "decision": {
            "action": _text(proposal.get("action"), 100),
            "assessment": _text(proposal.get("assessment"), 100),
            "rationale": _text(proposal.get("rationale")),
            "obstacles": [
                _text(item, 2_000)
                for item in _list(proposal.get("obstacles"), MAX_EVIDENCE)
                if isinstance(item, str)
            ],
            "uncertainty": _text(proposal.get("uncertainty")),
            "allocations": [
                _portable(item)
                for item in _list(proposal.get("allocations"), MAX_PARTICIPANTS)
                if isinstance(item, dict)
            ],
            "checker": _portable(_dict(proposal.get("checker"))) or None,
            "exploration_percent": _finite_percent(proposal.get("exploration_percent")),
        },
    }


def _evidence(
    binding: ResearchLink,
    item: dict[str, Any],
    problem: Mapping[str, Any],
    latest_versions: Mapping[str, int],
    superseded_ids: set[str],
) -> dict[str, Any]:
    record = _dict(item.get("record"))
    finding = _dict(item.get("finding")) or record
    checks = [
        check
        for check in _list(item.get("checks"), MAX_CHECKS)
        if isinstance(check, dict)
    ]
    if record.get("kind") == "verification":
        category, reason, receipt = _classify_verification(record)
    else:
        category, reason, receipt = _classify(binding, item, finding, checks, problem)
    identities = (
        (record.get("id"), record.get("version")),
        (finding.get("id"), finding.get("version")),
    )
    superseded = any(
        isinstance(identity, str)
        and (
            identity in superseded_ids
            or (
                type(version) is int
                and latest_versions.get(identity, version) > version
            )
        )
        for identity, version in identities
    )
    if superseded:
        category, reason = (
            "stale_or_unavailable",
            "This selected evidence or its underlying finding was superseded.",
        )
    originating_assignment = _dict(item.get("originating_assignment"))
    selected_assignment = _dict(item.get("assignment")) or originating_assignment
    finding_data = _dict(finding.get("data"))
    return {
        "id": record.get("id"),
        "version": record.get("version"),
        "category": category,
        "reason": reason,
        "selected_reason": _text(item.get("reason"), 2_000),
        "summary": _text(finding_data.get("text")),
        "author": _text(record.get("author"), 200),
        "at": _text(record.get("at"), 100),
        "assignment": _portable(selected_assignment),
        "task": _text(_dict(selected_assignment.get("data")).get("task"), 200),
        "originating_task": _text(item.get("originating_task"), 200),
        "originating_finding": {
            "id": finding.get("id"),
            "version": finding.get("version"),
            "author": _text(finding.get("author"), 200),
            "at": _text(finding.get("at"), 100),
            "assignment": _portable(originating_assignment),
            "task": _text(item.get("originating_task"), 200),
        },
        "artifacts": [
            _portable(artifact)
            for artifact in _list(item.get("artifacts"), MAX_ARTIFACTS)
            if isinstance(artifact, dict)
        ],
        "checks": [_portable(check) for check in checks],
        "receipt": _portable(receipt),
        "superseded": superseded,
        "record": _portable(record),
        "finding": _portable(finding),
        "consumer_checks": [
            {
                "id": _text(check.get("id"), 200),
                "assignment": _optional_text(
                    _dict(check.get("data")).get("assignment"), 200
                ),
                "method": _optional_text(_dict(check.get("data")).get("method"), 100),
                "verdict": _optional_text(_dict(check.get("data")).get("verdict"), 100),
                "text": _text(_dict(check.get("data")).get("text", ""), 2_000),
            }
            for check in checks
        ],
    }


def _classify_verification(record: Mapping[str, Any]) -> tuple[str, str, None]:
    """Classify the selected verification itself, not its source finding."""
    data = _dict(record.get("data"))
    if data.get("verdict") == "refutes":
        return "refutation", "A selected peer verification reports a refutation.", None
    if data.get("method") == "numerical":
        return (
            "numerical_evidence",
            "A selected numerical verification does not establish the claim.",
            None,
        )
    if data.get("verdict") == "inconclusive":
        return "inconclusive", "The selected peer verification was inconclusive.", None
    return (
        "unchecked_candidate",
        "This is an attributed peer verification, not an independent replay.",
        None,
    )


def _classify(
    binding: ResearchLink,
    item: Mapping[str, Any],
    finding: dict[str, Any],
    checks: list[dict[str, Any]],
    problem: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any] | None]:
    data = _dict(finding.get("data"))
    kind = data.get("kind")
    check_data = [_dict(check.get("data")) for check in checks]
    if kind == "counterexample" or any(
        data.get("verdict") == "refutes" for data in check_data
    ):
        return "refutation", "An attributed record reports a refutation.", None
    if kind == "dead_end":
        return (
            "failed_approach",
            "The producing team recorded this approach as a dead end.",
            None,
        )
    if any(data.get("method") == "numerical" for data in check_data):
        verdicts = {data.get("verdict") for data in check_data}
        label = (
            "Numerical evidence is inconclusive."
            if "inconclusive" in verdicts
            else "Numerical evidence does not establish the claim."
        )
        return "numerical_evidence", label, None
    if kind != "proof_candidate":
        return (
            "unchecked_candidate",
            "This is attributed evidence, not an independent check.",
            None,
        )
    try:
        bundle = json.loads(
            _text(data.get("text")),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
        if not isinstance(bundle, dict):
            raise ValueError("evidence is not an object")
        job = bundle["job"]
        origin = bundle["origin"]
        if not isinstance(job, dict) or not isinstance(origin, dict):
            raise ValueError("evidence provenance is not an object")
        request, report = job["request"], job["result"]
        declarations = bundle.get("declarations", [])
        if not isinstance(request, dict) or not isinstance(report, dict):
            raise ValueError("evidence check is incomplete")
        if (
            not isinstance(declarations, list)
            or not declarations
            or any(not isinstance(name, str) for name in declarations)
        ):
            raise ValueError("evidence declarations are missing")
        target = binding.declaration in declarations
        selected_name = binding.declaration if target else declarations[0]
        theorems = report.get("theorems")
        source = request.get("source")
        job_id = job.get("id")
        if (
            not isinstance(theorems, list)
            or any(not isinstance(theorem, dict) for theorem in theorems)
            or not isinstance(source, str)
            or not isinstance(job_id, str)
        ):
            raise ValueError("evidence check fields are malformed")
        theorem = next(
            theorem for theorem in theorems if theorem.get("name") == selected_name
        )
        axioms = theorem["axioms"]
        if not isinstance(axioms, list) or any(
            not isinstance(axiom, (str, dict))
            or (isinstance(axiom, dict) and not isinstance(axiom.get("name"), str))
            for axiom in axioms
        ):
            raise ValueError("evidence axioms are malformed")
        names = {
            axiom["name"] if isinstance(axiom, dict) else axiom for axiom in axioms
        }
        local_binding = (
            {
                "type": binding.type,
                "context_sha256": binding.context_sha256,
                "source_sha256": binding.source_sha256,
            }
            if target
            else next(
                (
                    check
                    for check in binding.local_checks
                    if check.get("declaration") == selected_name
                ),
                None,
            )
        )
        exact_local_binding = (
            local_binding is not None
            and theorem.get("type") == local_binding.get("type")
            and request.get("source_sha256") == local_binding.get("source_sha256")
            and request.get("context_sha256") == local_binding.get("context_sha256")
        )
        expected_environment = {
            key: value
            for key, value in binding.environment.items()
            if key not in {"fingerprint", "check"}
        }
        environment_ok = all(
            str(report.get(key, request.get(key, ""))) == expected
            for key, expected in expected_environment.items()
        )
        origin_fields = {
            "server",
            "project",
            "storage_id",
            "actor",
            "run_id",
            "task",
            "problem",
            "problem_version",
            "assignment",
            "statement",
            "assumptions",
        }
        digest = hashlib.sha256(
            json.dumps(
                {key: value for key, value in bundle.items() if key != "sha256"},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        assignment = _dict(item.get("originating_assignment"))
        valid_report = (
            not binding.stale_reasons
            and set(bundle) == {"format", "origin", "job", "declarations", "sha256"}
            and bundle.get("format") == "taskvalet.lean-lemma/v1"
            and set(origin) == origin_fields
            and origin.get("project") == binding.project
            and origin.get("problem") == binding.problem
            and origin.get("problem_version") == binding.problem_version
            and origin.get("statement") == problem.get("statement")
            and origin.get("assumptions") == problem.get("assumptions")
            and origin.get("actor") == finding.get("author")
            and origin.get("assignment") == data.get("assignment")
            and origin.get("assignment") == assignment.get("id")
            and origin.get("task") == item.get("originating_task")
            and bundle.get("sha256") == digest
            and job.get("project") == binding.project
            and job.get("state") == "completed"
            and isinstance(job.get("worker"), str)
            and job.get("worker") != origin.get("actor")
            and str(UUID(job_id)) == job_id
            and request.get("kind") == "glitnir.check/v1"
            and request.get("request_id") == job.get("id")
            and request.get("through_line") is None
            and report.get("status") == "checked_snapshot"
            and report.get("withheld") is None
            and report.get("source_sha256") == request.get("source_sha256")
            and hashlib.sha256(source.encode()).hexdigest()
            == request.get("source_sha256")
            and selected_name in declarations
            and theorem.get("status") == "checked"
            and isinstance(theorem.get("type"), str)
            and exact_local_binding
            and names <= set(binding.axiom_policy)
            and environment_ok
        )
        receipt = {
            "job": job,
            "declaration": theorem,
            "source_sha256": request.get("source_sha256"),
            "environment": expected_environment,
            "axiom_policy": list(binding.axiom_policy),
            "independent_replay": False,
        }
        if valid_report:
            independent = next(
                (
                    check
                    for check in binding.independent_checks
                    if not binding.stale_reasons
                    and check.get("declaration") == selected_name
                    and check.get("type") == theorem.get("type")
                    and check.get("source_sha256") == request.get("source_sha256")
                    and request.get("source_sha256") == binding.source_sha256
                    and _dict(check.get("record")).get("status") == "checked"
                    and _dict(check.get("record")).get("type_sha256")
                    == _sha256(theorem["type"])
                    and _dict(check.get("record")).get("prefix_sha256")
                    == check.get("context_sha256")
                    and check.get("context_sha256") == request.get("context_sha256")
                    and isinstance(check.get("external_axioms"), list)
                    and set(check["external_axioms"]) <= set(binding.axiom_policy)
                    and _dict(check.get("environment")).get("fingerprint")
                    == binding.environment.get("fingerprint")
                    and _dict(check.get("environment")).get("check")
                    == binding.environment.get("check")
                    and (
                        not target
                        or check.get("context_sha256") == binding.context_sha256
                    )
                ),
                None,
            )
            if independent is not None:
                receipt["independent_replay"] = True
                receipt["glitnir_check"] = independent
                if target:
                    return (
                        "checked_target",
                        "A current Glitnir replay checked this exact saved source "
                        "and target declaration.",
                        receipt,
                    )
                return (
                    "checked_helper",
                    "A current Glitnir replay checked this exact saved source "
                    "and helper declaration.",
                    receipt,
                )
            if target:
                return (
                    "reported_target_check",
                    "Taskvalet preserved a complete worker-reported target check; "
                    "Glitnir has not independently replayed it.",
                    receipt,
                )
            return (
                "reported_helper_check",
                "Taskvalet preserved a complete worker-reported helper check; "
                "Glitnir has not independently replayed it.",
                receipt,
            )
        return (
            "stale_or_unavailable",
            "The check receipt does not match the linked declaration, type, "
            "source, environment and axiom policy.",
            receipt,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        StopIteration,
        json.JSONDecodeError,
        RecursionError,
    ):
        for data in check_data:
            if data.get("verdict") == "inconclusive":
                return "inconclusive", "The attributed check was inconclusive.", None
        return (
            "unchecked_candidate",
            "No exact independent replay receipt is available.",
            None,
        )
