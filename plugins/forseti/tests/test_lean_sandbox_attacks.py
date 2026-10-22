"""Live attacks on the Lean sandbox from inside a proof.

A proof is a program: a tactic can run any IO the Lean process may. These tests
use that to probe the sandbox configuration itself: what ``/dev``, ``/proc``,
the environment and the filesystem expose, whether anything a proof starts can
outlive its step, and whether a trick that fools the
in-process first filter can reach acceptance. Each probe reports what it saw by
failing with the observation as its error, so nothing it saw is accepted.

They skip unless ``FORSETI_LEAN_PROJECT`` names a built release and a sandbox
works here, like every live Lean test.

bubblewrap on Linux lets a proof start processes inside its namespaces, so the
Linux probes observe through ``sh``. Seatbelt on macOS lets a proof start no
process at all, so the macOS probes observe through Lean's own IO instead, and
each states where the platform differs. Three Linux probes have no counterpart
here because Lean's IO cannot express them — shared memory, listing other
processes and the keyring's system calls; their denial on macOS rests on the
profile (`seatbelt_profile` grants no IPC, process information or Mach lookup)
and is exercised outside Lean in ``test_lean_sandbox.py``.
"""

import json
import platform
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from glitnir.lean import sandbox
from unit_checking import PROJECT_VARIABLE, check_unit

TIMEOUT_MS = 300_000
QUIET = "stdin := .null, stdout := .null, stderr := .null"


@pytest.fixture
def lean_project(lean_test_project, monkeypatch):
    """Point `check_unit` at the configured release, inside a working sandbox."""
    sandbox._bubblewrap.cache_clear()
    sandbox._seatbelt.cache_clear()
    if not sandbox.sandbox_available():
        pytest.skip("no sandbox works here")
    monkeypatch.setenv(PROJECT_VARIABLE, str(lean_test_project))
    return lean_test_project


linux = pytest.mark.skipif(
    platform.system() != "Linux", reason="probes through a shell under bubblewrap"
)
macos = pytest.mark.skipif(
    platform.system() != "Darwin", reason="probes Seatbelt, which is macOS only"
)


def _probe(shell: str) -> str:
    """Run a shell command from inside a proof and return what it printed."""
    script = (
        "run_tac do",
        "  let o ← IO.Process.output "
        f'{{ cmd := "sh", args := #["-c", {json.dumps(shell)}] }}',
        '  (throwError m!"PROBE<{o.stdout}|{o.stderr}>" '
        ": Lean.Elab.Tactic.TacticM Unit)",
        "trivial",
    )
    result = check_unit(": True", script, prelude="import Lean", timeout_ms=TIMEOUT_MS)
    assert result["accepted"] is False
    errors = [e for e in result["errors"] if e.startswith("PROBE<")]
    assert len(errors) == 1, result
    return errors[0].removeprefix("PROBE<").removesuffix(">")


def _io_probe(action: str) -> str:
    """Run an ``IO String`` action inside a proof; return ``OK …`` or ``ERR …``."""
    script = (
        "run_tac do",
        f"  let r ← ({action} : IO String).toBaseIO",
        '  let seen := match r with | .ok v => s!"OK {v}" | .error e => s!"ERR {e}"',
        '  (throwError m!"PROBE<{seen}>" : Lean.Elab.Tactic.TacticM Unit)',
        "trivial",
    )
    result = check_unit(": True", script, prelude="import Lean", timeout_ms=TIMEOUT_MS)
    assert result["accepted"] is False
    errors = [e for e in result["errors"] if e.startswith("PROBE<")]
    assert len(errors) == 1, result
    return errors[0].removeprefix("PROBE<").removesuffix(">")


def _read(path: Path | str) -> str:
    """An action reading a file."""
    return f"IO.FS.readFile {json.dumps(str(path))}"


def _list(path: Path | str) -> str:
    """An action listing a directory."""
    return (
        f"do let es ← System.FilePath.readDir {json.dumps(str(path))}; "
        "pure (toString (es.map (·.fileName)))"
    )


def _running(marker: str) -> list[str]:
    """Return the host processes whose command line contains the marker."""
    found = subprocess.run(["pgrep", "-af", marker], capture_output=True, text=True)
    return [line for line in found.stdout.splitlines() if "pgrep" not in line]


def _sleeper() -> tuple[str, str]:
    """Return a unique ``sleep`` duration and the command line it shows."""
    seconds = f"3000.{uuid.uuid4().int % 10**9:09d}"
    return seconds, f"sleep {seconds}"


