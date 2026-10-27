"""Images in prose, read only from the notebook's ``assets`` folder.

A prose line that is just Markdown image syntax shows an image::

    ![Phase portrait](assets/phase.png)

Lean ignores it and documentation tools render it. The file must be a relative
path inside the ``assets`` folder next to the notebook, with an image suffix.
The preview reports each referenced image with its current sha256 and serves
its bytes only at that hash, so the page never shows a version it did not
announce. An image is an illustration: it is not an input to checking, saved
results or the environment fingerprint, and it grants nothing.
"""

import hashlib
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import NotRequired, TypedDict

FOLDER = "assets"
#: Image types served, by suffix.
TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
#: Largest image shown; the editor's request bridge accepts 20 MB responses.
MAX_ASSET_BYTES = 10 * 1024 * 1024
# The renderer shows an image only when it is the whole (trimmed) line.
_IMAGE = re.compile(r"!\[[^\]]*\]\((\S+)\)")
_MAX_DIGESTS = 256


class AssetError(ValueError):
    """An image reference that cannot be shown, with a ``status`` word.

    The status is ``invalid``, ``missing``, ``too_large``, ``changed`` or
    ``unreferenced``.
    """

    def __init__(self, status: str, message: str) -> None:
        """Keep the status beside the human-readable message."""
        super().__init__(message)
        self.status = status


def references(markdowns: Iterable[str]) -> tuple[str, ...]:
    """Return every image path the prose shows, in order, once each."""
    found: list[str] = []
    for text in markdowns:
        for line in text.split("\n"):
            match = _IMAGE.fullmatch(line.strip())
            if match is not None:
                found.append(match[1])
    return tuple(dict.fromkeys(found))


def resolve(notebook_path: Path, reference: str) -> Path:
    """Return the real file an image reference names.

    Raises:
        AssetError: ``invalid`` if the reference is not a relative image path
            inside the assets folder, or a symbolic link leads out of it.
    """
    return _locate(notebook_path, reference)[1]


def _locate(notebook_path: Path, reference: str) -> tuple[Path, Path]:
    """Return the real assets folder and the real file inside it."""
    if any(ord(c) < 32 or ord(c) == 127 for c in reference):
        raise AssetError("invalid", f"{reference!r} contains control characters")
    path = Path(reference)
    parts = path.parts
    if (
        "\\" in reference
        or path.is_absolute()
        or len(parts) < 2
        or parts[0] != FOLDER
        or any(part in ("..", ".", "") for part in parts)
    ):
        raise AssetError(
            "invalid", f"{reference!r} is not a file in the {FOLDER}/ folder"
        )
    if path.suffix.lower() not in TYPES:
        raise AssetError("invalid", f"{reference!r} is not a supported image type")
    try:
        home = notebook_path.parent.resolve()
        if (home / FOLDER).is_symlink():
            raise AssetError("invalid", f"the {FOLDER}/ folder is a symbolic link")
        folder = (home / FOLDER).resolve()
        target = (home / reference).resolve()
    except (OSError, RuntimeError, ValueError):
        # The error text would name local absolute paths; keep it out.
        raise AssetError("invalid", f"{reference!r} cannot be resolved") from None
    if folder not in target.parents:
        raise AssetError("invalid", f"{reference!r} leaves the {FOLDER}/ folder")
    if target.suffix.lower() not in TYPES:
        raise AssetError(
            "invalid", f"{reference!r} links to a file that is not an image"
        )
    return folder, target


class AssetStatus(TypedDict):
    """One image the prose references: found with its digest, or why it is not shown."""

    path: str
    status: str
    sha256: NotRequired[str]
    message: NotRequired[str]


