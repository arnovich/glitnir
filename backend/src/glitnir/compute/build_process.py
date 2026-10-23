"""Supervise a trusted build group even when its owning worker disappears.

The inherited cache lock stays open until this process exits. Every command and
its ordinary descendants share this supervisor's session. Code that deliberately
escapes sessions still requires service-level containment.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from glitnir.compute.client import save_receipt


def main() -> None:
    """Record the command outcome, then kill this entire process group."""
    specification = json.loads(Path(sys.argv[1]).read_text())
    try:
        if os.getppid() != specification["parent"]:
            return
        process = subprocess.Popen(specification["command"], stdin=subprocess.DEVNULL)
        while process.poll() is None:
            if os.getppid() != specification["parent"]:
                return
            time.sleep(0.05)
        save_receipt(Path(specification["result"]), {"returncode": process.returncode})
    finally:
        # Includes descendants after a successful leader exit; never publish while
        # a background build writer still owns this session.
        os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    main()
