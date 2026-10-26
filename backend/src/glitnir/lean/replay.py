"""What `ProjectReplay.lean` reported, and an import closure's cache identity."""

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from glitnir.lean import replay_cache
from glitnir.lean.project_snapshot import ProjectSnapshot, SnapshotError, module_path


def module_files(
    snapshot: ProjectSnapshot, isolated: Path, name: str, loaded: str
) -> dict[str, str] | None:
    """Return the snapshot digests of the files a loaded module was read from.

    The module must have been read from its own path under the isolated
    dependency tree (links to snapshot artifacts) or the snapshot toolchain,
    and each of its files must be exactly the snapshot's entry, or absent from
    both. Each entry must link to the stored file named by its recorded digest,
    whose bytes never change. None means some file is unknown.
    """
    try:
        relative = str(module_path(name))
    except SnapshotError:
        return None
    for root, prefix, linked in (
        (isolated, "artifacts", True),
        (snapshot.toolchain / "lib/lean", "toolchain/lib/lean", False),
    ):
        if Path(loaded) == root / (relative + ".olean"):
            break
    else:
        return None
    files = {}
    for suffix in replay_cache.MODULE_SUFFIXES:
        location = root / (relative + suffix)
        digest = snapshot.digests.get(f"{prefix}/{relative}{suffix}")
        try:
            info = location.lstat()
        except FileNotFoundError:
            if digest is not None:
                return None
            files[suffix] = "absent"
            continue
        except OSError:
            return None
        if digest is None:
            return None
        if linked:
            target = snapshot.artifacts / (relative + suffix)
            if not stat.S_ISLNK(info.st_mode) or os.readlink(location) != str(target):
                return None
            location = target
        stored = snapshot.stored(f"{prefix}/{relative}{suffix}", digest)
        try:
            if stored is None or os.readlink(location) != str(stored):
                return None
        except OSError:
            return None
        files[suffix] = digest
    return files


def replay_identity(
    snapshot: ProjectSnapshot,
    isolated: Path,
    program: Path,
    imports: Any,
    closure: list[list[str]],
) -> tuple[str, str, list[list[str]]] | None:
    """Key, context and digested modules of these imports; None if a file is unknown."""
    toolchain = {
        path: digest
        for path, digest in snapshot.digests.items()
        if path.startswith("toolchain/")
    }
    if not toolchain:
        return None
    modules = []
    for name, loaded in closure:
        files = module_files(snapshot, isolated, name, loaded)
        if files is None:
            return None
        modules.append([name, replay_cache.module_digest(files)])
    context = replay_cache.context_digest(
        toolchain=toolchain,
        program_sha256=hashlib.sha256(program.read_bytes()).hexdigest(),
    )
    key = replay_cache.import_key(context=context, imports=imports, modules=modules)
    return key, context, modules


def _axiom_entry(value: Any) -> bool:
    """Whether ``value`` is one axiom as both helpers write it: name and module."""
    return (
        isinstance(value, dict)
        and set(value) == {"name", "module"}
        and isinstance(value["name"], str)
        and isinstance(value["module"], (str, type(None)))
    )


def replay_report(
    output: str, closure: list[list[str]], imports: Any
) -> dict[str, Any]:
    """Validate the kernel replay's report and that it loaded the predicted closure."""
    try:
        value = json.loads(output)
    except ValueError:
        value = None
    if (
        not isinstance(value, dict)
        or value.get("replay")
        not in ("full", "imports only", "imports incremental", "imports cached")
        or type(value.get("imports_replayed")) is not bool
        or type(value.get("extendable")) is not bool
        or not (
            value.get("base_modules") is None
            or (
                type(value["base_modules"]) is int
                and 0 <= value["base_modules"] <= len(closure)
            )
        )
        or (value["replay"] == "imports incremental")
        != (value.get("base_modules") is not None)
        or value.get("fresh") not in (None, "restated", "quotient")
        or not isinstance(value.get("imports"), list)
        or not isinstance(value.get("modules"), list)
        or not isinstance(value.get("theorems"), list)
        or not all(
            isinstance(item, dict)
            and set(item) == {"name", "axioms"}
            and isinstance(item["name"], str)
            and isinstance(item["axioms"], list)
            and all(_axiom_entry(axiom) for axiom in item["axioms"])
            for item in value["theorems"]
        )
        or len({item["name"] for item in value["theorems"]}) != len(value["theorems"])
        or (
            value["replay"] in ("imports only", "imports incremental")
            and value["theorems"]
        )
    ):
        raise SnapshotError("The independent kernel replay report is malformed.")
    if value["imports"] != imports or value["modules"] != closure:
        raise SnapshotError(
            "Independent kernel replay loaded a different import closure."
        )
    return value


def replayed_axioms(
    replayed: dict[str, Any], reported: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Return each declared theorem's axioms as the kernel replay collected them.

    The replay collects them in the environment where it kernel-checked the
    module, which holds no imported extension state; they alone decide a
    theorem's status. The module report collects them again over Lean's own
    import. Both must list the same theorems with equal axioms, in the same
    order and with the same modules, or the check fails: a difference means one
    of the two read something the other did not.
    """
    found = {item["name"]: item["axioms"] for item in replayed["theorems"]}
    if set(found) != set(reported):
        raise SnapshotError(
            "The independent kernel replay and the module report list different "
            "theorems."
        )
    for name, theorem in reported.items():
        if theorem["axioms"] != found[name]:
            raise SnapshotError(
                "The independent kernel replay and the module report disagree on "
                f"the axioms of {name}."
            )
    return found


def toolchain_provenance(snapshot: ProjectSnapshot, closure: list[list[str]]) -> None:
    """Require every module the toolchain provides to be loaded from the toolchain."""
    for name, loaded in closure:
        try:
            relative = str(module_path(name))
        except SnapshotError:
            continue  # Not a name the toolchain uses.
        trusted = snapshot.toolchain / "lib/lean" / (relative + ".olean")
        if os.path.lexists(trusted) and Path(loaded) != trusted:
            raise SnapshotError(
                f"The toolchain module {name} was loaded from outside the toolchain."
            )
