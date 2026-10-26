"""Fully replayed imports are recorded per user and reused only for identical files."""

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from glitnir.lean import replay_cache
from glitnir.lean.project_inspection import TOOLCHAIN
from glitnir.lean.replay_cache import cache_directory as real_cache_directory

KEY = "a" * 64
CONTEXT = "c" * 64
DIGEST = {name: hashlib.sha256(name.encode()).hexdigest() for name in "ABCDEFG"}
MODULES = [["A", DIGEST["A"]], ["B", DIGEST["B"]]]


def _record(directory, key=KEY, modules=None, extendable=True, context=CONTEXT):
    replay_cache.record(
        directory,
        key,
        context=context,
        modules=MODULES if modules is None else modules,
        extendable=extendable,
    )


def _contains(directory, key=KEY, modules=None, context=CONTEXT):
    return replay_cache.contains(
        directory, key, context=context, modules=MODULES if modules is None else modules
    )


def _key(**changes):
    toolchain = changes.pop("toolchain", {"toolchain/bin/lean": "1" * 64})
    program = changes.pop("program_sha256", "2" * 64)
    arguments = {
        "context": replay_cache.context_digest(
            toolchain=toolchain, program_sha256=program
        ),
        "imports": [{"module": "Dep", "all": False, "exported": True, "meta": False}],
        "modules": [
            ["Init", replay_cache.module_digest({".olean": "3" * 64, ".ir": "absent"})],
            ["Dep", replay_cache.module_digest({".olean": "4" * 64, ".ir": "absent"})],
        ],
    }
    arguments.update(changes)
    return replay_cache.import_key(**arguments)


def _digest(**files):
    return replay_cache.module_digest({".olean": "3" * 64, ".ir": "absent", **files})


def test_key_binds_toolchain_program_imports_and_every_module_file():
    """Any changed digest, module, import or toolchain file gives another key."""
    base = _key()
    assert len(base) == 64 and base == _key()
    init = ["Init", _digest()]
    others = [
        _key(toolchain={"toolchain/bin/lean": "9" * 64}),
        _key(toolchain={"toolchain/bin/lean": "1" * 64, "toolchain/lib/x": "0"}),
        _key(program_sha256="9" * 64),
        _key(imports=[]),
        _key(modules=[init]),
        _key(modules=[init, ["Dep", _digest(**{".olean": "5" * 64})]]),
        _key(modules=[init, ["Dep", _digest(**{".olean": "4" * 64, ".ir": "6"})]]),
        _key(modules=[["Dep", _digest(**{".olean": "4" * 64})], init]),
    ]
    assert base not in others and len(set(others)) == len(others)
    assert _digest() != _digest(**{".olean.private": "7" * 64})


def test_recorded_entry_is_private_and_found(tmp_path):
    """A record creates a 0700 directory and one 0600 file matching its key."""
    directory = tmp_path / "glitnir" / "replay"
    assert not _contains(directory)
    _record(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    entry = directory / (KEY + ".json")
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    value = json.loads(entry.read_text())
    assert value["key"] == KEY and value["toolchain"] == TOOLCHAIN
    assert value["modules"] == MODULES and value["extendable"] is True
    assert _contains(directory)
    assert not _contains(directory, modules=MODULES[:1])
    assert not _contains(directory, modules=MODULES[::-1])
    assert not _contains(directory, context="d" * 64)
    assert not _contains(directory, key="b" * 64)
    assert not _contains(directory, key="../" + KEY)
    assert sorted(p.name for p in directory.iterdir()) == [KEY + ".json"]


def _entry(**changes):
    value = {
        "format": replay_cache.FORMAT,
        "key": KEY,
        "toolchain": TOOLCHAIN,
        "context": CONTEXT,
        "modules": MODULES,
        "extendable": True,
        "created": 1.0,
    }
    value.update(changes)
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[" * 3000,
        json.dumps({"key": KEY}).encode(),
        _entry(format="glitnir.replay-cache/v1"),
        _entry(key="b" * 64),
        _entry(toolchain="leanprover/lean4:v0.0.0"),
        _entry(context="d" * 64),
        _entry(modules=2),
        _entry(modules=[["A", "not a digest"], ["B", DIGEST["B"]]]),
        _entry(modules=[["A", DIGEST["A"]], ["A", DIGEST["A"]]]),
        _entry(extendable=1),
        _entry(created="yesterday"),
        b" " * (replay_cache.ENTRY_BYTES + 1),
    ],
)
def test_malformed_or_mismatched_entries_are_misses(tmp_path, content):
    """Anything but this key's well-formed success record is a miss."""
    directory = tmp_path / "glitnir" / "replay"
    _record(directory)
    entry = directory / (KEY + ".json")
    entry.write_bytes(content)
    assert not _contains(directory)
    bigger = [*MODULES, ["C", DIGEST["C"]]]
    assert replay_cache.find_base(directory, context=CONTEXT, modules=bigger) is None


def test_a_parent_others_can_write_is_refused(tmp_path):
    """The `glitnir` directory above the cache must not be group/world-writable."""
    directory = tmp_path / "glitnir" / "replay"
    _record(directory)
    directory.parent.chmod(0o777)
    assert not _contains(directory)
    _record(directory, key="e" * 64)
    assert not (directory / ("e" * 64 + ".json")).exists()
    directory.parent.chmod(0o755)
    assert _contains(directory)


