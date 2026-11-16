"""No worker exit receipt before its exact job cgroup is observed empty."""

import os
from uuid import uuid4

import pytest


def test_job_exit_kills_group_and_waits_for_empty(tmp_path, monkeypatch):
    from glitnir.compute import containment

    root = tmp_path / "delegated"
    root.mkdir()
    (root / "cgroup.procs").write_text(str(os.getpid()))
    monkeypatch.setattr(containment, "CGROUP_ROOT", tmp_path)
    boot = str(uuid4())
    monkeypatch.setattr(containment, "boot_id", lambda: boot)
    monkeypatch.setattr(containment.sys, "platform", "linux")
    monkeypatch.setattr(containment, "host_id", lambda: "f" * 64)
    group = containment.JobGroup(root, str(uuid4()))
    (group.path / "cgroup.kill").touch()
    (group.path / "cgroup.events").write_text("populated 0\n")
    group.stop()
    assert (group.path / "cgroup.kill").read_text() == "1"
    assert group.receipt("a" * 64)["empty"] is True


def test_populated_or_unavailable_group_never_reports_exit(tmp_path, monkeypatch):
    from glitnir.compute import containment

    root = tmp_path / "delegated"
    root.mkdir()
    (root / "cgroup.procs").write_text(str(os.getpid()))
    monkeypatch.setattr(containment, "CGROUP_ROOT", tmp_path)
    boot = str(uuid4())
    monkeypatch.setattr(containment, "boot_id", lambda: boot)
    monkeypatch.setattr(containment.sys, "platform", "linux")
    monkeypatch.setattr(containment, "host_id", lambda: "f" * 64)
    group = containment.JobGroup(root, str(uuid4()))
    with pytest.raises(ValueError):
        group.receipt("a" * 64)
    (group.path / "cgroup.kill").touch()
    (group.path / "cgroup.events").write_text("populated 1\n")
    with pytest.raises(TimeoutError):
        group.stop(timeout=0)
    with pytest.raises(ValueError):
        group.receipt("a" * 64)


def test_group_requires_current_worker_delegation(tmp_path, monkeypatch):
    from glitnir.compute import containment

    monkeypatch.setattr(containment, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(containment.sys, "platform", "linux")
    monkeypatch.setattr(containment, "host_id", lambda: "f" * 64)
    (tmp_path / "cgroup.procs").write_text("999999999")
    with pytest.raises(ValueError):
        containment.JobGroup(tmp_path, str(uuid4()))


def test_boot_change_recovery_requires_original_host(tmp_path, monkeypatch):
    from glitnir.compute import containment

    monkeypatch.setattr(containment, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(containment, "host_id", lambda: "f" * 64)
    original, current, job = str(uuid4()), str(uuid4()), str(uuid4())
    monkeypatch.setattr(containment, "boot_id", lambda: current)
    intent = {
        "boot_id": original,
        "host_id": "f" * 64,
        "job_id": job,
        "cgroup": str(tmp_path / ("job-" + job)),
    }
    receipt = containment.recover_receipt(intent, "a" * 64)
    assert receipt["method"] == "linux-boot-change"
    assert receipt["boot_id"] == original and receipt["observed_boot_id"] == current
    with pytest.raises(ValueError, match="original host"):
        containment.recover_receipt(intent | {"host_id": "e" * 64}, "a" * 64)
    monkeypatch.setattr(containment, "boot_id", lambda: original)
    with pytest.raises(ValueError, match="changed boot"):
        containment.recover_receipt(intent, "a" * 64)
