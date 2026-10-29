"""Tests for the walkthrough outline checker shipped with the agent skill."""

import ast
import importlib.util
import json
import os
import re
import shutil
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
            "commit": "abc1234",
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
    project = data.get("project") if isinstance(data, dict) else None
    toolchain = project.get("toolchain") if isinstance(project, dict) else None
    root = project.get("root") if isinstance(project, dict) else None
    if isinstance(toolchain, str) and toolchain.strip() and isinstance(root, str):
        project_root = (tmp_path / root).resolve()
        try:
            project_root.relative_to(tmp_path.resolve())
        except ValueError:
            pass
        else:
            project_root.mkdir(parents=True, exist_ok=True)
            (project_root / "lean-toolchain").write_text(toolchain, encoding="utf-8")
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
    assert result.stdout.endswith("ok: 2 nodes, 0 Lean entries\n"), result.stdout


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
    notebook = tmp_path / "x.lean"
    notebook.write_text("import Mathlib\n", encoding="utf-8")
    assert run("check", str(path), "--notebook", str(notebook)).returncode == 0


def test_check_refuses_a_stale_or_missing_project_toolchain_pin(tmp_path: Path) -> None:
    path = write(tmp_path, outline())
    (tmp_path / "lean-toolchain").write_text(
        "leanprover/lean4:v4.33.1\n", encoding="utf-8"
    )
    mismatch = run("check", str(path))
    assert mismatch.returncode == 1
    assert (
        "project toolchain" in mismatch.stdout and "outline declares" in mismatch.stdout
    )

    (tmp_path / "lean-toolchain").unlink()
    missing = run("check", str(path))
    assert missing.returncode == 1
    assert "project lean-toolchain cannot be read" in missing.stdout


def test_running_leaves_the_skill_folder_unchanged(tmp_path: Path) -> None:
    before = snapshot_tree(SCRIPT.parent.parent)
    run("check", str(write(tmp_path, outline())))
    assert snapshot_tree(SCRIPT.parent.parent) == before


def test_script_parses_as_python_3_10() -> None:
    ast.parse(SCRIPT.read_text(), feature_version=(3, 10))


PYTHON_NEEDED = (
    "walkthrough.py needs Python 3.10 or newer; run it with Glitnir's backend "
    'Python or `uv run --no-project --python ">=3.10" python -B walkthrough.py ...`'
)


def test_an_old_python_is_told_what_to_run() -> None:
    problem = load_script().python_problem
    assert problem(3, 9) == problem(2, 7) == PYTHON_NEEDED
    assert problem(3, 10) == problem(3, 12) == problem(4, 0) == ""


def test_the_python_check_runs_before_anything_else() -> None:
    """An older Python reaches the check: no annotation or syntax stops it first."""
    source = SCRIPT.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))
    body = ast.parse(source).body
    assert isinstance(body[0], ast.Expr)  # the docstring
    future = body[1]
    assert isinstance(future, ast.ImportFrom) and future.module == "__future__"
    assert [alias.name for alias in future.names] == ["annotations"]
    imports = (ast.Import, ast.ImportFrom)
    first = next(i for i in range(2, len(body)) if not isinstance(body[i], imports))
    check, problem, exit_ = body[first : first + 3]
    assert isinstance(check, ast.FunctionDef) and check.name == "python_problem"
    assert ast.unparse(problem) == (
        "_PYTHON_PROBLEM = python_problem(sys.version_info.major, "
        "sys.version_info.minor)"
    )
    assert isinstance(exit_, ast.If) and "sys.exit(2)" in ast.unparse(exit_)


def old_python() -> str | None:
    """Return an installed Python older than 3.10, such as macOS's, if any."""
    for candidate in ("python3.9", "/usr/bin/python3"):
        path = shutil.which(candidate)
        if path is None:
            continue
        done = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info < (3, 10))"],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.stdout.strip() == "True":
            return path
    return None


