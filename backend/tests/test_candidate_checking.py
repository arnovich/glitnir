"""Checking a decision candidate: the saved notebook prefix plus a provider block."""

import hashlib
import threading
from pathlib import Path

import pytest

from glitnir.lean import project_checking as checking
from glitnir.lean.project_snapshot import SLOT_BUSY, SnapshotError

NOTEBOOK = "theorem before : True := True.intro\n-- @decide t : goal\nlater\n"
BLOCK = "namespace Decided_t\ntheorem t : True := True.intro\nend Decided_t"


def _theorem(name, status, line, axioms=()):
    return {
        "name": name,
        "type": "True",
        "axioms": list(axioms),
        "status": status,
        "start_line": line,
        "end_line": line,
    }


def _saved(tmp_path: Path, text: str = NOTEBOOK) -> tuple[Path, str]:
    source = tmp_path / "A.lean"
    source.write_text(text)
    return source, hashlib.sha256(text.encode()).hexdigest()


def _check(source, sha256, *, block=BLOCK, theorem="t", cancel=None, line=2):
    return checking.check_candidate(
        source,
        expected_sha256=sha256,
        insert_after_line=line,
        block=block,
        theorem=theorem,
        cancel=cancel or threading.Event(),
    )


def _fake(monkeypatch, theorems=None, error=None):
    """Replace the Lean check; record the checked text and its bound digest."""
    calls = []

    def fake(source, text, *, saved_sha256, cancel, progress, stages):
        calls.append((source, text, saved_sha256))
        if error is not None:
            raise error(text) if callable(error) else error
        return {"status": "checked_snapshot", "theorems": theorems or []}

    monkeypatch.setattr(checking, "_check_text", fake)
    return calls


def test_candidate_text_is_the_prefix_then_the_block(tmp_path, monkeypatch):
    """The checked text keeps saved lines through the directive, then the block."""
    source, sha256 = _saved(tmp_path)
    calls = _fake(monkeypatch, [_theorem("Decided_t.t", "checked", 4)])
    assert _check(source, sha256)["status"] == "checked"
    assert calls == [
        (
            source.absolute(),
            "theorem before : True := True.intro\n-- @decide t : goal\n" + BLOCK + "\n",
            sha256,
        )
    ]
    assert source.read_text() == NOTEBOOK


def test_directive_outside_the_notebook_is_refused(tmp_path, monkeypatch):
    source, sha256 = _saved(tmp_path)
    _fake(monkeypatch)
    for line in (0, 5):
        with pytest.raises(SnapshotError, match="outside the notebook"):
            _check(source, sha256, line=line)


@pytest.mark.parametrize(
    ("status", "axioms", "verdict", "message"),
    [
        (
            "checked",
            [{"name": "propext", "module": "Init.Core"}],
            "checked",
            "The checker reports `Decided_t.t` as checked.",
        ),
        (
            "admitted",
            [{"name": "sorryAx", "module": "Init.Prelude"}],
            "rejected",
            "`Decided_t.t` is admitted: its proof depends on `sorry`.",
        ),
        (
            "assumptions",
            [
                {"name": "propext", "module": "Init.Core"},
                {"name": "custom", "module": "A"},
            ],
            "rejected",
            "`Decided_t.t` depends on axioms outside Lean's standard set: `custom`.",
        ),
    ],
)
def test_only_a_checked_theorem_is_accepted(
    tmp_path, monkeypatch, status, axioms, verdict, message
):
    """`admitted` and `assumptions` reject; the matched theorem's axiom names stay."""
    source, sha256 = _saved(tmp_path)
    _fake(monkeypatch, [_theorem("Decided_t.t", status, 4, axioms)])
    assert _check(source, sha256) == {
        "status": verdict,
        "message": message,
        "axioms": [axiom["name"] for axiom in axioms],
        "type": "True",
    }


def test_standard_axioms_from_other_modules_are_named_with_their_module(
    tmp_path, monkeypatch
):
    """A standard axiom name the toolchain does not provide is not called extra."""
    source, sha256 = _saved(tmp_path)
    axioms = [
        {"name": "propext", "module": "A"},
        {"name": "Quot.sound", "module": None},
    ]
    _fake(monkeypatch, [_theorem("t", "assumptions", 3, axioms)])
    assert _check(source, sha256) == {
        "status": "rejected",
        "message": "`t` depends on axioms the checker could not trace to Lean's "
        "own modules: `propext` (from `A`), `Quot.sound` (from `an unknown module`).",
        "axioms": ["propext", "Quot.sound"],
        "type": "True",
    }


