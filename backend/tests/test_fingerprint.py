"""The fast environment fingerprint changes exactly when checking inputs change."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from glitnir.lean.fingerprint import (
    direct_imports,
    environment_fingerprint,
    git_state,
    header_imports,
    trace_digest,
)
from glitnir.lean.project_inspection import TOOLCHAIN


def _project(root: Path) -> Path:
    (root / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (root / "lakefile.toml").write_text('name = "demo"\n')
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": "1.2.0", "packages": []})
    )
    build = root / ".lake/build/lib/lean/Demo"
    build.mkdir(parents=True)
    (build / "Base.trace").write_text(
        json.dumps({"depHash": "aa", "outputs": {"o": ["1.olean"]}, "log": []})
    )
    source = root / "Demo.lean"
    source.write_text("import Demo.Base\n\ntheorem t : True := True.intro\n")
    return source


def test_direct_imports_reads_only_the_header():
    text = "/-! doc -/\nimport A.B\nimport C D\n\ntheorem t : True := by\n  trivial\n"
    assert direct_imports(text) == ["A.B", "C", "D"]


def test_direct_imports_reads_past_a_closed_leading_comment():
    assert direct_imports("/- c -/ import Foo\nimport Bar") == ["Foo", "Bar"]


def test_direct_imports_reads_after_a_multiline_comment_closes():
    assert direct_imports("/-\n-/ import B") == ["B"]


def test_direct_imports_handles_nested_and_trailing_comments():
    text = (
        "/- outer /- inner -/ still -/ import A -- note\n"
        "import /- mid -/ B /- x\n y -/ C\n"
        "-- import Hidden\n"
        "theorem t : True := True.intro\n"
        "import Late\n"
    )
    assert direct_imports(text) == ["A", "B", "C"]
    assert direct_imports("/- a --/ import A") == ["A"]
    assert direct_imports("/- unclosed\nimport A") == []


def _lake_trace(root: str, **changes) -> dict:
    """A Lake 4.35 module trace, as `lake build` writes it under `root`."""
    source = f"{root}/src/Demo/Base.lean"
    trace = {
        "synthetic": False,
        "schemaVersion": "2025-09-10",
        "outputs": {
            "o": ["d38bab13c7e11970.olean"],
            "m": False,
            "i": "7cf730b9456a20ff.ilean",
            "c": "eb70cff95869e9c9.c",
        },
        "log": [{"message": f".> lean {source} -o {root}/.lake/x", "level": "trace"}],
        "inputs": [
            ["deps", [["imports", "00000000000006bb"]]],
            ["Lean 4.35.0, commit eb6cafb", "407ce514b71d5a10"],
            [source, "4b76c7a070fe7a42"],
            ["options", [["-DautoImplicit=false", "4a85acb881fd49d0"]]],
            ["Module.name: Demo.Base", "5915f570797032cd"],
        ],
        "depHash": "1f6e75af085721d2",
    }
    trace.update(changes)
    return trace


def _write_trace(path: Path, trace: object) -> Path:
    path.write_text(json.dumps(trace))
    return path


def test_trace_digest_ignores_path_captions_synthetic_and_logs(tmp_path):
    """Lake hashes no caption, so a build elsewhere or fetched has the same digest."""
    built = _write_trace(tmp_path / "a.trace", _lake_trace("/home/runner/work/p"))
    moved = _lake_trace("/Users/me/longer/path/p", synthetic=True)
    moved["log"] = [{"message": "a lint warning", "level": "warning"}]
    elsewhere = _write_trace(tmp_path / "b.trace", moved)
    assert trace_digest(built) is not None
    assert trace_digest(built) == trace_digest(elsewhere)
    assert trace_digest(tmp_path / "missing.trace") is None


def test_a_trace_without_inputs_equals_a_built_one(tmp_path):
    """Mathlib's fetched traces carry only schemaVersion, depHash and outputs."""
    built = _lake_trace("/home/runner/work/p")
    fetched = {key: built[key] for key in ("schemaVersion", "depHash", "outputs")}
    assert trace_digest(_write_trace(tmp_path / "a.trace", built)) == trace_digest(
        _write_trace(tmp_path / "b.trace", fetched)
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"depHash": "1f6e75af085721d3"},
        {"schemaVersion": "2026-01-01"},
        *(
            {"outputs": {**_lake_trace("/p")["outputs"], key: value}}
            for key, value in (
                ("o", ["d38bab13c7e11971.olean"]),
                ("i", "7cf730b9456a20fe.ilean"),
                ("c", "eb70cff95869e9c8.c"),
                ("r", ["0123456789abcdef.olean.server"]),
                ("rs", ["0123456789abcdef.olean.private"]),
                ("m", True),
            )
        ),
    ],
)
def test_trace_digest_changes_with_the_build_identity(tmp_path, changes):
    base = trace_digest(_write_trace(tmp_path / "a.trace", _lake_trace("/p")))
    changed = trace_digest(
        _write_trace(tmp_path / "b.trace", _lake_trace("/p", **changes))
    )
    assert changed is not None and changed != base


@pytest.mark.parametrize(
    "content",
    ['"1f6e75af085721d2"', "1234", "[1, 2]", "null", "{}", '{"log": []}', "{"],
)
def test_non_dict_and_stub_traces_have_no_digest(tmp_path, content):
    """A trace with no depHash identifies no build, so it is never a digest."""
    (tmp_path / "t.trace").write_text(content)
    assert trace_digest(tmp_path / "t.trace") is None


