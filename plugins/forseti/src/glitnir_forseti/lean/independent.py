"""Establish a checked declaration's axioms outside the process that built it.

Reading ``#print axioms`` from the elaborating process trusts every program the
checked source runs: a tactic can log a forged report, and a command can
redefine ``#print axioms``. A tactic can even replace the environment, so the
theorem that is saved states something about redefined constants.

This module checks a unit without believing anything that process printed:

1. **Reference.** The prelude plus ``axiom forsetiSearch <statement>`` is
   compiled to an ``.olean``. Only the trusted prelude runs here. An axiom,
   not a theorem, because later claims assume a claim as an axiom: where the
   two readings differ (a theorem also takes ``include``d section variables
   and instance variables its type does not mention), the candidate is
   refused, so a confirmed statement means the same in both.
2. **Candidate.** The prelude plus the theorem and its proof is compiled to an
   ``.olean``, in a separate process and directory, under the same module name.
3. **Kernel replay.** ``leanchecker`` re-checks every declaration of the
   candidate module through the kernel.
4. **Comparison.** The fixed `AxiomReport.lean` program, shipped with this
   package, reads each ``.olean`` in its own process without running the
   module's code. Every other constant of the reference must appear in the
   candidate with an equal canonical entry, ``forsetiSearch`` must be the
   theorem the reference states as an axiom, and the axioms are collected
   from the candidate's module data. An entry keeps everything that fixes
   what a declaration means to the kernel, recursor rules included, and
   leaves out binder names, metadata, ``let``'s ``nondep`` flag and a
   definition's reducibility hints, which never change a term's meaning:
   the hygienic binder names of a ``match`` or an anonymous instance differ
   between the two modules, and
   Lean marks a theorem's non-dependent ``let`` as ``nondep`` but leaves an
   axiom's as written.
5. **Propositions.** The same program writes the canonical proposition of the
   theorem, of every axiom the module declares that it uses, and of any other
   constant the caller names (``bind``): the type and every declared constant
   its meaning reaches. The candidate's must equal the reference's, and each
   is returned as a fingerprint, so a caller can tell which proposition an
   axiom stood for, not only its name. Statement text is not enough: the same
   text can elaborate to different propositions under different names or
   contexts.

Every step runs confined (`glitnir.lean.sandbox`): the candidate may
write only its own build directory, cannot see the reference or the real
``/tmp``, can read but not change the report program, and has no network.
Without a working sandbox the check refuses to run and reports unknown, since
an unconfined proof could rewrite what the later steps read. The report
program is content-pinned and copied into the scratch directory before use.

The prelude is trusted, as it is for any replay: it fixes what the statement
means. Only the proof is untrusted. The result grants nothing by itself; callers
combine it with their own axiom policy.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any

from glitnir.lean.axiom_report import REPORT_PROGRAM, REPORT_SHA256
from glitnir.lean.environment import (
    SUPERVISOR_FAILED,
    bind_to_parent,
    kill_and_reap,
)
from glitnir.lean.sandbox import (
    SandboxUnavailable,
    confine,
    lean_readable,
    sandbox_available,
)
from glitnir.lean.scratch import scratch_directory

from glitnir_forseti.lean_release import LOCKFILE_BYTES, RELEASE_SHA256
from glitnir_forseti.lean_release import TOOLCHAIN as LEAN_TOOLCHAIN
from glitnir_forseti.lean_release import package_roots, select_project

from .release_environment import (
    lean_module_setup,
    lean_process_environment,
    lean_sysroot,
)
from .session import SessionLimits
from .unit import LEAN_WHITESPACE, THEOREM_NAME, declaration_head, render_unit

#: The module name both builds use, so private names and hygiene agree.
MODULE = "ForsetiIndependent"
#: The largest file a compile step may write. A compiled module is far larger
#: than any diagnostics: a notebook claim with a few ``simp`` proofs over
#: Mathlib already writes half a megabyte, so it is not held to the output cap.
MODULE_FILE_BYTES = 67_108_864
#: Separates proposition fingerprints from every other sha256 in Forseti. A
#: change after which a fingerprint made before it could equal one made after
#: it for another proposition must change it. Writing universe parameters by
#: position did not: a proposition without them is written byte for
#: byte as before, and one with them wrote names (strings) where it now writes
#: positions (numbers), so an old and a new text are never equal.
PROPOSITION_DOMAIN = "gimle.forseti.proposition/v1\n"


class IndependentStatus(str, Enum):
    """The outcome of an independent check."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IndependentCheck:
    """What the independent check established.

    Attributes:
        status: ``confirmed`` when the candidate kept every reference constant,
            passed kernel replay and its axioms were collected; ``rejected``
            when it did not compile, failed replay or changed the environment;
            ``unknown`` when a step could not complete.
        axioms: The collected axioms, for ``confirmed`` only.
        reason: Why the check was not confirmed.
        stop: For ``unknown`` because a process was stopped, what stopped
            it: ``timeout``, ``cancelled``, ``output`` or ``heartbeats``;
            otherwise empty.
        proposition: For ``confirmed``, the fingerprint of the proposition
            the theorem was proved to have (`proposition_fingerprint`).
        bound: For ``confirmed``, the fingerprint of the proposition of each
            axiom the theorem uses that the prelude declares, and of each
            constant the caller asked for, by full name, as the reference
            elaborated them.
    """

    status: IndependentStatus
    axioms: tuple[str, ...] = ()
    reason: str = ""
    stop: str = ""
    proposition: str = ""
    bound: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze the bound propositions."""
        object.__setattr__(self, "bound", MappingProxyType(dict(self.bound)))


def proposition_fingerprint(canonical: str) -> str:
    """Return the fingerprint of a canonical proposition from the report.

    Two fingerprints are equal only when the canonical texts are, and those
    are equal only when the kernel reads the same proposition over the same
    imports (`AxiomReport.lean`). Imported constants are only named in the
    canonical text, so the release pin, which fixes what every import means,
    is part of the fingerprint: a record made under another pin never matches.
    """
    text = f"{PROPOSITION_DOMAIN}{RELEASE_SHA256}\n{canonical}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_independently(
    prelude: str,
    statement: str,
    script: tuple[str, ...],
    limits: SessionLimits,
    project: Path | None = None,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
    bind: Sequence[str] = (),
) -> IndependentCheck:
    """Check one unit's theorem and collect its axioms out of process.

    Args:
        prelude: Imports and declarations before the theorem, trusted.
        statement: The theorem's binders and type, trusted.
        script: The tactic lines, untrusted.
        limits: Ceilings applied to each Lean process.
        project: The pinned Lean project; ``FORSETI_LEAN_PROJECT`` when None.
        deadline: A ``time.monotonic()`` instant no process may outlive, so
            the steps together respect one budget.
        cancel: Set to stop the running process and report ``cancelled``.
        bind: Full names of prelude constants whose propositions to return in
            ``bound`` besides the axioms the theorem uses.

    Returns:
        The outcome, with the collected axioms and the propositions when
        confirmed.
    """
    selected = select_project(project)
    elan = shutil.which("elan")
    if selected is None or elan is None:
        return IndependentCheck(IndependentStatus.UNKNOWN, reason="Lean unavailable")
    if not sandbox_available():
        return IndependentCheck(
            IndependentStatus.UNKNOWN, reason="no sandbox to confine the proof"
        )
    program = REPORT_PROGRAM.read_bytes()
    if hashlib.sha256(program).hexdigest() != REPORT_SHA256:
        return IndependentCheck(
            IndependentStatus.UNKNOWN, reason="the axiom report program was modified"
        )
    # Stripped exactly as `render_unit` strips the candidate, with Lean's
    # whitespace, so the reference states what the candidate proves.
    reference_source = (
        f"{prelude.rstrip(LEAN_WHITESPACE)}\n"
        f"axiom {declaration_head(THEOREM_NAME, statement)}\n"
    )
    candidate_source = render_unit(prelude, statement, script)
    with scratch_directory("glitnir-forseti-independent-") as root:
        reference = root / "reference"
        candidate = root / "candidate"
        shared = _merge_shared_namespaces(selected, root / "shared")
        (root / "report").mkdir()
        report_program = root / "report" / REPORT_PROGRAM.name
        report_program.write_bytes(program)
        run = partial(
            _run,
            limits=limits,
            project=selected,
            elan=elan,
            shared=shared,
            report=report_program,
            deadline=deadline,
            cancel=cancel,
        )
        steps = (
            (reference, reference_source),
            (candidate, candidate_source),
        )
        for directory, source in steps:
            outcome = _compile(directory, source, run)
            if outcome is not None:
                label = "reference" if directory is reference else "candidate"
                if label == "reference" or outcome.status is IndependentStatus.UNKNOWN:
                    return _with_label(outcome, label)
                return outcome
        # The candidate could write anything into its build directory while it
        # compiled, including forged modules for later steps to import. Only
        # its own module file moves on, into directories it never saw.
        reference = _isolate(reference, root / "reference-module")
        candidate = _isolate(candidate, root / "candidate-module")
        replay = run(["leanchecker", MODULE], lean_path=candidate)
        if not isinstance(replay, _Completed):
            return _stopped("kernel replay did not complete", replay)
        if replay.returncode != 0:
            return IndependentCheck(
                IndependentStatus.REJECTED,
                reason="kernel replay rejected the candidate: "
                + _tail(replay.stderr or replay.stdout),
            )
        found = _report(run, candidate, THEOREM_NAME)
        if isinstance(found, IndependentCheck):
            return found
        # The reference describes the same constants, so a candidate that
        # changed one cannot supply its own proposition for it.
        names = found.get("statements")
        wanted = [*(names if isinstance(names, dict) else ()), *bind]
        expected = _report(run, reference, THEOREM_NAME, wanted)
        if isinstance(expected, IndependentCheck):
            return expected
        return compare_reports(expected, found)


def compare_reports(
    reference: Mapping[str, Any], candidate: Mapping[str, Any]
) -> IndependentCheck:
    """Decide an independent check from the two module reports.

    Args:
        reference: The report of the reference module.
        candidate: The report of the candidate module, with its axioms.

    Returns:
        ``confirmed`` with the candidate's axioms and the reference's
        propositions, or ``rejected`` naming the first difference.
    """
    expected_constants, found = _constants(reference), _constants(candidate)
    if expected_constants is None or found is None:
        return IndependentCheck(
            IndependentStatus.UNKNOWN, reason="the axiom report was malformed"
        )
    if list(reference.get("imports", ())) != list(candidate.get("imports", ())):
        return IndependentCheck(
            IndependentStatus.REJECTED, reason="the candidate changed its imports"
        )
    declaration = candidate.get("declaration")
    if declaration is None or declaration not in expected_constants:
        return IndependentCheck(
            IndependentStatus.REJECTED,
            reason="the candidate's theorem is not the reference statement",
        )
    for name, constant in expected_constants.items():
        other = found.get(name)
        if other is None:
            return IndependentCheck(
                IndependentStatus.REJECTED, reason=f"the candidate dropped {name}"
            )
        if name == declaration:
            # An axiom's entry is not a theorem's; the type is compared below,
            # through the canonical propositions, which must be equal.
            same = (
                constant["kind"] == "axiom"
                and other["kind"] == "theorem"
                and constant["levels"] == other["levels"]
            )
        else:
            # Printed text is not compared: it carries binder names, which
            # the kernel ignores and hygiene makes differ between modules.
            same = all(constant[key] == other[key] for key in _COMPARED)
        if not same:
            return IndependentCheck(
                IndependentStatus.REJECTED, reason=f"the candidate changed {name}"
            )
    axioms = candidate.get("axioms")
    if not isinstance(axioms, list) or any(type(a) is not str for a in axioms):
        return IndependentCheck(
            IndependentStatus.UNKNOWN, reason="the axiom report was malformed"
        )
    expected, reported = _statements(reference), _statements(candidate)
    if (
        expected is None
        or reported is None
        or reference.get("declaration") != declaration
        or declaration not in expected
    ):
        return IndependentCheck(
            IndependentStatus.UNKNOWN, reason="the axiom report was malformed"
        )
    if reported.get(declaration) != expected[declaration]:
        return IndependentCheck(
            IndependentStatus.REJECTED,
            reason="the candidate's theorem is not the reference statement",
        )
    for name, statement in reported.items():
        if name in expected and statement != expected[name]:
            return IndependentCheck(
                IndependentStatus.REJECTED, reason=f"the candidate changed {name}"
            )
    return IndependentCheck(
        IndependentStatus.CONFIRMED,
        tuple(axioms),
        proposition=proposition_fingerprint(expected[declaration]),
        bound={
            name: proposition_fingerprint(statement)
            for name, statement in expected.items()
            if name != declaration
        },
    )


#: What must be equal of each reference constant in the candidate.
_COMPARED = ("kind", "levels", "meaning")


def _constants(report: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Return a report's constants by name; None when malformed."""
    constants = report.get("constants")
    if type(constants) is not list:
        return None
    described: dict[str, dict[str, Any]] = {}
    for constant in constants:
        if (
            type(constant) is not dict
            or type(constant.get("name")) is not str
            or type(constant.get("kind")) is not str
            or type(constant.get("levels")) is not list
            or type(constant.get("meaning")) is not str
            or constant["name"] in described
        ):
            return None
        described[constant["name"]] = constant
    return described


