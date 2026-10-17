"""Bind the whole import closure of a replay's imports to reviewed bytes.

`verify_project` pins the toolchain, the dependency lock and a few core
sources, and says outright that it does not authenticate compiled objects. A
replay that imports library modules loads thousands of ``.olean`` files, any
of which could redefine what a statement means. This module closes that gap
for each pinned import set:

1. The fixed `ImportClosure.lean` program, content-pinned, lists the imports'
   transitive closure and the file each module resolves to. It reads module
   headers and data without running any module's code, and runs confined.
2. Every loaded artifact of every module (``.olean`` and, where present,
   ``.ir``, ``.olean.server`` and ``.olean.private``) is hashed, and for a
   package module so is its ``.lean`` source.
3. The hashes are folded into one digest per package (and one for the
   toolchain's own modules) and compared with that import set's entry in
   ``lean-replay.v1.json``, which `tools/pin_lean_replay.py` writes from a
   reviewed build. The toolchain's programs and libraries are hashed too.

Lean writes the same ``.olean`` bytes for the same sources and toolchain, so a
rebuild from the pinned release matches. A mismatch fails closed: the
environment is refused, never trusted. What remains trusted is the process
that reads these files between the hash and the load, which is the caller's
own machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import threading
import time
from dataclasses import dataclass, replace
from functools import partial
from importlib.resources import files
from pathlib import Path

from glitnir.lean.environment import MAX_OUTPUT_BYTES
from glitnir.lean.sandbox import sandbox_available
from glitnir.lean.scratch import scratch_directory

from glitnir_forseti.lean_release import (
    LOCKFILE_BYTES,
    RELEASE_SHA256,
    TOOLCHAIN,
    LeanReleaseError,
    package_roots,
    select_project,
    verify_project,
)

from .independent import (
    IndependentCheck,
    IndependentStatus,
    _Completed,
    _merge_shared_namespaces,
    _run,
    _stopped,
)
from .session import SessionLimits

CLOSURE_PROGRAM = Path(__file__).with_name("ImportClosure.lean")
#: The reviewed closure program's bytes; a changed file is refused.
CLOSURE_SHA256 = "60d07a94daf2298ae5e053a74349efc2fa8a1d826e2192277657f3489becb97d"
REPLAY_SCHEMA = "gimle.forseti.lean-replay/v2"
#: The artifacts Lean may load for a module, beside its ``.olean``.
ARTIFACT_SUFFIXES = (".olean", ".ir", ".olean.server", ".olean.private")
TOOLCHAIN_PACKAGE = "toolchain"


_SHARED = (
    "libInit_shared",
    "libLake_shared",
    "libleanshared",
    "libleanshared_1",
    "libleanshared_2",
)
#: The toolchain programs and shared libraries every replay step executes,
#: relative to the toolchain root: the elaborator, the kernel replay and the
#: libraries they link, by operating system. Their bytes differ per platform,
#: so the pin records one digest per platform. Digests are kept across release
#: bumps (`build_pin`), so changing this list or how it is hashed must also
#: change `REPLAY_SCHEMA`; otherwise a kept digest can never match again.
TOOLCHAIN_BINARIES = {
    system: ("bin/lean", "bin/leanchecker")
    + tuple(f"lib/lean/{name}{suffix}" for name in _SHARED)
    for system, suffix in (("Linux", ".so"), ("Darwin", ".dylib"))
}


@dataclass(frozen=True)
class ClosureObservation:
    """What one environment's closure of one import set hashes to.

    Attributes:
        imports: The imported modules, as the replay imports them.
        modules: How many modules the closure has.
        packages: Per package, in name order: its module count and digest.
        platform: The platform the toolchain binaries were hashed on.
        binaries: The digest of that platform's `TOOLCHAIN_BINARIES`.
    """

    imports: tuple[str, ...]
    modules: int
    packages: tuple[tuple[str, int, str], ...]
    platform: str = ""
    binaries: str = ""

    def as_entry(self) -> dict[str, object]:
        """Return this import set's entry in the pin."""
        return {
            "imports": list(self.imports),
            "modules": self.modules,
            "packages": [
                {"name": name, "modules": count, "digest": digest}
                for name, count, digest in self.packages
            ],
        }


