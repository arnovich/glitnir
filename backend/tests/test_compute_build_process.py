"""Trusted build supervision retains the cache lock until orphan cleanup."""

import fcntl
import os
import signal
import subprocess
import sys
import time


def test_parent_death_stops_build_before_cache_lock_is_released(tmp_path):
    lock = tmp_path / "lock"
    started = tmp_path / "started"
    late = tmp_path / "late"
    specification = tmp_path / "command.json"
    parent_file = tmp_path / "parent.py"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import time; "
        f"Path({str(started)!r}).touch(); time.sleep(2); "
        f"Path({str(late)!r}).touch()",
    ]
    parent_file.write_text(
        "import fcntl,json,os,subprocess,sys,time\n"
        f'lock=open({str(lock)!r}, "a+b")\n'
        "fcntl.flock(lock, fcntl.LOCK_EX)\n"
        f'spec={dict(command=command, result=str(tmp_path / "result"))!r}\n'
        'spec["parent"]=os.getpid()\n'
        f'open({str(specification)!r}, "w").write(json.dumps(spec))\n'
        'child=subprocess.Popen([sys.executable,"-I","-B","-m",'
        f'"glitnir.compute.build_process",{str(specification)!r}], '
        "start_new_session=True, pass_fds=(lock.fileno(),))\n"
        "time.sleep(20)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, str(parent_file)], start_new_session=True
    )
    try:
        limit = time.monotonic() + 15
        while not started.exists() and time.monotonic() < limit:
            time.sleep(0.02)
        assert started.exists()
        parent.kill()
        parent.wait(timeout=5)
        with lock.open("a+b") as stream:
            limit = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < limit
                    time.sleep(0.02)
        time.sleep(2.2)
        assert not late.exists()
        assert not (tmp_path / "result").exists()
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid, signal.SIGKILL)
        parent.wait()