def test_entries_others_can_access_or_links_are_misses(tmp_path):
    """Group/world access, a linked entry or a linked directory is never trusted."""
    directory = tmp_path / "glitnir" / "replay"
    bigger = [*MODULES, ["C", DIGEST["C"]]]
    _record(directory)
    entry = directory / (KEY + ".json")
    entry.chmod(0o644)
    assert not _contains(directory)
    assert replay_cache.find_base(directory, context=CONTEXT, modules=bigger) is None
    entry.chmod(0o600)
    directory.chmod(0o755)
    assert not _contains(directory)
    directory.chmod(0o700)
    assert _contains(directory)

    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(entry.read_bytes())
    elsewhere.chmod(0o600)
    entry.unlink()
    entry.symlink_to(elsewhere)
    assert not _contains(directory)
    assert replay_cache.find_base(directory, context=CONTEXT, modules=bigger) is None

    linked = tmp_path / "linked" / "glitnir" / "replay"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(directory, target_is_directory=True)
    entry.unlink()
    _record(directory)
    assert _contains(directory)
    assert not _contains(linked)
    assert replay_cache.find_base(linked, context=CONTEXT, modules=bigger) is None
    _record(linked, key="c" * 64)
    assert not (directory / ("c" * 64 + ".json")).exists()


def test_record_replaces_a_planted_link_without_writing_through_it(tmp_path):
    """Recording swaps the name atomically; a link's target is never written."""
    directory = tmp_path / "glitnir" / "replay"
    _record(directory, key="d" * 64)
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    (directory / (KEY + ".json")).symlink_to(victim)
    _record(directory)
    assert victim.read_text() == "unchanged"
    assert not (directory / (KEY + ".json")).is_symlink()
    assert _contains(directory)


def test_unusable_directory_records_nothing(tmp_path):
    """A cache path that is a file or someone else's tree is skipped, not used."""
    blocked = tmp_path / "glitnir"
    blocked.write_text("a file")
    _record(blocked / "replay")
    assert blocked.read_text() == "a file"
    assert not _contains(blocked / "replay")


def test_prune_keeps_the_most_recently_used_entries(tmp_path, monkeypatch):
    """Entries beyond the bound are removed oldest first; a hit refreshes one."""
    monkeypatch.setattr(replay_cache, "MAX_ENTRIES", 3)
    directory = tmp_path / "glitnir" / "replay"
    keys = [str(n) * 64 for n in range(1, 6)]
    for age, key in enumerate(keys[:3]):
        _record(directory, key=key)
        os.utime(directory / (key + ".json"), (1000 + age, 1000 + age))
    assert _contains(directory, key=keys[0])  # now newest
    _record(directory, key=keys[3])
    _record(directory, key=keys[4])
    assert sorted(p.name for p in directory.iterdir()) == sorted(
        k + ".json" for k in (keys[0], keys[3], keys[4])
    )


def test_searching_for_a_base_refreshes_only_the_chosen_one(tmp_path, monkeypatch):
    """A miss reads every entry but keeps only the base it uses from eviction."""
    monkeypatch.setattr(replay_cache, "MAX_ENTRIES", 4)
    directory = tmp_path / "glitnir" / "replay"
    keys = [str(n) * 64 for n in range(5)]
    _record(directory, key=keys[0], modules=_closure("A"))
    for key in keys[1:4]:
        _record(directory, key=key, modules=_closure("A", "F"))
    for age, key in enumerate(keys[:4]):
        os.utime(directory / (key + ".json"), (1000 + age, 1000 + age))
    current = _closure("A", "B")
    assert replay_cache.find_base(directory, context=CONTEXT, modules=current) == ["A"]
    assert not _contains(directory, key=keys[1])  # a mismatch refreshes nothing
    _record(directory, key=keys[4], modules=current)
    assert sorted(p.name for p in directory.iterdir()) == sorted(
        k + ".json" for k in (keys[0], keys[2], keys[3], keys[4])
    )


