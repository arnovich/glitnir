"""Snapshot inputs live once, immutable and named by content, in a per-user store."""

import hashlib
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from glitnir.lean import artifact_store, project_snapshot, sandbox
from glitnir.lean.artifact_store import StoreError, blob_path, lease
from glitnir.lean.project_snapshot import SnapshotError, capture, store_file

NS = artifact_store.TOOLCHAIN


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blobs(directory: Path) -> list[Path]:
    return sorted(p for p in (directory / "blobs").rglob("*") if p.is_file())


def test_the_store_follows_the_cache_conventions(tmp_path, monkeypatch):
    """The store sits beside the replay cache, under the user's cache directory."""
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(artifact_store.replay_cache.sys, "platform", "darwin")
    assert artifact_store.store_directory() == (
        tmp_path / "Library/Caches/glitnir/artifacts"
    )
    monkeypatch.setattr(artifact_store.replay_cache.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert artifact_store.store_directory() == tmp_path / "xdg/glitnir/artifacts"


def test_a_stored_file_is_private_immutable_and_named_by_its_bytes(tmp_path):
    """Files are 0400 in 0700 directories, verified, and never replaced."""
    directory = tmp_path / "store"
    data = b"module bytes"
    digest = _sha(data)
    with lease(directory) as store:
        assert not store.contains(NS, digest, len(data))
        store.add(NS, digest, [b"module ", b"bytes"])
        path = store.path(NS, digest)
        assert path == blob_path(directory.resolve() / "blobs", NS, digest)
        assert path.read_bytes() == data
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        for folder in (directory, directory / "blobs", path.parent):
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert store.contains(NS, digest, len(data))
        inode = path.stat().st_ino
        # Bytes that do not hash to the name are never published under it.
        with pytest.raises(StoreError, match="changed"):
            store.add(NS, digest, [b"other bytes"])
        store.add(NS, digest, [data])  # already there: the stored file stays
        assert path.stat().st_ino == inode
        assert path.read_bytes() == data
        with pytest.raises(PermissionError):
            path.open("r+b")
    assert _blobs(directory) == [path]  # no temporary file is left behind


def test_concurrent_writers_of_one_digest_leave_one_valid_file(tmp_path):
    """Writers racing on the same bytes all succeed and share one stored file."""
    directory = tmp_path / "store"
    data = os.urandom(256 * 1024)
    digest = _sha(data)
    errors: list[BaseException] = []
    start = threading.Barrier(8, timeout=30)

    def write() -> None:
        try:
            with lease(directory) as store:
                start.wait()
                store.add(NS, digest, [data])
                assert store.contains(NS, digest, len(data))
        except BaseException as error:  # reported below
            errors.append(error)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    (stored,) = _blobs(directory)
    assert stored.name == digest and stored.read_bytes() == data


@pytest.mark.parametrize("tamper", ["writable", "size", "link", "directory"])
def test_a_malformed_stored_file_is_refused(tmp_path, tamper):
    """A stored name that is not a 0400 file of the right size is never used."""
    directory = tmp_path / "store"
    data = b"dependency"
    digest = _sha(data)
    with lease(directory) as store:
        store.add(NS, digest, [data])
        path = store.path(NS, digest)
        if tamper == "writable":
            path.chmod(0o600)
        elif tamper == "size":
            path.chmod(0o600)
            path.write_bytes(b"dependency!")
            path.chmod(0o400)
        elif tamper == "link":
            path.unlink()
            (tmp_path / "elsewhere").write_bytes(data)
            path.symlink_to(tmp_path / "elsewhere")
        else:
            path.unlink()
            path.mkdir()
        with pytest.raises(StoreError, match="not a valid stored file"):
            store.contains(NS, digest, len(data))
        with pytest.raises(StoreError):
            store.add(NS, digest, [data])


def test_capture_refuses_a_tampered_stored_input(tmp_path, project_snapshot_inputs):
    """A check never links its snapshot to a stored file it cannot vouch for."""
    root, toolchain, source, artifact, setup = project_snapshot_inputs
    directory = tmp_path / "store"
    with lease(directory) as store:
        namespace = artifact_store.project_namespace(root)
        digest, _ = store_file(artifact, store, namespace, max_bytes=1024)
        store.path(namespace, digest).chmod(0o600)
        destination = tmp_path / "check"
        destination.mkdir()
        with pytest.raises(SnapshotError, match="not a valid stored file"):
            capture(
                destination,
                source,
                setup,
                toolchain,
                root,
                threading.Event(),
                store=store,
            )


@pytest.mark.parametrize("mode", [0o755, 0o750])
def test_a_store_others_can_reach_is_refused(tmp_path, mode):
    """The store and its blob directory must be the user's private directories."""
    directory = tmp_path / "store"
    directory.mkdir(mode=0o700)
    directory.chmod(mode)
    with pytest.raises(StoreError, match="not private"):
        with lease(directory):
            pytest.fail("leased a shared store")
    directory.chmod(0o700)
    (directory / "blobs").mkdir(mode=0o700)
    (directory / "blobs").chmod(mode)
    with pytest.raises(StoreError, match="not private"):
        with lease(directory):
            pytest.fail("leased a shared blob directory")


def test_pruning_never_removes_a_file_an_open_lease_may_read(tmp_path):
    """While any check holds the store nothing is pruned; afterwards LRU goes."""
    directory = tmp_path / "store"
    files = [bytes([n]) * 100 for n in range(3)]
    with lease(directory, budget=0) as first:
        for data in files:
            first.add(NS, _sha(data), [data])
        with lease(directory, budget=0):
            pass  # cannot prune: the first lease is still open
        assert len(_blobs(directory)) == 3
        with lease(directory, budget=0) as second:
            assert second.contains(NS, _sha(files[0]), 100)
    # The first lease ended last and pruned to its budget of nothing.
    assert _blobs(directory) == []


def test_pruning_removes_least_recently_used_files_and_stale_temporaries(
    tmp_path, monkeypatch
):
    """Beyond the budget the least recently used go first; stale files go too.

    Use is recorded beside the files: using a stored file never changes it,
    not even its times.
    """
    monkeypatch.setattr(artifact_store, "MAX_USE_RECORDS", 3)
    directory = tmp_path / "store"
    files = [bytes([n]) * 100 for n in range(3)]
    for data in files:
        with lease(directory) as store:
            store.add(NS, _sha(data), [data])
    oldest = store.path(NS, _sha(files[0]))
    times = oldest.stat().st_mtime_ns, oldest.stat().st_ctime_ns
    with lease(directory) as store:
        assert store.contains(NS, _sha(files[0]), 100)
    assert (oldest.stat().st_mtime_ns, oldest.stat().st_ctime_ns) == times
    stale = oldest.parent / f".{_sha(files[0])}.123.{'0' * 16}.tmp"
    stale.write_bytes(b"interrupted")
    with lease(directory, budget=200):
        pass
    assert not stale.exists()
    # files[1] was used least recently; files[0] was used again last.
    assert [p.name for p in _blobs(directory)] == sorted(
        [_sha(files[0]), _sha(files[2])]
    )
    assert len(list((directory / "uses").iterdir())) == 3


def test_a_file_in_no_use_record_counts_as_used_when_stored(tmp_path):
    """Without a record, a stored file's age decides; bad records are ignored."""
    directory = tmp_path / "store"
    files = [bytes([n]) * 100 for n in range(2)]
    with lease(directory) as store:
        for age, data in zip((200, 100), files):
            store.add(NS, _sha(data), [data])
            marked = store.path(NS, _sha(data)).stat().st_mtime - age
            os.utime(store.path(NS, _sha(data)), (marked, marked))
    for record in (directory / "uses").iterdir():
        record.write_text("not a digest\n")
    with lease(directory, budget=100):
        pass
    assert [p.name for p in _blobs(directory)] == [_sha(files[1])]


def test_capture_links_every_input_to_its_stored_bytes(
    tmp_path, project_snapshot_inputs
):
    """Digests stay those of the bytes read; entries link to those stored bytes.

    Toolchain files go to the shared namespace and the project's to its own;
    project sources are hashed only, never stored.
    """
    root, toolchain, source, artifact, setup = project_snapshot_inputs
    directory = tmp_path / "store"
    snapshots = []
    for name in ("cold", "warm"):
        destination = tmp_path / name
        destination.mkdir()
        with lease(directory) as store:
            snapshots.append(
                capture(
                    destination,
                    source,
                    setup,
                    toolchain,
                    root,
                    threading.Event(),
                    store=store,
                )
            )
    cold, warm = snapshots
    assert cold.identity == warm.identity and cold.digests == warm.digests
    blobs = directory.resolve() / "blobs"
    project = artifact_store.project_namespace(root)
    assert (
        cold.stores
        == warm.stores
        == {
            "toolchain": blobs / "toolchain",
            "artifacts": blobs / project,
            "native": blobs / project,
        }
    )
    originals = {
        "toolchain/lib/lean/Lean.olean": toolchain / "lib/lean/Lean.olean",
        "artifacts/Dependency.olean": artifact,
    }
    for snapshot in snapshots:
        for relative, original in originals.items():
            entry = snapshot.root / relative
            digest = snapshot.digests[relative]
            assert digest == _sha(original.read_bytes())
            assert entry.is_symlink()
            assert os.readlink(entry) == str(snapshot.stored(relative, digest))
            assert entry.read_bytes() == original.read_bytes()
        assert snapshot.digests["sources/A.lean"] == _sha(source.read_bytes())
        assert not (snapshot.root / "sources").exists()
        # The executable is a private copy: the loader resolves its real path.
        lean = snapshot.toolchain / "bin/lean"
        assert lean.is_file() and not lean.is_symlink()
        assert snapshot.digests["toolchain/bin/lean"] == _sha(lean.read_bytes())
    assert sorted(p.relative_to(blobs).parts[0] for p in _blobs(directory)) == [
        project,
        "toolchain",
    ]
    assert _sha(source.read_bytes()) not in {p.name for p in _blobs(directory)}
    # Rewriting an input later never changes what a snapshot links to.
    artifact.write_bytes(b"dependency version two")
    assert (cold.artifacts / "Dependency.olean").read_bytes() == (
        b"dependency version one"
    )


def test_capture_without_a_lease_uses_a_private_store(
    tmp_path, project_snapshot_inputs
):
    """A caller holding no lease gets a store of its own inside the snapshot."""
    root, toolchain, source, _artifact, setup = project_snapshot_inputs
    captured = capture(tmp_path, source, setup, toolchain, root, threading.Event())
    assert captured.stores["toolchain"] == tmp_path.resolve() / "store/blobs/toolchain"
    assert (captured.artifacts / "Dependency.olean").read_bytes() == (
        b"dependency version one"
    )


def test_a_snapshot_over_its_total_names_the_total_limit(
    tmp_path, project_snapshot_inputs, monkeypatch
):
    """Small files that together pass the cap report the snapshot's limit."""
    root, toolchain, source, _artifact, setup = project_snapshot_inputs
    monkeypatch.setattr(project_snapshot, "MAX_SNAPSHOT_BYTES", 8)
    with pytest.raises(SnapshotError, match="Project snapshot exceeds its byte"):
        capture(tmp_path, source, setup, toolchain, root, threading.Event())


def test_a_large_input_is_read_again_and_must_hash_the_same(tmp_path, monkeypatch):
    """Inputs too large to hold in memory are stored only if unchanged."""
    monkeypatch.setattr(project_snapshot, "BUFFERED_BYTES", 4)
    source = tmp_path / "large.olean"
    source.write_bytes(b"large module bytes")
    with lease(tmp_path / "store") as store:
        digest, size = store_file(source, store, NS, max_bytes=1024)
        assert (digest, size) == (_sha(b"large module bytes"), 18)
        assert store.path(NS, digest).read_bytes() == b"large module bytes"
        changed = tmp_path / "changing.olean"
        changed.write_bytes(b"first version of it")
        opened = project_snapshot._open_input
        calls = []

        def rewriting(path, max_bytes):
            calls.append(path)
            if len(calls) == 2:
                path.write_bytes(b"other version of it")
            return opened(path, max_bytes)

        monkeypatch.setattr(project_snapshot, "_open_input", rewriting)
        with pytest.raises(StoreError, match="changed"):
            store_file(changed, store, NS, max_bytes=1024)
        assert not store.contains(NS, _sha(b"first version of it"), 19)
        assert not store.contains(NS, _sha(b"other version of it"), 19)


def test_a_full_disk_is_reported(tmp_path, monkeypatch):
    """Running out of space names the store instead of a bare OS error."""
    import errno

    def full(*_arguments):
        raise OSError(errno.ENOSPC, "No space left on device")

    with lease(tmp_path / "store") as store:
        monkeypatch.setattr(artifact_store.os, "write", full)
        with pytest.raises(StoreError, match="No space"):
            store.add(NS, _sha(b"x"), [b"x"])
    monkeypatch.undo()
    assert _blobs(tmp_path / "store") == []


def test_checks_see_only_their_own_store_namespaces_read_only(
    tmp_path, monkeypatch, project_snapshot_inputs
):
    """Lean reads the toolchain's and its project's stored files, nothing else."""
    from glitnir.lean import project_checking as checking

    root, toolchain, source, _artifact, setup = project_snapshot_inputs
    directory = tmp_path / "store"
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "Secret.olean").write_bytes(b"another project's file")
    with lease(directory) as store:
        store_file(
            other / "Secret.olean",
            store,
            artifact_store.project_namespace(other),
            max_bytes=1024,
        )
        destination = tmp_path / "check"
        destination.mkdir()
        captured = capture(
            destination, source, setup, toolchain, root, threading.Event(), store=store
        )
    seen = {}

    def confine(command, *, readable, writable, cwd, required):
        seen["readable"], seen["writable"] = list(readable), list(writable)
        raise sandbox.SandboxUnavailable("stop")

    monkeypatch.setattr(checking, "confine", confine)
    cwd = tmp_path / "build"
    cwd.mkdir()
    with pytest.raises(sandbox.SandboxUnavailable):
        checking._run(
            captured,
            ["lean", "--version"],
            cwd=cwd,
            readable=[],
            writable=True,
            cancel=threading.Event(),
            deadline=float("inf"),
        )
    blobs = directory.resolve() / "blobs"
    exposed = {captured.stores["toolchain"], captured.stores["artifacts"]}
    assert [p for p in seen["readable"] if p.is_relative_to(directory.resolve())] == (
        sorted(exposed)
    )
    assert blobs not in seen["readable"]
    assert seen["writable"] == [cwd]
    monkeypatch.setattr(sandbox, "_bubblewrap", lambda: "/usr/bin/bwrap")
    command = " ".join(
        sandbox.confine(
            [str(captured.toolchain / "bin/lean")],
            readable=seen["readable"],
            writable=seen["writable"],
            cwd=cwd,
        )
    )
    secret = str(blobs / artifact_store.project_namespace(other))
    assert secret not in command
    for path in exposed:
        assert f"--ro-bind {path} {path}" in command
        assert f"--bind {path} " not in command
    profile = sandbox.seatbelt_profile(seen["readable"], seen["writable"])
    writes = [line for line in profile.splitlines() if "file-write*" in line]
    assert secret not in profile
    for path in exposed:
        assert any(str(path) in line for line in profile.splitlines())
        assert not any(str(path) in line for line in writes)


