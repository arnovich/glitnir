"""Project checks bind independently replayed observations to private snapshots."""

from pathlib import Path

import pytest
from conftest import settle

from glitnir.lean.project_snapshot import (
    SLOT_BUSY,
    SnapshotError,
    copy_file,
    snapshot_slot,
)

#: An `Example.Helper` whose compiled code, if it ever ran in a replay, would
#: print a forged report and exit 0. It must build without messages: Lean keeps
#: them, with the source's absolute path, in the `.olean`, and a record of the
#: closure then matches no other project's build.
EVIL_HELPER = (
    "def helper : Nat := 1\n"
    "@[noinline] unsafe def evilImpl (_ : Unit) : Bool :=\n"
    "  match unsafeBaseIO (do\n"
    '      IO.println "{\\"replay\\":\\"imports cached\\"}"\n'
    "      (← IO.getStdout).flush\n"
    "      IO.Process.exit 0 : IO Bool).toBaseIO with\n"
    "  | .ok b => b\n"
    "  | .error _ => true\n"
    "@[implemented_by evilImpl] def evil2 (_ : Unit) : Bool := false\n"
    "def evil : Bool := evil2 ()\n"
)


def test_snapshot_copies_bytes_without_sharing_mutable_files(tmp_path: Path) -> None:
    """Changes to a dependency after capture cannot change the copied bytes."""
    source, target = tmp_path / "input", tmp_path / "output"
    source.write_bytes(b"original")
    digest = copy_file(source, target, max_bytes=1024)
    source.write_bytes(b"changed")
    assert target.read_bytes() == b"original"
    assert source.stat().st_ino != target.stat().st_ino
    assert len(digest) == 64


def test_snapshot_limits_and_regular_files(tmp_path: Path) -> None:
    """A file copy cannot consume an unbounded stream or follow a final link."""
    source, target = tmp_path / "input", tmp_path / "output"
    source.write_bytes(b"too large")
    with pytest.raises(SnapshotError, match="limit"):
        copy_file(source, target, max_bytes=2)
    source.unlink()
    source.symlink_to(target)
    with pytest.raises(OSError):
        copy_file(source, tmp_path / "other", max_bytes=1024)


def test_snapshot_slot_serializes_checks() -> None:
    """Independent preview services cannot prepare large snapshots concurrently."""
    with snapshot_slot():
        with pytest.raises(SnapshotError) as raised:
            with snapshot_slot():
                pytest.fail("a second checker acquired the slot")
    assert str(raised.value) == SLOT_BUSY


def test_a_waiting_caller_takes_the_slot_once_it_is_free() -> None:
    """With a cancel, a busy slot is waited for and the wait is announced once."""
    import threading

    told: list[int] = []
    taken = threading.Event()
    held = snapshot_slot()
    held.__enter__()
    waiter = threading.Thread(
        target=lambda: _take(threading.Event(), told, taken), daemon=True
    )
    waiter.start()
    try:
        assert not taken.wait(0.6)
        assert told == [1]
    finally:
        held.__exit__(None, None, None)
    assert taken.wait(5)
    waiter.join(5)
    assert told == [1]


def test_a_check_waits_for_a_view_holding_the_slot(tmp_path, monkeypatch) -> None:
    """Views drawn one after another never keep a check out: it waits its turn."""
    import hashlib
    import threading

    from glitnir.lean import project_checking
    from glitnir.lean.project_checking import check_project
    from glitnir.lean.project_snapshot import SLOT_WAITING

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := trivial\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    reached = threading.Event()

    def toolchain():
        reached.set()
        raise ValueError("reached the check")

    monkeypatch.setattr(project_checking, "_toolchain", toolchain)
    messages: list[str] = []
    results: list[dict] = []
    held = snapshot_slot()
    held.__enter__()
    try:
        worker = threading.Thread(
            target=lambda: results.append(
                check_project(source, expected_sha256=digest, progress=messages.append)
            ),
            daemon=True,
        )
        worker.start()
        assert not reached.wait(0.6), "the check started beside the view"
        assert SLOT_WAITING in messages
    finally:
        held.__exit__(None, None, None)
    worker.join(10)
    assert reached.is_set()
    assert results[0]["message"].startswith("reached the check")


def _take(cancel, told, taken) -> None:
    with snapshot_slot(cancel=cancel, waiting=lambda: told.append(1)):
        taken.set()


@pytest.mark.parametrize("cancelled", [True, False])
def test_waiting_for_the_slot_ends_on_cancel_or_deadline(cancelled) -> None:
    import threading
    import time

    from glitnir.lean.project_snapshot import (
        SLOT_WAIT_CANCELLED,
        SLOT_WAIT_TIMED_OUT,
    )

    cancel = threading.Event()
    if cancelled:
        cancel.set()
    with snapshot_slot():
        with pytest.raises(SnapshotError) as raised:
            with snapshot_slot(cancel=cancel, deadline=time.monotonic() + 0.3):
                pytest.fail("took a busy slot")
    expected = SLOT_WAIT_CANCELLED if cancelled else SLOT_WAIT_TIMED_OUT
    assert str(raised.value) == expected
    # Both end in words the provider runner reads as transient.
    assert expected.endswith(("cancelled.", "timed out."))