def test_axioms_are_returned_as_names_in_order_without_duplicates(
    tmp_path, monkeypatch
):
    """The saved decision holds axiom names; the checker's records carry modules."""
    source, sha256 = _saved(tmp_path)
    axioms = [
        {"name": "propext", "module": "Init.Core"},
        {"name": "Classical.choice", "module": "Init.Core"},
        {"name": "propext", "module": "Init.Core"},
    ]
    _fake(monkeypatch, [_theorem("Decided_t.t", "checked", 4, axioms)])
    result = _check(source, sha256)
    assert result["status"] == "checked"
    assert result["axioms"] == ["propext", "Classical.choice"]


def test_directive_on_the_empty_last_line_keeps_the_whole_file(tmp_path, monkeypatch):
    """After a final LF the split has an empty last line, which may be addressed."""
    source, sha256 = _saved(tmp_path)
    calls = _fake(monkeypatch, [_theorem("t", "checked", 5)])
    result = _check(source, sha256, line=4, block="theorem t : True := trivial")
    assert result["status"] == "checked"
    assert calls[0][1] == NOTEBOOK + "\ntheorem t : True := trivial\n"


def test_theorem_matches_only_inside_the_block(tmp_path, monkeypatch):
    """A plain name matches; declarations before the block never count."""
    source, sha256 = _saved(tmp_path)
    _fake(
        monkeypatch,
        [_theorem("before", "admitted", 1), _theorem("t", "checked", 3)],
    )
    result = _check(source, sha256, block="theorem t : True := True.intro")
    assert result["status"] == "checked"
    _fake(monkeypatch, [_theorem("t", "checked", 1), _theorem("other", "checked", 4)])
    assert _check(source, sha256) == {
        "status": "rejected",
        "message": "The block does not declare `t`.",
        "axioms": [],
        "type": None,
    }
    _fake(monkeypatch, [_theorem("Decided_t.tt", "checked", 4)])
    assert _check(source, sha256)["message"] == "The block does not declare `t`."


def test_a_bare_theorem_matches_its_namespaced_declaration(tmp_path, monkeypatch):
    source, sha256 = _saved(tmp_path)
    _fake(monkeypatch, [_theorem("Decided_t.t", "checked", 4)])
    assert _check(source, sha256, theorem="t")["status"] == "checked"


def test_a_qualified_theorem_matches_exactly_that_name(tmp_path, monkeypatch):
    """`Decided_t.t` names its namespace: `Other.t` is not it, `X.Decided_t.t` is."""
    source, sha256 = _saved(tmp_path)
    _fake(monkeypatch, [_theorem("Decided_t.t", "checked", 4)])
    result = _check(source, sha256, theorem="Decided_t.t")
    assert (result["status"], result["message"]) == (
        "checked",
        "The checker reports `Decided_t.t` as checked.",
    )
    _fake(monkeypatch, [_theorem("Other.t", "checked", 4)])
    assert _check(source, sha256, theorem="Decided_t.t") == {
        "status": "rejected",
        "message": "The block does not declare `Decided_t.t`.",
        "axioms": [],
        "type": None,
    }
    _fake(monkeypatch, [_theorem("XDecided_t.t", "checked", 4)])
    assert _check(source, sha256, theorem="Decided_t.t")["status"] == "rejected"
    _fake(monkeypatch, [_theorem("X.Decided_t.t", "checked", 4)])
    assert _check(source, sha256, theorem="Decided_t.t")["status"] == "checked"


def test_a_theorem_declared_twice_is_rejected(tmp_path, monkeypatch):
    source, sha256 = _saved(tmp_path)
    _fake(
        monkeypatch,
        [_theorem("A.t", "checked", 3), _theorem("B.t", "checked", 4)],
    )
    assert _check(source, sha256, block="a\nb\nc") == {
        "status": "rejected",
        "message": "The block declares `t` more than once.",
        "axioms": [],
        "type": None,
    }


def test_block_failures_are_reported_at_block_lines(tmp_path, monkeypatch):
    """A compile error names its block line; a reader failure its command's line."""
    source, sha256 = _saved(tmp_path)
    _fake(
        monkeypatch,
        error=lambda text: checking.CommandFailure(
            "lean did not complete successfully (exit 1).\n"
            "/tmp/s/A.lean:4:23: error: The rfl tactic failed\nmore detail",
            offset=text.index("theorem t"),
        ),
    )
    assert _check(source, sha256) == {
        "status": "rejected",
        "message": "Block line 2: The rfl tactic failed",
        "axioms": [],
        "type": None,
    }
    _fake(
        monkeypatch,
        error=lambda text: checking.CommandFailure(
            "lean did not complete successfully (exit 3).\nunknown identifier 'x'",
            offset=text.index("end Decided_t"),
        ),
    )
    assert _check(source, sha256)["message"] == "Block line 3: unknown identifier 'x'"
    _fake(
        monkeypatch,
        error=checking.CommandFailure(
            "lean did not complete successfully (exit 3).\n", offset=0
        ),
    )
    assert _check(source, sha256)["message"] == (
        "Line 1: The checker stopped at this command."
    )