def build_pin(
    observations: list[ClosureObservation], previous: dict[str, object] | None = None
) -> dict[str, object]:
    """Assemble the pin from one observation per import set.

    The pin names the toolchain its binary digests belong to. Digests pinned
    for other platforms are kept from ``previous`` when it names the same
    toolchain, whatever its release: the binaries change with the toolchain
    alone, and `verify_closure` re-observes every closure on the platform that
    replays, so a platform whose compiled library differs finds a mismatch
    there rather than a proof. A release bump on one platform therefore keeps
    the others. A pin from before the toolchain was named keeps them only for
    the same release.

    For the same release, every import set ``previous`` pins must be observed,
    byte-identically; an import set it does not pin is added.

    Raises:
        ValueError: If the observations disagree about the toolchain binaries,
            or a closure ``previous`` pins for the same release differs — the
            compiled library is not byte-identical across the platforms.
    """
    seen = {(o.platform, o.binaries) for o in observations}
    if len(seen) != 1:
        raise ValueError("the observations saw different toolchain binaries")
    ((platform, digest),) = seen
    closures = [o.as_entry() for o in observations]
    binaries: dict[str, str] = {}
    if previous is not None and previous.get("schema") == REPLAY_SCHEMA:
        same_release = (
            previous.get("release_sha256") == RELEASE_SHA256
            and previous.get("closure_program_sha256") == CLOSURE_SHA256
        )
        pinned = previous.get("closures")
        if same_release and type(pinned) is list:
            # Import sets pinned already must be byte-identical here; one this
            # platform adds is new, and replay observes it wherever it runs.
            known = {
                tuple(entry.get("imports", ())): entry
                for entry in pinned
                if type(entry) is dict
            }
            observed = {observation.imports for observation in observations}
            if not set(known) <= observed:
                raise ValueError(
                    "this run does not observe every import set pinned for this "
                    "release; pin them all, or none is dropped"
                )
            for observation, entry in zip(observations, closures):
                imports = observation.imports
                if imports in known and known[imports] != entry:
                    raise ValueError(
                        "the observed closures differ from the pinned ones for "
                        "this release: the compiled library is not "
                        "byte-identical here"
                    )
        named = previous.get("toolchain")
        kept = previous.get("toolchain_binaries")
        if type(kept) is dict and (
            named == TOOLCHAIN or (named is None and same_release)
        ):
            binaries.update({k: v for k, v in kept.items() if type(v) is str})
    binaries[platform] = digest
    return {
        "schema": REPLAY_SCHEMA,
        "release_sha256": RELEASE_SHA256,
        "closure_program_sha256": CLOSURE_SHA256,
        "toolchain": TOOLCHAIN,
        "toolchain_binaries": dict(sorted(binaries.items())),
        "closures": closures,
    }