def test_live_project_check_preserves_names_and_axiom_policy(prepared_lean_project):
    """Custom roots and a transitive import retain exact names and all assumptions."""
    import hashlib
    import os
    import subprocess

    from glitnir.lean.project_checking import check_project
    from glitnir.lean.project_inspection import _toolchain

    root = prepared_lean_project
    (root / "src/Example/Base.lean").write_text("def base : Nat := 1\n")
    (root / "src/Example/Helper.lean").write_text(
        "import Example.Base\ndef helper : Nat := base\n"
    )
    source = root / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "namespace A\ntheorem result : helper = 1 := rfl\nend A\n"
        "namespace B\ntheorem result : True := True.intro\nend B\n"
        "private theorem hidden : True := True.intro\n"
        "section\nvariable (n : Nat)\ntheorem reflexive : n = n := rfl\nend\n"
        "theorem equivalent (p q : Prop) (h : p ↔ q) : p = q := propext h\n"
        "theorem admitted : False := sorry\n"
        "axiom custom : False\ntheorem assumed : False := custom\n"
    )
    subprocess.run(
        [str(_toolchain() / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
    )
    before = source.read_bytes()
    result = check_project(source, expected_sha256=hashlib.sha256(before).hexdigest())
    assert result["status"] == "checked_snapshot", result
    found = {t["name"]: t for t in result["theorems"]}
    assert found["A.result"]["status"] == found["B.result"]["status"] == "checked"
    assert (found["A.result"]["start_line"], found["A.result"]["end_line"]) == (3, 3)
    assert (found["B.result"]["start_line"], found["B.result"]["end_line"]) == (6, 6)
    assert any(
        n.startswith("_private.Example.") and n.endswith("hidden") for n in found
    )
    assert found["reflexive"]["status"] == "checked"
    assert found["equivalent"]["status"] == "checked"
    assert found["equivalent"]["axioms"][0]["name"] == "propext"
    assert found["admitted"]["status"] == "admitted"
    assert found["assumed"]["status"] == "assumptions"
    assert found["assumed"]["axioms"] == [{"name": "custom", "module": "Example"}]
    assert source.read_bytes() == before
    assert not source.with_suffix(".forseti.json").exists()


def test_live_scoped_options_preserve_claims_and_ranges(prepared_lean_project):
    """Scoped and nested options keep named claims and their axiom statuses."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    text = (
        "namespace Scoped\n"
        "set_option maxHeartbeats 500000 in\n"
        "theorem result : True := True.intro\n"
        "set_option autoImplicit false in\n"
        "set_option autoImplicit true in\n"
        "set_option pp.explicit true in\n"
        "theorem identity (x : α) : x = x := rfl\n"
        "set_option maxHeartbeats 500000 in\n"
        "theorem admitted : False := sorry\n"
        "axiom custom : False\n"
        "set_option maxHeartbeats 500000 in\n"
        "theorem assumed : False := custom\n"
        "end Scoped\n"
    )
    source.write_text(text)
    result = check_project(
        source, expected_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    assert result.get("withheld") is None, result.get("withheld")
    found = {t["name"]: t for t in result["theorems"]}
    assert found["Scoped.result"]["status"] == "checked"
    assert (
        found["Scoped.result"]["start_line"],
        found["Scoped.result"]["end_line"],
    ) == (2, 3)
    assert found["Scoped.identity"]["status"] == "checked"
    assert "@Eq" in found["Scoped.identity"]["statement"]
    assert (
        found["Scoped.identity"]["start_line"],
        found["Scoped.identity"]["end_line"],
    ) == (4, 7)
    assert found["Scoped.admitted"]["status"] == "admitted"
    assert found["Scoped.assumed"]["status"] == "assumptions"
    assert source.read_text() == text


@pytest.mark.parametrize(
    ("text", "kept", "line", "message"),
    [
        (
            "theorem good : True := True.intro\n"
            "set_option maxHeartbeats 500000 in\n"
            "theorem bad : 1 = 2 := rfl\n",
            ["good"],
            2,
            "",
        ),
        (
            "set_option autoImplicit true in\n"
            "theorem inside (x : α) : x = x := rfl\n"
            "theorem outside (x : β) : x = x := rfl\n",
            ["inside"],
            3,
            "β",
        ),
        (
            "theorem good : True := True.intro\n"
            "set_option debug.skipKernelTC true in\n"
            "theorem bad : False := True.intro\n",
            ["good"],
            2,
            "type mismatch",
        ),
        (
            "theorem good : True := True.intro\n"
            "set_option maxHeartbeats 500000 in\n"
            "example : True := True.intro\n",
            ["good"],
            2,
            "unsupported scoped",
        ),
        (
            "theorem good : True := True.intro\n"
            "open Nat in theorem wrapped : True := True.intro\n",
            ["good"],
            2,
            "unsupported scoped",
        ),
    ],
)
def test_live_scoped_options_fail_closed(
    prepared_lean_project, text, kept, line, message
):
    """Options do not leak, mask bad proofs or enable unsupported scoped commands."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(text)
    result = check_project(
        source, expected_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    assert [t["name"] for t in result["theorems"]] == kept
    assert all(t["status"] == "checked" for t in result["theorems"])
    assert result["withheld"]["from_line"] == line
    assert message.casefold() in result["withheld"]["message"].casefold()


def test_live_scoped_option_check_cannot_split_the_command(prepared_lean_project):
    """A partial check includes the entire option wrapper and proof, or neither."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    text = (
        "theorem first : True := True.intro\n"
        "set_option maxHeartbeats 500000 in\n"
        "theorem wrapped : True := by\n"
        "  trivial\n"
        "  <;> trivial\n"
        "theorem after : True := True.intro\n"
    )
    source.write_text(text)
    for through in (2, 4, 5):
        result = check_project(
            source,
            expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
            through_line=through,
        )
        assert result["status"] == "checked_snapshot", result
        assert [t["name"] for t in result["theorems"]] == (
            ["first", "wrapped"] if through == 5 else ["first"]
        ), result
        assert all(t["status"] == "checked" for t in result["theorems"])


def test_live_claim_uses_name_their_notebook_module(prepared_lean_project):
    """A claim using another notebook's claim records it with that module.

    `Example` uses `Example.Helper`'s claim and one of its own; the standard
    library lemma it also names is no claim of the project.
    """
    import hashlib
    import os
    import subprocess

    from glitnir.lean.project_checking import check_project
    from glitnir.lean.project_inspection import _toolchain
    from glitnir.notebook.check_records import records_from_check, save_records
    from glitnir.service.preview import SourcePreviewService

    root = prepared_lean_project
    (root / "src/Example/Helper.lean").write_text(
        "def helper : Nat := 1\ntheorem helper_one : helper = 1 := rfl\n"
    )
    source = root / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "theorem local_one : helper = 1 := helper_one\n"
        "theorem both : helper = 1 ∧ True := ⟨local_one, trivial⟩\n"
        "theorem across : helper = 1 ∧ helper = 1 := ⟨helper_one, local_one⟩\n"
    )
    subprocess.run(
        [str(_toolchain() / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
    )
    text = source.read_text()
    result = check_project(
        source, expected_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    uses = {
        t["name"]: sorted((u["module"], u["name"], u["in_notebook"]) for u in t["uses"])
        for t in result["theorems"]
    }
    assert uses == {
        "local_one": [("Example.Helper", "helper_one", False)],
        "both": [("Example", "local_one", True)],
        "across": [
            ("Example", "local_one", True),
            ("Example.Helper", "helper_one", False),
        ],
    }
    save_records(source, records_from_check(text, result, "0" * 64))
    service = SourcePreviewService(source)
    try:
        claims = {c["name"]: c for c in service.snapshot()["project_claims"]}
    finally:
        service.close()
    [helper] = claims["local_one"]["uses"]
    assert helper["path"] == "Example/Helper.lean"


def test_live_check_through_the_first_of_two_claims(prepared_lean_project):
    """Only the first claim is rechecked; the second keeps its earlier result.

    A cut inside a command moves back to a command end; nothing is checked shortened.
    """
    import hashlib

    from glitnir.lean.project_checking import check_project
    from glitnir.service.preview import SourcePreviewService

    source = prepared_lean_project / "src/Example.lean"
    text = (
        "import Example.Helper\n"
        "theorem first : helper = 1 := by\n  rfl\n"
        "theorem second : True := trivial\n"
    )
    source.write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    service = SourcePreviewService(source)
    try:
        for through in (None, 3):
            service.check(sha, through)
            settle(service)
            assert service.snapshot()["preview"]["check"]["status"] == (
                "checked_snapshot"
            ), service.snapshot()["preview"]["check"]
        snapshot = service.snapshot()
    finally:
        service.close()
    check = snapshot["preview"]["check"]
    assert [t["name"] for t in check["theorems"]] == ["first"]
    assert check["withheld"]["through_line"] == 3
    assert snapshot["project_withheld"]["from_line"] == 4
    first, second = snapshot["project_claims"]
    assert (first["name"], first["current"]) == ("first", True)
    assert "not_rechecked" not in first
    assert (second["name"], second["current"]) == ("second", True)
    assert second["not_rechecked"] is True
    # A cut where Lean would read the proof on is never checked shortened:
    # it moves back to the end of the command before.
    text = (
        "import Example.Helper\n"
        "theorem first : True := by\n  trivial\n  <;> trivial\n"
        "theorem second : True := trivial\n"
    )
    source.write_text(text)
    split = check_project(
        source,
        expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
        through_line=3,
    )
    assert split["status"] == "checked_snapshot", split
    assert split["theorems"] == []
    assert split["withheld"] == {
        "from_line": 2,
        "message": "Checked through line 1 on request (line 3 is inside a command)",
        "through_line": 1,
    }


def test_isolation_rejects_symlink_ancestors(tmp_path):
    """The parent must not follow directories created by the candidate process."""
    from glitnir.lean.project_checking import _isolate

    build, outside, isolated = (tmp_path / n for n in ("build", "outside", "isolated"))
    for directory in (build, outside, isolated):
        directory.mkdir()
    (outside / "Victim.olean").write_bytes(b"secret")
    (build / "Nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _isolate(build, isolated, "Nested.Victim")
    assert not (isolated / "Nested/Victim.olean").exists()


def test_snapshot_cancellation_and_global_limit(tmp_path, monkeypatch):
    """Input copying and traversal terminate before executing any project code."""
    import threading
    import time

    from glitnir.lean import project_snapshot as snapshots

    cancel = threading.Event()
    source = tmp_path / "input"
    source.write_bytes(b"bytes")
    cancel.set()
    with pytest.raises(SnapshotError, match="cancelled"):
        copy_file(source, tmp_path / "cancelled", max_bytes=100, cancel=cancel)
    with pytest.raises(SnapshotError, match="timed out"):
        copy_file(source, tmp_path / "expired", max_bytes=100, deadline=0)
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_FILES", 1)
    with pytest.raises(SnapshotError, match="file limit"):
        list(snapshots._walk_files(tmp_path, threading.Event(), time.monotonic() + 10))


def test_scratch_budget_counts_total_bytes_and_ignores_links(tmp_path, monkeypatch):
    """Many individually small outputs cannot escape the aggregate scratch cap."""
    from glitnir.lean import project_checking as checking

    root = tmp_path / "stage"
    root.mkdir()
    secret = tmp_path / "outside"
    secret.write_bytes(b"x" * 100)
    (root / "link").symlink_to(secret)
    monkeypatch.setattr(checking, "STAGE_BYTES", 10)
    checking._check_stage(root)
    (root / "one").write_bytes(b"x" * 6)
    (root / "two").write_bytes(b"x" * 6)
    with pytest.raises(SnapshotError, match="scratch byte limit"):
        checking._check_stage(root)
    monkeypatch.setattr(checking, "STAGE_ENTRIES", 1)
    with pytest.raises(SnapshotError, match="scratch file limit"):
        checking._check_stage(root)


def test_output_limit_checks_size_after_child_seeks(tmp_path, monkeypatch):
    """A finished child cannot hide excess output by rewinding its descriptor."""
    import subprocess
    import sys
    import threading
    import time

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_snapshot import ProjectSnapshot

    snapshot = ProjectSnapshot(tmp_path, tmp_path, tmp_path, tmp_path, {}, "id", 0, 0)
    original = subprocess.Popen

    def completed(*args, **kwargs):
        process = original(*args, **kwargs)
        process.wait(timeout=10)
        return process

    monkeypatch.setattr(checking.subprocess, "Popen", completed)
    monkeypatch.setattr(checking, "OUTPUT_BYTES", 16)
    monkeypatch.setattr(
        checking,
        "confine",
        lambda *a, **kw: [
            sys.executable,
            "-c",
            "import os; os.write(1,b'x'*32); os.lseek(1,0,0)",
        ],
    )
    with pytest.raises(SnapshotError, match="output limit"):
        checking._run(
            snapshot,
            ["lean"],
            cwd=tmp_path,
            readable=[],
            writable=False,
            cancel=threading.Event(),
            deadline=time.monotonic() + 15,
        )


def test_live_project_check_rejects_forged_import(prepared_lean_project):
    """Ordinary imports trust a forged proof; fresh kernel replay rejects it."""
    import hashlib
    import os
    import subprocess

    from glitnir.lean.project_checking import check_project
    from glitnir.lean.project_inspection import _toolchain

    root = prepared_lean_project
    toolchain = _toolchain()
    source = root / "src/Example.lean"
    source.write_text("import Example.Helper\ntheorem result : False := bad\n")
    output = root / ".lake/build/lib/lean/Example/Helper.olean"
    fixture = Path(__file__).parent / "fixtures/project_checking/ForgeImport.lean"
    environment = {
        **os.environ,
        "LEAN_SYSROOT": str(toolchain),
        "LEAN_PATH": str(root / ".lake/build/lib/lean"),
    }
    subprocess.run(
        [str(toolchain / "bin/lean"), "--run", str(fixture), str(output)],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        [str(toolchain / "bin/lean"), str(source)],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        timeout=60,
    )
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "unavailable", result
    assert result["theorems"] == []
    assert "Independent kernel replay failed" in result["message"], result
    assert "timed out" not in result["message"]


def test_live_second_check_reuses_the_imported_replay(
    prepared_lean_project, monkeypatch, fresh_replay_cache
):
    """Imports are replayed once on their own; a repeated check reuses that."""
    import hashlib

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_checking import check_project

    original = checking.run_observer
    modes = []

    def observed(lean, arguments, **options):
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            modes.append(arguments[-1])
        return original(lean, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", observed)
    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "theorem result : helper = 1 := rfl\n"
        "theorem admitted : False := sorry\n"
        "axiom custom : False\ntheorem assumed : False := custom\n"
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    first = check_project(source, expected_sha256=digest)
    second = check_project(source, expected_sha256=digest)
    for result in (first, second):
        assert result["status"] == "checked_snapshot", result
    assert (first["replay"], second["replay"]) == ("full", "imports cached")
    assert modes == ["imports-only", "imports-cached", "imports-cached"]
    statuses = {t["name"]: t["status"] for t in second["theorems"]}
    assert statuses == {
        "result": "checked",
        "admitted": "admitted",
        "assumed": "assumptions",
    }
    assert [(t["name"], t["status"], t["axioms"]) for t in first["theorems"]] == [
        (t["name"], t["status"], t["axioms"]) for t in second["theorems"]
    ]


@pytest.mark.parametrize("cached", [False, True])
def test_live_replay_collects_the_axioms_that_decide_statuses(
    prepared_lean_project, monkeypatch, cached, fresh_replay_cache
):
    """The kernel replay's own axioms decide statuses, also across an import.

    `sorry` in an imported lemma reaches the notebook theorem that uses it, on a
    full replay and over the rebuilt imports alike.
    """
    import hashlib
    import json
    import os
    import subprocess

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_inspection import _toolchain

    root = prepared_lean_project
    (root / "src/Example/Helper.lean").write_text(
        "def helper : Nat := 1\ntheorem gap : helper = 1 := sorry\n"
    )
    source = root / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "theorem result : helper = 1 := rfl\n"
        "theorem admitted : False := sorry\n"
        "axiom custom : False\ntheorem assumed : False := custom\n"
        "theorem inherited : helper = 1 := gap\n"
    )
    subprocess.run(
        [str(_toolchain() / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
    )
    if not cached:
        monkeypatch.setattr(checking, "_replay_identity", lambda *args: None)
    original = checking.run_observer
    replays = []

    def observed(lean, arguments, **options):
        output = original(lean, arguments, **options)
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            replays.append((arguments[-1], json.loads(output)))
        return output

    monkeypatch.setattr(checking, "run_observer", observed)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = checking.check_project(source, expected_sha256=digest)
    assert result["status"] == "checked_snapshot", result
    modes = [mode for mode, _ in replays]
    assert modes == (["imports-only", "imports-cached"] if cached else ["full"])
    if cached:
        assert replays[0][1]["theorems"] == []  # The imports alone.
    replayed = {
        t["name"]: [a["name"] for a in t["axioms"]] for t in replays[-1][1]["theorems"]
    }
    assert replayed["result"] == []
    assert "sorryAx" in replayed["admitted"]
    assert "sorryAx" in replayed["inherited"]
    assert replayed["assumed"] == ["custom"]
    found = {t["name"]: t for t in result["theorems"]}
    assert {name: t["status"] for name, t in found.items()} == {
        "result": "checked",
        "admitted": "admitted",
        "assumed": "assumptions",
        "inherited": "admitted",
    }
    assert found["assumed"]["axioms"] == [{"name": "custom", "module": "Example"}]
    for name, theorem in found.items():
        assert [a["name"] for a in theorem["axioms"]] == replayed[name]


def _build(root):
    """Build `Example` in the test project with the supported toolchain."""
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain

    subprocess.run(
        [str(_toolchain() / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
    )


def _replays(monkeypatch):
    """Record each ProjectReplay run's mode and JSON report."""
    import json

    from glitnir.lean import project_checking as checking

    original = checking.run_observer
    replays = []

    def observed(lean, arguments, **options):
        output = original(lean, arguments, **options)
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            replays.append((arguments[-1], json.loads(output)))
        return output

    monkeypatch.setattr(checking, "run_observer", observed)
    return replays


@pytest.mark.parametrize("cached", [False, True])
def test_live_axioms_are_collected_after_replay_into_an_empty_environment(
    prepared_lean_project, monkeypatch, cached, fresh_replay_cache
):
    """Every axiom is found in the replayed environment, imported ones included.

    The replay starts from `mkEmptyEnvironment`, so every constant it holds was
    added after the import (the constant map's second stage). An axiom
    collection that looked only at imported constants would find none here and
    call these theorems checked; they each depend on all three standard axioms,
    reached only through the toolchain's and a dependency's own theorems.
    """
    import hashlib

    from glitnir.lean import project_checking as checking

    root = prepared_lean_project
    (root / "src/Example/Helper.lean").write_text(
        "def helper : Nat := 1\n"
        "theorem decidedly (p : Prop) : p ∨ ¬p := Classical.em p\n"
    )
    source = root / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "theorem direct (p : Prop) : p ∨ ¬p := Classical.em p\n"
        "theorem imported (p : Prop) : p ∨ ¬p := decidedly p\n"
        "theorem plain : helper = 1 := rfl\n"
    )
    _build(root)
    if not cached:
        monkeypatch.setattr(checking, "_replay_identity", lambda *args: None)
    replays = _replays(monkeypatch)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = checking.check_project(source, expected_sha256=digest)
    assert result["status"] == "checked_snapshot", result
    modes = [mode for mode, _ in replays]
    assert modes == (["imports-only", "imports-cached"] if cached else ["full"])
    standard = ["Classical.choice", "Quot.sound", "propext"]
    replayed = {
        t["name"]: sorted(a["name"] for a in t["axioms"])
        for t in replays[-1][1]["theorems"]
    }
    assert replayed == {"direct": standard, "imported": standard, "plain": []}
    found = {t["name"]: t for t in result["theorems"]}
    assert {name: t["status"] for name, t in found.items()} == {
        "direct": "checked",
        "imported": "checked",
        "plain": "checked",
    }
    for name in ("direct", "imported"):
        axioms = found[name]["axioms"]
        assert sorted(a["name"] for a in axioms) == standard
        assert all(a["module"].startswith("Init") for a in axioms), axioms


def test_live_full_and_cached_replays_decide_alike_through_imported_inductives(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """A full replay and one over the rebuilt imports decide every claim alike.

    The rebuilt imports hold the imported inductive types, constructors,
    recursors and quotient as their files store them, where a full replay holds
    the kernel's own. These proofs reduce through them: recursors (`List`,
    `Option`), structure eta, `Quot.lift` and a nested inductive (`Syntax`).
    """
    import hashlib

    from glitnir.lean import project_checking as checking

    recorded_replay_cache()
    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "theorem result : helper = 1 := rfl\n"
        "theorem lengths : [1, 2, 3].length = 3 := rfl\n"
        "theorem eta (p : Nat × Nat) : (p.1, p.2) = p := rfl\n"
        "theorem lifted (r : Nat → Nat → Prop) (f : Nat → Nat)\n"
        "    (h : ∀ a b, r a b → f a = f b) (a : Nat) :\n"
        "    Quot.lift f h (Quot.mk r a) = f a := rfl\n"
        "theorem mapped : (some 2).map (· + 1) = some 3 := rfl\n"
        "theorem nested :\n"
        "    (Lean.Syntax.node .none `k #[.missing, .missing]).getNumArgs = 2 := rfl\n"
        "theorem decided : 10 < 20 := by decide\n"
        "theorem classical (p : Prop) : p ∨ ¬p := Classical.em p\n"
    )
    replays = _replays(monkeypatch)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with monkeypatch.context() as uncached:
        uncached.setattr(checking, "_replay_identity", lambda *args: None)
        full = checking.check_project(source, expected_sha256=digest)
    cached = checking.check_project(source, expected_sha256=digest)
    assert [mode for mode, _ in replays] == ["full", "imports-cached"]
    for result in (full, cached):
        assert result["status"] == "checked_snapshot", result
    assert (full["replay"], cached["replay"]) == ("full", "imports cached")
    decided = [(t["name"], t["status"], t["axioms"]) for t in full["theorems"]]
    assert decided == [
        (t["name"], t["status"], t["axioms"]) for t in cached["theorems"]
    ]
    assert {name: status for name, status, _ in decided} == dict.fromkeys(
        (
            "result",
            "lengths",
            "eta",
            "lifted",
            "mapped",
            "nested",
            "decided",
            "classical",
        ),
        "checked",
    )


def _forge_constant(olean, mode, name, module):
    """Rewrite a built `.olean` in place with `ForgeConstant.lean` (see there)."""
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain

    forged = olean.with_name("Forged.olean")
    fixture = Path(__file__).parent / "fixtures/project_checking/ForgeConstant.lean"
    subprocess.run(
        [str(_toolchain() / "bin/lean"), "--run", str(fixture), mode, name, module]
        + [str(olean), str(forged)],
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain()), "LEAN_PATH": ""},
        check=True,
        capture_output=True,
        timeout=60,
    )
    forged.replace(olean)


def _records(cache):
    """The replay cache's entries, by file name."""
    return sorted(p.name for p in cache.iterdir()) if cache.exists() else []