def _copied_project(root: Path) -> Path:
    """`_project` at `root`, with a trace naming `root` as Lake writes it."""
    root.mkdir(parents=True, exist_ok=True)
    source = _project(root)
    trace = root / ".lake/build/lib/lean/Demo/Base.trace"
    _write_trace(trace, _lake_trace(str(root)))
    module = root / "Demo/Base.lean"
    module.parent.mkdir()
    module.write_text("def x : Nat := 1\n")
    return source


def test_a_project_built_at_another_path_has_the_same_fingerprint(tmp_path):
    """Saved results stay current in another checkout of identical builds."""
    first = _copied_project(tmp_path / "a/project")
    second = _copied_project(tmp_path / "b/much/longer/project")
    text = first.read_text()
    one = environment_fingerprint(first, text)
    two = environment_fingerprint(second, text)
    assert one["imports"]["Demo.Base"] is not None
    assert one["sha256"] == two["sha256"]
    assert one["stable_sha256"] == two["stable_sha256"]
    (second.parent / "Demo/Base.lean").write_text("def x : Nat := 2\n")
    assert environment_fingerprint(second, text)["sha256"] != one["sha256"]


def _git(directory: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    done = subprocess.run(
        ["git", *args], cwd=directory, check=True, capture_output=True, env=env
    )
    return done.stdout.decode().strip()


def _project_with_git_dependency(root: Path) -> Path:
    source = _copied_project(root)
    dependency = root / ".lake/packages/dep"
    dependency.mkdir(parents=True)
    _git(dependency, "init", "-q")
    (dependency / "Dep.lean").write_text("def y : Nat := 1\n")
    _git(dependency, "add", "Dep.lean")
    _git(dependency, "commit", "-qm", "init")
    head = _git(dependency, "rev-parse", "HEAD")
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packages": [{"name": "dep", "type": "git", "rev": head}],
            }
        )
    )
    return source


def test_a_dirty_dependency_still_changes_a_portable_fingerprint(tmp_path):
    first = _project_with_git_dependency(tmp_path / "a/project")
    second = _project_with_git_dependency(tmp_path / "b/x/project")
    text = first.read_text()
    one = environment_fingerprint(first, text)
    assert one["packages"][0]["clean"] is True
    assert environment_fingerprint(second, text)["sha256"] == one["sha256"]
    (tmp_path / "b/x/project/.lake/packages/dep/Dep.lean").write_text("def y := 2\n")
    dirty = environment_fingerprint(second, text)
    assert dirty["packages"][0]["clean"] is False
    assert dirty["sha256"] != one["sha256"]


def test_a_result_saved_under_a_v3_fingerprint_reads_as_out_of_date(tmp_path):
    """Version 3 hashed whole traces; its digests match nothing now."""
    from glitnir.notebook.check_records import claim_views, records_from_check

    source = _copied_project(tmp_path)
    found = environment_fingerprint(source, source.read_text())
    identity = {
        key: found[key]
        for key in ("toolchain", "files", "packages", "imports", "sources", "checker")
    }
    v3 = hashlib.sha256(
        b"glitnir.environment-fingerprint/v3\0"
        + json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    text = "theorem t : True := True.intro\n"
    result = {
        "status": "checked_snapshot",
        "source_sha256": "s",
        "environment_sha256": "e",
        "withheld": None,
        "theorems": [
            {
                "name": "t",
                "status": "checked",
                "axioms": [],
                "start_line": 1,
                "end_line": 1,
                "proposition_sha256": "p",
            }
        ],
    }
    records = records_from_check(text, result, v3)
    assert [v["current"] for v in claim_views(text, records, v3)] == [True]
    [view] = claim_views(text, records, found["sha256"])
    assert view["current"] is False


def test_rebuilt_dependency_changes_the_fingerprint(tmp_path):
    source = _project(tmp_path)
    text = source.read_text()
    before = environment_fingerprint(source, text)
    trace = tmp_path / ".lake/build/lib/lean/Demo/Base.trace"
    trace.write_text(json.dumps({"depHash": "bb", "outputs": {"o": ["2.olean"]}}))
    after = environment_fingerprint(source, text)
    assert before["sha256"] != after["sha256"]
    assert after["imports"]["Demo.Base"] is not None


def test_manifest_change_changes_the_fingerprint(tmp_path):
    source = _project(tmp_path)
    text = source.read_text()
    before = environment_fingerprint(source, text)
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps({"version": "1.2.0", "packages": [{"name": "x", "rev": "1"}]})
    )
    assert environment_fingerprint(source, text)["sha256"] != before["sha256"]


def test_notebook_text_does_not_change_the_fingerprint(tmp_path):
    source = _project(tmp_path)
    first = environment_fingerprint(source, source.read_text())
    second = environment_fingerprint(source, source.read_text() + "-- edit\n")
    assert first["sha256"] == second["sha256"]


def test_git_state_reports_head_and_cleanliness(tmp_path):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env=env,
        )

    run("init", "-q")
    (tmp_path / "f.lean").write_text("def x := 1\n")
    run("add", "f.lean")
    run("commit", "-qm", "init")
    head = run("rev-parse", "HEAD").stdout.decode().strip()
    assert git_state(tmp_path, head) == {
        "head": head,
        "at_manifest": True,
        "clean": True,
    }
    (tmp_path / "f.lean").write_text("def x := 2\n")
    assert git_state(tmp_path, head)["clean"] is False
    assert git_state(tmp_path, "0" * 40)["at_manifest"] is False


def test_loose_file_fingerprint_needs_no_project(tmp_path):
    source = tmp_path / "Loose.lean"
    source.write_text("theorem t : True := True.intro\n")
    result = environment_fingerprint(source, source.read_text())
    assert len(result["sha256"]) == 64
    assert result["packages"] == []