def test_a_failed_check_still_records_its_use_and_prunes(tmp_path):
    """Bookkeeping happens however the lease ends, an error included."""
    directory = tmp_path / "store"
    data = b"used by a failing check"
    with pytest.raises(RuntimeError):
        with lease(directory, budget=0) as store:
            store.add(NS, _sha(data), [data])
            raise RuntimeError("the check failed")
    assert len(list((directory / "uses").iterdir())) == 1
    assert _blobs(directory) == []  # pruned to its budget of nothing


def test_a_cache_parent_others_can_write_is_refused(tmp_path):
    """The store's parent must be the user's and writable by nobody else."""
    parent = tmp_path / "glitnir"
    parent.mkdir(mode=0o700)
    parent.chmod(0o775)
    with pytest.raises(StoreError, match="not private"):
        with lease(parent / "artifacts"):
            pytest.fail("leased a store under a shared directory")
    parent.chmod(0o755)
    with lease(parent / "artifacts"):
        pass


def test_pruning_keeps_the_free_space_reserve(tmp_path, monkeypatch):
    """However large the budget, the store never leaves less than the reserve."""
    directory = tmp_path / "store"
    files = [bytes([n]) * 1000 for n in range(3)]
    for data in files:
        with lease(directory) as store:
            store.add(NS, _sha(data), [data])
    monkeypatch.setattr(artifact_store, "FREE_RESERVE_BYTES", 10_000)
    # 9,000 bytes free: keeping 10,000 free means giving back 1,000 bytes.
    monkeypatch.setattr(
        artifact_store, "_free_bytes", lambda _descriptor: (9_000, 10**12)
    )
    with lease(directory):
        pass
    assert [p.name for p in _blobs(directory)] == sorted(
        [_sha(files[1]), _sha(files[2])]
    )


