"""The fast environment fingerprint changes exactly when checking inputs change."""

import json
import os
import subprocess
from pathlib import Path

from glitnir.lean.fingerprint import (
    direct_imports,
    environment_fingerprint,
    git_state,
    trace_digest,
)


def _project(root: Path) -> Path:
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.1\n")
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


def test_trace_digest_ignores_build_logs(tmp_path):
    first, second = tmp_path / "a.trace", tmp_path / "b.trace"
    first.write_text(json.dumps({"depHash": "aa", "log": [{"message": "/x"}]}))
    second.write_text(json.dumps({"depHash": "aa", "log": [{"message": "/y"}]}))
    assert trace_digest(first) == trace_digest(second)
    assert trace_digest(tmp_path / "missing.trace") is None


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


def test_checker_helper_sources_are_identified(tmp_path):
    source = _project(tmp_path)
    checker = environment_fingerprint(source, source.read_text())["checker"]
    assert sorted(checker["programs"]) == [
        "AxiomReport.lean",
        "ProjectRead.lean",
        "ProjectReplay.lean",
        "ProjectReport.lean",
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