def test_malformed_import_names_record_none_without_raising(tmp_path):
    source = _project(tmp_path)
    (tmp_path / "b.trace").write_text(json.dumps({"depHash": "cc"}))
    text = "import .\nimport /x\nimport a/../b\n"
    result = environment_fingerprint(source, text)
    assert result["imports"] == {".": None, "/x": None, "a/../b": None}


def test_deeply_nested_trace_records_none(tmp_path):
    source = _project(tmp_path)
    build = tmp_path / ".lake/build/lib/lean/Demo"
    (build / "Deep.trace").write_text("[" * 100000)
    result = environment_fingerprint(source, "import Demo.Deep\n")
    assert result["imports"] == {"Demo.Deep": None}


def test_a_missing_trace_is_a_known_state(tmp_path):
    """An import not built (a toolchain module, say) has no trace, every time."""
    source = _project(tmp_path)
    text = "import Demo.Missing\n"
    first, second = (environment_fingerprint(source, text) for _ in range(2))
    assert first["imports"] == {"Demo.Missing": None}
    assert first["unknown"] is False
    assert first["sha256"] == second["sha256"]


@pytest.mark.parametrize(
    "content",
    ["{", "[" * 100000, "{}", '{"log": []}', '{"depHash": 1}', "[]"],
    ids=["invalid", "nested", "empty", "stub", "numeric-hash", "list"],
)
def test_a_trace_that_is_no_lake_trace_matches_nothing(tmp_path, content):
    """A trace file Lake would reject is unknown, never the state of a missing one."""
    from glitnir.lean.fingerprint import unknown_git_note

    source = _project(tmp_path)
    text = "import Demo.Broken\n"
    missing = environment_fingerprint(source, text)
    (tmp_path / ".lake/build/lib/lean/Demo/Broken.trace").write_text(content)
    first, second = (environment_fingerprint(source, text) for _ in range(2))
    assert first["imports"] == {"Demo.Broken": None}
    assert first["unknown"] is True
    assert len({missing["sha256"], first["sha256"], second["sha256"]}) == 3
    assert first["stable_sha256"] == second["stable_sha256"]
    assert first["stable_sha256"] != missing["stable_sha256"]
    assert unknown_git_note(first) == (
        "Could not read the build traces of Demo.Broken; "
        "results are saved as out of date."
    )


def test_an_unreadable_trace_matches_nothing(tmp_path):
    """A trace that exists but cannot be read (a directory, here) is unknown."""
    source = _project(tmp_path)
    (tmp_path / ".lake/build/lib/lean/Demo/Odd.trace").mkdir()
    result = environment_fingerprint(source, "import Demo.Odd\n")
    assert result["unknown"] is True


def test_deeply_nested_manifest_has_no_packages(tmp_path):
    source = _project(tmp_path)
    (tmp_path / "lake-manifest.json").write_text("[" * 100000)
    result = environment_fingerprint(source, source.read_text())
    assert result["packages"] == []


def test_path_dependency_outside_project_changes_the_fingerprint(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    source = _project(project)
    (project / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.2.0",
                "packages": [{"name": "sib", "type": "path", "dir": "../sib"}],
            }
        )
    )
    sibling = tmp_path / "sib/.lake/build/lib/lean"
    sibling.mkdir(parents=True)
    trace = sibling / "Sib.trace"
    trace.write_text(json.dumps({"depHash": "aa", "outputs": {"o": ["1.olean"]}}))
    text = "import Sib\n"
    before = environment_fingerprint(source, text)
    assert before["imports"]["Sib"] is not None
    trace.write_text(json.dumps({"depHash": "bb", "outputs": {"o": ["2.olean"]}}))
    after = environment_fingerprint(source, text)
    assert before["sha256"] != after["sha256"]


def test_root_project_source_edit_changes_the_fingerprint(tmp_path):
    """An edited project module that is not rebuilt yet still invalidates results."""
    source = _project(tmp_path)
    module = tmp_path / "Demo/Base.lean"
    module.parent.mkdir()
    module.write_text("def x : Nat := 1\n")
    text = source.read_text()
    before = environment_fingerprint(source, text)
    module.write_text("def x : Nat := 22\n")
    after = environment_fingerprint(source, text)
    assert before["sha256"] != after["sha256"]
    assert before["imports"] == after["imports"]


def test_new_transitive_project_module_changes_the_fingerprint(tmp_path):
    source = _project(tmp_path)
    module = tmp_path / "Demo/Base.lean"
    module.parent.mkdir()
    module.write_text("import Demo.Deep.Leaf\ndef x : Nat := 1\n")
    text = source.read_text()
    before = environment_fingerprint(source, text)
    (tmp_path / "Demo/Deep").mkdir(parents=True)
    (tmp_path / "Demo/Deep/Leaf.lean").write_text("def y := 1\n")
    after = environment_fingerprint(source, text)
    assert after["sha256"] != before["sha256"]
    assert [name for name, _ in after["sources"]["files"]] == [
        "Demo/Base.lean",
        "Demo/Deep/Leaf.lean",
    ]
    (tmp_path / "Demo/Deep/Leaf.lean").write_text("def y := 2\n")
    assert environment_fingerprint(source, text)["sha256"] != after["sha256"]


def test_a_module_in_a_source_directory_is_found_by_its_path(tmp_path):
    """A lean_lib's srcDir is not read: any path ending in the module's counts."""
    source = _project(tmp_path)
    module = tmp_path / "src/lib/Demo/Base.lean"
    module.parent.mkdir(parents=True)
    module.write_text("def x : Nat := 1\n")
    text = source.read_text()
    before = environment_fingerprint(source, text)
    assert [name for name, _ in before["sources"]["files"]] == [
        "src/lib/Demo/Base.lean"
    ]
    module.write_text("def x : Nat := 2\n")
    assert environment_fingerprint(source, text)["sha256"] != before["sha256"]


