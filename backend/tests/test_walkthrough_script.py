"""Tests for the walkthrough outline checker shipped with the agent skill."""

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills/glitnir-notebooks/scripts/walkthrough.py"
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the script as a separate process and capture its text output."""
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def node(id: str, **fields: Any) -> dict[str, Any]:
    """Return a valid outline node, with the given fields overridden."""
    base: dict[str, Any] = {
        "id": id,
        "label": f"Lemma {id}",
        "kind": "lemma",
        "statement": f"Statement of {id}.",
        "idea": f"Idea of {id}.",
        "uses": [],
        "lean": [],
    }
    base.update(fields)
    return base


def outline(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid outline, with the given top-level keys replaced."""
    base: dict[str, Any] = {
        "format": "glitnir.walkthrough-outline/v1",
        "title": "A paper",
        "project": {
            "root": ".",
            "commit": "abc123",
            "toolchain": "leanprover/lean4:v4.35.0",
            "trusted": False,
        },
        "sources": [{"kind": "paper", "path": "paper.pdf"}],
        "nodes": [node("a"), node("b", uses=["a"])],
    }
    base.update(overrides)
    return base


def write(tmp_path: Path, data: Any) -> Path:
    """Write the data as `outline.json` under tmp_path and return the path."""
    path = tmp_path / "outline.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def snapshot_tree(folder: Path) -> dict[str, bytes]:
    """Return every file under the folder, keyed by relative path."""
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def test_a_minimal_outline_passes(tmp_path: Path) -> None:
    result = run("check", str(write(tmp_path, outline())))
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "change, message",
    [
        ({"format": "other/v1"}, "format"),
        ({"nodes": [node("a"), node("a")]}, "duplicate id a"),
        ({"nodes": [node("a", uses=["zz"])]}, "unknown use zz"),
        (
            {"nodes": [node("a", uses=["b"]), node("b", uses=["a"])]},
            "cycle a → b → a",
        ),
        ({"nodes": [node("a", kind="remark")]}, "kind remark"),
    ],
)
def test_structural_errors_are_all_reported(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    result = run("check", str(write(tmp_path, outline(**change))))
    assert result.returncode == 1 and message in result.stdout
    assert result.stdout.startswith("error: ")


def test_missing_keys_are_reported(tmp_path: Path) -> None:
    data = outline()
    del data["title"]
    del data["project"]["commit"]
    data["sources"] = [{"kind": "wiki"}]
    data["nodes"][0]["lean"] = [{"name": "X"}]
    del data["nodes"][1]["idea"]
    result = run("check", str(write(tmp_path, data)))
    assert result.returncode == 1
    for text in ("title", "commit", "source kind wiki", "path or url", "idea", "line"):
        assert text in result.stdout, result.stdout


def test_unreadable_outline_exits_2(tmp_path: Path) -> None:
    path = tmp_path / "outline.json"
    path.write_text("{not json", encoding="utf-8")
    assert run("check", str(path)).returncode == 2
    path.write_text("[1]", encoding="utf-8")
    assert run("check", str(path)).returncode == 2
    assert run("check", str(tmp_path / "absent.json")).returncode == 2


def test_usage_error_exits_2() -> None:
    assert run().returncode == 2


def test_notebook_option_is_accepted(tmp_path: Path) -> None:
    path = write(tmp_path, outline())
    assert run("check", str(path), "--notebook", "x.lean").returncode == 0


def test_running_leaves_the_skill_folder_unchanged(tmp_path: Path) -> None:
    before = snapshot_tree(SCRIPT.parent.parent)
    run("check", str(write(tmp_path, outline())))
    assert snapshot_tree(SCRIPT.parent.parent) == before


def test_script_parses_as_python_3_10() -> None:
    ast.parse(SCRIPT.read_text(), feature_version=(3, 10))
