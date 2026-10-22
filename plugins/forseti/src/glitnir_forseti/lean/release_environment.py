"""Pinned Lean process environment for the audited Forseti release.

Running Lean against the pinned release means the locked toolchain, the locked
package roots, and nothing inherited from the developer's shell.  This module
owns that construction so the candidate checker and the search session cannot
drift apart.  Project mode never imports it.

Nothing here decides anything.  It builds an argument list and an environment
for an untrusted process.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path

from glitnir_forseti.lean_release import (
    LOCKFILE_BYTES,
    SOURCE_PINS,
    TOOLCHAIN,
    package_roots,
)


def lean_process_environment(project: Path, elan: str) -> dict[str, str]:
    """Build the child environment for one pinned Lean process.

    The import roots come from the lockfile rather than from Lake evaluation,
    and no Lean override reaches the child from the calling shell.

    Args:
        project: The selected compatible Lean project root.
        elan: The absolute path of the ``elan`` executable.

    Returns:
        The exact environment mapping for the child process.
    """
    roots = package_roots(project, json.loads(LOCKFILE_BYTES))
    environment = {
        "PATH": os.pathsep.join((str(Path(elan).parent), "/usr/bin", "/bin")),
        "LEAN_PATH": os.pathsep.join(
            str(root / ".lake/build/lib/lean") for root in roots.values()
        ),
    }
    for name in ("HOME", "ELAN_HOME"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


@functools.cache
def lean_sysroot(elan: str) -> str:
    """Return the pinned toolchain's prefix, found outside every sandbox.

    Lean programs that load modules (``leanchecker``, the REPL) otherwise run
    ``lean --print-prefix`` to find it, and a confined process may not start
    another where the sandbox denies creating processes, as on macOS.

    Args:
        elan: The ``elan`` executable.

    Returns:
        The absolute toolchain prefix, for ``LEAN_SYSROOT``.

    Raises:
        OSError: If ``elan`` cannot be started.
        subprocess.SubprocessError: If it fails or does not answer in time.
    """
    return subprocess.check_output(
        [elan, "run", TOOLCHAIN, "lean", "--print-prefix"],
        text=True,
        timeout=10,
        stderr=subprocess.PIPE,
        env={
            k: os.environ[k] for k in ("PATH", "HOME", "ELAN_HOME") if k in os.environ
        },
    ).strip()


def lean_module_setup(project: Path, name: str) -> str:
    """Build the module setup that disambiguates the shared Gimle namespace.

    Args:
        project: The selected compatible Lean project root.
        name: The module name of the source unit being elaborated.

    Returns:
        The canonical JSON setup document for ``lean --setup``.
    """
    roots = package_roots(project, json.loads(LOCKFILE_BYTES))
    artifacts: dict[str, list[list[str]]] = {}
    for package, relative, _ in SOURCE_PINS:
        if relative.startswith("Gimle/") and relative.endswith(".lean"):
            stem = relative.removesuffix(".lean")
            artifacts[stem.replace("/", ".")] = _artifacts(
                roots[package] / ".lake/build/lib/lean", stem
            )
    # Every other Gimle module built in the pinned packages, so a unit can
    # import any of them (for example Gimle.Asgard.Model.Continuous): Lean
    # would otherwise look for it only in the first package with a Gimle folder.
    for root in roots.values():
        library = root / ".lake/build/lib/lean"
        for olean in sorted((library / "Gimle").rglob("*.olean")):
            stem = olean.relative_to(library).with_suffix("").as_posix()
            artifacts.setdefault(stem.replace("/", "."), _artifacts(library, stem))
    return json.dumps(
        {
            "name": name,
            "isModule": False,
            "importArts": artifacts,
            "plugins": [],
            "dynlibs": [],
            "options": {},
        }
    )


#: The files of one nested ``importArts`` entry, by position:
#: ``[[olean, olean.server?, olean.private?], [ir.sig, ir?]]``.
_ARTIFACT_GROUPS = (("olean", "olean.server", "olean.private"), ("ir.sig", "ir"))


def _artifacts(library: Path, stem: str) -> list[list[str]]:
    """List a module's built files in Lean's nested ``importArts`` shape.

    Lean reads each file by its position, so a group holds the files present
    up to the first one that is missing, and the IR group follows only a data
    group: a module outside the module system has only ``[[olean]]``.
    """
    base = str(library / stem)
    groups: list[list[str]] = []
    for suffixes in _ARTIFACT_GROUPS:
        group: list[str] = []
        for suffix in suffixes:
            path = base + "." + suffix
            if not Path(path).is_file():
                break
            group.append(path)
        if not group:
            break
        groups.append(group)
    return groups
