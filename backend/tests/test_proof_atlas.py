"""Proof atlases validate curated links without assigning proof status."""

import copy

import pytest

from glitnir.preview.atlas import validate_atlas


def test_valid_atlas_preserves_curated_routes(atlas: dict) -> None:
    """Validation neither invents dependency observations nor proof statuses."""
    actual = validate_atlas(atlas, [{"id": "result", "lean": []}], "a" * 40)
    assert actual == atlas
    assert "status" not in actual["nodes"][0]


@pytest.mark.parametrize(
    "fault", ["target", "cycle", "commit", "declaration", "duplicate"]
)
def test_invalid_atlas_fails_closed(atlas: dict, fault: str) -> None:
    """Broken references, cycles and source mismatches must not look complete."""
    raw = copy.deepcopy(atlas)
    if fault == "target":
        raw["routes"][0]["supports"]["result"] = ["missing"]
    elif fault == "cycle":
        raw["routes"][0]["supports"]["support"] = ["result"]
    elif fault == "commit":
        raw["commit"] = "b" * 40
    elif fault == "declaration":
        raw["nodes"][0]["declarations"] = ["Missing.theorem"]
    else:
        raw["nodes"].append(raw["nodes"][0])
    with pytest.raises(ValueError):
        validate_atlas(raw, [{"id": "result", "lean": []}], "a" * 40)


def test_loader_retains_statement_map_when_atlas_is_invalid(tmp_path) -> None:
    """A malformed optional atlas is diagnosed without hiding the outline."""
    import json

    from glitnir.preview.walkthrough import load_walkthrough

    notebook = tmp_path / "Guide.lean"
    notebook.write_text("/-! Guide -/")
    outline = {
        "format": "glitnir.walkthrough-outline/v1",
        "title": "Guide",
        "project": {"commit": "a" * 40},
        "nodes": [],
        "sources": [],
    }
    (tmp_path / "outline.json").write_text(json.dumps(outline))
    assert "atlas" not in load_walkthrough(notebook)
    (tmp_path / "atlas.json").write_text('{"format":"wrong"}')
    result = load_walkthrough(notebook)
    assert result["title"] == "Guide"
    assert "atlas" not in result
    assert "atlas.json" in result["problems"][0]


def test_validation_retains_nodes_beyond_graph_display_cap(atlas: dict) -> None:
    """A focused atlas can reach a late node without cutting off the source index."""
    seed = atlas["nodes"][0]
    atlas["nodes"] = [{**seed, "id": f"step{i}"} for i in range(300)]
    atlas["routes"][0].update(root="step299", supports={"step299": ["step298"]})
    actual = validate_atlas(atlas, [{"id": "result", "lean": []}], "a" * 40)
    assert actual["routes"][0]["root"] == "step299"
    assert len(actual["nodes"]) == 300


def test_validation_strips_unsolicited_status_fields(atlas: dict) -> None:
    """Author-provided verdicts cannot cross the atlas wire shape."""
    atlas["nodes"][0]["status"] = "checked"
    result = validate_atlas(atlas, [{"id": "result", "lean": []}], "a" * 40)
    assert "status" not in result["nodes"][0]


@pytest.mark.parametrize(
    "bad_sources",
    [None, [{"id": "result", "lean": 7}], [{"id": "result", "lean": []}] * 2],
)
def test_malformed_outline_bindings_are_diagnostics(atlas: dict, bad_sources) -> None:
    """Invalid source maps cannot crash the optional atlas loader."""
    with pytest.raises(ValueError):
        validate_atlas(atlas, bad_sources, "a" * 40)


def test_unrenderable_source_binding_suppresses_atlas(atlas: dict, tmp_path) -> None:
    """Raw bindings dropped by outline normalization leave only a diagnostic."""
    import json

    from glitnir.preview.walkthrough import load_walkthrough

    node = {
        "id": "result",
        "label": "Result",
        "kind": "theorem",
        "statement": "True",
        "idea": "Idea",
        "uses": [],
        "lean": [{"name": "Paper.result", "file": "P.lean", "line": False}],
    }
    atlas["nodes"][0]["declarations"] = ["Paper.result"]
    outline = {
        "format": "glitnir.walkthrough-outline/v1",
        "title": "Guide",
        "project": {"commit": "a" * 40},
        "nodes": [node],
        "sources": [],
    }
    (tmp_path / "outline.json").write_text(json.dumps(outline))
    (tmp_path / "atlas.json").write_text(json.dumps(atlas))
    result = load_walkthrough(tmp_path / "Guide.lean")
    assert "atlas" not in result
    assert any(
        "atlas declaration unavailable" in problem for problem in result["problems"]
    )


def test_disconnected_arguments_have_actionable_diagnostics(atlas: dict) -> None:
    """Coverage is across all routes, not necessarily every individual route."""
    from glitnir.preview.atlas import atlas_warnings

    atlas["nodes"].append({**atlas["nodes"][0], "id": "orphan"})
    assert atlas_warnings(atlas) == [
        "Arguments unreachable from every route root: orphan"
    ]
    atlas["routes"].append(
        {"id": "other", "label": "Other", "root": "orphan", "supports": {}}
    )
    assert atlas_warnings(atlas) == []


def test_authoring_cli_rejects_unrenderable_binding(atlas: dict, tmp_path) -> None:
    """Authoring must use the same normalized bindings as the preview."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    node = {
        "id": "result",
        "label": "Result",
        "kind": "theorem",
        "uses": [],
        "lean": [{"name": "Paper.result", "file": "P.lean", "line": False}],
    }
    atlas["nodes"][0]["declarations"] = ["Paper.result"]
    outline = {
        "format": "glitnir.walkthrough-outline/v1",
        "title": "Guide",
        "project": {"commit": "a" * 40},
        "nodes": [node],
        "sources": [],
    }
    path = tmp_path / "outline.json"
    path.write_text(json.dumps(outline))
    (tmp_path / "atlas.json").write_text(json.dumps(atlas))
    helper = (
        Path(__file__).resolve().parents[2]
        / "skills/glitnir-notebooks/scripts/proof_atlas.py"
    )
    result = subprocess.run(
        [sys.executable, str(helper), str(path)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "atlas declaration unavailable" in result.stderr
