"""Tests for confining untrusted Lean processes."""

import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from glitnir.lean import sandbox
from glitnir.lean.project_inspection import TOOLCHAIN


@pytest.fixture
def bwrap(monkeypatch):
    monkeypatch.setattr(sandbox, "_bubblewrap", lambda: "/usr/bin/bwrap")


@pytest.fixture
def no_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "_bubblewrap", lambda: None)
    monkeypatch.setattr(sandbox, "_seatbelt", lambda: None)


@pytest.fixture
def seatbelt(monkeypatch):
    monkeypatch.setattr(sandbox, "_bubblewrap", lambda: None)
    monkeypatch.setattr(sandbox, "_seatbelt", lambda: "/usr/bin/sandbox-exec")


macos = pytest.mark.skipif(platform.system() != "Darwin", reason="needs Seatbelt")


def test_confined_commands_see_a_read_only_root_and_named_paths(
    bwrap, tmp_path
) -> None:
    readable, writable = tmp_path / "project", tmp_path / "scratch"
    readable.mkdir()
    command = sandbox.confine(
        ["lean", "--json", "X.lean"],
        readable=[readable, tmp_path / "missing"],
        writable=[writable],
        cwd=writable,
    )
    assert command[:3] == ["/bin/sh", "-c", 'exec "$@" 9<"$0"']
    assert Path(command[3]).read_bytes() == sandbox.keyring_filter()
    assert command[4:7] == ["/usr/bin/bwrap", "--seccomp", "9"]
    joined = " ".join(command)
    for flag in ("--unshare-all", "--die-with-parent", "--new-session"):
        assert flag in command
    assert "--ro-bind / /" not in joined
    assert "--ro-bind /usr /usr" in joined
    assert "/etc/passwd" not in joined and "/home " not in joined
    assert "--tmpfs /tmp" in joined and f"--tmpfs {Path.home()}" in joined
    assert f"--ro-bind {readable} {readable}" in joined
    assert str(tmp_path / "missing") not in joined
    assert f"--bind {writable} {writable}" in joined
    assert command[-6:] == ["--chdir", str(writable), "--", "lean", "--json", "X.lean"]
    assert command.index("--tmpfs") < command.index(f"{readable}")


def test_system_links_are_recreated_and_missing_paths_skipped(
    monkeypatch, tmp_path
) -> None:
    usr, lib = tmp_path / "usr", tmp_path / "lib"
    usr.mkdir()
    lib.symlink_to("usr/lib")
    monkeypatch.setattr(
        sandbox, "SYSTEM_PATHS", (str(usr), str(lib), str(tmp_path / "nix"))
    )
    assert sandbox._system_binds() == [
        "--ro-bind",
        str(usr),
        str(usr),
        "--symlink",
        "usr/lib",
        str(lib),
    ]