@linux
def test_dev_holds_only_the_standard_character_devices(lean_project) -> None:
    seen = _probe("ls /dev; echo ---; find /dev -type b")
    listing, _, blocks = seen.partition("---")
    names = set(listing.split())
    assert names <= {
        "core",
        "fd",
        "full",
        "null",
        "ptmx",
        "pts",
        "random",
        "shm",
        "stderr",
        "stdin",
        "stdout",
        "tty",
        "urandom",
        "zero",
    }
    assert blocks.strip().strip("|") == ""


@linux
def test_writes_to_dev_shm_stay_inside_one_step(lean_project) -> None:
    name = f"forseti-{uuid.uuid4().hex}"
    assert "WROTE" in _probe(f"echo x > /dev/shm/{name} && echo WROTE")
    assert not Path("/dev/shm", name).exists()
    assert "ABSENT" in _probe(f"test -e /dev/shm/{name} || echo ABSENT")


@linux
def test_proc_shows_only_the_steps_own_processes(lean_project) -> None:
    seen = _probe("ls /proc | grep -cE '^[0-9]+$'; cat /proc/*/cmdline | tr '\\0' ' '")
    assert int(seen.split()[0]) < 20
    assert "pytest" not in seen
    # The only writable sysctls belong to the step's own IPC and PID
    # namespaces: a write succeeds there, which it could not unconfined, and
    # the host's value is left as it was.
    host = Path("/proc/sys/kernel/msgmax").read_text()
    written = _probe("echo 12345 > /proc/sys/kernel/msgmax; echo status=$?")
    assert "status=0" in written
    assert Path("/proc/sys/kernel/msgmax").read_text() == host
    assert "status=0" not in _probe(
        "echo x > /proc/sys/kernel/hostname; echo status=$?"
    )


@linux
def test_the_hosts_environment_does_not_reach_a_proof(
    lean_project, monkeypatch
) -> None:
    monkeypatch.setenv("FORSETI_PROBE_SECRET", "hunter2")
    seen = _probe("env")
    assert "hunter2" not in seen and "FORSETI_PROBE_SECRET" not in seen


@linux
def test_a_proof_cannot_read_system_configuration_or_home(lean_project) -> None:
    seen = _probe(
        "cat /etc/passwd /etc/hostname; ls /var /sys /root; ls -A $HOME; "
        f"ls {Path.home()}"
    )
    assert "root:" not in seen
    assert "No such file" in seen


@linux
def test_a_process_a_proof_starts_does_not_outlive_its_step(lean_project) -> None:
    seconds, marker = _sleeper()
    script = (
        "run_tac do",
        f'  let _ ← IO.Process.spawn {{ cmd := "sleep", args := #["{seconds}"], '
        f"{QUIET} }}",
        "trivial",
    )
    result = check_unit(": True", script, prelude="import Lean", timeout_ms=TIMEOUT_MS)
    assert result["errors"] == [], result
    deadline = time.monotonic() + 5
    while _running(marker) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _running(marker) == []


def test_a_kernel_skipping_proof_passes_the_first_filter_only(lean_project) -> None:
    """Adding a declaration without kernel checking fools the elaborating process.

    `debug.skipKernelTC` lets a tactic add ``forsetiSearch.bad : False`` with the
    value ``True.intro``. The elaborating process then reports no error and no
    axiom; only the independent check's kernel replay rejects it.
    """
    script = (
        "run_tac do",
        "  let decl := Lean.Declaration.thmDecl { name := `forsetiSearch.bad, "
        "levelParams := [], type := Lean.mkConst ``False, "
        "value := Lean.mkConst ``True.intro }",
        "  withTheReader Lean.Core.Context (fun c => { c with options := "
        "c.options.setBool `debug.skipKernelTC true }) (Lean.addDecl decl)",
        "exact forsetiSearch.bad",
    )
    result = check_unit(": False", script, prelude="import Lean", timeout_ms=TIMEOUT_MS)
    assert result["errors"] == [] and result["forbidden_axioms"] == []
    assert result["accepted"] is False
    assert result["independent"]["status"] == "rejected"
    assert "kernel replay rejected" in result["independent"]["reason"]


