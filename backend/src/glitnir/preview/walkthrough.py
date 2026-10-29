"""Read the paper-level map declared beside a walkthrough notebook."""

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, NotRequired, TypedDict
from urllib.parse import quote, urlsplit

from glitnir.lean.project import read_regular, toolchain_declaration
from glitnir.lean.project_inspection import (
    TOOLCHAIN,
    supported_version,
    toolchain_version,
)

FORMAT = "glitnir.walkthrough-outline/v1"
OUTLINE = "outline.json"
MAX_OUTLINE_BYTES = 512 * 1024
MAX_NODES = 240
MAX_USES = 240
MAX_LEAN_ENTRIES = 32
MAX_PROBLEMS = 80
MAX_TOOLCHAIN_CHARACTERS = 256
NODE_KINDS = frozenset(
    {
        "definition",
        "lemma",
        "proposition",
        "theorem",
        "corollary",
        "algorithm",
        "appendix",
    }
)
_COMMIT = re.compile(r"[0-9a-f]{7,40}")


class WalkthroughLean(TypedDict):
    """One Lean declaration named by a paper statement."""

    name: str
    file: str
    line: int
    note: str
    href: str


class WalkthroughNode(TypedDict):
    """One bounded statement from the walkthrough outline."""

    id: str
    label: str
    kind: str
    section: str
    pages: str
    statement: str
    idea: str
    uses: list[str]
    claim: str
    lean: list[WalkthroughLean]


class WalkthroughFormalization(TypedDict):
    """Whether the outlined formalization can use Glitnir's one Lean version."""

    supported: bool
    declared: str | None
    toolchain: str
    message: str | None


class WalkthroughMap(TypedDict):
    """The outline fields the local renderer needs."""

    format: str
    title: str
    overview: str
    paper: str
    paper_local: bool
    formalization: WalkthroughFormalization
    nodes: list[WalkthroughNode]
    problems: list[str]
    source: NotRequired[str]


def _text(value: Any, limit: int = 4096) -> str:
    """Return a bounded string, or the empty string for another JSON type."""
    return value[:limit] if isinstance(value, str) else ""


def _https(value: Any) -> str:
    """Return a bounded HTTPS URL that has a host."""
    target = _text(value, 4096)
    if not target or any(character.isspace() for character in target):
        return ""
    try:
        parsed = urlsplit(target)
    except ValueError:
        return ""
    return target if parsed.scheme == "https" and bool(parsed.hostname) else ""


def _lean_href(project: Any, entry: dict[str, Any]) -> str:
    """Build the outline's pinned public link for one Lean declaration."""
    if not isinstance(project, dict):
        return ""
    repository = _https(project.get("repository"))
    commit = _text(project.get("commit"), 40)
    file = _text(entry.get("file"), 1024)
    line = entry.get("line")
    path = _text(project.get("path"), 1024).strip("/")
    relative = PurePosixPath(file)
    if (
        not repository
        or not _COMMIT.fullmatch(commit)
        or not file
        or relative.is_absolute()
        or ".." in relative.parts
        or not isinstance(line, int)
        or isinstance(line, bool)
        or not 1 <= line <= 10_000_000
    ):
        return ""
    where = "/".join(part for part in (path, file) if part)
    location = quote(where, safe="/")
    return f"{repository.rstrip('/')}/blob/{commit}/{location}#L{line}"


def _paper(sources: Any) -> tuple[str, bool]:
    """Return the first paper source's public URL and local-copy flag."""
    if not isinstance(sources, list):
        return "", False
    for source in sources[:64]:
        if isinstance(source, dict) and source.get("kind") == "paper":
            return _https(source.get("url")), bool(_text(source.get("path"), 1024))
    return "", False


def _formalization(project: Any) -> WalkthroughFormalization:
    """Fail closed unless the outline declares Glitnir's supported Lean version."""
    raw = project.get("toolchain") if isinstance(project, dict) else None
    try:
        declared = (
            toolchain_declaration(raw)
            if isinstance(raw, str) and len(raw) <= MAX_TOOLCHAIN_CHARACTERS
            else None
        )
    except ValueError:
        declared = None
    if declared is None:
        return {
            "supported": False,
            "declared": None,
            "toolchain": TOOLCHAIN,
            "message": (
                "This walkthrough's formalization does not declare one valid Lean "
                f"toolchain. Glitnir checks Lean {supported_version()}, so its claims "
                "are unchecked."
            ),
        }
    version = toolchain_version(declared)
    supported = version == supported_version()
    return {
        "supported": supported,
        "declared": declared,
        "toolchain": TOOLCHAIN,
        "message": (
            None
            if supported
            else (
                f"This walkthrough's formalization uses Lean {version}. Glitnir "
                f"checks Lean {supported_version()}, so its claims are unchecked."
            )
        ),
    }