def test_prune_bounds_the_bytes_entries_take(tmp_path, monkeypatch):
    """Oldest entries go until the rest fit; the newest stays even if larger."""
    directory = tmp_path / "glitnir" / "replay"
    _record(directory, key="1" * 64)
    size = (directory / ("1" * 64 + ".json")).stat().st_size
    monkeypatch.setattr(replay_cache, "MAX_TOTAL_BYTES", 2 * size + size // 2)
    os.utime(directory / ("1" * 64 + ".json"), (1000, 1000))
    _record(directory, key="2" * 64)
    os.utime(directory / ("2" * 64 + ".json"), (1001, 1001))
    _record(directory, key="3" * 64)
    assert sorted(p.name for p in directory.iterdir()) == [
        "2" * 64 + ".json",
        "3" * 64 + ".json",
    ]
    monkeypatch.setattr(replay_cache, "MAX_TOTAL_BYTES", 1)
    _record(directory, key="4" * 64)
    assert [p.name for p in directory.iterdir()] == ["4" * 64 + ".json"]


def _closure(*names, **changed):
    return [[n, changed.get(n, DIGEST[n])] for n in names]


def test_find_base_picks_the_largest_recorded_subset_in_its_order(tmp_path):
    """Only extendable records of the same context with identical modules count."""
    directory = tmp_path / "glitnir" / "replay"
    current = _closure("A", "B", "C", "D", "E")
    assert replay_cache.find_base(directory, context=CONTEXT, modules=current) is None
    _record(directory, key="1" * 64, modules=_closure("A"))
    _record(directory, key="2" * 64, modules=_closure("B", "A", "C"))
    _record(directory, key="3" * 64, modules=_closure("A", "B", "C", "D", "F"))
    _record(
        directory, key="4" * 64, modules=_closure("A", "B", "C", "D"), extendable=False
    )
    _record(
        directory, key="5" * 64, modules=_closure("A", "B", "C", "E"), context="d" * 64
    )
    _record(directory, key="6" * 64, modules=_closure("A", "B", "D", "E", D="0" * 64))
    found = replay_cache.find_base(directory, context=CONTEXT, modules=current)
    assert found == ["B", "A", "C"]  # the recorded load order
    # The same module set under other roots is a base with nothing new.
    _record(directory, key="7" * 64, modules=_closure("E", "D", "C", "B", "A"))
    found = replay_cache.find_base(directory, context=CONTEXT, modules=current)
    assert found == ["E", "D", "C", "B", "A"]
    # One changed digest disqualifies a record.
    changed = _closure("A", "B", "C", "D", "E", A="0" * 64)
    assert replay_cache.find_base(directory, context=CONTEXT, modules=changed) is None


def test_cache_directory_follows_platform_conventions(tmp_path, monkeypatch):
    """On macOS Library/Caches; elsewhere an absolute XDG_CACHE_HOME or ~/.cache."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(replay_cache.sys, "platform", "darwin")
    assert real_cache_directory() == tmp_path / "Library/Caches/glitnir/replay"
    monkeypatch.setattr(replay_cache.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert real_cache_directory() == tmp_path / "xdg/glitnir/replay"
    monkeypatch.setenv("XDG_CACHE_HOME", "relative")
    assert real_cache_directory() == tmp_path / ".cache/glitnir/replay"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert real_cache_directory() == tmp_path / ".cache/glitnir/replay"


# ----- the checker's use of the cache ------------------------------------------


def _staged_check(tmp_path, monkeypatch, replay, reported=(), names=("Init", "Dep")):
    """Run a loose-file check with stub snapshot and stages over real file digests.

    ``replay(arguments, closure)`` stands in for the replay program's output.
    ``reported`` are the theorems (name and axioms) the module report lists,
    each declared over the whole source. ``names`` are the imported modules,
    a list a test may change between checks (``_staged_check.names``).
    Returns the recorded calls and the cache directory.
    """
    names = list(names)
    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_snapshot import ProjectSnapshot

    toolchain = tmp_path / "toolchain"
    toolchain.mkdir(exist_ok=True)
    monkeypatch.setattr(checking, "_toolchain", lambda: toolchain)
    cache = tmp_path / "cache" / "glitnir" / "replay"
    monkeypatch.setattr(replay_cache, "cache_directory", lambda: cache)

    def capture(root, source, setup, toolchain, project_root, cancel, **options):
        inputs = root / "inputs"
        files = {
            "toolchain/bin/lean": b"lean",
            "toolchain/lib/lean/Init.olean": b"init",
            "toolchain/lib/lean/Init.ir": b"init ir",
            "artifacts/Dep.olean": b"dependency",
            "artifacts/Extra.olean": b"extra",
        }
        store = options["store"]
        for relative, data in files.items():
            (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
            if relative == "toolchain/bin/lean":
                (inputs / relative).write_bytes(data)
                continue
            digest = hashlib.sha256(data).hexdigest()
            namespace = "toolchain"
            if not store.contains(namespace, digest, len(data)):
                store.add(namespace, digest, [data])
            (inputs / relative).symlink_to(store.path(namespace, digest))
        copied = inputs / "Source.lean"
        copied.write_text(options["text"])
        return ProjectSnapshot(
            root=inputs,
            toolchain=inputs / "toolchain",
            artifacts=inputs / "artifacts",
            source=copied,
            setup=setup,
            identity="0" * 64,
            bytes=0,
            files=len(files),
            digests={
                relative: hashlib.sha256(data).hexdigest()
                for relative, data in files.items()
            },
            stores={
                "toolchain": store.namespace("toolchain"),
                "artifacts": store.namespace("toolchain"),
            },
        )

    monkeypatch.setattr(checking, "capture", capture)
    calls: list[list[str]] = []
    deadlines: list[float] = []

    def run(_toolchain, arguments, **options):
        calls.append(arguments)
        deadlines.append(options["deadline"])
        program = Path(arguments[3]).name if "--run" in arguments else None
        if program == "ProjectRead.lean":
            size = Path(arguments[4]).stat().st_size
            declarations = [
                {"name": theorem["name"], "start": 0, "end": size}
                for theorem in reported
            ]
            Path(arguments[-2]).write_bytes(b"olean")
            Path(arguments[-1]).write_text(json.dumps({"declarations": declarations}))
        if "-o" in arguments:
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"olean")
        isolated = Path(arguments[5]) if program else None
        closure = (
            [
                [
                    name,
                    (
                        str(isolated.parent / "inputs/toolchain/lib/lean/Init.olean")
                        if name == "Init"
                        else str(isolated / f"{name}.olean")
                    ),
                ]
                for name in names
            ]
            if isolated
            else []
        )
        if program == "ProjectReport.lean":
            return json.dumps(
                {
                    "module": "GlitnirStandalone",
                    "imports": [{"module": "Dep"}],
                    "constants": [],
                    "theorems": [
                        {**theorem, "type": "T", "canonical": theorem["name"]}
                        for theorem in reported
                    ],
                    "modules": [
                        *closure,
                        ["GlitnirStandalone", str(isolated / "GlitnirStandalone")],
                    ],
                }
            )
        if program == "ProjectReplay.lean":
            return replay(arguments, closure)
        return ""

    monkeypatch.setattr(checking, "run_observer", run)
    _staged_check.deadlines = deadlines  # type: ignore[attr-defined]
    _staged_check.names = names  # type: ignore[attr-defined]
    return calls, cache


def _check(tmp_path):
    from glitnir.lean.project_checking import check_project

    source = tmp_path / "A.lean"
    source.write_text("def x : Nat := 1\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return check_project(source, expected_sha256=digest)


def _mode(arguments):
    return (
        "imports-incremental" if "imports-incremental" in arguments else arguments[-1]
    )


def _replayed(
    mode_reported=None,
    imports_replayed=None,
    fresh=None,
    theorems=(),
    extendable=True,
    bases=None,
):
    """Stand in for a successful replay that reports what it ran.

    ``theorems`` are the module's theorems with the axioms the replay collected.
    ``bases``, when a list, collects the base each incremental replay was given.
    """
    theorems = list(theorems)

    def replay(arguments, closure):
        mode = _mode(arguments)
        base = None
        if mode == "imports-incremental":
            base = json.loads(Path(arguments[-1]).read_text())
            if bases is not None:
                bases.append(base)
        ran = (
            mode_reported
            or {
                "full": "full",
                "imports-only": "imports only",
                "imports-incremental": "imports incremental",
                "imports-cached": "full" if fresh else "imports cached",
            }[mode]
        )
        importing = mode in ("imports-only", "imports-incremental")
        return json.dumps(
            {
                "replay": ran,
                "imports_replayed": (
                    imports_replayed if imports_replayed is not None else importing
                ),
                "extendable": extendable,
                "base_modules": (
                    len(base)
                    if ran == "imports incremental" and base is not None
                    else None
                ),
                "fresh": fresh,
                "imports": [{"module": "Dep"}],
                "modules": closure,
                "theorems": [] if importing else theorems,
            }
        )

    return replay


def _modes(calls):
    return [
        _mode(c) for c in calls if "--run" in c and c[3].endswith("ProjectReplay.lean")
    ]


def test_a_miss_replays_imports_once_and_records_then_a_hit_skips_them(
    tmp_path, monkeypatch
):
    """The first check replays the imports alone, records, then checks cached."""
    calls, cache = _staged_check(tmp_path, monkeypatch, _replayed())
    first = _check(tmp_path)
    assert first["status"] == "checked_snapshot", first
    assert first["replay"] == "full"
    assert "reused" not in first["message"]
    assert len(list(cache.iterdir())) == 1
    second = _check(tmp_path)
    assert second["status"] == "checked_snapshot", second
    assert second["replay"] == "imports cached"
    assert "reused" in second["message"]
    assert _modes(calls) == ["imports-only", "imports-cached", "imports-cached"]
    assert (first["theorems"], first["environment_sha256"]) == (
        second["theorems"],
        second["environment_sha256"],
    )


def test_the_imports_replay_has_its_own_deadline(tmp_path, monkeypatch):
    """The one-time import replay gets an hour; the check's budget starts after."""
    import time

    from glitnir.lean import project_checking as checking
    from glitnir.lean.limits import check_seconds

    monkeypatch.setenv("GLITNIR_ELABORATION_TIMEOUT_MS", "600000")
    slow = _replayed()

    def replay(arguments, closure):
        if arguments[-1] == "imports-only":
            time.sleep(0.2)
        return slow(arguments, closure)

    calls, _ = _staged_check(tmp_path, monkeypatch, replay)
    started = time.monotonic()
    assert _check(tmp_path)["status"] == "checked_snapshot"
    finished = time.monotonic()
    deadlines = dict(zip(_modes(calls), _staged_check.deadlines[-2:]))
    budget = check_seconds()
    assert (
        started + checking.IMPORTS_REPLAY_SECONDS
        <= deadlines["imports-only"]
        <= finished + checking.IMPORTS_REPLAY_SECONDS
    )
    # The 0.2 s the import replay took is added back to the check's deadline.
    assert deadlines["imports-cached"] >= started + budget + 0.2
    assert deadlines["imports-cached"] <= finished + budget
    # The reader, the compile and both reports ran under one deadline, which
    # the one-time import replay did not move; only the module replay's did.
    earlier = _staged_check.deadlines[:-2]
    assert len(earlier) == 4 and len(set(earlier)) == 1
    assert deadlines["imports-cached"] >= earlier[0] + 0.2


@pytest.mark.parametrize("failure", ["Project check timed out.", "cancelled"])
def test_a_failed_imports_replay_fails_the_check_and_records_nothing(
    tmp_path, monkeypatch, failure
):
    """A timed out or cancelled import replay is reported; no full replay follows."""
    from glitnir.lean.project_snapshot import SnapshotError

    def replay(arguments, closure):
        if arguments[-1] == "imports-only":
            raise SnapshotError(failure)
        return _replayed()(arguments, closure)

    calls, cache = _staged_check(tmp_path, monkeypatch, replay)
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert result["message"].startswith(
        "Independent kernel replay failed while replaying the imported modules once"
    )
    assert failure in result["message"]
    assert _modes(calls) == ["imports-only"]
    assert not cache.exists() or not list(cache.iterdir())


def test_imports_are_recorded_even_if_the_module_then_fails(tmp_path, monkeypatch):
    """The imports' record does not depend on the notebook's own constants."""
    from glitnir.lean.project_snapshot import SnapshotError

    def replay(arguments, closure):
        if arguments[-1] == "imports-cached":
            raise SnapshotError("lean did not complete successfully (exit 1).")
        return _replayed()(arguments, closure)

    calls, cache = _staged_check(tmp_path, monkeypatch, replay)
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "Independent kernel replay failed" in result["message"]
    assert len(list(cache.iterdir())) == 1
    _check(tmp_path)
    assert _modes(calls) == ["imports-only", "imports-cached", "imports-cached"]


def test_imports_that_do_not_rebuild_equally_are_not_recorded(tmp_path, monkeypatch):
    """Without an equal rebuild, nothing is recorded and the check replays in full."""
    calls, cache = _staged_check(
        tmp_path, monkeypatch, _replayed(imports_replayed=False)
    )
    assert _check(tmp_path)["replay"] == "full"
    assert _check(tmp_path)["replay"] == "full"
    assert _modes(calls) == ["imports-only", "full", "imports-only", "full"]
    assert not cache.exists() or not list(cache.iterdir())


def test_a_report_that_is_not_an_imports_replay_fails_the_check(tmp_path, monkeypatch):
    """The imports-only step must report that it replayed only the imports."""
    calls, cache = _staged_check(tmp_path, monkeypatch, _replayed(mode_reported="full"))
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "did not replay the imported modules" in result["message"]
    assert not cache.exists() or not list(cache.iterdir())


def test_a_fresh_fallback_is_reported_full(tmp_path, monkeypatch):
    """A module replayed with its imports from an empty environment is `full`."""
    calls, cache = _staged_check(tmp_path, monkeypatch, _replayed(fresh="quotient"))
    _check(tmp_path)
    result = _check(tmp_path)
    assert result["replay"] == "full"
    assert "reused" not in result["message"]
    assert _modes(calls) == ["imports-only", "imports-cached", "imports-cached"]


def test_unknown_digest_means_no_key_and_full_replay(tmp_path, monkeypatch):
    """A module whose file the snapshot did not record is never keyed."""
    from glitnir.lean import replay

    original = replay.module_files
    monkeypatch.setattr(
        replay,
        "module_files",
        lambda snapshot, isolated, name, loaded: (
            None if name == "Dep" else original(snapshot, isolated, name, loaded)
        ),
    )
    calls, cache = _staged_check(tmp_path, monkeypatch, _replayed())
    assert _check(tmp_path)["replay"] == "full"
    assert _check(tmp_path)["replay"] == "full"
    assert _modes(calls) == ["full", "full"]
    assert not cache.exists()


def test_replay_must_load_the_predicted_closure(tmp_path, monkeypatch):
    """A replay that loaded other modules or skipped unasked fails the check."""

    def other(arguments, closure):
        return _replayed()(arguments, closure[:1])

    _staged_check(tmp_path, monkeypatch, other)
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "different import closure" in result["message"]

    _staged_check(tmp_path, monkeypatch, lambda arguments, closure: "not json")
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "malformed" in result["message"]


@pytest.mark.parametrize("reported", ["imports cached", "imports only"])
def test_an_unrequested_skip_fails_the_check(tmp_path, monkeypatch, reported):
    """A full replay that reports skipping its imports is refused."""
    from glitnir.lean import project_checking as checking

    monkeypatch.setattr(checking, "replay_identity", lambda *args: None)
    _staged_check(tmp_path, monkeypatch, _replayed(mode_reported=reported))
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "skipped its imports" in result["message"]


def test_a_report_without_a_closure_fails_the_check(tmp_path, monkeypatch):
    """Without the reporter's closure the replay cannot be compared; refuse."""
    from glitnir.lean import project_checking as checking

    monkeypatch.setattr(checking, "import_closure", lambda report, module: None)
    calls, _ = _staged_check(tmp_path, monkeypatch, _replayed())
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "no import closure" in result["message"]
    assert _modes(calls) == []


def test_toolchain_modules_must_come_from_the_toolchain(tmp_path, monkeypatch):
    """A module the toolchain provides, loaded from elsewhere, fails the check."""

    def replay(arguments, closure):
        return _replayed()(arguments, closure)

    calls, _ = _staged_check(tmp_path, monkeypatch, replay)
    from glitnir.lean import project_checking as checking

    original = checking.import_closure

    def moved(report, module):
        closure = original(report, module)
        return [
            [name, path.replace("inputs/toolchain/lib/lean", "candidate-isolated")]
            for name, path in closure
        ]

    monkeypatch.setattr(checking, "import_closure", moved)
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "Init was loaded from outside the toolchain" in result["message"]


def test_changed_dependency_bytes_miss_the_record(tmp_path, monkeypatch):
    """The same names with one changed file are another key."""
    from glitnir.lean import project_checking as checking
    from glitnir.lean.project_snapshot import ProjectSnapshot

    calls, cache = _staged_check(tmp_path, monkeypatch, _replayed())
    _check(tmp_path)
    capture = checking.capture

    def changed(*args, **kwargs):
        snapshot = capture(*args, **kwargs)
        other = hashlib.sha256(b"other").hexdigest()
        kwargs["store"].add("toolchain", other, [b"other"])
        (snapshot.artifacts / "Dep.olean").unlink()
        (snapshot.artifacts / "Dep.olean").symlink_to(
            kwargs["store"].path("toolchain", other)
        )
        digests = {**snapshot.digests, "artifacts/Dep.olean": other}
        return ProjectSnapshot(
            **{**snapshot.__dict__, "digests": digests},
        )

    monkeypatch.setattr(checking, "capture", changed)
    assert _check(tmp_path)["replay"] == "full"
    assert _modes(calls) == [
        "imports-only",
        "imports-cached",
        "imports-only",
        "imports-cached",
    ]
    assert len(list(cache.iterdir())) == 2


def test_module_files_require_links_to_the_recorded_stored_files(tmp_path):
    """Digests come only from snapshot records of the exact loaded files.

    Every snapshot entry must link to the stored file named by its recorded
    digest: a regular file, or a link to another stored file, is unknown.
    """
    from glitnir.lean.artifact_store import _blob_path as stored_path
    from glitnir.lean.project_snapshot import ProjectSnapshot
    from glitnir.lean.replay import module_files

    blobs = tmp_path / "store/blobs"

    def blob_path(blobs, digest):
        return stored_path(
            blobs, "p" + "0" * 32 if digest[0] == "2" else "toolchain", digest
        )

    inputs = tmp_path / "inputs"
    lib = inputs / "toolchain/lib/lean"
    lib.mkdir(parents=True)
    (inputs / "artifacts").mkdir()
    digests = {
        "toolchain/lib/lean/Init.olean": "1" * 64,
        "artifacts/Dep.olean": "2" * 64,
    }
    for digest in digests.values():
        blob_path(blobs, digest).parent.mkdir(parents=True, exist_ok=True)
        blob_path(blobs, digest).write_bytes(digest.encode())
    (lib / "Init.olean").symlink_to(blob_path(blobs, "1" * 64))
    (inputs / "artifacts/Dep.olean").symlink_to(blob_path(blobs, "2" * 64))
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "Dep.olean").symlink_to(inputs / "artifacts/Dep.olean")
    snapshot = ProjectSnapshot(
        inputs, inputs / "toolchain", inputs / "artifacts", inputs, {}, "id", 0, 0
    )
    snapshot = ProjectSnapshot(
        **{
            **snapshot.__dict__,
            "digests": digests,
            "stores": {
                "toolchain": blobs / "toolchain",
                "artifacts": blobs / ("p" + "0" * 32),
            },
        }
    )
    assert module_files(snapshot, isolated, "Init", str(lib / "Init.olean")) == {
        ".olean": "1" * 64,
        ".olean.server": "absent",
        ".olean.private": "absent",
        ".ir.sig": "absent",
        ".ir": "absent",
    }
    assert module_files(snapshot, isolated, "Dep", str(isolated / "Dep.olean"))
    # Loaded from elsewhere, under another name, or with an unrecorded part.
    assert module_files(snapshot, isolated, "Dep", str(tmp_path / "Dep.olean")) is None
    assert module_files(snapshot, isolated, "Init", str(isolated / "Dep.olean")) is None
    # Lean 4.35 reads `.ir.sig` too, so an unrecorded one is unknown as well.
    for suffix in (".ir", ".ir.sig"):
        (lib / f"Init{suffix}").write_bytes(b"unrecorded")
        assert module_files(snapshot, isolated, "Init", str(lib / "Init.olean")) is None
        (lib / f"Init{suffix}").unlink()
    # An entry linking to another stored file, or not a link at all.
    (inputs / "artifacts/Dep.olean").unlink()
    (inputs / "artifacts/Dep.olean").symlink_to(blob_path(blobs, "1" * 64))
    assert module_files(snapshot, isolated, "Dep", str(isolated / "Dep.olean")) is None
    (inputs / "artifacts/Dep.olean").unlink()
    (inputs / "artifacts/Dep.olean").write_bytes(b"2" * 64)
    assert module_files(snapshot, isolated, "Dep", str(isolated / "Dep.olean")) is None
    (inputs / "artifacts/Dep.olean").unlink()
    (inputs / "artifacts/Dep.olean").symlink_to(blob_path(blobs, "2" * 64))
    (lib / "Init.olean").unlink()
    (lib / "Init.olean").write_bytes(b"1" * 64)
    assert module_files(snapshot, isolated, "Init", str(lib / "Init.olean")) is None
    (lib / "Init.olean").unlink()
    (lib / "Init.olean").symlink_to(blob_path(blobs, "1" * 64))
    # Without a store no entry is known.
    unstored = ProjectSnapshot(**{**snapshot.__dict__, "stores": {}})
    assert module_files(unstored, isolated, "Init", str(lib / "Init.olean")) is None
    # A recorded file that is missing, or an isolated copy that is not the link.
    del digests["toolchain/lib/lean/Init.olean"]
    assert module_files(snapshot, isolated, "Init", str(lib / "Init.olean")) is None
    (isolated / "Dep.olean").unlink()
    (isolated / "Dep.olean").write_bytes(b"dep")
    assert module_files(snapshot, isolated, "Dep", str(isolated / "Dep.olean")) is None