def test_without_a_sandbox_lean_does_not_start(
    no_sandbox, monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv(sandbox.UNSANDBOXED_VARIABLE, raising=False)
    with pytest.raises(sandbox.SandboxUnavailable, match="GLITNIR_UNSANDBOXED"):
        sandbox.confine(["lean"], readable=[], cwd=tmp_path)
    assert not sandbox.sandbox_available()


def test_opting_out_runs_lean_unconfined(no_sandbox, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(sandbox.UNSANDBOXED_VARIABLE, "1")
    assert sandbox.confine(["lean", "x"], readable=[], cwd=tmp_path) == ["lean", "x"]
    assert sandbox.unsandboxed_allowed()


def test_readable_paths_include_the_toolchain_and_resolved_links(
    monkeypatch, tmp_path
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "elan"))
    paths = sandbox.lean_readable(link)
    assert tmp_path / "elan" in paths and link in paths and real in paths


def test_the_probe_reports_a_missing_or_failing_bwrap(monkeypatch) -> None:
    sandbox._bubblewrap.cache_clear()
    try:
        monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
        assert sandbox._bubblewrap() is None
        sandbox._bubblewrap.cache_clear()
        monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/bin/false")
        assert sandbox._bubblewrap() is None
        sandbox._bubblewrap.cache_clear()

        def broken(*args, **kwargs):
            raise OSError("no")

        monkeypatch.setattr(sandbox.subprocess, "run", broken)
        assert sandbox._bubblewrap() is None
    finally:
        sandbox._bubblewrap.cache_clear()


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="needs bubblewrap")
def test_a_confined_process_cannot_write_outside_its_directories(tmp_path) -> None:
    sandbox._bubblewrap.cache_clear()
    if not sandbox.sandbox_available():
        pytest.skip("bubblewrap does not work here")
    outside, scratch = tmp_path / "outside.txt", tmp_path / "scratch"
    scratch.mkdir()
    command = sandbox.confine(
        ["sh", "-c", f"echo x > {outside}; echo y > {scratch}/inside.txt"],
        readable=[],
        writable=[scratch],
        cwd=scratch,
    )
    subprocess.run(command, capture_output=True)
    assert not outside.exists()
    assert (scratch / "inside.txt").read_text() == "y\n"


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="needs bubblewrap")
def test_a_confined_process_sees_only_system_programs_and_named_paths(
    tmp_path,
) -> None:
    sandbox._bubblewrap.cache_clear()
    if not sandbox.sandbox_available():
        pytest.skip("bubblewrap does not work here")
    readable, scratch = tmp_path / "project", tmp_path / "scratch"
    readable.mkdir()
    (readable / "visible.txt").write_text("v")
    scratch.mkdir()
    command = sandbox.confine(
        [
            "sh",
            "-c",
            f"ls /; echo ---; ls /etc; cat {readable}/visible.txt; "
            "cat /etc/passwd /etc/hostname; ls /home /var /sys /root",
        ],
        readable=[readable],
        writable=[scratch],
        cwd=scratch,
    )
    seen = subprocess.run(command, capture_output=True, text=True)
    top = set(seen.stdout.split("---")[0].split())
    assert "usr" in top and not top & {"var", "sys", "root", "opt", "srv", "mnt"}
    assert "v" in seen.stdout
    assert "root:" not in seen.stdout
    assert "No such file" in seen.stderr


def test_the_keyring_filter_denies_exactly_the_keyring_calls() -> None:
    program = sandbox.keyring_filter()
    ops = [program[i : i + 8] for i in range(0, len(program), 8)]
    assert len(program) % 8 == 0 and len(ops) == 1 + 3 * 8 + 1
    import struct

    decoded = [struct.unpack("<HBBI", op) for op in ops]
    assert decoded[0] == (0x20, 0, 0, 4) and decoded[-1] == (0x06, 0, 0, 0x80000000)
    denied = {k for code, jt, _, k in decoded if code == 0x15 and jt}
    assert {248, 249, 250, 286, 287, 288, 217, 218, 219} <= denied


def test_a_program_the_sandbox_cannot_see_is_refused(bwrap, tmp_path) -> None:
    hidden = tmp_path / "opt" / "elan"
    with pytest.raises(sandbox.SandboxUnavailable, match="outside every path"):
        sandbox.confine([str(hidden), "run"], readable=[], cwd=tmp_path)
    visible = sandbox.confine(
        [str(hidden), "run"], readable=[hidden.parent], cwd=tmp_path
    )
    assert str(hidden) in visible


def test_an_architecture_without_a_filter_is_refused(
    bwrap, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "riscv64")
    with pytest.raises(sandbox.SandboxUnavailable, match="seccomp"):
        sandbox.confine(["lean"], readable=[], cwd=tmp_path)


def test_the_elan_on_path_is_readable(monkeypatch, tmp_path) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: str(bin_directory / name))
    assert bin_directory in sandbox.lean_readable(tmp_path / "project")