def statuses(notebook_path: Path, found: Iterable[str]) -> list[AssetStatus]:
    """Report each referenced image.

    Each entry has ``path`` and ``status``: ``ok`` with the file's ``sha256``,
    or ``missing``, ``invalid`` or ``too_large`` with a ``message``.
    """
    report: list[AssetStatus] = []
    for reference in found:
        try:
            digest = _digest(*_locate(notebook_path, reference), reference)
        except AssetError as error:
            report.append(
                {"path": reference, "status": error.status, "message": str(error)}
            )
            continue
        except (OSError, ValueError):  # never let one image break the snapshot
            report.append(
                {
                    "path": reference,
                    "status": "invalid",
                    "message": f"{reference!r} cannot be read",
                }
            )
            continue
        report.append({"path": reference, "status": "ok", "sha256": digest})
    return report


def read(
    notebook_path: Path, found: Iterable[str], reference: str, sha256: str
) -> tuple[bytes, str]:
    """Return a referenced image's bytes and media type at the expected hash.

    Args:
        notebook_path: The notebook's ``.lean`` file.
        found: The image paths its prose refers to.
        reference: The image's path as the prose writes it.
        sha256: The version the page expects; other bytes are refused, so a
            file changed after the page looked is never shown as that one.

    Raises:
        AssetError: If the prose does not refer to it, or it is invalid,
            missing, too large or not the expected version.
    """
    if reference not in tuple(found):
        raise AssetError(
            "unreferenced", f"the notebook does not refer to {reference!r}"
        )
    folder, target = _locate(notebook_path, reference)
    body = _read(folder, target, reference)
    if hashlib.sha256(body).hexdigest() != sha256:
        raise AssetError(
            "changed", f"{reference} changed since the preview read it; reload"
        )
    return body, TYPES[target.suffix.lower()]


def _read(folder: Path, target: Path, reference: str) -> bytes:
    """Read a bounded regular file, refusing symbolic links on the way.

    `target` was resolved inside `folder`. Each directory below the folder and
    the file itself is opened relative to its parent without following links,
    so a directory swapped for a link after resolving cannot lead elsewhere.
    """
    nofollow = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    opened: list[int] = []
    try:
        *directories, name = target.relative_to(folder).parts
        opened.append(os.open(folder, nofollow | os.O_DIRECTORY))
        for part in directories:
            opened.append(os.open(part, nofollow | os.O_DIRECTORY, dir_fd=opened[-1]))
        opened.append(os.open(name, nofollow | os.O_NONBLOCK, dir_fd=opened[-1]))
        info = os.fstat(opened[-1])
        if not stat.S_ISREG(info.st_mode):
            raise AssetError("invalid", f"{reference} is not a regular file")
        if info.st_size > MAX_ASSET_BYTES:
            raise AssetError("too_large", _too_large(reference))
        chunks: list[bytes] = []
        remaining = MAX_ASSET_BYTES + 1
        while remaining > 0:
            chunk = os.read(opened[-1], min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except FileNotFoundError:
        raise AssetError("missing", f"{reference} is missing") from None
    except OSError as error:
        reason = error.strerror or "error"
        raise AssetError("invalid", f"{reference} cannot be read ({reason})") from None
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    body = b"".join(chunks)
    if len(body) > MAX_ASSET_BYTES:
        raise AssetError("too_large", _too_large(reference))
    return body


def _too_large(reference: str) -> str:
    return f"{reference} is too large to show (limit {MAX_ASSET_BYTES:,} bytes)"


#: Digests by file identity, size and modification time, so polling the
#: snapshot does not re-read unchanged images.
_DIGESTS: dict[tuple[str, int, int, int], str] = {}


def _digest(folder: Path, target: Path, reference: str) -> str:
    try:
        info = os.lstat(target)
    except OSError:
        info = None
    key = (
        (str(target), info.st_ino, info.st_size, info.st_mtime_ns)
        if info is not None and stat.S_ISREG(info.st_mode)
        else None
    )
    if key is not None and key in _DIGESTS and key[2] <= MAX_ASSET_BYTES:
        return _DIGESTS[key]
    digest = hashlib.sha256(_read(folder, target, reference)).hexdigest()
    if key is not None:
        if len(_DIGESTS) >= _MAX_DIGESTS:
            _DIGESTS.clear()
        _DIGESTS[key] = digest
    return digest