# ----- the replay's axioms decide statuses -------------------------------------


def _axiom(name, module="Init"):
    return {"name": name, "module": module}


AGREED = [
    {"name": "plain", "axioms": []},
    {"name": "standard", "axioms": [_axiom("propext")]},
    {"name": "admitted", "axioms": [_axiom("propext"), _axiom("sorryAx")]},
    {"name": "assumed", "axioms": [_axiom("custom", "GlitnirStandalone")]},
    {"name": "traced", "axioms": [_axiom("propext", "Dep")]},
]


@pytest.mark.parametrize("cached", [False, True])
def test_statuses_come_from_the_replays_axioms(tmp_path, monkeypatch, cached):
    """Agreeing axioms decide each status, on a full and on a cached replay."""
    from glitnir.lean import project_checking as checking

    if not cached:
        monkeypatch.setattr(checking, "replay_identity", lambda *args: None)
    calls, _ = _staged_check(
        tmp_path, monkeypatch, _replayed(theorems=AGREED), reported=AGREED
    )
    result = _check(tmp_path)
    assert result["status"] == "checked_snapshot", result
    assert _modes(calls) == (["imports-only", "imports-cached"] if cached else ["full"])
    found = {t["name"]: (t["status"], t["axioms"]) for t in result["theorems"]}
    assert found == {
        "plain": ("checked", []),
        "standard": ("checked", [_axiom("propext")]),
        "admitted": ("admitted", [_axiom("propext"), _axiom("sorryAx")]),
        "assumed": ("assumptions", [_axiom("custom", "GlitnirStandalone")]),
        # A standard name not declared by a toolchain module.
        "traced": ("assumptions", [_axiom("propext", "Dep")]),
    }


