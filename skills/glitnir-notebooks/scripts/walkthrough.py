"""Check a proof-walkthrough outline.

A walkthrough explains an existing Lean formalization through an
`outline.json`: the statements of a paper, their dependencies and the Lean
declarations that formalize them. This helper verifies such an outline.

Run it with the standard library only, without writing bytecode:

    python3 -B walkthrough.py check OUTLINE [--notebook FILE]
    python3 -B walkthrough.py map OUTLINE [--out DIR] [--links local|public]

`map` draws the outline's dependency graph and a clickable detail page, and
refuses to draw an outline that `check` reports errors for.

Exit status: 0 when no problem is an error, 1 when any is, 2 for an
unreadable outline or a usage error.
"""

import argparse
import html
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

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
_VARIABLE_RE = re.compile(r"^variable\b")
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
    namespaces: tuple[str, ...]  # enclosing namespace prefixes only, never opens
    variables: bool = False  # a `variable` command is in force at the declaration


def _collapse(text: str) -> str:
    """Collapse every run of whitespace to a single space."""
    return " ".join(text.split())


def _split_header(first_rest: str, following: list[str]) -> tuple[str, str]:
    """Return the header up to the top-level `:=` or `where`, and the text after."""
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
            elif depth <= 0 and (
                line.startswith(":=", position)
                or (
                    line.startswith("where", position)
                    and (position == 0 or not (line[position - 1].isalnum()))
                    and line[position + 5 :][:1] in ("", " ", "\t")
                )
            ):
                tail = [
                    line[position:],
                    *(rest.split("--", 1)[0] for rest in lines[index + 1 :]),
                ]
                return _collapse("".join(out) + line[:position]), _collapse(
                    " ".join(tail)
                )
            position += 1
        out.append(line + " ")
    return _collapse("".join(out)), ""


def _header(first_rest: str, following: list[str]) -> str:
    """Return the header text up to the top-level `:=` or `where`."""
    return _split_header(first_rest, following)[0]


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
    scoped_vars: list[bool] = []
    file_vars = False
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
            for part in match.group(1).split("."):
                frames.append(("namespace", (part,)))
                scoped_vars.append(False)
            continue
        match = _SECTION_RE.match(line)
        if match:
            frames.append(("section", ()))
            scoped_vars.append(False)
            continue
        if _MUTUAL_RE.match(line):
            frames.append(("mutual", ()))
            scoped_vars.append(False)
            continue
        match = _END_RE.match(line)
        if match:
            if frames and frames[-1][0] == "namespace":
                closing = (match.group(1) or "x").count(".") + 1
            else:
                closing = 1
            keep = max(len(frames) - closing, 0)
            del frames[keep:]
            del scoped_vars[keep:]
            continue
        if _VARIABLE_RE.match(line):
            if scoped_vars:
                scoped_vars[-1] = True
            else:
                file_vars = True
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
            Declaration(
                full, file, index + 1, header, prefixes, file_vars or any(scoped_vars)
            )
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


_CLAIM_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|nonrec|partial|unsafe"
    r"|public|scoped|local)\s+)*"
    r"(?:theorem|lemma)\s+(?P<name>[^\s:(\[{⟨]+)(?P<rest>.*)$"
)
_EXACT_RE = re.compile(r":=\s*by\s+exact\s+(?P<target>\S+)")
_TOKEN_RE = re.compile(
    r"(?:[^\W\d]|«[^»]*»)(?:[\w'!?]|«[^»]*»|\.(?=[^\W\d]|«))*|\d+|\S"
)
_IDENT_START_RE = re.compile(r"[^\W\d]|«")


def _tokens(text: str) -> list[str]:
    """Split Lean text into identifiers (dots and `«…»` included) and symbols."""
    return _TOKEN_RE.findall(text)


def same_statement(written: str, declared: str, namespaces: Sequence[str]) -> bool:
    """Tell whether a written header restates the declared one.

    The headers match token by token. A declared name may be written in full,
    or with any of the enclosing namespaces as its prefix.
    """
    left, right = _tokens(written), _tokens(declared)
    if len(left) != len(right):
        return False
    for token, wanted in zip(left, right):
        if token == wanted:
            continue
        if _IDENT_START_RE.match(wanted) and any(
            token == f"{space}.{wanted}" for space in namespaces
        ):
            continue
        return False
    return True


