"""Tests for the walkthrough outline checker shipped with the agent skill."""

import ast
import importlib.util
import json
import os
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


LEAN_SOURCE = """namespace A.B
theorem one : True := trivial
private theorem two (n : Nat) :
    n = n := rfl
end B
@[simp] theorem three : 1 = 1 := rfl
end A
theorem A.four : True := trivial
structure S : Prop where
  x : True
"""


def load_script() -> Any:
    """Import the script as a module, without leaving bytecode behind."""
    spec = importlib.util.spec_from_file_location("walkthrough_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    return module


def git(folder: Path, *args: str) -> str:
    """Run git in the folder with a fixed identity and return its output."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(
        ["git", "-C", str(folder), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return done.stdout.strip()


def make_project(tmp_path: Path, init: bool = True) -> str:
    """Create `proj/Main.lean` and `other/` in tmp_path; return the commit."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "proj/Main.lean").write_text(LEAN_SOURCE, encoding="utf-8")
    (tmp_path / "other/x.txt").write_text("x", encoding="utf-8")
    if not init:
        return ""
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "first")
    return git(tmp_path, "rev-parse", "HEAD")


def entry(**fields: Any) -> dict[str, Any]:
    """Return a correct lean entry for `A.B.one`, with fields overridden."""
    base: dict[str, Any] = {
        "name": "A.B.one",
        "file": "Main.lean",
        "line": 2,
        "type": ": True",
    }
    base.update(fields)
    return base


def lean_outline(commit: str, *entries: dict[str, Any]) -> dict[str, Any]:
    """Return an outline over `proj` whose one node lists the entries."""
    data = outline(nodes=[node("a", lean=list(entries))])
    data["project"]["root"] = "proj"
    data["project"]["commit"] = commit
    return data


def test_declarations_are_found_with_headers_and_namespaces(tmp_path: Path) -> None:
    make_project(tmp_path, init=False)
    found = {
        d.name: d for d in load_script().declarations(tmp_path / "proj", "Main.lean")
    }
    assert list(found) == ["A.B.one", "A.B.two", "A.three", "A.four", "S"]
    assert (found["A.B.one"].type, found["A.B.one"].line) == (": True", 2)
    assert (found["A.B.two"].type, found["A.B.two"].line) == ("(n : Nat) : n = n", 3)
    assert (found["A.three"].type, found["A.three"].line) == (": 1 = 1", 6)
    assert (found["A.four"].type, found["A.four"].line) == (": True", 8)
    assert (found["S"].type, found["S"].line) == (": Prop", 9)
    assert found["A.B.one"].namespaces == ("A", "A.B")
    assert found["A.four"].namespaces == ()


def test_opened_namespaces_are_recorded(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text(
        "open Foo Bar.Baz\ndef x : Nat := 1\n", encoding="utf-8"
    )
    (found,) = load_script().declarations(tmp_path, "F.lean")
    assert found.namespaces == ("Foo", "Bar.Baz")


def test_matching_entries_pass(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    data = lean_outline(commit, entry(), entry(name="S", line=9, type=": Prop"))
    result = run("check", str(write(tmp_path, data)))
    assert result.returncode == 0 and result.stdout == "", result.stdout


def test_entry_at_another_line_is_reported(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    result = run("check", str(write(tmp_path, lean_outline(commit, entry(line=5)))))
    assert result.returncode == 1 and "now at line 2" in result.stdout


def test_entry_with_another_type_shows_both(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    data = lean_outline(commit, entry(type=": False"))
    result = run("check", str(write(tmp_path, data)))
    assert result.returncode == 1
    assert ": False" in result.stdout and ": True" in result.stdout


def test_missing_declaration_and_file_are_errors(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    data = lean_outline(commit, entry(name="A.nope"), entry(file="Gone.lean"))
    result = run("check", str(write(tmp_path, data)))
    assert result.returncode == 1
    assert "A.nope" in result.stdout and "Gone.lean" in result.stdout


def test_lean_entries_are_skipped_after_structural_errors(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    data = lean_outline(commit, entry(name="A.nope"))
    data["title"] = None
    del data["sources"]
    result = run("check", str(write(tmp_path, data)))
    assert result.returncode == 1 and "A.nope" not in result.stdout


def test_commit_in_a_sibling_folder_gives_no_note(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    (tmp_path / "other/y.txt").write_text("y", encoding="utf-8")
    git(tmp_path, "add", "other/y.txt")
    git(tmp_path, "commit", "-q", "-m", "sibling")
    result = run("check", str(write(tmp_path, lean_outline(commit, entry()))))
    assert result.returncode == 0 and result.stdout == "", result.stdout


def test_commit_in_the_project_gives_a_note(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    (tmp_path / "proj/More.lean").write_text("def y : Nat := 1\n", encoding="utf-8")
    git(tmp_path, "add", "proj/More.lean")
    git(tmp_path, "commit", "-q", "-m", "more")
    result = run("check", str(write(tmp_path, lean_outline(commit, entry()))))
    assert result.returncode == 0
    assert f"note: project moved 1 commits since {commit}" in result.stdout


def test_project_outside_git_gives_a_note(tmp_path: Path) -> None:
    make_project(tmp_path, init=False)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "check",
            str(write(tmp_path, lean_outline("abc", entry()))),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
    )
    assert result.returncode == 0
    assert result.stdout.startswith("note: ") and "git" in result.stdout
