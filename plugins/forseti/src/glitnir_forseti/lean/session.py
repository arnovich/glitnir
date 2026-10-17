"""Bounded elaboration sessions against the pinned Lean release.

A session elaborates one source unit and reports what Lean said about it:
the structured diagnostics, and the goals any tactic block left open.  It is
untrusted search machinery.  A completed elaboration is an observation, never
proof authority, and a declaration admitted with ``sorry`` is a failed attempt.

The transport is deliberately behind :class:`LeanSession` so the subprocess
implementation can later be replaced by a Lean-side worker answering a
protocol, without changing any caller.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Protocol

from glitnir.lean.environment import (
    LEAN_AXIOM_ALLOWLIST,
    MAX_SOURCE_BYTES,
    bind_to_parent,
    kill_and_reap,
)
from glitnir.lean.limits import SessionLimits
from glitnir.lean.sandbox import SandboxUnavailable, confine, lean_readable
from glitnir.lean.scratch import scratch_directory

from glitnir_forseti.lean_release import TOOLCHAIN as LEAN_TOOLCHAIN
from glitnir_forseti.lean_release import select_project

from .goals import GoalState, parse_goal_state
from .messages import MessageStream, parse_messages
from .release_environment import lean_module_setup, lean_process_environment

_AXIOM_MARKER = "depends on axioms: ["

#: Reported when no Lean process exit status was observed.
NO_EXIT_CODE = -1


class SessionStatus(str, Enum):
    """Closed observations from one untrusted elaboration attempt."""

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    UNAVAILABLE = "unavailable"
    OUTPUT_LIMIT = "output_limit"


@dataclass(frozen=True, slots=True)
class Elaboration:
    """Everything one elaboration attempt observed."""

    status: SessionStatus
    source_sha256: str
    stream: MessageStream
    goal_states: tuple[GoalState, ...]
    axioms: tuple[str, ...]
    exit_code: int = NO_EXIT_CODE

    def __post_init__(self) -> None:
        """Own exact bounded observation fields from even a hostile runner."""
        if type(self.status) is not SessionStatus:
            raise TypeError("status must be a SessionStatus")
        if type(self.source_sha256) is not str:
            raise TypeError("source_sha256 must be a string")
        if type(self.stream) is not MessageStream:
            raise TypeError("stream must be a MessageStream")
        if type(self.goal_states) is not tuple or any(
            type(state) is not GoalState for state in self.goal_states
        ):
            raise TypeError("goal_states must be a tuple of GoalState values")
        if type(self.axioms) is not tuple or any(
            type(axiom) is not str for axiom in self.axioms
        ):
            raise TypeError("axioms must be a tuple of strings")
        if type(self.exit_code) is not int:
            raise TypeError("exit_code must be a non-Boolean integer")
        if self.exit_code < NO_EXIT_CODE:
            raise ValueError("exit_code must not precede the unobserved sentinel")

    def succeeded(self) -> bool:
        """Report whether Lean accepted the unit with nothing left open.

        A true result means Lean exited cleanly, raised no error and admitted
        no ``sorry``.  It does not mean the statement is the intended one, and
        it grants no proof authority.

        The exit code is checked separately from the parsed diagnostics: a
        process that fails before emitting any JSON — a missing setup file, an
        unreadable import root — produces an empty message stream, and only its
        exit status distinguishes that from a unit Lean accepted in silence.

        Returns:
            True when the attempt completed cleanly with no error and no
            ``sorry``.
        """
        return (
            self.status is SessionStatus.COMPLETED
            and self.exit_code == 0
            and not self.stream.errors()
            and not self.stream.has_sorry()
        )

    def forbidden_axioms(self) -> tuple[str, ...]:
        """Return every reported axiom outside the permitted set.

        Returns:
            The ordered axioms that are not in the allowlist.
        """
        return tuple(
            axiom for axiom in self.axioms if axiom not in LEAN_AXIOM_ALLOWLIST
        )

    def open_goals(self) -> GoalState:
        """Return every goal left open across the unit's tactic blocks.

        Returns:
            One state carrying the concatenated remaining goals.
        """
        goals = tuple(goal for state in self.goal_states for goal in state.goals)
        truncated = any(state.truncated for state in self.goal_states)
        return GoalState(goals, truncated)


class LeanSession(Protocol):
    """Untrusted execution adapter for one Lean source unit."""

    def elaborate(self, source: str, limits: SessionLimits) -> Elaboration:
        """Elaborate the source and report the observed diagnostics."""


def _axioms(stream: MessageStream) -> tuple[str, ...]:
    found: set[str] = set()
    for message in stream.messages:
        index = message.text.find(_AXIOM_MARKER)
        if index < 0:
            continue
        body = message.text[index + len(_AXIOM_MARKER) :]
        listed, _, _ = body.partition("]")
        found.update(name.strip() for name in listed.split(",") if name.strip())
    return tuple(axiom for axiom in LEAN_AXIOM_ALLOWLIST if axiom in found) + tuple(
        sorted(found.difference(LEAN_AXIOM_ALLOWLIST))
    )


def _observation(
    status: SessionStatus,
    digest: str,
    raw: bytes,
    exit_code: int = NO_EXIT_CODE,
) -> Elaboration:
    stream = parse_messages(raw)
    states = tuple(
        parse_goal_state(message.text) for message in stream.unsolved_goals()
    )
    return Elaboration(status, digest, stream, states, _axioms(stream), exit_code)


class RecordedLeanSession:
    """Replay captured Lean output so callers can be tested without Lean."""

    def __init__(self, responses: tuple[tuple[SessionStatus, bytes, int], ...]) -> None:
        """Hold the ordered responses this session will return.

        Args:
            responses: Ordered status, raw ``lean --json`` output and process
                exit status triples.
        """
        self._responses = list(responses)
        self.requests: list[str] = []

    def elaborate(self, source: str, limits: SessionLimits) -> Elaboration:
        """Return the next recorded response for the supplied source.

        Args:
            source: The Lean source unit that would have been elaborated.
            limits: The ceilings a real session would have applied.

        Returns:
            The next recorded observation.
        """
        if type(source) is not str:
            raise TypeError("source must be a string")
        if type(limits) is not SessionLimits:
            raise TypeError("limits must be a SessionLimits")
        self.requests.append(source)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if not self._responses:
            return _observation(SessionStatus.UNAVAILABLE, digest, b"")
        status, raw, exit_code = self._responses.pop(0)
        return _observation(status, digest, raw, exit_code)


class SubprocessLeanSession:
    """Elaborate a source unit in the explicitly selected pinned release."""

    def __init__(self, project: Path | None = None) -> None:
        """Prefer an explicit project over ``FORSETI_LEAN_PROJECT``.

        Args:
            project: The compatible Lean project root, or None to configure it
                from the environment.
        """
        self._project = select_project(project)

    @property
    def project(self) -> Path | None:
        """Return the Lean project selected for this session."""
        return self._project

    def elaborate(self, source: str, limits: SessionLimits) -> Elaboration:
        """Run pinned Lean over the source and report what it observed.

        Args:
            source: The Lean source unit to elaborate.
            limits: The wall-clock, heartbeat and output ceilings to apply.

        Returns:
            The observed status, diagnostics, remaining goals and axioms.
        """
        if type(source) is not str:
            raise TypeError("source must be a string")
        if type(limits) is not SessionLimits:
            raise TypeError("limits must be a SessionLimits")
        encoded = source.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds its byte limit")
        digest = hashlib.sha256(encoded).hexdigest()
        elan = shutil.which("elan")
        if self._project is None or elan is None:
            return _observation(SessionStatus.UNAVAILABLE, digest, b"")
        return self._run(source, limits, digest, self._project, elan)

    def _run(
        self,
        source: str,
        limits: SessionLimits,
        digest: str,
        project: Path,
        elan: str,
    ) -> Elaboration:
        # The unit is untrusted: it is written to a private scratch directory,
        # the only place the confined process may write. That is a subdirectory,
        # so the confined process cannot reach the owner marker beside it.
        with scratch_directory("glitnir-forseti-session-") as owned:
            scratch = owned / "work"
            scratch.mkdir()
            source_path = scratch / "ForsetiSession.lean"
            source_path.write_text(source, encoding="utf-8")
            setup_path = source_path.with_suffix(".setup.json")
            setup_path.write_text(lean_module_setup(project, source_path.stem))
            return self._elaborate(
                limits, digest, project, elan, source_path, setup_path
            )

    def _elaborate(
        self,
        limits: SessionLimits,
        digest: str,
        project: Path,
        elan: str,
        source_path: Path,
        setup_path: Path,
    ) -> Elaboration:
        try:
            command = confine(
                [
                    elan,
                    "run",
                    LEAN_TOOLCHAIN,
                    "lean",
                    "--json",
                    f"-DmaxHeartbeats={limits.max_heartbeats}",
                    "--setup",
                    setup_path.name,
                    source_path.name,
                ],
                readable=lean_readable(project),
                writable=(source_path.parent,),
                cwd=source_path.parent,
            )
        except SandboxUnavailable:
            return _observation(SessionStatus.UNAVAILABLE, digest, b"")
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                cwd=source_path.parent,
                env=lean_process_environment(project, elan),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                preexec_fn=(
                    partial(bind_to_parent, os.getpid(), limits.max_output_bytes)
                    if os.name == "posix"
                    else None
                ),
            )
            try:
                returncode = process.wait(timeout=limits.timeout_ms / 1000)
            except subprocess.TimeoutExpired:
                kill_and_reap(process)
                return _observation(SessionStatus.TIMEOUT, digest, b"")
            except KeyboardInterrupt:
                kill_and_reap(process)
                return _observation(SessionStatus.INTERRUPTED, digest, b"")
            size = output.tell()
            output.seek(0)
            raw = output.read(min(size, limits.max_output_bytes + 1))
        if size > limits.max_output_bytes:
            return _observation(SessionStatus.OUTPUT_LIMIT, digest, b"")
        return _observation(SessionStatus.COMPLETED, digest, raw, returncode)
