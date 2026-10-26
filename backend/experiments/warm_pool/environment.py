"""Public fixtures and exact snapshots for the offline warm-process experiment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from experiments.warm_pool.resources import Budget
from glitnir.compute.protocol import checker_digest, context_digest
from glitnir.lean import artifact_store
from glitnir.lean.environment import bind_to_parent, kill_and_reap
from glitnir.lean.project_inspection import TOOLCHAIN, _toolchain, inspect_project
from glitnir.lean.project_snapshot import ProjectSnapshot, capture
from glitnir.lean.proof_trace import _absolute_setup

PROGRAM = Path(__file__).with_name("Worker.lean")


def create_fixture(
    root: Path, toolchain: Path, *, profile: str = "Basic", budget: Budget
) -> Path:
    """Build only fixed public inputs; never execute a submitted project's hooks."""
    if profile not in {"Basic", "Standard", "State"} or root.exists():
        raise ValueError("Use a new fixture directory and a supported profile")
    root.mkdir(mode=0o700, parents=True)
    (root / "PoolBench").mkdir()
    (root / "lean-toolchain").write_text(TOOLCHAIN + "\n")
    (root / "lakefile.toml").write_text(
        'name = "pool_bench"\n[[lean_lib]]\nname = "PoolBench"\n'
    )
    modules = {
        "Basic": "namespace PoolBench\ndef value : Nat := 1\nend PoolBench\n",
        "Standard": (
            "import Std.Data.HashMap\nnamespace PoolBench\n"
            "def value : Nat := 1\nend PoolBench\n"
        ),
        "State": (
            "import Lean\nnamespace PoolBench\n"
            "initialize counter : IO.Ref Nat ← IO.mkRef 0\nend PoolBench\n"
        ),
    }
    (root / "PoolBench" / f"{profile}.lean").write_text(modules[profile])
    source = root / "PoolBench/Candidate.lean"
    source.write_text(f"import PoolBench.{profile}\n")
    build(
        [
            str(toolchain / "bin/lake"),
            "--keep-toolchain",
            "--no-cache",
            "build",
            f"PoolBench/{profile}.lean:leanArts",
        ],
        root=root,
        budget=budget,
        env={
            "PATH": f"{toolchain / 'bin'}:/usr/bin:/bin",
            "LEAN_SYSROOT": str(toolchain),
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LEAN_NUM_THREADS": "2",
        },
    )
    return source


def build(
    command: list[str],
    *,
    root: Path,
    budget: Budget,
    env: dict[str, str],
    seconds: float = 120,
) -> None:
    """Bound trusted fixture builds and always reap their ordinary descendants."""
    deadline = time.monotonic() + seconds
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
            preexec_fn=partial(bind_to_parent, os.getpid(), 1_000_000),
        )
        try:
            while process.poll() is None:
                if budget.cancel.wait(0.05) or time.monotonic() >= deadline:
                    raise ValueError("fixture build cancelled or timed out")
                if os.fstat(output.fileno()).st_size > 1_000_000:
                    raise ValueError("fixture build output ceiling exceeded")
            if process.returncode:
                raise ValueError("fixed fixture build failed")
        finally:
            kill_and_reap(process)


def _signature(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return info.st_ino, info.st_size, info.st_mtime_ns


@dataclass(frozen=True)
class FrozenEnvironment:
    """A native snapshot, with cheap edit guards in addition to its content identity.

    The snapshot's stored bytes, not an untrusted report or these stat guards,
    identify what Lean reads. Stat guards detect ordinary operator edits; this
    experiment does not defend against a malicious host modifying its own cache.
    """

    key: str
    source: Path
    context_sha256: str
    snapshot: ProjectSnapshot
    program: Path
    setup_file: Path
    guards: tuple[tuple[Path, tuple[int, int, int]], ...]
    preparation_ms: float

    def assert_current(self) -> None:
        """Refuse changed original source/configuration, program or compiled inputs."""
        if context_digest(self.source) != self.context_sha256 or any(
            _signature(path) != signature for path, signature in self.guards
        ):
            raise ValueError("frozen environment inputs changed")


def freeze(
    source: Path, destination: Path, store: artifact_store.ArtifactStore, budget: Budget
) -> FrozenEnvironment:
    """Capture complete native inputs once; the caller retains the store lease."""
    started = time.monotonic()
    source = source.resolve()
    # Upper-bound the first store fill, including duplicate paths, and leave
    # 512 MiB for independently bounded native-check scratch. Existing stored
    # toolchain bytes need not be reserved again for a later environment.
    toolchain = _toolchain()
    if not (store.blobs / artifact_store.TOOLCHAIN).exists():
        needed = sum(
            path.stat().st_size
            for path in (toolchain / "lib").rglob("*")
            if path.is_file() and path.suffix not in {".a", ".ilean", ".tbd"}
        )
        budget.reserve(needed + 512 * 1024**2)
    budget.check()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    inspection = inspect_project(source, timeout_seconds=120)
    if inspection["status"] != "resolved":
        raise ValueError(
            "Prepare fixture imports first: " + inspection.get("message", "unresolved")
        )
    project, toolchain = Path(inspection["project"]["root"]), _toolchain()
    context = context_digest(source)
    snapshot = capture(
        destination,
        source,
        inspection["setup"],
        toolchain,
        project,
        budget.cancel,
        configuration={
            item["path"]: item["sha256"] for item in inspection["resolution"]["inputs"]
        },
        store=store,
    )
    program = snapshot.root / "Worker.lean"
    program.write_bytes(PROGRAM.read_bytes())
    setup_file = snapshot.root / "warm-setup.json"
    setup_file.write_text(json.dumps(_absolute_setup(snapshot.setup, snapshot.root)))
    identity = hashlib.sha256(
        json.dumps(
            {
                "format": "glitnir.warm-experiment/v1",
                "snapshot": snapshot.identity,
                "worker": hashlib.sha256(program.read_bytes()).hexdigest(),
                "checker": checker_digest(),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    originals = {PROGRAM, source}
    for relative in snapshot.digests:
        if relative.startswith("toolchain/"):
            originals.add(toolchain / relative.removeprefix("toolchain/"))
    for parent, directories, files in os.walk(project, followlinks=False):
        directories[:] = [name for name in directories if name not in {".git", ".venv"}]
        for name in files:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError("Experimental fixtures cannot contain links")
            originals.add(path)
    guards = tuple((path, _signature(path)) for path in sorted(originals))
    environment = FrozenEnvironment(
        identity,
        source,
        context,
        snapshot,
        program,
        setup_file,
        guards,
        (time.monotonic() - started) * 1000,
    )
    environment.assert_current()
    return environment