@pytest.mark.parametrize("forgery", ["identity", "binder", "data"])
def test_live_an_imported_recursor_unlike_the_kernels_is_never_recorded(
    prepared_lean_project, monkeypatch, fresh_replay_cache, forgery
):
    """An imported recursor is recorded only if it is exactly the kernel's own.

    `Example.Helper`'s file is rewritten so that `Two.rec`'s type is `==` to the
    kernel's recursor but not alike: its motive is renamed (`binder`) or its
    root node's cached level-parameter flag is cleared (`data`, which would let
    the kernel skip instantiating the motive's universe). A full replay holds
    the kernel's own recursor and accepts the file; the rebuilt imports would
    hold the file's copy, so the import replay records nothing and the check
    replays in full. `identity` rewrites the file unchanged and is recorded.
    The notebook reduces through `Two.rec` by way of `Two.casesOn`, whose own
    type is unchanged: naming `Two.rec` would make the elaborator, which trusts
    the file, read the forged type, and the theorem would not compile.
    """
    root = prepared_lean_project
    (root / "src/Example/Helper.lean").write_text(
        "def helper : Nat := 1\ninductive Two where\n  | one\n  | two\n"
    )
    source = root / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "theorem result :\n"
        "    Two.casesOn (motive := fun _ => Nat) Two.two 1 2 = 2 := rfl\n"
    )
    _build(root)
    olean = root / ".lake/build/lib/lean/Example/Helper.olean"
    _forge_constant(olean, forgery, "Two.rec", "Example.Helper")
    result, modes = _check_recording_modes(source, monkeypatch)
    assert result["status"] == "checked_snapshot", result
    assert result["replay"] == "full", result
    assert [(t["name"], t["status"]) for t in result["theorems"]] == [
        ("result", "checked")
    ]
    if forgery == "identity":
        assert modes == ["imports-only", "imports-cached"]
        assert _records(fresh_replay_cache)
    else:
        assert modes == ["imports-only", "full"]
        assert not _records(fresh_replay_cache)


#: A base whose theorem `two` `Example.Extra` restates.
TWO_HELPER = "def helper : Nat := 1\ntheorem two : (1 : Nat) + 1 = 2 := rfl\n"


@pytest.mark.parametrize("forgery", ["identity", "data"])
def test_live_a_theorem_restated_over_a_base_must_be_alike_to_extend_it(
    prepared_lean_project, monkeypatch, recorded_replay_cache, forgery
):
    """A base theorem restated by a later module extends the base only if alike.

    `Example.Extra` declares `two` as `Example.Helper` does, without importing
    it, and the notebook imports both: Lean keeps the later copy, Extra's, so
    the rebuilt imports would hold that copy, which the replay of the base
    never checked. An identical copy extends the recorded base. A copy `==` to
    the base's but with a cached flag changed (`data`) does not: the imports
    are replayed from scratch, so the kernel checks that copy itself, and only
    then recorded.
    """
    import hashlib
    import json

    from glitnir.lean import project_checking as checking

    root = prepared_lean_project
    source = root / "src/Example.lean"
    recorded_replay_cache(TWO_HELPER)
    _grow_closure(
        root,
        "theorem two : (1 : Nat) + 1 = 2 := rfl\n",
        "import Example.Helper\nimport Example.Extra\n"
        "theorem result : (1 : Nat) + 1 = 2 := two\n",
        helper=TWO_HELPER,
    )
    olean = root / ".lake/build/lib/lean/Example/Extra.olean"
    _forge_constant(olean, forgery, "two", "Example.Extra")
    original = checking.run_observer
    replays = []

    def observed(lean, arguments, **options):
        output = original(lean, arguments, **options)
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            replays.append(json.loads(output)["replay"])
        return output

    monkeypatch.setattr(checking, "run_observer", observed)
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    assert [(t["name"], t["status"]) for t in result["theorems"]] == [
        ("result", "checked")
    ]
    if forgery == "identity":
        assert replays == ["imports incremental", "imports cached"]
        assert result["replay"] == "imports cached (+1 module)", result
    else:
        assert replays == ["imports only", "imports cached"]
        assert result["replay"] == "full", result


@pytest.mark.parametrize(
    "edit",
    [
        # A field missing: the objects after it are read from the wrong slots.
        ("  irBaseExts : NonScalar\n", ""),
        # A flag before `quotInit`: the objects match, the flag's byte does not.
        ("  quotInit : Bool\n", "  padding : Bool\n  quotInit : Bool\n"),
    ],
    ids=["missing_field", "moved_flag"],
)
def test_live_a_kernel_environment_mirror_that_reads_other_fields_fails_closed(
    prepared_lean_project, monkeypatch, fresh_replay_cache, edit
):
    """`rebuild` builds nothing through a mirror unlike Lean's own layout.

    `KernelFields` is changed as after a Lean whose layout moved: the import
    replay stops before replaying anything, nothing is recorded and no claim
    gets a status.
    """
    import hashlib

    from glitnir.lean import project_checking as checking

    programs = checking._programs
    old, new = edit

    def misread(destination):
        reader, reporter, replay = programs(destination)
        text = replay.read_text()
        start = text.index("structure KernelFields where")
        assert text.count(old, start, start + 400) == 1
        replay.write_text(text[:start] + text[start:].replace(old, new, 1))
        return reader, reporter, replay

    monkeypatch.setattr(checking, "_programs", misread)
    source = prepared_lean_project / "src/Example.lean"
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "unavailable", result
    assert result["theorems"] == []
    assert "Independent kernel replay failed" in result["message"], result
    assert "unexpected kernel environment layout" in result["message"], result
    assert not _records(fresh_replay_cache)


def test_live_native_decide_reports_assumptions_never_standard_axioms(
    prepared_lean_project, monkeypatch, fresh_replay_cache
):
    """Lean 4.35's `native_decide` adds an axiom of the module that used it.

    That axiom is outside Lean's standard set, so the theorem's status is
    `assumptions`, on a full replay and over cached imports alike, while the
    kernel's own `decide` stays checked.
    """
    import hashlib

    from glitnir.lean import project_checking as checking

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "import Example.Helper\n"
        "theorem native : 2 + 2 = 4 := by native_decide\n"
        "theorem kernel : 2 + 2 = 4 := by decide\n"
    )
    replays = _replays(monkeypatch)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    for replay in ("full", "imports cached"):
        result = checking.check_project(source, expected_sha256=digest)
        assert result["status"] == "checked_snapshot", result
        assert result["replay"] == replay, result
        found = {t["name"]: t for t in result["theorems"]}
        assert found["kernel"]["status"] == "checked"
        assert found["kernel"]["axioms"] == []
        assert found["native"]["status"] == "assumptions", found["native"]
        (axiom,) = found["native"]["axioms"]
        assert axiom["module"] == "Example", axiom
        assert axiom["name"].startswith("native._native.native_decide.ax"), axiom
        assert axiom["name"] not in checking.LEAN_AXIOM_ALLOWLIST
    assert [mode for mode, _ in replays] == [
        "imports-only",
        "imports-cached",
        "imports-cached",
    ]


def test_live_a_forged_standard_axiom_is_reported_as_an_assumption(
    prepared_lean_project,
):
    """An axiom named like a standard one, but not the toolchain's, is assumed.

    The dependency declares its own `propext` (of `False`) over `Init.Prelude`
    alone, which leaves out the toolchain's `Init.Core` and its `propext`.
    """
    import hashlib

    from glitnir.lean.project_checking import check_project

    root = prepared_lean_project
    (root / "src/Example/Helper.lean").write_text(
        "prelude\nimport Init.Prelude\naxiom propext : False\n"
    )
    source = root / "src/Example.lean"
    source.write_text(
        "prelude\nimport Example.Helper\ntheorem forged : False := propext\n"
    )
    _build(root)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = check_project(source, expected_sha256=digest)
    assert result["status"] == "checked_snapshot", result
    (theorem,) = result["theorems"]
    assert theorem["name"] == "forged"
    assert theorem["status"] == "assumptions", theorem
    assert theorem["axioms"] == [{"name": "propext", "module": "Example.Helper"}]


