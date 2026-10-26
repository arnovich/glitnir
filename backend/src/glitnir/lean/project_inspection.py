"""Inspect prepared Lake module setup explicitly; never grant proof status.

The version-specific adapter runs read-only, without network access. Returned
setup is a historical description, not a transitive proof-environment identity.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any

from glitnir.lean.artifacts import artifact_files, valid_artifacts
from glitnir.lean.awake import awake
from glitnir.lean.environment import bind_to_parent, kill_and_reap
from glitnir.lean.limits import check_seconds
from glitnir.lean.prepared_packages import PROGRAM, validate_resolution
from glitnir.lean.project import describe_project, read_regular
from glitnir.lean.sandbox import SandboxUnavailable, confine

TOOLCHAIN = "leanprover/lean4:v4.35.0"
# `lake setup-file` lists every module in the import closure; for `import
# Mathlib` that is over 5 MB, so leave room for the library to grow.
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
#: Lake's exit status when a `--no-build` run finds a target out of date.
NO_BUILD_EXIT = 3
MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*")


#: The origin elan assumes for a toolchain named without one.
_ORIGIN = "leanprover/lean4:"
#: A release named with its leading `v`, such as `v4.35.0` or `v4.35.0-rc1`.
_RELEASE = re.compile(r"v(\d+\.\d+\.\d+\S*)")
#: What every refusal and the editor's banner say after the version sentence.
_OFF = (
    "Glitnir checks Lean {supported}, so checking, proof steps, views, evidence "
    "and decisions are off; reading works as usual."
)


def _version(declaration: str) -> str:
    """Name a declared toolchain as a Lean version, the way elan resolves it.

    The default origin is dropped, and the `v` of a release: elan picks the
    same toolchain for `leanprover/lean4:v4.35.0`, `v4.35.0` and `4.35.0`.
    """
    name = declaration.removeprefix(_ORIGIN)
    release = _RELEASE.fullmatch(name)
    return release.group(1) if release else name


def supported_version() -> str:
    """Return the Lean version Glitnir checks, such as `4.35.0`, from `TOOLCHAIN`."""
    return _version(TOOLCHAIN)


def lean_support(project: dict[str, Any]) -> dict[str, Any]:
    """Decide whether Glitnir runs Lean for a `describe_project` description.

    A Lake project is supported when its `lean-toolchain` declares the
    supported version; a loose file also when no `lean-toolchain` is found.
    An unreadable declaration is never supported, and the message names its
    read error. Only this decides it, so the editor's banner and every
    refusal say the same `message` (None when supported). `declared` is the
    declaration as read, or None.
    """
    declared = project["toolchain"]
    error = project["files"]["lean-toolchain"].get("error")
    if declared is not None:
        supported = _version(declared) == supported_version()
    else:
        supported = project["kind"] == "loose" and error == "missing"
    message = None
    if not supported:
        if declared is not None:
            first = f"This project uses Lean {_version(declared)}."
        elif error is not None and error != "missing":
            first = f"This project's lean-toolchain could not be read: {error}."
        else:
            first = "This project declares no Lean version."
        message = f"{first} {_OFF.format(supported=supported_version())}"
    return {
        "supported": supported,
        "declared": declared,
        "toolchain": TOOLCHAIN,
        "message": message,
    }


class NeedsBuild(ValueError):
    """Lake found an import that is not built, or is out of date; nothing was built."""


def _source_digest(source: Path) -> str:
    """Use the same normalized saved-source binding as the source preview."""
    text = read_regular(source, 16 * 1024 * 1024).decode("utf-8")
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()


def _toolchain_directory() -> str:
    """Name elan's directory for `TOOLCHAIN`, such as `leanprover--lean4---v4.35.0`."""
    return TOOLCHAIN.replace("/", "--").replace(":", "---")


def lake_matches(version: str) -> bool:
    """Tell whether `lake --version` output reports the supported Lean version.

    Lake's own version moves with every release; the Lean version it was
    built with is the binding fact.
    """
    return f"(Lean version {supported_version()})" in version


