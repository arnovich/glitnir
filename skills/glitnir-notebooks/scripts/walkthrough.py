"""Check a proof-walkthrough outline.

A walkthrough explains an existing Lean formalization through an
`outline.json`: the statements of a paper, their dependencies and the Lean
declarations that formalize them. This helper verifies such an outline.

Run it with the standard library only, without writing bytecode:

    python3 -B walkthrough.py check OUTLINE [--notebook FILE]

Exit status: 0 when no problem is an error, 1 when any is, 2 for an
unreadable outline or a usage error.
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

FORMAT = "glitnir.walkthrough-outline/v1"
NODE_KINDS = (
    "definition",
    "lemma",
    "proposition",
    "theorem",
    "corollary",
    "algorithm",
    "appendix",
)
SOURCE_KINDS = ("paper", "index", "readme", "note", "other")

_TOP_KEYS = ("format", "title", "project", "sources", "nodes")
_PROJECT_KEYS = ("root", "commit", "toolchain", "trusted")
_NODE_KEYS = ("id", "label", "kind", "statement", "idea", "uses", "lean")
_LEAN_KEYS = ("name", "file", "line", "type")


@dataclass(frozen=True)
class Problem:
    """One finding about an outline: an error or an informational note."""

    level: Literal["error", "note"]
    message: str


class OutlineError(ValueError):
    """Raised when an outline cannot be read as a JSON object."""


def load_outline(path: Path) -> dict[str, Any]:
    """Read the outline file, raising OutlineError if it is not a JSON object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise OutlineError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise OutlineError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise OutlineError(f"{path} must contain a JSON object")
    return data


def _error(message: str) -> Problem:
    """Return an error problem."""
    return Problem("error", message)


def _missing(where: str, item: dict[str, Any], keys: tuple[str, ...]) -> list[Problem]:
    """Report each required key absent from the mapping."""
    return [_error(f"{where}: missing key {key}") for key in keys if key not in item]


def _check_project(project: Any) -> list[Problem]:
    """Check the `project` object."""
    if not isinstance(project, dict):
        return [_error("project: must be an object")]
    problems = _missing("project", project, _PROJECT_KEYS)
    commit = project.get("commit")
    if "commit" in project and not (
        isinstance(commit, str) and _COMMIT_RE.fullmatch(commit)
    ):
        problems.append(
            _error("project commit must be 7 to 40 lowercase hex characters")
        )
    return problems


def _check_sources(sources: Any) -> list[Problem]:
    """Check the `sources` list."""
    if not isinstance(sources, list):
        return [_error("sources: must be a list")]
    problems: list[Problem] = []
    for index, source in enumerate(sources):
        where = f"source {index}"
        if not isinstance(source, dict):
            problems.append(_error(f"{where}: must be an object"))
            continue
        kind = source.get("kind")
        if kind not in SOURCE_KINDS:
            problems.append(_error(f"{where}: unknown source kind {kind}"))
        if "path" not in source and "url" not in source:
            problems.append(_error(f"{where}: needs a path or url"))
    return problems


def _check_lean(where: str, entries: Any) -> list[Problem]:
    """Check one node's `lean` list."""
    if not isinstance(entries, list):
        return [_error(f"{where}: lean must be a list")]
    problems: list[Problem] = []
    for index, entry in enumerate(entries):
        label = f"{where} lean {index}"
        if isinstance(entry, dict):
            problems.extend(_missing(label, entry, _LEAN_KEYS))
        else:
            problems.append(_error(f"{label}: must be an object"))
    return problems


def _used_ids(value: Any) -> list[str]:
    """Return the string ids in a node's `uses` value, or none if not a list."""
    return [u for u in value if isinstance(u, str)] if isinstance(value, list) else []


def _check_nodes(nodes: Any) -> list[Problem]:
    """Check each node's shape, id uniqueness, uses and absence of cycles."""
    if not isinstance(nodes, list):
        return [_error("nodes: must be a list")]
    problems: list[Problem] = []
    uses: dict[str, list[str]] = {}
    for index, item in enumerate(nodes):
        if not isinstance(item, dict):
            problems.append(_error(f"node {index}: must be an object"))
            continue
        node_id = item.get("id")
        where = f"node {node_id if isinstance(node_id, str) else index}"
        problems.extend(_missing(where, item, _NODE_KEYS))
        if isinstance(node_id, str):
            if node_id in uses:
                problems.append(_error(f"{where}: duplicate id {node_id}"))
            else:
                uses[node_id] = _used_ids(item.get("uses"))
        if "kind" in item and item["kind"] not in NODE_KINDS:
            problems.append(_error(f"{where}: unknown node kind {item['kind']}"))
        if "uses" in item and not isinstance(item["uses"], list):
            problems.append(_error(f"{where}: uses must be a list"))
        problems.extend(_check_lean(where, item.get("lean", [])))
    for node_id, used_ids in uses.items():
        for used_id in used_ids:
            if used_id not in uses:
                problems.append(_error(f"node {node_id}: unknown use {used_id}"))
    cycle = _find_cycle(uses)
    if cycle:
        problems.append(_error("cycle " + " → ".join(cycle)))
    return problems