def test_the_seatbelt_profile_denies_all_it_does_not_name(tmp_path) -> None:
    readable, scratch = tmp_path / "project", tmp_path / "scratch"
    readable.mkdir()
    scratch.mkdir()
    profile = sandbox.seatbelt_profile([readable, tmp_path / "missing"], [scratch])
    lines = profile.splitlines()
    assert lines[:2] == ["(version 1)", "(deny default)"]
    denials = [line for line in lines[2:] if not line.startswith("(allow ")]
    assert len(denials) == 3 and all(d.startswith("(deny ") for d in denials)
    real = str(readable.resolve())
    for action in ("process-exec", "file-read*"):
        assert any(action in line and f'(subpath "{real}")' in line for line in lines)
    writes = [line for line in lines if "file-write*" in line]
    assert any(f'(subpath "{scratch.resolve()}")' in line for line in writes)
    assert not any(str(readable.resolve()) in line for line in writes)
    assert not any("process-exec" in line and "scratch" in line for line in lines)
    assert "missing" not in profile
    for absent in (
        "process-fork",
        "process-info",
        "network",
        "mach-lookup",
        "ipc-posix",
        "signal",
    ):
        assert absent not in profile
    assert '(allow file-read* (literal "/")' in profile  # the root may be listed
    assert not any(
        "subpath" in line and ('"/tmp"' in line or '"/private/var"' in line)
        for line in lines
        if line.startswith("(allow")
    )
    # Later rules win: host volumes are hidden again, but the loader's cache
    # is not; code written to scratch cannot be mapped; no device properties.
    hidden = lines.index('(deny file-read* process-exec (subpath "/System/Volumes"))')
    assert lines[hidden + 1] == (
        "(allow file-read* process-exec "
        '(subpath "/System/Volumes/Preboot/Cryptexes"))'
    )
    assert f'(deny file-map-executable (subpath "{scratch.resolve()}")' in profile
    assert lines[-1] == "(deny iokit-get-properties)"


def test_a_writable_path_may_not_be_executable(tmp_path) -> None:
    project = tmp_path / "project"
    (project / "build").mkdir(parents=True)
    for readable, writable in (
        (project, project / "build"),
        (project / "build", project),
    ):
        with pytest.raises(sandbox.SandboxUnavailable, match="writable and exec"):
            sandbox.seatbelt_profile([readable], [writable])


def test_seatbelt_strings_are_escaped_and_control_characters_refused(
    tmp_path,
) -> None:
    odd = tmp_path / 'a "quoted\\ name'
    odd.mkdir()
    assert (
        sandbox._quoted(odd)
        == '"' + str(odd).replace("\\", "\\\\").replace('"', '\\"') + '"'
    )
    with pytest.raises(sandbox.SandboxUnavailable, match="profile"):
        sandbox._quoted("/tmp/a\nb")