def test_quoted_module_names_find_their_files(tmp_path):
    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    (tmp_path / "Demo/odd-name.lean").write_text("def q := 1\n")
    source.write_text("import Demo.«odd-name»\n")
    found = environment_fingerprint(source, source.read_text())["sources"]
    assert [name for name, _ in found["files"]] == ["Demo/odd-name.lean"]


def test_reopening_after_unrelated_or_identical_writes_keeps_the_fingerprint(
    tmp_path,
):
    """Saved results stay current unless an imported source's bytes change.

    Before 0.1.1 every project source counted by size and modification time,
    so another notebook's edit, or a rewrite with the same bytes (a re-save, a
    branch switched back), made every saved result read as out of date.
    """
    source = _project(tmp_path)
    module = tmp_path / "Demo/Base.lean"
    module.parent.mkdir()
    module.write_text("def x : Nat := 1\n")
    sibling = tmp_path / "Other.lean"
    sibling.write_text("def unrelated := 1\n")
    text = source.read_text()
    before = environment_fingerprint(source, text)
    os.utime(module, ns=(10**18, 10**18))
    module.write_bytes(module.read_bytes())
    sibling.write_text("def unrelated := 2\n")
    os.utime(sibling, ns=(10**18, 10**18))
    after = environment_fingerprint(source, text)
    assert after["sha256"] == before["sha256"]
    assert after["sources"] == before["sources"]


def test_a_sibling_notebook_the_notebook_imports_counts(tmp_path):
    source = _project(tmp_path)
    sibling = tmp_path / "Other.lean"
    sibling.write_text("def used := 1\n")
    source.write_text("import Other\ntheorem t : True := True.intro\n")
    text = source.read_text()
    before = environment_fingerprint(source, text)
    sibling.write_text("def used := 2\n")
    assert environment_fingerprint(source, text)["sha256"] != before["sha256"]


def test_import_cycles_end(tmp_path):
    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    (tmp_path / "Demo/Base.lean").write_text("import Demo.Loop\n")
    (tmp_path / "Demo/Loop.lean").write_text("import Demo.Base\n")
    found = environment_fingerprint(source, source.read_text())["sources"]
    assert [name for name, _ in found["files"]] == [
        "Demo/Base.lean",
        "Demo/Loop.lean",
    ]


def test_saving_the_notebook_itself_keeps_the_fingerprint(tmp_path):
    """The notebook's own text is covered by each claim's prefix hash instead."""
    source = _project(tmp_path)
    before = environment_fingerprint(source, source.read_text())
    source.write_text(source.read_text() + "-- a longer edit\n")
    os.utime(source, ns=(1, 1))
    after = environment_fingerprint(source, source.read_text())
    assert before["sha256"] == after["sha256"]


def test_prose_images_do_not_change_the_fingerprint(tmp_path):
    """Images are illustrations, never an input to checking."""
    source = _project(tmp_path)
    before = environment_fingerprint(source, source.read_text())
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/plot.png").write_bytes(b"png")
    (tmp_path / "assets/sketch.svg").write_text("<svg/>")
    after = environment_fingerprint(source, source.read_text())
    assert before["sha256"] == after["sha256"]


def test_sources_under_dot_directories_are_ignored(tmp_path):
    source = _project(tmp_path)
    text = source.read_text()
    before = environment_fingerprint(source, text)
    (tmp_path / ".lake/packages/dep").mkdir(parents=True)
    (tmp_path / ".lake/packages/dep/Dep.lean").write_text("def z := 1\n")
    (tmp_path / ".lake/build/lib/lean/Demo/Gen.lean").write_text("def w := 1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/X.lean").write_text("def v := 1\n")
    assert environment_fingerprint(source, text)["sha256"] == before["sha256"]


def test_symlinked_sources_are_not_followed(tmp_path):
    source = _project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "Far.lean").write_text("def f := 1\n")
    (tmp_path / "Link").symlink_to(outside, target_is_directory=True)
    (tmp_path / "Linked.lean").symlink_to(outside / "Far.lean")
    text = source.read_text()
    result = environment_fingerprint(source, text)
    names = [entry[0] for entry in result["sources"]["files"]]
    assert names == []


def test_source_walk_is_bounded(tmp_path, monkeypatch):
    from glitnir.lean import fingerprint

    source = _project(tmp_path)
    for index in range(5):
        (tmp_path / f"M{index}.lean").write_text("def a := 1\n")
    source.write_text("".join(f"import M{index}\n" for index in range(5)))
    monkeypatch.setattr(fingerprint, "_MAX_SOURCES", 3)
    result = environment_fingerprint(source, source.read_text())
    assert len(result["sources"]["files"]) == 3
    assert result["sources"]["truncated"] is True


def test_axiom_allowlist_changes_the_fingerprint(tmp_path, monkeypatch):
    """A Glitnir upgrade that changes what checking accepts invalidates results."""
    from glitnir.lean import project_checking

    source = _project(tmp_path)
    text = source.read_text()
    before = environment_fingerprint(source, text)
    monkeypatch.setattr(
        project_checking,
        "LEAN_AXIOM_ALLOWLIST",
        (*project_checking.LEAN_AXIOM_ALLOWLIST, "Extra.axiom"),
    )
    assert environment_fingerprint(source, text)["sha256"] != before["sha256"]