def _changed(theorems, name, axioms):
    return [
        {**theorem, "axioms": axioms} if theorem["name"] == name else theorem
        for theorem in theorems
    ]


@pytest.mark.parametrize(
    ("replayed", "reported"),
    [
        # The replay walks into `sorry` the module report does not see.
        (_changed(AGREED, "plain", [_axiom("sorryAx")]), AGREED),
        # The module report sees an axiom the kernel-checked environment lacks.
        (AGREED, _changed(AGREED, "plain", [_axiom("custom", "GlitnirStandalone")])),
        # Same axiom, another module.
        (_changed(AGREED, "standard", [_axiom("propext", "Dep")]), AGREED),
        # Same axioms, another order.
        (
            _changed(AGREED, "admitted", [_axiom("sorryAx"), _axiom("propext")]),
            AGREED,
        ),
    ],
)
def test_disagreeing_axioms_fail_the_check(tmp_path, monkeypatch, replayed, reported):
    """Any difference between the replay's and the report's axioms fails closed."""
    _staged_check(
        tmp_path, monkeypatch, _replayed(theorems=replayed), reported=reported
    )
    result = _check(tmp_path)
    assert result["status"] == "unavailable", result
    assert result["theorems"] == []
    assert "disagree on the axioms of" in result["message"]