def _statements(report: Mapping[str, Any]) -> dict[str, str] | None:
    """Return a report's canonical propositions by name; None when malformed."""
    statements = report.get("statements")
    if type(statements) is not dict or any(
        type(v) is not str for v in statements.values()
    ):
        return None
    return statements


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _Stopped:
    """A process that was killed before it finished, and why."""

    cause: str


def _stopped(reason: str, result: Any) -> IndependentCheck:
    """``unknown`` for a step that did not complete, naming what stopped it."""
    cause = result.cause if isinstance(result, _Stopped) else ""
    return IndependentCheck(
        IndependentStatus.UNKNOWN,
        reason=f"{reason} ({cause})" if cause else reason,
        stop=cause if cause in ("timeout", "cancelled", "output") else "",
    )


def _run(
    arguments: Sequence[str],
    *,
    limits: SessionLimits,
    project: Path,
    elan: str,
    shared: Path,
    report: Path,
    lean_path: Path | None = None,
    cwd: Path | None = None,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> _Completed | _Stopped:
    """Run one confined toolchain program; `_Stopped` when it did not complete.

    Only ``cwd`` is writable, and only for compilation. The build directory
    named by ``lean_path``, the shared namespace and the report program are
    readable; nothing else under the scratch directory is visible.
    """
    environment = lean_process_environment(project, elan)
    # leanchecker would otherwise start ``lean`` to find its toolchain.
    try:
        environment["LEAN_SYSROOT"] = lean_sysroot(elan)
    except (OSError, subprocess.SubprocessError):
        return _Stopped("toolchain")
    first = [str(lean_path)] if lean_path is not None else []
    environment["LEAN_PATH"] = os.pathsep.join(
        (*first, str(shared), environment["LEAN_PATH"])
    )
    extra = [shared, report.parent, *([lean_path] if lean_path else [])]
    # The shared tree holds links into the packages; those resolve through the
    # project, which is readable.
    if deadline is not None and time.monotonic() >= deadline:
        return _Stopped("timeout")
    if cancel is not None and cancel.is_set():
        return _Stopped("cancelled")
    try:
        command = confine(
            [elan, "run", LEAN_TOOLCHAIN, *arguments],
            readable=lean_readable(project, *extra),
            writable=[cwd] if cwd is not None else [],
            cwd=cwd or report.parent,
        )
    except SandboxUnavailable:
        return _Stopped("sandbox")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            command,
            cwd=cwd or report.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            preexec_fn=(
                partial(
                    bind_to_parent,
                    os.getpid(),
                    MODULE_FILE_BYTES if cwd is not None else limits.max_output_bytes,
                )
                if os.name == "posix"
                else None
            ),
        )
        end = time.monotonic() + limits.timeout_ms / 1000
        if deadline is not None:
            end = min(end, deadline)
        stopped = _wait(process, end, cancel)
        if stopped is not None:
            return stopped
        returncode = process.returncode
        if returncode == SUPERVISOR_FAILED:
            return _Stopped("sandbox")  # the sandbox, not Lean, failed
        texts = []
        for handle in (out, err):
            size = handle.tell()
            if size > limits.max_output_bytes:
                return _Stopped("output")
            handle.seek(0)
            texts.append(handle.read().decode("utf-8", errors="replace"))
    return _Completed(returncode, texts[0], texts[1])