def _find_cycle(uses: dict[str, list[str]]) -> list[str]:
    """Return the first dependency cycle found depth-first, closed at both ends."""
    done: set[str] = set()
    path: list[str] = []

    def visit(node_id: str) -> list[str]:
        if node_id in path:
            return path[path.index(node_id) :] + [node_id]
        if node_id in done or node_id not in uses:
            return []
        path.append(node_id)
        for used_id in uses[node_id]:
            cycle = visit(used_id)
            if cycle:
                return cycle
        path.pop()
        done.add(node_id)
        return []

    for start in uses:
        cycle = visit(start)
        if cycle:
            return cycle
    return []


def check_outline(outline: dict[str, Any]) -> list[Problem]:
    """Return every structural problem found in the outline."""
    problems: list[Problem] = []
    if outline.get("format") != FORMAT:
        problems.append(_error(f"format must be {FORMAT}, not {outline.get('format')}"))
    problems.extend(_missing("outline", outline, _TOP_KEYS[1:]))
    if "project" in outline:
        problems.extend(_check_project(outline["project"]))
    if "sources" in outline:
        problems.extend(_check_sources(outline["sources"]))
    if "nodes" in outline:
        problems.extend(_check_nodes(outline["nodes"]))
    return problems


_DECL_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|nonrec|partial|unsafe"
    r"|public|scoped|local)\s+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class|inductive|instance|opaque|axiom)"
    r"\s+(?P<name>[^\s:(\[{⟨]+)(?P<rest>.*)$"
)
_NAMESPACE_RE = re.compile(r"^namespace\s+(\S+)")
_SECTION_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*(?:(?:public|noncomputable|private)\s+)*section\b"
)
_MUTUAL_RE = re.compile(r"^mutual\b")
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
_END_RE = re.compile(r"^end(?:\s+(\S+))?\s*$")
_OPEN_RE = re.compile(r"^open\s+([^\n]*)")
_OPENERS = "([{⟨"
_CLOSERS = ")]}⟩"
_GIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Declaration:
    """A Lean declaration found by scanning source text."""

    name: str
    file: str
    line: int
    type: str
    namespaces: tuple[str, ...]


def _collapse(text: str) -> str:
    """Collapse every run of whitespace to a single space."""
    return " ".join(text.split())


def _header(first_rest: str, following: list[str]) -> str:
    """Return the header text up to the top-level `:=` or `where`."""
    depth = 0
    out: list[str] = []
    lines = [first_rest, *following]
    for index, line in enumerate(lines):
        line = line.split("--", 1)[0]
        if index and line.lstrip().startswith("|"):
            break
        position = 0
        while position < len(line):
            char = line[position]
            if char in _OPENERS:
                depth += 1
            elif char in _CLOSERS:
                depth -= 1
            elif depth <= 0 and line.startswith(":=", position):
                return _collapse("".join(out) + line[:position])
            elif (
                depth <= 0
                and line.startswith("where", position)
                and (position == 0 or not (line[position - 1].isalnum()))
                and line[position + 5 :][:1] in ("", " ", "\t")
            ):
                return _collapse("".join(out) + line[:position])
            position += 1
        out.append(line + " ")
    return _collapse("".join(out))


def _continuation(lines: list[str], start: int) -> list[str]:
    """Return the indented lines directly after line `start`."""
    rest: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line[0].isspace():
            break
        rest.append(line)
    return rest