def test_pruning_scans_only_when_recorded_totals_may_exceed_the_bounds(
    tmp_path, monkeypatch
):
    """The recorded total plus later additions decide whether to scan."""
    directory = tmp_path / "store"
    scans = []
    real_scan = artifact_store._scan

    def scan(blobs):
        scans.append(1)
        return real_scan(blobs)

    monkeypatch.setattr(artifact_store, "_scan", scan)
    with lease(directory, budget=250) as store:
        store.add(NS, _sha(b"a" * 100), [b"a" * 100])
    assert len(scans) == 1  # nothing recorded yet
    with lease(directory, budget=250) as store:
        store.add(NS, _sha(b"b" * 100), [b"b" * 100])
    assert len(scans) == 1  # 100 recorded + 100 added fits 250
    with lease(directory, budget=250) as store:
        store.add(NS, _sha(b"c" * 100), [b"c" * 100])
    assert len(scans) == 2  # 300 may exceed 250: scan and prune
    assert len(_blobs(directory)) == 2


def test_a_new_project_at_an_old_projects_path_gets_another_namespace(tmp_path):
    """The namespace binds the directory itself, not only its path."""
    first = tmp_path / "project"
    first.mkdir()
    namespace = artifact_store.project_namespace(first)
    (tmp_path / "alias").symlink_to(first)
    assert artifact_store.project_namespace(tmp_path / "alias") == namespace
    first.rename(tmp_path / "moved")
    first.mkdir()  # another directory, while the first still exists
    assert artifact_store.project_namespace(first) != namespace


