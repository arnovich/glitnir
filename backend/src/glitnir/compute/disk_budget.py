"""Sample all registered-entry bytes with bounded, non-following tree scans."""

import os
import stat
from collections.abc import Callable
from pathlib import Path

MAX_ITEMS = 200000
MAX_DEPTH = 128
_TREES = {"project", "stage", "build-home"}


def check_disk_budget(
    root: Path, max_bytes: int, monitor: Callable[[], None] = lambda: None
) -> None:
    """Keep one byte ceiling, with separate bounds for the entry's owned trees.

    Project, private stage, build HOME and remaining entry data each admit at most
    MAX_ITEMS entries (directories included). Count logical regular-file/link
    sizes, as before; hardlinked copies still count twice. Only live paths that
    disappeared may be omitted. Immutable inventories remain separate and strict.
    Directory descriptors prevent a concurrent link swap from escaping the tree.
    """
    counts = {name: 0 for name in (*_TREES, "entry")}
    size = visited = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def scan(descriptor: int, scope: str, depth: int) -> None:
        nonlocal size, visited
        with os.scandir(descriptor) as entries:
            for entry in entries:
                visited += 1
                if visited % 256 == 0:
                    monitor()
                current = entry.name if depth == 0 and entry.name in _TREES else scope
                counts[current] += 1
                if counts[current] > MAX_ITEMS:
                    raise ValueError("Environment exceeds traversal budget")
                try:
                    info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if depth >= MAX_DEPTH:
                        raise ValueError("Environment exceeds directory depth budget")
                    try:
                        child = os.open(entry.name, flags, dir_fd=descriptor)
                    except FileNotFoundError:
                        continue
                    try:
                        scan(child, current, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    size += info.st_size
                    if size > max_bytes:
                        raise ValueError("Environment exceeds its disk budget")
                else:
                    raise ValueError("Environment contains a non-regular file")

    monitor()
    try:
        descriptor = os.open(root, flags)
    except FileNotFoundError:
        return
    try:
        scan(descriptor, "entry", 0)
    finally:
        os.close(descriptor)
    monitor()