@pytest.mark.parametrize(
    "replayed",
    [AGREED[1:], [*AGREED, {"name": "extra", "axioms": []}], []],
)
def test_the_replay_must_list_the_reported_theorems(tmp_path, monkeypatch, replayed):
    """A theorem missing from either list fails the check."""
    _staged_check(tmp_path, monkeypatch, _replayed(theorems=replayed), reported=AGREED)
    result = _check(tmp_path)
    assert result["status"] == "unavailable", result
    assert "list different theorems" in result["message"]


@pytest.mark.parametrize(
    "theorems",
    [
        [{"name": "plain"}],
        [{"name": "plain", "axioms": [], "type": "T"}],
        [{"name": "plain", "axioms": ["sorryAx"]}],
        [{"name": "plain", "axioms": [{"name": "sorryAx"}]}],
        [{"name": "plain", "axioms": [{**_axiom("sorryAx"), "trusted": True}]}],
        [{"name": "plain", "axioms": [_axiom("sorryAx", 1)]}],
        [{"name": "plain", "axioms": []}, {"name": "plain", "axioms": []}],
        "plain",
    ],
)
def test_malformed_replayed_axioms_fail_the_check(tmp_path, monkeypatch, theorems):
    """Replayed theorems must be unique names with name/module axiom entries."""
    from glitnir.lean import project_checking as checking

    monkeypatch.setattr(checking, "replay_identity", lambda *args: None)
    reported = [{"name": "plain", "axioms": []}]

    def replay(arguments, closure):
        value = json.loads(_replayed(theorems=reported)(arguments, closure))
        return json.dumps({**value, "theorems": theorems})

    _staged_check(tmp_path, monkeypatch, replay, reported=reported)
    result = _check(tmp_path)
    assert result["status"] == "unavailable", result
    assert "kernel replay report is malformed" in result["message"]


