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


def test_opened_namespaces_are_not_recorded(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text(
        "open Real Complex\nnamespace P\ndef x : Nat := 1\nend P\n", encoding="utf-8"
    )
    (found,) = load_script().declarations(tmp_path, "F.lean")
    assert found.namespaces == ("P",)


def test_open_namespaces_do_not_license_a_prefix() -> None:
    same = load_script().same_statement
    assert not same(": Real.exp x = 1", ": exp x = 1", ())
    assert same(": Printed.GroupingLemma", ": GroupingLemma", ("Printed",))


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
    assert same(": A.B.Foo", ": Foo", ("A", "A.B"))
    assert not same(": C.Foo", ": Foo", ("A",))
    assert same(": A.Foo x", ": A.Foo x", ())
    assert not same(": Foo x", ": Foo", ("A",))
    assert not same(": Foo y", ": Foo x", ())
    assert same(": «a.b».c", ": «a.b».c", ())


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
    ],
)
def test_the_notebook_must_not_define_or_open(
    tmp_path: Path, line: str, word: str
) -> None:
    data = claim_outline(make_project(tmp_path))
    text = f"import X\n{line}\ntheorem w_one : True := by\n  exact A.B.one\n"
    out = notebook_run(tmp_path, data, text).stdout
    assert f"walkthrough must not define or open anything: line 2: {word}" in out


def test_a_plain_notebook_passes(tmp_path: Path) -> None:
    data = claim_outline(make_project(tmp_path))
    text = (
        "import Mathlib\n/-! prose\ndef not_code := 1\n-/\n-- open Real\n"
        "/-- doc -/\nnamespace N\ntheorem w_one : True := by\n  exact A.B.one\n"
        "end N\n#print axioms w_one\n"
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


def test_map_html_loads_only_katex_from_cdnjs(tmp_path: Path) -> None:
    page = map_page(tmp_path, outline())
    base = "https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/"
    assert base + "katex.min.css" in page
    assert base + "katex.min.js" in page
    assert base + "contrib/auto-render.min.js" in page
    assert page.count("https://") == 3


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
            _, found, rest = line.partition("python3 -B scripts/walkthrough.py ")
            words = rest.split("`")[0].split()
            if found and words:
                commands.append(words)
    return commands


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


def test_a_qualification_the_project_does_not_declare_is_an_error(
    tmp_path: Path,
) -> None:
    result = qualified_run(tmp_path, "Q.X")
    assert result.returncode == 1
    assert "claim c writes Q.X, which the project does not declare" in result.stdout


def test_the_map_legend_explains_dashed_edges() -> None:
    svg = load_script().map_svg(outline())
    assert 'stroke-dasharray="5 4"' in svg and "used by an appendix" in svg


def test_map_labels_keep_28_characters() -> None:
    short = load_script()._short
    assert short("x" * 28) == "x" * 28
    assert short("x" * 29) == "x" * 27 + "…"
