"""Bounded local scheduling for experimental, non-authoritative elaboration."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4


class Interrupted(Exception):
    """A response is unknown; this request must not be executed again silently."""


class Aborted(Interrupted):
    """One explicit cancellation or elapsed deadline, separate from proof failure."""

    def __init__(self, state: str, reason: str) -> None:
        """Retain the terminal execution state without inventing a proof verdict."""
        super().__init__(reason)
        self.state = state


@dataclass(frozen=True)
class Limits:
    """Small experiment ceilings; these do not configure production workers."""

    workers: int = 1
    max_pending: int = 16
    max_requests: int = 8
    request_seconds: int = 60
    memory_mb: int = 4096
    max_jobs: int = 128

    def __post_init__(self) -> None:
        """Reject booleans, unbounded histories and oversized experimental pools."""
        for name, low, high in (
            ("workers", 1, 2),
            ("max_pending", 1, 128),
            ("max_requests", 1, 8),
            ("request_seconds", 1, 300),
            ("memory_mb", 1024, 4096),
            ("max_jobs", 1, 128),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid experimental {name} limit")


class Environment(Protocol):
    """An operator-frozen imported environment, never model-selected build policy."""

    @property
    def key(self) -> str:
        """Content identity of the imported snapshot and experimental program."""
        ...

    def assert_current(self) -> None:
        """Reject changed inputs before using a resident elaborator."""


@dataclass
class Job:
    """One stable request with a separate terminal future and cancellation token."""

    identity: str
    client: str
    environment: Environment
    source: str
    submitted: float
    deadline: float
    cancel: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    future: Future[dict[str, Any]] = field(default_factory=Future)

    def remaining(self) -> float:
        """Include queue and startup time in the original request deadline."""
        if self.cancel.is_set():
            raise Aborted("cancelled", "explicit cancellation")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise Aborted("timed_out", "original request deadline exhausted")
        return left


def wait_for(job: Job, stop: threading.Event, *, started: bool = False) -> None:
    """Wait for one probe boundary while forwarding the shared resource guard."""
    while not job.future.done() and not (started and job.started.is_set()):
        if stop.is_set():
            job.cancel.set()
        time.sleep(0.05)


class Session(Protocol):
    """One serial imported process; errors never cause an implicit re-execution."""

    @property
    def environment(self) -> Environment:
        """The immutable imported input selected for this session."""
        ...

    generation: int
    requests: int
    closed: bool

    def check(self, request: Job) -> dict[str, Any]:
        """Return untrusted diagnostics, or interrupt this logical request."""

    def close(self) -> None:
        """Kill and reap the process and its ordinary descendants."""


Factory = Callable[[Environment, Limits, int], Session]


class Pool:
    """An offline bounded FIFO, with one current environment per worker slot."""

    def __init__(
        self, limits: Limits, *, session_factory: Factory | None = None
    ) -> None:
        """Bind execution to trusted setup; no network listener or credential exists."""
        if session_factory is None:
            from experiments.warm_pool.session import LeanSession

            session_factory = LeanSession
        self.limits = limits
        self._factory: Factory = session_factory
        self._executor = ThreadPoolExecutor(max_workers=limits.workers)
        self._capacity = threading.BoundedSemaphore(limits.max_pending)
        self._lock, self._local = threading.Lock(), threading.local()
        self._jobs: dict[str, Job] = {}
        self._sessions: list[Session] = []
        self._generation, self._closed = 0, False

    def __enter__(self) -> Pool:
        """Own all child processes for the context's lifetime."""
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Cancel outstanding work and reap processes even when the caller raises."""
        self.close()

    def submit(
        self,
        environment: Environment,
        source: str,
        *,
        client: str,
        identity: str | None = None,
    ) -> Job:
        """Return immediately; identical IDs retrieve the original future."""
        if (
            not isinstance(source, str)
            or not 0 < len(source.encode()) <= 1_000_000
            or "\r" in source
        ):
            raise ValueError("Use bounded LF source bytes")
        if not isinstance(client, str) or not 0 < len(client) <= 64:
            raise ValueError("Choose a bounded logical client")
        identity = identity or str(uuid4())
        if str(UUID(identity)) != identity:
            raise ValueError("Use a canonical request UUID")
        with self._lock:
            if self._closed:
                raise ValueError("Experimental pool is closed")
            previous = self._jobs.get(identity)
            if previous:
                if (previous.client, previous.environment.key, previous.source) != (
                    client,
                    environment.key,
                    source,
                ):
                    raise ValueError("Request identity already names different inputs")
                return previous
            if len(self._jobs) >= self.limits.max_jobs:
                raise ValueError("Experimental job history is full")
            if not self._capacity.acquire(blocking=False):
                raise ValueError("Experimental pending queue is full")
            submitted = time.monotonic()
            job = Job(
                identity,
                client,
                environment,
                source,
                submitted,
                submitted + self.limits.request_seconds,
            )
            self._jobs[identity] = job
            self._executor.submit(self._work, job)
            return job

    def _work(self, job: Job) -> None:
        started = time.monotonic()
        session: Session | None = getattr(self._local, "session", None)
        generation = 0
        result: dict[str, Any] = {
            "request_id": job.identity,
            "client": job.client,
            "source_sha256": hashlib.sha256(job.source.encode()).hexdigest(),
            "environment_sha256": job.environment.key,
            "queue_ms": (started - job.submitted) * 1000,
        }
        try:
            job.remaining()
            job.environment.assert_current()
            if session and (
                session.closed
                or session.requests >= self.limits.max_requests
                or session.environment.key != job.environment.key
            ):
                session.close()
                session = None
            if session is None:
                with self._lock:
                    self._generation += 1
                    session = self._factory(
                        job.environment, self.limits, self._generation
                    )
                    self._sessions.append(session)
                self._local.session = session
            generation = session.generation
            advisory = session.check(job)
            job.remaining()
            job.environment.assert_current()
            result.update(
                state="completed", advisory={**advisory, "proof_authority": False}
            )
        except (Interrupted, ValueError, OSError) as error:
            if session:
                session.close()
            result.update(
                state=error.state if isinstance(error, Aborted) else "interrupted",
                reason=str(error),
            )
        except BaseException as error:
            if session:
                session.close()
            job.future.set_exception(error)
            return
        finally:
            self._capacity.release()
        result.update(
            generation=generation,
            service_ms=(time.monotonic() - started) * 1000,
            response_ms=(time.monotonic() - job.submitted) * 1000,
        )
        job.future.set_result(result)

    def close(self) -> None:
        """Stop the local experiment without reassigning interrupted jobs."""
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if not job.future.done():
                    job.cancel.set()
        self._executor.shutdown(wait=True)
        for session in self._sessions:
            session.close()