def test_a_scan_time_in_the_future_forces_a_full_scan(tmp_path, monkeypatch):
    """A recorded scan that is not in the past never lets pruning be skipped."""
    directory = tmp_path / "store"
    with lease(directory, budget=10**6) as store:
        store.add(NS, _sha(b"a"), [b"a"])
    usage = directory / ".usage"
    usage.write_text('{"total": 0, "scanned": %d}' % (time.time_ns() + 10**15))
    scans = []
    real_scan = artifact_store._scan
    monkeypatch.setattr(
        artifact_store, "_scan", lambda blobs: scans.append(1) or real_scan(blobs)
    )
    with lease(directory, budget=10**6):
        pass
    assert scans == [1]


def test_capture_without_a_lease_keeps_its_files_after_returning(
    tmp_path, monkeypatch, project_snapshot_inputs
):
    """The private store is never pruned: the snapshot is used afterwards."""
    monkeypatch.setattr(artifact_store, "MAX_STORE_BYTES", 0)
    root, toolchain, source, _artifact, setup = project_snapshot_inputs
    captured = capture(tmp_path, source, setup, toolchain, root, threading.Event())
    assert (captured.artifacts / "Dependency.olean").read_bytes() == (
        b"dependency version one"
    )
    assert (captured.toolchain / "lib/lean/Lean.olean").read_bytes() == (
        b"trusted library"
    )


def test_pruning_removes_entries_of_an_earlier_layout(tmp_path):
    """Namespace-less shards and stray files in the blob directory are removed."""
    directory = tmp_path / "store"
    with lease(directory) as store:
        store.add(NS, _sha(b"kept"), [b"kept"])
    legacy = directory / "blobs" / _sha(b"old")[:2]
    legacy.mkdir(mode=0o700)
    (legacy / _sha(b"old")).write_bytes(b"old")
    (legacy / _sha(b"old")).chmod(0o400)
    (directory / "blobs" / "stray").write_bytes(b"stray")
    (directory / ".usage").unlink()
    with lease(directory):
        pass
    assert sorted(p.name for p in (directory / "blobs").iterdir()) == [NS]
    assert [p.name for p in _blobs(directory)] == [_sha(b"kept")]