def _claims_in(text: str) -> list[tuple[str, str, str]]:
    """Return (name, header, tail) for each column-0 theorem or lemma in text."""
    lines = text.splitlines()
    found: list[tuple[str, str, str]] = []
    in_comment = False
    for index, line in enumerate(lines):
        if in_comment:
            in_comment = "-/" not in line
            continue
        if line.startswith("/-"):
            in_comment = "-/" not in line
            continue
        match = _CLAIM_RE.match(line)
        if match:
            header, tail = _split_header(
                match.group("rest"), _continuation(lines, index)
            )
            found.append((match.group("name"), header, tail))
    return found


def check_notebook(
    outline: dict[str, Any], base: Path, notebook: Path
) -> list[Problem]:
    """Compare the notebook's claims with the outline and the project."""
    try:
        text = notebook.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [_error(f"notebook {notebook} cannot be read")]
    root = _project_root(outline, base)
    nodes = {
        str(item["claim"]): item
        for item in outline.get("nodes", [])
        if isinstance(item.get("claim"), str)
    }
    problems = _forbidden_in(text)
    written: set[str] = set()
    for name, header, tail in _claims_in(text):
        written.add(name)
        exact = _EXACT_RE.fullmatch(tail)
        if not exact:
            problems.append(_error(f"claim {name} is not 'by exact <project theorem>'"))
            continue
        item = nodes.get(name)
        if item is None:
            problems.append(_error(f"claim {name} is not a claim of any node"))
            continue
        target = exact.group("target")
        entries = [e for e in item.get("lean", []) if e.get("name") == target]
        if not entries:
            problems.append(
                _error(
                    f"claim {name}: {target} is not a lean name of node {item['id']}"
                )
            )
            continue
        problems.extend(_compare_claim(root, name, header, target, entries[0]))
    for claim, item in nodes.items():
        if claim not in written:
            problems.append(
                _error(f"node {item['id']}: claim {claim} is not in the notebook")
            )
    return problems


_FORBIDDEN_WORDS = frozenset(
    "open variable universe def abbrev notation infix infixl infixr prefix postfix"
    " macro syntax axiom instance opaque structure class inductive attribute"
    " set_option local scoped".split()
)
_PREFIX_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|nonrec|partial|unsafe|public)\s+)*"
)


def _forbidden_in(text: str) -> list[Problem]:
    """Report each column-0 command that defines or opens something."""
    problems: list[Problem] = []
    in_comment = False
    for number, line in enumerate(text.splitlines(), start=1):
        if in_comment:
            in_comment = "-/" not in line
            continue
        if line.startswith("/-"):
            in_comment = "-/" not in line
            continue
        words = line[_PREFIX_RE.match(line).end() :].split()  # type: ignore[union-attr]
        if words and words[0] in _FORBIDDEN_WORDS:
            problems.append(
                _error(
                    f"walkthrough must not define or open anything: "
                    f"line {number}: {words[0]}"
                )
            )
    return problems


def _compare_claim(
    root: Path, claim: str, header: str, target: str, entry: dict[str, Any]
) -> list[Problem]:
    """Compare one claim's header with the declaration it cites."""
    file = str(entry["file"])
    try:
        found = [d for d in declarations(root, file) if d.name == target]
    except (OSError, UnicodeDecodeError):
        return [_error(f"claim {claim}: file {file} cannot be read")]
    if not found:
        return [_error(f"claim {claim}: no declaration {target} in {file}")]
    if any(d.variables for d in found):
        return [
            _error(
                f"claim {claim} cites {target}, which depends on section "
                "variables; restate it in prose instead"
            )
        ]
    if any(same_statement(header, d.type, d.namespaces) for d in found):
        return []
    return [
        _error(
            f"claim {claim}: statement {header!r} differs from "
            f"{target} in {file}: {found[0].type!r}"
        )
    ]