def test_an_imports_replay_reports_no_theorems(tmp_path, monkeypatch):
    """The imports-only step checks no module constant, so it reports none."""

    def replay(arguments, closure):
        value = json.loads(_replayed(theorems=AGREED)(arguments, closure))
        return json.dumps({**value, "theorems": AGREED})

    calls, cache = _staged_check(tmp_path, monkeypatch, replay, reported=AGREED)
    result = _check(tmp_path)
    assert result["status"] == "unavailable", result
    assert "malformed" in result["message"]
    assert _modes(calls) == ["imports-only"]
    assert not cache.exists() or not list(cache.iterdir())


# ----- the test suite's own cache fixtures -------------------------------------


def test_default_cache_is_private_to_a_test_without_live_fixtures(session_replay_cache):
    """Stub-driven tests never share entries; only live tests share the session's."""
    assert replay_cache.cache_directory() != session_replay_cache
    assert not replay_cache.cache_directory().exists()


@pytest.fixture
def project_toolchain():
    """Stand in for the live toolchain gate; nothing here runs Lean."""
    return None


def test_a_live_test_shares_the_session_cache(project_toolchain, session_replay_cache):
    """A test using `project_toolchain` records into the one session cache."""
    assert replay_cache.cache_directory() == session_replay_cache


def test_a_fresh_cache_is_empty_and_not_the_session_cache(
    session_replay_cache, fresh_replay_cache
):
    """`fresh_replay_cache` wins over the default whatever ran before."""
    assert replay_cache.cache_directory() == fresh_replay_cache
    assert fresh_replay_cache != session_replay_cache
    assert not fresh_replay_cache.exists()