def test_live_imports_without_the_kernel_quotient_replay_fresh(
    prepared_lean_project, fresh_replay_cache
):
    """Imports lacking `init_quot` never serve as a base: replay from empty.

    The reader elaborates statements with `sorryAx _ true` in place of proofs,
    which `Init.Prelude` declares, so without it a theorem is withheld. The
    dependency declares its own `Bool` and `sorryAx`, without the quotient.
    """
    import hashlib
    import os
    import subprocess

    from glitnir.lean.project_checking import check_project
    from glitnir.lean.project_inspection import _toolchain

    root = prepared_lean_project
    (root / "src/Example/Helper.lean").write_text(
        "prelude\nuniverse u\n"
        "inductive Bool : Type\n  | false\n  | true\nexport Bool (false true)\n"
        "axiom sorryAx (α : Sort u) (synthetic : Bool) : α\n"
        "inductive MyTrue : Prop\n  | intro\n"
    )
    source = root / "src/Example.lean"
    source.write_text(
        "prelude\nimport Example.Helper\ntheorem t : MyTrue := MyTrue.intro\n"
    )
    subprocess.run(
        [str(_toolchain() / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    for _ in range(2):
        result = check_project(source, expected_sha256=digest)
        assert result["status"] == "checked_snapshot", result
        assert result["replay"] == "full", result
        assert result["withheld"] is None, result
        assert [(t["name"], t["status"]) for t in result["theorems"]] == [
            ("t", "checked")
        ]


#: Lean 4.35 removed `Lean.reduceBool`, `Lean.ofReduceBool` and their `Nat`
#: forms, so the kernel runs no compiled code. A proof naming them no longer
#: elaborates: the check withholds it, with no status.
REMOVED_REDUCTION = [
    "theorem windows : System.Platform.isWindows = false :=\n"
    "  Lean.ofReduceBool _ _ rfl\n",
    "theorem reduced : Lean.reduceBool true = true := rfl\n",
    "theorem counted : Lean.reduceNat 2 = 2 := rfl\n",
]


@pytest.mark.parametrize("proof", REMOVED_REDUCTION)
def test_live_removed_kernel_reduction_is_withheld_on_the_cached_path(
    prepared_lean_project, monkeypatch, recorded_replay_cache, proof
):
    """A proof by `Lean.ofReduceBool` gets no status, over reused imports too."""
    import hashlib

    from glitnir.lean import project_checking as checking

    recorded_replay_cache()
    original = checking.run_observer
    modes = []

    def observed(lean, arguments, **options):
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            modes.append(arguments[-1])
        return original(lean, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", observed)
    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "import Example.Helper\ntheorem result : helper = 1 := rfl\n" + proof
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    for _ in range(2):
        result = checking.check_project(source, expected_sha256=digest)
        assert result["status"] == "checked_snapshot", result
        assert result["replay"] == "imports cached", result
        assert [(t["name"], t["status"]) for t in result["theorems"]] == [
            ("result", "checked")
        ], result
        assert result["withheld"]["from_line"] == 3, result
        assert "Unknown" in result["withheld"]["message"], result
    assert modes == ["imports-cached", "imports-cached"]


def test_live_dependency_ir_cannot_run_in_the_cached_replay(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """A dependency's `implemented_by` code never runs while the module replays.

    The dependency's IR would print a forged replay report and exit 0. The
    module, forged just before its replay, adds an unchecked theorem of `False`
    and one whose statement would have had the kernel evaluate that IR through
    Lean 4.34 (`Lean.reduceBool`, which Lean 4.35 no longer declares).
    """
    import hashlib
    import os
    import subprocess

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_inspection import _toolchain

    root = prepared_lean_project
    toolchain = _toolchain()
    recorded_replay_cache(EVIL_HELPER)
    (root / "src/Example/Helper.lean").write_text(EVIL_HELPER)
    source = root / "src/Example.lean"
    source.write_text("import Example.Helper\ntheorem result : helper = 1 := rfl\n")
    subprocess.run(
        [str(toolchain / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(toolchain)},
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    fixture = Path(__file__).parent / "fixtures/project_checking/ForgeTrigger.lean"
    original = checking.run_observer
    modes = []

    def forged(lean, arguments, **options):
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            modes.append(arguments[-1])
            if arguments[-1] != "imports-only":
                olean = Path(arguments[5]) / "Example.olean"
                output = olean.with_name("Forged.olean")
                subprocess.run(
                    [str(toolchain / "bin/lean"), "--run", str(fixture), str(olean)]
                    + [str(output)],
                    env={
                        **os.environ,
                        "LEAN_SYSROOT": str(toolchain),
                        "LEAN_PATH": "",
                    },
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                olean.unlink()
                output.rename(olean)
        return original(lean, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", forged)
    result = checking.check_project(source, expected_sha256=digest)
    assert modes == ["imports-cached"]
    assert result["status"] == "unavailable", result
    assert result["theorems"] == []
    assert "Independent kernel replay failed" in result["message"], result
    # Whichever forged constant the replay reaches first fails it.
    assert "unknown constant 'Lean.reduceBool'" in result["message"] or (
        "forgedLocal" in result["message"]
    ), result


def test_live_cached_replay_still_checks_the_module(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """With imports reused, a forged constant in the module itself is rejected."""
    import hashlib
    import os
    import subprocess

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_inspection import _toolchain

    recorded_replay_cache()
    source = prepared_lean_project / "src/Example.lean"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    toolchain = _toolchain()
    fixture = Path(__file__).parent / "fixtures/project_checking/ForgeModule.lean"
    original = checking.run_observer
    modes = []

    def forged(lean, arguments, **options):
        # Add an unchecked false theorem to the module just before its replay.
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            modes.append(arguments[-1])
            olean = Path(arguments[5]) / "Example.olean"
            output = olean.with_name("Forged.olean")
            subprocess.run(
                [str(toolchain / "bin/lean"), "--run", str(fixture), str(olean)]
                + [str(output)],
                env={**os.environ, "LEAN_SYSROOT": str(toolchain), "LEAN_PATH": ""},
                check=True,
                capture_output=True,
                timeout=60,
            )
            olean.unlink()
            output.rename(olean)
        return original(lean, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", forged)
    result = checking.check_project(source, expected_sha256=digest)
    assert modes == ["imports-cached"]
    assert result["status"] == "unavailable", result
    assert "Independent kernel replay failed" in result["message"], result
    assert "forgedLocal" in result["message"], result


def _grow_closure(root, extra, notebook, helper=None):
    """Add `Example.Extra` (over `Example.Helper`) and rebuild the project."""
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain

    if helper is not None:
        (root / "src/Example/Helper.lean").write_text(helper)
    (root / "src/Example/Extra.lean").write_text(extra)
    (root / "src/Example.lean").write_text(notebook)
    subprocess.run(
        [str(_toolchain() / "bin/lake"), "--keep-toolchain", "build", "Example"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "LEAN_SYSROOT": str(_toolchain())},
    )


def _check_recording_modes(source, monkeypatch, forge=None):
    """Check `source`, returning the result and the replay modes it ran.

    ``forge(olean)``, when given, rewrites `Example.Extra`'s isolated `.olean`
    just before an incremental import replay reads it.
    """
    import hashlib

    from glitnir.lean import project_checking as checking

    original = checking.run_observer
    modes = []

    def observed(lean, arguments, **options):
        if "--run" in arguments and arguments[3].endswith("ProjectReplay.lean"):
            mode = (
                "imports-incremental"
                if "imports-incremental" in arguments
                else arguments[-1]
            )
            modes.append(mode)
            if forge is not None and mode == "imports-incremental":
                forge(Path(arguments[5]) / "Example/Extra.olean")
        return original(lean, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", observed)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        return checking.check_project(source, expected_sha256=digest), modes
    finally:
        monkeypatch.setattr(checking, "run_observer", original)


def _forge_with(fixture):
    """Replace an isolated `.olean` (a link) with a forged copy made by `fixture`."""
    import os
    import subprocess

    from glitnir.lean.project_inspection import _toolchain

    def forge(olean):
        output = olean.with_name("Forged.olean")
        subprocess.run(
            [str(_toolchain() / "bin/lean"), "--run", str(fixture), str(olean)]
            + [str(output)],
            env={**os.environ, "LEAN_SYSROOT": str(_toolchain()), "LEAN_PATH": ""},
            check=True,
            capture_output=True,
            timeout=60,
        )
        olean.unlink()
        output.rename(olean)

    return forge


def test_live_a_superset_replays_only_its_new_module(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """A recorded closure is reused for a larger one; only the new module replays."""
    root = prepared_lean_project
    source = root / "src/Example.lean"
    recorded_replay_cache()
    _grow_closure(
        root,
        "import Example.Helper\ndef extra : Nat := helper + 1\n",
        "import Example.Extra\ntheorem result : extra = 2 := rfl\n",
    )
    grown, modes = _check_recording_modes(source, monkeypatch)
    assert grown["status"] == "checked_snapshot", grown
    assert grown["replay"] == "imports cached (+1 module)", grown
    assert [(t["name"], t["status"]) for t in grown["theorems"]] == [
        ("result", "checked")
    ]
    assert modes == ["imports-incremental", "imports-cached"]
    again, modes = _check_recording_modes(source, monkeypatch)
    assert again["replay"] == "imports cached", again
    assert modes == ["imports-cached"]


def test_live_a_changed_base_module_is_not_reused(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """A base module whose files changed disqualifies its record."""
    root = prepared_lean_project
    source = root / "src/Example.lean"
    recorded_replay_cache()
    _grow_closure(
        root,
        "import Example.Helper\ndef extra : Nat := helper + 1\n",
        "import Example.Extra\ntheorem result : extra = 3 := rfl\n",
        helper="def helper : Nat := 2\n",
    )
    changed, modes = _check_recording_modes(source, monkeypatch)
    assert changed["status"] == "checked_snapshot", changed
    assert changed["replay"] == "full", changed
    assert modes == ["imports-only", "imports-cached"]


def test_live_a_forged_new_module_is_rejected_over_a_base(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """The new module's constants are kernel-checked: a forged one fails."""
    root = prepared_lean_project
    source = root / "src/Example.lean"
    recorded_replay_cache()
    _grow_closure(
        root,
        "import Example.Helper\ndef extra : Nat := helper + 1\n",
        "import Example.Extra\ntheorem result : extra = 2 := rfl\n",
    )
    fixture = Path(__file__).parent / "fixtures/project_checking/ForgeModule.lean"
    result, modes = _check_recording_modes(
        source, monkeypatch, forge=_forge_with(fixture)
    )
    assert modes == ["imports-incremental"]
    assert result["status"] == "unavailable", result
    assert "while replaying the imported modules once" in result["message"], result
    assert "forgedLocal" in result["message"], result


def test_live_native_decide_in_a_new_module_is_an_assumption(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """`native_decide` over a base is an assumption of the module that used it."""
    root = prepared_lean_project
    source = root / "src/Example.lean"
    recorded_replay_cache()
    _grow_closure(
        root,
        "import Example.Helper\ntheorem decided : helper + 1 = 2 := by native_decide\n",
        "import Example.Extra\ntheorem result : helper + 1 = 2 := decided\n",
    )
    result, modes = _check_recording_modes(source, monkeypatch)
    assert modes == ["imports-incremental", "imports-cached"]
    assert result["status"] == "checked_snapshot", result
    assert result["replay"] == "imports cached (+1 module)", result
    (theorem,) = result["theorems"]
    assert theorem["status"] == "assumptions", theorem
    (axiom,) = theorem["axioms"]
    assert axiom["module"] == "Example.Extra", axiom
    assert axiom["name"].startswith("decided._native.native_decide.ax"), axiom


def test_live_base_ir_cannot_run_while_a_new_module_replays(
    prepared_lean_project, monkeypatch, recorded_replay_cache
):
    """A base module's `implemented_by` code never runs in the incremental replay.

    As in the cached replay: the forged module names `Lean.reduceBool`, which
    Lean 4.35 no longer declares, and an unchecked theorem of `False`.
    """
    root = prepared_lean_project
    source = root / "src/Example.lean"
    recorded_replay_cache(EVIL_HELPER)
    _grow_closure(
        root,
        "import Example.Helper\ndef extra : Nat := helper + 1\n",
        "import Example.Extra\ntheorem result : extra = 2 := rfl\n",
        helper=EVIL_HELPER,
    )
    fixture = Path(__file__).parent / "fixtures/project_checking/ForgeTrigger.lean"
    result, modes = _check_recording_modes(
        source, monkeypatch, forge=_forge_with(fixture)
    )
    assert modes == ["imports-incremental"]
    assert result["status"] == "unavailable", result
    assert result["theorems"] == []
    # Whichever forged constant the replay reaches first fails it.
    assert "unknown constant 'Lean.reduceBool'" in result["message"] or (
        "forgedLocal" in result["message"]
    ), result


@pytest.mark.parametrize(
    ("text", "kept", "line", "message"),
    [
        (
            "theorem good : True := True.intro\ndef bad : Nat := False\n",
            ["good"],
            2,
            "type mismatch",
        ),
        (
            "theorem good : True := True.intro\n"
            "theorem bad : 1 = 2 := rfl\ntheorem after : True := True.intro\n",
            ["good"],
            2,
            "",
        ),
        (
            "set_option debug.skipKernelTC true\nexample : False := True.intro\n",
            [],
            2,
            "type mismatch",
        ),
        (
            # As a theorem, the example sees only the variables its statement
            # mentions; Lean's own example would also include `h`.
            "theorem good : True := True.intro\nsection\n"
            "variable (n : Nat) (h : n > 0)\nexample : n ≥ 1 := h\nend\n",
            ["good"],
            4,
            "section variables its statement mentions",
        ),
        (
            "theorem good : True := True.intro\n"
            "example : decl_name% ≠ `_example := by decide\n",
            ["good"],
            2,
            "decl_name%",
        ),
        (
            # The lexer cannot read past the interpolated string, so it knows
            # of no command end after line 1: the check stops there.
            'theorem good : True := True.intro\ndef s : String := s!"{1}"\n'
            "example : True := trivial\n",
            ["good"],
            2,
            "cannot read this file",
        ),
        (
            # A nonexistent name is a Lean error, like any other.
            "theorem good : True := True.intro\n#print axioms missing\n"
            "theorem after : True := True.intro\n",
            ["good"],
            2,
            "unknown constant",
        ),
        (
            # Lean would add the equation lemma it names to the module.
            "def f : Nat → Nat\n  | 0 => 0\n  | n + 1 => f n\n"
            "theorem good : f 0 = 0 := rfl\n#print axioms f.eq_1\n",
            ["good"],
            5,
            "generates on demand",
        ),
        (
            # Only `#print axioms` is accepted, no other `#print` form.
            "theorem good : True := True.intro\n#print good\n",
            ["good"],
            2,
            "unsupported reference command",
        ),
    ],
)
def test_live_project_check_withholds_from_first_failure(
    prepared_lean_project, text, kept, line, message
):
    """Claims before a failing command keep full checks; later ones are withheld."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(text)
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    assert [t["name"] for t in result["theorems"]] == kept
    assert all(t["status"] == "checked" for t in result["theorems"])
    assert result["withheld"]["from_line"] == line
    assert message in result["withheld"]["message"].lower()


def test_live_examples_are_checked_as_theorems(prepared_lean_project):
    """A Prop example is a claim with its replay status; others claim nothing."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "theorem good : True := True.intro\n"
        "example : True := trivial\n"
        "/-- A documented example. -/\n"
        "example (p : Prop) (h : p) : p := h\n"
        "example : 1 = 1 := sorry\n"
        "axiom custom : False\n"
        "example : False := custom\n"
        "example : Nat := 3\n"
        "example := (3 : Nat)\n"
        "namespace N\nsection\n"
        "variable (n : Nat) (m : Nat) [Inhabited Nat]\n"
        "example : n = n := by rfl\n"
        "end\nend N\n"
        "theorem after : True := trivial\n"
    )
    before = source.read_bytes()
    result = check_project(source, expected_sha256=hashlib.sha256(before).hexdigest())
    assert result["status"] == "checked_snapshot", result
    assert result["withheld"] is None
    found = [
        (t["example"], t["start_line"], t["end_line"], t["status"])
        for t in result["theorems"]
    ]
    assert found == [
        (False, 1, 1, "checked"),
        (True, 2, 2, "checked"),
        (True, 3, 4, "checked"),
        (True, 5, 5, "admitted"),
        (True, 7, 7, "assumptions"),
        (True, 13, 13, "checked"),
        (False, 16, 16, "checked"),
    ]
    assert result["non_claims"] == [
        {"start_line": 8, "end_line": 8},
        {"start_line": 9, "end_line": 9},
    ]
    examples = [t for t in result["theorems"] if t["example"]]
    assert all(
        t["name"].startswith("_private.Example.0.") and "_glitnir_example_" in t["name"]
        for t in examples
    )
    assert examples[3]["axioms"] == [{"name": "custom", "module": "Example"}]
    # Section variables are included as for a theorem: `n` (in the statement)
    # and the instance, not the unused `m`.
    sectioned = examples[4]
    assert ".N._glitnir_example_" in sectioned["name"]
    assert sectioned["type"].startswith("∀ (n : Nat)"), sectioned
    assert "Inhabited Nat" in sectioned["type"] and "m" not in sectioned["type"]
    assert source.read_bytes() == before


def test_live_example_statements_are_printed_in_their_scope(prepared_lean_project):
    """An example's statement reads as Lean prints it where it stands.

    The printed form uses the example's namespace, open declarations and
    notation; the kernel form beside it names every constant in full.
    """
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "namespace Notebook.Shared\ndef t : Nat := 2\nend Notebook.Shared\n"
        "namespace Notebook.Inner\nopen Notebook.Shared\n"
        "example : @t = @Notebook.Shared.t := rfl\n"
        "example : t + 1 = 3 := rfl\n"
        "theorem plain : t = 2 := rfl\n"
        "end Notebook.Inner\n"
    )
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    found = [(t["example"], t["statement"]) for t in result["theorems"]]
    assert found == [(True, "t = t"), (True, "t + 1 = 3"), (False, "t = 2")]
    kernel = result["theorems"][0]["type"]
    assert kernel.startswith("Eq ") and "Notebook.Shared.t" in kernel, kernel


def test_live_print_axioms_is_read_and_changes_no_status(prepared_lean_project):
    """Final `#print axioms` lines check fully; statuses come from the replay."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "theorem good : True := True.intro\n"
        "example : 1 = 1 := rfl\n"
        "theorem admitted : False := sorry\n"
        "axiom custom : False\n"
        "theorem assumed : False := custom\n"
        "private theorem hidden : True := True.intro\n"
        "#print axioms good\n"
        "#print axioms admitted\n"
        "#print axioms assumed\n"
        "#print axioms hidden\n"
        "#print axioms Nat.add_comm\n"
    )
    before = source.read_bytes()
    result = check_project(source, expected_sha256=hashlib.sha256(before).hexdigest())
    assert result["status"] == "checked_snapshot", result
    assert result["withheld"] is None
    found = [(t["start_line"], t["status"]) for t in result["theorems"]]
    assert found == [
        (1, "checked"),
        (2, "checked"),
        (3, "admitted"),
        (5, "assumptions"),
        (6, "checked"),
    ]
    named = {t["name"]: t for t in result["theorems"]}
    assert named["good"]["axioms"] == []
    assert named["assumed"]["axioms"] == [{"name": "custom", "module": "Example"}]
    assert source.read_bytes() == before


def test_live_skipped_kernel_check_cannot_fake_an_example(prepared_lean_project):
    """`debug.skipKernelTC` admits a false example to the module; replay refuses."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "import Lean\nset_option debug.skipKernelTC true\n"
        "example : False := by\n"
        "  run_tac Lean.Elab.Tactic.closeMainGoal `fixture "
        "(Lean.mkConst ``True.intro)\n"
    )
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "unavailable", result
    assert result["theorems"] == []
    assert "Independent kernel replay failed" in result["message"], result


def test_live_project_check_rejects_changed_definition(prepared_lean_project):
    """A proof cannot replace the meaning of a definition in its own proposition."""
    import hashlib

    from glitnir.lean.project_checking import check_project

    source = prepared_lean_project / "src/Example.lean"
    source.write_text(
        "import Lean\ndef two : Nat := 2\n"
        "theorem result : two = 3 := by\n"
        "  run_tac do\n"
        "    let env ← Lean.importModules #[{module := `Lean}] {}\n"
        "    let main ← Lean.getEnv\n"
        "    Lean.setEnv (env.setMainModule main.mainModule)\n"
        "    Lean.addDecl <| .defnDecl { name := `two, levelParams := [], "
        "type := Lean.mkConst `Nat, value := Lean.mkNatLit 3, "
        "hints := .abbrev, safety := .safe }\n"
        "    Lean.Elab.Tactic.closeMainGoal `fixture <| Lean.mkApp2 "
        "(Lean.mkConst ``Eq.refl [Lean.Level.succ Lean.Level.zero]) "
        "(Lean.mkConst ``Nat) (Lean.mkNatLit 3)\n"
    )
    result = check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "unavailable", result
    assert result["theorems"] == []
    assert "changed reference definition two" in result["message"], result


def test_stale_or_cancelled_request_never_captures(tmp_path, monkeypatch):
    """Reject invalid requests before preparing a costly environment snapshot."""
    import threading

    from glitnir.lean import project_checking as checking

    source = tmp_path / "A.lean"
    source.write_text("theorem t : True := True.intro\n")
    monkeypatch.setattr(checking, "capture", lambda *a: pytest.fail("captured"))
    result = checking.check_project(source, expected_sha256="0" * 64)
    assert result["status"] == "unavailable"
    assert "changed" in result["message"]
    cancel = threading.Event()
    cancel.set()
    assert (
        "cancelled"
        in checking.check_project(source, expected_sha256="0" * 64, cancel=cancel)[
            "message"
        ]
    )


def test_snapshot_identity_binds_copied_artifacts(tmp_path, project_snapshot_inputs):
    """Changing a compiled dependency changes identity despite unchanged source."""
    import threading

    from glitnir.lean.project_snapshot import capture

    root, toolchain, source, artifact, setup = project_snapshot_inputs
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    captured = capture(first, source, setup, toolchain, root, threading.Event())
    original = captured.artifacts.joinpath("Dependency.olean").read_bytes()
    artifact.write_bytes(b"dependency version two")
    again = capture(second, source, setup, toolchain, root, threading.Event())
    assert captured.identity != again.identity
    assert captured.source.read_bytes() == again.source.read_bytes()
    assert captured.artifacts.joinpath("Dependency.olean").read_bytes() == original
    assert str(captured.artifacts) in captured.setup["importArts"]["Dependency"][0][0]
    assert str(artifact) not in str(captured.setup)


def test_snapshot_refuses_toolchain_shadowing(tmp_path, project_snapshot_inputs):
    """Project artifacts cannot replace the modules used to compile fixed helpers."""
    import threading

    from glitnir.lean.project_snapshot import capture

    root, toolchain, source, artifact, setup = project_snapshot_inputs
    setup["importArts"] = {"Lean": [[str(artifact)]]}
    with pytest.raises(SnapshotError, match="shadows"):
        capture(tmp_path, source, setup, toolchain, root, threading.Event())


def test_running_project_stage_is_cancelled_and_reaped(tmp_path, monkeypatch):
    """Cancellation terminates a running child, not just future stages."""
    import subprocess
    import sys
    import threading
    import time

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_snapshot import ProjectSnapshot

    snapshot = ProjectSnapshot(tmp_path, tmp_path, tmp_path, tmp_path, {}, "id", 0, 0)
    original = subprocess.Popen
    children = []
    cancel = threading.Event()

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        cancel.set()
        return child

    def command(*args, **kwargs):
        assert kwargs["required"] is True
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    monkeypatch.setattr(checking.subprocess, "Popen", spawn)
    monkeypatch.setattr(checking, "confine", command)
    with pytest.raises(SnapshotError, match="cancelled"):
        checking._run(
            snapshot,
            ["lean"],
            cwd=tmp_path,
            readable=[],
            writable=False,
            cancel=cancel,
            deadline=time.monotonic() + 10,
        )
    assert children and children[0].poll() is not None


def test_running_out_of_memory_names_the_limit_and_the_setting(tmp_path, monkeypatch):
    """Lean's memory error becomes advice naming the limit and the setting."""
    import threading
    import time

    from glitnir.lean import project_checking as checking

    toolchain = tmp_path / "toolchain"
    (toolchain / "bin").mkdir(parents=True)
    lean = toolchain / "bin" / "lean"
    lean.write_text(
        "#!/bin/sh\n"
        "echo \"excessive memory consumption detected at 'interpreter' "
        '(potential solution: increase memory consumption threshold)" >&2\n'
        "exit 1\n"
    )
    lean.chmod(0o755)
    monkeypatch.setattr(checking, "confine", lambda command, **_options: command)
    with pytest.raises(SnapshotError) as raised:
        checking.run_observer(
            toolchain,
            ["lean", "-M6400", "A.lean"],
            cwd=tmp_path,
            readable=[],
            writable=False,
            cancel=threading.Event(),
            deadline=time.monotonic() + 30,
        )
    assert str(raised.value).startswith("Lean reached its memory limit of 6400 MB.")
    assert "glitnir.memoryLimitMb" in str(raised.value)


@pytest.mark.parametrize(
    "configured, expected", [("", 600), ("120000", 120), ("3600000", 1800)]
)
def test_check_timeout_uses_bounded_shared_setting(monkeypatch, configured, expected):
    """The editor's timeout setting bounds a saved-file check, capped at 30 minutes."""
    from glitnir.lean import limits

    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", configured)
    assert limits.check_seconds() == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_line_range_counts_utf8_bytes(newline):
    """Reader byte offsets map to the same 1-based lines for LF and CRLF files."""
    from glitnir.lean.project_checking import line_range

    raw = newline.join(["-- λ header", "theorem a : True := by", "  trivial", ""])
    text = raw.replace("\r\n", "\n")
    start = text.index("theorem a")
    end = text.index("trivial") + len("trivial")
    start_byte = len(text[:start].encode())
    end_byte = len(text[:end].encode())
    assert line_range(text, start_byte, end_byte) == (2, 3)


def test_first_error_offset_reads_lean_positions():
    """A positioned Lean error maps to a byte offset; other output does not."""
    from glitnir.lean.project_checking import first_error_offset

    text = "theorem a : True := True.intro\ntheorem \u03bbb : 1 = 2 := rfl\n"
    stderr = (
        "/tmp/x/Source.lean:2:22: error: type mismatch\n"
        "/tmp/x/Source.lean:2:0: warning: unused\n"
    )
    offset = first_error_offset(stderr, text)
    assert offset == len(
        "theorem a : True := True.intro\ntheorem \u03bbb : 1 = 2 := ".encode()
    )
    assert first_error_offset("lean: out of memory\n", text) is None


def test_check_reruns_once_on_the_prefix(tmp_path, monkeypatch):
    """A later failure withholds later claims; earlier claims are rechecked alone."""
    import hashlib

    from glitnir.lean import project_checking as checking

    source = tmp_path / "A.lean"
    source.write_text("theorem a : True := True.intro\ndef bad : Nat := False\n")
    texts = []

    def fake(source, text, **_):
        texts.append(text)
        if "bad" in text:
            raise checking.CommandFailure("type mismatch", offset=text.index("def bad"))
        return {"status": "checked_snapshot", "theorems": [{"name": "a"}]}

    monkeypatch.setattr(checking, "_check_text", fake)
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert texts == [source.read_text(), "theorem a : True := True.intro\n"]
    assert result["withheld"] == {"from_line": 2, "message": "type mismatch"}
    assert result["theorems"] == [{"name": "a"}]


def test_failure_in_the_prefix_is_unavailable(tmp_path, monkeypatch):
    """The prefix is checked once; a second failure makes the check unavailable."""
    import hashlib

    from glitnir.lean import project_checking as checking

    source = tmp_path / "A.lean"
    source.write_text("def x : Nat := 1\ndef bad : Nat := False\n")

    def fake(source, text, **_):
        raise checking.CommandFailure("still failing", offset=len(text) - 1)

    monkeypatch.setattr(checking, "_check_text", fake)
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "unavailable"
    assert result["theorems"] == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            "lean did not complete successfully (exit 4).\n"
            "unsupported example proof syntax",
            "unsupported example proof syntax",
        ),
        (
            "lean did not complete successfully (exit 3).\ntype mismatch\n  False\n"
            "has type\n  Prop : Type\nbut is expected to have type\n  Nat : Type",
            "type mismatch",
        ),
        (
            "lean did not complete successfully (exit 1).\n"
            "/tmp/x/Source.lean:2:23: error: The rfl tactic failed",
            "The rfl tactic failed",
        ),
        ("lean did not complete successfully (exit 1).\n", ""),
    ],
)
def test_failure_message_skips_the_exit_header(error, expected):
    """The displayed message is the Lean diagnostic, not the exit-status header."""
    from glitnir.lean.project_checking import failure_message

    assert failure_message(error) == expected
    assert len(failure_message("x" * 900)) == 500


def _reader_accepted_kinds() -> set[str]:
    """Command kinds `ProjectRead.lean` elaborates, read from its source."""
    import re

    from glitnir.lean.project_checking import PROGRAMS

    source = (PROGRAMS / "ProjectRead.lean").read_text()
    assert (
        source.count(
            "unless allowed declaration.getKind || printAxioms declaration.getKind do"
        )
        == 1
    )
    allowed = source.split("private def allowed (kind : Name) : Bool :=")[1]
    allowed = allowed.split("].contains kind")[0]
    inert = source.split("private def printAxioms (kind : Name) : Bool :=")[1]
    inert = inert.split("\n\n")[0]
    kinds = set(re.findall(r"``Lean\.Parser\.Command\.(\w+)", allowed))
    found = re.findall(r"``Lean\.Parser\.Command\.(\w+)", inert)
    assert found == ["printAxioms"] and "||" not in inert
    return kinds | set(found) | set(re.findall(r", `(\w+)$", allowed))


def test_reader_accepts_print_axioms_and_no_other_hash_command():
    """`#print axioms` is the one `#`-command the reader reads; others stay refused."""
    assert _reader_accepted_kinds() == {
        "declaration",
        "namespace",
        "section",
        "end",
        "open",
        "export",
        "variable",
        "set_option",
        "attribute",
        "moduleDoc",
        "lemma",
        "printAxioms",
    }


def test_reader_refuses_a_print_axioms_that_adds_a_declaration():
    """The reader compares constants after `#print axioms` and stops with exit 4."""
    from glitnir.lean.project_checking import PROGRAMS, failure_message

    source = (PROGRAMS / "ProjectRead.lean").read_text()
    guard = source.split("if printAxioms stx.getKind &&")[1].split("state := after")[0]
    assert "after.env.constants.map₂.toList.length != before.length" in guard
    assert "writeFailure report commandStart.byteIdx declarations" in guard
    assert "return 4" in guard
    error = (
        "lean did not complete successfully (exit 4).\n#print axioms would add a "
        "declaration Lean generates on demand (such as an equation lemma); print "
        "a declared constant's axioms"
    )
    assert failure_message(error).startswith("#print axioms would add a declaration")


def test_reader_failure_reads_the_written_offset(tmp_path):
    """A reader failure report yields a CommandFailure; bad reports do not."""
    from glitnir.lean.project_checking import CommandFailure, _reader_failure
    from glitnir.lean.project_snapshot import SnapshotError

    report = tmp_path / "source-report.json"
    error = SnapshotError("boom")
    text = "abc\ndef\n"
    assert _reader_failure(error, report, text) is None
    report.write_text('{"failed_at": 4, "declarations": []}')
    failure = _reader_failure(error, report, text)
    assert isinstance(failure, CommandFailure) and failure.offset == 4
    for body in (
        '{"failed_at": 99}',
        '{"failed_at": -1}',
        '{"failed_at": "4"}',
        '{"failed_at": true}',
        "[]",
        "not json",
    ):
        report.write_text(body)
        assert _reader_failure(error, report, text) is None


def test_candidate_failure_maps_to_the_owning_theorem():
    """A positioned compile error maps to its theorem's start, else is not mapped."""
    from glitnir.lean.project_checking import CommandFailure, _candidate_failure
    from glitnir.lean.project_snapshot import SnapshotError

    text = "theorem a : True := True.intro\ntheorem b : 1 = 2 := rfl\n"
    second = text.index("theorem b")
    entries = [
        {"name": "a", "start": 0, "end": second - 1},
        {"name": "b", "start": second, "end": len(text) - 1},
    ]
    inside = SnapshotError("lean failed\nS.lean:2:23: error: The rfl tactic failed")
    failure = _candidate_failure(inside, text, entries)
    assert isinstance(failure, CommandFailure) and failure.offset == second
    outside = SnapshotError("S.lean:1:0: error: unknown")
    assert _candidate_failure(outside, text, entries[1:]) is None
    assert _candidate_failure(SnapshotError("no position"), text, entries) is None


_EXAMPLES = (
    "theorem a : True := True.intro\n"
    "example : True := trivial\n"
    "/-- doc -/\nexample (p : Prop) (h : p) : p := h\n"
)


def _example_entries(text: str, tag: str = "_glitnir_example_") -> list[dict]:
    """Reader entries for `_EXAMPLES`, as `ProjectRead.lean` reports them."""
    data = text.encode()
    second = data.index(b"example")
    doc = data.index(b"/-- doc")
    third = data.index(b"example", doc)
    return [
        {"name": "a", "start": 0, "end": second - 1},
        {
            "name": f"_private.M.0.{tag}{second}",
            "start": second,
            "end": doc - 1,
            "keyword": [second, second + 7],
        },
        {
            "name": f"_private.M.0.{tag}{third}",
            "start": doc,
            "end": len(data) - 1,
            "keyword": [third, third + 7],
        },
    ]


def test_example_tag_never_occurs_in_the_source():
    """Generated theorem names cannot be written, declared or used by the source."""
    from glitnir.lean.project_checking import EXAMPLE_TAG, example_tag

    assert example_tag("example : True := trivial") == EXAMPLE_TAG
    text = f"def {EXAMPLE_TAG}1 : Nat := 1 -- _{EXAMPLE_TAG}\n"
    tag = example_tag(text)
    assert tag not in text and tag.endswith(EXAMPLE_TAG)


def _edits(text, entries, unclaimed=(), tag="_glitnir_example_"):
    """The edits `example_edits` makes of `text` with the reader's report."""
    from glitnir.lean.project_checking import example_edits

    edits, kept = example_edits(text, entries, list(unclaimed), tag)
    assert kept == list(unclaimed)
    return edits


def test_examples_are_rewritten_only_at_their_keywords():
    """The compiled text differs only where `example` became the named theorem."""
    from glitnir.lean.project_checking import check_rewrite, rewrite_examples

    data = _EXAMPLES.encode()
    entries = _example_entries(_EXAMPLES)
    edits = _edits(_EXAMPLES, entries)
    second, third = entries[1]["keyword"][0], entries[2]["keyword"][0]
    assert edits == [
        (second, f"private theorem _glitnir_example_{second}".encode()),
        (third, f"private theorem _glitnir_example_{third}".encode()),
    ]
    compiled = rewrite_examples(data, edits)
    assert compiled.decode() == (
        "theorem a : True := True.intro\n"
        f"private theorem _glitnir_example_{second} : True := trivial\n"
        "/-- doc -/\n"
        f"private theorem _glitnir_example_{third} (p : Prop) (h : p) : p := h\n"
    )
    check_rewrite(data, compiled, edits)
    pairs = zip(_EXAMPLES.split("\n"), compiled.decode().split("\n"), strict=True)
    for saved, built in pairs:
        if saved.startswith("example"):
            assert built.startswith("private theorem _glitnir_example_")
            assert built.endswith(saved.removeprefix("example"))
        else:
            assert built == saved
    assert rewrite_examples(data, []) == data
    check_rewrite(data, data, [])


@pytest.mark.parametrize(
    "compiled",
    [
        lambda c: c.replace(b"True.intro", b"True.Intro"),
        lambda c: c.replace(b"trivial", b"trivial\n"),
        lambda c: c.replace(b"private theorem", b"private  theorem", 1),
        lambda c: c + b"theorem b : True := trivial\n",
        lambda c: c[:-1],
    ],
)
def test_any_other_difference_in_the_compiled_text_fails(compiled):
    """The checker refuses compiled text that differs anywhere but the keywords."""
    from glitnir.lean.project_checking import check_rewrite, rewrite_examples
    from glitnir.lean.project_snapshot import SnapshotError

    data = _EXAMPLES.encode()
    edits = _edits(_EXAMPLES, _example_entries(_EXAMPLES))
    with pytest.raises(SnapshotError, match="differs from the saved source"):
        check_rewrite(data, compiled(rewrite_examples(data, edits)), edits)
    with pytest.raises(SnapshotError, match="differs from the saved source"):
        check_rewrite(data, rewrite_examples(data, edits), [(0, b"theorem")])
    with pytest.raises(SnapshotError, match="differs from the saved source"):
        check_rewrite(data, data.replace(b"example", b"exampl\n", 1), [(0, b"x\n")])


def _malformed(entries):
    """Ways a reader report could misdescribe the examples of `_EXAMPLES`."""
    second = entries[1]["keyword"][0]
    yield {**entries[1], "keyword": [second, second + 6]}
    yield {**entries[1], "keyword": [second + 1, second + 8]}
    yield {**entries[1], "keyword": [0, 7]}
    yield {**entries[1], "keyword": (second, second + 7)}
    yield {**entries[1], "keyword": [second, True]}
    yield {**entries[1], "end": second + 3}
    yield {**entries[1], "start": second + 1}
    yield {**entries[1], "start": 0}  # `theorem a …` is not a comment
    yield {**entries[1], "name": "_private.M.0._glitnir_example_0"}
    yield {**entries[1], "name": f"_glitnir_example_{second}.extra"}


@pytest.mark.parametrize("index", range(10))
def test_malformed_example_reports_are_refused(index):
    """Keyword spans must hold `example` inside their entry, under their name."""
    from glitnir.lean.project_snapshot import SnapshotError

    entries = _example_entries(_EXAMPLES)
    entries[1] = list(_malformed(entries))[index]
    with pytest.raises(SnapshotError, match="malformed|do not match"):
        _edits(_EXAMPLES, entries)


def _claim(text, keyword, start=None, tag="_glitnir_example_"):
    """A reader entry claiming the `example` at byte offset `keyword`."""
    end = text.encode().find(b"\n", keyword)
    return {
        "name": f"_private.M.0.{tag}{keyword}",
        "start": keyword if start is None else start,
        "end": end if end > 0 else len(text.encode()),
        "keyword": [keyword, keyword + 7],
    }


@pytest.mark.parametrize(
    "text",
    [
        "-- example : False := x\nexample : True := trivial\n",
        "/- example : False := x -/\nexample : True := trivial\n",
        "/-- example : False -/\nexample : True := trivial\n",
        'def s : String := "example : False"\nexample : True := trivial\n',
        "def «example» : Nat := 1\nexample : True := trivial\n",
    ],
)
def test_an_example_span_outside_code_is_refused(text):
    """A span on `example` text in a comment, string or name is not a keyword."""
    from glitnir.lean.project_snapshot import SnapshotError

    real = text.encode().rindex(b"example")
    fake = text.encode().index(b"example")
    assert _edits(text, [_claim(text, real)]) == [
        (real, f"private theorem _glitnir_example_{real}".encode())
    ]
    with pytest.raises(SnapshotError, match="do not match|malformed"):
        _edits(text, [_claim(text, fake), _claim(text, real)])
    with pytest.raises(SnapshotError, match="do not match|malformed"):
        _edits(text, [_claim(text, fake)])


def test_the_reader_must_account_for_every_example():
    """The reader can neither drop an example nor claim one twice.

    An unaccounted keyword withholds from the end of the last reported command
    before it, where the source can still be checked alone.
    """
    from glitnir.lean.project_checking import CommandFailure
    from glitnir.lean.project_snapshot import SnapshotError

    text = "example : True := trivial\nexample : Nat := 3\nexample := 3\n"
    first, second, third = (
        i for i in range(len(text)) if text.startswith("example", i)
    )
    plain = [
        {"start": second, "end": third - 1, "keyword": [second, second + 7]},
        {"start": third, "end": len(text) - 1, "keyword": [third, third + 7]},
    ]
    assert _edits(text, [_claim(text, first)], plain) == [
        (0, b"private theorem _glitnir_example_0")
    ]
    for dropped in ([], plain[:1], plain[1:]):
        missing = (plain[:1] if not dropped or dropped == plain[1:] else plain[1:])[0]
        line = text.count("\n", 0, missing["start"]) + 1
        with pytest.raises(CommandFailure, match=f"Line {line} has") as raised:
            _edits(text, [_claim(text, first)], dropped)
        # Withheld from after the last reported command before it.
        ends = [first] + [p["start"] for p in dropped if p["start"] < missing["start"]]
        assert raised.value.offset == text.index("\n", max(ends)) + 1
    with pytest.raises(SnapshotError, match="do not match"):
        _edits(text, [_claim(text, first)], plain + plain[:1])
    with pytest.raises(CommandFailure) as raised:
        _edits(text, [], plain)
    assert raised.value.offset == 0  # nothing before it: unavailable
    with pytest.raises(SnapshotError, match="malformed"):
        _edits(text, [_claim(text, first)], [{"keyword": [second, second + 7]}])


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("example : True := trivial\n", True),
        ("example:True := trivial\n", True),
        ("example (p : Prop) (h : p) : p := h\n", True),
        ("example {p : Prop} (h : p) : p := h\n", True),
        ("example [Inhabited Nat] : True := trivial\n", True),
        ("example ⦃p : Prop⦄ (h : p) : p := h\n", True),
        ("example\n  : True := trivial\n", True),
        ("-- note\n/- a -/ /-- doc -/\nexample : True := trivial\n", True),
        ("/-- one -/ /-- two -/\nexample : True := trivial\n", False),
        ("/-! module -/\nexample : True := trivial\n", False),
    ],
)
def test_a_claimed_keyword_starts_its_command(text, ok):
    """Only whitespace, comments and one docstring precede a claimed keyword."""
    from glitnir.lean.project_snapshot import SnapshotError

    keyword = text.encode().index(b"example")
    claim = _claim(text, keyword, start=0)
    if ok:
        assert _edits(text, [claim])[0][0] == keyword
    else:
        with pytest.raises(SnapshotError, match="malformed"):
            _edits(text, [claim])


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("@[simp] example : True := trivial\n", True),
        ("@[instance]\nexample : Inhabited Nat := ⟨0⟩\n", True),
        ("@[simp, aesop safe [constructors]] example : True := trivial\n", True),
        ("/-- doc -/ @[simp] example : True := trivial\n", True),
        ("private example : True := trivial\n", True),
        ("protected example : True := trivial\n", True),
        ("public example : True := trivial\n", True),
        ("meta example : True := trivial\n", True),
        ("noncomputable example : True := trivial\n", True),
        ("unsafe example : True := trivial\n", True),
        ("partial example : True := trivial\n", True),
        ("nonrec example : True := trivial\n", True),
        ("/-- doc -/\nexample : True := trivial\n", False),
        ("variable [Inhabited Nat]\nexample : True := trivial\n", False),
        ("def l : List Nat := [1, 2]\nexample : True := trivial\n", False),
        ("def a : Array Nat := #[1]\nexample : True := trivial\n", False),
        ("noncomputable section\nexample : True := trivial\n", False),
        ("def x := Foo.private\nexample : True := trivial\n", False),
    ],
)
def test_attributes_and_modifiers_are_refused_by_the_lexer(text, refused):
    """A reader that hides an attribute or modifier cannot get the example checked.

    The fake report starts the claim at its keyword, so only the trusted
    lexer sees what precedes it.
    """
    from glitnir.lean.project_checking import CommandFailure

    keyword = text.encode().index(b"example")
    claim = _claim(text, keyword)
    if refused:
        with pytest.raises(CommandFailure, match="attributes or modifiers") as raised:
            _edits(text, [claim])
        assert raised.value.offset == keyword
    else:
        assert _edits(text, [claim])[0][0] == keyword


@pytest.mark.parametrize("stray_first", [False, True])
def test_the_earliest_of_several_example_failures_wins(stray_first):
    """Stopping at the earliest reason leaves no other reason in the prefix."""
    from glitnir.lean.project_checking import CommandFailure

    attribute = "@[simp] example : True := trivial\n"
    stray = "def n : Lean.Name := `example\n"
    middle = stray + attribute if stray_first else attribute + stray
    text = "theorem a : True := trivial\n" + middle
    data = text.encode()
    keyword = data.index(b"example : True")
    theorem = {"name": "a", "start": 0, "end": text.index("\n")}
    claim = _claim(text, keyword)  # a lying reader starts it at the keyword
    with pytest.raises(CommandFailure) as raised:
        _edits(text, [theorem, claim])
    if stray_first:
        assert raised.value.offset == data.index(b"def n")
        assert "outside an example command" in str(raised.value)
    else:
        # Mid-line; `check_project` moves it back to the line's start.
        assert raised.value.offset == keyword
        assert "attributes or modifiers" in str(raised.value)


@pytest.mark.parametrize(
    ("cut", "kept", "line"),
    [
        ("theorem b", "theorem a : True := trivial\n", 2),
        ("True := sorry", "theorem a : True := trivial\n", 2),
        ("rivial", None, None),  # inside the first line: nothing is left
    ],
)
def test_a_lying_reader_cannot_cut_inside_a_line(
    tmp_path, monkeypatch, cut, kept, line
):
    """A reader's failed_at inside a line withholds that whole line.

    The saved results hash whole lines, so a prefix ending mid-line could show
    a declaration as checked although its tail was never compiled.
    """
    import hashlib
    import json

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_snapshot import SnapshotError

    text = "theorem a : True := trivial\ntheorem b : True := sorry\n"
    source = tmp_path / "A.lean"
    source.write_text(text)
    report = tmp_path / "source-report.json"
    report.write_text(
        json.dumps({"failed_at": text.encode().index(cut.encode()), "declarations": []})
    )
    texts = []

    def fake(source, checked, **_):
        texts.append(checked)
        if len(texts) == 1:
            failure = checking._reader_failure(SnapshotError("boom"), report, checked)
            assert failure is not None
            raise failure
        return {"status": "checked_snapshot", "theorems": []}

    monkeypatch.setattr(checking, "_check_text", fake)
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    if kept is None:
        assert result["status"] == "unavailable" and len(texts) == 1
    else:
        assert texts[1] == kept
        assert result["withheld"]["from_line"] == line


def test_an_example_keyword_outside_a_command_withholds_from_before_it():
    """A quoted `example` the reader cannot account for keeps earlier claims."""
    from glitnir.lean.project_checking import CommandFailure

    text = (
        "theorem a : True := trivial\n"
        "def n : Lean.Name := `example\n"
        "example : True := trivial\n"
    )
    keyword = text.encode().rindex(b"example")
    theorem = {"name": "a", "start": 0, "end": text.index("\n")}
    with pytest.raises(CommandFailure, match="Line 2 has the `example`") as raised:
        _edits(text, [theorem, _claim(text, keyword)])
    assert raised.value.offset == text.index("def n")


def test_an_unreadable_file_withholds_from_its_first_claimed_example():
    """Where the lexer cannot follow Lean, examples are refused, not guessed."""
    from glitnir.lean.project_checking import CommandFailure, example_edits

    text = 'def s : String := s!"{1}"\nexample : True := trivial\n'
    keyword = text.encode().index(b"example")
    with pytest.raises(CommandFailure, match="cannot read this file") as raised:
        _edits(text, [_claim(text, keyword)])
    assert raised.value.offset == keyword
    plain = {"start": keyword, "end": len(text) - 1, "keyword": [keyword, keyword + 7]}
    # Nothing is rewritten, and its non-claims are not recorded either: they
    # were never reconciled with the lexer.
    assert example_edits(text, [], [plain], "_glitnir_example_") == ([], [])


def test_examples_using_decl_name_are_refused():
    """`decl_name%` reads the theorem's name, so such examples are not checked."""
    from glitnir.lean.project_checking import CommandFailure

    text = (
        "theorem a : True := trivial\nexample : decl_name% ≠ `_example := by decide\n"
    )
    keyword = text.encode().index(b"example")
    with pytest.raises(CommandFailure, match="decl_name%") as raised:
        _edits(text, [_claim(text, keyword)])
    assert raised.value.offset == keyword


def test_compiled_positions_map_back_to_the_saved_source():
    """Offsets after a rewritten keyword shift back; inside it, to the keyword."""
    from glitnir.lean.project_checking import original_offset, rewrite_examples

    data = _EXAMPLES.encode()
    edits = _edits(_EXAMPLES, _example_entries(_EXAMPLES))
    compiled = rewrite_examples(data, edits)
    for marker in (b"theorem a", b"True := trivial", b"/-- doc", b"(p : Prop)", b"h\n"):
        assert original_offset(compiled.index(marker), edits) == data.index(marker)
    second = edits[0][0]
    for inside in range(second, second + len(edits[0][1])):
        assert original_offset(inside, edits) == second
    assert original_offset(len(compiled), edits) == len(data)


@pytest.mark.parametrize(
    ("message", "noted"),
    [("Unknown identifier `h`", True), ("The rfl tactic failed", False)],
)
def test_candidate_failure_in_an_example_maps_through_the_rewrite(message, noted):
    """A compile error past a rewritten keyword is the example's.

    The section-variable note is added only to an unknown identifier.
    """
    from glitnir.lean.project_checking import (
        EXAMPLE_NOTE,
        CommandFailure,
        _candidate_failure,
        rewrite_examples,
    )
    from glitnir.lean.project_snapshot import SnapshotError

    text = "example : True := trivial\nexample : 1 = 2 := rfl\n"
    second = text.index("example : 1")
    entries = [_claim(text, 0), _claim(text, second)]
    edits = _edits(text, entries)
    compiled = rewrite_examples(text.encode(), edits).decode()
    # The column is past the end of the saved line, inside the compiled one.
    column = compiled.split("\n")[1].index("rfl")
    assert column > len(text.split("\n")[1])
    error = SnapshotError(f"lean failed\nS.lean:2:{column}: error: {message}")
    failure = _candidate_failure(error, text, entries, compiled, edits)
    assert isinstance(failure, CommandFailure)
    assert failure.offset == second
    assert failure.note == (EXAMPLE_NOTE if noted else "")
    theorem = SnapshotError("S.lean:1:0: error: Unknown identifier `h`")
    plain = [{"name": "t", "start": 0, "end": second - 1}]
    assert _candidate_failure(theorem, text, plain).note == ""


@pytest.mark.parametrize("text", ["theorem a : True := trivial -- x\r\r\n", "a\rb\n"])
def test_stray_carriage_returns_are_refused(tmp_path, monkeypatch, text):
    """Lean would read a carriage return left after CRLF normalisation again."""
    import hashlib

    from glitnir.lean import project_checking as checking

    def capture(*_args, **_options):
        raise AssertionError("captured a file with a stray carriage return")

    monkeypatch.setattr(checking, "capture", capture)
    source = tmp_path / "A.lean"
    source.write_bytes(text.encode())
    normal = text.replace("\r\n", "\n")
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(normal.encode()).hexdigest()
    )
    assert result == {
        "status": "unavailable",
        "message": checking.CARRIAGE_RETURN,
        "theorems": [],
    }


def test_withheld_message_carries_the_example_note(tmp_path, monkeypatch):
    """A failing example says it was checked as a theorem."""
    import hashlib

    from glitnir.lean import project_checking as checking

    source = tmp_path / "A.lean"
    source.write_text("theorem a : True := True.intro\nexample : 1 = 2 := rfl\n")

    def fake(source, text, **_):
        if "example" in text:
            raise checking.CommandFailure(
                "S.lean:2:19: error: rfl failed",
                offset=text.index("example"),
                note=checking.EXAMPLE_NOTE,
            )
        return {"status": "checked_snapshot", "theorems": [{"name": "a"}]}

    monkeypatch.setattr(checking, "_check_text", fake)
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["withheld"] == {
        "from_line": 2,
        "message": f"rfl failed ({checking.EXAMPLE_NOTE})",
    }


def _staged_examples(tmp_path, monkeypatch, extra=(), statements=(), uses=None):
    """Check `_EXAMPLES` with stub stages; return the result and compiled texts.

    The candidate's report adds the theorems named in ``extra``; the reader
    reports the printed statements ``statements`` for its first entries, and
    the candidate's report the ``uses`` given by theorem name.
    """
    import hashlib
    import json

    from glitnir.lean import project_checking as checking

    calls = _fake_check_stages(tmp_path, monkeypatch)
    fake = checking.run_observer
    tag = "_glitnir_example_"
    entries = _example_entries(_EXAMPLES, tag)
    for entry, statement in zip(entries, statements):
        entry["statement"] = statement
    names = [entry["name"] for entry in entries]
    compiled: list[bytes] = []

    def run(toolchain, arguments, **options):
        program = Path(arguments[3]).name if "--run" in arguments else None
        if program == "ProjectRead.lean":
            assert arguments[7] == tag
            assert Path(arguments[4]).read_text() == _EXAMPLES
            fake(toolchain, arguments, **options)
            Path(arguments[-1]).write_text(json.dumps({"declarations": entries}))
            return ""
        if "-o" in arguments:
            compiled.append(Path(arguments[-1]).read_bytes())
            assert Path(arguments[-1]).parent in [Path(p) for p in options["readable"]]
        if program == "ProjectReport.lean":
            report = json.loads(fake(toolchain, arguments, **options))
            report["module"] = "GlitnirStandalone"
            found = names + (list(extra) if "candidate" in arguments[5] else [])
            report["theorems"] = [
                {"name": n, "type": "True", "canonical": n, "axioms": []} for n in found
            ]
            for theorem in report["theorems"]:
                if uses and theorem["name"] in uses and "candidate" in arguments[5]:
                    theorem["uses"] = uses[theorem["name"]]
            return json.dumps(report)
        if program == "ProjectReplay.lean":
            replay = json.loads(fake(toolchain, arguments, **options))
            replay["theorems"] = [{"name": n, "axioms": []} for n in names]
            return json.dumps(replay)
        return fake(toolchain, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", run)
    source = tmp_path / "A.lean"
    source.write_text(_EXAMPLES)
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(_EXAMPLES.encode()).hexdigest()
    )
    return result, compiled, entries, calls


def test_a_check_compiles_examples_as_named_theorems(tmp_path, monkeypatch):
    """The reader names each example; only the compiled copy is rewritten.

    The reader receives the tag; the candidate compiles a separate file that
    differs from the frozen source only at the keywords; each example is then
    bound, replayed and reported like a theorem, marked as an example.
    """
    result, compiled, entries, calls = _staged_examples(tmp_path, monkeypatch)
    assert result["status"] == "checked_snapshot", result
    tag = "_glitnir_example_"
    second, third = entries[1]["keyword"][0], entries[2]["keyword"][0]
    assert compiled == [
        _EXAMPLES.replace("example : True", f"private theorem {tag}{second} : True")
        .replace("example (p", f"private theorem {tag}{third} (p")
        .encode()
    ]
    assert [
        (t["name"], t["example"], t["start_line"], t["end_line"], t["status"])
        for t in result["theorems"]
    ] == [
        ("a", False, 1, 1, "checked"),
        (entries[1]["name"], True, 2, 2, "checked"),
        (entries[2]["name"], True, 3, 4, "checked"),
    ]
    assert result["non_claims"] == []
    assert len(calls) == 5  # reader, candidate, two reporters, replay


def test_statements_are_printed_by_the_reader_beside_the_kernel_form(
    tmp_path, monkeypatch
):
    """The reader's printed statement is shown; the reporter's type is kept.

    Anything but a string from the (untrusted) reader is left out.
    """
    result, _, _, _ = _staged_examples(tmp_path, monkeypatch, statements=["True", 3])
    assert result["status"] == "checked_snapshot", result
    assert [(t["statement"], t["type"]) for t in result["theorems"]] == [
        ("True", "True"),
        (None, "True"),
        (None, "True"),
    ]


def test_a_check_reports_the_claims_each_claim_uses(tmp_path, monkeypatch):
    """Uses come from the candidate's report: this notebook's claims only.

    A loose file has no other notebook modules, so a use of an imported
    theorem, or of a theorem of this module that is not a claim, is dropped.
    """
    entries = _example_entries(_EXAMPLES)
    first, second = entries[0]["name"], entries[1]["name"]
    reported = [
        {"name": first, "module": "GlitnirStandalone"},
        {"name": first, "module": "GlitnirStandalone"},
        {"name": "helper.eq_1", "module": "GlitnirStandalone"},
        {"name": "trivial", "module": "Init.Core"},
    ]
    result, _, _, _ = _staged_examples(tmp_path, monkeypatch, uses={second: reported})
    assert result["status"] == "checked_snapshot", result
    assert [t["uses"] for t in result["theorems"]] == [
        [],
        [{"module": "GlitnirStandalone", "name": first, "in_notebook": True}],
        [],
    ]


@pytest.mark.parametrize(
    "uses", [{"name": "a"}, [{"name": "a"}], [{"name": 1, "module": "M"}], ["a"]]
)
def test_a_malformed_uses_report_is_refused(tmp_path, monkeypatch, uses):
    """The module report is fixed code: malformed uses mean a broken check."""
    second = _example_entries(_EXAMPLES)[1]["name"]
    result, _, _, _ = _staged_examples(tmp_path, monkeypatch, uses={second: uses})
    assert result["status"] == "unavailable", result
    assert "report is malformed" in result["message"]


def test_project_modules_are_the_ones_the_project_builds(tmp_path):
    """Modules compiled in the project are notebooks; packages' are not."""
    from glitnir.lean.project_checking import project_modules

    root = tmp_path / "project"
    build = root / ".lake/build/lib/lean"
    package = root / ".lake/packages/mathlib/.lake/build/lib/lean"
    for directory in (build, package):
        directory.mkdir(parents=True)
    (build / "Sibling.olean").write_bytes(b"")
    (package / "Mathlib.olean").write_bytes(b"")
    outside = tmp_path / "toolchain/lib/lean/Init.olean"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"")
    setup = {
        "importArts": {
            "Sibling": [[".lake/build/lib/lean/Sibling.olean"]],
            "Absolute": [[str(build / "Sibling.olean")]],
            "Mathlib": [[".lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean"]],
            "Init": [[str(outside)]],
        }
    }
    assert project_modules(setup, root.resolve()) == {"Sibling", "Absolute"}
    assert project_modules(setup, None) == set()


def test_claim_uses_keep_claims_of_this_and_other_notebooks():
    """Of the theorems named, only claims here and other notebooks' count."""
    from glitnir.lean.project_checking import claim_uses

    reported = [
        {"name": "A.one", "module": "A"},
        {"name": "A.one.eq_1", "module": "A"},
        {"name": "B.two", "module": "B"},
        {"name": "_private.B.0.hidden", "module": "B"},
        {"name": "Nat.add_zero", "module": "Init.Core"},
        {"name": "Mathlib.thing", "module": "Mathlib"},
    ]
    assert claim_uses(reported, "A", {"A.one", "A.three"}, {"B"}) == [
        {"module": "A", "name": "A.one", "in_notebook": True},
        {"module": "B", "name": "B.two", "in_notebook": False},
    ]


def test_a_candidate_cannot_declare_a_reserved_name(tmp_path, monkeypatch):
    """Only the claimed examples may carry a name reserved for examples."""
    result, _, _, _ = _staged_examples(
        tmp_path, monkeypatch, extra=["_private.M.0._glitnir_example_99"]
    )
    assert result["status"] == "unavailable", result
    assert "reserved for examples" in result["message"]


def test_a_check_without_examples_compiles_the_frozen_source(tmp_path, monkeypatch):
    """Files without examples compile exactly the frozen snapshot copy."""
    from glitnir.lean import project_checking as checking

    calls = _fake_check_stages(tmp_path, monkeypatch)
    source = tmp_path / "A.lean"
    source.write_text("def x : Nat := 1\n")
    import hashlib

    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    compile_call = next(c for c in calls if "-o" in c)
    assert Path(compile_call[-1]).parent.name == "snapshot"
    reader = next(c for c in calls if "--run" in c)
    assert reader[7] == checking.EXAMPLE_TAG


def test_live_loose_file_checks_and_records_proof_steps(tmp_path, project_toolchain):
    """A file outside any Lake project checks and traces against the toolchain."""
    import hashlib

    from glitnir.lean.project import describe_project
    from glitnir.lean.project_checking import check_project
    from glitnir.lean.proof_trace import trace_file

    source = tmp_path / "Loose.lean"
    source.write_text(
        "theorem t : True := True.intro\ntheorem u : True := by exact True.intro\n"
    )
    assert describe_project(source)["kind"] == "loose"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    checked = check_project(source, expected_sha256=digest)
    assert checked["status"] == "checked_snapshot", checked
    assert checked["module"] == "GlitnirStandalone"
    assert [(t["name"], t["status"]) for t in checked["theorems"]] == [
        ("t", "checked"),
        ("u", "checked"),
    ]
    assert checked["withheld"] is None
    traced = trace_file(source, expected_sha256=digest)
    assert traced["status"] == "ready", traced
    assert traced["errors"] == []
    assert [(n["declaration"], n["text"]) for n in traced["nodes"]] == [
        ("u", "exact True.intro")
    ]
    assert traced["nodes"][0]["before"] == ["⊢ True"]


def _fake_check_stages(tmp_path, monkeypatch) -> list[list[str]]:
    """Run a loose-file check with stub snapshot and Lean stages; record arguments."""
    import json

    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_snapshot import ProjectSnapshot

    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    monkeypatch.setattr(checking, "_toolchain", lambda: toolchain)

    def capture(root, source, setup, toolchain, project_root, cancel, **options):
        copied = root / "snapshot" / "Source.lean"
        copied.parent.mkdir()
        copied.write_text(options["text"])
        return ProjectSnapshot(
            root=root / "snapshot",
            toolchain=toolchain,
            artifacts=root / "snapshot",
            source=copied,
            setup=setup,
            identity="0" * 64,
            bytes=0,
            files=1,
        )

    monkeypatch.setattr(checking, "capture", capture)
    calls: list[list[str]] = []

    def run(_toolchain, arguments, **_options):
        calls.append(arguments)
        program = Path(arguments[3]).name if "--run" in arguments else None
        if program == "ProjectRead.lean":
            Path(arguments[-2]).write_bytes(b"olean")
            Path(arguments[-1]).write_text(json.dumps({"declarations": []}))
        if "-o" in arguments:
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"olean")
        if program == "ProjectReport.lean":
            return json.dumps(
                {
                    "module": "M",
                    "imports": [],
                    "constants": [],
                    "theorems": [],
                    "modules": [["GlitnirStandalone", "GlitnirStandalone.olean"]],
                }
            )
        if program == "ProjectReplay.lean":
            return json.dumps(
                {
                    "replay": "full",
                    "imports_replayed": True,
                    "extendable": True,
                    "base_modules": None,
                    "fresh": None,
                    "imports": [],
                    "modules": [],
                    "theorems": [],
                }
            )
        return ""

    monkeypatch.setattr(checking, "run_observer", run)
    return calls


def test_a_check_reports_how_long_each_stage_took(tmp_path, monkeypatch):
    """`stage_ms` names every stage a loose file's check runs, in milliseconds."""
    import hashlib

    from glitnir.lean import project_checking as checking

    _fake_check_stages(tmp_path, monkeypatch)
    source = tmp_path / "A.lean"
    source.write_text("def x : Nat := 1\n")
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert result["status"] == "checked_snapshot", result
    stage_ms = result["stage_ms"]
    assert list(stage_ms) == [
        "capture",
        "read",
        "compile",
        "report_reference",
        "report_candidate",
        "reports",
        "replay",
        "total",
    ]
    assert all(type(ms) is int and ms >= 0 for ms in stage_ms.values())
    assert stage_ms["total"] >= stage_ms["reports"] + stage_ms["replay"]


def test_a_check_that_times_out_still_reports_its_stages(tmp_path, monkeypatch):
    """An unavailable result says how far the check got and how long it took."""
    import hashlib

    from glitnir.lean import project_checking as checking

    _fake_check_stages(tmp_path, monkeypatch)
    fake = checking.run_observer

    def run(toolchain, arguments, **options):
        if "-o" in arguments:
            raise SnapshotError("Project check timed out.")
        return fake(toolchain, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", run)
    source = tmp_path / "A.lean"
    source.write_text("def x : Nat := 1\n")
    result = checking.check_project(
        source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert (result["status"], result["message"]) == (
        "unavailable",
        "Project check timed out.",
    )
    assert list(result["stage_ms"]) == ["capture", "read", "compile", "total"]


def test_stage_times_add_up_a_repeated_stage_and_survive_a_failure(monkeypatch):
    from glitnir.lean import project_checking as checking

    clock = iter([0.0, 1.0, 1.5, 2.0, 4.0, 10.0])
    monkeypatch.setattr(checking.time, "monotonic", lambda: next(clock))
    stages = checking.StageTimes()
    with stages.stage("read"):
        pass
    with pytest.raises(ValueError):
        with stages.stage("read"):
            raise ValueError("stopped")
    assert stages.summary() == {"read": 2500, "total": 10000}
    assert checking.stage_summary({"read": 2500, "total": 10000}) == (
        "read 2.5 s, total 10.0 s"
    )


class _Slept:
    """A sleep watch whose note a test sets."""

    note_text = ""

    def note(self) -> str:
        return self.note_text


def test_a_check_is_kept_awake_from_capture_to_its_last_lean_run(
    tmp_path, monkeypatch, awake_depth
):
    """The assertion covers the whole check, not only each Lean process.

    The capture between Lean runs is held too, and a progress message after
    the computer slept says how long it slept.
    """
    import hashlib

    from glitnir.lean import awake as awake_module
    from glitnir.lean import project_checking as checking

    _fake_check_stages(tmp_path, monkeypatch)
    depth = awake_depth(checking)
    monkeypatch.setattr(awake_module, "SleepWatch", _Slept)
    seen: list[tuple[str, int]] = []
    capture, run = checking.capture, checking.run_observer

    def held_capture(*args, **options):
        seen.append(("capture", depth[0]))
        _Slept.note_text = " (the computer slept for 12 min)"
        return capture(*args, **options)

    def held_run(*args, **options):
        seen.append(("lean", depth[0]))
        return run(*args, **options)

    monkeypatch.setattr(checking, "capture", held_capture)
    monkeypatch.setattr(checking, "run_observer", held_run)
    monkeypatch.setattr(_Slept, "note_text", "")
    source = tmp_path / "A.lean"
    source.write_text("def x : Nat := 1\n")
    messages: list[str] = []
    result = checking.check_project(
        source,
        expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        progress=messages.append,
    )
    assert result["status"] == "checked_snapshot", result
    assert seen == [("capture", 1)] + [("lean", 1)] * 5
    assert depth == [0]
    assert messages[0] == "Preparing isolated environment"
    assert len(messages) > 1
    assert all(m.endswith(" (the computer slept for 12 min)") for m in messages[1:])


@pytest.mark.parametrize(
    "configured, expected", [(None, "-M16384"), ("20480", "-M20480")]
)
def test_every_check_stage_uses_the_configured_memory_limit(
    tmp_path, monkeypatch, capsys, configured, expected
):
    """The command-line check passes `GLITNIR_MEMORY_LIMIT_MB` to every Lean run."""
    import json

    from glitnir.lean import limits
    from glitnir.service.cli import main

    source = tmp_path / "A.lean"
    source.write_text("def x : Nat := 1\n")
    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 65536)
    if configured is None:
        monkeypatch.delenv("GLITNIR_MEMORY_LIMIT_MB", raising=False)
    else:
        monkeypatch.setenv("GLITNIR_MEMORY_LIMIT_MB", configured)
    calls = _fake_check_stages(tmp_path, monkeypatch)
    assert main(["check", str(source)]) == 0, capsys.readouterr()
    assert json.loads(capsys.readouterr().out)["status"] == "checked_snapshot"
    assert len(calls) == 5  # reader, candidate, two reporters, replay
    for arguments in calls:
        assert arguments[0] == "lean"
        assert [a for a in arguments if a.startswith("-M")] == [expected]


def test_snapshot_capture_shares_the_check_time_limit(
    tmp_path, project_snapshot_inputs, monkeypatch
):
    """Copying inputs gets the configured check limit, not a fixed five minutes."""
    import threading

    from glitnir.lean import project_snapshot

    root, toolchain, source, _artifact, setup = project_snapshot_inputs
    clock = iter([1000.0] + [1000.0 + 301.0] * 1000)
    monkeypatch.setattr(project_snapshot.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(project_snapshot, "check_seconds", lambda: 600.0)
    destination = tmp_path / "copy"
    destination.mkdir()
    captured = project_snapshot.capture(
        destination, source, setup, toolchain, root, threading.Event()
    )
    assert captured.artifacts.joinpath("Dependency.olean").exists()

    monkeypatch.setattr(project_snapshot, "check_seconds", lambda: 300.0)
    clock = iter([2000.0] + [2000.0 + 301.0] * 1000)
    monkeypatch.setattr(project_snapshot.time, "monotonic", lambda: next(clock))
    late = tmp_path / "late"
    late.mkdir()
    with pytest.raises(project_snapshot.SnapshotError, match="timed out"):
        project_snapshot.capture(
            late, source, setup, toolchain, root, threading.Event()
        )


_TWO = "theorem a : True := by\n  trivial\ntheorem b : True := trivial\n"


def _partial(tmp_path, monkeypatch, text, through_line, fail_at=None):
    """Run `check_project` through ``through_line`` over a stub `_check_text`.

    The stub records each text and rest it was asked to check; with
    ``fail_at``, the first check fails at that offset of its text.
    """
    import hashlib

    from glitnir.lean import project_checking as checking

    source = tmp_path / "A.lean"
    source.write_text(text)
    asked: list[tuple[str, str | None]] = []

    def fake(source, checked, *, rest=None, **_):
        asked.append((checked, rest))
        if fail_at is not None and len(asked) == 1:
            raise checking.CommandFailure("S.lean:3:0: error: bad", offset=fail_at)
        return {
            "status": "checked_snapshot",
            "source_sha256": hashlib.sha256(checked.encode()).hexdigest(),
            "theorems": [],
        }

    monkeypatch.setattr(checking, "_check_text", fake)
    result = checking.check_project(
        source,
        expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
        through_line=through_line,
    )
    return result, asked


def test_a_check_through_a_line_checks_only_that_prefix(tmp_path, monkeypatch):
    """Lines 1..N are checked as a prefix; the rest is passed only to parse."""
    import hashlib

    result, asked = _partial(tmp_path, monkeypatch, _TWO, 2)
    assert asked == [
        ("theorem a : True := by\n  trivial\n", "theorem b : True := trivial\n")
    ]
    assert result["withheld"] == {
        "from_line": 3,
        "message": "Checked through line 2 on request",
        "through_line": 2,
    }
    assert result["source_sha256"] == hashlib.sha256(_TWO.encode()).hexdigest()


@pytest.mark.parametrize("line", [3, 4, 99])
def test_a_check_through_the_last_line_is_a_whole_check(tmp_path, monkeypatch, line):
    """Nothing is left after the cut: it is an ordinary check of the file."""
    result, asked = _partial(tmp_path, monkeypatch, _TWO, line)
    assert asked == [(_TWO, None)]
    assert result["withheld"] is None


def test_a_failure_inside_the_prefix_withholds_from_the_failing_command(
    tmp_path, monkeypatch
):
    """The failing command's line is withheld; the request is remembered."""
    text = _TWO + "theorem c : True := trivial\n"
    result, asked = _partial(
        tmp_path, monkeypatch, text, 3, fail_at=text.index("theorem b") + 3
    )
    assert asked == [
        (_TWO, "theorem c : True := trivial\n"),
        (
            "theorem a : True := by\n  trivial\n",
            text[len("theorem a : True := by\n  trivial\n") :],
        ),
    ]
    assert result["withheld"] == {"from_line": 3, "message": "bad", "through_line": 3}


@pytest.mark.parametrize("line", [0, -1, "2", True, 1.5])
def test_a_check_through_a_bad_line_is_refused(tmp_path, monkeypatch, line):
    result, asked = _partial(tmp_path, monkeypatch, _TWO, line)
    assert result["status"] == "unavailable" and not asked


def test_a_check_through_a_line_runs_the_whole_pipeline_on_the_prefix(
    tmp_path, monkeypatch
):
    """Reader, compile, both reports and the kernel replay all run on the prefix.

    The reader alone also receives the saved text after the cut, to parse.
    The kernel replay runs as in any check, so it uses and feeds the replay
    cache the same way.
    """
    import hashlib

    from glitnir.lean import project_checking as checking

    calls = _fake_check_stages(tmp_path, monkeypatch)
    fake = checking.run_observer
    seen = {}

    def run(toolchain, arguments, **options):
        if "--run" in arguments and Path(arguments[3]).name == "ProjectRead.lean":
            seen["source"] = Path(arguments[4]).read_text()
            seen["rest"] = Path(arguments[10]).read_text()
            assert Path(arguments[10]).parent in [Path(p) for p in options["readable"]]
            return fake(toolchain, arguments[:10], **options)
        if "-o" in arguments:
            seen["compiled"] = Path(arguments[-1]).read_text()
        return fake(toolchain, arguments, **options)

    monkeypatch.setattr(checking, "run_observer", run)
    source = tmp_path / "A.lean"
    source.write_text(_TWO)
    result = checking.check_project(
        source,
        expected_sha256=hashlib.sha256(_TWO.encode()).hexdigest(),
        through_line=2,
    )
    assert result["status"] == "checked_snapshot", result
    prefix = "theorem a : True := by\n  trivial\n"
    assert seen == {
        "source": prefix,
        "rest": "theorem b : True := trivial\n",
        "compiled": prefix,
    }
    programs = [Path(c[3]).name for c in calls if "--run" in c]
    assert programs.count("ProjectReplay.lean") >= 1
    assert result["withheld"]["through_line"] == 2


_PROOFS = (
    "theorem a : True := trivial\n"
    "theorem b : True := by\n  trivial\n  <;> trivial\n"
    "theorem c : True := trivial\n"
)


def test_a_check_through_a_line_inside_a_proof_backs_off_to_a_command_end(
    tmp_path, monkeypatch
):
    """Line 3 is inside `b`'s proof: lines 1 to 1 are checked, and said so."""
    result, asked = _partial(tmp_path, monkeypatch, _PROOFS, 3)
    assert asked == [("theorem a : True := trivial\n", _PROOFS[28:])]
    assert result["withheld"] == {
        "from_line": 2,
        "message": "Checked through line 1 on request (line 3 is inside a command)",
        "through_line": 1,
    }


def test_a_check_through_a_line_of_the_first_command_is_refused(tmp_path, monkeypatch):
    text = (
        "theorem a : True := by\n  trivial\n  <;> trivial\n"
        "theorem b : True := trivial\n"
    )
    result, asked = _partial(tmp_path, monkeypatch, text, 2)
    assert result["status"] == "unavailable" and not asked
    assert "first command" in result["message"]


def test_a_failure_offset_inside_a_proof_never_cuts_the_proof(tmp_path, monkeypatch):
    """A reader report naming a line inside a proof backs off to a command end.

    The failure is reported where it was found; claims from the cut on are
    withheld.
    """
    text = _PROOFS
    result, asked = _partial(
        tmp_path, monkeypatch, text, None, fail_at=text.index("  <;>")
    )
    assert [checked for checked, _ in asked][1] == "theorem a : True := trivial\n"
    assert result["withheld"] == {"from_line": 2, "message": "line 4: bad"}


def test_a_failure_with_no_command_end_before_it_is_unavailable(tmp_path, monkeypatch):
    text = "theorem a : True := by\n  trivial\n  <;> trivial\n"
    result, asked = _partial(
        tmp_path, monkeypatch, text, None, fail_at=text.index("  <;>")
    )
    assert result["status"] == "unavailable" and len(asked) == 1


def test_a_forged_claim_below_the_checked_lines_fails_the_check(tmp_path, monkeypatch):
    """A prefix check cannot report a claim ending after its last line."""
    import hashlib

    from glitnir.lean import project_checking as checking

    source = tmp_path / "A.lean"
    source.write_text(_PROOFS)

    def fake(source, checked, **_):
        return {
            "status": "checked_snapshot",
            "source_sha256": "s",
            "theorems": [{"name": "b", "start_line": 2, "end_line": 4}],
        }

    monkeypatch.setattr(checking, "_check_text", fake)
    result = checking.check_project(
        source,
        expected_sha256=hashlib.sha256(_PROOFS.encode()).hexdigest(),
        through_line=1,
    )
    assert result["status"] == "unavailable"
    assert "ending after line 1" in result["message"]


def test_uses_beyond_the_cap_are_marked_truncated(tmp_path, monkeypatch):
    """A claim keeps at most MAX_USES uses, and says when there were more."""
    from glitnir.lean import project_checking as checking

    many = [
        {"module": "M", "name": f"x{i}", "in_notebook": True}
        for i in range(checking.MAX_USES + 1)
    ]
    monkeypatch.setattr(checking, "claim_uses", lambda *a: many)
    result, _, _, _ = _staged_examples(tmp_path, monkeypatch)
    assert all(len(t["uses"]) == checking.MAX_USES for t in result["theorems"])
    assert all(t["uses_truncated"] is True for t in result["theorems"])