KIND_COLOUR = {
    "definition": ("#e8eef7", "#5b7bb0"),
    "lemma": ("#e3f4ef", "#2f8f74"),
    "proposition": ("#efe8f8", "#7a52b3"),
    "theorem": ("#fdeee2", "#c96a1b"),
    "corollary": ("#fdf5dc", "#b38a12"),
    "algorithm": ("#e6f4e1", "#4f9a35"),
    "appendix": ("#f1f1f1", "#8a8a8a"),
}
_KATEX = "https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/"


def _esc(value: Any) -> str:
    """Escape any outline value for HTML text or a quoted attribute."""
    return html.escape(str(value), quote=True)


def direct_uses(nodes: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """Drop each use that is reachable through another use of the same node."""
    uses = {
        n["id"]: [u for u in n.get("uses", []) if isinstance(u, str)] for n in nodes
    }

    def reach(start: str) -> set[str]:
        seen: set[str] = set()
        todo = [u for u in uses.get(start, []) if u in uses]
        while todo:
            current = todo.pop()
            if current not in seen:
                seen.add(current)
                todo.extend(u for u in uses[current] if u in uses)
        return seen

    reached = {i: reach(i) for i in uses}
    result: dict[str, list[str]] = {}
    for i, own in uses.items():
        known = [u for u in own if u in uses]
        result[i] = [
            u for u in known if not any(u in reached[v] for v in known if v != u)
        ]
    return result


def _layers(nodes: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Return the longest path from a node with no uses; those sit at 0."""
    uses = {n["id"]: [u for u in _used_ids(n.get("uses")) if u] for n in nodes}
    depth: dict[str, int] = {}

    def visit(i: str) -> int:
        if i not in depth:
            depth[i] = 1 + max((visit(u) for u in uses[i] if u in uses), default=-1)
        return depth[i]

    for i in uses:
        visit(i)
    return depth


def _positions(
    nodes: Sequence[dict[str, Any]], width: int, row: int, top: int
) -> dict[str, tuple[float, float]]:
    """Place each node: one row per layer, barycentre order within a row."""
    depth = _layers(nodes)
    uses = {n["id"]: _used_ids(n.get("uses")) for n in nodes}
    users: dict[str, list[str]] = {i: [] for i in uses}
    for i, used in uses.items():
        for u in used:
            if u in users:
                users[u].append(i)
    rows: dict[int, list[str]] = {}
    for i, d in depth.items():
        rows.setdefault(d, []).append(i)
    top_layer = max(rows, default=0)
    for _ in range(6):
        for d in sorted(rows):
            below = {i: k for k, i in enumerate(rows.get(d - 1, []))}
            above = {i: k for k, i in enumerate(rows.get(d + 1, []))}

            def key(i: str) -> float:
                ref = [below[u] for u in uses[i] if u in below]
                ref += [above[u] for u in users[i] if u in above]
                return sum(ref) / len(ref) if ref else 0.0

            rows[d].sort(key=key)
    pos: dict[str, tuple[float, float]] = {}
    for d, ids in rows.items():
        gap = width / (len(ids) + 1)
        for k, i in enumerate(ids):
            pos[i] = (gap * (k + 1), top + (top_layer - d) * row)
    return pos


def _short(label: str, limit: int = 20) -> str:
    """Shorten a label to fit a box."""
    return label if len(label) <= limit else label[: limit - 1] + "…"


def map_svg(outline: dict[str, Any]) -> str:
    """Draw the nodes as a dependency graph, statements on top of their uses."""
    nodes = [n for n in outline["nodes"] if isinstance(n, dict)]
    row, top, box_w, box_h = 92, 110, 150, 46
    layers = _layers(nodes)
    widest = max(
        (list(layers.values()).count(d) for d in set(layers.values())), default=1
    )
    width = max(1180, widest * (box_w + 24))
    height = top + row * (max(layers.values(), default=0) + 1) + 20
    pos = _positions(nodes, width, row, top)
    direct = direct_uses(nodes)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        'font-family="Helvetica, Arial, sans-serif">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#9aa3ad"/></marker></defs>',
        f'<rect width="{width}" height="{height}" rx="14" fill="#ffffff"/>',
        '<text x="24" y="36" font-size="20" font-weight="700" fill="#222">'
        f'{_esc(outline.get("title", ""))}</text>',
        '<text x="24" y="56" font-size="13" fill="#555">An arrow runs from a '
        "statement to each statement whose proof uses it directly. "
        "Read from the top down.</text>",
    ]
    for n in nodes:
        x2, y2 = pos[n["id"]]
        for u in direct[n["id"]]:
            x1, y1 = pos[u]
            start, end = y1 - box_h / 2, y2 + box_h / 2
            mid = (start + end) / 2
            dashed = ' stroke-dasharray="5 4"' if n.get("kind") == "appendix" else ""
            out.append(
                f'<path d="M{x1:.0f},{start:.0f} C{x1:.0f},{mid:.0f} '
                f'{x2:.0f},{mid:.0f} {x2:.0f},{end + 6:.0f}" fill="none" '
                f'stroke="#9aa3ad" stroke-width="1.4"{dashed} '
                'marker-end="url(#arrow)"/>'
            )
    for n in nodes:
        x, y = pos[n["id"]]
        fill, stroke = KIND_COLOUR.get(str(n.get("kind")), ("#f1f1f1", "#8a8a8a"))
        sub = " · ".join(str(p) for p in (n.get("section"), n.get("kind")) if p)
        out.append(
            f'<g data-id="{_esc(n["id"])}"><title>{_esc(n.get("label", ""))}</title>'
            f'<rect x="{x - box_w / 2:.0f}" y="{y - box_h / 2:.0f}" width="{box_w}" '
            f'height="{box_h}" rx="9" fill="{fill}" stroke="{stroke}" '
            'stroke-width="1.6"/>'
            f'<text x="{x:.0f}" y="{y - 3:.0f}" font-size="13" font-weight="700" '
            f'text-anchor="middle" fill="#222">{_esc(_short(str(n.get("label", ""))))}'
            f'</text><text x="{x:.0f}" y="{y + 14:.0f}" font-size="11" '
            f'text-anchor="middle" fill="{stroke}">{_esc(_short(sub, 26))}</text></g>'
        )
    for k, (name, (fill, stroke)) in enumerate(reversed(KIND_COLOUR.items())):
        x = width - 24 - (k + 1) * 104
        out.append(
            f'<rect x="{x}" y="70" width="14" height="14" rx="3" fill="{fill}" '
            f'stroke="{stroke}"/><text x="{x + 20}" y="82" font-size="12" '
            f'fill="#444">{name}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)


def _first_page(pages: Any) -> str | None:
    """Return the first page number in a node's `pages`, if it has one."""
    found = re.search(r"\d+", str(pages)) if pages is not None else None
    return found.group(0) if found else None


def _paper_target(outline: dict[str, Any], base: Path, links: str) -> str | None:
    """Return the paper's location for the chosen links, if it has one."""
    paper = next(
        (
            s
            for s in outline.get("sources", [])
            if isinstance(s, dict) and s.get("kind") == "paper"
        ),
        None,
    )
    if paper is None:
        return None
    if links == "public":
        url = paper.get("url")
        return url if isinstance(url, str) and url else None
    path = paper.get("path")
    return (base / path).resolve().as_uri() if isinstance(path, str) else None


def _lean_href(outline: dict[str, Any], base: Path, links: str, lean: Any) -> str:
    """Return the link for one Lean entry, or "" if it cannot be built."""
    file, line = str(lean["file"]), lean["line"]
    project = outline["project"]
    if links == "local":
        root = _project_root(outline, base).resolve()
        return f"vscode://file{root}/{file}:{line}"
    repository = project.get("repository")
    if not isinstance(repository, str) or not repository:
        return ""
    folder = str(project.get("path") or "").strip("/")
    where = f"{folder}/{file}" if folder else file
    return f"{repository.rstrip('/')}/blob/{project['commit']}/{where}#L{line}"


def _page_data(
    outline: dict[str, Any], base: Path, links: Literal["local", "public"]
) -> dict[str, Any]:
    """Return what the page script needs: the nodes with their links built."""
    paper = _paper_target(outline, base, links)
    nodes = []
    for n in outline["nodes"]:
        page = _first_page(n.get("pages"))
        href = paper
        if href and page and href.lower().endswith(".pdf"):
            href = f"{href}#page={page}"
        nodes.append(
            {
                "id": n["id"],
                "label": n.get("label", ""),
                "kind": n.get("kind", ""),
                "section": n.get("section", ""),
                "pages": n.get("pages", ""),
                "statement": n.get("statement", ""),
                "idea": n.get("idea", ""),
                "uses": _used_ids(n.get("uses")),
                "paper": href or "",
                "lean": [
                    {
                        "name": lean.get("name", ""),
                        "file": lean["file"],
                        "line": lean["line"],
                        "note": lean.get("note", ""),
                        "href": _lean_href(outline, base, links, lean),
                    }
                    for lean in n.get("lean", [])
                ],
            }
        )
    return {"nodes": nodes}


_PAGE_STYLE = """
:root { --bg:#f6f7f9; --panel:#fff; --ink:#1f2328; --muted:#5b636d;
  --line:#dde1e6; --code:#f0f2f4; --link:#2457a6; }
@media (prefers-color-scheme: dark) { :root { --bg:#16181b; --panel:#1f2328;
  --ink:#e6e8ea; --muted:#9aa3ad; --line:#363b42; --code:#2a2f35;
  --link:#7aa7e8; } }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 Helvetica, Arial, sans-serif; }
main { display:grid; grid-template-columns:minmax(0,1.6fr) minmax(320px,1fr);
  gap:16px; padding:16px; max-width:1500px; margin:auto; }
@media (max-width: 900px) { main { grid-template-columns:1fr; } }
#map svg { width:100%; height:auto; }
#map [data-id] { cursor:pointer; }
#map [data-id]:hover rect { filter:brightness(0.95); }
#map .chosen rect { stroke-width:4; }
aside { background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:18px 20px; align-self:start;
  position:sticky; top:16px; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:14px; margin:18px 0 6px; color:var(--muted);
  text-transform:uppercase; letter-spacing:.04em; }
.meta, .note { color:var(--muted); font-size:13px; }
ul { padding-left:18px; } li { margin:6px 0; }
code { font-size:12.5px; background:var(--code); padding:1px 4px;
  border-radius:4px; }
a { color:var(--link); }
"""

_PAGE_SCRIPT = """
const DATA = JSON.parse(document.getElementById('walkthrough-data').textContent);
const byId = Object.fromEntries(DATA.nodes.map(n => [n.id, n]));
const esc = s => String(s).replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function show(id) {
  const n = byId[id];
  document.querySelectorAll('#map .chosen')
    .forEach(g => g.classList.remove('chosen'));
  document.querySelectorAll('#map [data-id]').forEach(g => {
    if (g.dataset.id === id) g.classList.add('chosen');
  });
  const users = DATA.nodes.filter(m => m.uses.includes(id));
  const link = i => byId[i]
    ? `<a href="#" data-go="${esc(i)}">${esc(byId[i].label)}</a>` : esc(i);
  const where = [n.section, n.kind].filter(Boolean).map(esc).join(' · ');
  const paper = n.paper && n.pages !== ''
    ? ` · <a href="${esc(n.paper)}">paper p. ${esc(n.pages)}</a>` : '';
  const lean = n.lean.map(l => {
    const spot = `${esc(l.file)}:${esc(l.line)}`;
    const at = l.href ? `<a href="${esc(l.href)}">${spot}</a>` : spot;
    const note = l.note ? `<div class="note">${esc(l.note)}</div>` : '';
    return `<li><code>${esc(l.name)}</code><br>${at}${note}</li>`;
  }).join('');
  const detail = document.getElementById('detail');
  detail.innerHTML = `<h1>${esc(n.label)}</h1>
    <div class="meta">${where}${paper}</div>
    <h2>Statement</h2><p>${esc(n.statement)}</p>
    <h2>Idea</h2><p>${esc(n.idea)}</p>
    <h2>Uses</h2><p>${n.uses.map(link).join(', ') || 'nothing above it'}</p>
    <h2>Used by</h2><p>${users.map(m => link(m.id)).join(', ')
      || 'nothing: it is a final result'}</p>
    <h2>In Lean</h2><ul>${lean || '<li>No Lean entry.</li>'}</ul>`;
  if (window.renderMathInElement) {
    renderMathInElement(detail, {delimiters: [
      {left: '$$', right: '$$', display: true},
      {left: '$', right: '$', display: false}]});
  }
}
document.addEventListener('click', e => {
  const go = e.target.closest('[data-go]');
  if (go) { e.preventDefault(); show(go.dataset.go); return; }
  const box = e.target.closest('#map [data-id]');
  if (box) show(box.dataset.id);
});
"""


def _json_for_script(data: Any) -> str:
    """Serialise data for a script element, so it cannot end the element."""
    text = json.dumps(data, ensure_ascii=False)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def map_html(
    outline: dict[str, Any], base: Path, links: Literal["local", "public"]
) -> str:
    """Return the page: the map and a detail panel for the chosen node."""
    title = _esc(outline.get("title", ""))
    overview = outline.get("overview")
    intro = f'<p class="meta">{_esc(overview)}</p>' if overview else ""
    data = _json_for_script(_page_data(outline, base, links))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{_KATEX}katex.min.css">
<script defer src="{_KATEX}katex.min.js"></script>
<script defer src="{_KATEX}contrib/auto-render.min.js"></script>
<style>{_PAGE_STYLE}</style></head><body>
<main>
<section id="map">{map_svg(outline)}</section>
<aside id="detail"><h1>Pick a statement</h1>{intro}
<p class="meta">Click any box in the map to see what it says, the idea of its
proof, where it is in the paper and which Lean declarations formalize it.</p>
</aside>
</main>
<script type="application/json" id="walkthrough-data">{data}</script>
<script>{_PAGE_SCRIPT}</script></body></html>
"""


def write_map(
    outline: dict[str, Any],
    base: Path,
    out: Path,
    links: Literal["local", "public"],
) -> tuple[Path, Path]:
    """Write `assets/map.svg` and `map.html` under out; return their paths."""
    svg_path, html_path = out / "assets" / "map.svg", out / "map.html"
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(map_svg(outline), encoding="utf-8")
    html_path.write_text(map_html(outline, base, links), encoding="utf-8")
    return svg_path, html_path


def _build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(prog="walkthrough.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check an outline")
    check.add_argument("outline", type=Path)
    check.add_argument("--notebook", type=Path, help="notebook to compare")
    draw = commands.add_parser("map", help="draw the outline's map")
    draw.add_argument("outline", type=Path)
    draw.add_argument("--out", type=Path, help="folder to write (default: outline's)")
    draw.add_argument(
        "--links",
        choices=("local", "public"),
        default="local",
        help="link to this machine's files or to public addresses",
    )
    return parser


def _run_map(
    args: argparse.Namespace, outline: dict[str, Any], problems: list[Problem]
) -> int:
    """Draw the map unless the outline has errors; return the exit status."""
    for problem in problems:
        print(f"{problem.level}: {problem.message}")
    if any(p.level == "error" for p in problems):
        return 1
    base = args.outline.parent
    out = args.out if args.out is not None else base
    for path in write_map(outline, base, out, args.links):
        print(path)
    return 0


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
    if args.command == "map":
        return _run_map(args, outline, problems)
    if not any(p.level == "error" for p in problems):
        base = args.outline.parent
        problems += check_lean_entries(outline, base) + check_commit(outline, base)
        if args.notebook is not None and not any(p.level == "error" for p in problems):
            problems += check_notebook(outline, base, args.notebook)
    for problem in problems:
        print(f"{problem.level}: {problem.message}")
    return 1 if any(p.level == "error" for p in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
