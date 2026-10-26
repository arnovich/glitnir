"""Read Taskvalet's bounded source-evidence wire format without dependencies."""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from glitnir.compute.protocol import digest, validate_request
from glitnir.lean.environment import LEAN_AXIOM_ALLOWLIST
from glitnir.lean.project import read_regular
from glitnir.lean.project_inspection import TOOLCHAIN

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z")
ORIGIN_FIELDS = {
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


def canonical(value: dict[str, Any]) -> str:
    """Match the versioned UTF-8 wire digest, without changing source whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate keys, including inside source provenance."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in lemma bundle")
        result[key] = value
    return result


def load_bundle(path: Path) -> dict[str, Any]:
    """Read bounded untrusted JSON, never a URL or executable configuration."""
    try:
        return validate_bundle(
            json.loads(read_regular(path, 250_000), object_pairs_hook=unique_object)
        )
    except RecursionError as error:
        raise ValueError("Lemma bundle exceeds JSON nesting limits") from error


def validate_bundle(value: Any) -> dict[str, Any]:
    """Validate reported evidence; only a later native replay can accept a proof."""
    try:
        return _validate_bundle(value)
    except (KeyError, TypeError, OverflowError, RecursionError) as error:
        raise ValueError("Malformed Lean lemma bundle") from error


def _validate_bundle(value: Any) -> dict[str, Any]:
    """Validate the full wire payload under the CLI's ValueError boundary."""
    if not isinstance(value, dict) or set(value) != {
        "format",
        "origin",
        "job",
        "declarations",
        "sha256",
    }:
        raise ValueError("Invalid Lean lemma bundle fields")
    if (
        value["format"] != "taskvalet.lean-lemma/v1"
        or len(canonical(value).encode()) > 50_000
    ):
        raise ValueError("Unsupported or oversized Lean lemma bundle")
    payload = {k: v for k, v in value.items() if k != "sha256"}
    if digest(canonical(payload).encode()) != value["sha256"]:
        raise ValueError("Lemma bundle digest differs from its contents")
    origin = value["origin"]
    if not isinstance(origin, dict) or set(origin) != ORIGIN_FIELDS:
        raise ValueError("Invalid lemma origin")
    if any(
        not isinstance(v, str) or not v or len(v) > 50_000
        for k, v in origin.items()
        if k != "problem_version"
    ):
        raise ValueError("Invalid lemma origin values")
    if type(origin["problem_version"]) is not int or origin["problem_version"] < 1:
        raise ValueError("Invalid lemma problem version")
    for name in ("run_id", "problem", "assignment"):
        if str(UUID(origin[name])) != origin[name]:
            raise ValueError("Noncanonical lemma identity")
    url = urlsplit(origin["server"])
    if (
        not url.hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or (
            url.scheme != "https"
            and not (
                url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError("Invalid lemma server origin")
    if origin["actor"] != "agent/" + origin["run_id"]:
        raise ValueError("Invalid lemma author")
    job = value["job"]
    if (
        not isinstance(job, dict)
        or job.get("state") != "completed"
        or job.get("project") != origin["project"]
        or not isinstance(job.get("worker"), str)
        or not job["worker"]
    ):
        raise ValueError("Lemma requires an attributed completed job")
    request, report = job.get("request"), job.get("result")
    if not isinstance(request, dict) or not isinstance(report, dict):
        raise ValueError("Lemma requires source and a completed report")
    validate_request(request)
    if str(UUID(job["id"])) != job["id"] or job["id"] != request["request_id"]:
        raise ValueError("Lemma job identity differs from its request")
    if request.get("through_line") is not None or report.get("withheld") is not None:
        raise ValueError("Lemma requires a complete dedicated module")
    if (
        report.get("status") != "checked_snapshot"
        or report.get("source_sha256") != request["source_sha256"]
    ):
        raise ValueError("Lemma report differs from its source")
    if report.get("toolchain") != TOOLCHAIN:
        raise ValueError("Lemma toolchain must equal the supported toolchain")
    module = report.get("module", "")
    if (
        not isinstance(module, str)
        or not NAME.fullmatch(module)
        or request["path"] != module.replace(".", "/") + ".lean"
    ):
        raise ValueError("Lemma module must match its source-root path")
    names, theorems = value["declarations"], report.get("theorems")
    if (
        not isinstance(names, list)
        or not 1 <= len(names) <= 64
        or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in names)
        or len(set(names)) != len(names)
        or not isinstance(theorems, list)
    ):
        raise ValueError("Invalid lemma declaration selection")
    for name in names:
        found = [t for t in theorems if isinstance(t, dict) and t.get("name") == name]
        if len(found) != 1 or found[0].get("status") != "checked":
            raise ValueError("Expected exactly one checked lemma declaration")
        axioms = found[0].get("axioms")
        if not isinstance(axioms, list) or any(
            not isinstance(a, dict) or a.get("name") not in LEAN_AXIOM_ALLOWLIST
            for a in axioms
        ):
            raise ValueError("Lemma declaration uses unsupported axioms")
    return value