@pytest.fixture
def replay_records(tmp_path_factory):
    """Stand in for the session's recorded bases with one written record."""
    directory = tmp_path_factory.mktemp("recorded") / "glitnir" / "replay"
    _record(directory)
    return lambda helper, toolchain: directory


def test_a_recorded_cache_is_a_private_copy_of_the_recorded_base(
    replay_records, recorded_replay_cache
):
    """A test starts from the recorded base, and what it records stays its own."""
    recorded = replay_records("helper", None)
    directory = recorded_replay_cache()
    assert replay_cache.cache_directory() == directory != recorded
    assert replay_cache.contains(directory, KEY, context=CONTEXT, modules=MODULES)
    grown = MODULES + [["C", DIGEST["C"]]]
    assert replay_cache.find_base(directory, context=CONTEXT, modules=grown) == [
        "A",
        "B",
    ]
    _record(directory, key="b" * 64, modules=grown)
    assert [p.name for p in directory.iterdir()] != [KEY + ".json"]
    assert [p.name for p in recorded.iterdir()] == [KEY + ".json"]


def test_a_superset_replays_only_its_new_modules_over_the_largest_base(
    tmp_path, monkeypatch
):
    """A recorded closure is the base of a larger one; the larger is recorded too."""
    bases: list = []
    calls, cache = _staged_check(
        tmp_path, monkeypatch, _replayed(bases=bases), names=("Init",)
    )
    assert _check(tmp_path)["replay"] == "full"
    _staged_check.names.append("Dep")  # type: ignore[attr-defined]
    grown = _check(tmp_path)
    assert grown["status"] == "checked_snapshot", grown
    assert grown["replay"] == "imports cached (+1 module)"
    assert "except 1 module replayed now" in grown["message"]
    assert bases == [["Init"]]
    assert _check(tmp_path)["replay"] == "imports cached"
    _staged_check.names.append("Extra")  # type: ignore[attr-defined]
    assert _check(tmp_path)["replay"] == "imports cached (+1 module)"
    assert bases == [["Init"], ["Init", "Dep"]]  # the largest base
    assert _modes(calls) == [
        "imports-only",
        "imports-cached",
        "imports-incremental",
        "imports-cached",
        "imports-cached",
        "imports-incremental",
        "imports-cached",
    ]
    assert len(list(cache.iterdir())) == 3


def test_a_base_that_is_not_extendable_is_not_used(tmp_path, monkeypatch):
    """Only closures recorded as extendable serve as bases."""
    calls, cache = _staged_check(
        tmp_path, monkeypatch, _replayed(extendable=False), names=("Init",)
    )
    _check(tmp_path)
    _staged_check.names.append("Dep")  # type: ignore[attr-defined]
    assert _check(tmp_path)["replay"] == "full"
    assert _modes(calls)[2] == "imports-only"


def test_a_failed_incremental_replay_records_nothing(tmp_path, monkeypatch):
    """A new module the kernel rejects fails the check; no record is written."""
    from glitnir.lean.project_snapshot import SnapshotError

    def replay(arguments, closure):
        if _mode(arguments) == "imports-incremental":
            raise SnapshotError("while replaying declaration 'bad'")
        return _replayed()(arguments, closure)

    calls, cache = _staged_check(tmp_path, monkeypatch, replay, names=("Init",))
    _check(tmp_path)
    _staged_check.names.append("Dep")  # type: ignore[attr-defined]
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "while replaying the imported modules once" in result["message"]
    assert len(list(cache.iterdir())) == 1


def test_a_replay_that_ignored_its_base_counts_as_a_full_import_replay(
    tmp_path, monkeypatch
):
    """The program may refuse a base and replay all imports; that is `full`."""

    def replay(arguments, closure):
        if _mode(arguments) == "imports-incremental":
            return _replayed(mode_reported="imports only")(arguments, closure)
        return _replayed()(arguments, closure)

    calls, cache = _staged_check(tmp_path, monkeypatch, replay, names=("Init",))
    _check(tmp_path)
    _staged_check.names.append("Dep")  # type: ignore[attr-defined]
    assert _check(tmp_path)["replay"] == "full"
    assert len(list(cache.iterdir())) == 2


def test_an_incremental_report_without_a_base_fails_the_check(tmp_path, monkeypatch):
    """A replay claiming to extend a base it was not given is refused."""

    def replay(arguments, closure):
        output = json.loads(_replayed()(arguments, closure))
        if _mode(arguments) == "imports-only":
            output.update(replay="imports incremental", base_modules=1)
        return json.dumps(output)

    _staged_check(tmp_path, monkeypatch, replay)
    result = _check(tmp_path)
    assert result["status"] == "unavailable"
    assert "extended a base it was not given" in result["message"]


def test_a_base_holding_every_module_reports_imports_cached(tmp_path, monkeypatch):
    """The same modules recorded under other imports add nothing: `imports cached`."""
    calls, cache = _staged_check(tmp_path, monkeypatch, _replayed())
    _check(tmp_path)
    monkeypatch.setattr(replay_cache, "contains", lambda *args, **kwargs: False)
    again = _check(tmp_path)
    assert again["replay"] == "imports cached", again
    assert _modes(calls)[2:] == ["imports-incremental", "imports-cached"]