def declarations(root: Path, file: str) -> list[Declaration]:
    """Return the declarations at column 0 of a Lean file, in order."""
    lines = (root / file).read_text(encoding="utf-8").splitlines()
    frames: list[tuple[str, tuple[str, ...]]] = []
    opened: list[str] = []
    found: list[Declaration] = []
    in_comment = False
    for index, line in enumerate(lines):
        if in_comment:
            in_comment = "-/" not in line
            continue
        if line.startswith("/-"):
            in_comment = "-/" not in line
            continue
        match = _NAMESPACE_RE.match(line)
        if match:
            frames.extend(("namespace", (part,)) for part in match.group(1).split("."))
            continue
        match = _SECTION_RE.match(line)
        if match:
            frames.append(("section", ()))
            continue
        if _MUTUAL_RE.match(line):
            frames.append(("mutual", ()))
            continue
        match = _END_RE.match(line)
        if match:
            if frames and frames[-1][0] == "namespace":
                closing = (match.group(1) or "x").count(".") + 1
            else:
                closing = 1
            del frames[max(len(frames) - closing, 0) :]
            continue
        match = _OPEN_RE.match(line)
        if match:
            opened.extend(w for w in match.group(1).split() if w != "in")
            continue
        match = _DECL_RE.match(line)
        if not match:
            continue
        parts = [part for _, names in frames for part in names]
        prefixes = tuple(".".join(parts[: i + 1]) for i in range(len(parts)))
        name = match.group("name").rstrip(".")
        if name.startswith("_root_."):
            full = name[len("_root_.") :]
        else:
            full = ".".join([*parts, name])
        header = _header(match.group("rest"), _continuation(lines, index))
        found.append(
            Declaration(full, file, index + 1, header, prefixes + tuple(opened))
        )
    return found


def _lean_entries(outline: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return each node's well-formed lean entries with the node's id."""
    entries: list[tuple[str, dict[str, Any]]] = []
    for item in outline.get("nodes", []):
        for lean in item.get("lean", []):
            if all(key in lean for key in _LEAN_KEYS):
                entries.append((str(item.get("id")), lean))
    return entries


def _project_root(outline: dict[str, Any], base: Path) -> Path:
    """Return the Lean project folder, which `project.root` gives relative to base."""
    return base / str(outline["project"]["root"])


def check_lean_entries(outline: dict[str, Any], base: Path) -> list[Problem]:
    """Compare each node's `lean` entries with the project's source text."""
    root = _project_root(outline, base)
    scanned: dict[str, list[Declaration] | None] = {}
    problems: list[Problem] = []
    for node_id, lean in _lean_entries(outline):
        file, name = str(lean["file"]), str(lean["name"])
        where = f"node {node_id}: {name}"
        if file not in scanned:
            try:
                scanned[file] = declarations(root, file)
            except (OSError, UnicodeDecodeError):
                scanned[file] = None
        found = scanned[file]
        if found is None:
            problems.append(_error(f"{where}: file {file} cannot be read"))
            continue
        matches = [d for d in found if d.name == name]
        if not matches:
            problems.append(_error(f"{where}: no declaration with this name in {file}"))
            continue
        match = next((d for d in matches if d.line == lean["line"]), matches[0])
        if match.line != lean["line"]:
            problems.append(
                _error(
                    f"{where}: listed at line {lean['line']}, now at line {match.line}"
                )
            )
        if _collapse(str(lean["type"])) != match.type:
            problems.append(
                _error(
                    f"{where}: type is {match.type!r} in {file}, "
                    f"outline says {_collapse(str(lean['type']))!r}"
                )
            )
    return problems


def _git(root: Path, *args: str) -> str | None:
    """Return git's output for the project folder, or None if it cannot run."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def check_commit(outline: dict[str, Any], base: Path) -> list[Problem]:
    """Note how far the project has moved since the outline's commit."""
    root = _project_root(outline, base)
    recorded = str(outline["project"]["commit"])
    if not _COMMIT_RE.fullmatch(recorded):
        return [Problem("note", "project commit not checked: not a commit hash")]
    latest = _git(root, "log", "-1", "--format=%H", "--", ".")
    if not latest:
        return [
            Problem("note", "project commit not checked: git cannot read the project")
        ]
    if latest.startswith(recorded) or recorded.startswith(latest):
        return []
    count = _git(root, "rev-list", "--count", f"{recorded}..HEAD", "--", ".")
    if count is None:
        return [Problem("note", f"commit {recorded} is not in the project's history")]
    return [Problem("note", f"project moved {count} commits since {recorded}")]


def _build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(prog="walkthrough.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check an outline")
    check.add_argument("outline", type=Path)
    check.add_argument("--notebook", type=Path, help="notebook to compare")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return the exit status."""
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as error:
        return 2 if error.code else 0
    try:
        outline = load_outline(args.outline)
    except OutlineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    problems = check_outline(outline)
    if not any(p.level == "error" for p in problems):
        base = args.outline.parent
        problems += check_lean_entries(outline, base) + check_commit(outline, base)
    for problem in problems:
        print(f"{problem.level}: {problem.message}")
    return 1 if any(p.level == "error" for p in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
