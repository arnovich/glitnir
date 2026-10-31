"""Validate optional, curated proof atlases independently of checking observations."""

import json
from pathlib import Path
from typing import Any, TypedDict, cast

from glitnir.lean.project import read_regular

FORMAT = "glitnir.proof-atlas/v1"
MAX_BYTES = 1024 * 1024
MAX_NODES = 2000


class AtlasNode(TypedDict):
    """An explanatory argument bound to one existing outline statement."""

    id: str
    label: str
    statement: str
    declarations: list[str]
    goal: str
    scope: str
    role: str
    idea: str
    difference: str


class AtlasRoute(TypedDict):
    """Selected supporting arguments, never extracted Lean dependencies."""

    id: str
    label: str
    root: str
    supports: dict[str, list[str]]


class ProofAtlas(TypedDict):
    """A versioned atlas for the outline's pinned source revision."""

    format: str
    commit: str
    nodes: list[AtlasNode]
    routes: list[AtlasRoute]


def _require(condition: bool, message: str) -> None:
    """Raise a diagnostic rather than silently dropping invalid information."""
    if not condition:
        raise ValueError(message)


def _string(value: Any, limit: int = 8192) -> bool:
    """Require bounded, non-empty text."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def validate_atlas(raw: Any, statements: list[Any], commit: Any) -> ProofAtlas:
    """Validate the complete bounded atlas before selecting a neighborhood."""
    _require(isinstance(raw, dict), "atlas must be an object")
    _require(raw.get("format") == FORMAT, f"atlas format must be {FORMAT}")
    _require(
        _string(commit, 40) and raw.get("commit") == commit,
        "atlas source commit differs from the outline",
    )
    nodes, routes = raw.get("nodes"), raw.get("routes")
    _require(
        isinstance(nodes, list) and 0 < len(nodes) <= MAX_NODES,
        f"atlas needs 1–{MAX_NODES} nodes",
    )
    _require(
        isinstance(routes, list) and 0 < len(routes) <= 16, "atlas needs 1–16 routes"
    )
    _require(isinstance(statements, list), "outline nodes must be a list")
    sources = {
        n["id"]: n
        for n in statements[:240]
        if isinstance(n, dict) and isinstance(n.get("id"), str)
    }
    source_ids = [n.get("id") for n in statements if isinstance(n, dict)]
    _require(
        all(isinstance(key, str) for key in source_ids)
        and len(set(source_ids)) == len(source_ids),
        "ambiguous outline statement ids",
    )
    ids: set[str] = set()
    for node in nodes:
        _require(isinstance(node, dict), "atlas node must be an object")
        for field in ("id", "label", "statement", "goal", "scope", "role", "idea"):
            _require(
                _string(node.get(field), 128 if field in {"id", "statement"} else 8192),
                f"atlas node needs valid {field}",
            )
        _require(node["id"] not in ids, "duplicate atlas node id")
        ids.add(node["id"])
        _require(node["statement"] in sources, "unknown atlas statement binding")
        _require(
            isinstance(node.get("difference"), str) and len(node["difference"]) <= 8192,
            "atlas difference must be bounded text",
        )
        refs = node.get("declarations")
        _require(
            isinstance(refs, list)
            and len(refs) <= 32
            and all(_string(r, 512) for r in refs),
            "atlas declarations must be a bounded name list",
        )
        _require(
            isinstance(sources[node["statement"]].get("lean"), list),
            "outline declarations must be a list",
        )
        known = [
            r.get("name")
            for r in sources[node["statement"]].get("lean", [])
            if isinstance(r, dict)
        ]
        _require(
            len(set(refs)) == len(refs) and all(known.count(r) == 1 for r in refs),
            "unknown or ambiguous atlas declaration binding",
        )
    route_ids: set[str] = set()
    for route in routes:
        _require(isinstance(route, dict), "atlas route must be an object")
        _require(
            _string(route.get("id"), 128) and route["id"] not in route_ids,
            "duplicate or invalid atlas route id",
        )
        route_ids.add(route["id"])
        _require(_string(route.get("label"), 512), "atlas route needs a label")
        _require(
            isinstance(route.get("root"), str) and route["root"] in ids,
            "unknown atlas route root",
        )
        edges = route.get("supports")
        _require(
            isinstance(edges, dict) and len(edges) <= MAX_NODES,
            "atlas supports must be a bounded object",
        )
        for origin, targets in edges.items():
            _require(
                origin in ids
                and isinstance(targets, list)
                and len(targets) <= MAX_NODES,
                "invalid atlas support list",
            )
            _require(
                all(isinstance(t, str) and t in ids for t in targets),
                "unknown atlas support target",
            )
            _require(
                len(set(targets)) == len(targets), "duplicate atlas support target"
            )
        # Iterative topological traversal avoids recursion limits for large atlases.
        incoming = dict.fromkeys(ids, 0)
        for targets in edges.values():
            for target in targets:
                incoming[target] += 1
        ready = [key for key, count in incoming.items() if not count]
        visited = 0
        while ready:
            key = ready.pop()
            visited += 1
            for target in edges.get(key, []):
                incoming[target] -= 1
                if not incoming[target]:
                    ready.append(target)
        _require(visited == len(ids), "atlas route contains a cycle")
    # Construct the wire shape explicitly; unknown JSON fields cannot carry statuses.
    return {
        "format": FORMAT,
        "commit": commit,
        "nodes": [
            cast(AtlasNode, {key: node[key] for key in AtlasNode.__annotations__})
            for node in nodes
        ],
        "routes": [
            cast(AtlasRoute, {key: route[key] for key in AtlasRoute.__annotations__})
            for route in routes
        ],
    }


def load_atlas(path: Path, outline: dict[str, Any]) -> ProofAtlas | None:
    """Read a regular bounded sidecar; absence leaves existing notebooks alone."""
    try:
        data = read_regular(path.parent / "atlas.json", MAX_BYTES)
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(data)
    except (UnicodeError, ValueError) as error:
        raise ValueError("atlas.json is not valid UTF-8 JSON") from error
    project = outline.get("project")
    commit = project.get("commit") if isinstance(project, dict) else None
    return validate_atlas(raw, outline.get("nodes", []), commit)