def _wait(
    process: subprocess.Popen[bytes], end: float, cancel: threading.Event | None
) -> _Stopped | None:
    """Wait until the process exits; kill and reap it at ``end`` or on cancel."""
    try:
        while True:
            remaining = end - time.monotonic()
            if cancel is not None and cancel.is_set():
                kill_and_reap(process)
                return _Stopped("cancelled")
            if remaining <= 0:
                kill_and_reap(process)
                return _Stopped("timeout")
            try:
                process.wait(timeout=min(remaining, 0.05 if cancel else remaining))
                return None
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        kill_and_reap(process)
        return _Stopped("cancelled")
    except BaseException:
        kill_and_reap(process)
        raise


def _merge_shared_namespaces(project: Path, destination: Path) -> Path:
    """Link every package's build of a shared top-level namespace into one tree.

    Lean looks a module up in the first search-path root that has its
    top-level directory. ``Gimle.*`` modules are split between the pinned
    packages, and ``lean --setup`` works around that for elaboration, but
    ``leanchecker`` and ``lean --run`` read only ``LEAN_PATH``. A root holding
    links to all of them, placed first, resolves every module.
    """
    destination.mkdir()
    roots = package_roots(project, json.loads(LOCKFILE_BYTES))
    for root in roots.values():
        library = root / ".lake/build/lib/lean"
        for top in ("Gimle",):
            for source in sorted((library / top).rglob("*")):
                if source.is_dir():
                    continue
                target = destination / source.relative_to(library)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source)
    return destination