def closure_identity(imports: tuple[str, ...]) -> str:
    """Digest what the pin says about one import set, for artifact metadata.

    It covers the release pin, the closure program and that import set's
    entry, so pinning another import set does not change it. The toolchain
    binaries are verified on every replay but left out, so an artifact made on
    one platform replays on another that the pin also covers.

    Raises:
        KeyError: If the pin has no entry for ``imports``.
    """
    pin = replay_pin()
    entry = _entry(pin, imports)
    if entry is None:
        raise KeyError(f"no replay pin for {imports}")
    document = {
        "release_sha256": pin.get("release_sha256"),
        "closure_program_sha256": pin.get("closure_program_sha256"),
        "closure": entry,
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def _entry(pin: dict[str, object], imports: tuple[str, ...]) -> dict | None:
    """Find the pin's entry for an import set, or None."""
    closures = pin.get("closures")
    if type(closures) is not list:
        return None
    for entry in closures:
        if type(entry) is dict and entry.get("imports") == list(imports):
            return entry
    return None


def replay_pin() -> dict[str, object]:
    """Read the packaged replay pin."""
    pin: dict[str, object] = json.loads(
        files("glitnir_forseti").joinpath("lean-replay.v1.json").read_bytes()
    )
    return pin


def toolchain_root() -> Path:
    """Locate the pinned toolchain in the elan home, as ``elan run`` does."""
    elan_home = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
    directory = TOOLCHAIN.replace("/", "--").replace(":", "---")
    return (elan_home / "toolchains" / directory).resolve()


def current_platform() -> str:
    """Name the platform the toolchain binaries are built for."""
    return f"{platform.system()}-{platform.machine()}"


def observe_closure(
    imports: tuple[str, ...],
    limits: SessionLimits,
    project: Path | None = None,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> ClosureObservation | IndependentCheck:
    """Hash the closure of ``imports`` in a verified project.

    Returns:
        The observation, or an ``unknown`` check saying why none was made.
    """
    selected = select_project(project)
    elan = shutil.which("elan")
    if selected is None or elan is None:
        return _unknown("Lean unavailable")
    try:
        verify_project(selected)
    except LeanReleaseError as error:
        return _unknown(f"Lean environment mismatch: {error}")
    if not sandbox_available():
        return _unknown("no sandbox to confine Lean")
    program = CLOSURE_PROGRAM.read_bytes()
    if hashlib.sha256(program).hexdigest() != CLOSURE_SHA256:
        return _unknown("the closure program was modified")
    try:
        with scratch_directory("glitnir-forseti-closure-") as scratch:
            return _observe(
                imports,
                limits,
                selected,
                elan,
                scratch,
                program,
                deadline,
                cancel,
            )
    except (OSError, LeanReleaseError) as error:
        # A file that vanished or cannot be read: the environment is unusable,
        # which says nothing about any proof.
        return _unknown(f"Lean environment unreadable: {error}")


def _observe(
    imports: tuple[str, ...],
    limits: SessionLimits,
    project: Path,
    elan: str,
    root: Path,
    program: bytes,
    deadline: float | None,
    cancel: threading.Event | None,
) -> ClosureObservation | IndependentCheck:
    """List the closure confined, then hash what it names."""
    shared = _merge_shared_namespaces(project, root / "shared")
    (root / "program").mkdir()
    copy = root / "program" / CLOSURE_PROGRAM.name
    copy.write_bytes(program)
    # The listing is one line per module, thousands of them, from the fixed
    # program; it gets the output ceiling rather than the caller's limit, which
    # bounds what an untrusted proof may print.
    listing_limits = replace(limits, max_output_bytes=MAX_OUTPUT_BYTES)
    result = _run(
        ["lean", "--run", str(copy), *imports],
        limits=listing_limits,
        project=project,
        elan=elan,
        shared=shared,
        report=copy,
        deadline=deadline,
        cancel=cancel,
    )
    if not isinstance(result, _Completed):
        return _stopped("the closure listing did not complete", result)
    if result.returncode != 0:
        return _unknown("the closure listing failed: " + result.stderr.strip()[-300:])
    roots = package_roots(project, json.loads(LOCKFILE_BYTES))
    return digest_closure(
        imports,
        result.stdout,
        roots,
        toolchain_root(),
        deadline=deadline,
        cancel=cancel,
    )


def digest_closure(
    imports: tuple[str, ...],
    listing: str,
    roots: dict[str, Path],
    toolchain: Path,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> ClosureObservation | IndependentCheck:
    """Fold a closure listing into per-package digests of the files it loads.

    Each module must exist in exactly one library, so the file hashed here is
    the only one any search order could load; a copy of the module anywhere
    else in the search roots is refused rather than resolved.

    Args:
        imports: The imported modules.
        listing: ``name<TAB>olean path`` lines from `ImportClosure.lean`.
        roots: Package roots from the pinned lockfile.
        toolchain: The pinned toolchain's root directory.
        deadline: A ``time.monotonic()`` instant to stop hashing by.
        cancel: Set to stop hashing.
    """
    libraries = {
        name: (root / ".lake/build/lib/lean").resolve() for name, root in roots.items()
    }
    libraries[TOOLCHAIN_PACKAGE] = toolchain / "lib/lean"
    lines: dict[str, list[str]] = {name: [] for name in libraries}
    count = 0
    for line in listing.splitlines():
        if cancel is not None and cancel.is_set():
            return IndependentCheck(
                IndependentStatus.UNKNOWN,
                reason="hashing was cancelled",
                stop="cancelled",
            )
        if deadline is not None and time.monotonic() >= deadline:
            return IndependentCheck(
                IndependentStatus.UNKNOWN,
                reason="hashing ran out of time",
                stop="timeout",
            )
        name, separator, location = line.partition("\t")
        if not separator or not name:
            return _unknown(f"the closure listing was malformed: {line[:120]!r}")
        olean = Path(location).resolve()
        owner = next(
            (p for p, library in libraries.items() if olean.is_relative_to(library)),
            None,
        )
        if owner is None:
            return _unknown(f"{name} resolves outside the pinned packages")
        library = libraries[owner]
        stem = olean.relative_to(library).as_posix().removesuffix(".olean")
        if stem.replace("/", ".") != name:
            return _unknown(f"{name} resolves to a file of another module")
        copies = [
            p for p, other in libraries.items() if (other / f"{stem}.olean").exists()
        ]
        if copies != [owner]:
            return _unknown(f"{name} exists in more than one package: {copies}")
        entry = [name]
        for suffix in ARTIFACT_SUFFIXES:
            artifact = library / (stem + suffix)
            if artifact.is_file():
                entry.append(f"{suffix}={_sha256(artifact)}")
        if owner != TOOLCHAIN_PACKAGE:
            source = roots[owner].resolve() / (stem + ".lean")
            if not source.is_file():
                return _unknown(f"{name} has no source in its package")
            entry.append(f".lean={_sha256(source)}")
        lines[owner].append(" ".join(entry))
        count += 1
    if count == 0:
        return _unknown("the closure listing was empty")
    packages = tuple(
        (
            name,
            len(entries),
            hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest(),
        )
        for name, entries in sorted(lines.items())
        if entries
    )
    return ClosureObservation(
        imports, count, packages, current_platform(), _binaries(toolchain)
    )


def _binaries(toolchain: Path) -> str:
    """Digest the toolchain programs and libraries replay executes."""
    entries = []
    for relative in TOOLCHAIN_BINARIES.get(platform.system(), ()):
        path = toolchain / relative
        entries.append(f"{relative}={_sha256(path) if path.is_file() else 'absent'}")
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def verify_closure(
    imports: tuple[str, ...],
    limits: SessionLimits,
    project: Path | None = None,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> IndependentCheck:
    """Check the environment's closure of ``imports`` against the pin.

    Returns:
        ``confirmed`` when every package digest and the toolchain binaries
        match, otherwise ``unknown`` naming the first difference: a mismatched
        environment is unavailable, not evidence about any proof.
    """
    pin = replay_pin()
    if (
        pin.get("schema") != REPLAY_SCHEMA
        or pin.get("release_sha256") != RELEASE_SHA256
    ):
        return _unknown("the replay pin does not belong to the packaged release pin")
    if pin.get("closure_program_sha256") != CLOSURE_SHA256:
        return _unknown("the replay pin was made with another closure program")
    # The release pin already fixes the toolchain; this guards a pin edited
    # by hand, whose binary digests would then belong to another toolchain.
    if pin.get("toolchain") != TOOLCHAIN:
        return _unknown("the replay pin was made for another Lean toolchain")
    entry = _entry(pin, imports)
    if entry is None:
        return _unknown(f"the replay pin has no closure for {list(imports)}")
    binaries = pin.get("toolchain_binaries")
    if type(binaries) is not dict or type(binaries.get(current_platform())) is not str:
        return _unknown(
            f"Lean environment mismatch: the replay pin has no toolchain binaries "
            f"for {current_platform()}"
        )
    observed = observe_closure(
        imports, limits, project, deadline=deadline, cancel=cancel
    )
    if isinstance(observed, IndependentCheck):
        return observed
    if observed.binaries != binaries[current_platform()]:
        return _unknown("Lean environment mismatch: the toolchain binaries differ")
    if observed.as_entry() == entry:
        return IndependentCheck(IndependentStatus.CONFIRMED)
    found = {name: (count, digest) for name, count, digest in observed.packages}
    for package in entry["packages"]:
        if found.get(package["name"]) != (package["modules"], package["digest"]):
            return _unknown(
                f"Lean environment mismatch: {package['name']} differs from the "
                "replay pin"
            )
    return _unknown("Lean environment mismatch: the import closure differs")


def _unknown(reason: str) -> IndependentCheck:
    """Return an ``unknown`` check: the environment could not be confirmed."""
    return IndependentCheck(IndependentStatus.UNKNOWN, reason=reason)


def _sha256(path: Path) -> str:
    """Hash a file in blocks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
