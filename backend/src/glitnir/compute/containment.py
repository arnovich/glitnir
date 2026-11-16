"""Optional operator-delegated Linux cgroup containment for verified job exit.

Trusted preparation commands and sandboxed Lean processes enter the job cgroup
before dispatch. The parent worker never enters it and alone reports its exit.
"""

import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

CGROUP_ROOT = Path("/sys/fs/cgroup")


def boot_id() -> str:
    """Read the actual kernel boot identity, never infer it from wall time."""
    return str(UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))


def host_id() -> str:
    """Bind recovery to this operator-provisioned host, not a copied journal."""
    path = Path("/etc/machine-id")
    info = path.stat()
    value = path.read_text().strip()
    if info.st_uid != 0 or info.st_mode & 0o022 or len(value) != 32:
        raise ValueError("A protected unique Linux machine identity is required")
    int(value, 16)
    return hashlib.sha256(value.encode()).hexdigest()


def recover_receipt(intent: dict[str, Any], source_sha256: str) -> dict[str, Any]:
    """Observe original work as stopped, without killing or dispatching any process."""
    current_host, current_boot = host_id(), boot_id()
    if intent.get("host_id") != current_host:
        raise ValueError("Worker exit recovery must run on the original host")
    original = str(UUID(intent["boot_id"]))
    job = str(UUID(intent["job_id"]))
    path = Path(intent["cgroup"])
    if (
        not path.is_relative_to(CGROUP_ROOT)
        or path != path.resolve()
        or path.name != "job-" + job
    ):
        raise ValueError("Original job cgroup binding changed")
    if current_boot == original:
        raise ValueError(
            "Recovery requires a changed boot; retry a sealed exit otherwise"
        )
    return {
        "format": "glitnir.worker-exit/v1",
        "method": "linux-boot-change",
        "boot_id": original,
        "observed_boot_id": current_boot,
        "host_id": current_host,
        "job_id": job,
        "source_sha256": source_sha256,
        "cgroup": str(path),
        "empty": True,
    }


class JobGroup:
    """Own one newly created child cgroup in the worker's delegated service."""

    path: Path
    job_id: str
    boot: str
    host: str
    _verified: bool

    def __init__(self, root: Path, job_id: str) -> None:
        """Refuse foreign roots and reuse; no process is dispatched here."""
        root = root.absolute()
        if (
            sys.platform != "linux"
            or root != root.resolve()
            or not root.is_relative_to(CGROUP_ROOT)
            or str(UUID(job_id)) != job_id
            or str(os.getpid()) not in (root / "cgroup.procs").read_text().split()
        ):
            raise ValueError(
                "Exit reporting requires this worker's delegated Linux cgroup"
            )
        self.path = root / ("job-" + job_id)
        self.path.mkdir(mode=0o700)
        self.job_id = job_id
        self.boot = boot_id()
        self.host = host_id()
        self._verified = False

    def stop(self, *, timeout: float = 5) -> None:
        """Kill the complete subtree and verify populated=0 before attesting exit."""
        self._verified = False
        if boot_id() != self.boot:
            raise ValueError("Worker boot changed during job cleanup")
        (self.path / "cgroup.kill").write_text("1")
        deadline = time.monotonic() + timeout
        while True:
            fields = dict(
                line.split()
                for line in (self.path / "cgroup.events").read_text().splitlines()
            )
            if fields.get("populated") == "0":
                self._verified = True
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Job cgroup still contains processes")
            time.sleep(0.02)

    def receipt(self, source_sha256: str) -> dict[str, Any]:
        """Emit a report only after successful complete-subtree cleanup."""
        if not self._verified:
            raise ValueError("Job process exit is not verified")
        return {
            "format": "glitnir.worker-exit/v1",
            "method": "linux-cgroup-v2",
            "boot_id": self.boot,
            "host_id": self.host,
            "job_id": self.job_id,
            "source_sha256": source_sha256,
            "cgroup": str(self.path),
            "empty": True,
        }