def _isolate(build: Path, destination: Path) -> Path:
    """Copy a build's module file, and nothing else, into a fresh directory."""
    destination.mkdir()
    olean = f"{MODULE}.olean"
    source = build / olean
    if source.is_symlink() or not source.is_file():
        return destination
    shutil.copyfile(source, destination / olean)
    return destination


def _compile(directory: Path, source: str, run: Any) -> IndependentCheck | None:
    """Compile one unit to ``directory/MODULE.olean``; None on success."""
    directory.mkdir()
    (directory / f"{MODULE}.lean").write_text(source, encoding="utf-8")
    project = run.keywords["project"]
    (directory / f"{MODULE}.setup.json").write_text(
        lean_module_setup(project, MODULE), encoding="utf-8"
    )
    result = run(
        [
            "lean",
            "--json",
            f"-DmaxHeartbeats={run.keywords['limits'].max_heartbeats}",
            "--setup",
            f"{MODULE}.setup.json",
            "-o",
            f"{MODULE}.olean",
            f"{MODULE}.lean",
        ],
        cwd=directory,
    )
    if not isinstance(result, _Completed):
        return _stopped("compilation did not complete", result)
    written = (directory / f"{MODULE}.olean").is_file()
    if (
        result.returncode in _LIMIT_EXITS
        and not written
        and _first_error(result.stdout) == _NO_ERROR
    ):
        # Killed by the file-size or CPU limit, or by the kernel for memory:
        # Lean ran out of a resource, which says nothing about the proof
        # (invariant 4). A proof can exit with the same code itself, so this
        # can turn its own rejection into unknown, never into acceptance.
        return IndependentCheck(
            IndependentStatus.UNKNOWN,
            reason=f"compilation was stopped by a limit (exit {result.returncode})",
        )
    if result.returncode != 0 or not written:
        error = _first_error(result.stdout)
        if _HEARTBEATS in error:
            # Elaboration ran out of its budget, which says nothing about the
            # proof (invariant 4). A proof printing this text itself can only
            # turn its own rejection into unknown, never into acceptance.
            return IndependentCheck(
                IndependentStatus.UNKNOWN,
                reason="compilation ran out of heartbeats: " + error,
                stop="heartbeats",
            )
        return IndependentCheck(
            IndependentStatus.REJECTED, reason="did not compile: " + error
        )
    return None