def _lean_entries(
    value: Any, project: Any, where: str, problems: list[str]
) -> list[WalkthroughLean]:
    """Validate and bound a node's Lean declaration list."""
    if not isinstance(value, list):
        problems.append(f"{where}.lean must be a list")
        return []
    entries: list[WalkthroughLean] = []
    if len(value) > MAX_LEAN_ENTRIES:
        problems.append(f"{where}.lean has more than {MAX_LEAN_ENTRIES} entries")
    for index, raw in enumerate(value[:MAX_LEAN_ENTRIES]):
        if not isinstance(raw, dict):
            problems.append(f"{where}.lean[{index}] must be an object")
            continue
        name, file, note = (
            _text(raw.get("name"), 512),
            _text(raw.get("file"), 1024),
            _text(raw.get("note"), 4096),
        )
        line = raw.get("line")
        if (
            not name
            or not file
            or not isinstance(line, int)
            or isinstance(line, bool)
            or not 1 <= line <= 10_000_000
        ):
            problems.append(f"{where}.lean[{index}] has no valid name, file and line")
            continue
        entries.append(
            {
                "name": name,
                "file": file,
                "line": line,
                "note": note,
                "href": _lean_href(project, raw),
            }
        )
    return entries


def _nodes(value: Any, project: Any, problems: list[str]) -> list[WalkthroughNode]:
    """Validate the statement list while retaining unknown references."""
    if not isinstance(value, list):
        problems.append("nodes must be a list")
        return []
    if len(value) > MAX_NODES:
        problems.append(f"nodes has more than {MAX_NODES} entries")
    nodes: list[WalkthroughNode] = []
    seen: set[str] = set()
    for index, raw in enumerate(value[:MAX_NODES]):
        where = f"nodes[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where} must be an object")
            continue
        identifier_value = raw.get("id")
        identifier = identifier_value if isinstance(identifier_value, str) else ""
        label = _text(raw.get("label"), 512)
        kind = _text(raw.get("kind"), 32)
        if not identifier or len(identifier) > 128 or identifier in seen:
            problems.append(f"{where} has no unique id")
            continue
        if not label:
            problems.append(f"{where} has no label")
            continue
        if kind not in NODE_KINDS:
            problems.append(f"{where} has unknown kind {kind or '(missing)'}")
            kind = "unknown"
        uses_value = raw.get("uses")
        if not isinstance(uses_value, list):
            problems.append(f"{where}.uses must be a list")
            uses_value = []
        if len(uses_value) > MAX_USES:
            problems.append(f"{where}.uses has more than {MAX_USES} entries")
        uses = [
            use
            for use in uses_value[:MAX_USES]
            if isinstance(use, str) and use and len(use) <= 128
        ]
        if len(uses) != len(uses_value[:MAX_USES]):
            problems.append(f"{where}.uses contains an invalid statement id")
        seen.add(identifier)
        nodes.append(
            {
                "id": identifier,
                "label": label,
                "kind": kind,
                "section": _text(raw.get("section"), 128),
                "pages": _text(raw.get("pages"), 128),
                "statement": _text(raw.get("statement"), 8192),
                "idea": _text(raw.get("idea"), 8192),
                "uses": uses,
                "claim": _text(raw.get("claim"), 512),
                "lean": _lean_entries(raw.get("lean"), project, where, problems),
            }
        )
    known = {node["id"] for node in nodes}
    for node in nodes:
        for reference in node["uses"]:
            if reference not in known:
                problems.append(f"{node['id']}: unknown statement {reference} in uses")
    return nodes


def _empty(*problems: str) -> WalkthroughMap:
    """Return a readable diagnostic in place of an unsafe outline."""
    return {
        "format": FORMAT,
        "title": "Walkthrough statement map",
        "overview": "",
        "paper": "",
        "paper_local": False,
        "formalization": _formalization(None),
        "nodes": [],
        "problems": list(problems)[:MAX_PROBLEMS],
        "source": OUTLINE,
    }


def load_walkthrough(path: Path) -> WalkthroughMap | None:
    """Read the outline beside `path`, without following or running anything."""
    outline = path.parent / OUTLINE
    try:
        data = read_regular(outline, MAX_OUTLINE_BYTES)
    except FileNotFoundError:
        return None
    except ValueError as error:
        message = (
            f"{OUTLINE} is too large to preview"
            if "too large" in str(error)
            else f"{OUTLINE} could not be read"
        )
        return _empty(message)
    except (OSError, PermissionError):
        return _empty(f"{OUTLINE} could not be read")
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _empty(f"{OUTLINE} is not valid UTF-8 JSON")
    if not isinstance(raw, dict):
        return _empty(f"{OUTLINE} must contain a JSON object")
    if raw.get("format") != FORMAT:
        return _empty(f"{OUTLINE} format must be {FORMAT}")
    problems: list[str] = []
    title = _text(raw.get("title"), 1024)
    if not title:
        problems.append("title must be a non-empty string")
        title = "Walkthrough statement map"
    project = raw.get("project")
    nodes = _nodes(raw.get("nodes"), project, problems)
    paper, paper_local = _paper(raw.get("sources"))
    return {
        "format": FORMAT,
        "title": title,
        "overview": _text(raw.get("overview"), 4096),
        "paper": paper,
        "paper_local": paper_local,
        "formalization": _formalization(project),
        "nodes": nodes,
        "problems": problems[:MAX_PROBLEMS],
        "source": OUTLINE,
    }
