"""The preview reads a walkthrough's bounded paper-level outline."""

import json
from pathlib import Path

from glitnir.service.preview import SourcePreviewService


def _outline(nodes: list[dict]) -> dict:
    return {
        "format": "glitnir.walkthrough-outline/v1",
        "title": "A guided argument",
        "overview": "The paper's main line.",
        "project": {
            "root": "../formalization",
            "repository": "https://github.com/example/formalization",
            "path": "lean",
            "commit": "a" * 40,
            "toolchain": "leanprover/lean4:v4.35.0",
            "trusted": "Main.lean",
        },
        "sources": [
            {
                "kind": "paper",
                "url": "https://example.org/paper.pdf",
                "version": "v2",
            }
        ],
        "nodes": nodes,
    }


def _node(identifier: str, **changes) -> dict:
    result = {
        "id": identifier,
        "label": f"Lemma {identifier}",
        "kind": "lemma",
        "section": "§2",
        "pages": "12-13",
        "statement": f"Statement {identifier}.",
        "idea": f"Idea {identifier}.",
        "uses": [],
        "lean": [
            {
                "name": f"Paper.{identifier}",
                "file": "Paper/Main.lean",
                "line": 17,
                "type": ": True",
                "note": "The formal version.",
            }
        ],
    }
    result.update(changes)
    return result


def _snapshot(tmp_path: Path, outline: object | None) -> dict:
    notebook = tmp_path / "Walkthrough.lean"
    notebook.write_text(
        "/-! # Walkthrough -/\n\nnamespace Guide\n"
        "theorem result : True := by\n  trivial\nend Guide\n"
    )
    if outline is not None:
        (tmp_path / "outline.json").write_text(json.dumps(outline))
    service = SourcePreviewService(notebook)
    try:
        return service.snapshot()
    finally:
        service.close()


def test_snapshot_reads_the_bounded_outline_beside_the_walkthrough(
    tmp_path: Path,
) -> None:
    """The snapshot contains renderer data and pinned public links."""
    snapshot = _snapshot(
        tmp_path,
        _outline(
            [
                _node("foundation", kind="definition", lean=[]),
                _node("result", uses=["foundation"], claim="result"),
            ]
        ),
    )
    walkthrough = snapshot["walkthrough"]
    assert (walkthrough["format"], walkthrough["title"], walkthrough["paper"]) == (
        "glitnir.walkthrough-outline/v1",
        "A guided argument",
        "https://example.org/paper.pdf",
    )
    assert walkthrough["problems"] == []
    assert walkthrough["nodes"][1]["claim"] == "result"
    assert walkthrough["nodes"][1]["lean"][0]["href"] == (
        "https://github.com/example/formalization/blob/"
        + "a" * 40
        + "/lean/Paper/Main.lean#L17"
    )


def test_missing_and_malformed_outlines_never_break_the_notebook(
    tmp_path: Path,
) -> None:
    """No outline is silent, while an unsafe outline becomes a diagnostic."""
    assert "walkthrough" not in _snapshot(tmp_path, None)
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    snapshot = _snapshot(
        malformed,
        {
            "format": "wrong",
            "title": "private roadmap",
            "nodes": [_node("secret", statement="do not render this")],
        },
    )
    assert snapshot["walkthrough"]["nodes"] == []
    problems = snapshot["walkthrough"]["problems"]
    assert any("format" in problem for problem in problems)
    assert "private roadmap" not in json.dumps(snapshot["walkthrough"])
    assert "do not render this" not in json.dumps(snapshot["walkthrough"])


def test_unknown_references_and_claims_are_preserved_as_diagnostics(
    tmp_path: Path,
) -> None:
    """Unknown ids stay visible so the renderer can explain missing edges."""
    snapshot = _snapshot(
        tmp_path,
        _outline([_node("result", uses=["missing"], claim="not_in_notebook")]),
    )
    walkthrough = snapshot["walkthrough"]
    assert walkthrough["nodes"][0]["uses"] == ["missing"]
    assert any(
        "unknown statement missing" in problem for problem in walkthrough["problems"]
    )


def test_outline_input_is_bounded(tmp_path: Path) -> None:
    """An oversized adjacent file is never parsed or copied to the snapshot."""
    notebook = tmp_path / "Walkthrough.lean"
    notebook.write_text("theorem result : True := by\n  trivial\n")
    (tmp_path / "outline.json").write_bytes(b" " * (512 * 1024 + 1))
    service = SourcePreviewService(notebook)
    try:
        walkthrough = service.snapshot()["walkthrough"]
    finally:
        service.close()
    assert walkthrough["nodes"] == []
    assert any("too large" in problem for problem in walkthrough["problems"])


def test_outline_refuses_symlinks_without_disclosing_the_target(tmp_path: Path) -> None:
    """A refused adjacent file reports no local target path."""
    private = tmp_path / "private-customer-name"
    private.mkdir()
    target = private / "outline.json"
    target.write_text(json.dumps(_outline([])))
    notebook = tmp_path / "Walkthrough.lean"
    notebook.write_text("theorem result : True := by\n  trivial\n")
    (tmp_path / "outline.json").symlink_to(target)
    service = SourcePreviewService(notebook)
    try:
        walkthrough = service.snapshot()["walkthrough"]
    finally:
        service.close()
    assert walkthrough["nodes"] == []
    assert "private-customer-name" not in json.dumps(walkthrough)
    assert walkthrough["problems"] == ["outline.json could not be read"]


def test_overlong_statement_ids_are_rejected_instead_of_truncated(
    tmp_path: Path,
) -> None:
    """Distinct identities never become a false dependency after bounding."""
    valid = "a" * 128
    snapshot = _snapshot(
        tmp_path,
        _outline([_node(valid), _node("result", uses=[valid + "x"])]),
    )
    walkthrough = snapshot["walkthrough"]
    assert walkthrough["nodes"][1]["uses"] == []
    assert any("invalid statement id" in problem for problem in walkthrough["problems"])


def test_path_only_paper_is_explicitly_local(tmp_path: Path) -> None:
    """The renderer can explain why a valid local paper has no public link."""
    outline = _outline([_node("result")])
    outline["sources"] = [{"kind": "paper", "path": "paper.pdf"}]
    walkthrough = _snapshot(tmp_path, outline)["walkthrough"]
    assert walkthrough["paper"] == ""
    assert walkthrough["paper_local"] is True
