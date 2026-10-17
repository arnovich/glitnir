"""The CI-only proof cache refuses changed bytes and unsafe archive members."""

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools/restore_lean_build_cache.py"
_SPEC = importlib.util.spec_from_file_location("restore_lean_build_cache", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
cache = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cache)


def _archive(tmp_path: Path, extra: str = "", link: bool = False) -> tuple[Path, str]:
    """Make a tiny archive with the real allowlisted names and optional attack."""
    path = tmp_path / "cache.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        for index, name in enumerate(sorted(cache.MEMBERS)):
            member = tarfile.TarInfo(name)
            data = name.encode()
            member.size = len(data)
            if link and index == 0:
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                member.size = 0
            bundle.addfile(member, io.BytesIO(data))
        if extra:
            bundle.addfile(tarfile.TarInfo(extra))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_restore_preserves_only_audited_outputs(tmp_path: Path) -> None:
    """Valid members are restored with their exact bytes."""
    archive, digest = _archive(tmp_path)
    cache.restore_cache(archive, tmp_path, digest)
    for name in cache.MEMBERS:
        assert (tmp_path / ".lake/build" / name).read_bytes() == name.encode()


def test_checksum_precedes_any_writes(tmp_path: Path) -> None:
    """Altered archives cannot populate even the build directory."""
    archive, _ = _archive(tmp_path)
    with pytest.raises(ValueError, match="checksum"):
        cache.restore_cache(archive, tmp_path, "0" * 64)
    assert not (tmp_path / ".lake").exists()


@pytest.mark.parametrize("extra", ["../escape", "/absolute", "bin/Checker"])
def test_unexpected_members_are_rejected(tmp_path: Path, extra: str) -> None:
    """Traversal and unreviewed outputs are rejected before extraction."""
    archive, digest = _archive(tmp_path, extra)
    with pytest.raises(ValueError, match="exactly"):
        cache.restore_cache(archive, tmp_path, digest)
    assert not (tmp_path / ".lake").exists()


def test_archive_symlink_is_rejected(tmp_path: Path) -> None:
    """An allowlisted member name cannot hide a link."""
    archive, digest = _archive(tmp_path, link=True)
    with pytest.raises(ValueError, match="exactly"):
        cache.restore_cache(archive, tmp_path, digest)
    assert not (tmp_path / ".lake").exists()


def test_destination_symlink_cannot_escape(tmp_path: Path) -> None:
    """An existing cache directory cannot redirect extraction outside checkout."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".lake").symlink_to(outside, target_is_directory=True)
    archive, digest = _archive(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        cache.restore_cache(archive, project, digest)
    assert list(outside.iterdir()) == []
