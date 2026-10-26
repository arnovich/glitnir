"""Confine untrusted Lean processes to a read-only filesystem without network.

A Lean proof is a program. Tactics, commands and macros in checked source run
at elaboration time with the rights of the process, and can read and write any
file the user can. Without confinement a proof can rewrite the files later
checking steps trust — the axiom report program, a reference ``.olean``, the
toolchain itself — so no check that runs after it is sound.

On Linux every Lean process Glitnir starts therefore runs under bubblewrap:

- only the system's programs and libraries (`SYSTEM_PATHS`), the Lean
  toolchain and the paths a caller names are visible, all read-only; the rest
  of the filesystem, including ``/etc``, ``/home``, ``/var`` and ``/sys``, is
  not there at all;
- a private ``/tmp``, and write access only to the directories the caller names;
- new user, mount, PID, IPC, UTS, cgroup and network namespaces, a new session,
  and death with the parent;
- a seccomp filter denying the kernel keyring calls (`keyctl`, `add_key`,
  `request_key`). Namespaces do not isolate the session keyring, so without it
  a proof could read the host's keys, leave keys for a later step, and have the
  kernel run ``request-key`` helpers on the host, outside every namespace.

On macOS, which has no bubblewrap, the same command runs under Seatbelt
(``sandbox-exec``) with a generated profile that denies everything it does not
name (`seatbelt_profile`):

- reads of the system libraries (`DARWIN_SYSTEM_PATHS`, less the host volumes
  under ``/System/Volumes``), the toolchain and the named paths, of the
  metadata of their ancestors, and of the listing of ``/``; the contents of the
  rest of the filesystem, including ``/etc``, ``/Users`` and ``/tmp``, refuse
  access;
- writes only to the directories the caller names, which are never executable
  and whose files cannot be mapped as code; no network, Mach services, POSIX
  shared memory, device properties, process information or signals to other
  processes;
- no new process at all: ``fork`` and ``posix_spawn`` are denied, so nothing a
  proof starts can outlive its step. A supervisor outside the sandbox
  (``_supervisor.py``) kills the process when Glitnir exits, since macOS has no
  parent-death signal, and when Glitnir stops it (`environment.kill_and_reap`),
  since the process can leave its process group.

Where no working sandbox exists, starting Lean fails. Setting
``GLITNIR_UNSANDBOXED=1`` lets Lean run unconfined, for exploration on systems
with neither sandbox; checks that grant acceptance still refuse to run then.
"""

import functools
import os
import platform
import shutil
import struct
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from glitnir.lean.scratch import persistent_scratch_directory

#: Set to ``1`` to run Lean without a sandbox when none is available.
UNSANDBOXED_VARIABLE = "GLITNIR_UNSANDBOXED"
#: What a confined process sees of the system: programs, libraries and the
#: dynamic loader's configuration. A symbolic link among them, as on a
#: merged-``/usr`` system, is recreated as the same link.
SYSTEM_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/nix/store",
)

#: What a process confined by Seatbelt on macOS may read and execute from the
#: system: programs, libraries, the dynamic loader's shared cache (under
#: ``/System/Volumes/Preboot/Cryptexes`` since macOS 13, ``/private/var/db/dyld``
#: before) and the TLS configuration ``elan`` loads when it starts.
DARWIN_SYSTEM_PATHS = (
    "/bin",
    "/usr/bin",
    "/usr/lib",
    "/System",
    "/private/var/db/dyld",
    "/private/etc/ssl",
)
#: Host data under `DARWIN_SYSTEM_PATHS` that stays hidden: the other volumes,
#: whose Preboot volume records the host's users. The loader's cryptexes on it
#: are exposed again.
_DARWIN_HIDDEN = ("/System/Volumes",)
_DARWIN_UNHIDDEN = ("/System/Volumes/Preboot/Cryptexes",)
#: Devices a Seatbelt-confined process may read; it may write only the first.
_DARWIN_DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
_SEATBELT = "/usr/bin/sandbox-exec"
_SUPERVISOR = Path(__file__).with_name("_supervisor.py")

