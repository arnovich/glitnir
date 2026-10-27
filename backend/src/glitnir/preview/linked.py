"""Reading another notebook that a notebook's prose links to.

A link is a plain relative `.lean` path; it is resolved below the project
root and read without following any symbolic link. Nothing here runs Lean.
"""

import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import TypedDict

from glitnir.lean.environment import MAX_SOURCE_BYTES
from glitnir.lean.project import describe_project
from glitnir.notebook.document import NotebookError

#: The longest link to another notebook the preview resolves.
MAX_LINK_CHARACTERS = 1024

_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: A Lean identifier part, as module names are spelled in links.
LEAN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")


def _plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def unbuilt_sibling_imports(path: Path, imports: dict[str, str | None]) -> list[str]:
    """Name directly imported sibling notebooks that have no Lake build yet."""
    return [
        module
        for module, trace in imports.items()
        if trace is None
        and _MODULE_NAME.fullmatch(module)
        and _plain_file(path.parent / f"{module}.lean")
    ]


class Reference(TypedDict):
    """A sibling notebook that prose links may open: its module and file name."""

    module: str
    file: str


def sibling_references(path: Path) -> list[Reference]:
    """List sibling notebooks that prose links may open, never following links."""
    siblings = sorted(
        candidate
        for candidate in path.parent.glob("*.lean")
        if _MODULE_NAME.fullmatch(candidate.stem) and _plain_file(candidate)
    )
    return [{"module": item.stem, "file": item.name} for item in siblings]


class NotebookLinkError(NotebookError):
    """A link to another notebook that the preview refuses to read.

    Attributes:
        status: The HTTP status that describes the refusal: 400 for a link
            that is not a plain relative ``.lean`` path, 404 for a file that
            is missing, a symbolic link, not regular or outside the project,
            and 413 for a file above the preview's size limit.
    """

    def __init__(self, message: str, status: int) -> None:
        """Keep the refusal's message and HTTP status."""
        super().__init__(message)
        self.status = status


def _link_parts(link: str) -> list[str]:
    """Split a relative ``.lean`` link; refuse anything that could leave it."""
    parts = link.split("/")
    if (
        not link
        or len(link) > MAX_LINK_CHARACTERS
        or link.startswith("/")
        or any(character in link for character in "\\\0")
        or any(part in ("", ".", "..") for part in parts)
        or not link.endswith(".lean")
        or parts[-1] == ".lean"
    ):
        raise NotebookLinkError(
            "Send a .lean path relative to this notebook, without '.' or '..'.", 400
        )
    return parts


def _named_exactly(descriptor: int, part: str) -> bool:
    """Whether the directory open at `descriptor` has an entry spelled `part`.

    A case-insensitive file system opens ``MAIN.lean`` as ``Main.lean``; the
    link must use the entry's own case. Unicode normalization is not case,
    so both sides are compared in NFC.
    """
    wanted = unicodedata.normalize("NFC", part)
    return any(
        unicodedata.normalize("NFC", entry) == wanted
        for entry in os.listdir(descriptor)
    )


def _read_below(directory: Path, parts: list[str], notebook: os.stat_result) -> bytes:
    """Read ``directory/parts`` without following any link below `directory`.

    Each directory and the file are opened relative to the one before with
    ``O_NOFOLLOW``, so a link swapped in after a check is refused too. Each
    part must match its directory entry exactly, and the file must not be
    `notebook`, the previewed notebook itself.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for depth, part in enumerate(parts):
            if not _named_exactly(descriptor, part):
                raise FileNotFoundError("no entry with exactly this name")
            if depth == len(parts) - 1:
                break
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with os.fdopen(file, "rb") as stream:
        found = os.fstat(stream.fileno())
        if not stat.S_ISREG(found.st_mode):
            raise IsADirectoryError("not a regular file")
        if (found.st_dev, found.st_ino) == (notebook.st_dev, notebook.st_ino):
            raise NotebookLinkError("That link names this notebook itself.", 404)
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise NotebookLinkError(
            "That notebook is larger than the 16 MiB preview limit.", 413
        )
    return data


def read_linked_notebook(notebook: Path, link: str) -> tuple[Path, str]:
    """Resolve and read another notebook that `notebook`'s prose links to.

    The link is relative to `notebook`'s directory. It must name a regular
    ``.lean`` file reached through no symbolic link and lying under the
    notebook's resolved project root: its Lake root, a loose file's
    toolchain directory, or else its own directory.

    Returns:
        The linked file's path and its saved text with LF line endings.

    Raises:
        NotebookLinkError: When the link or the file is refused.
    """
    parts = _link_parts(link)
    directory = notebook.absolute().parent
    root = Path(describe_project(notebook)["root"] or directory).resolve()
    target = directory.joinpath(*parts)
    try:
        # A symbolic link loop raises RuntimeError here; never report a path.
        inside = target.resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        inside = False
    if not inside:
        raise NotebookLinkError("That notebook is outside this project.", 404)
    try:
        data = _read_below(directory, parts, os.stat(notebook))
        text = data.decode("utf-8")
    except FileNotFoundError:
        raise NotebookLinkError("There is no such notebook.", 404) from None
    except (OSError, UnicodeDecodeError):
        raise NotebookLinkError(
            "That notebook is not a readable regular file; links are not followed.",
            404,
        ) from None
    return target, text.replace("\r\n", "\n")


def project_root(notebook: Path) -> tuple[str, str | None]:
    """Name the project a notebook belongs to: its kind and resolved root."""
    project = describe_project(notebook)
    root = project["root"]
    return project["kind"], str(Path(root).resolve()) if root else None