@pytest.mark.parametrize(
    "message",
    [SLOT_BUSY, "Project check cancelled.", "Project check timed out."],
)
def test_interruptions_are_raised_not_rejected(tmp_path, monkeypatch, message):
    """A busy slot or stopped check says nothing about the block."""
    source, sha256 = _saved(tmp_path)
    _fake(monkeypatch, error=SnapshotError(message))
    with pytest.raises(SnapshotError) as raised:
        _check(source, sha256)
    assert str(raised.value) == message


def test_cancelled_or_changed_saved_file_never_yields_a_verdict(tmp_path, monkeypatch):
    source, sha256 = _saved(tmp_path)
    calls = _fake(monkeypatch, [_theorem("t", "checked", 3)])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SnapshotError, match="cancelled"):
        _check(source, sha256, cancel=cancel)
    with pytest.raises(SnapshotError, match="Saved source changed"):
        _check(source, "0" * 64)
    assert calls == []

    def edit_during_check(source, text, **_):
        source.write_text(NOTEBOOK + "edited\n")
        return {"status": "checked_snapshot", "theorems": [_theorem("t", "checked", 3)]}

    monkeypatch.setattr(checking, "_check_text", edit_during_check)
    with pytest.raises(SnapshotError, match="Saved source changed"):
        _check(source, sha256)


def test_environment_errors_are_raised_as_snapshot_errors(tmp_path, monkeypatch):
    source, sha256 = _saved(tmp_path)
    _fake(monkeypatch, error=checking.SandboxUnavailable("no sandbox here"))
    with pytest.raises(SnapshotError, match="no sandbox here"):
        _check(source, sha256)


LIVE_NOTEBOOK = "import Example.Helper\n-- @decide t : two plus two\n"


@pytest.mark.parametrize("fails", [False, True])
def test_stage_times_are_filled_but_never_part_of_the_verdict(
    tmp_path, monkeypatch, fails
):
    """A decision saves the verdict, so the times go to the caller's dict only."""
    source, sha256 = _saved(tmp_path)

    def fake(source, text, *, stages, **_):
        with stages.stage("read"):
            pass
        if fails:
            raise checking.CommandFailure("bad", offset=text.index("theorem t"))
        return {"status": "checked_snapshot", "theorems": [_theorem("t", "checked", 4)]}

    monkeypatch.setattr(checking, "_check_text", fake)
    stage_ms: dict[str, int] = {}
    verdict = checking.check_candidate(
        source,
        expected_sha256=sha256,
        insert_after_line=2,
        block=BLOCK,
        theorem="t",
        cancel=threading.Event(),
        stage_ms=stage_ms,
    )
    assert verdict["status"] == ("rejected" if fails else "checked"), verdict
    assert set(verdict) == {"status", "message", "axioms", "type"}
    assert set(stage_ms) == {"read", "total"}


@pytest.mark.parametrize(
    ("block", "status", "message"),
    [
        ("theorem t : 2 + 2 = 4 := by decide", "checked", "as checked"),
        ("theorem t : False := by sorry", "rejected", "depends on `sorry`"),
        (
            "theorem other : 2 + 2 = 4 := by decide",
            "rejected",
            "does not declare `t`",
        ),
        (
            "axiom extra : 2 + 2 = 4\ntheorem t : 2 + 2 = 4 := extra",
            "rejected",
            "`extra`",
        ),
        (
            "namespace Decided_t\ntheorem t : 2 + 2 = 5 := by decide\nend Decided_t",
            "rejected",
            "Block line 2:",
        ),
    ],
)
def test_live_candidate_check(prepared_lean_project, block, status, message):
    """The project checker alone decides whether a provider's block is acceptable."""
    source = prepared_lean_project / "src/Example.lean"
    source.write_text(LIVE_NOTEBOOK)
    result = checking.check_candidate(
        source,
        expected_sha256=hashlib.sha256(LIVE_NOTEBOOK.encode()).hexdigest(),
        insert_after_line=2,
        block=block,
        theorem="t",
        cancel=threading.Event(),
    )
    assert result["status"] == status, result
    assert message in result["message"], result
    if status == "checked":
        # The checker reports the kernel form, which names the instances used.
        assert result["type"] == "Eq (instHAdd.hAdd 2 2) 4", result
    assert source.read_text() == LIVE_NOTEBOOK