def _toolchain() -> Path:
    """Locate the supported installed toolchain without invoking elan."""
    home = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
    root = (home / "toolchains" / _toolchain_directory()).resolve()
    if not all((root / "bin" / name).is_file() for name in ("lake", "lean")):
        raise ValueError(
            f"Install {TOOLCHAIN} with elan before inspecting this project."
        )
    return root


def _run(
    arguments: list[str],
    root: Path,
    toolchain: Path,
    cancel: threading.Event,
    *,
    program: Path | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Run Lake with no writable roots and bounded lifetime/output files.

    The lifetime defaults to the configured check time limit: `lake setup-file`
    for a notebook importing all of Mathlib takes minutes, even when warm.
    """
    if cancel.is_set():
        raise ValueError("Project inspection cancelled.")
    executable = (
        [
            str(toolchain / "bin/lean"),
            "-M2048",
            "--plugin",
            str(
                toolchain
                / "lib/lean"
                / (
                    "libLake_shared.dylib"
                    if sys.platform == "darwin"
                    else "libLake_shared.so"
                )
            ),
            "--run",
            str(program),
        ]
        if program is not None
        else [str(toolchain / "bin/lake")]
    )
    command = confine(
        [*executable, *arguments],
        readable=[root, toolchain, *([program] if program is not None else [])],
        cwd=root,
        required=True,
    )
    # Never inherit Lean/Lake search-path, package-server, shell or loader overrides.
    environment = {
        "PATH": f"{toolchain / 'bin'}:/usr/bin:/bin",
        "LEAN_SYSROOT": str(toolchain),
        "LEAN_PATH": "",
        "LAKE_HOME": str(toolchain),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
    }
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, awake():
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            preexec_fn=partial(bind_to_parent, os.getpid(), MAX_OUTPUT_BYTES),
        )
        try:
            deadline = time.monotonic() + (
                check_seconds() if timeout_seconds is None else timeout_seconds
            )
            while process.poll() is None:
                if cancel.is_set():
                    raise ValueError("Project inspection cancelled.")
                if time.monotonic() >= deadline:
                    raise ValueError("Project inspection timed out.")
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            texts = []
            for stream in (out, err):
                if os.fstat(stream.fileno()).st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("Project inspection exceeded its output limit.")
                stream.seek(0)
                texts.append(stream.read(MAX_OUTPUT_BYTES + 1).decode("utf-8"))
            if process.returncode != 0:
                failure = (
                    NeedsBuild
                    if program is not None and process.returncode == NO_BUILD_EXIT
                    else ValueError
                )
                raise failure(
                    "Lake could not inspect this prepared project. Build its "
                    "dependencies with its declared toolchain, then retry. "
                    "Configuration needing writes, subprocesses on macOS, or paths "
                    "outside the project is not supported by this inspection.\n"
                    + (texts[1] + texts[0])[-4000:]
                )
            return texts[0]
        finally:
            # Also reap descendant processes if the Lake parent exited first.
            kill_and_reap(process)


def _validate_setup(value: Any, root: Path, toolchain: Path) -> dict[str, Any]:
    """Validate the supported Lake schema and all returned artifact locations."""
    if not isinstance(value, dict):
        raise ValueError("Lake returned an invalid module setup.")
    required = {"name", "isModule", "importArts", "options", "plugins", "dynlibs"}
    if not required <= value.keys() or value.keys() - required - {"package"}:
        raise ValueError("Lake returned an unsupported module setup schema.")
    if not isinstance(value["name"], str) or not value["name"]:
        raise ValueError("Lake returned an invalid module name.")
    if type(value["isModule"]) is not bool or (
        "package" in value and not isinstance(value["package"], str)
    ):
        raise ValueError("Lake returned invalid module properties.")
    options, imports = value["options"], value["importArts"]
    if not isinstance(options, dict) or not all(
        isinstance(k, str) and (type(v) in (str, bool) or (type(v) is int and v >= 0))
        for k, v in options.items()
    ):
        raise ValueError("Lake returned invalid Lean options.")
    if not isinstance(imports, dict) or not all(
        isinstance(k, str) and valid_artifacts(v) for k, v in imports.items()
    ):
        raise ValueError("Lake returned invalid import artifacts.")

    def artifact(name: Any) -> None:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError("Lake returned an invalid artifact path.")
        path = (root / name).resolve()
        if not any(path.is_relative_to(base) for base in (root, toolchain)):
            raise ValueError("Lake returned an artifact outside the inspection roots.")
        if not path.is_file():
            raise ValueError("Lake artifact is missing or is not a regular file.")

    for paths in imports.values():
        for path, _ in artifact_files(paths):
            artifact(path)
    for field in ("plugins", "dynlibs"):
        if not isinstance(value[field], list):
            raise ValueError(f"Lake returned invalid {field}.")
        for entry in value[field]:
            if field == "plugins" and isinstance(entry, dict):
                if set(entry) != {"path", "initFn"} or not (
                    entry["initFn"] is None or isinstance(entry["initFn"], str)
                ):
                    raise ValueError("Lake returned an invalid plugin.")
                artifact(entry["path"])
            else:
                artifact(entry)
    return value


def inspect_project(
    source: Path,
    *,
    expected_sha256: str | None = None,
    cancel: threading.Event | None = None,
    timeout_seconds: float | None = None,
    imports: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resolve prepared module setup on explicit request, without proof metadata.

    `timeout_seconds` bounds the whole inspection, every Lake run together;
    by default it is the configured check time limit. A caller that answers a
    quick request passes a shorter one.
    `imports` are modules set up as if the source's header imported them too,
    from the project's existing build. A module no package provides is left
    out of the setup; a missing or out-of-date build of one, as of any import,
    makes the inspection unavailable, with `needs_build` set: nothing is built.
    """
    try:
        for module in imports:
            if not MODULE_NAME.fullmatch(module):
                raise ValueError(f"Invalid module name `{module}`.")
        source = source.absolute()
        digest = _source_digest(source)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("Saved source changed; refresh the preview and retry.")
        project = describe_project(source)
        if project["kind"] != "lake":
            raise ValueError(
                "Inspection needs a prepared Lake project; loose files still preview."
            )
        support = lean_support(project)
        if not support["supported"]:
            raise ValueError(support["message"])
        files = project["files"]
        for name, info in files.items():
            if "error" in info and not (
                name in ("lakefile.lean", "lakefile.toml")
                and info["error"] == "missing"
            ):
                raise ValueError(f"Prepare the project's {name}: {info['error']}.")
        root = Path(project["root"]).resolve()
        if not source.resolve().is_relative_to(root):
            raise ValueError("Source is outside the project root.")
        toolchain = _toolchain()
        cancellation = cancel if cancel is not None else threading.Event()
        deadline = time.monotonic() + (
            check_seconds() if timeout_seconds is None else timeout_seconds
        )

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ValueError("Project inspection timed out.")
            return left

        version = _run(
            ["--version"],
            root,
            toolchain,
            cancellation,
            timeout_seconds=remaining(),
        ).strip()
        if not lake_matches(version):
            raise ValueError(
                "Installed Lake does not match the supported "
                f"Lean {supported_version()} adapter."
            )
        output = _run(
            ["setup-file", str(source), *imports],
            root,
            toolchain,
            cancellation,
            program=PROGRAM,
            timeout_seconds=remaining(),
        )
        report = json.loads(output)
        if not isinstance(report, dict) or set(report) != {"setup", "resolution"}:
            raise ValueError("Invalid prepared project inspection report.")
        setup = _validate_setup(report["setup"], root, toolchain)
        resolution = validate_resolution(report["resolution"], root)
        if _source_digest(source) != digest or describe_project(source) != project:
            raise ValueError(
                "Source or project configuration changed; retry inspection."
            )
        return {
            "status": "resolved",
            "message": (
                f"Module setup resolved for {setup['name']}. "
                "Setup only; use Check saved file for independent observations. "
                "Prepared local dependency contents; "
                "Git revision and cleanliness are not verified."
            ),
            "source_sha256": digest,
            "project": project,
            "toolchain_root": str(toolchain),
            "lake_version": version,
            "setup": setup,
            "resolution": resolution,
        }
    except NeedsBuild as error:
        return {"status": "unavailable", "message": str(error), "needs_build": True}
    except (OSError, ValueError, RecursionError, SandboxUnavailable) as error:
        return {"status": "unavailable", "message": str(error)}
