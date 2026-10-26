"""What the preview service shares with its run and view components."""

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from glitnir.lean.fingerprint import Fingerprint
from glitnir.preview.snapshot import Fingerprints
from glitnir.providers import Providers

CLOSED = "This preview is closed."


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreviewContext:
    """The service's shared objects, handed to each of its components.

    The locks and the cancel event are the service's own instances, so every
    lock ordering holds across the service and its components. The callables
    are the service's: `text` reads the saved source, `fingerprint` and
    `snapshot` are `_fingerprint` and `_snapshot`, `unsupported` says why Lean
    cannot run for this notebook, and `providers` loads installed providers.

    The callables are bound at construction, so a component reaches the service
    only through them: patching a service attribute such as `_text` afterwards
    does not reach the components. Tests replace the context with
    `dataclasses.replace` instead.
    """

    path: Path
    cancel: threading.Event
    state_lock: threading.Lock
    records_lock: threading.Lock
    providers: Callable[[], Providers]
    text: Callable[[], str]
    fingerprint: Callable[[str], Fingerprints]
    snapshot: Callable[[str, Fingerprint], dict[str, Any]]
    unsupported: Callable[[], str | None]