def test_supported_toolchain_changes_the_fingerprint(tmp_path, monkeypatch):
    """Moving to another Lean invalidates results without any helper edit."""
    from glitnir.lean import project_inspection

    source = _project(tmp_path)
    text = source.read_text()
    before = environment_fingerprint(source, text)
    assert before["checker"]["toolchain"] == project_inspection.TOOLCHAIN
    monkeypatch.setattr(project_inspection, "TOOLCHAIN", "leanprover/lean4:v4.99.0")
    after = environment_fingerprint(source, text)
    assert after["checker"]["toolchain"] == "leanprover/lean4:v4.99.0"
    assert after["checker"]["programs"] == before["checker"]["programs"]
    assert after["sha256"] != before["sha256"]


def test_checker_helper_sources_are_identified(tmp_path):
    source = _project(tmp_path)
    checker = environment_fingerprint(source, source.read_text())["checker"]
    assert sorted(checker["programs"]) == [
        "AxiomReport.lean",
        "ProjectRead.lean",
        "ProjectReplay.lean",
        "ProjectReport.lean",
        "ViewElab.lean",
    ]
    assert all(len(digest) == 64 for digest in checker["programs"].values())


def test_git_state_survives_non_utf8_output(tmp_path, monkeypatch):
    """Undecodable git output is replaced, never raised from the fingerprint."""
    from glitnir.lean import fingerprint

    real_run = subprocess.run

    def fake_git(command, **kwargs):
        return real_run(["/bin/sh", "-c", "printf 'x\\377'"], **kwargs)

    monkeypatch.setattr(fingerprint.subprocess, "run", fake_git)
    state = git_state(tmp_path, "r")
    assert state["head"] == "x�"
    assert state["clean"] is False


