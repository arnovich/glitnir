"""Two real worker processes share only a read-only prepared environment."""

import os
import subprocess
import sys
import time
from pathlib import Path

from glitnir.compute.protocol import make_request
from glitnir.lean.environment import kill_and_reap


def _native_descendants(workers):
    """Find native Lean descendants of each worker on supported Unix hosts."""
    listing = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,comm="], text=True, timeout=10
    )
    processes = {}
    for line in listing.splitlines():
        pid, parent, executable = line.strip().split(None, 2)
        processes[int(pid)] = (int(parent), Path(executable).name)
    result = []
    for worker in workers:
        descendants = {worker.pid}
        while True:
            expanded = descendants | {
                pid for pid, (parent, _) in processes.items() if parent in descendants
            }
            if expanded == descendants:
                break
            descendants = expanded
        result.append(
            {pid for pid in descendants if processes.get(pid, (0, ""))[1] == "lean"}
        )
    return result


def test_live_two_slots_overlap_cancel_and_recover(
    prepared_lean_project, two_slot_server, tmp_path
):
    """Native checks overlap; one slot's cancellation permits subsequent recovery."""
    url, state, lock = two_slot_server
    source = prepared_lean_project / "src/Example.lean"
    original = source.read_text()
    # Keep the first job elaborating until its next lease heartbeat cancels it.
    source.write_text(
        "import Example.Helper\n"
        "theorem result : True := by\n"
        "  run_tac Lean.Elab.Tactic.liftIO (IO.sleep 30000)\n"
        "  trivial\n"
    )
    cancelled = make_request(source, "prepared", 600, 4096)
    source.write_text(original)
    recovered = make_request(source, "prepared", 600, 4096)
    other = make_request(source, "prepared", 600, 4096)
    # Distinct jobs with the same frozen candidate, as a real queue would assign.
    assert recovered["request_id"] != other["request_id"]
    state["queues"] = {"slot0": [cancelled, recovered], "slot1": [other]}
    script = Path(__file__).parent / "fixtures/compute_slot.py"
    processes, logs, slots = [], [], []
    try:
        for index in range(2):
            slot = tmp_path / f"slot{index}"
            slots.append(slot)
            for name in ("home", "tmp", "cache"):
                (slot / name).mkdir(parents=True)
            log = (slot / "worker.log").open("w+")
            logs.append(log)
            environment = {
                **os.environ,
                "HOME": str(slot / "home"),
                "TMPDIR": str(slot / "tmp"),
                "XDG_CACHE_HOME": str(slot / "cache"),
                "ELAN_HOME": os.environ.get("ELAN_HOME", str(Path.home() / ".elan")),
                "GLITNIR_LEAN_THREADS": str(index + 1),
            }
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(script),
                        url,
                        f"slot{index}",
                        str(prepared_lean_project),
                        str(slot / "journal"),
                        "2" if index == 0 else "1",
                    ],
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            native = _native_descendants(processes)
            if all(native):
                break
            assert all(
                p.poll() is None for p in processes
            ), "worker ended before native overlap"
            time.sleep(0.1)
        else:
            raise AssertionError(
                "No overlapping native Lean processes in the two slots"
            )
        assert native[0].isdisjoint(native[1])
        with lock:
            state["cancelled"].add(cancelled["request_id"])
        # The next heartbeat is at most ten seconds away. Before handing out
        # recovery, prove that cancelled work left no native descendants behind.
        assert state["recovery_waiting"].wait(timeout=20)
        assert not _native_descendants(processes)[0]
        state["allow_recovery"].set()
        for process in processes:
            assert process.wait(timeout=600) == 0
        with lock:
            assert len(state["workers"]) == 2
            identities = [state["workers"][f"slot{i}"]["worker_id"] for i in range(2)]
            assert len(set(identities)) == 2
            assert len(state["claims"]) == 3
            assert cancelled["request_id"] not in state["completed"]
            for request in (recovered, other):
                completion = state["completed"][request["request_id"]]
                result = completion["result"]
                assert result["status"] == "checked_snapshot", result
                assert result["source_sha256"] == request["source_sha256"]
                assert result["theorems"][0]["status"] == "checked", result
        for slot, identity in zip(slots, identities):
            assert (slot / "journal" / identity / "stage").is_dir()
            cache = slot / (
                "home/Library/Caches" if sys.platform == "darwin" else "cache"
            )
            assert list((cache / "glitnir/replay").glob("*.json"))
        assert source.read_text() == original
    except BaseException:
        for log in logs:
            log.flush()
            log.seek(0)
            print(log.read())
        raise
    finally:
        for process in processes:
            kill_and_reap(process)
        for log in logs:
            log.close()