def _report(run: Any, directory: Path, target: str, names: Sequence[str] = ()) -> Any:
    arguments = ["lean", "--run", str(run.keywords["report"]), MODULE, target]
    arguments.extend(names)
    result = run(arguments, lean_path=directory)
    if not isinstance(result, _Completed):
        return _stopped("the axiom report did not complete", result)
    if result.returncode != 0:
        return IndependentCheck(
            IndependentStatus.REJECTED,
            reason="the axiom report failed: " + _tail(result.stderr),
        )
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return IndependentCheck(
            IndependentStatus.UNKNOWN, reason="the axiom report was not JSON"
        )
    if type(report) is not dict or type(report.get("constants")) is not list:
        return IndependentCheck(
            IndependentStatus.UNKNOWN, reason="the axiom report was malformed"
        )
    return report


def _with_label(check: IndependentCheck, label: str) -> IndependentCheck:
    status = (
        IndependentStatus.UNKNOWN
        if label == "reference" and check.status is IndependentStatus.REJECTED
        else check.status
    )
    return IndependentCheck(
        status, check.axioms, f"{label}: {check.reason}", check.stop
    )


_NO_ERROR = "Lean exited with an error"
#: How Lean reports an exhausted ``maxHeartbeats`` budget.
_HEARTBEATS = "maximum number of heartbeats"
#: Exit codes of a process killed by SIGKILL, SIGXCPU or SIGXFSZ, as reported
#: through elan (128 + signal) or by Python (-signal).
_LIMIT_EXITS = frozenset(
    code for signal in (9, 24, 25) for code in (128 + signal, -signal)
)


def _first_error(output: str) -> str:
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("severity") == "error":
            return (str(message.get("data", "")).splitlines() or ["error"])[0]
    return _NO_ERROR


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no output"