class _Clock:
    """A monotonic clock that only moves when a stubbed git command takes time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _stub_git(monkeypatch, statuses: list[object], head: object = "abc"):
    """Answer `rev-parse` with `head` and `status` from `statuses` in turn.

    An answer that is a string is the output; `TIMEOUT` takes the whole
    timeout given and raises; a `CompletedProcess` or exception is used as is.
    Returns the commands with their timeouts, and the fake clock.
    """
    from glitnir.lean import fingerprint

    clock = _Clock()
    monkeypatch.setattr(fingerprint, "_clock", clock)
    calls: list[tuple[list[str], float]] = []

    def answer(command, value, timeout):
        if value is TIMEOUT:
            clock.now += timeout
            raise subprocess.TimeoutExpired(command, timeout)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, subprocess.CompletedProcess):
            return value
        return subprocess.CompletedProcess(command, 0, value, "")

    def fake_git(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        if "rev-parse" in command:
            return answer(command, head, kwargs["timeout"])
        return answer(command, statuses.pop(0), kwargs["timeout"])

    monkeypatch.setattr(fingerprint.subprocess, "run", fake_git)
    return calls, clock


TIMEOUT = object()


def _timeouts(calls, name: str) -> list[float]:
    return [timeout for command, timeout in calls if name in command]


def test_git_status_is_retried_once_after_a_timeout(tmp_path, monkeypatch):
    """A slow first `git status` (cold cache, concurrent copy) is retried."""
    calls, _ = _stub_git(monkeypatch, [TIMEOUT, ""])
    assert git_state(tmp_path, "abc") == {
        "head": "abc",
        "at_manifest": True,
        "clean": True,
    }
    assert _timeouts(calls, "rev-parse") == [10]
    assert _timeouts(calls, "status") == [60, 30]  # the rest of the 90 s


def test_git_status_that_never_answers_is_unknown(tmp_path, monkeypatch):
    """Two timeouts record an unknown state with a reason, never a guess."""
    calls, _ = _stub_git(monkeypatch, [TIMEOUT, TIMEOUT])
    state = git_state(tmp_path, "abc")
    assert state["clean"] is None
    assert state["reason"] == "git status did not answer in time"
    assert len(_timeouts(calls, "status")) == 2


def test_git_failure_is_unknown_with_its_reason_and_not_retried(tmp_path, monkeypatch):
    """A non-zero exit is a definite failure: unknown, explained, run once."""
    refused = subprocess.CompletedProcess(
        ["git"],
        128,
        "",
        "fatal: detected dubious ownership in repository at '/m'\nhint: x\n",
    )
    calls, _ = _stub_git(monkeypatch, [refused], head=refused)
    state = git_state(tmp_path, "abc")
    assert state == {
        "head": None,
        "at_manifest": False,
        "clean": None,
        "reason": "fatal: detected dubious ownership in repository at '/m'",
    }
    assert len(calls) == 2
    _stub_git(monkeypatch, [OSError(2, "No such file or directory")])
    state = git_state(tmp_path, "abc")
    assert state["clean"] is None
    assert state["reason"] == "git could not run: No such file or directory"


def test_git_reason_is_bounded_and_printable(tmp_path, monkeypatch):
    """A long or odd error line never becomes a long or multi-line message."""
    noisy = subprocess.CompletedProcess(["git"], 1, "", "\n  e\x1b" + "x" * 500)
    _stub_git(monkeypatch, [noisy])
    reason = git_state(tmp_path, "abc")["reason"]
    assert reason.startswith("e?x") and len(reason) == 200


def test_dirty_checkout_is_reported_dirty_without_retrying(tmp_path, monkeypatch):
    """A modified tracked file is a definite answer."""
    calls, _ = _stub_git(monkeypatch, [" M Mathlib/Order/Basic.lean\n"])
    state = git_state(tmp_path, "abc")
    assert state["clean"] is False and "reason" not in state
    assert len(_timeouts(calls, "status")) == 1


def _git_dependency_project(root: Path, names=("mathlib",)) -> Path:
    source = _project(root)
    packages = []
    for name in names:
        (root / ".lake/packages" / name / ".git").mkdir(parents=True)
        packages.append({"name": name, "type": "git", "rev": "abc"})
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": "1.2.0", "packages": packages})
    )
    return source


def test_unknown_cleanliness_never_matches_a_saved_fingerprint(tmp_path, monkeypatch):
    """Results saved clean, dirty or unknown are out of date while git cannot answer.

    Only identical known states match, so a dirty dependency is never taken
    for the clean one it was saved with.
    """
    from glitnir.lean.fingerprint import unknown_git_note

    source = _git_dependency_project(tmp_path)
    text = source.read_text()
    _stub_git(monkeypatch, ["", "", " M x\n", " M x\n"] + [TIMEOUT] * 4)
    clean = [environment_fingerprint(source, text) for _ in range(2)]
    dirty = [environment_fingerprint(source, text) for _ in range(2)]
    unknown = [environment_fingerprint(source, text) for _ in range(2)]
    assert clean[0]["sha256"] == clean[1]["sha256"]
    assert dirty[0]["sha256"] == dirty[1]["sha256"]
    assert unknown_git_note(clean[0]) is None and unknown_git_note(dirty[0]) is None
    assert [u["packages"][0]["clean"] for u in unknown] == [None, None]
    digests = {
        clean[0]["sha256"],
        dirty[0]["sha256"],
        unknown[0]["sha256"],
        unknown[1]["sha256"],
    }
    assert len(digests) == 4
    assert unknown_git_note(unknown[0]) == (
        "Could not read the git state of mathlib: git status did not answer "
        "in time; results are saved as out of date."
    )


def test_unknown_head_never_matches_a_saved_fingerprint(tmp_path, monkeypatch):
    """A git dependency whose HEAD cannot be read is unknown too."""
    source = _git_dependency_project(tmp_path)
    text = source.read_text()
    _stub_git(monkeypatch, ["", ""], head=TIMEOUT)
    first, second = (environment_fingerprint(source, text) for _ in range(2))
    assert first["packages"][0]["head"] is None
    assert first["packages"][0]["clean"] is True
    assert first["sha256"] != second["sha256"]


def test_one_deadline_bounds_git_across_all_dependencies(tmp_path, monkeypatch):
    """Git gets 90 s per fingerprint in total; later dependencies are unknown."""
    from glitnir.lean.fingerprint import unknown_git_note

    source = _git_dependency_project(tmp_path, ("aesop", "batteries", "mathlib"))
    calls, clock = _stub_git(monkeypatch, [TIMEOUT, TIMEOUT])
    start = clock.now
    found = environment_fingerprint(source, source.read_text())
    assert clock.now - start == 90
    assert [timeout for _, timeout in calls] == [10, 60, 30]
    states = {p["name"]: (p["clean"], p["reason"]) for p in found["packages"]}
    assert states == {
        "aesop": (None, "git status did not answer in time"),
        "batteries": (None, "git was not read within the 90 s limit"),
        "mathlib": (None, "git was not read within the 90 s limit"),
    }
    assert unknown_git_note(found) == (
        "Could not read the git state of aesop: git status did not answer in "
        "time; batteries, mathlib: git was not read within the 90 s limit; "
        "results are saved as out of date."
    )


def test_git_runs_in_one_checkout_do_not_overlap(tmp_path, monkeypatch):
    """A second reader waits for the first instead of running git alongside it."""
    import threading
    import time

    from glitnir.lean import fingerprint

    active: list[list[str]] = []
    overlapped: list[list[str]] = []
    entered, release = threading.Event(), threading.Event()

    def fake_git(command, **kwargs):
        if active:
            overlapped.append(command)
        active.append(command)
        if "status" in command:
            entered.set()
            release.wait(5)
        active.pop()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(fingerprint.subprocess, "run", fake_git)
    results: list[dict] = []

    def read() -> None:
        results.append(git_state(tmp_path, "r"))

    first = threading.Thread(target=read)
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=read)
    second.start()
    time.sleep(0.2)  # the second reader would run git now without the lock
    release.set()
    first.join(10)
    second.join(10)
    assert overlapped == []
    assert [r["clean"] for r in results] == [True, True]


# ----- reading the header with the lexer --------------------------------------


@pytest.mark.parametrize(
    "text, imports",
    [
        ("module import Foo\n", ["Foo"]),
        ("prelude import Foo\n", ["Foo"]),
        ("module /- c -/ import Foo\n", ["Foo"]),
        ("module\nprelude\nimport Foo.Bar\n", ["Foo.Bar"]),
        ("public meta import A\nprivate import all B.c\n\ndef x := 1\n", ["A", "B.c"]),
        ("import «Foo Bar».X\nimport Y\n", ["«Foo Bar».X", "Y"]),
        ("/- a\n long\n comment -/\nimport A -- note\n\ntheorem t := 1\n", ["A"]),
        ("module\nimport A\npublic section\n", ["A"]),
        ("import A\n/-! prose that says\nimport B\n-/\n", ["A"]),
        ("", []),
        ("theorem t : True := trivial\n", []),
        ("import A", ["A"]),
    ],
)
def test_the_header_is_read_as_lean_reads_it(text, imports):
    assert header_imports(text) == imports


@pytest.mark.parametrize(
    "text",
    [
        "foo import Bar\n",  # an unknown word before the imports end
        "import Foo\nfoo\nimport Bar\n",  # an import after the header
        "import 3\n",  # not a module name
        "import A.\n",
        "import A . B\n",
        "module module import A\n",
        'import A\ndef s := s!"{x}"\n',  # the lexer cannot settle the header's end
        "/- never closed\nimport A\n",
    ],
)
def test_a_header_not_fully_understood_is_none(text):
    assert header_imports(text) is None


def _sources(result):
    return [name for name, _ in result["sources"]["files"]]


@pytest.mark.parametrize(
    "header",
    [
        "foo import Demo.Base\n",
        "import 3\n",
        'import Demo.Base\ndef s := s!"{x}"\n',
    ],
)
def test_a_misread_header_hashes_every_project_source(tmp_path, header):
    """Fail safe: what a header might import is every source of the project."""
    source = _project(tmp_path)
    (tmp_path / "Other.lean").write_text("def unrelated := 1\n")
    source.write_text(header)
    result = environment_fingerprint(source, header)
    assert result["sources"]["closure"] is False
    assert _sources(result) == ["Other.lean"]
    before = result["sha256"]
    (tmp_path / "Other.lean").write_text("def unrelated := 2\n")
    assert environment_fingerprint(source, header)["sha256"] != before


def test_module_and_prelude_headers_keep_their_closure(tmp_path):
    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    (tmp_path / "Demo/Base.lean").write_text("def x := 1\n")
    for header in ("module import Demo.Base\n", "prelude import Demo.Base\n"):
        result = environment_fingerprint(source, header)
        assert result["sources"]["closure"] is True
        assert _sources(result) == ["Demo/Base.lean"]


def test_a_misread_header_in_the_closure_hashes_every_source(tmp_path):
    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    (tmp_path / "Demo/Base.lean").write_text("weird import Demo.Hidden\n")
    (tmp_path / "Demo/Hidden.lean").write_text("def h := 1\n")
    result = environment_fingerprint(source, source.read_text())
    assert result["sources"]["closure"] is False
    assert _sources(result) == ["Demo/Base.lean", "Demo/Hidden.lean"]


def test_a_large_source_is_hashed_by_streaming_and_never_raises(tmp_path):
    """A generated module past any read limit still counts, by its bytes."""
    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    large = tmp_path / "Demo/Base.lean"
    body = "import Demo.Leaf\n" + "-- generated\n" * (6 * 1024 * 1024 // 13)
    large.write_text(body)
    assert large.stat().st_size > 4 * 1024 * 1024
    (tmp_path / "Demo/Leaf.lean").write_text("def leaf := 1\n")
    text = source.read_text()
    before = environment_fingerprint(source, text)
    assert before["unknown"] is False
    assert _sources(before) == ["Demo/Base.lean", "Demo/Leaf.lean"]
    large.write_text(body + "-- one more\n")
    assert environment_fingerprint(source, text)["sha256"] != before["sha256"]


def test_an_unreadable_source_is_unknown_and_matches_nothing(tmp_path, monkeypatch):
    from glitnir.lean import fingerprint

    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    (tmp_path / "Demo/Base.lean").write_text("def x := 1\n")
    text = source.read_text()
    monkeypatch.setattr(fingerprint, "_read_source", lambda path: (None, None, None))
    monkeypatch.setattr(fingerprint, "_DIGESTS", {})
    first = environment_fingerprint(source, text)
    second = environment_fingerprint(source, text)
    assert first["unknown"] is True and first["sha256"] != second["sha256"]
    assert first["stable_sha256"] == second["stable_sha256"]
    assert first["sources"]["unknown"] == ["Demo/Base.lean"]
    note = fingerprint.unknown_git_note(first)
    assert "Could not read the project sources (Demo/Base.lean)" in note


def test_a_source_written_while_read_is_unknown_and_not_cached(tmp_path, monkeypatch):
    from glitnir.lean import fingerprint

    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    module = tmp_path / "Demo/Base.lean"
    module.write_text("def x := 1\n")
    real = os.read

    def writing(descriptor, size):
        data = real(descriptor, size)
        if data and os.fstat(descriptor).st_ino == module.stat().st_ino:
            # Another write lands while the module is being read.
            later = module.stat().st_mtime_ns + 10**9
            os.utime(module, ns=(later, later))
        return data

    monkeypatch.setattr(fingerprint, "_DIGESTS", {})
    monkeypatch.setattr(fingerprint.os, "read", writing)
    result = environment_fingerprint(source, source.read_text())
    assert result["unknown"] is True
    assert fingerprint._DIGESTS == {}


def test_the_digest_cache_key_includes_the_change_time(tmp_path, monkeypatch):
    from glitnir.lean import fingerprint

    monkeypatch.setattr(fingerprint, "_DIGESTS", {})
    module = tmp_path / "Base.lean"
    module.write_text("def x := 1\n")
    fingerprint._source_digest(str(module), os.lstat(module))
    [key] = fingerprint._DIGESTS
    info = os.lstat(module)
    assert key[-2:] == (info.st_mtime_ns, info.st_ctime_ns)


def test_a_truncated_walk_matches_nothing(tmp_path, monkeypatch):
    from glitnir.lean import fingerprint

    source = _project(tmp_path)
    for index in range(4):
        (tmp_path / f"M{index}.lean").write_text("def a := 1\n")
    monkeypatch.setattr(fingerprint, "_MAX_SOURCES", 2)
    text = source.read_text()
    first = environment_fingerprint(source, text)
    assert first["unknown"] is True
    assert first["sha256"] != environment_fingerprint(source, text)["sha256"]


def test_provider_modules_count_like_the_notebooks_imports(tmp_path):
    """A view's or evidence's provider modules are part of its environment."""
    source = _project(tmp_path)
    (tmp_path / "Demo").mkdir()
    provided = tmp_path / "Demo/View.lean"
    provided.write_text("def drawn := 1\n")
    trace = tmp_path / ".lake/build/lib/lean/Demo/View.trace"
    trace.write_text(json.dumps({"depHash": "v1"}))
    text = source.read_text()
    plain = environment_fingerprint(source, text)
    with_provider = environment_fingerprint(source, text, ("Demo.View",))
    assert "Demo/View.lean" not in _sources(plain)
    assert _sources(with_provider) == ["Demo/View.lean"]
    assert with_provider["imports"]["Demo.View"] is not None
    assert plain["sha256"] != with_provider["sha256"]
    provided.write_text("def drawn := 2\n")
    assert environment_fingerprint(source, text) == plain
    changed = environment_fingerprint(source, text, ("Demo.View",))
    assert changed["sha256"] != with_provider["sha256"]
    trace.write_text(json.dumps({"depHash": "v2"}))
    rebuilt = environment_fingerprint(source, text, ("Demo.View",))
    assert rebuilt["sha256"] != changed["sha256"]


def test_the_stable_digest_ignores_only_the_unknown_marker(tmp_path, monkeypatch):
    from glitnir.lean import fingerprint

    source = _project(tmp_path)
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps({"packages": [{"name": "dep", "type": "git", "rev": "r"}]})
    )
    (tmp_path / ".lake/packages/dep/.git").mkdir(parents=True)
    reasons = iter(["git status did not answer in time", "git was not read"])
    monkeypatch.setattr(
        fingerprint,
        "git_state",
        lambda *a: {
            "head": None,
            "at_manifest": False,
            "clean": None,
            "reason": next(reasons),
        },
    )
    text = source.read_text()
    first = environment_fingerprint(source, text)
    second = environment_fingerprint(source, text)
    assert first["sha256"] != second["sha256"]
    assert first["stable_sha256"] == second["stable_sha256"]
    monkeypatch.setattr(
        fingerprint,
        "git_state",
        lambda *a: {"head": "r", "at_manifest": True, "clean": True},
    )
    known = environment_fingerprint(source, text)
    assert known["unknown"] is False
    assert known["stable_sha256"] != first["stable_sha256"]
    assert environment_fingerprint(source, text)["sha256"] == known["sha256"]


def test_headers_are_kept_by_digest_never_by_text(monkeypatch):
    """Polls reread the same texts; the cache holds digests, not 16 MB texts."""
    from glitnir.lean import fingerprint

    monkeypatch.setattr(fingerprint, "_HEADERS", {})
    monkeypatch.setattr(fingerprint, "_MAX_HEADERS", 2)
    reads = []
    real = fingerprint._header_imports
    monkeypatch.setattr(
        fingerprint,
        "_header_imports",
        lambda text, complete: reads.append(text) or real(text, complete),
    )
    text = "import A\n" + "-- body\n" * 1000
    assert header_imports(text) == ["A"]
    assert header_imports(text) == ["A"]
    assert len(reads) == 1
    assert all(
        isinstance(key[0], bytes) and len(key[0]) == 32 for key in fingerprint._HEADERS
    )
    header_imports("import B\n")
    header_imports("import C\n")
    assert len(fingerprint._HEADERS) == 2


def _lake_build(root: Path, toolchain: Path) -> None:
    subprocess.run(
        [str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(toolchain)},
    )


def _whole_trace_digest(path: Path) -> str:
    """The version 3 digest: the whole trace but its log."""
    trace = json.loads(path.read_text())
    trace.pop("log", None)
    return hashlib.sha256(json.dumps(trace, sort_keys=True).encode()).hexdigest()


def test_live_fingerprint_is_portable_across_checkouts(
    prepared_lean_project, project_toolchain, tmp_path
):
    """Results checked in one checkout are current in a rebuild at another path.

    Lake writes each module's absolute source path into its trace, so version 3
    of the fingerprint, which hashed whole traces, never matched there.
    """
    from glitnir.notebook.check_records import load_records, records_path
    from glitnir.service.preview import SourcePreviewService

    first = prepared_lean_project
    source = first / "src/Example.lean"
    text = source.read_text()
    service = SourcePreviewService(source)
    try:
        service.check(hashlib.sha256(text.encode()).hexdigest())
        service._worker.join(timeout=900)
        assert not service._worker.is_alive()
        assert service._check["status"] == "checked_snapshot", service._check
        [claim] = service.snapshot()["project_claims"]
    finally:
        service.close()
    assert (claim["name"], claim["current"]) == ("result", True)
    assert load_records(source) is not None

    # A fresh build of the same sources at another, longer path, not a copy.
    second = tmp_path / "b/x/project"
    shutil.copytree(first, second, ignore=shutil.ignore_patterns(".lake"))
    assert not (second / ".lake").exists()
    _lake_build(second, project_toolchain)
    moved = second / "src/Example.lean"
    shutil.copyfile(records_path(source), records_path(moved))
    traces = [
        root / ".lake/build/lib/lean/Example/Helper.trace" for root in (first, second)
    ]
    assert str(second).encode() in traces[1].read_bytes()
    assert _whole_trace_digest(traces[0]) != _whole_trace_digest(traces[1])
    assert trace_digest(traces[0]) == trace_digest(traces[1])

    def current() -> bool:
        service = SourcePreviewService(moved)
        try:
            [claim] = service.snapshot()["project_claims"]
        finally:
            service.close()
        return claim["current"]

    assert current() is True
    helper = second / "src/Example/Helper.lean"
    original = helper.read_text()
    helper.write_text("def helper : Nat := 0 + 1\n")
    _lake_build(second, project_toolchain)
    assert trace_digest(traces[1]) != trace_digest(traces[0])
    assert current() is False
    helper.write_text(original)
    _lake_build(second, project_toolchain)
    assert current() is True
    lakefile = second / "lakefile.toml"
    lakefile.write_text(
        lakefile.read_text().replace("autoImplicit = false", "autoImplicit = true")
    )
    _lake_build(second, project_toolchain)
    assert trace_digest(traces[1]) != trace_digest(traces[0])
    assert current() is False