@pytest.mark.skipif(old_python() is None, reason="needs a Python older than 3.10")
def test_an_old_python_exits_2_with_one_line() -> None:
    done = subprocess.run(
        [str(old_python()), "-B", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2 and done.stdout == ""
    assert done.stderr == f"error: {PYTHON_NEEDED}\n", done.stderr


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
    # dataclasses look the module up to read its string annotations
    sys.modules[spec.name] = module
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
    (tmp_path / "proj/lean-toolchain").write_text(
        "leanprover/lean4:v4.35.0", encoding="utf-8"
    )
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
    assert found["A.four"].namespaces == ("A",)
    assert found["S"].namespaces == ()


def test_opened_namespaces_are_not_recorded(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text(
        "open Real Complex\nnamespace P\ndef x : Nat := 1\nend P\n", encoding="utf-8"
    )
    (found,) = load_script().declarations(tmp_path, "F.lean")
    assert found.namespaces == ("P",)


def test_open_namespaces_do_not_license_a_prefix() -> None:
    same = load_script().same_statement
    assert not same(": Real.exp x = 1", ": exp x = 1", (), set())
    known = {"Printed.GroupingLemma"}
    assert same(": Printed.GroupingLemma", ": GroupingLemma", ("Printed",), known)
    assert not same(": GroupingLemma", ": GroupingLemma", ("Printed",), known)


def test_variables_are_tracked_per_scope(tmp_path: Path) -> None:
    source = (
        "namespace A\nvariable (n : Int)\ntheorem t : n = n := rfl\nend A\n"
        "section\nvariable (m : Int)\nend\ntheorem u : True := trivial\n"
        "variable (k : Int)\ntheorem v : True := trivial\n"
    )
    (tmp_path / "F.lean").write_text(source, encoding="utf-8")
    found = {
        d.name: d.variables for d in load_script().declarations(tmp_path, "F.lean")
    }
    assert found == {"A.t": True, "u": False, "v": True}


def test_matching_entries_pass(tmp_path: Path) -> None:
    commit = make_project(tmp_path)
    data = lean_outline(commit, entry(), entry(name="S", line=9, type=": Prop"))
    result = run("check", str(write(tmp_path, data)))
    assert (
        result.returncode == 0 and result.stdout == "ok: 1 nodes, 2 Lean entries\n"
    ), result.stdout


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
    assert (
        result.returncode == 0 and result.stdout == "ok: 1 nodes, 1 Lean entries\n"
    ), result.stdout


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
            str(write(tmp_path, lean_outline("abc1234", entry()))),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
    )
    assert result.returncode == 0
    assert result.stdout.startswith("note: ") and "git" in result.stdout


def names_in(tmp_path: Path, source: str) -> dict[str, tuple[str, ...]]:
    """Scan the source text and return each declaration's name and namespaces."""
    (tmp_path / "F.lean").write_text(source, encoding="utf-8")
    found = load_script().declarations(tmp_path, "F.lean")
    return {d.name: d.namespaces for d in found}


def test_module_modifiers_and_attribute_lines_are_scanned(tmp_path: Path) -> None:
    source = (
        "public theorem t : True := trivial\n"
        "@[expose] public def q : Nat := 1\n"
        "@[simp]\n"
        "theorem s : True := trivial\n"
        "scoped instance : Inhabited Nat := ⟨0⟩\n"
        "local instance i : Inhabited Nat := ⟨0⟩\n"
    )
    assert list(names_in(tmp_path, source)) == ["t", "q", "s", "i"]


@pytest.mark.parametrize(
    "opener",
    ["section", "noncomputable section", "public section", "@[expose] public section"],
)
def test_sections_close_only_themselves(tmp_path: Path, opener: str) -> None:
    source = (
        f"namespace Foo\n{opener}\ndef a : Nat := 1\nend\ndef b : Nat := 1\nend Foo\n"
    )
    assert list(names_in(tmp_path, source)) == ["Foo.a", "Foo.b"]


def test_mutual_blocks_close_only_themselves(tmp_path: Path) -> None:
    source = "namespace N\nmutual\ndef a : Nat := 1\nend\ndef b : Nat := 1\nend N\n"
    assert list(names_in(tmp_path, source)) == ["N.a", "N.b"]


def test_dotted_section_names_pop_one_frame(tmp_path: Path) -> None:
    source = "namespace N\nsection A.B\nend A.B\ndef g : Nat := 1\nend N\n"
    assert list(names_in(tmp_path, source)) == ["N.g"]


def test_namespace_ends_by_full_or_last_component(tmp_path: Path) -> None:
    full = "namespace A.B\nend A.B\ndef x : Nat := 1\n"
    last = "namespace A.B\nend B\ndef x : Nat := 1\n"
    assert list(names_in(tmp_path, full)) == ["x"]
    assert list(names_in(tmp_path, last)) == ["A.x"]


@pytest.mark.parametrize("commit", ["--output=x", "", "ABCDEF1", "abc12", "g" * 8])
def test_a_bad_project_commit_is_rejected_without_running_git(
    tmp_path: Path, commit: str
) -> None:
    make_project(tmp_path)
    before = snapshot_tree(tmp_path / "proj")
    result = run("check", str(write(tmp_path, lean_outline(commit, entry()))))
    assert result.returncode == 1
    assert "7 to 40 lowercase hex characters" in result.stdout
    assert snapshot_tree(tmp_path / "proj") == before
    assert not list(tmp_path.glob("**/*..HEAD"))


def claim_outline(commit: str) -> dict[str, Any]:
    """Return an outline whose node `one` claims `w_one` and cites `A.B.one`."""
    data = lean_outline(commit, entry())
    data["nodes"][0]["id"] = "one"
    data["nodes"][0]["claim"] = "w_one"
    return data


def notebook_run(tmp_path: Path, data: dict[str, Any], text: str) -> Any:
    """Run `check` with a notebook containing the text."""
    notebook = tmp_path / "nb.lean"
    notebook.write_text(text, encoding="utf-8")
    return run("check", str(write(tmp_path, data)), "--notebook", str(notebook))


def test_a_matching_claim_passes(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = "import X\n\ntheorem w_one : True := by\n  exact A.B.one\n"
    result = notebook_run(tmp_path, data, text)
    assert (
        result.returncode == 0
        and result.stdout == "ok: 1 nodes, 1 Lean entries, 1 claims\n"
    ), result.stdout


def test_a_multi_line_claim_header_passes(tmp_path: Path) -> None:
    data = lean_outline(
        make_project(tmp_path),
        entry(name="A.three", line=6, type=": 1 = 1"),
    )
    data["nodes"][0]["claim"] = "w_three"
    text = "theorem w_three :\n    1 = 1 := by\n  exact A.three\n"
    result = notebook_run(tmp_path, data, text)
    assert (
        result.returncode == 0
        and result.stdout == "ok: 1 nodes, 1 Lean entries, 1 claims\n"
    ), result.stdout


def binder_run(tmp_path: Path, binder: str) -> str:
    """Check a claim citing a theorem whose header starts with the binder."""
    (tmp_path / "proj").mkdir()
    header = f"{binder} : n = n"
    (tmp_path / "proj/B.lean").write_text(
        f"theorem t {header} := rfl\n", encoding="utf-8"
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "b")
    data = lean_outline(
        git(tmp_path, "rev-parse", "HEAD"),
        entry(name="t", file="B.lean", line=1, type=header),
    )
    data["nodes"][0]["claim"] = "c"
    text = f"theorem c {header} := by\n  exact t\n"
    return str(notebook_run(tmp_path, data, text).stdout)


@pytest.mark.parametrize("binder", ["(n : Nat)", "⦃n : Nat⦄"])
def test_a_claim_citing_explicit_arguments_is_an_error(
    tmp_path: Path, binder: str
) -> None:
    out = binder_run(tmp_path, binder)
    assert "claim c cites t, which takes explicit arguments" in out, out


@pytest.mark.parametrize("binder", ["{n : Nat}", "{n : Nat} [inst : Inhabited Nat]"])
def test_implicit_and_instance_binders_are_allowed(tmp_path: Path, binder: str) -> None:
    out = binder_run(tmp_path, binder)
    assert out == "ok: 1 nodes, 1 Lean entries, 1 claims\n", out


def test_explicit_binders_are_found_only_before_the_type() -> None:
    explicit = load_script().takes_explicit_arguments
    assert explicit("(n : Nat) : n = n")
    assert explicit("{m : Nat} ⦃n : Nat⦄ : n = m")
    assert not explicit(": ∀ (n : Nat), n = n")
    assert not explicit("{f : (n : Nat) → Nat} [inst : Inhabited Nat] : f = f")


def test_same_statement_resolves_namespaces() -> None:
    same = load_script().same_statement
    known = {"A.B.Foo", "A.Foo", "C.Foo"}
    assert same(": A.B.Foo", ": Foo", ("A", "A.B"), known)
    assert not same(": A.Foo", ": Foo", ("A", "A.B"), known)
    assert not same(": Foo", ": Foo", ("A", "A.B"), known)
    assert not same(": C.Foo", ": Foo", ("A",), known)
    assert same(": A.Foo x", ": A.Foo x", (), known)
    assert not same(": Foo x", ": Foo", ("A",), known)
    assert not same(": Foo y", ": Foo x", (), known)
    assert same(": «a.b».c", ": «a.b».c", (), known)


def test_resolution_follows_lean() -> None:
    resolve = load_script().resolve
    known = {"A.cost", "A.B.cost", "A.B.t", "cost", "A.size"}
    assert resolve("cost", ("A", "A.B"), known) == "A.B.cost"
    assert resolve("cost", ("A",), known) == "A.cost"
    assert resolve("cost", (), known) == "cost"
    assert resolve("size", ("A", "A.B"), known) == "A.size"
    assert resolve("B.cost", ("A",), known) == "A.B.cost"
    assert resolve("B.t.symm", ("A",), known) == "A.B.t.symm"
    assert resolve("Nat.succ", ("A",), known) == "Nat.succ"


def test_bound_names_are_found() -> None:
    bound = load_script().bound_names
    assert bound("{n m : Nat} [inst : Inhabited Nat] : n = m") == {"n", "m", "inst"}
    assert bound("{n} : ∀ k ∈ s, ∃ (j : Nat) (i), j = k + i") >= {"n", "k", "j", "i"}
    assert bound(": fun x => x = f") == {"x"}
    assert "Inhabited" not in bound("[Inhabited Nat] : True")
    assert "a" not in bound(": s = {a}")


@pytest.mark.parametrize(
    "text",
    [
        "theorem w_one : True := A.B.one\n",
        "theorem w_one : True := by\n  exact A.B.one\n  rfl\n",
        "theorem w_one : True := by\n  simp\n",
        "theorem w_one : True\n",
    ],
)
def test_a_claim_must_be_by_exact(tmp_path: Path, text: str) -> None:
    data = claim_outline(make_project(tmp_path))
    result = notebook_run(tmp_path, data, text)
    assert result.returncode == 1
    assert "claim w_one is not 'by exact <project theorem>'" in result.stdout


def test_a_claim_must_cite_its_nodes_theorem(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = "theorem w_one : 1 = 1 := by\n  exact A.three\n"
    result = notebook_run(tmp_path, data, text)
    assert result.returncode == 1 and "A.three" in result.stdout


def test_a_claim_with_another_statement_shows_both(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    result = notebook_run(
        tmp_path, data, "theorem w_one : False := by\n  exact A.B.one\n"
    )
    assert result.returncode == 1
    assert ": False" in result.stdout and ": True" in result.stdout


def test_an_unlisted_claim_is_an_error(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = "theorem w_one : True := by\n  exact A.B.one\n"
    text += "theorem w_other : True := by\n  exact A.B.one\n"
    result = notebook_run(tmp_path, data, text)
    assert result.returncode == 1 and "w_other" in result.stdout


def test_a_missing_claim_is_an_error(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    result = notebook_run(tmp_path, data, "import X\n")
    assert result.returncode == 1 and "w_one" in result.stdout


def test_an_unreadable_notebook_is_an_error(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    result = run(
        "check", str(write(tmp_path, data)), "--notebook", str(tmp_path / "no.lean")
    )
    assert result.returncode == 1 and "no.lean" in result.stdout


def test_notebook_is_skipped_after_earlier_errors(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    data["nodes"][0]["lean"][0]["type"] = ": False"
    result = notebook_run(tmp_path, data, "import X\n")
    assert result.returncode == 1 and "w_one" not in result.stdout


def test_a_claim_citing_a_declaration_with_variables_is_an_error(
    tmp_path: Path,
) -> None:
    commit = make_project(tmp_path)
    (tmp_path / "proj/V.lean").write_text(
        "namespace A\nvariable (n : Int)\ntheorem t : n = n := rfl\nend A\n"
        "section\nvariable (m : Int)\nend\ntheorem u : True := trivial\n",
        encoding="utf-8",
    )
    git(tmp_path, "add", "proj/V.lean")
    git(tmp_path, "commit", "-q", "-m", "v")
    data = lean_outline(
        commit,
        entry(name="A.t", file="V.lean", line=3, type=": n = n"),
        entry(name="u", file="V.lean", line=8, type=": True"),
    )
    data["nodes"][0]["claim"] = "c"
    data["nodes"].append(node("b", claim="d", lean=[data["nodes"][0]["lean"][1]]))
    data["nodes"][0]["lean"] = data["nodes"][0]["lean"][:1]
    text = (
        "variable (n : Nat)\ntheorem c : n = n := by\n  exact A.t\n"
        "theorem d : True := by\n  exact u\n"
    )
    out = notebook_run(tmp_path, data, text).stdout
    assert "claim c cites A.t, which depends on section variables" in out
    assert "claim d" not in out


ONLY = "walkthrough must contain only imports, prose, namespaces and claims"


@pytest.mark.parametrize(
    "line, word",
    [
        ("open Real", "open"),
        ("variable (n : Nat)", "variable"),
        ("def x := 1", "def"),
        ("@[simp] def x := 1", "def"),
        ("private noncomputable def x := 1", "def"),
        ("set_option autoImplicit true", "set_option"),
        ('local notation "x" => 1', "local"),
        ("example : 1 = 2 := by sorry", "example"),
        ("export A (size)", "export"),
        ("alias foo := A.B.one", "alias"),
        ("macro_rules | `(x) => `(1)", "macro_rules"),
        ('notation3 "x" => 1', "notation3"),
        ("irreducible_def x : Nat := 1", "irreducible_def"),
        ("#eval 1", "#eval"),
        ("#print A.B.one", "#print"),
        ("noncomputable section", "noncomputable"),
        ("@[simp]", "@[simp]"),
    ],
)
def test_the_notebook_holds_only_allowed_commands(
    tmp_path: Path, line: str, word: str
) -> None:
    data = claim_outline(make_project(tmp_path))
    text = f"import X\n{line}\ntheorem w_one : True := by\n  exact A.B.one\n"
    out = notebook_run(tmp_path, data, text).stdout
    assert f"{ONLY}: line 2: {word} in " in out, out
    assert not any(w in out for w in STRUCTURAL_WORDS), out


@pytest.mark.parametrize(
    "text, number, word",
    [
        ("/-- doc -/ theorem bogus : 1 = 2 := by sorry\n", 2, "theorem after -/"),
        ("/-! prose\nmore -/ example : 1 = 2 := by sorry\n", 3, "example after -/"),
        ("namespace N\n  theorem bogus : 1 = 2 := by sorry\nend N\n", 3, "theorem"),
        ("#print axioms w_one\n  #eval 1\n", 3, "#eval"),
    ],
)
def test_code_hidden_after_comments_or_indented_is_an_error(
    tmp_path: Path, text: str, number: int, word: str
) -> None:
    data = claim_outline(make_project(tmp_path))
    claim = "theorem w_one : True := by\n  exact A.B.one\n"
    out = notebook_run(tmp_path, data, "import X\n" + text + claim).stdout
    assert f"{ONLY}: line {number}: {word} in " in out, out


def test_a_plain_notebook_passes(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = (
        "import Mathlib\n/-! prose\ndef not_code := 1\n-/\n-- open Real\n"
        "  -- an indented comment\n/- a comment -/\nsection\nnamespace N\n"
        "/-- doc -/\ntheorem w_one : True := by\n  exact A.B.one\n"
        "end N\nend\n#print axioms w_one\n"
    )
    result = notebook_run(tmp_path, data, text)
    assert (
        result.returncode == 0
        and result.stdout == "ok: 1 nodes, 1 Lean entries, 1 claims\n"
    ), result.stdout


def test_direct_uses_drops_a_use_reached_through_another() -> None:
    nodes = [node("a"), node("b", uses=["a"]), node("c", uses=["a", "b"])]
    assert load_script().direct_uses(nodes) == {"c": ["b"], "b": ["a"], "a": []}


def test_map_svg_has_a_group_per_node_and_an_edge_per_direct_use() -> None:
    nodes = [node("a"), node("b", uses=["a"]), node("c", uses=["a", "b"])]
    svg = load_script().map_svg(outline(nodes=nodes))
    assert svg.count("<g data-id=") == 3
    for node_id in "abc":
        assert f'<g data-id="{node_id}"' in svg
    assert svg.count("<path d=") == 2 + 1  # two edges and the arrow head


def test_map_svg_escapes_outline_text() -> None:
    data = outline(title="T <b> & co", nodes=[node("a", label="L <i>")])
    svg = load_script().map_svg(data)
    assert "<b>" not in svg and "<i>" not in svg
    assert "T &lt;b&gt; &amp; co" in svg


def map_page(tmp_path: Path, data: dict[str, Any], links: str = "local") -> str:
    """Run `map` on the outline and return the page it wrote."""
    result = run("map", str(write(tmp_path, data)), "--links", links)
    assert result.returncode == 0, result.stdout + result.stderr
    return (tmp_path / "map.html").read_text(encoding="utf-8")


def test_map_html_escapes_outline_text(tmp_path: Path) -> None:
    text = 'x < y & "z" </script>'
    page = map_page(tmp_path, outline(nodes=[node("a", statement=text)]))
    assert text not in page and "x < y" not in page
    assert page.count("<script type=") == 1
    # The three scripts are the two KaTeX ones, the data and the page code.
    assert page.count("</script>") == page.count("<script")
    start = page.index('<script type="application/json"')
    data = page[start : page.index("</script>", start)]
    assert "<" not in data[data.index(">") + 1 :]
    raw = data[data.index(">") + 1 :]
    assert json.loads(raw)["nodes"][0]["statement"] == text


KATEX_INTEGRITY = {
    "katex.min.css": "sha512-fHwaWebuwA7NSF5Qg/af4UeDx9XqUpYpOGgubo3yWu+b2IQR4UeQwbb42"
    "Ti7gVAjNtVoI/I9TEoYeu9omwcC6g==",
    "katex.min.js": "sha512-LQNxIMR5rXv7o+b1l8+N1EZMfhG7iFZ9HhnbJkTp4zjNr5Wvst75AqUeFD"
    "xeRUa7l5vEDyUiAip//r+EFLLCyA==",
    "contrib/auto-render.min.js": "sha512-iWiuBS5nt6r60fCz26Nd0Zqe0nbk1ZTIQbl3Kv7kYsX+"
    "yKMUFHzjaH2+AnM6vp2Xs+gNmaBAVWJjSmuPw76Efg==",
}


def test_map_html_loads_only_katex_from_cdnjs(tmp_path: Path) -> None:
    page = map_page(tmp_path, outline())
    base = "https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/"
    tags = re.findall(r"<(?:link|script)\b[^>]*https://[^>]*>", page)
    assert len(tags) == 3 and page.count("https://") == 3, tags
    for tag, (file, integrity) in zip(tags, KATEX_INTEGRITY.items()):
        assert f'"{base}{file}"' in tag, tag
        assert f'integrity="{integrity}"' in tag, tag
        assert 'crossorigin="anonymous"' in tag, tag


def link_outline(tmp_path: Path) -> dict[str, Any]:
    """Return an outline with a Lean entry, a paper and public coordinates."""
    entry_ = {"name": "X", "file": "eth/X.lean", "line": 3, "type": ": True"}
    data = outline(nodes=[node("a", pages="12-14", lean=[entry_])])
    data["project"].update(
        {
            "root": "proj",
            "commit": "abc1234",
            "repository": "https://github.com/o/r",
            "path": "sub",
        }
    )
    data["sources"] = [
        {"kind": "paper", "path": "Paper.PDF", "url": "https://example.org/p.pdf"}
    ]
    return data


def test_local_links_use_vscode_and_file(tmp_path: Path) -> None:
    page = map_page(tmp_path, link_outline(tmp_path))
    root = (tmp_path / "proj").resolve()
    assert f"vscode://file{root}/eth/X.lean:3" in page
    assert (tmp_path / "Paper.PDF").resolve().as_uri() + "#page=12" in page


def test_public_links_hold_no_local_path(tmp_path: Path) -> None:
    page = map_page(tmp_path, link_outline(tmp_path), "public")
    assert str(tmp_path) not in page
    assert "vscode://" not in page and "file://" not in page
    assert "https://github.com/o/r/blob/abc1234/sub/eth/X.lean#L3" in page
    assert "https://example.org/p.pdf#page=12" in page


def test_public_links_are_omitted_when_they_cannot_be_built(tmp_path: Path) -> None:
    data = link_outline(tmp_path)
    del data["project"]["repository"]
    data["sources"] = [{"kind": "paper", "path": "paper.pdf"}]
    page = map_page(tmp_path, data, "public")
    assert "/blob/" not in page and "#L3" not in page
    assert "paper.pdf" not in page


def test_page_links_need_a_pdf_and_pages(tmp_path: Path) -> None:
    data = link_outline(tmp_path)
    data["sources"] = [{"kind": "paper", "url": "https://example.org/p.html"}]
    page = map_page(tmp_path, data, "public")
    assert "https://example.org/p.html" in page and "#page=" not in page


def test_map_writes_both_files_under_out_and_nothing_elsewhere(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    path = write(folder, outline())
    out = tmp_path / "out"
    before = snapshot_tree(folder)
    result = run("map", str(path), "--out", str(out))
    assert result.returncode == 0, result.stdout
    assert snapshot_tree(folder) == before
    assert sorted(snapshot_tree(out)) == ["assets/map.svg", "map.html"]
    assert str(out / "map.html") in result.stdout
    assert str(out / "assets/map.svg") in result.stdout


def test_map_defaults_to_the_outlines_folder(tmp_path: Path) -> None:
    map_page(tmp_path, outline())
    assert (tmp_path / "assets/map.svg").is_file()


def test_map_refuses_an_outline_with_errors(tmp_path: Path) -> None:
    data = outline(nodes=[node("a", uses=["zz"])])
    result = run("map", str(write(tmp_path, data)))
    assert result.returncode == 1 and "unknown use zz" in result.stdout
    assert not (tmp_path / "map.html").exists()


def test_map_of_an_unreadable_outline_exits_2(tmp_path: Path) -> None:
    path = tmp_path / "outline.json"
    path.write_text("{", encoding="utf-8")
    assert run("map", str(path)).returncode == 2


SKILL = SCRIPT.parent.parent
STRUCTURAL_WORDS = ("format", "duplicate", "unknown use", "cycle", "kind")


def first_json_block(path: Path) -> str:
    """Return the text of the first fenced `json` block in a Markdown file."""
    text = path.read_text(encoding="utf-8")
    start = text.index("```json\n") + len("```json\n")
    return text[start : text.index("\n```", start)]


def test_the_documented_example_outline_is_valid(tmp_path: Path) -> None:
    example = json.loads(first_json_block(SKILL / "references/walkthrough-outline.md"))
    assert not (tmp_path / example["project"]["root"]).exists()
    result = run("check", str(write(tmp_path, example)))
    assert result.returncode != 2, result.stderr
    for line in result.stdout.splitlines():
        assert not any(word in line for word in STRUCTURAL_WORDS), line
        assert line.startswith("note: ") or "cannot be read" in line, line


def documented_commands() -> list[list[str]]:
    """Return the arguments of every helper command the skill documents."""
    commands = []
    for path in [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]:
        for line in path.read_text(encoding="utf-8").splitlines():
            _, found, rest = line.partition("python -B scripts/walkthrough.py ")
            words = rest.split("`")[0].split()
            if found and words:
                commands.append(words)
    return commands


UV_RUN = 'uv run --no-project --python ">=3.10" python -B scripts/walkthrough.py'


def test_the_skill_names_a_python_that_can_run_the_helper() -> None:
    """The backend's Python or uv, never the system `python3` (3.9 on macOS)."""
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]
    }
    for name, text in texts.items():
        assert "python3" not in text, name
    for name in ("SKILL.md", "walkthroughs.md"):
        assert "glitnir.pythonPath" in texts[name] and UV_RUN in texts[name], name
    assert "python3" not in SCRIPT.read_text(encoding="utf-8")


def test_every_documented_command_exists() -> None:
    commands = documented_commands()
    assert {words[0] for words in commands} == {"check", "map"}
    for words in commands:
        usage = run(words[0], "--help").stdout
        for word in words[1:]:
            if word.startswith("--"):
                assert word in usage, (word, words)


def split_outline(commit: str) -> dict[str, Any]:
    """Return an outline whose three nodes claim ca, cb and cc from `A.B.one`."""
    data = lean_outline(commit)
    data["nodes"] = [node(i, claim=f"c{i}", lean=[entry()]) for i in "abc"]
    return data


def claim_text(*names: str) -> str:
    """Return notebook text restating `A.B.one` under each name."""
    return "".join(f"theorem {n} : True := by\n  exact A.B.one\n" for n in names)


def notebooks_run(tmp_path: Path, data: dict[str, Any], *texts: str) -> Any:
    """Run `check` with one `--notebook` per text."""
    args = ["check", str(write(tmp_path, data))]
    for index, text in enumerate(texts):
        part = tmp_path / f"Part{index}.lean"
        part.write_text(text, encoding="utf-8")
        args += ["--notebook", str(part)]
    return run(*args)


def test_claims_may_be_split_across_notebooks(tmp_path: Path) -> None:
    data = split_outline(make_project(tmp_path))
    both = notebooks_run(tmp_path, data, claim_text("ca", "cb"), claim_text("cc"))
    assert (
        both.returncode == 0
        and both.stdout == "ok: 3 nodes, 3 Lean entries, 3 claims\n"
    ), both.stdout
    one = notebooks_run(tmp_path, data, claim_text("ca"), claim_text("cc"))
    assert one.returncode == 1 and "claim cb is not in" in one.stdout
    assert "ca" not in one.stdout and "cc" not in one.stdout


def test_a_claim_in_two_notebooks_is_an_error(tmp_path: Path) -> None:
    data = split_outline(make_project(tmp_path))
    result = notebooks_run(
        tmp_path, data, claim_text("ca", "cb"), claim_text("cb", "cc")
    )
    assert result.returncode == 1
    assert "claim cb is in both" in result.stdout
    assert "Part0.lean" in result.stdout and "Part1.lean" in result.stdout


def test_each_notebook_must_not_define_or_open(tmp_path: Path) -> None:
    data = split_outline(make_project(tmp_path))
    result = notebooks_run(
        tmp_path, data, claim_text("ca", "cb"), "open Real\n" + claim_text("cc")
    )
    assert result.returncode == 1
    assert "Part1.lean" in result.stdout and "line 1: open" in result.stdout


def qualified_project(tmp_path: Path) -> str:
    """Create a project declaring `Q.P.X` and `Q.P.t : X`; commit it.

    A `.lake` package declares `Q.X`, which the scan must not see.
    """
    (tmp_path / "proj/P").mkdir(parents=True)
    (tmp_path / "proj/P/Defs.lean").write_text(
        "namespace Q.P\ndef X : Prop := True\nend Q.P\n", encoding="utf-8"
    )
    (tmp_path / "proj/Main.lean").write_text(
        "namespace Q.P\ntheorem t : X := trivial\nend Q.P\n", encoding="utf-8"
    )
    (tmp_path / "proj/.lake/packages/m").mkdir(parents=True)
    (tmp_path / "proj/.lake/packages/m/M.lean").write_text(
        "namespace Q\ndef X : Prop := True\nend Q\n", encoding="utf-8"
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "proj/P", "proj/Main.lean")
    git(tmp_path, "commit", "-q", "-m", "q")
    return git(tmp_path, "rev-parse", "HEAD")


def qualified_run(tmp_path: Path, written: str) -> Any:
    """Check a claim restating `Q.P.t` as `: <written>`."""
    data = lean_outline(
        qualified_project(tmp_path),
        entry(name="Q.P.t", file="Main.lean", line=2, type=": X"),
    )
    data["nodes"][0]["claim"] = "c"
    return notebook_run(tmp_path, data, f"theorem c : {written} := by\n  exact Q.P.t\n")


def test_a_qualification_the_project_declares_passes(tmp_path: Path) -> None:
    result = qualified_run(tmp_path, "Q.P.X")
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("written", ["Q.X", "X"])
def test_a_name_lean_does_not_resolve_to_the_projects_is_an_error(
    tmp_path: Path, written: str
) -> None:
    result = qualified_run(tmp_path, written)
    assert result.returncode == 1
    assert "there X means Q.P.X" in result.stdout, result.stdout


def test_the_map_legend_explains_dashed_edges() -> None:
    svg = load_script().map_svg(outline())
    assert 'stroke-dasharray="5 4"' in svg and "used by an appendix" in svg


def test_map_labels_keep_28_characters() -> None:
    short = load_script()._short
    assert short("x" * 28) == "x" * 28
    assert short("x" * 29) == "x" * 27 + "…"


RESOLVE_SOURCE = """def size := 1
namespace A
def size := 2
def cost := 5
def n := 3
namespace B
def cost := 7
theorem t_inner : cost = 7 := rfl
theorem t_size : size = 2 := rfl
theorem t_bound {n : Nat} : n = n := rfl
end B
end A
def cost := 9
theorem A.four : cost = 5 := rfl
namespace Printed
def GroupingLemma : Prop := True
theorem groupingLemma : GroupingLemma := trivial
end Printed
"""
RESOLVE_LINES = {
    "A.B.t_inner": (8, ": cost = 7"),
    "A.B.t_size": (9, ": size = 2"),
    "A.B.t_bound": (10, "{n : Nat} : n = n"),
    "A.four": (14, ": cost = 5"),
    "Printed.groupingLemma": (17, ": GroupingLemma"),
}


def resolve_run(tmp_path: Path, target: str, claim: str) -> Any:
    """Check the claim line `theorem c ...` citing the target in RESOLVE_SOURCE."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj/R.lean").write_text(RESOLVE_SOURCE, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "proj/R.lean")
    git(tmp_path, "commit", "-q", "-m", "r")
    line, type_ = RESOLVE_LINES[target]
    data = lean_outline(
        git(tmp_path, "rev-parse", "HEAD"),
        entry(name=target, file="R.lean", line=line, type=type_),
    )
    data["nodes"][0]["claim"] = "c"
    text = f"namespace W\n{claim} := by\n  exact {target}\nend W\n"
    return notebook_run(tmp_path, data, text)


@pytest.mark.parametrize(
    "target, claim, means",
    [
        ("A.B.t_inner", "theorem c : A.cost = 7", "there cost means A.B.cost"),
        ("A.B.t_inner", "theorem c : cost = 7", "there cost means A.B.cost"),
        ("A.B.t_size", "theorem c : size = 2", "there size means A.size"),
        ("A.four", "theorem c : cost = 5", "there cost means A.cost"),
        ("Printed.groupingLemma", "theorem c : GroupingLemma", "Printed.Grouping"),
    ],
)
def test_a_name_lean_resolves_elsewhere_is_an_error(
    tmp_path: Path, target: str, claim: str, means: str
) -> None:
    result = resolve_run(tmp_path, target, claim)
    assert result.returncode == 1 and means in result.stdout, result.stdout


@pytest.mark.parametrize(
    "target, claim",
    [
        ("A.B.t_inner", "theorem c : A.B.cost = 7"),
        ("A.B.t_size", "theorem c : A.size = 2"),
        ("A.B.t_bound", "theorem c {n : Nat} : n = n"),
        ("A.four", "theorem c : A.cost = 5"),
        ("Printed.groupingLemma", "theorem c : Printed.GroupingLemma"),
    ],
)
def test_the_name_lean_resolves_to_passes(
    tmp_path: Path, target: str, claim: str
) -> None:
    result = resolve_run(tmp_path, target, claim)
    assert result.stdout == "ok: 1 nodes, 1 Lean entries, 1 claims\n", result.stdout


def test_a_walkthrough_namespace_the_project_uses_is_an_error(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = "namespace A\ntheorem w_one : True := by\n  exact A.B.one\nend A\n"
    out = notebook_run(tmp_path, data, text).stdout
    assert "line 1: namespace A is a namespace of the project" in out, out


def test_a_claim_writing_the_name_of_a_claim_is_an_error(tmp_path: Path) -> None:
    data = split_outline(make_project(tmp_path))
    data["nodes"][1]["lean"] = [entry(name="A.three", line=6, type=": 1 = 1")]
    text = (
        "theorem ca : True := by\n  exact A.B.one\n"
        "theorem cb : 1 = 1 := by\n  exact A.three\n"
        "theorem cc : True := by\n  exact A.B.one\n"
    )
    assert notebooks_run(tmp_path, data, text).returncode == 0
    data["nodes"][2]["lean"] = [entry(name="S", line=9, type=": Prop")]
    text = text.replace("theorem cc : True", "theorem cc : Prop").replace(
        "exact A.B.one\n", "exact S\n"
    )
    data["nodes"][0]["claim"] = "Prop"
    text = text.replace("theorem ca", "theorem Prop", 1)
    out = notebooks_run(tmp_path, data, text).stdout
    assert "claim cc writes Prop, which names a claim of the walkthrough" in out, out


def cite_run(tmp_path: Path, source: str, type_: str = ": 1 = 2") -> str:
    """Check claim c restating `t` declared on line 1 of the source."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj/T.lean").write_text(source, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "proj/T.lean")
    git(tmp_path, "commit", "-q", "-m", "t")
    data = lean_outline(
        git(tmp_path, "rev-parse", "HEAD"),
        entry(name="t", file="T.lean", line=1, type=type_),
    )
    data["nodes"][0]["claim"] = "c"
    text = f"theorem c {type_} := by\n  exact t\n"
    return str(notebook_run(tmp_path, data, text).stdout)


@pytest.mark.parametrize(
    "source, message",
    [
        ("axiom t : 1 = 2\n", "claim c cites t, which is an axiom, not a theorem"),
        ("def t : 1 = 2 := sorry\n", "claim c cites t, which is a def, not a theorem"),
        (
            "theorem t : 1 = 2 := by sorry\n",
            "claim c cites t, whose proof contains sorry",
        ),
        (
            "theorem t : 1 = 2 := by admit\n",
            "claim c cites t, whose proof contains admit",
        ),
        (
            "lemma t : 1 = 2 := by\n  simp\n  · sorry\n",
            "claim c cites t, whose proof contains sorry",
        ),
        (
            "theorem t : 1 = 2 :=\n  sorry\n",
            "claim c cites t, whose proof contains sorry",
        ),
    ],
)
def test_a_claim_must_cite_a_proved_theorem(
    tmp_path: Path, source: str, message: str
) -> None:
    out = cite_run(tmp_path, source)
    assert message in out, out


def test_sorry_in_strings_comments_and_names_is_not_a_proof_gap(tmp_path: Path) -> None:
    source = (
        "theorem t : True := by -- sorry\n"
        "  /- admit\n  sorry -/\n"
        '  have : "sorry" = "sorry" := rfl\n'
        "  have : '\"' = '\"' := rfl\n"
        "  exact no_sorry.admit_free\n"
    )
    out = cite_run(tmp_path, source, ": True")
    assert out == "ok: 1 nodes, 1 Lean entries, 1 claims\n", out


@pytest.mark.parametrize("line", ["3", 0, -1, True, 1.5])
def test_a_lean_line_must_be_a_positive_integer(tmp_path: Path, line: Any) -> None:
    data = outline(nodes=[node("a", lean=[entry(line=line)])])
    result = run("check", str(write(tmp_path, data)))
    assert result.returncode == 1
    assert "node a lean 0: line must be a positive integer" in result.stdout


def test_a_claim_on_two_nodes_is_an_error(tmp_path: Path) -> None:
    data = outline(nodes=[node("a", claim="w"), node("b", claim="w")])
    result = run("check", str(write(tmp_path, data)))
    assert result.returncode == 1
    assert "claim w is listed on nodes a and b" in result.stdout


def source_run(
    tmp_path: Path, source: str, target: str, line: int, type_: str, notebook: str
) -> Any:
    """Check the notebook against `proj/R.lean`, its one node citing the target."""
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj/R.lean").write_text(source, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "proj/R.lean")
    git(tmp_path, "commit", "-q", "-m", "r")
    data = lean_outline(
        git(tmp_path, "rev-parse", "HEAD"),
        entry(name=target, file="R.lean", line=line, type=type_),
    )
    claim = notebook.split("theorem ", 1)[1].split()[0]
    data["nodes"][0]["claim"] = claim
    return notebook_run(tmp_path, data, notebook)


def in_w(claim: str, target: str) -> str:
    """Return notebook text holding the claim, proved by the target, in `W`."""
    return f"import X\nnamespace W\n{claim} := by\n  exact {target}\nend W\n"


PROTECTED_SOURCE = (
    "def cost := 9\nnamespace A\nprotected def cost := 5\n"
    "theorem t : cost = 9 := rfl\nend A\n"
)
OK_ONE = "ok: 1 nodes, 1 Lean entries, 1 claims\n"


def test_protected_declarations_are_recorded(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text(PROTECTED_SOURCE, encoding="utf-8")
    found = {d.name: d for d in load_script().declarations(tmp_path, "F.lean")}
    assert found["A.cost"].protected and not found["cost"].protected


def test_a_short_name_never_means_a_protected_declaration() -> None:
    resolve = load_script().resolve
    known = {"cost", "A.cost", "A.B.cost"}
    assert resolve("cost", ("A",), known, {"A.cost"}) == "cost"
    assert resolve("cost.succ", ("A",), known, {"A.cost"}) == "cost.succ"
    assert resolve("A.cost", ("A",), known, {"A.cost"}) == "A.cost"
    assert resolve("cost", ("A", "A.B"), known, {"A.B.cost"}) == "A.cost"
    assert resolve("B.cost", ("A",), known, {"A.B.cost"}) == "A.B.cost"


def test_a_claim_naming_a_protected_declaration_is_an_error(tmp_path: Path) -> None:
    claim = in_w("theorem c : A.cost = 9", "A.t")
    result = source_run(tmp_path, PROTECTED_SOURCE, "A.t", 4, ": cost = 9", claim)
    assert result.returncode == 1 and "differs from A.t" in result.stdout
    assert "there cost means" not in result.stdout, result.stdout


def test_a_claim_skipping_a_protected_declaration_passes(tmp_path: Path) -> None:
    claim = in_w("theorem c : cost = 9", "A.t")
    result = source_run(tmp_path, PROTECTED_SOURCE, "A.t", 4, ": cost = 9", claim)
    assert result.stdout == OK_ONE, result.stdout


def test_an_equal_protected_value_does_not_excuse_its_name(tmp_path: Path) -> None:
    source = PROTECTED_SOURCE.replace("9", "5")
    claim = in_w("theorem c : A.cost = 5", "A.t")
    result = source_run(tmp_path, source, "A.t", 4, ": cost = 5", claim)
    assert result.returncode == 1 and "differs from A.t" in result.stdout


@pytest.mark.parametrize(
    "line, word",
    [
        ("import Y example : (1 : Nat) = 2 := by sorry", "example after import"),
        ("import Y open Nat", "open after import"),
        ("import A.B C", "C after import"),
        ("import", "import"),
        (
            "#print axioms w_one example : 1 = 2 := by sorry",
            "example after #print axioms",
        ),
        ("#print axioms w_one A.B.one", "A.B.one after #print axioms"),
        ("#print axioms", "#print axioms"),
    ],
)
def test_an_import_or_print_line_holds_nothing_else(
    tmp_path: Path, line: str, word: str
) -> None:
    data = claim_outline(make_project(tmp_path))
    text = f"import X\n{line}\ntheorem w_one : True := by\n  exact A.B.one\n"
    out = notebook_run(tmp_path, data, text).stdout
    assert f"{ONLY}: line 2: {word} in " in out, out
    assert not any(w in out for w in STRUCTURAL_WORDS), out


def test_import_and_print_lines_may_end_in_a_comment(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = (
        "import A.B -- note\nimport C\ntheorem w_one : True := by\n  exact A.B.one\n"
        "#print axioms w_one -- note\n#print axioms W.w_one\n"
    )
    result = notebook_run(tmp_path, data, text)
    assert result.stdout == OK_ONE, result.stdout


BOUND_SOURCE = (
    "def n := 4\nnamespace A\ndef n := 3\n"
    "theorem t : (∃ n : Nat, n = 3) ∧ n = 3 := ⟨⟨3, rfl⟩, rfl⟩\nend A\n"
)


@pytest.mark.parametrize(
    "claim",
    [
        "theorem c : (∃ n : Nat, n = 3) ∧ n = 3",
        "theorem c : (∃ n : Nat, n = 3) ∧ A.n = 3",
    ],
)
def test_a_name_both_bound_and_declared_is_an_error(tmp_path: Path, claim: str) -> None:
    type_ = ": (∃ n : Nat, n = 3) ∧ n = 3"
    result = source_run(tmp_path, BOUND_SOURCE, "A.t", 4, type_, in_w(claim, "A.t"))
    assert result.returncode == 1
    assert "claim c binds and uses n; explain it in prose instead" in result.stdout


def test_a_name_used_before_its_binder_is_an_error(tmp_path: Path) -> None:
    source = (
        "namespace A\ndef n := 3\n"
        "theorem t {m : Fin n} {n : Nat} : m = m ∧ n = n := ⟨rfl, rfl⟩\nend A\n"
    )
    type_ = "{m : Fin n} {n : Nat} : m = m ∧ n = n"
    claim = in_w("theorem c {m : Fin A.n} {n : Nat} : m = m ∧ n = n", "A.t")
    result = source_run(tmp_path, source, "A.t", 3, type_, claim)
    assert "claim c binds and uses n" in result.stdout, result.stdout


def test_a_bound_name_the_project_does_not_declare_passes(tmp_path: Path) -> None:
    source = (
        "namespace A\ndef m := 3\n"
        "theorem t : (∃ n : Nat, n = 3) ∧ m = 3 := ⟨⟨3, rfl⟩, rfl⟩\nend A\n"
    )
    type_ = ": (∃ n : Nat, n = 3) ∧ m = 3"
    claim = in_w("theorem c : (∃ n : Nat, n = 3) ∧ A.m = 3", "A.t")
    result = source_run(tmp_path, source, "A.t", 3, type_, claim)
    assert result.stdout == OK_ONE, result.stdout


def test_loose_names_are_those_that_may_also_be_free() -> None:
    loose = load_script().loose_names
    assert loose("{n : Nat} : n = n") == set()
    assert loose("{n : Nat} {m : Fin n} : ∀ n, n = m") == set()
    assert loose("{m : Fin n} {n : Nat} : m = m") == {"n"}
    assert loose(": (∃ n : Nat, n = 3) ∧ n = 3") == {"n"}
    assert loose(": s = {x | x = 1}") == {"x"}


def test_a_claim_name_must_be_plain(tmp_path: Path) -> None:
    source = "def cost := 9\nnamespace A\ndef cost := 5\nend A\n"
    source += "theorem t : cost = 9 := rfl\n"
    notebook = "import X\ntheorem A.c : cost = 9 := by\n  exact t\n"
    result = source_run(tmp_path, source, "t", 5, ": cost = 9", notebook)
    assert result.returncode == 1
    assert "claim A.c must have a plain name" in result.stdout, result.stdout


def files_run(tmp_path: Path, files: dict[str, str], target: str, notebook: str) -> Any:
    """Check the notebook against a project of the files, citing the target.

    The target's entry is filled in from what the scan finds, so the outline
    itself is never what fails.
    """
    (tmp_path / "proj").mkdir()
    for name, text in files.items():
        (tmp_path / "proj" / name).write_text(text, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "proj")
    git(tmp_path, "commit", "-q", "-m", "p")
    script = load_script()
    file, found = next(
        (name, d)
        for name in files
        for d in script.declarations(tmp_path / "proj", name)
        if d.name == target
    )
    data = lean_outline(
        git(tmp_path, "rev-parse", "HEAD"),
        entry(name=target, file=file, line=found.line, type=found.type),
    )
    data["nodes"][0]["claim"] = notebook.split("theorem ", 1)[1].split()[0]
    return notebook_run(tmp_path, data, notebook)


MIXED_FILES = {
    "Stmt.lean": "namespace A\nprotected def cost := 5\nend A\n",
    "Main.lean": (
        "def cost := 9\nnamespace A\ndef cost := 7\n"
        "theorem t : cost = 7 := rfl\nend A\n"
    ),
}


@pytest.mark.parametrize("written", ["cost", "A.cost"])
def test_a_name_declared_both_protected_and_not_is_an_error(
    tmp_path: Path, written: str
) -> None:
    claim = in_w(f"theorem c : {written} = 7", "A.t")
    out = files_run(tmp_path, MIXED_FILES, "A.t", claim).stdout
    assert (
        "claim c uses A.cost, which the project declares both protected and not; "
        "explain it in prose instead"
    ) in out, out
    assert not any(w in out for w in STRUCTURAL_WORDS), out


def test_a_protected_name_in_every_file_still_passes(tmp_path: Path) -> None:
    files = {**MIXED_FILES, "Main.lean": PROTECTED_SOURCE}
    files["Stmt.lean"] = files["Stmt.lean"].replace("5", "9")
    claim = in_w("theorem c : cost = 9", "A.t")
    assert files_run(tmp_path, files, "A.t", claim).stdout == OK_ONE


GUILLEMET_SOURCE = (
    "def cost := 9\nnamespace A\ndef cost := 5\n"
    "theorem t : «cost» = 5 := rfl\nend A\n"
)


def test_a_guillemet_name_resolves_like_its_plain_spelling(tmp_path: Path) -> None:
    claim = in_w("theorem c : «cost» = 5", "A.t")
    out = files_run(tmp_path, {"R.lean": GUILLEMET_SOURCE}, "A.t", claim).stdout
    assert "there cost means A.cost" in out, out


@pytest.mark.parametrize("written", ["A.cost", "«A».«cost»", "A.«cost»"])
def test_a_guillemet_name_restated_in_full_passes(tmp_path: Path, written: str) -> None:
    claim = in_w(f"theorem c : {written} = 5", "A.t")
    out = files_run(tmp_path, {"R.lean": GUILLEMET_SOURCE}, "A.t", claim).stdout
    assert out == OK_ONE, out


def test_a_guillemet_use_before_its_binder_is_an_error(tmp_path: Path) -> None:
    source = (
        "def n := 4\nnamespace A\ndef n := 3\n"
        "theorem t {m : Fin «n»} {n : Nat} : m = m ∧ n = n := ⟨rfl, rfl⟩\nend A\n"
    )
    claim = in_w("theorem c {m : Fin «n»} {n : Nat} : m = m ∧ n = n", "A.t")
    out = files_run(tmp_path, {"R.lean": source}, "A.t", claim).stdout
    assert "claim c binds and uses n" in out, out


def test_guillemets_are_dropped_only_from_plain_components() -> None:
    script = load_script()
    assert script._tokens("«cost» = «A».«b'»") == ["cost", "=", "A.b'"]
    assert script._tokens("«a.b».c «x y» «fun»") == ["«a.b».c", "«x y»", "«fun»"]
    assert script.same_statement(": «a.b».c", ": «a.b».c", (), set())


@pytest.mark.parametrize(
    "line, word",
    [
        ("section #exit", "#exit after section"),
        ("namespace N #exit", "#exit after namespace"),
        ("end N #exit", "#exit after end"),
        ("end #exit", "#exit after end"),
        ("section A B", "B after section"),
        ("#exit", "#exit"),
        ("namespace", "namespace"),
    ],
)
def test_a_structural_line_holds_only_its_name(
    tmp_path: Path, line: str, word: str
) -> None:
    data = claim_outline(make_project(tmp_path))
    text = f"import X\n{line}\ntheorem w_one : True := by\n  exact A.B.one\n"
    out = notebook_run(tmp_path, data, text).stdout
    assert f"{ONLY}: line 2: {word} in " in out, out
    assert not any(w in out for w in STRUCTURAL_WORDS), out


def test_structural_lines_may_name_and_end_in_a_comment(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = (
        "import X\nsection S -- part\nnamespace N.M -- part\n"
        "theorem w_one : True := by\n  exact A.B.one\nend N.M -- part\nend S\n"
    )
    assert notebook_run(tmp_path, data, text).stdout == OK_ONE


@pytest.mark.parametrize(
    "source, gap",
    [
        ("theorem t : 1 = 2 :=\nsorry\n", "sorry"),
        ("theorem t : 1 = 2 :=\n\nsorry\n", "sorry"),
        ("theorem t : 1 = 2 :=\n(by\nsorry)\n", "sorry"),
        ("theorem t : 1 = 2 := by\nadmit\n", "admit"),
        ("theorem t : 1 = 2 := by\n  stop\n  rfl\n", "stop"),
        ("theorem t : 1 = 2 :=\nby stop\n", "stop"),
        (
            "theorem t : 1 = 2 := ((fun (_ : Array Nat) (h : 1 = 2) => h)\n"
            "#[1]\nsorry)\n",
            "sorry",
        ),
        ("theorem t : 1 = 2 := (id\n#v[1]\nsorry)\n", "sorry"),
        (
            'theorem t : 1 = 2 :=\nlet _s := "--" /-\ntheorem x\n-/\nsorry\n',
            "sorry",
        ),
        ("theorem t : 1 = 2 := /-\ntheorem x\n-/\nsorry\n", "sorry"),
        ('theorem t : 1 = 2 :=\nlet _s := "a\ntheorem x"\nsorry\n', "sorry"),
        ("theorem t : 1 = 2 :=\nlet _c := '\"'\n/-\ntheorem x\n-/\nsorry\n", "sorry"),
    ],
)
def test_a_proof_continued_at_column_0_is_scanned(
    tmp_path: Path, source: str, gap: str
) -> None:
    out = cite_run(tmp_path, source)
    assert f"claim c cites t, whose proof contains {gap}" in out, out


@pytest.mark.parametrize(
    "after",
    [
        "theorem u : True := sorry\n",
        "\n/-- doc -/\n@[simp] theorem u : True := sorry\n",
        "#check (sorry : Nat)\n",
        "#print axioms t\n#eval (sorry : Nat)\n",
        "example : True := sorry\n",
        "namespace N\ndef u : Nat := sorry\nend N\n",
    ],
)
def test_the_next_command_is_not_part_of_the_proof(tmp_path: Path, after: str) -> None:
    out = cite_run(tmp_path, "theorem t : True :=\ntrivial\n" + after, ": True")
    assert out == OK_ONE, out


@pytest.mark.parametrize(
    "attribute", ["@[simp] ", "@[instance] ", "@[simp, reducible]"]
)
def test_a_claim_with_attributes_is_an_error(tmp_path: Path, attribute: str) -> None:
    data = claim_outline(make_project(tmp_path))
    text = f"import X\n{attribute}theorem w_one : True := by\n  exact A.B.one\n"
    out = notebook_run(tmp_path, data, text).stdout
    assert "claim w_one has attributes; a claim takes none" in out, out
    assert not any(w in out for w in STRUCTURAL_WORDS), out


def test_a_declaration_after_its_docstring_on_one_line_is_scanned(
    tmp_path: Path,
) -> None:
    source = (
        "/-- doc -/ def x := 1\n/-- doc -/ theorem y : x = 1 := rfl\n"
        "/- a\nb -/ def z := 2\n/- a /- b -/ c -/ def w := 3\n/-- only a doc -/\n"
        "/- a -/ /- b -/ def v := 4\n/- a\n-/ /-- b -/ def u := 5\n"
    )
    assert list(names_in(tmp_path, source)) == ["x", "y", "z", "w", "v", "u"]


@pytest.mark.parametrize("comments", ["/-- doc -/", "/- a -/ /- b -/"])
def test_a_declaration_on_its_docstring_line_resolves_names(
    tmp_path: Path, comments: str
) -> None:
    source = (
        f"def cost := 9\nnamespace A\n{comments} def cost := 5\n"
        "/-- doc -/ theorem t : cost = 5 := rfl\nend A\n"
    )
    claim = in_w("theorem c : cost = 5", "A.t")
    out = files_run(tmp_path, {"R.lean": source}, "A.t", claim).stdout
    assert "there cost means A.cost" in out, out