def test_supervisor_flags_never_write_imported_bytecode(seatbelt, tmp_path) -> None:
    """An isolated supervisor cannot rely on Python environment settings."""
    module = tmp_path / "fresh_module.py"
    module.write_text("value = 7\n")
    command = sandbox.confine(
        ["/usr/bin/true"], readable=[tmp_path], writable=[], cwd=tmp_path
    )
    interpreter = command[: command.index(str(sandbox._SUPERVISOR))]
    program = (
        "import importlib.util, sys; "
        "spec = importlib.util.spec_from_file_location('fresh_module', sys.argv[1]); "
        "module = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module); assert module.value == 7"
    )
    subprocess.run(
        [*interpreter, "-c", program, str(module)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
        timeout=10,
    )
    assert not (tmp_path / "__pycache__").exists()


def test_on_macos_the_command_runs_under_the_supervisor(seatbelt, tmp_path) -> None:
    command = sandbox.confine(
        ["/usr/bin/true", "x"], readable=[tmp_path], writable=[], cwd=tmp_path
    )
    assert command[:6] == [
        sys.executable,
        "-I",
        "-B",
        "-S",
        str(sandbox._SUPERVISOR),
        str(os.getpid()),
    ]
    assert command[6:8] == ["/usr/bin/sandbox-exec", "-p"]
    assert command[8].startswith("(version 1)\n(deny default)")
    assert command[9:] == ["/usr/bin/true", "x"]
    with pytest.raises(sandbox.SandboxUnavailable, match="may execute"):
        sandbox.confine(
            [str(tmp_path / "elsewhere")],
            readable=[],
            writable=[tmp_path],
            cwd=tmp_path,
        )


@macos
def test_a_seatbelt_process_reads_writes_and_starts_only_what_it_may(
    tmp_path,
) -> None:
    sandbox._seatbelt.cache_clear()
    assert sandbox.sandbox_available()
    readable, scratch = tmp_path / "project", tmp_path / "scratch"
    readable.mkdir()
    (readable / "visible.txt").write_text("v\n")
    scratch.mkdir()
    outside = tmp_path / "outside.txt"

    def run(*command: str) -> subprocess.CompletedProcess[str]:
        wrapped = sandbox.confine(
            command, readable=[readable], writable=[scratch], cwd=scratch
        )
        return subprocess.run(wrapped, capture_output=True, text=True, cwd=scratch)

    # Shell builtins only: a failed fork ends the shell.
    seen = run(
        "/bin/sh",
        "-c",
        f"read v < {readable}/visible.txt && echo read=$v; "
        "read p < /etc/passwd && echo passwd; "
        f"echo x > {outside}; echo y > {scratch}/inside.txt",
    )
    assert "read=v" in seen.stdout and "passwd" not in seen.stdout, seen
    assert not outside.exists()
    assert (scratch / "inside.txt").read_text() == "y\n"
    # A lone command would be exec'd in place; a second one forces a fork.
    forked = run("/bin/sh", "-c", "/bin/echo forked; true")
    assert "forked" not in forked.stdout and "fork: Operation not permitted" in (
        forked.stderr
    )
    # The keychains answer through Mach services, which cannot be looked up.
    keychains = ["/usr/bin/security", "list-keychains"]
    assert (
        "keychain" in subprocess.run(keychains, capture_output=True, text=True).stdout
    )
    listed = run(*keychains)
    assert listed.returncode != 0 and "keychain" not in listed.stdout, listed
    # A listener of our own: reachable unconfined, so only the sandbox refuses.
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = str(server.getsockname()[1])
        nc = ["/usr/bin/nc", "-vz", "-w", "2", "127.0.0.1", port]
        assert subprocess.run(nc, capture_output=True).returncode == 0
        connected = run(*nc)
    assert connected.returncode != 0 and "Operation not permitted" in (
        connected.stderr
    ), connected


def _supervised(parent: int, *command: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-I", "-S", str(sandbox._SUPERVISOR), str(parent), *command],
        start_new_session=True,
    )


def _gone(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _started(marker: Path) -> int:
    deadline = time.monotonic() + 10
    while not (marker.exists() and marker.read_text().strip()):
        assert time.monotonic() < deadline, "the command never started"
        time.sleep(0.05)
    return int(marker.read_text())


@macos
def test_stopping_a_supervised_command_reaches_it_outside_its_group(
    tmp_path,
) -> None:
    """A confined process may call ``setpgid``; killing the group then misses it.

    `kill_and_reap` asks the supervisor first, which kills it by process id.
    """
    from glitnir.lean.environment import kill_and_reap

    marker = tmp_path / "pid"
    leaver = (
        "import os, pathlib, time; os.setpgid(0, 0); "
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(600)"
    )
    supervisor = _supervised(os.getpid(), sys.executable, "-c", leaver)
    child = _started(marker)
    try:
        assert os.getpgid(child) != supervisor.pid
        kill_and_reap(supervisor)
        assert _gone(child), "the command outlived kill_and_reap"
    finally:
        if not _gone(child, 0):
            os.kill(child, signal.SIGKILL)


@macos
def test_the_supervisor_reports_its_own_failure_apart_from_the_command() -> None:
    from glitnir.lean._supervisor import FAILED

    assert _supervised(os.getpid(), "/nonexistent/program").wait(10) == FAILED


@macos
def test_the_supervisor_passes_on_the_commands_exit_status() -> None:
    parent = os.getpid()
    assert _supervised(parent, "/bin/sh", "-c", "exit 7").wait(10) == 7
    assert _supervised(parent, "/bin/sh", "-c", "kill -9 $$").wait(10) == 137


@macos
def test_the_supervisor_kills_the_command_when_the_parent_dies(tmp_path) -> None:
    marker = tmp_path / "pid"
    command = ["/bin/sh", "-c", f"echo $$ > {marker}; exec sleep 600"]
    supervisor = f"[sys.executable, '-I', '-S', {str(sandbox._SUPERVISOR)!r}, "
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, os, time; "
            f"subprocess.Popen({supervisor}str(os.getpid()), *{command!r}]); "
            "time.sleep(600)",
        ]
    )
    child = _started(marker)
    try:
        parent.send_signal(signal.SIGKILL)
        parent.wait()
        assert _gone(child), "the command outlived the process that started it"
    finally:
        if not _gone(child, 0):
            os.kill(child, signal.SIGKILL)


@macos
def test_the_supervisor_kills_the_command_if_the_parent_is_already_gone() -> None:
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()
    status = _supervised(gone.pid, "/bin/sleep", "600").wait(10)
    assert status == 128 + signal.SIGKILL


