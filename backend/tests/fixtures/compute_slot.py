"""Run a bounded number of real worker leases for the loopback integration test."""

import sys
import threading
from pathlib import Path

from glitnir.compute.client import Client, Settings
from glitnir.compute.worker import Worker


def main() -> None:
    """Register one slot and process only the explicitly assigned test jobs."""
    url, token, prepared, journal, count = sys.argv[1:]
    client = Client(Settings(url, "lab", "prepared", token))
    worker = Worker(client, Path(prepared), Path(journal))
    worker.register()
    for _ in range(int(count)):
        assert worker.run_once(threading.Event())


if __name__ == "__main__":
    main()
