"""The sandboxed Lean run and its limits."""

import pytest

from glitnir.lean.project_snapshot import SnapshotError


def test_scratch_budget_counts_total_bytes_and_ignores_links(tmp_path, monkeypatch):
    """Many individually small outputs cannot escape the aggregate scratch cap."""
    from glitnir.lean import observer

    root = tmp_path / "stage"
    root.mkdir()
    secret = tmp_path / "outside"
    secret.write_bytes(b"x" * 100)
    (root / "link").symlink_to(secret)
    monkeypatch.setattr(observer, "STAGE_BYTES", 10)
    observer._check_stage(root)
    (root / "one").write_bytes(b"x" * 6)
    (root / "two").write_bytes(b"x" * 6)
    with pytest.raises(SnapshotError, match="scratch byte limit"):
        observer._check_stage(root)
    monkeypatch.setattr(observer, "STAGE_ENTRIES", 1)
    with pytest.raises(SnapshotError, match="scratch file limit"):
        observer._check_stage(root)


def test_running_out_of_memory_names_the_limit_and_the_setting(tmp_path, monkeypatch):
    """Lean's memory error becomes advice naming the limit and the setting."""
    import threading
    import time

    from glitnir.lean import observer

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
    monkeypatch.setattr(observer, "confine", lambda command, **_options: command)
    with pytest.raises(SnapshotError) as raised:
        observer.run_observer(
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