def test_the_seatbelt_probe_needs_macos_and_a_working_sandbox_exec(
    monkeypatch,
) -> None:
    sandbox._seatbelt.cache_clear()
    try:
        monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
        assert sandbox._seatbelt() is None
        sandbox._seatbelt.cache_clear()
        monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(sandbox, "_SEATBELT", "/nonexistent/sandbox-exec")
        assert sandbox._seatbelt() is None
        sandbox._seatbelt.cache_clear()
        monkeypatch.setattr(sandbox, "_SEATBELT", "/bin/sh")

        def broken(*args, **kwargs):
            raise OSError("no")

        monkeypatch.setattr(sandbox.subprocess, "run", broken)
        assert sandbox._seatbelt() is None
    finally:
        sandbox._seatbelt.cache_clear()


@pytest.mark.parametrize(
    ("runs", "reads", "forks", "usable"),
    [
        (0, 1, 1, True),
        (1, 1, 1, False),  # nothing runs
        (0, 0, 1, False),  # files outside the profile are readable
        (0, 1, 0, False),  # processes can start
    ],
)
def test_the_seatbelt_probe_needs_every_denial_it_rests_on(
    monkeypatch, tmp_path, runs, reads, forks, usable
) -> None:
    statuses = iter((runs, reads, forks))
    seatbelt = tmp_path / "sandbox-exec"
    seatbelt.touch()
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox, "_SEATBELT", str(seatbelt))
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, next(statuses)),
    )
    sandbox._seatbelt.cache_clear()
    try:
        assert (sandbox._seatbelt() is not None) is usable
    finally:
        sandbox._seatbelt.cache_clear()


@macos
def test_the_seatbelt_probe_refuses_a_profile_that_is_not_enforced(
    monkeypatch,
) -> None:
    sandbox._seatbelt.cache_clear()
    try:
        assert sandbox._seatbelt() == "/usr/bin/sandbox-exec"
        sandbox._seatbelt.cache_clear()
        monkeypatch.setattr(
            sandbox, "seatbelt_profile", lambda r, w: "(version 1)(allow default)"
        )
        assert sandbox._seatbelt() is None
    finally:
        sandbox._seatbelt.cache_clear()


@macos
@pytest.mark.skipif(
    shutil.which("cc") is None or shutil.which("elan") is None,
    reason="needs a C compiler and elan",
)
def test_code_written_to_scratch_cannot_be_loaded(tmp_path) -> None:
    """``exec`` from scratch is denied, and so is mapping code written there.

    Lean loads native code with ``Lean.loadDynlib``; the same library loads from
    a readable directory, so only the scratch rule refuses it.
    """
    source = tmp_path / "hello.c"
    source.write_text(
        "#include <unistd.h>\n"
        "__attribute__((constructor)) static void hello(void) "
        '{ write(1, "LOADED\\n", 7); }\n'
    )
    readable, scratch = tmp_path / "readable", tmp_path / "scratch"
    for directory in (readable, scratch):
        directory.mkdir()
        subprocess.run(
            ["cc", "-dynamiclib", "-o", directory / "hello.dylib", source], check=True
        )
    (scratch / "T.lean").write_text("theorem t : True := trivial\n")

    def load(library: Path) -> subprocess.CompletedProcess[str]:
        command = [
            shutil.which("elan") or "elan",
            "run",
            TOOLCHAIN,
            "lean",
            f"--load-dynlib={library}",
            "T.lean",
        ]
        wrapped = sandbox.confine(
            command,
            readable=[*sandbox.lean_readable(readable), readable],
            writable=[scratch],
            cwd=scratch,
        )
        return subprocess.run(wrapped, capture_output=True, text=True, cwd=scratch)

    assert "LOADED" in load(readable / "hello.dylib").stdout
    refused = load(scratch / "hello.dylib")
    assert "LOADED" not in refused.stdout and refused.returncode != 0, refused


def test_the_supervisor_imports_where_there_is_no_kqueue(monkeypatch) -> None:
    """Linux has no kqueue, yet the independent check imports its exit status."""
    import importlib
    import select

    monkeypatch.delattr(select, "kqueue", raising=False)
    monkeypatch.delitem(sys.modules, "glitnir.lean._supervisor", raising=False)
    supervisor = importlib.import_module("glitnir.lean._supervisor")
    assert supervisor.FAILED == 125
