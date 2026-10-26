"""Preflight and sampled resource guards for an owned offline scratch tree."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from types import TracebackType


class Budget:
    """Cancel preparation and checks on a disk, free-space or wall-time breach.

    Disk accounting deduplicates hard links and excludes symbolic links. The
    monitor is sampled, not an OS quota: trusted fixture preparation is also
    preflighted, and native checking retains its own scratch/output bounds.
    """

    def __init__(
        self, root: Path, *, disk_bytes: int = 3 * 1024**3, seconds: int = 1800
    ) -> None:
        """Observe only an existing, private tree owned by this experiment."""
        self.root = root.resolve(strict=True)
        self.disk_bytes, self.deadline = disk_bytes, time.monotonic() + seconds
        self.cancel, self._stop = threading.Event(), threading.Event()
        self.reason = ""
        self.peak_bytes = 0
        self._thread: threading.Thread | None = None

    def check(self) -> int:
        """Check unique regular bytes and record the observed high-water mark."""
        total = 0
        seen = set()
        for parent, _, names in os.walk(self.root, followlinks=False):
            for name in names:
                path = Path(parent) / name
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                identity = info.st_dev, info.st_ino
                if not path.is_symlink() and path.is_file() and identity not in seen:
                    seen.add(identity)
                    total += info.st_size
        self.peak_bytes = max(total, self.peak_bytes)
        if total > self.disk_bytes:
            self.reason = "experiment-owned disk ceiling exceeded"
        elif time.monotonic() >= self.deadline:
            self.reason = "experiment wall-time ceiling exceeded"
        elif shutil.disk_usage(self.root).free < 1024**3:
            self.reason = "experiment free-disk reserve reached"
        if self.reason:
            self.cancel.set()
            raise ValueError(self.reason)
        return total

    def reserve(self, additional: int) -> None:
        """Refuse a known preparation upper bound before storing any new bytes."""
        current = self.check()
        if current + additional > self.disk_bytes or additional > (
            shutil.disk_usage(self.root).free - 1024**3
        ):
            raise ValueError(
                "snapshot and scratch reserve do not fit experiment budget"
            )

    def __enter__(self) -> Budget:
        """Start a half-second monitor, alongside the preparation preflight."""
        self.check()

        def watch() -> None:
            while not self._stop.wait(0.5):
                try:
                    self.check()
                except (ValueError, OSError) as error:
                    self.reason = str(error)
                    self.cancel.set()
                    return

        self._thread = threading.Thread(target=watch, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Join the monitor and surface a breach even if a child failed first."""
        self._stop.set()
        if self._thread:
            self._thread.join()
        self.check()