#: The file descriptor the seccomp filter is handed to bubblewrap on.
_FILTER_FD = 9
#: ``AUDIT_ARCH_*`` values and the keyring system calls on that architecture:
#: ``add_key``, ``request_key`` and ``keyctl``. On x86-64 the x32 ABI's
#: numbers differ only in bit 30, which the filter masks off.
_KEYRING_CALLS = (
    (0xC000003E, (248, 249, 250), 0xBFFFFFFF),  # x86-64 and x32
    (0x40000003, (286, 287, 288), 0xFFFFFFFF),  # i386
    (0xC00000B7, (217, 218, 219), 0xFFFFFFFF),  # aarch64
)
_ALLOW = 0x7FFF0000
_DENY = 0x00050000 | 1  # SECCOMP_RET_ERRNO with EPERM
_KILL = 0x80000000  # SECCOMP_RET_KILL_PROCESS, for any other architecture


class SandboxUnavailable(RuntimeError):
    """Raised when Lean must be confined and no sandbox works here."""


@functools.cache
def _bubblewrap() -> str | None:
    """Return a working ``bwrap`` executable, probing it once per process."""
    executable = shutil.which("bwrap")
    if executable is None:
        return None
    try:
        probe = subprocess.run(
            [
                "/bin/sh",
                "-c",
                f'exec "$@" {_FILTER_FD}<"$0"',
                str(_filter_file()),
                executable,
                "--seccomp",
                str(_FILTER_FD),
                *_system_binds(),
                "--unshare-all",
                "--",
                "true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return executable if probe.returncode == 0 else None


def _system_binds() -> list[str]:
    """Return the bubblewrap arguments exposing `SYSTEM_PATHS` read-only."""
    binds: list[str] = []
    for name in SYSTEM_PATHS:
        path = Path(name)
        if path.is_symlink():
            binds += ["--symlink", os.readlink(path), name]
        elif path.exists():
            binds += ["--ro-bind", name, name]
    return binds


def _keyring_filter() -> bytes:
    """Return the classic-BPF seccomp program denying the keyring calls.

    Each architecture's block loads the system call number and returns
    ``EPERM`` for a keyring call and allow for anything else. A process of an
    architecture the filter does not know is killed, so a proof cannot reach
    the calls through a foreign binary it writes and runs.
    """
    program: list[tuple[int, int, int, int]] = [(0x20, 0, 0, 4)]  # load arch
    for arch, calls, mask in _KEYRING_CALLS:
        body: list[tuple[int, int, int, int]] = [(0x20, 0, 0, 0)]  # load nr
        body.append((0x54, 0, 0, mask))  # nr &= mask
        for index, call in enumerate(calls):
            body.append((0x15, len(calls) - index, 0, call))  # jeq → deny
        body += [(0x06, 0, 0, _ALLOW), (0x06, 0, 0, _DENY)]
        program.append((0x15, 0, len(body), arch))  # jeq arch, else skip
        program += body
    program.append((0x06, 0, 0, _KILL))
    return b"".join(struct.pack("<HBBI", *op) for op in program)


@functools.cache
def _filter_file() -> Path:
    """Write the seccomp program once per process, readable only by its owner."""
    directory = persistent_scratch_directory("glitnir-seccomp-")
    path = directory / "keyring.bpf"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_keyring_filter())
    return path


def _visible(executable: str, roots: Iterable[Path]) -> bool:
    """Whether the program a command starts lies under a path the sandbox binds."""
    program = Path(executable)
    if not program.is_absolute():
        return True  # looked up on PATH inside the sandbox
    candidates = {program, program.resolve()}
    for root in roots:
        for base in {root, root.resolve()}:
            if any(c == base or base in c.parents for c in candidates):
                return True
    return False


@functools.cache
def _seatbelt() -> str | None:
    """Return ``sandbox-exec`` on macOS if it confines, probing it once.

    The probe checks that a confined program runs and that the two denials the
    other guarantees rest on are enforced: opening ``/etc/hosts``, which the
    profile does not name, and starting a process.
    """
    if platform.system() != "Darwin" or not Path(_SEATBELT).exists():
        return None
    profile = seatbelt_profile((), ())

    def status(*command: str) -> int:
        return subprocess.run(
            [_SEATBELT, "-p", profile, *command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode

    try:
        runs = status("/usr/bin/true") == 0
        reads = status("/bin/sh", "-c", "exec 3</etc/hosts") == 0
        # A lone command would be exec'd in place; the second forces a fork.
        forks = status("/bin/sh", "-c", "/usr/bin/true; true") == 0
    except (OSError, subprocess.SubprocessError):
        return None
    return _SEATBELT if runs and not reads and not forks else None


def _quoted(path: Path | str) -> str:
    """Return a path as a Seatbelt string literal."""
    text = str(path)
    if any(ord(c) < 0x20 for c in text):
        raise SandboxUnavailable(f"cannot name {text!r} in a sandbox profile")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _both(paths: Iterable[Path | str]) -> list[Path]:
    """Return each existing path as given and through its symbolic links.

    Seatbelt matches the path the kernel resolved, and a lookup through a link
    such as ``/tmp`` reads the link itself first, so both forms are named.
    """
    found = [Path(p) for p in paths if Path(p).exists()]
    return list(dict.fromkeys(q for p in found for q in (p, p.resolve())))


def seatbelt_profile(readable: Iterable[Path], writable: Iterable[Path]) -> str:
    """Return the Seatbelt profile confining one Lean process on macOS.

    Everything is denied unless named: reading and executing the system paths
    and ``readable``, reading and writing ``writable``, the metadata of every
    ancestor directory so paths resolve, a few devices and the ``hw`` sysctls
    the runtime sizes itself by. Creating a process is never allowed, nor
    mapping code from ``writable``, nor reading device properties such as the
    serial number, nor anything under ``/System/Volumes`` but the loader's
    cryptexes.

    Args:
        readable: Paths to expose read-only, beyond `DARWIN_SYSTEM_PATHS`.
        writable: Directories the process may write.

    Returns:
        The profile, for ``sandbox-exec -p``.

    Raises:
        SandboxUnavailable: If a writable path lies within an executable one or
            contains one, which would let the process run what it wrote.
    """
    executable = _both([*DARWIN_SYSTEM_PATHS, *readable])
    writable = _both(writable)
    for written in writable:
        for program in executable:
            if written.is_relative_to(program) or program.is_relative_to(written):
                raise SandboxUnavailable(
                    f"{written} would be both writable and executable"
                )
    ancestors = dict.fromkeys(
        parent for path in [*executable, *writable] for parent in path.parents
    )
    ancestors.update(dict.fromkeys(_both(("/tmp", "/var", "/etc"))))

    def rule(
        action: str, kind: str, paths: Iterable[Path | str], verb: str = "allow"
    ) -> str:
        names = " ".join(f"({kind} {_quoted(p)})" for p in paths)
        return f"({verb} {action} {names})" if names else ""

    return "\n".join(
        line
        for line in (
            "(version 1)",
            "(deny default)",
            rule("process-exec", "subpath", executable),
            rule("file-read*", "subpath", [*executable, *writable]),
            rule("file-read*", "literal", ["/", *_DARWIN_DEVICES]),
            rule("file-read-metadata", "literal", ancestors),
            rule("file-write*", "subpath", writable),
            rule("file-write*", "literal", _DARWIN_DEVICES[:1]),
            '(allow sysctl-read (sysctl-name-prefix "hw."))',
            # Later rules take precedence over earlier ones.
            rule("file-read* process-exec", "subpath", _DARWIN_HIDDEN, "deny"),
            rule("file-read* process-exec", "subpath", _DARWIN_UNHIDDEN),
            rule("file-map-executable", "subpath", writable, "deny"),
            "(deny iokit-get-properties)",
        )
        if line
    )


def sandbox_available() -> bool:
    """Whether untrusted Lean can be confined on this system."""
    return _bubblewrap() is not None or _seatbelt() is not None


def _unsandboxed_allowed() -> bool:
    """Whether the caller opted into running Lean unconfined."""
    return os.environ.get(UNSANDBOXED_VARIABLE) == "1"


def lean_readable(project: Path, *extra: Path) -> tuple[Path, ...]:
    """Return the paths a confined Lean process needs to read.

    Args:
        project: The Lean project; its packages live beneath it.
        extra: Further paths, such as a REPL executable's directory.

    Returns:
        The Lean toolchain home, the directory of the ``elan`` on ``PATH`` (which
        every adapter runs), the project and the extras, each also resolved
        through symbolic links.
    """
    elan_home = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
    elan = shutil.which("elan")
    launcher = [Path(elan).parent, Path(elan).resolve().parent] if elan else []
    paths = [elan_home, *launcher, project, *extra]
    return tuple(dict.fromkeys(p for path in paths for p in (path, path.resolve())))


def confine(
    command: Sequence[str],
    *,
    readable: Iterable[Path],
    writable: Iterable[Path] = (),
    cwd: Path,
    required: bool = False,
) -> list[str]:
    """Wrap a command so it runs confined.

    Args:
        command: The program and its arguments.
        readable: Paths to expose read-only, beyond `SYSTEM_PATHS` (on macOS
            `DARWIN_SYSTEM_PATHS`, and also executable there).
        writable: Directories the process may write.
        cwd: The working directory inside the sandbox; under Seatbelt the
            caller's own ``Popen(cwd=...)`` applies instead.
        required: Refuse the legacy unconfined opt-out when true.

    Returns:
        The wrapped command, or the command itself when no sandbox exists and
        the caller opted out with ``GLITNIR_UNSANDBOXED=1``.

    Raises:
        SandboxUnavailable: If no sandbox works and the caller did not opt out.
    """
    readable, writable = list(readable), list(writable)
    bwrap = _bubblewrap()
    if bwrap is None and _seatbelt() is not None:
        return _seatbelt_command(command, readable, writable)
    if bwrap is None:
        if not required and _unsandboxed_allowed():
            return list(command)
        if required:
            raise SandboxUnavailable("project inspection requires a working sandbox")
        raise SandboxUnavailable(
            "no working sandbox; install bubblewrap (or use macOS), or set "
            f"{UNSANDBOXED_VARIABLE}=1 to run Lean unconfined without acceptance"
        )
    system = [Path(p) for p in SYSTEM_PATHS if Path(p).exists()]
    if command and not _visible(command[0], [*system, *readable, *writable]):
        raise SandboxUnavailable(
            f"{command[0]} is outside every path the sandbox exposes; "
            "install it under the system paths or the Lean toolchain"
        )
    if platform.machine().lower() not in ("x86_64", "amd64", "aarch64", "arm64"):
        raise SandboxUnavailable(
            f"no seccomp filter for {platform.machine()}; the keyring calls "
            "cannot be denied here"
        )
    wrapped = [
        "/bin/sh",
        "-c",
        f'exec "$@" {_FILTER_FD}<"$0"',
        str(_filter_file()),
        bwrap,
        "--seccomp",
        str(_FILTER_FD),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        *_system_binds(),
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        str(Path.home()),
    ]
    for path in readable:
        if path.exists():
            wrapped += ["--ro-bind", str(path), str(path)]
    for path in writable:
        wrapped += ["--bind", str(path), str(path)]
    return [*wrapped, "--chdir", str(cwd), "--", *command]


def _seatbelt_command(
    command: Sequence[str], readable: list[Path], writable: list[Path]
) -> list[str]:
    """Wrap a command in ``sandbox-exec`` under the supervisor (macOS).

    The working directory is inherited from the caller, as ``sandbox-exec``
    does not set one.
    """
    executable = [Path(p) for p in DARWIN_SYSTEM_PATHS] + readable
    if command and not _visible(command[0], executable):
        raise SandboxUnavailable(
            f"{command[0]} is outside every path the sandbox may execute; "
            "install it under the system paths or the Lean toolchain"
        )
    profile = seatbelt_profile(readable, writable)
    return [
        sys.executable,
        "-I",
        "-B",
        "-S",
        str(_SUPERVISOR),
        str(os.getpid()),
        _SEATBELT,
        "-p",
        profile,
        *command,
    ]
