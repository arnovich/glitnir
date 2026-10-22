"""Read the module artifacts Lake's `setup-file` lists for each import.

Lake reports each import's files as `importArts`, in the nested shape of the
supported Lean release (4.33 and later): `[[olean, oleanServer?, oleanPrivate?],
[irSig, ir?]]`, where the second group is absent for a file outside the module
system. Only this shape is read; any other is refused.
"""

from collections.abc import Callable
from typing import Any, cast

#: The suffix each position of a nested `importArts` entry stands for.
SUFFIXES = ((".olean", ".olean.server", ".olean.private"), (".ir.sig", ".ir"))


def valid_artifacts(value: Any) -> bool:
    """Tell whether `value` is one `importArts` entry of the supported shape.

    That is one or two lists of paths: the data group of one to three, then
    optionally the IR group of one or two.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= len(SUFFIXES):
        return False
    for group, suffixes in zip(value, SUFFIXES, strict=False):
        if not isinstance(group, list) or not 1 <= len(group) <= len(suffixes):
            return False
        if not all(isinstance(path, str) for path in group):
            return False
    return True


def _checked(value: Any) -> list[list[str]]:
    if not valid_artifacts(value):
        raise ValueError("Lake returned invalid import artifacts.")
    return cast(list[list[str]], value)


def artifact_files(value: Any) -> list[tuple[str, str]]:
    """Return each path of one `importArts` entry with the suffix it stands for."""
    return [
        (path, suffix)
        for group, suffixes in zip(_checked(value), SUFFIXES, strict=False)
        for path, suffix in zip(group, suffixes, strict=False)
    ]


def map_artifacts(value: Any, change: Callable[[str, str], str]) -> list[list[str]]:
    """Rewrite each path of one `importArts` entry, keeping its shape.

    `change` receives each path with the suffix it stands for.
    """
    return [
        [change(path, suffix) for path, suffix in zip(group, suffixes, strict=False)]
        for group, suffixes in zip(_checked(value), SUFFIXES, strict=False)
    ]


def olean_path(value: Any) -> str:
    """Return the `.olean` path of one `importArts` entry."""
    return _checked(value)[0][0]