@pytest.fixture
def host_key():
    """Put a secret in this process's session keyring, and remove it after."""
    if shutil.which("keyctl") is None:
        pytest.skip("needs keyctl")
    name = f"forseti-probe-{uuid.uuid4().hex}"
    added = subprocess.run(
        ["keyctl", "add", "user", name, "hunter2", "@s"],
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        pytest.skip(f"no session keyring here: {added.stderr}")
    yield name
    subprocess.run(["keyctl", "unlink", added.stdout.strip(), "@s"], check=False)


@linux
def test_a_proof_cannot_read_or_plant_keys_in_the_hosts_keyring(
    lean_project, host_key
) -> None:
    planted = f"forseti-planted-{uuid.uuid4().hex}"
    seen = _probe(
        f"keyctl print %user:{host_key}; "
        f"keyctl add user {planted} x @s; echo added=$?; "
        "keyctl request2 user debug:forseti hello @s; echo requested=$?"
    )
    assert "hunter2" not in seen
    assert "added=0" not in seen and "requested=0" not in seen
    host = subprocess.run(["keyctl", "show", "@s"], capture_output=True, text=True)
    assert host_key in host.stdout and planted not in host.stdout


#: How Lean words ``EPERM``, the error Seatbelt denies with. Unconfined access
#: to a root-only file fails with ``EACCES``, worded "permission denied".
DENIED = "operation not permitted"


@macos
def test_a_proof_cannot_start_any_process(lean_project) -> None:
    """Seatbelt denies ``posix_spawn`` itself, so nothing can outlive a step.

    Where bubblewrap lets the process start and kills it with the step, here
    it never starts.
    """
    seconds, marker = _sleeper()
    # An absolute path the profile may execute, so only the spawn is refused.
    seen = _io_probe(
        f'do let c ← IO.Process.spawn {{ cmd := "/bin/sleep", '
        f'args := #["{seconds}"], {QUIET} }}; pure (toString c.pid)'
    )
    assert seen.startswith("ERR") and DENIED in seen
    assert _running(marker) == []


@macos
def test_dev_admits_only_the_standard_devices(lean_project) -> None:
    """``/dev`` cannot be listed, and no disk can be opened.

    macOS has no private ``/dev``: the host's nodes exist, but only ``null``,
    ``zero``, ``random`` and ``urandom`` open.
    """
    assert DENIED in _io_probe(_list("/dev"))
    assert DENIED in _io_probe(_read("/dev/disk0"))
    assert _io_probe(_read("/dev/null")) == "OK "


@macos
def test_the_hosts_environment_does_not_reach_a_proof_on_macos(
    lean_project, monkeypatch
) -> None:
    """Without a shell a proof cannot list its environment; it asks by name."""
    monkeypatch.setenv("FORSETI_PROBE_SECRET", "hunter2")
    seen = _io_probe('do pure (toString (← IO.getEnv "FORSETI_PROBE_SECRET"))')
    assert seen == "OK none"


@macos
def test_a_proof_cannot_read_system_configuration_home_or_keychains(
    lean_project,
) -> None:
    """Paths outside the sandbox refuse access rather than being absent.

    Each exists and is readable by this process, so only the sandbox refuses.
    The keychains stand in for the Linux kernel keyring: their files are
    unreadable, and the Mach services that serve keys cannot be looked up
    (``test_lean_sandbox.py``). The Preboot volume under ``/System`` records
    the host's users; the rest of ``/System`` is readable, as system libraries.
    """
    home = Path.home()
    preboot = Path("/System/Volumes/Preboot")
    for path, action in (
        (Path("/etc/passwd"), _read),
        (home, _list),
        (Path("/Users"), _list),
        (Path("/private/var"), _list),
        (Path("/tmp"), _list),
        (Path(tempfile.gettempdir()), _list),
        (lean_project.parent, _list),
        (home / "Library" / "Keychains", _list),
        (preboot, _list),
    ):
        # Readable unconfined, so only the sandbox refuses.
        (path.read_bytes() if path.is_file() else list(path.iterdir()))
        seen = _io_probe(action(path))
        assert seen.startswith("ERR") and DENIED in seen, (path, seen)
    assert _io_probe(_list("/System/Library")).startswith("OK")


@macos
def test_a_proof_writes_only_its_own_directory(lean_project) -> None:
    """Writes outside the step's directory fail; there is no private ``/tmp``."""
    name = f"forseti-{uuid.uuid4().hex}"
    for target in (f"/tmp/{name}", Path.home() / name):
        seen = _io_probe(
            f'do IO.FS.writeFile {json.dumps(str(target))} "x"; pure "wrote"'
        )
        assert DENIED in seen
        assert not Path(target).exists()
    assert _io_probe('do IO.FS.writeFile "inside.txt" "x"; pure "wrote"') == (
        "OK wrote"
    )
